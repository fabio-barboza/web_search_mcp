"""Única fonte de configuração do projeto. Todo `os.getenv` mora aqui."""

import json
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()

# --- Log ---

# DEBUG | INFO | ERROR. Valor inválido cai em INFO. logging.basicConfig sem
# `stream` usa StreamHandler(), que aponta pro stderr por padrão — em stdio
# nada pode ir pro stdout, então não passamos stream explícito aqui.
_LOG_LEVEL_NAME = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# --- Modelo (API compatível com OpenAI) ---

# Vazio ("") = detectar em runtime o modelo já carregado no servidor, sem
# forçar troca (ver llm._resolve_model — feito lá, não aqui, porque essa
# detecção bate na rede e config.py não pode falhar no import: read_url não
# usa LLM nenhum e não pode morrer por causa de config de modelo). Preencha
# MODEL só para forçar um modelo específico.
MODEL = os.getenv("MODEL") or ""
MODEL_BASE_URL = os.getenv("MODEL_BASE_URL", "http://localhost:8200/v1")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "not-needed")
MODEL_TIMEOUT = int(os.getenv("MODEL_TIMEOUT", "120"))
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0"))

# Janela de contexto do modelo, em tokens. Precisa bater com o que o servidor
# subiu (--ctx-size), porque é daqui que sai o orçamento do dossiê: estourar
# não degrada nada, o provider recusa a chamada inteira com HTTP 400 e a
# pesquisa se perde depois de já ter pago busca e scraping.
MODEL_CONTEXT_TOKENS = int(os.getenv("MODEL_CONTEXT_TOKENS", "65536"))

# Quanto da janela fica reservado para o que NÃO é dossiê: as instruções, a
# pergunta e a resposta que o modelo ainda vai gerar. O resumo mede ~700
# tokens; o resto é folga para a imprecisão da estimativa de tokens.
MODEL_RESERVE_TOKENS = int(os.getenv("MODEL_RESERVE_TOKENS", "4096"))

# EXTRA_BODY: JSON cru, mesclado no payload do /chat/completions. Genérico
# entre providers (não é um único campo fixo tipo "enable_thinking") porque
# cada modelo pede uma chave diferente — ex. Qwen3:
# EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}
_extra_body_raw = os.getenv("EXTRA_BODY", "").strip()
if _extra_body_raw:
    try:
        EXTRA_BODY = json.loads(_extra_body_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"EXTRA_BODY não é JSON válido: {e}") from e
else:
    EXTRA_BODY = {}

# EXTRA_SYSTEM_PROMPT: texto apenso ao final do system prompt em toda chamada.
# Não cabe em EXTRA_BODY porque não é payload da API, é conteúdo de mensagem
# — ex. muse-glimmer: EXTRA_SYSTEM_PROMPT="Reasoning strength: low"
EXTRA_SYSTEM_PROMPT = os.getenv("EXTRA_SYSTEM_PROMPT", "").strip()

# --- SearXNG ---

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8886")
SEARXNG_MAX_RESULTS = int(os.getenv("SEARXNG_MAX_RESULTS", "10"))
SEARXNG_TIMEOUT = int(os.getenv("SEARXNG_TIMEOUT", "10"))
# "auto": o SearXNG detecta o idioma pelo texto da própria query. Fixar
# pt-BR empurrava toda busca para página brasileira, inclusive as queries em
# inglês que _generate_queries produz — medido em 10/09/2026 no google cse,
# 6 assuntos sem relação entre si (Python, Raft, vitamina B12, BG3,
# Tailscale, pão de fermentação natural): pt-BR 24/51 resultados com o
# assunto no título/URL, auto 46/60. As queries em português ficaram iguais.
SEARXNG_LANGUAGE = os.getenv("SEARXNG_LANGUAGE", "auto")
SEARXNG_CATEGORIES = os.getenv("SEARXNG_CATEGORIES", "general,news")  # string, vai direto no param

# --- Fonte de links: Google CSE via Tor (plans/tor.md) ---


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "sim")


def _parse_tor_channels(raw: str) -> list[tuple[str, int, int]]:
    """"host:socks:controle,host:socks:controle" -> [(host, socks, controle)]."""
    channels = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            host, socks_port, control_port = item.rsplit(":", 2)
            channels.append((host, int(socks_port), int(control_port)))
        except ValueError as e:
            raise ValueError(
                f"TOR_CHANNELS: {item!r} não está no formato host:porta_socks:porta_controle"
            ) from e
    return channels


# google_tor: Google CSE pelos canais Tor, com CSE direto e SearXNG de
# reserva. searxng: só o SearXNG, como antes — rollback é trocar e reiniciar.
SEARCH_BACKEND = os.getenv("SEARCH_BACKEND", "google_tor").strip().lower()
if SEARCH_BACKEND not in ("google_tor", "searxng"):
    raise ValueError(f"SEARCH_BACKEND={SEARCH_BACKEND!r}: use google_tor ou searxng")

