"""Chamada de chat completion numa API compatível com OpenAI, via `requests`."""

import json
import logging
import time

import requests

from . import config

logger = logging.getLogger(__name__)

def _list_models() -> list[dict]:
    """GET /models do provider. Levanta em falha de rede/HTTP."""
    resp = requests.get(f"{config.MODEL_BASE_URL}/models", timeout=config.MODEL_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", [])


def _resolve_model(items: list[dict] | None = None) -> str:
    """Devolve o nome do modelo a usar.

    MODEL preenchido no .env força um modelo específico, sem bater na rede.
    MODEL vazio detecta o modelo já carregado no servidor via GET /models,
    para nunca forçar troca — mas de forma agnóstica de provider: só existem
    "status" de load/unload em routers tipo llama.cpp/llama-swap; providers
    OpenAI-compat genéricos (vLLM, OpenAI, etc.) não expõem isso, então nesse
    caso caímos no único modelo da lista, ou pedimos para o usuário nomear.

    Sem cache: reconsulta a cada chamada. Um cache preso ao processo detecta
    o modelo certo uma vez e insiste nele depois, mesmo que o usuário troque
    de modelo no client (ex. webui) — daí o MCP força reload do modelo velho
    a cada tool call, brigando com o client pelo slot do router.
    """
    if config.MODEL:
        return config.MODEL

    if items is None:
        try:
            items = _list_models()
        except requests.RequestException as e:
            logger.error("_resolve_model: falha ao consultar %s/models: %s", config.MODEL_BASE_URL, e)
            raise

    loaded = []
    for item in items:
        status = item.get("status")
        # Formato do llama.cpp router: {"status": {"value": "loaded"}}.
        # Outros providers podem não ter "status" nenhum — o .get cobre isso.
        value = status.get("value") if isinstance(status, dict) else status
        if value in ("loaded", "ready"):
            loaded.append(item["id"])

    if loaded:
        resolved = loaded[0]
        # Com um modelo residente a escolha é óbvia; com vários ela é a ordem
        # da lista, que não quer dizer nada. Avisar porque o sintoma é mudo:
        # a pesquisa continua funcionando, só que no modelo errado.
        if len(loaded) > 1:
            logger.warning(
                "_resolve_model: %d modelos carregados (%s); usando o primeiro: %s. "
                "Defina MODEL no .env para escolher.",
                len(loaded), ", ".join(loaded), resolved,
            )
        else:
            logger.info("_resolve_model: detectado modelo carregado: %s", resolved)
        return resolved

    if len(items) == 1:
        resolved = items[0]["id"]
        logger.info("_resolve_model: único modelo disponível: %s", resolved)
        return resolved

    logger.error(
        "_resolve_model: não deu para detectar o modelo carregado em %s/models "
        "(sem status de load e mais de um modelo na lista)",
        config.MODEL_BASE_URL,
    )
    raise RuntimeError(
        "Não deu para detectar qual modelo está carregado em "
        f"{config.MODEL_BASE_URL}/models (provider não expõe status de "
        "load, e há mais de um modelo na lista). Defina MODEL no .env."
    )


def context_tokens() -> int:
    """Janela de contexto (em tokens) do modelo em uso.

    Routers tipo llama.cpp/llama-swap expõem em /models os args com que o
    modelo subiu (status.args, incluindo --ctx-size). Quando esse dado
    existe, ele vale mais que o MODEL_CONTEXT_TOKENS do .env: o valor do
    .env é declarado à mão e dessincroniza quando o usuário troca de modelo
    no router — para baixo desperdiça janela, para cima mata a chamada com
    HTTP 400 depois de a busca e o scraping já terem sido pagos.

    Nunca levanta: qualquer falha (provider sem status.args, rede fora)
    cai no valor do .env. Sem cache, pela mesma razão do _resolve_model —
    o modelo carregado pode mudar entre chamadas.
    """
    try:
        items = _list_models()
        model = _resolve_model(items)
    except Exception as e:
        logger.info("context_tokens: sem detecção via /models (%s); usando MODEL_CONTEXT_TOKENS=%d",
                    e, config.MODEL_CONTEXT_TOKENS)
        return config.MODEL_CONTEXT_TOKENS

    for item in items:
        if item.get("id") != model:
            continue
        status = item.get("status")
        args = status.get("args") if isinstance(status, dict) else None
        if isinstance(args, list) and "--ctx-size" in args:
            try:
                detected = int(args[args.index("--ctx-size") + 1])
            except (IndexError, ValueError):
                break
            if detected > 0:
                logger.info("context_tokens: detectado ctx-size=%d do modelo %s", detected, model)
                return detected
        break
    return config.MODEL_CONTEXT_TOKENS


# Geração em repetição: o modelo entra num ciclo e reescreve o mesmo trecho
# até alguém cortar. Medido em 07/10/2026 no qwen3.6:35B, duas vezes: 175 mil
# tokens de raciocínio numa triagem (temperatura 0) e a mesma linha 432 vezes
# num resumo (temperatura 0.6, 84 s até o teto de tokens). Temperatura não
# evita, prazo e teto só limitam o estrago; o que corta em segundos é ver a
# repetição no próprio stream. Um bloco de até _LOOP_MAX_PERIOD linhas que
# aparece _LOOP_REPEATS vezes seguidas é ciclo. O piso de caracteres deixa de
# fora o que repete por natureza (um número por linha, separador de tabela).
_LOOP_REPEATS = 4
_LOOP_MAX_PERIOD = 8
_LOOP_MIN_BLOCK_CHARS = 20


def _reasoning_settings(model: str) -> tuple[bool, float | None, dict, int]:
    """(liga?, temperatura, body, teto de tokens) do raciocínio para o modelo.

    Tabela do modelo no MODELS_FILE, por cima do [default], por cima das
    variáveis de ambiente (ver config).
    """
    settings = {**config.MODEL_SETTINGS.get("default", {}), **config.MODEL_SETTINGS.get(model.lower(), {})}
    return (
        settings.get("reasoning", config.USE_REASONING),
        settings.get("temperature", config.REASONING_TEMPERATURE),
        settings.get("body", config.REASONING_BODY),
        settings.get("reasoning_budget_tokens", config.REASONING_BUDGET_TOKENS),
    )


class GenerationLoop(RuntimeError):
    """O modelo entrou em repetição antes de produzir uma resposta."""


class _LoopDetector:
    """Acompanha um texto em streaming e acusa quando ele vira um ciclo de linhas."""

    def __init__(self):
        self._lines: list[str] = []
        self._ends: list[int] = []  # posição, no texto todo, do fim de cada linha guardada
        self._pending = ""
        self._seen = 0

    def feed(self, piece: str) -> int | None:
        """Devolve onde cortar o texto (fim da 1ª ocorrência do bloco) ao ver um ciclo."""
        self._pending += piece
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._seen += len(line) + 1
            line = line.strip()
            if not line:
                continue
            self._lines.append(line)
            self._ends.append(self._seen)
            cut = self._cycle()
            if cut is not None:
                return cut
        return None

    def _cycle(self) -> int | None:
        lines = self._lines
        for period in range(1, _LOOP_MAX_PERIOD + 1):
            span = period * _LOOP_REPEATS
            if len(lines) < span:
                break
            block = lines[-period:]
            if sum(len(line) for line in block) < _LOOP_MIN_BLOCK_CHARS:
                continue
            if all(lines[-(i + 1) * period:len(lines) - i * period] == block for i in range(1, _LOOP_REPEATS)):
                return self._ends[len(lines) - span + period - 1]
        return None


def chat(system: str, user: str, temperature: float | None = None, reasoning: bool = False) -> str:
    """Chamada de chat completion numa API compatível com OpenAI.

    reasoning=True liga o raciocínio como o modelo em uso pede (ver
    _reasoning_settings): o body dele vai por cima do EXTRA_BODY, com o teto
    de tokens de raciocínio, e a chamada usa a temperatura de raciocínio, a
    não ser que temperature venha explícita. Pedido só pelas chamadas que
    decidem a qualidade da resposta (ver config).

    A resposta é lida em streaming e a chamada inteira tem prazo de
    config.MODEL_TIMEOUT: estourou, a conexão é fechada (o que cancela a
    geração no servidor) e sobe requests.Timeout. O mesmo corte acontece
    quando o texto vira repetição: na resposta, volta o que veio antes do
    ciclo; no raciocínio não há resposta para devolver e sobe GenerationLoop.
    """
    model = _resolve_model()
    if config.EXTRA_SYSTEM_PROMPT:
        system = f"{system}\n\n{config.EXTRA_SYSTEM_PROMPT}"
    enabled, reasoning_temperature, reasoning_body, budget = _reasoning_settings(model)
    thinking = bool(reasoning and enabled and reasoning_body)
    if temperature is None:
        temperature = config.MODEL_TEMPERATURE
        if thinking and reasoning_temperature is not None:
            temperature = reasoning_temperature
    payload = {
        "model": model,
        "temperature": temperature,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if config.MODEL_MAX_TOKENS > 0:
        payload["max_tokens"] = config.MODEL_MAX_TOKENS
    if config.EXTRA_BODY:
        payload.update(config.EXTRA_BODY)
    if thinking:
        payload.update(reasoning_body)
        if budget > 0:
            # O body do modelo manda: quem definiu o próprio teto fica com ele.
            payload.setdefault("reasoning_budget_tokens", budget)
    prompt_chars = len(system) + len(user)
    started = time.monotonic()
    try:
        with requests.post(
            f"{config.MODEL_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config.MODEL_API_KEY}"},
            timeout=config.MODEL_TIMEOUT,
            stream=True,
        ) as r:
            if not r.ok:
                r.content  # carrega o corpo antes de a conexão fechar: o log do erro precisa dele
            r.raise_for_status()
            content, reasoning_chars, finish = _read_stream(r, started + config.MODEL_TIMEOUT)
    except GenerationLoop as e:
        logger.error(
            "chat: falha ao chamar %s (model=%s, prompt=%d chars, %.1fs): %s",
            config.MODEL_BASE_URL, model, prompt_chars, time.monotonic() - started, e,
        )
        raise
    except requests.RequestException as e:
        # O corpo diz o que o status esconde ("context length exceeded", nome
        # de modelo errado, param recusado). Sem ele um 400 é indepurável.
        body = getattr(e.response, "text", "")[:500] if e.response is not None else ""
        logger.error(
            "chat: falha ao chamar %s (model=%s, prompt=%d chars, %.1fs): %s %s",
            config.MODEL_BASE_URL, model, prompt_chars, time.monotonic() - started, e, body,
        )
        raise
    # Uma linha por chamada: é daqui que sai onde o tempo da pesquisa vai
    # (raciocínio x resposta) na hora de calibrar por modelo.
    logger.info(
        "chat: model=%s raciocínio=%s temp=%s prompt=%d chars, pensou=%d chars, resposta=%d chars, "
        "fim=%s, %.1fs",
        model, thinking, temperature, prompt_chars, reasoning_chars, len(content), finish,
        time.monotonic() - started,
    )
    if finish == "loop":
        logger.error(
            "chat: resposta em repetição cortada aos %.1fs (model=%s, ficaram %d chars)",
            time.monotonic() - started, model, len(content),
        )
    if finish == "length":
        logger.error(
            "chat: geração cortada no teto de %d tokens (model=%s, pensou=%d chars, resposta=%d chars)",
            config.MODEL_MAX_TOKENS, model, reasoning_chars, len(content),
        )
    return content


def _read_stream(r: requests.Response, deadline: float) -> tuple[str, int, str | None]:
    """Junta os deltas do SSE. Devolve (conteúdo, chars de raciocínio, finish_reason).

    O timeout do requests vale por leitura do socket, não pela resposta toda:
    um modelo em loop manda um token atrás do outro e nunca o dispara. Por
    isso o prazo total é conferido aqui, a cada evento, junto com a repetição
    (ver _LoopDetector).
    """
    if "text/event-stream" not in r.headers.get("Content-Type", ""):
        # Provider que ignora "stream": resposta inteira num JSON só.
        choice = r.json()["choices"][0]
        message = choice["message"]
        return message.get("content") or "", len(message.get("reasoning_content") or ""), choice.get("finish_reason")
    parts: list[str] = []
    reasoning_chars = 0
    finish = None
    answer_loop, reasoning_loop = _LoopDetector(), _LoopDetector()
    for raw in r.iter_lines():
        if time.monotonic() > deadline:
            raise requests.Timeout(f"prazo total de {config.MODEL_TIMEOUT}s estourado no meio da geração")
        if not raw.startswith(b"data:"):
            continue
        data = raw[5:].strip()
        if data == b"[DONE]":
            break
        choices = json.loads(data).get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        thought = delta.get("reasoning_content") or ""
        reasoning_chars += len(thought)
        if thought and reasoning_loop.feed(thought) is not None:
            raise GenerationLoop(f"raciocínio em repetição após {reasoning_chars} chars")
        if delta.get("content"):
            parts.append(delta["content"])
            cut = answer_loop.feed(delta["content"])
            if cut is not None:
                # Sair do laço fecha a conexão (o `with` de quem chamou) e o
                # servidor para de gerar.
                return "".join(parts)[:cut].rstrip(), reasoning_chars, "loop"
        finish = choices[0].get("finish_reason") or finish
    return "".join(parts), reasoning_chars, finish
