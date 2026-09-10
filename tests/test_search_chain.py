from unittest.mock import MagicMock, patch

import pytest
import requests

from web_search_mcp import config
from web_search_mcp.tools import research
from web_search_mcp.util.google_cse import GoogleCSE, GoogleUnavailable
from web_search_mcp.util.search_chain import SearchChain, build_search
from web_search_mcp.util.searxng import SearXNG

_GOOGLE_RESULTS = [{"url": "https://a.com/1", "title": "a", "engines": ["google cse (tor-a)"]}]
_SEARXNG_RESULTS = [{"url": "https://b.com/2", "title": "b", "engines": ["brave"]}]


def _google(down=False):
    g = MagicMock(spec=GoogleCSE)
    g.max_results = 10
    g.stats.return_value = {"consultas": 1}
    if down:
        g.search.side_effect = GoogleUnavailable("barrado")
        g.health.return_value = {"google cse": "indisponível via Tor e direto"}
    else:
        g.search.return_value = list(_GOOGLE_RESULTS)
        g.health.return_value = {}
    return g


def _searxng(down=None):
    s = MagicMock(spec=SearXNG)
    s.search.return_value = list(_SEARXNG_RESULTS)
    s.health.return_value = down or {}
    return s


class TestSearchChain:
    def test_google_ok_skips_searxng(self):
        fallback = _searxng()
        chain = SearchChain(_google(), fallback)
        assert chain.search("q") == _GOOGLE_RESULTS
        fallback.search.assert_not_called()
        assert chain.health() == {}

    def test_google_down_falls_back_to_searxng(self):
        fallback = _searxng(down={"bing": "CAPTCHA"})
        chain = SearchChain(_google(down=True), fallback)
        assert chain.search("q", time_range="day") == _SEARXNG_RESULTS
        fallback.search.assert_called_once_with("q", time_range="day")
        assert set(chain.health()) == {"bing", "google cse"}
        assert chain.stats()["caminho:searxng"] == 1

    def test_no_fallback_propagates(self):
        chain = SearchChain(_google(down=True), None)
        with pytest.raises(requests.RequestException):
            chain.search("q")

    def test_reset_health_reaches_both(self):
        google, fallback = _google(), _searxng()
        SearchChain(google, fallback).reset_health()
        google.reset_health.assert_called_once()
        fallback.reset_health.assert_called_once()


class TestHealthNote:
    """A regra "busca degradada tem que ser dita" vale no caminho novo."""

    def test_google_down_is_announced(self, monkeypatch):
        chain = SearchChain(_google(down=True), _searxng(down={"bing": "CAPTCHA"}))
        monkeypatch.setattr(research, "_search", chain)
        results = chain.search("q")
        note = research._search_health_note(results)
        assert "AVISO DE INFRAESTRUTURA" in note
        assert "google cse" in note

    def test_google_ok_has_no_note(self, monkeypatch):
        chain = SearchChain(_google(), _searxng(down={"bing": "CAPTCHA", "brave": "CAPTCHA"}))
        monkeypatch.setattr(research, "_search", chain)
        assert research._search_health_note(chain.search("q")) == ""

    def test_research_web_reports_search_error(self, monkeypatch):
        monkeypatch.setattr(research, "_search", SearchChain(_google(down=True), None))
        with patch.object(research, "_generate_queries", return_value=["pergunta sem fallback"]):
            out = research.research_web("pergunta sem fallback")
        assert out.startswith("Erro ao consultar a busca")


class TestBuildSearch:
    def test_searxng_backend_is_plain_searxng(self, monkeypatch):
        monkeypatch.setattr(config, "SEARCH_BACKEND", "searxng")
        assert type(build_search()) is SearXNG

    def test_google_tor_backend(self, monkeypatch):
        monkeypatch.setattr(config, "SEARCH_BACKEND", "google_tor")
        monkeypatch.setattr(config, "SEARXNG_FALLBACK", True)
        chain = build_search()
        assert isinstance(chain, SearchChain)
        assert isinstance(chain.fallback, SearXNG)

    def test_searxng_fallback_off(self, monkeypatch):
        monkeypatch.setattr(config, "SEARCH_BACKEND", "google_tor")
        monkeypatch.setattr(config, "SEARXNG_FALLBACK", False)
        assert build_search().fallback is None
