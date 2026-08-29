import logging

from ..util.scraper import WebScraper

logger = logging.getLogger(__name__)

# limit=None: URL pedida explicitamente é lida por inteiro, sem corte.
_scraper = WebScraper(limit=None)


def read_url(url: str) -> str:
    """Abre uma URL específica e devolve o conteúdo principal da página.

    UMA página por chamada, e o texto bruto inteiro entra no seu contexto.
    Se você precisa ler VÁRIAS páginas, use analyze_urls (aceita até 8 de
    uma vez, lê todas, e devolve só a análise) — encadear read_url gasta
    contexto e tempo à toa.

    O texto volta sem atribuição montada: é você que fica responsável por
    dizer de qual URL saiu cada afirmação. Quando o que você quer são fatos
    com fonte verificável, research_web e analyze_urls já devolvem o link
    junto de cada item; read_url não.

    Use quando o usuário fornecer um link e pedir para você ler, resumir,
    analisar ou extrair algo dele. Diferente de research_web, aqui não há
    busca nem resumo interno: a página é lida e o texto bruto (em Markdown)
    volta para você processar conforme o que foi pedido.

    Args:
        url: O endereço completo da página (http/https).
    """
    logger.info("read_url chamada: url=%s", url)
    text = _scraper.read(url)
    if WebScraper.failed(text):
        logger.error("read_url falhou: url=%s motivo=%s", url, text)
    else:
        logger.info("read_url ok: url=%s tamanho=%d", url, len(text))
    return text