TOR_CHANNELS = _parse_tor_channels(
    os.getenv("TOR_CHANNELS", "127.0.0.1:9060:9061,127.0.0.1:9070:9071")
)
# Vazia não quebra a busca: sem ela o NEWNYM fica desligado e o canal barrado
# troca de circuito só pela credencial SOCKS nova (IsolateSOCKSAuth), que já
# basta. Os containers do search-engine/ é que não sobem sem ela.
TOR_CONTROL_PASSWORD = os.getenv("TOR_CONTROL_PASSWORD", "")

# CX público do blackle.com, o mesmo do motor google cse do SearXNG.
GOOGLE_CSE_CX = os.getenv("GOOGLE_CSE_CX", "partner-pub-8993703457585266:4862972284")
# Idioma da interface, não do resultado: sem `lr`, nada restringe a língua das
# páginas (mesma lição do SEARXNG_LANGUAGE=auto acima).
GOOGLE_CSE_HL = os.getenv("GOOGLE_CSE_HL", "pt-BR")
# Por requisição. Medido em 10/09/2026, 8 circuitos novos: 2-4 s por
# requisição via Tor, contando a montagem do circuito.
GOOGLE_CSE_TIMEOUT = int(os.getenv("GOOGLE_CSE_TIMEOUT", "15"))
GOOGLE_CSE_DIRECT_FALLBACK = _env_bool("GOOGLE_CSE_DIRECT_FALLBACK", True)
SEARXNG_FALLBACK = _env_bool("SEARXNG_FALLBACK", True)

# --- Scraper ---

SCRAPER_TIMEOUT = int(os.getenv("SCRAPER_TIMEOUT", "6"))

# SCRAPER_LIMIT vazio = None = lê a página inteira (text[:None] devolve tudo).
SCRAPER_LIMIT = int(v) if (v := os.getenv("SCRAPER_LIMIT", "").strip()) else None

# --- Research (calibragem medida em produção) ---

# Quantas páginas ENTRAM no dossiê, somando todas as buscas.
RESEARCH_PAGE_BUDGET = int(os.getenv("RESEARCH_PAGE_BUDGET", "5"))

# Reserva de links. Link morto, bloqueado ou sem texto não gasta vaga do
# orçamento: cede o lugar para o próximo da reserva.
RESEARCH_POOL_SIZE = int(os.getenv("RESEARCH_POOL_SIZE", "20"))

# Teto de tentativas. Sem ele, uma sequência ruim de links varreria a
# reserva inteira e estouraria a latência.
RESEARCH_MAX_WAVES = int(os.getenv("RESEARCH_MAX_WAVES", "4"))

# Máximo de URLs do MESMO domínio na reserva de candidatos. Sem teto, uma
# busca cujo top-10 é todo de um site enche o dossiê com um veículo só.
# 0 = sem limite.
RESEARCH_MAX_PER_DOMAIN = int(os.getenv("RESEARCH_MAX_PER_DOMAIN", "2"))

# Teto de caracteres POR PÁGINA no dossiê (só no research; o read_url segue
# lendo a página inteira, porque lá a URL foi pedida de propósito).
#
# Sem teto o dossiê é ilimitado: o normal é 15-50k caracteres, mas basta uma
# página gigante para estourar. Medido: uma query devolveu 412k caracteres =
# 103k tokens, contra os 65k de contexto do modelo — a chamada morria com
# "exceed_context_size_error" e a pesquisa inteira se perdia.
#
# 25k por página é folgado de propósito: 5 páginas cabem em ~31k tokens, e o
# corte só morde o outlier. Cortar de leve piora a resposta (medido: dossiê
# de 20k caracteres rendeu resumo PIOR e mais lento que o de 27k — com menos
# material o modelo enrola em vez de citar).
RESEARCH_PAGE_CHARS = int(os.getenv("RESEARCH_PAGE_CHARS", "25000"))

# --- Servidor MCP ---

MCP_NAME = os.getenv("MCP_NAME", "web-search")
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8765"))

# CORS do transporte --http, só usado por clientes de navegador (ex. MCP
# Inspector). "*" libera qualquer origem; lista separada por vírgula restringe.
MCP_CORS_ALLOW_ORIGINS = [
    o.strip() for o in os.getenv("MCP_CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()
]

# datetime.now() lê o relógio do host onde o server roda. Em container o
# padrão é UTC: TZ no .env corrige. tzset() é necessário porque o load_dotenv
# escreve em os.environ depois de o processo já ter lido o fuso.
if os.getenv("TZ"):
    time.tzset()

# --- Eval ---

EVAL_JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL") or MODEL
