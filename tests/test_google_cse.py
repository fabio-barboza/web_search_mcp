import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from web_search_mcp.util import google_cse
from web_search_mcp.util.google_cse import (
    GoogleCSE,
    GoogleUnavailable,
    date_sort,
    parse_jsonp,
    parse_token,
)
from web_search_mcp.util.tor import ChannelPool, TorChannel

CSE_JS = (
    "(function(){var a=1;})();\n"
    'google.cse.init({"cx": "cx1", "cse_token": "TOK1", '
    '"cselibVersion": "LIBV", "exp": ["cc", "sps"]});'
)


def _resp(text, status=200):
    r = MagicMock()
    r.text = text
    r.status_code = status
    r.ok = status < 400
    return r


def _jsonp(payload):
    return "/*O_o*/\n_(" + json.dumps(payload) + ");"


def _items(*urls, content="texto"):
    return [
        {"unescapedUrl": u, "titleNoFormatting": f"t{i}", "contentNoFormatting": content}
        for i, u in enumerate(urls)
    ]


OK = _jsonp({"results": _items("https://a.com/1", "https://b.com/2")})
# Formatos medidos em 10/09/2026 (bloqueio e token recusado vêm com HTTP 200).
BLOCKED = _jsonp({"error": {"code": 429, "message": "Our systems have detected unusual traffic from your network."}})
BAD_TOKEN = _jsonp({"error": {"code": 403, "message": "Unauthorized access to internal API."}})
NOTHING = _jsonp({"cursor": {}, "findMoreOnGoogle": {}, "spelling": {}})


def _via(proxies):
    """"tor-a#0" = canal tor-a, credencial 0; "direto" = sem proxy."""
    if not proxies:
        return "direto"
    cred = proxies["https"].split("//", 1)[1].split(":", 1)[0]
    name, _nonce, generation = cred.rsplit("-", 2)
    return f"{name}#{generation}"


class FakeGoogle:
    """requests.get falso: `search(via, params)` decide a resposta da busca."""

    def __init__(self, search, token=lambda via: _resp(CSE_JS)):
        self._search = search
        self._token = token
        self.searches: list[tuple[str, dict]] = []
        self.tokens: list[str] = []

    def __call__(self, url, params=None, proxies=None, **kwargs):
        via = _via(proxies)
        if url == google_cse._TOKEN_URL:
            self.tokens.append(via)
            return self._token(via)
        self.searches.append((via, params))
        r = self._search(via, params)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def vias(self):
        return [v for v, _ in self.searches]


def _client(direct=True):
    pool = ChannelPool([
        TorChannel("tor-a", "127.0.0.1", 9060, 9061),
        TorChannel("tor-b", "127.0.0.1", 9070, 9071),
    ])
    return GoogleCSE(pool, cx="cx1", hl="pt-BR", timeout=5, direct_fallback=direct, max_results=10)


def _run(client, fake, *args, **kwargs):
    with patch.object(google_cse.requests, "get", side_effect=fake):
        return client.search(*args, **kwargs)


def _tor_blocked(via, params):
    return _resp(BLOCKED) if via.startswith("tor-") else _resp(OK)


class TestParse:
    def test_token(self):
        assert parse_token(CSE_JS) == {"cse_tok": "TOK1", "cselibv": "LIBV", "exp": "cc,sps"}

    def test_token_without_exp(self):
        assert parse_token('x({"cse_token": "T"});')["exp"] == ""

    def test_token_missing_raises(self):
        with pytest.raises(ValueError):
            parse_token('x({"cx": "cx1"});')
        with pytest.raises(ValueError):
            parse_token("<html>sorry</html>")

    def test_jsonp(self):
        assert parse_jsonp(OK)["results"][0]["unescapedUrl"] == "https://a.com/1"


class TestResults:
    def test_maps_fields_and_decreasing_score(self):
        items = _items("https://a.com/1", "https://b.com/2", "https://c.com/3")
        items.insert(1, {"titleNoFormatting": "sem url"})
        fake = FakeGoogle(lambda via, p: _resp(_jsonp({"results": items})))
        out = _run(_client(), fake, "q")

        assert [r["url"] for r in out] == ["https://a.com/1", "https://b.com/2", "https://c.com/3"]
        assert out[0]["title"] == "t0"
        assert out[0]["content"] == "texto"
        assert out[0]["engines"] == ["google cse (tor-a)"]
        scores = [r["score"] for r in out]
        assert scores == sorted(scores, reverse=True) and len(set(scores)) == 3

    def test_request_params(self):
        fake = FakeGoogle(lambda via, p: _resp(OK))
        _run(_client(), fake, "raft consensus")
        params = fake.searches[0][1]
        assert params["q"] == "raft consensus"
        assert params["cx"] == "cx1"
        assert params["cse_tok"] == "TOK1"
        assert params["cselibv"] == "LIBV"
        assert params["exp"] == "cc,sps"
        assert params["hl"] == "pt-BR"
        assert params["num"] == "20"
        # Sem lr: nada restringe o idioma do resultado.
        assert "lr" not in params
        assert "sort" not in params

    def test_date_sort(self):
        now = datetime(2026, 9, 10, 12)
        assert date_sort("day", now) == "date:r:20260909:20260910"
        assert date_sort("week", now) == "date:r:20260903:20260910"
        assert date_sort("year", now) == "date:r:20250910:20260910"
        assert date_sort("century", now) is None

    def test_time_range_goes_to_sort(self):
        fake = FakeGoogle(lambda via, p: _resp(OK))
        _run(_client(), fake, "q", time_range="day")
        assert fake.searches[0][1]["sort"].startswith("date:r:")

    def test_empty_results_is_not_a_block(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(NOTHING))
        assert _run(client, fake, "zxqvbnm") == []
        assert fake.vias == ["tor-a#0"]
        assert all(c.healthy() for c in client.pool.channels)

    def test_snippet_mentioning_unusual_traffic_is_not_a_block(self):
        # Bloqueio se decide pelo campo de erro, nunca por texto no corpo:
        # senão uma pergunta SOBRE isso viraria failover.
        body = _jsonp({"results": _items("https://a.com/1", content="Our systems have detected unusual traffic")})
        fake = FakeGoogle(lambda via, p: _resp(body))
        assert len(_run(_client(), fake, "q")) == 1
        assert fake.vias == ["tor-a#0"]


