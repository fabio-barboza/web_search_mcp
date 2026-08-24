"""Chamada de chat completion numa API compatível com OpenAI, via `requests`."""

import logging

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


def chat(system: str, user: str, temperature: float | None = None) -> str:
    """Chamada de chat completion numa API compatível com OpenAI."""
    model = _resolve_model()
    if config.EXTRA_SYSTEM_PROMPT:
        system = f"{system}\n\n{config.EXTRA_SYSTEM_PROMPT}"
    payload = {
        "model": model,
        "temperature": temperature if temperature is not None else config.MODEL_TEMPERATURE,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if config.EXTRA_BODY:
        payload.update(config.EXTRA_BODY)
    try:
        r = requests.post(
            f"{config.MODEL_BASE_URL}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {config.MODEL_API_KEY}"},
            timeout=config.MODEL_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        # O corpo diz o que o status esconde ("context length exceeded", nome
        # de modelo errado, param recusado). Sem ele um 400 é indepurável.
        body = getattr(e.response, "text", "")[:500] if e.response is not None else ""
        logger.error(
            "chat: falha ao chamar %s (model=%s, prompt=%d chars): %s %s",
            config.MODEL_BASE_URL, model, len(system) + len(user), e, body,
        )
        raise
    return r.json()["choices"][0]["message"]["content"]
