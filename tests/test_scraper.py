import socket
from unittest.mock import patch

import pytest

from web_search_mcp.util.scraper import WebScraper, _is_chrome

FIXTURE_HTML = """
<html>
<head><title>Página de título</title></head>
<body>
<nav>Menu Início Sobre Contato</nav>
<header class="topo">Cabeçalho do site</header>
<h1>Título principal do artigo</h1>
<article>
<p>Este é o parágrafo principal do artigo, com conteúdo relevante e longo o suficiente.</p>
<p>Este é o parágrafo principal do artigo, com conteúdo relevante e longo o suficiente.</p>
<div class="cookie-consent">Usamos cookies para melhorar sua experiência de navegação.</div>
</article>
<footer>Rodapé - Copyright 2026 - Todos os direitos reservados</footer>
</body>
</html>
"""


class TestIsSafeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/",
            "http://127.0.0.1/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://169.254.169.254/",
            "file:///etc/passwd",
        ],
    )
    def test_rejects_unsafe(self, url):
        assert WebScraper._is_safe_url(url) is False

    def test_rejects_nonexistent_host(self):
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no address")):
            assert WebScraper._is_safe_url("http://host-que-nao-existe.invalid/") is False

    def test_accepts_public_host(self):
        with patch("socket.gethostbyname", return_value="93.184.216.34"):
            assert WebScraper._is_safe_url("http://example.com/") is True


class TestIsChrome:
    def test_sidebar_widget_is_chrome(self):
        el = type("El", (), {"tag": "div", "get": lambda self, k, d="": {"class": "sidebarWidget"}.get(k, d)})()
        assert _is_chrome(el) is True

    def test_downloads_is_not_chrome(self):
        el = type("El", (), {"tag": "div", "get": lambda self, k, d="": {"class": "downloads"}.get(k, d)})()
        assert _is_chrome(el) is False


class TestClean:
    def test_strips_chrome_and_extracts_title(self):
        result = WebScraper._clean(FIXTURE_HTML)
        assert "Título principal do artigo" in result
        assert "parágrafo principal do artigo" in result
        assert "Menu Início Sobre Contato" not in result
        assert "Cabeçalho do site" not in result
        assert "Copyright 2026" not in result

    def test_duplicate_line_removed(self):
        result = WebScraper._clean(FIXTURE_HTML)
        assert result.count("Este é o parágrafo principal do artigo") == 1

    def test_short_line_removed(self):
        html = "<html><body><article><p>ok</p><p>Um parágrafo suficientemente longo para não ser cortado.</p></article></body></html>"
        result = WebScraper._clean(html)
        assert "ok" not in result.splitlines()

    def test_max_chrome_share_protects_dense_block(self):
        # class de casca ("sidebar") mas concentra quase todo o texto: não some.
        html = (
            '<html><body><div class="sidebar">'
            + "Conteúdo denso que ocupa a página inteira e não deveria ser removido. " * 20
            + "</div></body></html>"
        )
        result = WebScraper._clean(html)
        assert "Conteúdo denso" in result


class TestFailed:
    def test_recognizes_each_notice(self):
        assert WebScraper.failed("(URL bloqueada por segurança)") is True
        assert WebScraper.failed("(não foi possível ler a página: timeout)") is True
        assert WebScraper.failed("(sem conteúdo extraível)") is True

    def test_normal_content_not_confused(self):
        assert WebScraper.failed("Conteúdo normal da página, nada de aviso aqui.") is False


class TestUnusable:
    def test_explicit_failure_is_unusable(self):
        assert WebScraper.unusable("(sem conteúdo extraível)")
        assert WebScraper.unusable("(não foi possível ler a página: 403)")

    def test_short_page_is_unusable_but_not_failed(self):
        curta = "x" * 100
        assert WebScraper.unusable(curta)
        # failed() continua só para aviso explícito: o read_url devolve
        # página curta sem tratar como erro.
        assert not WebScraper.failed(curta)

    def test_real_content_is_usable(self):
        assert not WebScraper.unusable("x" * 5000)


class TestLinkDensity:
    _INDEX = (
        "<html><body><main>"
        + "".join(f'<div><a href="/n{i}">Manchete número {i} sobre um assunto</a></div>' for i in range(40))
        + "</main></body></html>"
    )
    _ARTICLE = (
        "<html><body><main><h1>Título da matéria</h1>"
        + "".join(f"<p>{'texto corrido de parágrafo real. ' * 20}</p>" for _ in range(10))
        + '<p>Leia também <a href="/outra">esta outra matéria</a>.</p>'
        + "</main></body></html>"
    )

    def test_index_page_has_high_density(self):
        assert WebScraper.link_density(self._INDEX) >= 0.65

    def test_article_has_low_density(self):
        assert WebScraper.link_density(self._ARTICLE) < 0.65

    def test_broken_html_returns_zero(self):
        assert WebScraper.link_density("") == 0.0

    def test_reject_index_discards_only_when_asked(self):
        sc = WebScraper()
        assert sc._extract(self._INDEX, reject_index=True).startswith("(página de índice")
        assert not sc._extract(self._INDEX, reject_index=False).startswith("(página de índice")

    def test_rejected_index_counts_as_unusable(self):
        sc = WebScraper()
        assert WebScraper.unusable(sc._extract(self._INDEX, reject_index=True))

    def test_article_survives_reject_index(self):
        sc = WebScraper()
        out = sc._extract(self._ARTICLE, reject_index=True)
        assert not WebScraper.failed(out)
        assert "parágrafo real" in out


class TestRedirected:
    """Corpus de assuntos não relacionados, nos DOIS desfechos.

    A regra tem que aceitar canonicalização (o caso comum e benigno) e
    recusar 3xx que troca de página (o que trava o agente em chute de
    endereço). Medido em 29/08/2026 contra tailscale.com/kb/9999999/x, que
    devolve 308 -> /docs com HTTP 200 e 2.2k chars de conteúdo válido.
    """

    @pytest.mark.parametrize("requested,final", [
        # mesma página: só canonicalização
        ("http://exemplo.com/artigo", "https://exemplo.com/artigo"),
        ("https://www.bbc.com/news/123", "https://bbc.com/news/123"),
        ("https://pypi.org/project/requests/", "https://pypi.org/project/requests"),
        ("https://loja.com/item/42", "https://loja.com/item/42?utm_source=x"),
        ("https://Docs.Python.ORG/3/library/os.html", "https://docs.python.org/3/library/os.html"),
    ])
    def test_canonicalizacao_nao_e_redirect(self, requested, final):
        assert WebScraper.redirected(requested, final) is False
        assert WebScraper.redirect_notice(requested, final) == ""

    @pytest.mark.parametrize("requested,final", [
        # caiu em outra página: soft 404, hub genérico, domínio trocado
        ("https://tailscale.com/kb/9999999/nao-existe", "https://tailscale.com/docs"),
        ("https://bg3.wiki/wiki/Pagina_Inventada", "https://bg3.wiki/wiki/Main_Page"),
        ("https://receitas.com/bolo-de-cenoura", "https://receitas.com/"),
        ("https://banco.com.br/tarifas/2019", "https://banco.com.br/atendimento"),
        ("https://old.site.com/manual", "https://novo.site.com/manual"),
    ])
    def test_pagina_trocada_e_redirect(self, requested, final):
        assert WebScraper.redirected(requested, final) is True
        notice = WebScraper.redirect_notice(requested, final)
        assert requested in notice and final in notice
        assert "não existe ou foi movida" in notice

    def test_final_vazio_nao_avisa(self):
        assert WebScraper.redirect_notice("https://x.com/a", "") == ""
