"""Chamada de chat completion numa API compatível com OpenAI, via `requests`."""

import logging

import requests

from . import config

logger = logging.getLogger(__name__)

def _resolve_model() -> str:
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

    try:
        resp = requests.get(f"{config.MODEL_BASE_URL}/models", timeout=config.MODEL_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error("_resolve_model: falha ao consultar %s/models: %s", config.MODEL_BASE_URL, e)
        raise
    items = resp.json().get("data", [])

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
