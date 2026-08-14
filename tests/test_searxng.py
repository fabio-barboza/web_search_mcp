from unittest.mock import MagicMock, patch

from web_search_mcp.util.searxng import SearXNG


def _mock_response(results):
    resp = MagicMock()
    resp.json.return_value = {"results": results}
    resp.raise_for_status.return_value = None
    return resp


class TestSearch:
    def test_params_built_correctly(self):
        client = SearXNG(base_url="http://searx.local", max_results=5, timeout=3, language="pt-BR", categories="general,news")
        with patch("web_search_mcp.util.searxng.requests.get", return_value=_mock_response([])) as get:
            client.search("clima em sao paulo")

        args, kwargs = get.call_args
        assert args[0] == "http://searx.local/search"
        params = kwargs["params"]
        assert params["q"] == "clima em sao paulo"
        assert params["format"] == "json"
        assert params["language"] == "pt-BR"
        assert params["categories"] == "general,news"
        assert "time_range" not in params

    def test_time_range_only_when_requested(self):
        client = SearXNG()
        with patch("web_search_mcp.util.searxng.requests.get", return_value=_mock_response([])) as get:
            client.search("cotacao dolar", time_range="day")

        params = get.call_args.kwargs["params"]
        assert params["time_range"] == "day"

    def test_truncates_to_max_results(self):
        client = SearXNG(max_results=2)
        results = [{"url": f"http://x.com/{i}"} for i in range(10)]
        with patch("web_search_mcp.util.searxng.requests.get", return_value=_mock_response(results)):
            out = client.search("qualquer coisa")

        assert len(out) == 2
