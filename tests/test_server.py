import asyncio

from fastmcp import Client

from web_search_mcp import server


def _run(coro):
    return asyncio.run(coro)


class TestTools:
    def test_registered_tools(self):
        async def go():
            async with Client(server.mcp) as client:
                return await client.list_tools()

        tools = _run(go())
        assert {t.name for t in tools} == {"read_url", "research_web", "analyze_urls"}


class TestPrompt:
    def test_pesquisador_prompt_registered(self):
        async def go():
            async with Client(server.mcp) as client:
                return await client.list_prompts()

        prompts = _run(go())
        assert {p.name for p in prompts} == {"pesquisador"}

    def test_pesquisador_prompt_has_fresh_date(self):
        assert "Data e hora atual:" in server.pesquisador()
