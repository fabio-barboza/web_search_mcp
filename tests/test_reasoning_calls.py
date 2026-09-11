from unittest.mock import MagicMock, patch

from web_search_mcp import config, llm
from web_search_mcp.tools import analyze, research

OFF = {"chat_template_kwargs": {"enable_thinking": False}}
LOW = {"chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "low"}}


def _post():
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    resp.raise_for_status.return_value = None
    return patch("web_search_mcp.llm.requests.post", return_value=resp)


def _sent(post):
    return post.call_args.kwargs["json"]["chat_template_kwargs"]


class TestReasoningBody:
    def test_applied_over_extra_body_only_when_asked(self):
        with patch.object(config, "USE_REASONING", True), \
             patch.object(config, "EXTRA_BODY", OFF), patch.object(config, "REASONING_BODY", LOW), \
             patch.object(llm, "_resolve_model", return_value="m"), _post() as post:
            llm.chat(system="s", user="u", reasoning=True)
            asked = _sent(post)
            llm.chat(system="s", user="u")
            plain = _sent(post)
        assert asked == LOW["chat_template_kwargs"]
        assert plain == OFF["chat_template_kwargs"]

    def test_use_reasoning_false_keeps_extra_body(self):
        with patch.object(config, "USE_REASONING", False), \
             patch.object(config, "EXTRA_BODY", OFF), patch.object(config, "REASONING_BODY", LOW), \
             patch.object(llm, "_resolve_model", return_value="m"), _post() as post:
            llm.chat(system="s", user="u", reasoning=True)
        assert _sent(post) == OFF["chat_template_kwargs"]

    def test_empty_body_keeps_extra_body(self):
        with patch.object(config, "USE_REASONING", True), \
             patch.object(config, "EXTRA_BODY", OFF), patch.object(config, "REASONING_BODY", {}), \
             patch.object(llm, "_resolve_model", return_value="m"), _post() as post:
            llm.chat(system="s", user="u", reasoning=True)
        assert _sent(post) == OFF["chat_template_kwargs"]


class TestWhoAsksForReasoning:
    """Triagem, resumo e análise decidem a qualidade; as chamadas curtas não pedem."""

    def test_rerank_asks(self):
        cands = [{"url": "https://a.com/x", "title": "a"}, {"url": "https://b.com/y", "title": "b"}]
        with patch.object(research, "chat", return_value="1") as chat:
            research._rerank("pergunta", cands, 1)
        assert chat.call_args.kwargs["reasoning"] is True

    def test_summarize_asks(self):
        with patch.object(research, "chat", return_value="resumo") as chat:
            research._summarize("pergunta", "dossiê", False)
        assert chat.call_args.kwargs["reasoning"] is True

    def test_analyze_asks(self):
        pages = [("conteúdo " * 100, "http://a.com")]
        with patch.object(analyze._scraper, "read_many_located", return_value=pages), \
             patch.object(analyze, "chat", return_value="ok") as chat:
            analyze.analyze_urls(["http://a.com"])
        assert chat.call_args.kwargs["reasoning"] is True

    def test_bridge_picker_does_not_ask(self):
        holders = [{"title": "Pai Putrefato é Mystic Carrion", "content": "Trumbo"}]
        with patch.object(research, "chat", return_value="1") as chat:
            research._pick_bridge_terms(["Pai Putrefato"], holders, ["Trumbo", "Comments"])
        assert not chat.call_args.kwargs.get("reasoning")
