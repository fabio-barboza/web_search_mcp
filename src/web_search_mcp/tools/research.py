import logging
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

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
    "distintos — 4 buscas quando a pergunta for ampla (panorama, 'Brasil e "
    "mundo', comparação de várias coisas), porque ângulo que não vira busca "
    "não aparece no resultado. Uma busca por linha, sem numerar, sem explicar. Use os "
    "termos que apareceriam na página procurada, não a pergunta inteira. "
    "Se a pergunta for sobre uma pessoa, inclua buscas que combinem o nome "
    "com onde ela apareceria (github, linkedin, currículo, empresa). "
    "Se o assunto tiver boa cobertura internacional (tecnologia, ciência, "
    "notícia mundial), escreva UMA das buscas em inglês — as fontes "
    "internacionais de referência costumam ter o material mais completo. "
    "Se a pergunta pedir um panorama das notícias do dia, dedique uma busca "
    "só às manchetes gerais (ex.: 'principais manchetes Brasil hoje') e uma "
    "em inglês às internacionais NOMEANDO veículos de referência (ex.: "
    "'world news today Reuters BBC AP') — sem o nome dos veículos a busca "
    "em inglês devolve agregadores de SEO, e sem essas buscas o resultado "
    "enviesa para a editoria que calhar de ranquear melhor."
)

_BASE_INSTRUCTION = (
    "Você é um pesquisador web. Recebe uma pergunta e o material coletado "
    "de várias páginas, numerado em blocos FONTE [n]. Extraia APENAS os "
    "fatos que respondem à pergunta e escreva um resumo curto e direto em "
    "português do Brasil. Cada fato termina com a marcação [n] da fonte de "
    "onde saiu, e a data/hora do dado quando houver — use o número, NUNCA "
    "escreva o nome do veículo (a legenda número→URL é montada fora). "
    "Nunca combine numa mesma afirmação informações de fontes diferentes: "
    "se duas fontes contribuem, escreva duas frases, cada uma com sua "
    "marcação. Copie nomes de pessoas, cargos e números exatamente como "
    "estão na fonte, sem aproximar nem fundir. Capa de portal mistura "
    "notícia do dia com reportagem antiga: só apresente como fato de hoje "
    "o que o material datar de hoje. Ignore páginas irrelevantes ou que "
    "falharam. Nunca invente nada: se o material não responder, diga "
    "exatamente o que faltou. Não copie o conteúdo bruto das páginas. Se a "
    "pergunta pedir um panorama de notícias do dia, priorize COBERTURA "
    "sobre profundidade: 12 ou mais manchetes distintas, uma ou duas "
    "linhas cada, agrupadas por seção — não gaste o espaço aprofundando "
    "poucos temas. Fontes em outros idiomas valem tanto quanto as em "
    "português: traduza os fatos delas com fidelidade, mantendo nomes "
    "próprios, siglas e termos técnicos na forma original quando não "
    "houver tradução consagrada."
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
    # 5 = original + 4 variantes (o teto de 4 cortava justamente a busca
    # dedicada às manchetes internacionais em pergunta ampla — medido:
    # panorama "Brasil e mundo" perdia a manchete do dia por falta de
    # fan-out, não por leitura ruim das páginas).
    return queries[:5]


# Parâmetros de tracking: não mudam o conteúdo da página, só a string da
# URL — sem removê-los, a mesma matéria vinda de duas buscas conta como duas
# URLs diferentes e o sinal de concordância se perde.
_TRACKING_KEYS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "src"}


# Marcas de payload injetado no parâmetro de query. Visto em produção: uma
# página da USP entrou na lista de fontes com
# "?xml=data:gsf,<krpano><include url="//yapuza.xyz/..."/></krpano>" —
# injeção de SEO num domínio legítimo. Nada disso aparece em URL de conteúdo
# real, e citar uma dessas como fonte é pior que ter uma fonte a menos.
_URL_PAYLOAD_MARKS = ("<", ">", "data:", "javascript:", "\\x3c", "%3c")


def _is_suspicious_url(url: str) -> bool:
    """True quando a query string carrega marcação ou outro esquema embutido."""
    try:
        query = urlsplit(url).query
    except ValueError:
        return True
    decoded = unquote(query).lower()
    return any(m in decoded for m in _URL_PAYLOAD_MARKS)


