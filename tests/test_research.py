from unittest.mock import patch

from web_search_mcp import config
from web_search_mcp.tools import research


class TestGenerateQueries:
    def test_original_question_always_first(self):
        with patch("web_search_mcp.tools.research.chat", return_value="busca 1\nbusca 2"):
            queries = research._generate_queries("quem foi Santos Dumont")
        assert queries[0] == "quem foi Santos Dumont"

    def test_dedupe_case_insensitive(self):
        with patch("web_search_mcp.tools.research.chat", return_value="Santos Dumont\nsantos dumont\nbiografia"):
            queries = research._generate_queries("Santos Dumont")
        assert queries.count("Santos Dumont") == 1 or "santos dumont" not in [q.lower() for q in queries[1:]]
        lowered = [q.lower() for q in queries]
        assert len(lowered) == len(set(lowered))

    def test_capped_at_four(self):
        with patch("web_search_mcp.tools.research.chat", return_value="a\nb\nc\nd\ne"):
            queries = research._generate_queries("pergunta")
        assert len(queries) <= 4

    def test_llm_exception_falls_back_to_question_only(self):
        with patch("web_search_mcp.tools.research.chat", side_effect=RuntimeError("boom")):
            queries = research._generate_queries("pergunta qualquer")
        assert queries == ["pergunta qualquer"]


class TestSearchOne:
    def test_recent_with_few_results_completes_without_time_range(self):
        def fake_search(query, time_range=None):
            if time_range:
                return [{"url": "http://a.com"}]
            return [{"url": "http://a.com"}, {"url": "http://b.com"}]

        with patch.object(research._search, "search", side_effect=fake_search):
            results = research._search_one(("q", True))

        urls = {r["url"] for r in results}
        assert "http://a.com" in urls
        assert "http://b.com" in urls


class TestReadPages:
    def test_failed_page_does_not_spend_budget(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(6)]

        call_count = {"n": 0}

        def fake_read_many(urls):
            call_count["n"] += 1
            # primeira onda: primeiro link falha, resto tem sucesso
            out = []
            for i, u in enumerate(urls):
                if call_count["n"] == 1 and i == 0:
                    out.append("(sem conteúdo extraível)")
                else:
                    # Precisa passar de _MIN_USEFUL_CHARS, senão o próprio
                    # piso de tamanho descarta a página e o teste mede a
                    # coisa errada.
                    out.append(f"conteúdo de {u} " + "x" * 2000)
            return out

        with patch.object(research._scraper, "read_many", side_effect=fake_read_many), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4):
            pages = research._read_pages(candidates)

        assert len(pages) == 3
        assert all(not research.WebScraper.failed(p) for _, _, p in pages)

    def test_respects_max_waves(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(20)]

        with patch.object(research._scraper, "read_many", return_value=None) as m, \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 5), \
             patch.object(config, "RESEARCH_MAX_WAVES", 2):
            m.side_effect = lambda urls: ["(sem conteúdo extraível)" for _ in urls]
            pages = research._read_pages(candidates)

        assert m.call_count == 2
        assert pages == []

    def test_stops_at_context_budget(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(10)]

        # 12288 tokens úteis * _CHARS_PER_TOKEN = orçamento em caracteres.
        # Páginas de 12000 + cabeçalho (~79): duas cabem, a terceira não.
        with patch.object(research._scraper, "read_many", side_effect=lambda urls: ["c" * 12000 for _ in urls]), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 10), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4), \
             patch.object(config, "MODEL_CONTEXT_TOKENS", 16384), \
             patch.object(config, "MODEL_RESERVE_TOKENS", 4096):
            pages = research._read_pages(candidates)

        assert len(pages) == 2
        assert len(research._render_dossier(pages)) <= research._dossier_char_budget()

    def test_first_page_enters_even_if_over_budget(self):
        """Uma página sozinha maior que a janela ainda é melhor que nada: ela
        entra e o _render_dossier corta o excesso."""
        with patch.object(research._scraper, "read_many", side_effect=lambda urls: ["c" * 500000 for _ in urls]), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 1), \
             patch.object(config, "MODEL_CONTEXT_TOKENS", 16384), \
             patch.object(config, "MODEL_RESERVE_TOKENS", 4096):
            pages = research._read_pages([{"url": "http://x.com/gigante"}])
            assert len(pages) == 1
            assert len(research._render_dossier(pages)) == research._dossier_char_budget()


