import logging
from urllib.parse import urlsplit

from ..util.scraper import WebScraper

logger = logging.getLogger(__name__)

# limit=None: URL pedida explicitamente é lida por inteiro, sem corte.
_scraper = WebScraper(limit=None)

# O texto cru não carrega de onde veio, e o agente escreve a resposta muito
# depois de ter lido — em cadeia longa, a URL já saiu do alcance dele.
# Observado em 29/08/2026: 21 leituras por read_url e a resposta final citou
# "ACL policy examples - Tailscale Docs", nome de página sem link nenhum. Não
# foi desobediência: não havia link no material para preservar. O research_web
# monta a atribuição por código; aqui ela precisa vir junto do texto, senão
# este caminho é um buraco por onde a regra escapa.
_SOURCE_HEADER = (
    "Fonte desta página: {url}\n"
    "Link pronto para citar: [{host}]({url})\n"
    "Ao usar qualquer fato daqui na sua resposta, mantenha esse link junto do "
    "fato. Marcador sem link, ou nome de página sem endereço, é referência que "
    "o usuário não consegue conferir.\n\n"
)


def _source_header(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return _SOURCE_HEADER.format(url=url, host=host.removeprefix("www.") or url)


def read_url(url: str) -> str:
    """Abre uma URL específica e devolve o conteúdo principal da página.

    UMA página por chamada, e o texto bruto inteiro entra no seu contexto.
    Se você precisa ler VÁRIAS páginas, use analyze_urls (aceita até 8 de
    uma vez, lê todas, e devolve só a análise) — encadear read_url gasta
    contexto e tempo à toa.

    O texto vem com um cabeçalho "Fonte desta página" e um link markdown
    pronto. Use esse link ao citar qualquer coisa que tenha lido aqui: nome
    de página sem endereço não é fonte, é referência que o usuário não
    consegue conferir.

    Use quando o usuário fornecer um link e pedir para você ler, resumir,
    analisar ou extrair algo dele. Diferente de research_web, aqui não há
    busca nem resumo interno: a página é lida e o texto bruto (em Markdown)
    volta para você processar conforme o que foi pedido.

    Args:
        url: O endereço completo da página (http/https).
    """
    logger.info("read_url chamada: url=%s", url)
    text, final = _scraper.read_many_located([url])[0]
    if WebScraper.failed(text):
        logger.error("read_url falhou: url=%s motivo=%s", url, text)
        return text
    notice = WebScraper.redirect_notice(url, final)
    if notice:
        logger.warning("read_url redirecionada: pedida=%s final=%s", url, final)
    logger.info("read_url ok: url=%s tamanho=%d", final, len(text))
    # O cabeçalho usa a URL final: citar o endereço que o servidor abandonou
    # num 3xx é atribuir o texto a uma página que não o contém.
    return notice + _source_header(final) + text
