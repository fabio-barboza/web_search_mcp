from unittest.mock import patch

import pytest

from web_search_mcp import config
from web_search_mcp.tools import research


@pytest.fixture(autouse=True)
def _offline_context_tokens(monkeypatch):
    """context_tokens() consulta /models na rede; aqui ele espelha o config,
    então os testes que ajustam MODEL_CONTEXT_TOKENS seguem valendo."""
    monkeypatch.setattr(research, "context_tokens", lambda: config.MODEL_CONTEXT_TOKENS)


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

    def test_capped_at_five(self):
        with patch("web_search_mcp.tools.research.chat", return_value="a\nb\nc\nd\ne\nf"):
            queries = research._generate_queries("pergunta")
        assert len(queries) <= 5

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


class TestPanoramaSeeds:
    def test_news_panorama_gets_br_and_world_seeds(self):
        seeds = research._panorama_seeds(
            "Faça um resumo das principais noticias no Brasil e no mundo hoje?", True
        )
        urls = [s["url"] for s in seeds]
        assert "https://g1.globo.com/" in urls
        assert "https://www.bbc.com/news" in urls

    def test_br_only_panorama_skips_world_seeds(self):
        seeds = research._panorama_seeds("principais notícias do dia", True)
        urls = [s["url"] for s in seeds]
        assert urls and all("bbc" not in u and "apnews" not in u for u in urls)

    def test_topic_panorama_gets_no_seeds(self):
        assert research._panorama_seeds("principais notícias de tecnologia hoje", True) == []

    def test_non_recent_gets_no_seeds(self):
        assert research._panorama_seeds("história das notícias no Brasil", False) == []

    def test_non_news_question_gets_no_seeds(self):
        assert research._panorama_seeds("qual a cotação do dólar hoje?", True) == []


class TestNormalizeUrl:
    def test_strips_tracking_fragment_and_trailing_slash(self):
        url = "https://Site.com/artigo/?utm_source=x&utm_campaign=y&fbclid=abc&id=7#secao"
        assert research._normalize_url(url) == "https://site.com/artigo?id=7"

    def test_equivalent_urls_share_key(self):
        a = "https://g1.globo.com/noticia/?utm_source=twitter"
        b = "https://g1.globo.com/noticia/"
        assert research._normalize_url(a) == research._normalize_url(b)

    def test_invalid_url_returned_as_is(self):
        assert research._normalize_url("http://[invalido") == "http://[invalido"


class TestMergeDomainCap:
    def test_same_story_with_tracking_counts_as_agreement(self):
        per_query = [
            [{"url": "https://a.com/x?utm_source=s1", "score": 1}],
            [{"url": "https://a.com/x/", "score": 1}],
            [{"url": "https://b.com/y", "score": 9}],
        ]
        with patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 0):
            merged = research._merge_results(per_query)
        urls = [r["url"] for r in merged]
        # a.com/x achada por 2 buscas ganha de b.com/y (score alto, 1 busca)
        # e entra uma vez só.
        assert urls[0].startswith("https://a.com/x")
        assert len([u for u in urls if "a.com/x" in u]) == 1

    def test_per_domain_cap(self):
        per_query = [[
            {"url": f"https://mesmo.com/{i}", "score": 10 - i} for i in range(5)
        ] + [{"url": "https://outro.com/1", "score": 0.1}]]
        with patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 2):
            merged = research._merge_results(per_query)
        domains = [research.urlsplit(r["url"]).netloc for r in merged]
        assert domains.count("mesmo.com") == 2
        assert "outro.com" in domains

    def test_cap_zero_means_unlimited(self):
        per_query = [[{"url": f"https://mesmo.com/{i}"} for i in range(5)]]
        with patch.object(config, "RESEARCH_MAX_PER_DOMAIN", 0):
            merged = research._merge_results(per_query)
        assert len(merged) == 5


class TestSearchOneSafe:
    def test_variant_failure_returns_empty_not_raise(self):
        with patch.object(research, "_search_one", side_effect=RuntimeError("boom")):
            assert research._search_one_safe(("variante", False)) == []

    def test_variant_failure_does_not_kill_collect(self):
        def fake_search_one(args):
            query, _ = args
            if query == "pergunta":
                return [{"url": "http://ok.com"}]
            raise RuntimeError("variante quebrou")

        with patch("web_search_mcp.tools.research._generate_queries", return_value=["pergunta", "ruim"]), \
             patch.object(research, "_search_one", side_effect=fake_search_one):
            results = research._collect_links("pergunta", False)
        assert [r["url"] for r in results] == ["http://ok.com"]


class TestArticleLinks:
    HTML = """
    <html><body>
      <a href="/politica/noticia/2026/08/24/manchete-principal-do-dia.ghtml">Manchete principal</a>
      <a href="/esportes/">Esportes</a>
      <a href="https://outro-site.com/2026/08/24/materia-externa.html">Externa</a>
      <a href="/news/articles/c1234abcd">Article BBC-style</a>
      <a href="/coluna/um-slug-bem-comprido-com-muitas-palavras-aqui">Slug longo</a>
      <a href="/politica/noticia/2026/08/24/manchete-principal-do-dia.ghtml?utm_source=x">Duplicada</a>
      <a href="#topo">Âncora</a>
    </body></html>
    """

    def test_extracts_article_links_in_dom_order(self):
        links = research._article_links("https://www.site.com/", self.HTML, 10)
        urls = [u for u, _ in links]
        assert urls == [
            "https://www.site.com/politica/noticia/2026/08/24/manchete-principal-do-dia.ghtml",
            "https://www.site.com/news/articles/c1234abcd",
            "https://www.site.com/coluna/um-slug-bem-comprido-com-muitas-palavras-aqui",
        ]
        assert links[0][1] == "Manchete principal"

    def test_respects_limit(self):
        links = research._article_links("https://www.site.com/", self.HTML, 1)
        assert len(links) == 1

    def test_invalid_html_returns_empty(self):
        assert research._article_links("https://x.com/", "", 5) == []


class TestExpandSeeds:
    def test_interleaves_across_fronts_and_falls_back(self):
        def fake_download(url):
            if "a.com" in url:
                return True, (
                    '<a href="/2026/08/24/a1-materia-um">A1</a>'
                    '<a href="/2026/08/24/a2-materia-dois">A2</a>'
                )
            return False, "(falhou)"

        seeds = [
            {"url": "https://a.com/", "title": "", "content": ""},
            {"url": "https://b.com/", "title": "", "content": ""},
        ]
        with patch.object(research._scraper, "_download", side_effect=fake_download):
            out = research._expand_seeds(seeds)
        urls = [r["url"] for r in out]
        # híbrido: capas primeiro (agenda), depois as matérias intercaladas
        assert urls == [
            "https://a.com/",
            "https://b.com/",
            "https://a.com/2026/08/24/a1-materia-um",
            "https://a.com/2026/08/24/a2-materia-dois",
        ]
