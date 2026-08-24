from unittest.mock import patch

from web_search_mcp.tools import analyze


class TestAnalyzeUrls:
    def test_happy_path_two_urls(self):
        pages = ["conteúdo A " * 100, "conteúdo B " * 100]
        with patch.object(analyze._scraper, "read_many", return_value=pages), \
             patch.object(analyze, "chat", return_value="análise pronta") as chat:
            out = analyze.analyze_urls(
                ["http://a.com", "http://b.com"], "compare os dois"
            )
        assert "análise pronta" in out
        assert "http://a.com" in out and "http://b.com" in out
        user_msg = chat.call_args.kwargs["user"]
        assert "compare os dois" in user_msg
        assert "http://a.com" in user_msg

    def test_failed_page_reported_not_analyzed(self):
        pages = ["conteúdo bom " * 100, "(não foi possível ler a página: 403)"]
        with patch.object(analyze._scraper, "read_many", return_value=pages), \
             patch.object(analyze, "chat", return_value="ok"):
            out = analyze.analyze_urls(["http://ok.com", "http://ruim.com"])
        assert "URLs analisadas:\n- http://ok.com" in out
        assert "URLs que falharam:\n- http://ruim.com" in out

    def test_all_failed(self):
        pages = ["(não foi possível ler a página: timeout)"]
        with patch.object(analyze._scraper, "read_many", return_value=pages), \
             patch.object(analyze, "chat") as chat:
            out = analyze.analyze_urls(["http://morto.com"])
        assert "Nenhuma das URLs pôde ser lida" in out
        chat.assert_not_called()

    def test_empty_input(self):
        assert analyze.analyze_urls([]) == "Nenhuma URL fornecida."
        assert analyze.analyze_urls(["", "  "]) == "Nenhuma URL fornecida."

    def test_too_many_urls(self):
        urls = [f"http://x.com/{i}" for i in range(9)]
        out = analyze.analyze_urls(urls)
        assert "máximo por chamada" in out

    def test_pages_truncated_to_budget(self):
        big = "x" * 1_000_000
        with patch.object(analyze._scraper, "read_many", return_value=[big, big]), \
             patch.object(analyze, "_dossier_char_budget", return_value=10_000), \
             patch.object(analyze, "chat", return_value="ok") as chat:
            analyze.analyze_urls(["http://a.com", "http://b.com"])
        user_msg = chat.call_args.kwargs["user"]
        assert len(user_msg) < 12_000
