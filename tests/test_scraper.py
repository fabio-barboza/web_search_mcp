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
