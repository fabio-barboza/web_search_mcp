import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from web_search_mcp import server
from web_search_mcp.tools import research


def _ctx(tool="research_web", session="s1"):
    return SimpleNamespace(
        message=SimpleNamespace(name=tool),
        fastmcp_context=SimpleNamespace(session_id=session),
    )


class _Tool:
    """call_next falso: conta execuções e devolve texto + structured."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, context):
        self.calls += 1
        return ToolResult(
            content=[TextContent(type="text", text="resultado")],
            structured_content={"result": "resultado"},
        )


def _run(guard, tool, ctx, at):
    with patch.object(server.time, "monotonic", return_value=at):
        return asyncio.run(guard.on_call_tool(ctx, tool))


class TestChainGuard:
    def test_first_calls_pass_untouched(self):
        guard, tool = server._ChainGuard(), _Tool()
        for t in (0, 5):
            out = _run(guard, tool, _ctx(), t)
            assert out.content[0].text == "resultado"
        assert tool.calls == 2

    def test_soft_note_from_third_chained_call(self):
        guard, tool = server._ChainGuard(), _Tool()
        for t in (0, 5):
            _run(guard, tool, _ctx(), t)
        out = _run(guard, tool, _ctx(), 10)
        assert out.content[0].text.startswith("AVISO: esta é a 3ª pesquisa")
        assert out.content[1].text == "resultado"
        assert out.structured_content["result"].startswith("AVISO:")
        assert tool.calls == 3

    def test_hard_stop_does_not_run_tool(self):
        guard, tool = server._ChainGuard(), _Tool()
        for t in (0, 5, 10, 15):
            _run(guard, tool, _ctx(), t)
        out = _run(guard, tool, _ctx(), 20)
        assert out.content[0].text.startswith("PESQUISA NÃO EXECUTADA")
        assert tool.calls == 4

    def test_gap_resets_chain(self):
        """Pergunta nova do usuário chega bem depois da última resposta."""
        guard, tool = server._ChainGuard(), _Tool()
        for t in (0, 5, 10, 15):
            _run(guard, tool, _ctx(), t)
        out = _run(guard, tool, _ctx(), 15 + server._CHAIN_GAP_SECONDS + 1)
        assert out.content[0].text == "resultado"

    def test_sessions_are_independent(self):
        guard, tool = server._ChainGuard(), _Tool()
        for t in (0, 5, 10, 15):
            _run(guard, tool, _ctx(session="a"), t)
        out = _run(guard, tool, _ctx(session="b"), 16)
        assert out.content[0].text == "resultado"

    def test_read_url_is_not_counted(self):
        guard, tool = server._ChainGuard(), _Tool()
        for t in range(10):
            out = _run(guard, tool, _ctx(tool="read_url"), t)
        assert out.content[0].text == "resultado"
        assert tool.calls == 10

    def test_tool_sees_earlier_questions_of_the_same_turn_only(self):
        seen = []

        async def tool(context):
            seen.append(research.chain_questions.get())
            return ToolResult(content=[TextContent(type="text", text="r")])

        def ctx(q, session="s1"):
            c = _ctx(session=session)
            c.message.arguments = {"query": q}
            return c

        guard = server._ChainGuard()
        _run(guard, tool, ctx("pergunta A"), 0)
        _run(guard, tool, ctx("pergunta B"), 5)
        _run(guard, tool, ctx("outra sessão", session="s2"), 6)
        _run(guard, tool, ctx("turno novo"), 5 + server._CHAIN_GAP_SECONDS + 1)
        assert seen == [(), ("pergunta A",), (), ()]
        assert research.chain_questions.get() == ()

    def test_registered_on_server(self):
        assert any(isinstance(m, server._ChainGuard) for m in server.mcp.middleware)
