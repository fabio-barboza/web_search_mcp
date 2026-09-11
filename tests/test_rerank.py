from unittest.mock import patch

import pytest

from web_search_mcp import config
from web_search_mcp.tools import research


@pytest.fixture(autouse=True)
def _offline_context_tokens(monkeypatch):
    monkeypatch.setattr(research, "context_tokens", lambda: config.MODEL_CONTEXT_TOKENS)


def _cands(n):
    return [{"url": f"http://s{i}.com/p", "title": f"T{i}", "content": f"trecho {i}"} for i in range(1, n + 1)]


class TestRerank:
    def test_picks_in_llm_order_dedup_and_range(self):
        cands = _cands(5)
        with patch.object(research, "chat", return_value="3\n1\n3\n99\n0\n5"):
            picks = research._rerank("p", cands, k=10)
        assert [r["url"] for r in picks] == ["http://s3.com/p", "http://s1.com/p", "http://s5.com/p"]

    def test_caps_at_k(self):
        with patch.object(research, "chat", return_value="1\n2\n3\n4"):
            picks = research._rerank("p", _cands(5), k=2)
        assert len(picks) == 2

    def test_prompt_carries_title_site_and_snippet(self):
        with patch.object(research, "chat", return_value="1") as chat:
            research._rerank("minha pergunta", _cands(2), k=5)
        user = chat.call_args.kwargs["user"]
        assert "minha pergunta" in user
        assert "[2] T2 — s2.com — trecho 2" in user

    def test_llm_failure_returns_none(self):
        with patch.object(research, "chat", side_effect=RuntimeError("fora")):
            assert research._rerank("p", _cands(3), k=5) is None

    def test_answer_without_valid_number_returns_none(self):
        with patch.object(research, "chat", return_value="nenhum serve"):
            assert research._rerank("p", _cands(3), k=5) is None

    def test_single_candidate_skips_llm(self):
        with patch.object(research, "chat") as chat:
            assert research._rerank("p", _cands(1), k=5) is None
        chat.assert_not_called()


class TestSelectAndRead:
    def test_reads_only_picks(self):
        cands = _cands(4)
        picks = [cands[2], cands[0]]
        read = [(cands[2], cands[2]["url"], "texto")]
        with patch.object(research, "_rerank", return_value=picks), \
             patch.object(research, "_read_pages", return_value=read) as rp:
            pages, unread = research._select_and_read("p", cands, False)
        assert rp.call_args.args[0] == picks
        assert pages == read
        # O escolhido que não abriu vira trecho citável.
        assert unread == [cands[0]]

    def test_rerank_failure_reads_merge_order(self):
        cands = _cands(3)
        with patch.object(research, "_rerank", return_value=None), \
             patch.object(research, "_read_pages", return_value=[]) as rp:
            pages, unread = research._select_and_read("p", cands, False)
        assert rp.call_args.args[0] == cands
        assert unread == []

    def test_no_pick_opens_falls_back_to_rest(self):
        cands = _cands(4)
        picks = [cands[1]]
        fallback = [(cands[0], cands[0]["url"], "texto")]
        with patch.object(research, "_rerank", return_value=picks), \
             patch.object(research, "_read_pages", side_effect=[[], fallback]) as rp:
            pages, _ = research._select_and_read("p", cands, False)
        assert rp.call_args_list[1].args[0] == [cands[0], cands[2], cands[3]]
        assert pages == fallback


class TestMergeDepth:
    def test_uses_every_position_the_search_returned(self):
        """Posições além de max_results (limite antigo, do SearXNG) entram."""
        per_query = [[{"url": f"https://s{i}.com/p"} for i in range(20)]]
        with patch.object(research._search, "max_results", 10), \
             patch.object(config, "RESEARCH_POOL_SIZE", 60), \
             patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 0):
            merged = research._merge_results(per_query)
        assert len(merged) == 20

    def test_pool_still_caps(self):
        per_query = [[{"url": f"https://a{i}.com"} for i in range(20)],
                     [{"url": f"https://b{i}.com"} for i in range(20)]]
        with patch.object(config, "RESEARCH_POOL_SIZE", 25):
            assert len(research._merge_results(per_query)) == 25


class TestDomainCapAfterTriage:
    def test_collect_links_does_not_cap_domains(self):
        """O merge entrega todas as páginas do mesmo site; quem corta é a
        triagem, que sabe qual delas responde."""
        with patch.object(research, "_generate_queries", return_value=["p"]), \
             patch.object(research, "_search_one", return_value=[
                 {"url": f"https://wiki.com/{i}"} for i in range(5)]), \
             patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 2):
            urls = [r["url"] for r in research._collect_links("p", False)]
        assert len(urls) == 5

    def test_cap_applies_in_triage_order(self):
        home = {"url": "https://wiki.com/", "title": "home"}
        char = {"url": "https://wiki.com/personagem", "title": "personagem"}
        quest = {"url": "https://wiki.com/quest", "title": "quest"}
        other = {"url": "https://outro.com/x", "title": "outro"}
        with patch.object(research, "_rerank", return_value=[quest, other, char, home]), \
             patch.object(research, "_read_pages", return_value=[]) as rp, \
             patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 2):
            research._select_and_read("p", [home, char, quest, other], False)
        # A homepage, primeira no merge, sai; a quest, primeira na triagem, fica.
        assert rp.call_args_list[0].args[0] == [quest, other, char]


class TestSnippetSources:
    def test_snippet_entries_are_marked_numbered_and_listed(self):
        page = ({"title": "Lida", "content": "c"}, "http://lida.com", "conteúdo real")
        unread = {"url": "http://video.com/v", "title": "Vídeo", "content": "trecho que liga os nomes"}
        with patch.object(research, "_collect_links", return_value=[page[0], unread]), \
             patch.object(research, "_select_and_read", return_value=([page], [unread])), \
             patch.object(research, "_summarize", return_value="fato [2]") as summ:
            out = research.research_web("pergunta sobre trecho", user_message="pergunta sobre trecho")
        dossier = summ.call_args.args[1]
        assert "FONTE [2]" in dossier and "resultado de busca não lido" in dossier
        assert "trecho que liga os nomes" in dossier
        assert "[video.com](http://video.com/v)" in out
        assert "[2] http://video.com/v (só título e trecho da busca)" in out
