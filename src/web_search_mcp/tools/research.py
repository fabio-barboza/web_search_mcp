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
from ..util.search_chain import build_search

logger = logging.getLogger(__name__)

# Google CSE via Tor com SearXNG de reserva, ou só o SearXNG (SEARCH_BACKEND).
# Mesma interface nos dois casos: o pipeline abaixo não sabe qual é.
_search = build_search()
_scraper = WebScraper(limit=config.RESEARCH_PAGE_CHARS)

# Janela usada quando a pergunta pede dado recente.
_RECENT_WINDOW = "day"

_QUERIES_INSTRUCTION = (
    "Você planeja buscas web. Dada uma pergunta, escreva 3 buscas curtas e "
    "DIFERENTES entre si que, juntas, cobrem a resposta por ângulos "
    "distintos — 4 buscas quando a pergunta for ampla (várias coisas "
    "comparadas, várias facetas), porque ângulo que não vira busca "
    "não aparece no resultado. Uma busca por linha, sem numerar, sem explicar. Use os "
    "termos que apareceriam na página procurada, não a pergunta inteira. "
    "Se a pergunta for sobre uma pessoa, inclua buscas que combinem o nome "
    "com onde ela apareceria (github, linkedin, currículo, empresa). "
    "Se o assunto puder ter cobertura internacional, escreva UMA das buscas "
    "em inglês — quando existe material em inglês, costuma ser o mais "
    "completo. "
    "Nomes próprios, apelidos, títulos, siglas, comandos e qualquer termo "
    "específico da pergunta entram em TODAS as buscas exatamente como "
    "estão escritos, inclusive na busca em inglês, onde só as palavras "
    "comuns mudam de língua. Nunca troque um nome por tradução, sinônimo, "
    "número ou identificação que você supõe, nem acrescente o que acha "
    "que ele significa: se o palpite estiver errado, a busca inteira "
    "procura outra coisa. Se a pergunta der mais de um nome para a mesma "
    "coisa (\"X / Y\", \"X (Y)\", \"X ou Y\"), faça uma busca para cada "
    "nome: um deles pode estar errado, e só a busca pelo outro acha."
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
    "estão na fonte, sem aproximar nem fundir. Quando várias fontes dão "
    "valores diferentes para o MESMO dado, não liste um por fonte: "
    "apresente o valor em que mais fontes concordam — sem maioria, o de "
    "horário ou data mais tardio —, com a hora ou data e as marcações, e "
    "diga numa frase quais fontes divergem e com que valor. "
    "Capa de portal mistura "
    "notícia do dia com reportagem antiga: só apresente como fato de hoje "
    "o que o material datar de hoje. Cada bloco traz a data de publicação "
    "da página: se ela for anterior ao período que a pergunta pede, diga "
    "que o dado é daquela data em vez de apresentá-lo como atual. "
    "Ignore páginas irrelevantes ou que "
    "falharam. Nunca invente nada: se o material não responder, diga "
    "exatamente o que faltou. A pergunta pode trazer premissa errada — "
    "um nome trocado, uma equivalência (\"X, que é Y\"): só afirme "
    "identidade ou equivalência que o material mostrar, e quando nada no "
    "material ligar o nome da pergunta ao que as fontes descrevem, diga "
    "isso em vez de responder sobre a coisa mais parecida. Quando o "
    "material responde o núcleo da pergunta mas não repete um detalhe "
    "dela (uma parte, uma data, uma versão), responda o núcleo primeiro e "
    "diga numa frase que as fontes não confirmam aquele detalhe — nunca "
    "troque o detalhe por outro valor que as fontes também não dão. Não copie o conteúdo bruto das páginas. Se a "
    "pergunta for ampla, priorize COBERTURA sobre profundidade: mais itens "
    "curtos, de uma ou duas linhas, em vez de poucos temas aprofundados. "
    "Fontes em outros idiomas valem tanto quanto as em "
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


_KEYWORD_TOKEN_RE = re.compile(r"[\w'’.+#/-]+")


def _keyword_query(question: str) -> str:
    """A pergunta sem palavras de função, com nomes, acentos e caixa intactos.

    Motor de busca rankeia por termo: a pergunta inteira em linguagem
    natural casa página por palavra de função e palavra solta. Medido em
    10/09/2026 no Google CSE: "Como inicia a chain do Pai Putrefato no ato 3
    de BG3?" trouxe 1 resultado do assunto e 9 PDFs acadêmicos; a mesma
    pergunta em palavras-chave trouxe ~10 do assunto. "" quando não sobra
    nada que a distinga da pergunta (pergunta já curta).
    """
    kept = []
    for i, token in enumerate(_KEYWORD_TOKEN_RE.findall(question)):
        # Só no fim: "BG3." perde o ponto, ".NET" continua ".NET".
        token = token.rstrip(".'’/-")
        if not token:
            continue
        # Maiúscula fora do início da frase é nome, mesmo quando a palavra
        # também é de função: "Lei do Bem" sem o "Bem" busca outra lei.
        is_name = i > 0 and token[0].isupper()
        if is_name or _fold(token) not in _STOPWORDS:
            kept.append(token)
    keywords = " ".join(kept)
    if len(kept) < 2 or _fold(keywords) == _fold(" ".join(_KEYWORD_TOKEN_RE.findall(question))):
        return ""
    return keywords


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


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

    # A forma em palavras-chave entra sempre, montada em código: não depende
    # de o LLM obedecer a regra de manter os nomes (medido em 10/09/2026, com
    # a regra no prompt, 2 de 3 variantes ainda trocavam o nome da pergunta
    # por uma tradução inventada e voltavam com 0 resultados).
    queries = [question]
    keywords = _keyword_query(question)
    if keywords:
        variants = [keywords, *variants]
    for v in variants:
        if v.lower() not in {q.lower() for q in queries}:
            queries.append(v)
    # 5 = original + 4 variantes. O teto de 4 cortava a última busca gerada
    # em pergunta ampla, e o ângulo perdido não voltava por leitura melhor
    # das páginas: o que não vira busca não existe no resultado.
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


def _search_one_safe(args: tuple[str, bool]) -> list[dict]:
    """_search_one que não derruba as irmãs: variante que falhar vira lista
    vazia. A busca da pergunta original continua propagando erro — se ela
    falhou, a busca está fora e não há o que aproveitar."""
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

    # Sem teto de domínio aqui: ele é aplicado depois da triagem, em
    # _select_and_read. No merge ele cortava sem julgar — medido em
    # 10/09/2026, as 2 vagas de um wiki foram para a página de um personagem
    # e para a homepage do wiki, e a página que respondia (outra do mesmo
    # site) nunca chegou à triagem.
    return _merge_results(per_query, domain_cap=0)


def _cap_per_domain(candidates: list[dict]) -> list[dict]:
    """Teto de RESEARCH_MAX_PER_DOMAIN por site, mantendo a ordem recebida."""
    cap = config.RESEARCH_MAX_PER_DOMAIN
    if not cap:
        return candidates
    out: list[dict] = []
    per_domain: dict[str, int] = {}
    for r in candidates:
        domain = urlsplit(r.get("url", "")).netloc.lower()
        if per_domain.get(domain, 0) >= cap:
            continue
        per_domain[domain] = per_domain.get(domain, 0) + 1
        out.append(r)
    return out


def _search_health_note(results: list[dict]) -> str:
    """Aviso quando a busca rodou com a maior parte dos motores fora.

    Sem número mágico: compara os motores suspensos com os que de fato
    trouxeram resultado nesta busca. Só avisa quando os mortos são maioria
    — um motor a menos entre dez não muda a cobertura, mas dez entre onze
    transformam a busca em casamento de nome de marca (medido em
    29/08/2026: só o bing respondia, e "tailscale ACL limitar portas" e
    "tailscale acl ports dst example" devolviam a mesma lista de
    homepages). O aviso existe para o agente parar em vez de reformular a
    pergunta para sempre contra uma infraestrutura que não vai melhorar
    nos próximos minutos.
    """
    down = _search.health()
    if not down:
        return ""
    live = {e for r in results for e in (r.get("engines") or [])}
    if len(down) <= len(live):
        return ""
    nomes = ", ".join(f"{k} ({v})" if v else k for k, v in sorted(down.items()))
    respondeu = ", ".join(sorted(live)) or "nenhum"
    return (
        f"AVISO DE INFRAESTRUTURA: {len(down)} motores de busca estão "
        f"suspensos agora ({nomes}); respondeu apenas: {respondeu}. A "
        f"cobertura abaixo está incompleta por isso, não porque o assunto "
        f"não exista. Repetir a pesquisa, reescrever a pergunta ou chutar "
        f"endereços com read_url NÃO vai melhorar: a suspensão dura horas. "
        f"Se o material abaixo não responder, diga ao usuário que a busca "
        f"está degradada em vez de tentar de novo.\n\n"
    )


def _merge_results(per_query: list[list[dict]], domain_cap: int | None = None) -> list[dict]:
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

    # Sem demoção léxica (candidato sem palavra da pergunta no título/URL ia
    # para o fim). Ela existia para o SearXNG com 12 de 14 motores suspensos,
    # que casava a frase pela palavra funcional e devolvia verbete de
    # dicionário. No Google CSE ela só errava: medido em 10/09/2026, mesmos
    # resultados crus de 12 perguntas sem relação entre si, o top-5 mudou em
    # 3 e nas 3 para pior — rebaixou bcb.gov.br/conversao (cotação do
    # dólar), auth0 (OAuth2), paper do Raft no arxiv e guia de BG3 no
    # reddit, páginas certas cujo título usa outras palavras.
    def rank(r: dict) -> tuple[int, float]:
        try:
            score = float(r.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        return (-agreement.get(_normalize_url(r.get("url", "")), 0), -score)

    ranked = [sorted(results, key=rank) for results in per_query]

    # Teto por domínio: sem ele, uma busca cujo top-10 é todo do mesmo site
    # enche a reserva com um veículo só e o dossiê perde variedade (medido:
    # metade das fontes de um resultado saiu da mesma redação). 0 = sem teto.
    cap = config.RESEARCH_MAX_PER_DOMAIN if domain_cap is None else domain_cap
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
            if cap and per_domain.get(domain, 0) >= cap:
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
    index_first_class: bool = False,
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
    budget = page_budget or config.RESEARCH_PAGE_BUDGET

    char_budget = _dossier_char_budget()
    chars_used = 0

    def admit(r: dict, url: str, page: str) -> bool:
        """Põe a página no dossiê; False quando o orçamento de contexto acabou."""
        nonlocal chars_used
        # _render_dossier põe cabeçalho (título, URL, resumo da busca)
        # antes de cada página; o orçamento conta isso também.
        cost = len(page) + len(url) + len(r.get("title", "")) + len(r.get("content", "")) + 64
        if pages_read and chars_used + cost > char_budget:
            logger.info(
                "_read_pages: orçamento de contexto atingido (%d de %d caracteres, "
                "%d páginas); parando de ler e resumindo o que já tem",
                chars_used, char_budget, len(pages_read),
            )
            return False
        pages_read.append((r, url, page))
        chars_used += cost
        return True

    # Capa, seção e hub vão para a reserva em vez de sumir. A matéria vem
    # primeiro — ela responde; a capa é manchete solta e mistura dia com
    # reportagem antiga. Mas quando as ondas acabam sem matéria suficiente,
    # descartar a capa deixa vaga vazia. Medido em 10/09/2026, "principais
    # notícias do Brasil e do mundo hoje": 18 candidatos baixados, 13
    # descartados como índice/hub (g1, Estadão, CNN, Brasil 247, Al Jazeera,
    # news.un.org...), o dossiê fechou com 3 páginas e o bloco Brasil saiu
    # de um boletim setorial — as manchetes do dia estavam justamente nas
    # capas descartadas.
    #
    # index_first_class (o `recent` da pergunta): quando o dado é de agora, a
    # página de índice É onde ele mora — manchete do dia, cotação, placar,
    # previsão — e a matéria costuma ser a de ontem. Aí o índice entra na
    # ordem do ranking, sem esperar vaga sobrar. Critério de tempo, que quem
    # chama já declara; nada aqui olha o assunto. Medido em 10/09/2026, só
    # com reserva: "notícias de hoje" usou 1 capa das 11 reservadas (g1,
    # Estadão e Al Jazeera ficaram de fora, e o dossiê pegou a página de
    # seção da NPR com notícia de março); "cotação do dólar hoje" deixou na
    # reserva economia.uol.com.br/cotacoes e valor-data.
    reserve: list[tuple[dict, str, str]] = []

    for wave in range(config.RESEARCH_MAX_WAVES):
        if not queue or len(pages_read) >= budget:
            break
        remaining = budget - len(pages_read)
        batch, queue = queue[:remaining], queue[remaining:]
        pages = _scraper.read_many_dated([u for _, u in batch], mark_index=True)
        for (r, url), (page, page_date, is_index) in zip(batch, pages):
            r["_date"] = page_date
            if WebScraper.unusable(page):
                logger.error(
                    "_read_pages: onda %d, descartada url=%s (%d chars) motivo=%s",
                    wave + 1, url, len(page), page[:120],
                )
                continue
            if is_index or _is_hub_page(url, page):
                if index_first_class:
                    r["_index"] = True
                    if not admit(r, url, page):
                        return pages_read
                    continue
                logger.info(
                    "_read_pages: onda %d, reserva url=%s: capa/índice, só entra "
                    "se faltar matéria", wave + 1, url,
                )
                reserve.append((r, url, page))
                continue
            if not admit(r, url, page):
                return pages_read

    for r, url, page in reserve:
        if len(pages_read) >= budget:
            break
        r["_index"] = True
        if not admit(r, url, page):
            break
        logger.info("_read_pages: vaga preenchida pela reserva: url=%s", url)
    return pages_read


def _render_dossier(pages_read: list[tuple[dict, str, str]]) -> str:
    # Numerar as fontes ([1], [2]...) dá ao resumo um jeito de citar sem
    # escrever nome de veículo: o modelo aponta o número, e o código monta a
    # legenda número→URL — atribuição verificável, nunca inventada.
    dossier = "\n\n".join(
        f"===== FONTE [{i}] =====\n"
        f"Título: {r.get('title', '').strip()}\n"
        f"URL: {url}\n"
        f"Publicado em: {r.get('_date') or 'data não informada'}\n"
        + ("Tipo: capa/índice — lista de manchetes, não matéria apurada\n" if r.get("_index") else "")
        + ("Tipo: resultado de busca não lido — vale só o que o título e o trecho dizem\n"
           if r.get("_snippet_only") else "")
        + f"Resumo da busca: {r.get('content', '').strip()}\n"
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


# Triagem dos candidatos pelo título e trecho, antes de baixar qualquer um.
#
# O merge ordena por concordância e score, e o round-robin dá a mesma vaga a
# cada busca — inclusive à busca ruim. Medido em 10/09/2026: a busca pela
# pergunta inteira em linguagem natural trouxe 1 resultado do assunto e 9 PDFs
# acadêmicos que só casavam palavra solta ("pai", "ato", "inicia"), e a
# variante em palavras-chave trouxe 10 do assunto; no merge, os PDFs pegaram
# metade das vagas, entraram com 25k caracteres cada (a maior parte do
# prefill de 54k tokens, 53 s só para o modelo ler) e a página que respondia
# ficou fora do top 12. O mesmo em pergunta sem relação nenhuma: "doença
# cobreiro" tinha HIV e alergia (clevelandclinic, acaai) no top 6, vindos de
# uma variante que traduziu o nome errado.
#
# Uma chamada curta de LLM julgando o assunto provável de cada resultado
# resolve os três, porque não depende de palavra em comum — medido no mesmo
# dia em 3 perguntas sem relação entre si (jogo, lei, doença): 1,4-2,4 s,
# 3-7k caracteres de prompt, e as escolhas foram só páginas do assunto
# (bg3.wiki; gov.br/planalto/wikipedia; dasa/tuasaude/MSD/Ministério da
# Saúde), sem nenhum dos PDFs e páginas de outra doença que o merge punha no
# topo. Um corte por palavra da pergunta no título já foi tentado e errava
# para o outro lado (ver _merge_results): página certa com título em outras
# palavras.
_RERANK_INSTRUCTION = (
    "Você escolhe quais resultados de busca abrir para responder a uma "
    "pergunta. Recebe a pergunta e uma lista numerada de resultados "
    "(título, site, trecho). Devolva os números dos resultados com mais "
    "chance de conter a resposta, do mais para o menos provável, um número "
    "por linha, no máximo {k}, sem explicar. Julgue pelo assunto provável "
    "da página, não por palavra solta em comum: página sobre outro assunto "
    "que só repete uma palavra da pergunta não serve. O que a pergunta chama "
    "por um nome pode aparecer nos resultados com outro nome ou noutra "
    "língua; resultados que citam o nome exato ajudam a reconhecer isso. "
    "Cada página escolhida será baixada e lida como texto: vídeo, post de "
    "rede social e fórum com login quase nunca rendem o texto da resposta — "
    "prefira páginas com o conteúdo escrito (artigo, wiki, documentação, "
    "guia)."
)

# Vagas extras além do orçamento de páginas: parte dos escolhidos não abre
# (vídeo, muro de login) e a vaga passa para o próximo escolhido. Medido em
# 10/09/2026: com folga 4, 3 dos 4 escolhidos de uma pesquisa eram vídeo e o
# dossiê fechou com 1 página — o resumo negou o que a pergunta pedia.
_RERANK_SLACK = 6
_RERANK_TITLE_CHARS = 90
_RERANK_SNIPPET_CHARS = 160


def _rerank(query: str, candidates: list[dict], k: int) -> list[dict] | None:
    """Candidatos escolhidos pelo LLM, em ordem; None quando a triagem falha.

    None (LLM fora, resposta sem número válido) volta para a ordem do merge:
    sem triagem a pesquisa ainda funciona, só lê pior.
    """
    if len(candidates) <= 1:
        return None
    lines = [
        f"[{i}] {(r.get('title') or '').strip()[:_RERANK_TITLE_CHARS]} — "
        f"{urlsplit(r.get('url', '')).netloc} — "
        f"{(r.get('content') or '').strip()[:_RERANK_SNIPPET_CHARS]}"
        for i, r in enumerate(candidates, 1)
    ]
    try:
        content = chat(
            system=_RERANK_INSTRUCTION.format(k=k),
            user=f"Pergunta: {query}\n\nResultados:\n" + "\n".join(lines),
        )
    except Exception as e:
        logger.error("_rerank: LLM falhou, seguindo na ordem do merge: %s", e)
        return None
    picks: list[dict] = []
    seen: set[int] = set()
    for token in re.findall(r"\d+", content):
        n = int(token)
        if 1 <= n <= len(candidates) and n not in seen:
            seen.add(n)
            picks.append(candidates[n - 1])
        if len(picks) >= k:
            break
    if not picks:
        logger.error("_rerank: resposta sem número válido (%r), seguindo na ordem do merge", content[:200])
        return None
    logger.info("_rerank: %d de %d candidatos escolhidos", len(picks), len(candidates))
    return picks


# Trechos de busca que entram no dossiê sem a página: o escolhido pela
# triagem que não abriu (vídeo, muro de login) às vezes é justamente o que
# liga o nome da pergunta ao nome que as páginas lidas usam — medido em
# 10/09/2026: o título "Pista de Trumbo, Servo do Pai Putrefato" era a única
# ligação entre o nome localizado da pergunta e a wiki em inglês, e sumia
# junto com a página do vídeo. Título e trecho são curtos; o custo é nada.
_SNIPPET_SOURCES_MAX = 8


def _select_and_read(
    query: str, results: list[dict], recent: bool
) -> tuple[list[tuple[dict, str, str]], list[dict]]:
    """Triagem + leitura. Devolve (páginas lidas, escolhidos que não abriram).

    Só os escolhidos são lidos: o que a triagem deixou de fora foi julgado de
    outro assunto, e lê-lo só dilui o dossiê e alonga o prefill. A ordem do
    merge volta inteira apenas quando nenhum escolhido abre.
    """
    if not results:
        return [], []
    picks = _rerank(query, results, config.RESEARCH_PAGE_BUDGET + _RERANK_SLACK)
    if picks is None:
        return _read_pages(_cap_per_domain(results), index_first_class=recent), []
    # O teto de domínio vale na ordem da triagem: entre páginas do mesmo site
    # ficam as julgadas mais úteis, não as que o merge viu primeiro.
    picks = _cap_per_domain(picks)
    pages_read = _read_pages(picks, index_first_class=recent)
    if not pages_read:
        logger.info("_select_and_read: nenhum escolhido abriu; lendo o resto na ordem do merge")
        picked = {id(r) for r in picks}
        rest = _cap_per_domain([r for r in results if id(r) not in picked])
        pages_read = _read_pages(rest, index_first_class=recent)
    read = {url for _, url, _ in pages_read}
    unread = [r for r in picks if r.get("url", "").strip() not in read and (r.get("title") or r.get("content"))]
    return pages_read, unread[:_SNIPPET_SOURCES_MAX]


def _snippet_sources(unread: list[dict]) -> list[tuple[dict, str, str]]:
    """Escolhidos não lidos no formato de página do dossiê, marcados como trecho."""
    out = []
    for r in unread:
        r["_snippet_only"] = True
        out.append((r, r["url"].strip(), "(página não lida: só o título e o trecho do buscador acima)"))
    return out


def _build_dossier(query: str, recent: bool) -> tuple[str, list[tuple[dict, str, str]]]:
    """Busca, lê e monta o dossiê. Separado de _summarize para o eval
    conseguir o dossiê sem repesquisar."""
    results = _collect_links(query, recent)
    pages_read, unread = _select_and_read(query, results, recent)
    sources = pages_read + _snippet_sources(unread) if pages_read else []
    return _render_dossier(sources), sources


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
    "who how when where is are was were be been do does did "
    # Verbos e pronomes de função: aparecem na pergunta inteira em linguagem
    # natural e não dizem nada sobre o assunto — "Tem como limitar X?" e
    # "Posso limitar X?" são a mesma pergunta para o anti-repetição.
    "tem tenho temos ter tinha pode posso podem podemos poder podia deve "
    "devo devem dever preciso precisa precisam vai vou vamos vao vão "
    "eu ele ela eles elas voce você nos nós lhe dele dela isto aquele aquela "
    "nao não sim mas tambem também so só ainda ja mesmo cada qualquer "
    "has have had can could should would will shall may might must need "
    "it its this that these those they he she we you not but also only"
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

    Se o resumo disser que o material não responde, NÃO pesquise de novo
    trocando o nome por palpites seus: diga ao usuário o que não foi
    encontrado e peça o nome exato ou mais contexto.

    Args:
        query: A pergunta completa em linguagem natural, com as palavras do
            usuário. Nomes próprios, apelidos e termos que ele usou vão
            exatamente como ele escreveu: não traduza, não troque por um
            equivalente e não acrescente entre parênteses o que você acha
            que o nome significa — se o palpite estiver errado, a pesquisa
            inteira procura a coisa errada. Não reduza a palavras-chave nem
            parta em pedaços: a reformulação em termos de busca é feita aqui
            dentro, e uma pergunta inteira dá um resultado melhor que um
            fragmento.
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

    _search.reset_health()
    try:
        results = _collect_links(query, recent)
    except requests.RequestException as e:
        logger.error("research_web: busca falhou para query=%r: %s", query, e)
        return f"Erro ao consultar a busca: {e}"

    if not results:
        logger.info("research_web: nenhum resultado para query=%r", query)
        # Também entra no cache anti-loop: resultado vazio é o gatilho mais
        # comum de re-chamada em círculo.
        outcome = _search_health_note(results) + (
            "Nenhum resultado encontrado. Não repita a busca com a mesma "
            "pergunta reescrita; diga ao usuário que não encontrou."
        )
        _remember_result(query, outcome)
        return outcome

    pages_read, unread = _select_and_read(query, results, recent)
    if not pages_read:
        logger.error("research_web: todas as %d páginas candidatas falharam para query=%r", len(results), query)
        outcome = _search_health_note(results) + "Nenhuma das páginas encontradas pôde ser lida."
        _remember_result(query, outcome)
        return outcome

    # Daqui em diante "pages_read" inclui os trechos de busca: eles são
    # numerados, citáveis e aparecem na legenda como qualquer fonte, marcados.
    pages_read = pages_read + _snippet_sources(unread)
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
    sources = "\n".join(
        f"[{i}] {url}" + (" (só título e trecho da busca)" if r.get("_snippet_only") else "")
        for i, (r, url, _) in enumerate(pages_read, 1)
    )

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
        "marcador sem link é referência que o usuário não consegue conferir. "
        "Se o resumo disser que o material não responde, não pesquise de "
        "novo com nomes ou termos supostos por você: diga ao usuário o que "
        "não foi encontrado e peça o nome exato ou mais contexto."
    )

    now = datetime.now().astimezone()
    utc_now = now.astimezone(timezone.utc)
    stamp = (
        f"Pesquisa realizada em {now.strftime('%d/%m/%Y %H:%M')} {_format_offset(now)} "
        f"({utc_now.strftime('%H:%M')} UTC)."
    )
    logger.info("research_web ok: query=%r páginas_lidas=%d", query, len(pages_read))
    result = (
        f"{_search_health_note(results)}{stamp}\n\n{summary}\n\n"
        f"{relay_note}\n\nURLs consultadas:\n{sources}"
    )
    _remember_result(query, result)
    return result
