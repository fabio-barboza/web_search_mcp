import ipaddress
import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlsplit

import lxml.etree
import lxml.html
import requests
import trafilatura

from .. import config

logger = logging.getLogger(__name__)

# Tags que nunca carregam conteúdo e a casca de navegação da página.
_DISCARD_TAGS = (
    "script style noscript template iframe svg canvas form button select "
    "nav header footer aside figure noframes"
).split()

# Palavras que aparecem em class/id de menu, rodapé, banner e afins. O
# conteúdo raramente usa esses nomes.
_DISCARD_MARKS = (
    "menu nav navbar navegacao breadcrumb topo rodape footer header sidebar "
    "side-bar cookie consent lgpd banner publicidade advert ads anuncio "
    "newsletter assine subscribe paywall comentario comment social share "
    "compartilh relacionad related recomendad popup modal skip-link"
).split()

_XPATH_TAGS = " | ".join(f"//{t}" for t in _DISCARD_TAGS)

# Um bloco com class/id de casca que concentre boa parte do texto da página
# não é casca: é o conteúdo com nome ruim (ex.: <body class="has-sidebar">).
# Remover por nome sem esse teto apagava a página inteira.
_MAX_CHROME_SHARE = 0.4

# Início dos avisos devolvidos no lugar do conteúdo quando a leitura falha.
_FAILURE_NOTICES = (
    "(URL bloqueada por segurança)",
    "(não foi possível ler a página",
    "(sem conteúdo extraível)",
    "(página de índice: só links, sem conteúdo próprio)",
)

# Fração do texto que está dentro de <a> acima da qual a página é um índice
# (capa de portal, página de tópico, hub de links) e não conteúdo próprio.
#
# Medido: folha-topicos/inteligencia-artificial 0.98, capa da CNN Brasil 0.73;
# do outro lado, wiki de classe do BG3 0.39, fextralife 0.35, Wikipédia 0.30,
# página de item do bg3.wiki 0.24, matéria do g1 0.02. O corte em 0.65 fica no
# meio do vão, sem encostar na página de item — que é lista de atributos, tem
# a MESMA cara de índice pelo tamanho das linhas, e é justamente o que uma
# pergunta sobre equipamento precisa ler.
_MAX_LINK_DENSITY = 0.65

# Abaixo disto a página não tem o que aproveitar, mesmo tendo devolvido 200 e
# algum texto: é tela de login, muro de cookie, "ative o JavaScript" ou casca
# que sobreviveu à extração.
#
# Medido em 25 páginas de 5 pesquisas: 4 delas (16%) caíam aqui — chatgpt.com
# com 229 caracteres, mapgenie.io com 181, sede.funciona.gob.es com 12. Todas
# contavam como leitura bem-sucedida e queimavam uma das 5 vagas do
# RESEARCH_PAGE_BUDGET, que então não era reposta pela reserva.
#
# 600 é folgado: a menor página legítima que vi nessa amostra tinha 1614.
_MIN_USEFUL_CHARS = 600

# Blocos que devem virar quebra de linha no texto final.
_BLOCK_TAGS = {
    "p", "div", "li", "tr", "br", "section", "article", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "td", "th", "dt", "dd",
}

# Cabeçalhos de browser real. "Mozilla/5.0" sozinho é um UA truncado que não
# corresponde a browser nenhum, e servidor com filtro de bot devolve 406 Not
# Acceptable (medido em sejaceo.com: "Mozilla/5.0" = 406, UA completo = 200).
# Accept/Accept-Language sem o UA completo não bastam: também dá 406.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}


def _is_chrome(el) -> bool:
    """True quando class/id do elemento denuncia navegação, banner ou afins."""
    if not isinstance(el.tag, str):
        return False
    raw = f"{el.get('class', '')} {el.get('id', '')}".lower()
    if not raw.strip():
        return False
    for token in re.split(r"[^a-z0-9]+", raw):
        # startswith em vez de "in": "sidebarWidget" é casca, "downloads" não.
        if token and any(token.startswith(m) for m in _DISCARD_MARKS):
            return True
    return False


def _remove(root, elements) -> None:
    """Remove elementos ainda ligados à raiz (o pai pode já ter saído)."""
    for el in elements:
        node = el
        while node is not None and node is not root:
            node = node.getparent()
        if node is root and el.getparent() is not None:
            el.getparent().remove(el)


