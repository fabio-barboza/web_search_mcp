import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from .. import config
from ..llm import chat, context_tokens
from ..util.scraper import WebScraper
from ..util.searxng import SearXNG

logger = logging.getLogger(__name__)

_search = SearXNG()
_scraper = WebScraper(limit=config.RESEARCH_PAGE_CHARS)

# Janela usada quando a pergunta pede dado recente.
_RECENT_WINDOW = "day"

_QUERIES_INSTRUCTION = (
    "Você planeja buscas web. Dada uma pergunta, escreva 3 buscas curtas e "
    "DIFERENTES entre si que, juntas, cobrem a resposta por ângulos "
    "distintos. Uma busca por linha, sem numerar, sem explicar. Use os "
    "termos que apareceriam na página procurada, não a pergunta inteira. "
    "Se a pergunta for sobre uma pessoa, inclua buscas que combinem o nome "
    "com onde ela apareceria (github, linkedin, currículo, empresa). "
    "Se o assunto tiver boa cobertura internacional (tecnologia, ciência, "
    "notícia mundial), escreva UMA das buscas em inglês — as fontes "
    "internacionais de referência costumam ter o material mais completo. "
    "Se a pergunta pedir um panorama das notícias do dia, dedique uma busca "
    "só às manchetes gerais (ex.: 'principais manchetes Brasil hoje') e uma "
    "em inglês às internacionais (ex.: 'top world news today') — sem elas o "
    "resultado enviesa para a editoria que calhar de ranquear melhor."
)

_BASE_INSTRUCTION = (
    "Você é um pesquisador web. Recebe uma pergunta e o material coletado "
    "de várias páginas. Extraia APENAS os fatos que respondem à pergunta e "
    "escreva um resumo curto e direto em português do Brasil, citando a URL "
    "da fonte de cada fato e a data/hora do dado quando houver. Ignore "
    "páginas irrelevantes ou que falharam. Nunca invente nada: se o "
    "material não responder, diga exatamente o que faltou. Não copie o "
    "conteúdo bruto das páginas. Fontes em outros idiomas valem tanto "
    "quanto as em português: traduza os fatos delas com fidelidade, "
    "mantendo nomes próprios, siglas e termos técnicos na forma original "
    "quando não houver tradução consagrada."
)

_RECENT_INSTRUCTION = (
    "A pergunta pede um dado atual: prefira sempre a informação mais "
    "recente e diga a que momento ela se refere."
)

_TIMELESS_INSTRUCTION = (
    "A pergunta NÃO depende da data de hoje: não descarte uma fonte por ser "
    "antiga. Material publicado há anos pode ser a resposta correta."
)