def _normalize_url(url: str) -> str:
    """Chave de dedupe: minúsculas no host, sem tracking/fragmento/barra final.

    Só a CHAVE — a URL original segue intacta no resultado e no dossiê,
    porque é ela que abre no navegador de quem recebe a lista de fontes.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.startswith("utm_") and k not in _TRACKING_KEYS
        ]
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
    )


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


# Capas de veículos de referência, semeadas no topo dos candidatos quando a
# pergunta pede panorama de notícias. Motor de busca não devolve capa para
# "principais manchetes hoje" — devolve digest de SEO e agregador requentado
# (medido: recall de 1/16 contra as manchetes do Google News). A capa é onde
# as manchetes do dia realmente estão, e o _clean do scraper foi feito para
# esse tipo de página. Fora do caso panorama, nada muda.
_SEEDS_BR = [
    "https://g1.globo.com/",
    "https://noticias.uol.com.br/",
    "https://www.cnnbrasil.com.br/",
    # Redundância de agenda BR: g1 às vezes extrai curto demais (<600 chars)
    # e o UOL devolve 403 sob repetição — a lista da EBC extrai sempre.
    "https://agenciabrasil.ebc.com.br/ultimas",
]
_SEEDS_WORLD = [
    "https://www.bbc.com/news",
    "https://apnews.com/",
]

_NEWS_RE = re.compile(r"not[íi]cias?|manchetes?|headlines?|\bnews\b", re.I)
_WORLD_RE = re.compile(r"mundo|internacion|world|global", re.I)
# Panorama temático (tecnologia, games...) tem fontes próprias melhores que
# capa de jornal generalista: nesses casos a busca normal resolve.
_TOPIC_RE = re.compile(
    r"tecnolog|ci[êe]nc|game|jogo|esport|econom|pol[íi]tic|sa[úu]de", re.I
)


def _panorama_seeds(query: str, recent: bool) -> list[dict]:
    if not recent or not _NEWS_RE.search(query) or _TOPIC_RE.search(query):
        return []
    seeds = list(_SEEDS_BR)
    if _WORLD_RE.search(query):
        seeds += _SEEDS_WORLD
    return [{"url": u, "title": "", "content": ""} for u in seeds]


# Quantas matérias extrair de cada capa semeada. A ordem do DOM da capa é a
# hierarquia editorial do veículo — os primeiros links são a manchete.
# 2 e não mais: a mesma história aparece no topo de várias capas, e cada
# matéria repetida sobre ela rouba uma vaga da pauta seguinte (medido:
# Braskem ocupou 3 das 10 páginas de um panorama).
_ARTICLES_PER_FRONT = 2

# Caminho com cara de matéria: data na URL, /article(s)/, ou slug longo com
# hífens. Link de navegação (/politica/, /esportes/) não passa.
_ARTICLE_PATH_RE = re.compile(r"/20\d{2}/\d{2}/|/articles?/")


def _article_links(front_url: str, html: str, limit: int) -> list[tuple[str, str]]:
    """Extrai (url, título) das matérias de uma capa, na ordem do DOM.

    Capa é ótima para saber O QUE é notícia e péssima como conteúdo: só
    manchete, sem corpo — resumo feito dela erra contexto e cita o índice
    como fonte. Daqui saem os links; quem entra no dossiê é a matéria.
    """
    import lxml.etree
    import lxml.html
    from urllib.parse import urljoin

    try:
        root = lxml.html.fromstring(html)
    except (lxml.etree.ParserError, ValueError):
        return []

    host = urlsplit(front_url).netloc.lower().removeprefix("www.")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in root.iter("a"):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(front_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().removeprefix("www.") != host:
            continue
        path = parts.path.rstrip("/")
        slug = path.rsplit("/", 1)[-1]
        if not (_ARTICLE_PATH_RE.search(path) or (len(slug) > 25 and "-" in slug)):
            continue
        key = _normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        out.append((url, " ".join(a.text_content().split())[:120]))
        if len(out) >= limit:
            break
    return out


def _expand_seeds(seeds: list[dict]) -> list[dict]:
    """Capas semeadas + as matérias do topo de cada uma.

    Híbrido de propósito: a capa dá a AGENDA (o que é notícia agora, muitas
    manchetes) e as matérias dão o CORPO (contexto que evita erro de resumo
    e URL verificável por item). Só matéria perdia cobertura (medido:
    recall de manchetes caiu de 5/16 para 1/16); só capa perdia precisão.
    Capa cujo download falhar segue como candidata ela mesma.
    """
    urls = [s["url"] for s in seeds]
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        downloads = list(pool.map(_scraper._download, urls))

    per_seed: list[list[dict]] = []
    for seed, (ok, html) in zip(seeds, downloads):
        links = _article_links(seed["url"], html, _ARTICLES_PER_FRONT) if ok else []
        per_seed.append([{"url": u, "title": t, "content": ""} for u, t in links])

    # Capas primeiro (cobertura garantida), depois as matérias intercaladas
    # entre veículos (manchete de cada um antes da 2ª matéria de qualquer um)
    # — assim o corte do orçamento de páginas preserva o equilíbrio.
    expanded = list(seeds)
    for i in range(max((len(lst) for lst in per_seed), default=0)):
        for lst in per_seed:
            if i < len(lst):
                expanded.append(lst[i])
    return expanded


def _search_one_safe(args: tuple[str, bool]) -> list[dict]:
    """_search_one que não derruba as irmãs: variante que falhar vira lista
    vazia. A busca da pergunta original continua propagando erro — se ela
    falhou, o SearXNG está fora e não há o que aproveitar."""
    try:
        return _search_one(args)
    except Exception as e:
        logger.error("_search_one_safe: busca da variante %r falhou: %s", args[0], e)
        return []


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
            per_query = list(pool.map(_search_one_safe, [(q, recent) for q in extras]))
    else:
        per_query = []
    per_query.insert(0, original_results)

    merged = _merge_results(per_query)

    seeds = _panorama_seeds(query, recent)
    if seeds:
        articles = _expand_seeds(seeds)
        seeded = {_normalize_url(s["url"]) for s in articles}
        merged = articles + [
            r for r in merged if _normalize_url(r.get("url", "")) not in seeded
        ]
    return merged


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
    per_query = [
        [r for r in results if not _is_suspicious_url(r.get("url", ""))]
        for results in per_query
    ]

    agreement: dict[str, int] = {}
    for results in per_query:
        for key in {_normalize_url(r["url"]) for r in results if r.get("url")}:
            agreement[key] = agreement.get(key, 0) + 1

    def rank(r: dict) -> tuple[int, float]:
        try:
            score = float(r.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return -agreement.get(_normalize_url(r.get("url", "")), 0), -score

    ranked = [sorted(results, key=rank) for results in per_query]

    # Teto por domínio: sem ele, uma busca cujo top-10 é todo do mesmo site
    # enche a reserva com um veículo só e o dossiê perde variedade (medido:
    # metade das fontes de um panorama saiu da mesma redação). 0 = sem teto.
    merged: list[dict] = []
    seen: set[str] = set()
    per_domain: dict[str, int] = {}
    for i in range(_search.max_results):
        for results in ranked:
            if i >= len(results):
                continue
            url = results[i].get("url", "")
            key = _normalize_url(url)
            if not url or key in seen:
                continue
            domain = urlsplit(url).netloc.lower()
            if (
                config.RESEARCH_MAX_PER_DOMAIN
                and per_domain.get(domain, 0) >= config.RESEARCH_MAX_PER_DOMAIN
            ):
                continue
            merged.append(results[i])
            seen.add(key)
            per_domain[domain] = per_domain.get(domain, 0) + 1
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


# Página-hub (capa, seção de portal) que a densidade de links do scraper não
# pega: o que sobra da extração é menu e manchete solta, texto que não está
# dentro de <a> e por isso não conta como link. Assinatura combinada, medida
# no corpus real: caminho raso (cnnbrasil.com.br/tecnologia/), nenhuma prosa
# de verdade e muitas linhas.
#
# Os três juntos são necessários. Sozinho, o caminho raso derruba
# fextralife.com/Rings e eldenring.wiki/Weapons — lista de itens é o que uma
# pergunta sobre equipamento precisa ler. Sozinha, a falta de prosa derruba a
# página de item do bg3.wiki, que é tabela de atributos. E o piso de linhas
# protege o post curto de blog publicado na raiz do domínio.
_HUB_MAX_PATH_SEGMENTS = 1
_HUB_MAX_PROSE_LINES = 3
_HUB_MIN_LINES = 30
_PROSE_LINE_CHARS = 200


def _is_hub_page(url: str, text: str) -> bool:
    """True quando a página é vitrine de links de outras páginas."""
    try:
        segments = [s for s in urlsplit(url).path.split("/") if s]
    except ValueError:
        return False
    if len(segments) > _HUB_MAX_PATH_SEGMENTS:
        return False
    lines = [l for l in text.splitlines() if l.strip()]
    prose = sum(1 for l in lines if len(l.strip()) >= _PROSE_LINE_CHARS)
    return len(lines) >= _HUB_MIN_LINES and prose <= _HUB_MAX_PROSE_LINES


# REGRA: marcador de referência só pode aparecer se o link estiver NO TEXTO.
#
# "[n]" sozinho depende da legenda numerada, que fica no fim da resposta e é
# exatamente o que o agente de chat corta ao reescrever (observado no Open
# WebUI: resumo chega com [1] [9] e nenhuma lista de URLs). O usuário fica
# com um número que não resolve para nada e não tem como conferir o fato.
#
# Então a citação vira link markdown com a URL embutida: o link viaja dentro
# da frase, sobrevive ao corte da legenda e é clicável. Montado em código a
# partir da URL realmente lida — nunca pedido ao modelo, que erra URL mas
# acerta o número.
_CITATION_RE = re.compile(r"[ \t]*\[(\d{1,2})\]")


def _label_citations(summary: str, pages_read: list[tuple[dict, str, str]]) -> str:
    """Troca [n] por link markdown [dominio](url); apaga [n] sem fonte."""
    links: dict[int, str] = {}
    for i, (_, url, _) in enumerate(pages_read, 1):
        host = urlsplit(url).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        links[i] = f"[{host}]({url})"

    def swap(m: re.Match) -> str:
        n = int(m.group(1))
        # Número fora da lista é citação inventada: sai do texto, junto com o
        # espaço que o precedia, senão sobra " ." no meio da frase.
        if n not in links:
            return ""
        return f" {links[n]}"

    return _CITATION_RE.sub(swap, summary)


def _read_pages(
    candidates: list[dict],
    page_budget: int | None = None,
    reject_index: bool = False,
) -> list[tuple[dict, str, str]]:
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
        budget = page_budget or config.RESEARCH_PAGE_BUDGET
        if not queue or len(pages_read) >= budget:
            break
        remaining = budget - len(pages_read)
        batch, queue = queue[:remaining], queue[remaining:]
        pages = _scraper.read_many([u for _, u in batch], reject_index=reject_index)
        for (r, url), page in zip(batch, pages):
            if WebScraper.unusable(page):
                logger.error(
                    "_read_pages: onda %d, descartada url=%s (%d chars) motivo=%s",
                    wave + 1, url, len(page), page[:120],
                )
                continue
            if reject_index and _is_hub_page(url, page):
                logger.info(
                    "_read_pages: onda %d, descartada url=%s: página-hub "
                    "(vitrine de links, sem conteúdo próprio)",
                    wave + 1, url,
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
    # Numerar as fontes ([1], [2]...) dá ao resumo um jeito de citar sem
    # escrever nome de veículo: o modelo aponta o número, e o código monta a
    # legenda número→URL — atribuição verificável, nunca inventada.
    dossier = "\n\n".join(
        f"===== FONTE [{i}] =====\n"
        f"Título: {r.get('title', '').strip()}\n"
        f"URL: {url}\n"
        f"Resumo da busca: {r.get('content', '').strip()}\n"
        f"Conteúdo da página:\n{page}"
        for i, (r, url, page) in enumerate(pages_read, 1)
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
    panorama = bool(_panorama_seeds(query, recent))
    pages_read = (
        _read_pages(
            results,
            config.RESEARCH_PANORAMA_PAGES if panorama else None,
            reject_index=not panorama,
        )
        if results
        else []
    )
    return _render_dossier(pages_read), pages_read


def _page_budget(query: str, recent: bool) -> int | None:
    """Panorama usa orçamento próprio (capas + matérias); o resto, o padrão."""
    return config.RESEARCH_PANORAMA_PAGES if _panorama_seeds(query, recent) else None


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


# Anti-loop: agente de chat em modo tool-calling às vezes re-chama a
# pesquisa em círculo, reformulando a mesma pergunta (observado no Open
# WebUI com qwen3.6:35B no-think: prompt vago sobre BG3 → chamadas sem
# parar). Instrução na docstring não segura loop; devolver o resultado
# anterior na hora segura — o retorno é instantâneo (sem busca nem LLM) e
# vem com ordem explícita de parada.
_REPEAT_TTL_SECONDS = 600
_REPEAT_MAX_ENTRIES = 50
_recent_calls: dict[str, tuple[float, str]] = {}

_REPEAT_NOTE = (
    "AVISO: esta pergunta (ou uma quase igual) acabou de ser pesquisada "
    "nesta mesma conversa. O resultado abaixo é o MESMO de antes — "
    "pesquisar de novo não traz material novo. NÃO chame research_web "
    "outra vez para esta pergunta: responda ao usuário com o que está "
    "abaixo, e se algo faltar, diga ao usuário o que faltou.\n\n"
)

# Palavras de função: mudam a frase sem mudar a pergunta. Lista de mecânica
# de linguagem, não de assunto — vale para qualquer tema.
_STOPWORDS = frozenset((
    "a as o os um uma uns umas de do da dos das em no na nos nas por para "
    "com sem sobre e ou que qual quais quanto quantos quando onde como "
    "quem me meu minha seu sua ao aos à às pelo pela é são foi ser esta "
    "este essa esse isso aquilo mais menos muito bem ja já entao então "
    "the a an of in on at to for from by with about and or what which "
    "who how when where is are was were be been do does did"
.split()))


def _content_tokens(query: str) -> frozenset[str]:
    """Palavras que carregam o assunto: sem acento, sem pontuação, sem função.

    A comparação é por CONJUNTO, então ordem e repetição não contam — é
    exatamente a liberdade que uma reformulação usa.
    """
    folded = unicodedata.normalize("NFKD", query.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return frozenset(t for t in re.findall(r"\w+", folded) if t not in _STOPWORDS)


def _repeat_key(query: str) -> str:
    return " ".join(query.lower().split())


def _cached_result(query: str) -> str | None:
    """Resultado recente da MESMA pergunta, se houver.

    "Mesma" = mesmo conjunto de palavras de conteúdo. Reformular, reordenar
    ou trocar palavra de função devolve o cache; trocar uma palavra de
    conteúdo é outra pergunta e vai para a busca.

    Similaridade de string não serve aqui, e a medição mostra por quê:
    "principais notícias do Brasil hoje" x "principais notícias do mundo
    hoje" dá 0.84 de SequenceMatcher — acima de qualquer corte que ainda
    reconheça repetição real ("do Brasil" x "no Brasil" dá 0.97) — e as duas
    perguntas são genuinamente diferentes. O erro é assimétrico: deixar de
    barrar uma repetição custa uma busca; barrar pergunta nova devolve
    resposta errada com cara de certa, calada.
    """
    now = time.monotonic()
    for k in [k for k, (ts, _) in _recent_calls.items() if now - ts > _REPEAT_TTL_SECONDS]:
        del _recent_calls[k]
    key = _repeat_key(query)
    hit = _recent_calls.get(key)
    if hit:
        return hit[1]
    tokens = _content_tokens(query)
    if not tokens:
        return None
    for k, (_, result) in _recent_calls.items():
        if _content_tokens(k) == tokens:
            return result
    return None


def _remember_result(query: str, result: str) -> None:
    if len(_recent_calls) >= _REPEAT_MAX_ENTRIES:
        oldest = min(_recent_calls, key=lambda k: _recent_calls[k][0])
        del _recent_calls[oldest]
    _recent_calls[_repeat_key(query)] = (time.monotonic(), result)


def research_web(query: str, recent: bool = False) -> str:
    """Pesquisa na web e devolve um resumo com fontes.

    Use para qualquer informação que você não saiba com certeza — e também
    quando acha que sabe mas o assunto pode ter mudado desde o seu treino:
    nesses casos, prefira pesquisar a responder de memória. Passe a
    pergunta completa em linguagem natural — a busca, a leitura das páginas
    e o resumo são feitos internamente.

    UMA CHAMADA POR PERGUNTA. Esta ferramenta já reformula a pergunta em
    vários ângulos de busca por dentro, roda todos em paralelo e lê as
    melhores páginas do conjunto. Chamar de novo com a mesma pergunta escrita
    de outro jeito não traz material novo: relê as mesmas páginas e gasta o
    mesmo tempo outra vez. Só chame outra vez quando a pergunta for
    genuinamente outra, ou quando o resumo apontar o que faltou. Pergunta
    NOVA do usuário = chamada nova, mesmo que seja sobre o mesmo assunto de
    antes: cada pergunta diferente merece sua própria pesquisa.

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

    cached = _cached_result(query)
    if cached is not None:
        logger.warning("research_web: repetição detectada, devolvendo resultado anterior: query=%r", query)
        return _REPEAT_NOTE + cached

    try:
        results = _collect_links(query, recent)
    except requests.RequestException as e:
        logger.error("research_web: SearXNG falhou para query=%r: %s", query, e)
        return f"Erro ao consultar o SearXNG: {e}"

    if not results:
        logger.info("research_web: nenhum resultado para query=%r", query)
        # Também entra no cache anti-loop: resultado vazio é o gatilho mais
        # comum de re-chamada em círculo.
        outcome = (
            "Nenhum resultado encontrado. Não repita a busca com a mesma "
            "pergunta reescrita; diga ao usuário que não encontrou."
        )
        _remember_result(query, outcome)
        return outcome

    panorama = bool(_panorama_seeds(query, recent))
    pages_read = _read_pages(
        results,
        config.RESEARCH_PANORAMA_PAGES if panorama else None,
        reject_index=not panorama,
    )
    if not pages_read:
        logger.error("research_web: todas as %d páginas candidatas falharam para query=%r", len(results), query)
        outcome = "Nenhuma das páginas encontradas pôde ser lida."
        _remember_result(query, outcome)
        return outcome

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

    summary = _label_citations(summary, pages_read)

    # A legenda numerada vai por código, não pelo resumo: o modelo cita [n]
    # e aqui sabemos exatamente qual URL é cada número — atribuição de fonte
    # nunca é redigida pelo modelo. O _read_pages já garantiu que todas
    # abriram, e a numeração segue a ordem dos blocos FONTE [n] do dossiê.
    sources = "\n".join(f"[{i}] {url}" for i, (_, url, _) in enumerate(pages_read, 1))

    # O agente que consome a tool tende a reescrever o resumo e, nisso,
    # apagar as marcações [n] e trocar a atribuição por uma lista genérica
    # de veículos redigida por ele (observado no Open WebUI: datas sumiram e
    # um erro novo entrou na reescrita). A nota abaixo é endereçada a esse
    # agente — é a única camada onde dá para defender a atribuição.
    relay_note = (
        "Nota para o agente: cada fato do resumo termina com um link "
        "markdown para a página de onde saiu. Ao apresentar ao usuário, "
        "mantenha esses links INTEIROS e as datas — são a atribuição por "
        "item. Nunca troque um link por um número entre colchetes, por nome "
        "de veículo, nem condense tudo numa lista genérica no rodapé: "
        "marcador sem link é referência que o usuário não consegue conferir."
    )

    now = datetime.now().astimezone()
    utc_now = now.astimezone(timezone.utc)
    stamp = (
        f"Pesquisa realizada em {now.strftime('%d/%m/%Y %H:%M')} {_format_offset(now)} "
        f"({utc_now.strftime('%H:%M')} UTC)."
    )
    logger.info("research_web ok: query=%r páginas_lidas=%d", query, len(pages_read))
    result = f"{stamp}\n\n{summary}\n\n{relay_note}\n\nURLs consultadas:\n{sources}"
    _remember_result(query, result)
    return result
