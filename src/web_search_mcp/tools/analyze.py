"""analyze_urls: lê URLs fornecidas e devolve análise no contexto do MCP.

O complemento do par read_url/research_web: read_url devolve o texto cru
(e uma página grande entope o contexto do agente chamador); research_web
pesquisa sozinho. Aqui as URLs vêm prontas do usuário e o que volta é só a
análise — o conteúdo bruto morre neste processo, como no research_web.
"""

import logging

from .. import config
from ..llm import chat
from ..util.scraper import WebScraper
from .research import _dossier_char_budget

logger = logging.getLogger(__name__)

_scraper = WebScraper(limit=None)

# Máximo de URLs por chamada. Acima disso o orçamento por página fica pequeno
# demais para uma análise honesta — melhor o agente dividir em duas chamadas.
_MAX_URLS = 8

_INSTRUCTION = (
    "Você analisa páginas web a pedido do usuário. Recebe o conteúdo de uma "
    "ou mais páginas e um pedido (resumo, parecer técnico, opinião, "
    "comparação). Responda em português do Brasil, com base apenas no "
    "material fornecido, citando a URL ao afirmar algo específico de uma "
    "página. Ao comparar páginas, organize por critérios claros. Se o pedido "
    "for parecer ou opinião, deixe explícito o que é fato do material e o "
    "que é avaliação sua. Traduza com fidelidade material em outros idiomas, "
    "mantendo nomes próprios e termos técnicos na forma original quando não "
    "houver tradução consagrada. Nunca invente: se o material não sustentar "
    "uma resposta, diga o que faltou."
)


def analyze_urls(urls: list[str], request: str = "Resuma o conteúdo.") -> str:
    """Lê uma ou mais URLs e devolve uma análise pronta, sem o texto bruto.

    Use quando o usuário fornecer a(s) URL(s) e quiser resumo, parecer
    técnico, opinião ou comparação entre páginas: a leitura e a análise
    acontecem internamente e só o resultado volta — o conteúdo integral das
    páginas não entra no seu contexto. Prefira read_url apenas quando o
    texto completo da página for necessário de verdade.

    Args:
        urls: 1 a 8 URLs completas (http/https), na ordem em que devem ser
            referidas. Para comparação, passe todas na mesma chamada.
        request: O pedido em linguagem natural, como o usuário fez ("resuma",
            "dê um parecer técnico sobre a proposta", "compare os dois
            produtos e recomende um"). Vazio = resumo.
    """
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        return "Nenhuma URL fornecida."
    if len(urls) > _MAX_URLS:
        return (
            f"São {len(urls)} URLs; o máximo por chamada é {_MAX_URLS}. "
            "Divida em mais de uma chamada."
        )
    logger.info("analyze_urls: %d url(s), request=%r", len(urls), request)

    pages = _scraper.read_many_located(urls)

    # Orçamento repartido por página: N páginas gigantes precisam caber
    # juntas na janela; o corte por página preserva o começo de todas em vez
    # de deixar a última página de fora inteira.
    per_page = max(_dossier_char_budget() // len(urls) - 256, 1000)

    read_ok: list[str] = []
    failed: list[tuple[str, str]] = []
    blocks: list[str] = []
    for url, (page, final) in zip(urls, pages):
        if WebScraper.failed(page):
            failed.append((url, page))
            continue
        read_ok.append(url)
        # A URL entregue vale mais que a pedida: citar o endereço que o
        # servidor abandonou num 3xx é atribuir o texto a uma página que não
        # o contém.
        header = f"URL: {final}"
        if WebScraper.redirected(url, final):
            header += f" (redirecionada de {url}, que não existe mais)"
        blocks.append(f"{header}\nConteúdo:\n{page[:per_page]}")

    if not read_ok:
        details = "\n".join(f"- {u}: {m}" for u, m in failed)
        return f"Nenhuma das URLs pôde ser lida:\n{details}"

    dossier = "\n\n---\n\n".join(blocks)
    if failed:
        dossier += "\n\n---\n\nPáginas que FALHARAM (não usar, apenas relatar):\n" + "\n".join(
            f"- {u}: {m}" for u, m in failed
        )

    analysis = chat(
        system=_INSTRUCTION,
        user=f"Pedido: {request or 'Resuma o conteúdo.'}\n\nMaterial:\n\n{dossier}",
    )

    sources = "\n".join(f"- {u}" for u in read_ok)
    tail = f"\n\nURLs analisadas:\n{sources}"
    if failed:
        tail += "\n\nURLs que falharam:\n" + "\n".join(f"- {u}" for u, _ in failed)
    return analysis + tail