def _format_offset(dt: datetime) -> str:
    """"-03" em vez do "-0300" do %z: minutos só aparecem quando != 0."""
    total_minutes = int(dt.utcoffset().total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{sign}{hours:02d}" + (f":{minutes:02d}" if minutes else "")


def _generate_queries(question: str) -> list[str]:
    """Gera variantes de busca. A pergunta original sempre entra primeiro.

    Uma busca só limita o alcance: os motores rankeiam pelo termo exato, e
    ângulos que a pergunta não menciona nunca aparecem (procurar o nome de
    alguém não devolve o GitHub dele; "nome + github" devolve).
    """
    try:
        # A data entra no system porque busca de notícia funciona melhor
        # datada, e o modelo não tem como saber que dia é hoje sozinho.
        today = datetime.now().astimezone().strftime("%d/%m/%Y")
        content = chat(system=f"Data de hoje: {today}. {_QUERIES_INSTRUCTION}", user=question)
        variants = [
            line.strip(" -*\"'")
            for line in content.splitlines()
            if line.strip()
        ]
    except Exception as e:
        # Sem variantes o pipeline ainda funciona: busca só a pergunta.
        logger.error("_generate_queries: LLM falhou, seguindo só com a pergunta original: %s", e)
        variants = []

    queries = [question]
    for v in variants:
        if v.lower() not in {q.lower() for q in queries}:
            queries.append(v)
    return queries[:4]


def _search_one(args: tuple[str, bool]) -> list[dict]:
    """Busca uma query, com o fallback de data. Usada em paralelo."""
    query, recent = args
    results: list[dict] = []
    seen: set[str] = set()

    if recent:
        results = _search.search(query, time_range=_RECENT_WINDOW)
        seen = {r.get("url", "") for r in results}

    # Fallback: filtro de data zera temas históricos (ex.: "quem foi Santos
    # Dumont" com time_range=day devolve 0). Completa sem filtro.
    if len(results) < _search.max_results:
        for r in _search.search(query):
            url = r.get("url", "")
            if url and url not in seen:
                results.append(r)
                seen.add(url)
    return results


def _collect_links(query: str, recent: bool) -> list[dict]:
    """Roda várias buscas em paralelo e mescla os resultados.

    A busca da pergunta original não espera o LLM: ela já é conhecida antes
    de gerar variante nenhuma. Rodar as duas coisas ao mesmo tempo esconde a
    latência da geração de queries atrás da rede do SearXNG.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(_search_one, (query, recent))
        variants = pool.submit(_generate_queries, query)
        queries = variants.result()
        original_results = original.result()

    # _generate_queries devolve a pergunta original em primeiro lugar; ela já
    # foi buscada acima, então aqui só faltam as variantes.
    extras = [q for q in queries if q != query]
    if extras:
        with ThreadPoolExecutor(max_workers=len(extras)) as pool:
            per_query = list(pool.map(_search_one, [(q, recent) for q in extras]))
    else:
        per_query = []
    per_query.insert(0, original_results)

    return _merge_results(per_query)


def _merge_results(per_query: list[list[dict]]) -> list[dict]:
    """Mescla os resultados das várias buscas, melhores primeiro.

    Round-robin (1º de cada busca, depois o 2º de cada) reparte o orçamento
    de páginas entre os ângulos, em vez de gastar tudo nos 10 primeiros da
    busca original. Isso continua.

    O que mudou é a ordem DENTRO de cada busca, que antes era a do SearXNG e
    ia crua. Dois sinais que estavam sendo jogados fora:

    - Concordância entre buscas. Se três dos quatro ângulos devolvem a mesma
      URL, ela é do assunto; o `seen` de antes descartava a repetição e com
      ela a informação. Foi assim que uma pesquisa sobre a quest do Art
      Cullagh leu a Wikipédia do deus nórdico Baldr — página grande, achada
      por um ângulo só, que sozinha ocupou metade do dossiê.
    - O `score` do SearXNG, que ele calcula somando os motores que acharam o
      resultado e varia bastante (4,0 a 0,25 dentro de uma mesma busca).

    Desempate por concordância primeiro: um resultado achado por duas buscas
    diferentes vale mais que um com score alto numa busca só.
    """
    agreement: dict[str, int] = {}
    for results in per_query:
        for url in {r.get("url", "") for r in results if r.get("url")}:
            agreement[url] = agreement.get(url, 0) + 1

    def rank(r: dict) -> tuple[int, float]:
        url = r.get("url", "")
        try:
            score = float(r.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return -agreement.get(url, 0), -score

    ranked = [sorted(results, key=rank) for results in per_query]

    merged: list[dict] = []
    seen: set[str] = set()
    for i in range(_search.max_results):
        for results in ranked:
            if i >= len(results):
                continue
            url = results[i].get("url", "")
            if url and url not in seen:
                merged.append(results[i])
                seen.add(url)
    return merged[: config.RESEARCH_POOL_SIZE]


# Caracteres por token, para estimar o tamanho do dossiê sem tokenizer.
#
# Medido neste tráfego: 3,51 (dossiê de 45k caracteres) e 4,00 (o de 412k).
# 2,5 é deliberadamente pessimista — a estimativa erra sempre para cima, e
# 30% de folga cobre página com muito código ou símbolo, que tokeniza bem
# pior que prosa. O custo da folga é zero na prática: dossiê real fica em
# 15-50k caracteres, longe do teto.
_CHARS_PER_TOKEN = 2.5


def _dossier_char_budget() -> int:
    """Quantos caracteres de dossiê cabem na janela do modelo.

    context_tokens() detecta a janela real do modelo carregado quando o
    provider a expõe (llama-swap); MODEL_CONTEXT_TOKENS é o fallback.
    """
    usable = context_tokens() - config.MODEL_RESERVE_TOKENS
    return max(int(usable * _CHARS_PER_TOKEN), 0)


def _read_pages(candidates: list[dict]) -> list[tuple[dict, str, str]]:
    """Lê candidatos em ondas até juntar RESEARCH_PAGE_BUDGET páginas boas.

    Antes o orçamento era gasto na primeira leva: link morto, bloqueado ou
    sem texto extraível queimava uma vaga e ainda entrava no dossiê como
    aviso de erro. Agora a falha só custa uma tentativa — a vaga passa para
    o próximo candidato da reserva.

    Cada onda baixa em paralelo, então o custo de uma reposição é uma
    rodada de rede, não uma leitura serial por página.

    Há dois orçamentos, e o que acabar primeiro encerra a leitura: número de
    páginas e tamanho em caracteres. O segundo existe porque estourar a
    janela do modelo não degrada a resposta — o provider recusa a chamada
    com HTTP 400 e todo o trabalho de busca e scraping vai fora.
    """
    pages_read: list[tuple[dict, str, str]] = []
    queue = [(r, r.get("url", "").strip()) for r in candidates]
    queue = [(r, u) for r, u in queue if u]

    char_budget = _dossier_char_budget()
    chars_used = 0

    for wave in range(config.RESEARCH_MAX_WAVES):
        if not queue or len(pages_read) >= config.RESEARCH_PAGE_BUDGET:
            break
        remaining = config.RESEARCH_PAGE_BUDGET - len(pages_read)
        batch, queue = queue[:remaining], queue[remaining:]
        pages = _scraper.read_many([u for _, u in batch])
        for (r, url), page in zip(batch, pages):
            if WebScraper.unusable(page):
                logger.error(
                    "_read_pages: onda %d, descartada url=%s (%d chars) motivo=%s",
                    wave + 1, url, len(page), page[:120],
                )
                continue
            # _render_dossier põe cabeçalho (título, URL, resumo da busca)
            # antes de cada página; o orçamento conta isso também.
            cost = len(page) + len(url) + len(r.get("title", "")) + len(r.get("content", "")) + 64
            if pages_read and chars_used + cost > char_budget:
                logger.info(
                    "_read_pages: orçamento de contexto atingido (%d de %d caracteres, "
                    "%d páginas); parando de ler e resumindo o que já tem",
                    chars_used, char_budget, len(pages_read),
                )
                return pages_read
            pages_read.append((r, url, page))
            chars_used += cost
    return pages_read


def _render_dossier(pages_read: list[tuple[dict, str, str]]) -> str:
    dossier = "\n\n".join(
        f"Fonte: {r.get('title', '').strip()}\n"
        f"URL: {url}\n"
        f"Resumo da busca: {r.get('content', '').strip()}\n"
        f"Conteúdo da página:\n{page}"
        for r, url, page in pages_read
    )

    # Rede de segurança, não o controle principal: quem segura o tamanho é o
    # orçamento do _read_pages. Isto cobre o que passa por fora dele — uma
    # única página maior que a janela inteira, ou o dossiê montado pelo eval.
    # Cortar aqui perde o fim do material; estourar perde a chamada toda.
    budget = _dossier_char_budget()
    if len(dossier) > budget:
        logger.warning(
            "_render_dossier: dossiê de %d caracteres excede o orçamento de %d; truncando",
            len(dossier), budget,
        )
        dossier = dossier[:budget]
    return dossier


def _build_dossier(query: str, recent: bool) -> tuple[str, list[tuple[dict, str, str]]]:
    """Busca, lê e monta o dossiê. Separado de _summarize para o eval
    conseguir o dossiê sem repesquisar."""
    results = _collect_links(query, recent)
    pages_read = _read_pages(results) if results else []
    return _render_dossier(pages_read), pages_read


def _summarize(query: str, dossier: str, recent: bool) -> str:
    # astimezone() sem argumento anexa o fuso do host ao horário ingênuo. O
    # offset vai impresso porque o agente que consome a tool pode estar em
    # outro fuso: "12:18" sozinho é ambíguo, "12:18 -03" não é.
    now = datetime.now().astimezone()
    context = _RECENT_INSTRUCTION if recent else _TIMELESS_INSTRUCTION
    return chat(
        system=(
            f"Data e hora atual: {now.strftime('%d/%m/%Y %H:%M')} "
            f"{_format_offset(now)}. {_BASE_INSTRUCTION} {context}"
        ),
        user=f"Pergunta: {query}\n\nMaterial coletado:\n\n{dossier}",
    )


def research_web(query: str, recent: bool = False) -> str:
    """Pesquisa na web e devolve um resumo com fontes.

    Use para qualquer informação que você não saiba com certeza. Passe a
    pergunta completa em linguagem natural — a busca, a leitura das páginas
    e o resumo são feitos internamente.

    UMA CHAMADA POR PERGUNTA. Esta ferramenta já reformula a pergunta em
    vários ângulos de busca por dentro, roda todos em paralelo e lê as
    melhores páginas do conjunto. Chamar de novo com a mesma pergunta escrita
    de outro jeito não traz material novo: relê as mesmas páginas e gasta o
    mesmo tempo outra vez. Só chame outra vez quando a pergunta for
    genuinamente outra, ou quando o resumo apontar o que faltou.

    Args:
        query: A pergunta completa em linguagem natural, do jeito que o
            usuário faria. Não reduza a palavras-chave nem parta em pedaços:
            a reformulação em termos de busca é feita aqui dentro, e uma
            pergunta inteira dá um resultado melhor que um fragmento.
        recent: True apenas quando a resposta depende do dia de hoje
            (clima, cotação, placar, notícia de agora). False para fatos
            estáveis (história, biografia, conceitos, documentação), pois
            filtrar por data descarta as fontes boas.
    """
    logger.info("research_web chamada: query=%r recent=%s", query, recent)

    try:
        results = _collect_links(query, recent)
    except requests.RequestException as e:
        logger.error("research_web: SearXNG falhou para query=%r: %s", query, e)
        return f"Erro ao consultar o SearXNG: {e}"

    if not results:
        logger.info("research_web: nenhum resultado para query=%r", query)
        return "Nenhum resultado encontrado."

    pages_read = _read_pages(results)
    if not pages_read:
        logger.error("research_web: todas as %d páginas candidatas falharam para query=%r", len(results), query)
        return "Nenhuma das páginas encontradas pôde ser lida."

    dossier = _render_dossier(pages_read)
    try:
        summary = _summarize(query, dossier, recent)
    except Exception as e:
        # As páginas já foram lidas e custaram a rede toda; devolver exceção
        # aqui joga esse trabalho fora e deixa o agente que chamou sem nada
        # nas mãos. A lista de URLs abaixo ainda é uma resposta útil.
        logger.error("research_web: resumo falhou para query=%r: %s", query, e)
        summary = (
            f"(o resumo automático falhou: {e} — as páginas abaixo foram "
            f"lidas com sucesso e podem ser abertas com read_url)"
        )

    # A lista de URLs vai por código, não pelo resumo: o modelo ora cita a
    # URL, ora troca pelo nome do veículo, ora omite (medido em 3 de 4
    # execuções, ver refino.md). Aqui sabemos exatamente o que foi lido —
    # e o _read_pages já garantiu que todas abriram.
    sources = "\n".join(f"- {url}" for _, url, _ in pages_read)

    now = datetime.now().astimezone()
    utc_now = now.astimezone(timezone.utc)
    stamp = (
        f"Pesquisa realizada em {now.strftime('%d/%m/%Y %H:%M')} {_format_offset(now)} "
        f"({utc_now.strftime('%H:%M')} UTC)."
    )
    logger.info("research_web ok: query=%r páginas_lidas=%d", query, len(pages_read))
    return f"{stamp}\n\n{summary}\n\nURLs consultadas:\n{sources}"