class TestRenderDossier:
    def test_truncates_to_budget(self):
        pages = [({"title": "t", "content": "c"}, "http://x.com", "p" * 100000)]
        with patch.object(config, "MODEL_CONTEXT_TOKENS", 8192), \
             patch.object(config, "MODEL_RESERVE_TOKENS", 4096):
            dossier = research._render_dossier(pages)
        assert len(dossier) == int((8192 - 4096) * research._CHARS_PER_TOKEN)

    def test_leaves_small_dossier_intact(self):
        pages = [({"title": "t", "content": "c"}, "http://x.com", "conteúdo curto")]
        dossier = research._render_dossier(pages)
        assert "conteúdo curto" in dossier
        assert len(dossier) < 200


class TestCollectLinks:
    def test_round_robin_dedupe_and_pool_cap(self):
        # _generate_queries sempre devolve a pergunta original em primeiro
        # lugar; _collect_links conta com isso para não buscá-la duas vezes.
        with patch("web_search_mcp.tools.research._generate_queries", return_value=["pergunta", "q2"]), \
             patch.object(config, "RESEARCH_POOL_SIZE", 3):

            def fake_search_one(args):
                query, _recent = args
                if query == "pergunta":
                    return [{"url": "http://a.com"}, {"url": "http://shared.com"}]
                return [{"url": "http://b.com"}, {"url": "http://shared.com"}]

            with patch("web_search_mcp.tools.research._search_one", side_effect=fake_search_one):
                results = research._collect_links("pergunta", False)

        urls = [r["url"] for r in results]
        # shared.com vem primeiro por concordância: é a única achada pelas
        # duas buscas. As exclusivas de cada ângulo vêm depois, uma de cada,
        # mantendo o round-robin.
        assert urls[0] == "http://shared.com"
        assert set(urls[1:3]) == {"http://a.com", "http://b.com"}
        assert len(urls) == len(set(urls))
        assert len(urls) <= 3


class TestResearchWeb:
    def test_no_results_message(self):
        with patch.object(research, "_collect_links", return_value=[]):
            result = research.research_web("pergunta sem resultado")
        assert result == "Nenhum resultado encontrado."

    def test_all_pages_failed_message(self):
        with patch.object(research, "_collect_links", return_value=[{"url": "http://a.com"}]), \
             patch.object(research, "_read_pages", return_value=[]):
            result = research.research_web("pergunta")
        assert result == "Nenhuma das páginas encontradas pôde ser lida."

    def test_timestamp_present_with_offset(self):
        pages_read = [({"title": "T", "content": "C"}, "http://a.com", "conteúdo da página")]
        with patch.object(research, "_collect_links", return_value=[{"url": "http://a.com"}]), \
             patch.object(research, "_read_pages", return_value=pages_read), \
             patch.object(research, "_summarize", return_value="resumo final"):
            result = research.research_web("pergunta")

        assert result.startswith("Pesquisa realizada em ")
        assert "UTC)." in result.splitlines()[0]
        assert "resumo final" in result
        assert "URLs consultadas:" in result
        assert "http://a.com" in result


class TestMergeResults:
    def test_agreement_beats_score(self):
        """URL achada por duas buscas passa na frente de score alto isolado."""
        per_query = [
            [{"url": "http://solo.com", "score": 9.0}, {"url": "http://ambas.com", "score": 0.1}],
            [{"url": "http://outra.com", "score": 5.0}, {"url": "http://ambas.com", "score": 0.1}],
        ]
        merged = research._merge_results(per_query)
        assert merged[0]["url"] == "http://ambas.com"

    def test_score_orders_within_same_agreement(self):
        per_query = [[
            {"url": "http://baixo.com", "score": 0.2},
            {"url": "http://alto.com", "score": 8.0},
        ]]
        merged = research._merge_results(per_query)
        assert [r["url"] for r in merged] == ["http://alto.com", "http://baixo.com"]

    def test_round_robin_still_spreads_across_queries(self):
        """Cada busca continua colocando seu melhor resultado antes de
        qualquer segundo colocado."""
        per_query = [
            [{"url": "http://a1.com", "score": 9.0}, {"url": "http://a2.com", "score": 8.0}],
            [{"url": "http://b1.com", "score": 1.0}, {"url": "http://b2.com", "score": 0.5}],
        ]
        urls = [r["url"] for r in research._merge_results(per_query)]
        assert urls[:2] == ["http://a1.com", "http://b1.com"]

    def test_missing_or_invalid_score_does_not_crash(self):
        per_query = [[
            {"url": "http://sem.com"},
            {"url": "http://lixo.com", "score": "abc"},
            {"url": "http://ok.com", "score": 3.0},
        ]]
        urls = [r["url"] for r in research._merge_results(per_query)]
        assert urls[0] == "http://ok.com"
        assert len(urls) == 3