class TestFailover:
    def test_blocked_channel_fails_over_immediately(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(BLOCKED) if via.startswith("tor-a") else _resp(OK))
        start = time.monotonic()
        with patch("time.sleep", side_effect=AssertionError("sleep no failover")):
            out = _run(client, fake, "q")
        assert time.monotonic() - start < 0.5

        assert fake.vias == ["tor-a#0", "tor-b#0"]
        assert out[0]["engines"] == ["google cse (tor-b)"]
        a, b = client.pool.channels
        assert not a.healthy()  # renovando: fora do rodízio
        assert b.healthy()

    def test_both_blocked_retries_first_with_new_circuit_then_direct(self):
        fake = FakeGoogle(_tor_blocked)
        out = _run(_client(), fake, "q")
        assert fake.vias == ["tor-a#0", "tor-b#0", "tor-a#1", "direto"]
        assert out[0]["engines"] == ["google cse (direto)"]

    def test_everything_blocked_raises_and_reports_health(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(BLOCKED))
        with pytest.raises(GoogleUnavailable) as exc:
            _run(client, fake, "q")
        assert isinstance(exc.value, requests.RequestException)
        assert len(fake.searches) == 4
        assert "google cse" in client.health()
        client.reset_health()
        assert client.health() == {}

    def test_no_direct_fallback(self):
        fake = FakeGoogle(lambda via, p: _resp(BLOCKED))
        with pytest.raises(GoogleUnavailable):
            _run(_client(direct=False), fake, "q")
        assert fake.vias == ["tor-a#0", "tor-b#0", "tor-a#1"]

    def test_http_status_and_network_errors_fail_over(self):
        def search(via, p):
            if via.startswith("tor-a"):
                return _resp("", status=429)
            if via.startswith("tor-b"):
                return requests.ConnectTimeout("socks")
            return _resp(OK)

        fake = FakeGoogle(search)
        assert _run(_client(), fake, "q")
        assert fake.vias == ["tor-a#0", "tor-b#0", "tor-a#1", "direto"]

    def test_sorry_page_fails_over(self):
        sorry = "<html><body>Our systems have detected unusual traffic</body></html>"
        fake = FakeGoogle(lambda via, p: _resp(sorry) if via.startswith("tor-a") else _resp(OK))
        _run(_client(), fake, "q")
        assert fake.vias == ["tor-a#0", "tor-b#0"]

    def test_rotation_across_queries(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(OK))
        for _ in range(3):
            _run(client, fake, "q")
        assert fake.vias == ["tor-a#0", "tor-b#0", "tor-a#0"]

    def test_stats(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(BLOCKED) if via.startswith("tor-a") else _resp(OK))
        _run(client, fake, "q")
        stats = client.stats()
        assert stats["consultas"] == 1
        assert stats["bloqueio:tor-a"] == 1
        assert stats["caminho:tor-b"] == 1
        assert stats["failover"] == 1


class TestToken:
    def test_cached_between_queries(self):
        client = _client()
        fake = FakeGoogle(lambda via, p: _resp(OK))
        _run(client, fake, "q1")
        _run(client, fake, "q2")
        assert len(fake.tokens) == 1

    def test_rejected_token_is_renewed_once_on_same_channel(self):
        answers = iter([_resp(BAD_TOKEN), _resp(OK)])
        fake = FakeGoogle(lambda via, p: next(answers))
        assert _run(_client(), fake, "q")
        assert fake.tokens == ["tor-a#0", "tor-a#0"]
        assert fake.vias == ["tor-a#0", "tor-a#0"]

    def test_token_rejected_twice_fails_over(self):
        fake = FakeGoogle(lambda via, p: _resp(BAD_TOKEN) if via.startswith("tor-a") else _resp(OK))
        assert _run(_client(), fake, "q")
        assert fake.vias == ["tor-a#0", "tor-a#0", "tor-b#0"]

    def test_blocked_token_fetch_counts_as_attempt(self):
        fake = FakeGoogle(
            lambda via, p: _resp(OK),
            token=lambda via: _resp("<html>sorry</html>") if via.startswith("tor-a") else _resp(CSE_JS),
        )
        assert _run(_client(), fake, "q")
        assert fake.tokens == ["tor-a#0", "tor-b#0"]
        assert fake.vias == ["tor-b#0"]