class WebScraper:
    """Baixa URLs e extrai o conteúdo principal em Markdown.

    Não é uma tool: é chamado direto pelo Python, via HTTP.
    """

    def __init__(self, limit: int | None = config.SCRAPER_LIMIT, timeout: int = config.SCRAPER_TIMEOUT):
        # Sem limite por padrão: o teto fixo cortava justamente as páginas
        # de maior densidade (agregadores de "manchetes do dia", com 30+
        # itens em lista), enquanto em matéria comum quase não fazia falta.
        # Quem quiser teto passa limit explícito.
        self.limit = limit
        self.timeout = timeout

    def read_many_dated(
        self, urls: list[str], reject_index: bool = False
    ) -> list[tuple[str, str | None]]:
        """read_many + a data de publicação de cada página, quando houver.

        Quem monta dossiê precisa da data: sem ela, matéria velha entra como
        se fosse de hoje. Medido: "previsão do tempo para os próximos 5 dias"
        trouxe uma matéria publicada uma semana antes e o resumo apresentou
        os dias 23-26 como sendo os próximos — errado, e sem nenhum sinal de
        que era material vencido.
        """
        return [(text, date) for text, date, _ in self._fetch(urls, reject_index)]

    def read_many_located(
        self, urls: list[str], reject_index: bool = False
    ) -> list[tuple[str, str]]:
        """read_many + a URL final de cada página, depois dos redirects.

        Quem pediu uma URL específica precisa saber se caiu em outra. Um
        redirect 3xx para uma página genérica é indistinguível de um acerto
        quando só o texto volta: o servidor responde 200, a extração dá
        conteúdo, e nada denuncia que o endereço pedido não existe. Sem esse
        sinal não há condição de parada, e quem chamou fica chutando
        endereços e recebendo sempre a mesma página.
        """
        return [(text, final) for text, _, final in self._fetch(urls, reject_index)]

    def _fetch(
        self, urls: list[str], reject_index: bool = False
    ) -> list[tuple[str, str | None, str]]:
        """Baixa e extrai: (texto, data de publicação, URL final)."""
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=len(urls)) as pool:
            downloaded = list(pool.map(self._download, urls))
        out: list[tuple[str, str | None, str]] = []
        for (ok, html, final), requested in zip(downloaded, urls):
            if not ok:
                out.append((html, None, final or requested))
                continue
            out.append((self._extract(html, reject_index), self._page_date(html), final))
        return out

    @staticmethod
    def _page_date(html: str) -> str | None:
        """Data de publicação declarada pela página (YYYY-MM-DD), se houver."""
        try:
            meta = trafilatura.extract_metadata(html)
        except Exception:  # metadados malformados não podem derrubar a leitura
            return None
        return getattr(meta, "date", None) if meta else None

    def read_many(self, urls: list[str], reject_index: bool = False) -> list[str]:
        """Lê várias URLs: download em paralelo, extração serial.

        Só o download é paralelizado. A extração roda no thread principal
        porque o lxml (dentro do trafilatura) é extensão C e uso concorrente
        é o suspeito de um crash de heap (malloc_consolidate). Medido: a
        extração leva ~0,2s para 10 páginas em série contra ~0,2s em
        paralelo — não há nada a ganhar arriscando.
        """
        return [text for text, _, _ in self._fetch(urls, reject_index)]

    def read(self, url: str) -> str:
        """Lê uma única URL."""
        return self.read_many([url])[0]

    @staticmethod
    def link_density(html: str) -> float:
        """Fração do texto da página que está dentro de links.

        Índice (capa, página de tópico) é quase só âncora; conteúdo próprio
        tem texto que não é link. Mede depois da poda de casca, senão o menu
        de navegação sozinho já joga qualquer página para cima.
        """
        try:
            root = lxml.html.fromstring(html)
        except (lxml.etree.ParserError, ValueError):
            return 0.0
        _remove(root, root.xpath(_XPATH_TAGS))
        total_all = max(len(root.text_content()), 1)
        _remove(root, [
            el for el in root.iter()
            if _is_chrome(el) and len(el.text_content()) < total_all * _MAX_CHROME_SHARE
        ])
        body = root
        for xp in ("//main", "//article"):
            found = root.xpath(xp)
            if found and len(found[0].text_content()) > 400:
                body = found[0]
                break
        total = max(len(" ".join(body.text_content().split())), 1)
        anchor = sum(len(" ".join(a.text_content().split())) for a in body.xpath(".//a"))
        return anchor / total

    @staticmethod
    def redirected(requested: str, final: str) -> bool:
        """True quando a página entregue não é o endereço que foi pedido.

        Compara só host (sem www) e caminho (sem barra final), ignorando
        esquema e query: canonicalização (http->https, // final, ?utm=) não
        é o agente ter caído em outro lugar. Um 3xx que troca o caminho é.
        """
        def key(u: str) -> tuple[str, str]:
            parts = urlsplit(u)
            host = parts.netloc.lower().removeprefix("www.")
            return host, parts.path.rstrip("/").lower()

        try:
            return key(requested) != key(final)
        except ValueError:  # URL malformada: sem base de comparação
            return False

    @staticmethod
    def redirect_notice(requested: str, final: str) -> str:
        """Aviso a prefixar no texto quando houve redirect, senão vazio."""
        if not final or not WebScraper.redirected(requested, final):
            return ""
        return (
            f"(atenção: {requested} não existe ou foi movida — o servidor "
            f"redirecionou para {final}, e o texto abaixo é dessa outra "
            f"página. Não tente adivinhar variações do endereço pedido: "
            f"procure o link real a partir da página entregue ou de uma busca.)\n\n"
        )

    @staticmethod
    def failed(text: str) -> bool:
        """True quando o texto é um aviso de falha, não conteúdo da página.

        Quem monta a lista de fontes precisa saber quais URLs realmente
        foram lidas — citar uma página que não abriu é pior que não citar.
        """
        return text.startswith(_FAILURE_NOTICES)

    @staticmethod
    def unusable(text: str) -> bool:
        """True quando não há o que aproveitar: falha explícita OU texto curto
        demais para ser conteúdo.

        Separado de failed() de propósito. Quem pede uma URL específica
        (read_url) quer de volta o que houver, por menor que seja; quem está
        montando um dossiê com vagas contadas (research) precisa recusar a
        página vazia para a vaga passar ao próximo candidato da reserva.
        """
        return WebScraper.failed(text) or len(text.strip()) < _MIN_USEFUL_CHARS

    def _download(self, url: str) -> tuple[bool, str, str]:
        """Devolve (sucesso, html, URL final) ou (False, motivo, URL pedida)."""
        if not self._is_safe_url(url):
            logger.error("download bloqueado por segurança: url=%s", url)
            return False, "(URL bloqueada por segurança)", url
        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            # Devolve texto em vez de levantar: uma página ruim não deve
            # derrubar a pesquisa inteira.
            logger.error("download falhou: url=%s erro=%s", url, e)
            return False, f"(não foi possível ler a página: {e})", url
        return True, resp.text, resp.url or url

    def _extract(self, html: str, reject_index: bool = False) -> str:
        # Índice não responde pergunta: o modelo lê manchete solta e serve
        # chamada de capa como se fosse fato apurado, tudo sob uma fonte só.
        # Só o research fora do panorama rejeita — o panorama LÊ capa de
        # propósito, e o read_url/analyze_urls devolve a página que pediram.
        if reject_index and self.link_density(html) >= _MAX_LINK_DENSITY:
            return "(página de índice: só links, sem conteúdo próprio)"

        # trafilatura isola o conteúdo principal (sem menu/nav/rodapé/scripts).
        # favor_recall: páginas de dados (clima, cotação) põem o número em
        # widgets curtos, que o modo preciso descarta como boilerplate.
        text = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=False,
            include_tables=True,
            favor_recall=True,
        ) or ""

        # Fallback: a extração de "conteúdo principal" mira o miolo do artigo
        # e poda como boilerplate o que estiver fora dele — em perfis (bio do
        # GitHub) e em agregadores de manchetes, o dado que interessa mora
        # justamente aí. Sintoma: texto curto demais para a página.
        if len(text) < 1200:
            raw = self._clean(html)
            if len(raw) > len(text) * 1.5:
                text = raw

        if not text:
            return "(sem conteúdo extraível)"
        # limit=None lê a página inteira (usado quando o usuário pede uma URL
        # explícita); text[:None] devolve o texto completo.
        return text[: self.limit]

    @staticmethod
    def _clean(html: str) -> str:
        """Extrai título e conteúdo, descartando a casca da página.

        Substitui o trafilatura.html2txt, que devolvia a página inteira sem
        limpar: menu, rodapé e banner de cookie vinham no topo e eram o
        primeiro texto que o LLM lia. Aqui a poda é estrutural (tags e
        class/id de navegação) em vez de por tamanho de linha, para não
        matar as manchetes curtas de uma página de agregador.
        """
        try:
            root = lxml.html.fromstring(html)
        except (lxml.etree.ParserError, ValueError):
            return ""

        title = ""
        for xp in ("//h1", "//title"):
            found = root.xpath(xp)
            if found and found[0].text_content().strip():
                title = found[0].text_content().strip()
                break

        _remove(root, root.xpath(_XPATH_TAGS))

        # Poda por class/id, protegendo quem concentra o texto da página.
        total = max(len(root.text_content()), 1)
        chrome = [
            el
            for el in root.iter()
            if _is_chrome(el) and len(el.text_content()) < total * _MAX_CHROME_SHARE
        ]
        _remove(root, chrome)

        # Prefere <main>/<article> quando existem e têm corpo; senão, body.
        body = root
        for xp in ("//main", "//article"):
            found = root.xpath(xp)
            if found and len(found[0].text_content()) > 400:
                body = found[0]
                break

        # text_content() cola tudo numa linha só: marca fim de bloco antes.
        for el in body.iter():
            if el.tag in _BLOCK_TAGS:
                el.tail = (el.tail or "") + "\n"

        lines, seen = [], set()
        for line in body.text_content().splitlines():
            line = " ".join(line.split())
            # Menu e rodapé repetem o mesmo texto em vários lugares da
            # página; manchete não repete. Descartar duplicata limpa o que
            # a poda estrutural deixou passar.
            if len(line) < 12 or line in seen:
                continue
            seen.add(line)
            lines.append(line)

        body_text = "\n".join(lines)
        return f"# {title}\n\n{body_text}" if title else body_text

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """Bloqueia SSRF: recusa esquema não-http e IP privado/loopback/interno."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(parsed.hostname))
        except (socket.gaierror, ValueError):
            return False
        return not (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        )
