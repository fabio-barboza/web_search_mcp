import re
from unittest.mock import patch

import pytest

from web_search_mcp import config
from web_search_mcp.tools import research


@pytest.fixture(autouse=True)
def _offline_context_tokens(monkeypatch):
    """context_tokens() consulta /models na rede; aqui ele espelha o config,
    então os testes que ajustam MODEL_CONTEXT_TOKENS seguem valendo."""
    monkeypatch.setattr(research, "context_tokens", lambda: config.MODEL_CONTEXT_TOKENS)


@pytest.fixture(autouse=True)
def _clean_repeat_cache():
    """O cache anti-loop é global ao módulo; sem limpar, um teste devolve o
    resultado cacheado de outro."""
    research._recent_calls.clear()
    yield
    research._recent_calls.clear()


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

        def fake_read_many(urls, reject_index=False):
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
            return [(t, None) for t in out]

        with patch.object(research._scraper, "read_many_dated", side_effect=fake_read_many), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4):
            pages = research._read_pages(candidates)

        assert len(pages) == 3
        assert all(not research.WebScraper.failed(p) for _, _, p in pages)

    def test_respects_max_waves(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(20)]

        with patch.object(research._scraper, "read_many_dated", return_value=None) as m, \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 5), \
             patch.object(config, "RESEARCH_MAX_WAVES", 2):
            m.side_effect = lambda urls, reject_index=False: [("(sem conteúdo extraível)", None) for _ in urls]
            pages = research._read_pages(candidates)

        assert m.call_count == 2
        assert pages == []

    def test_stops_at_context_budget(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(10)]

        # 12288 tokens úteis * _CHARS_PER_TOKEN = orçamento em caracteres.
        # Páginas de 12000 + cabeçalho (~79): duas cabem, a terceira não.
        with patch.object(research._scraper, "read_many_dated", side_effect=lambda urls, reject_index=False: [("c" * 12000, None) for _ in urls]), \
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
        with patch.object(research._scraper, "read_many_dated", side_effect=lambda urls, reject_index=False: [("c" * 500000, None) for _ in urls]), \
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
        assert result.startswith("Nenhum resultado encontrado.")

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


class TestLexicalDemotion:
    """Candidato sem token de conteúdo em comum com a pergunta vai ao fim.

    Corpus de temas não relacionados e os dois desfechos: o que precisa
    afundar E o que precisa continuar em cima.
    """

    @pytest.mark.parametrize("query,bom,ruim", [
        (
            "Como enfrentar o ultimo chefão em The Witcher 3?",
            {"url": "https://www.thewitcher.com/br/pt-br", "title": "The Witcher"},
            {"url": "https://en.wikipedia.org/wiki/Como_1907", "title": "Como 1907"},
        ),
        (
            "Qual a melhor estratégia contra Ketheric Thorm?",
            {"url": "https://gamerant.com/bg3-ketheric-thorm-guide/", "title": "Ketheric"},
            {"url": "https://www.dicio.com.br/qual/", "title": "Qual - Dicio"},
        ),
        (
            "Quando sai o próximo lançamento do telescópio James Webb?",
            {"url": "https://nasa.gov/webb/launch", "title": "James Webb telescope"},
            {"url": "https://www.onthisday.com/today/birthdays.php", "title": "Birthdays"},
        ),
    ])
    def test_unrelated_candidate_sinks(self, query, bom, ruim):
        # O ruim entra com score maior de propósito: a demoção tem que
        # vencer o score, senão o lixo continua ocupando vaga.
        ruim = {**ruim, "score": 9.0}
        bom = {**bom, "score": 0.1}
        urls = [r["url"] for r in research._merge_results([[ruim, bom]], query)]
        assert urls.index(bom["url"]) < urls.index(ruim["url"])
        # Demoção, não descarte: o candidato segue disponível como reserva.
        assert ruim["url"] in urls

    def test_junk_only_query_does_not_take_a_front_slot(self):
        """Round-robin não pode dar a vaga do 1º lugar a uma busca que só
        devolveu lixo: o 2º resultado bom de outra busca vem antes."""
        boa = [
            {"url": "https://bg3.wiki/wiki/Ketheric_Thorm/Combat", "title": "Ketheric"},
            {"url": "https://gamespot.com/baldurs-gate-3-ketheric-guide/", "title": "Ketheric guide"},
        ]
        lixo = [{"url": "https://customerservice.costco.com/return-policy", "title": "Return policy"}]
        urls = [r["url"] for r in research._merge_results(
            [boa, lixo], "Qual a melhor estratégia contra Ketheric Thorm em Baldur's Gate 3?")]
        assert urls[:2] == [boa[0]["url"], boa[1]["url"]]
        assert urls[2] == lixo[0]["url"]

    def test_no_query_keeps_previous_order(self):
        a = {"url": "https://a.com/x", "title": "", "score": 1.0}
        b = {"url": "https://b.com/y", "title": "", "score": 9.0}
        urls = [r["url"] for r in research._merge_results([[a, b]])]
        assert urls[0] == b["url"]


class TestPageDate:
    def test_dossier_carries_publication_date(self):
        pages = [({"title": "t", "content": "", "_date": "2026-08-22"},
                  "https://x.com/a", "conteúdo")]
        assert "Publicado em: 2026-08-22" in research._render_dossier(pages)

    def test_dossier_says_when_date_unknown(self):
        pages = [({"title": "t", "content": ""}, "https://x.com/a", "conteúdo")]
        assert "Publicado em: data não informada" in research._render_dossier(pages)


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


class TestRepeatGuard:
    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        research._recent_calls.clear()
        yield
        research._recent_calls.clear()

    def test_exact_repeat_returns_cached_with_note(self):
        research._remember_result("qual a build de força?", "RESULTADO ANTERIOR")
        with patch.object(research, "_collect_links") as collect:
            out = research.research_web("Qual a build de força?")
        collect.assert_not_called()
        assert out.startswith(research._REPEAT_NOTE)
        assert "RESULTADO ANTERIOR" in out

    def test_reformulation_hits_cache(self):
        """Reordenar, acentuar ou trocar palavra de função é a MESMA pergunta."""
        research._remember_result("qual a capital da Australia?", "CACHEADO")
        assert research._cached_result("qual é a capital da Austrália?") == "CACHEADO"

    def test_word_order_ignored(self):
        research._remember_result("melhores itens Gloomstalker ato 3", "CACHEADO")
        assert research._cached_result("ato 3 itens melhores Gloomstalker") == "CACHEADO"

    def test_one_content_word_changed_is_another_question(self):
        """Regressão: 'Brasil' x 'mundo' dá 0.84 de SequenceMatcher e vinha
        sendo servido do cache — o segundo lado do panorama era descartado."""
        research._remember_result("principais notícias do Brasil hoje", "CACHEADO")
        assert research._cached_result("principais notícias do mundo hoje") is None

    def test_function_word_changed_is_same_question(self):
        research._remember_result("principais notícias do Brasil hoje", "CACHEADO")
        assert research._cached_result("principais notícias no Brasil hoje") == "CACHEADO"

    def test_sibling_questions_not_confused(self):
        research._remember_result("melhores armas no ato 1", "CACHEADO")
        assert research._cached_result("melhores armaduras no ato 1") is None
        assert research._cached_result("melhores armas no ato 3") is None

    def test_only_stopwords_never_matches(self):
        research._remember_result("o que é isso", "CACHEADO")
        assert research._cached_result("e o que foi") is None

    def test_different_query_misses(self):
        research._remember_result("cotação do dólar hoje", "X")
        assert research._cached_result("previsão do tempo em Curitiba") is None

    def test_expired_entry_ignored(self):
        research._remember_result("pergunta", "VELHO")
        key = research._repeat_key("pergunta")
        ts, res = research._recent_calls[key]
        research._recent_calls[key] = (ts - research._REPEAT_TTL_SECONDS - 1, res)
        assert research._cached_result("pergunta") is None

    def test_different_question_same_shape_misses(self):
        research._remember_result(
            "melhores itens para Gloomstalker Ranger em Baldur's Gate 3 Ato 3",
            "X",
        )
        assert research._cached_result(
            "Baldur's Gate 3 usar muitos scrolls de Minor Globe de "
            "Invulnerability e Disintegrate no modo Honra"
        ) is None

    def test_empty_result_also_cached(self):
        with patch.object(research, "_collect_links", return_value=[]):
            first = research.research_web("busca sem resultado nenhum xyz")
        assert "Nenhum resultado" in first
        with patch.object(research, "_collect_links") as collect:
            second = research.research_web("busca sem resultado nenhum xyz")
        collect.assert_not_called()
        assert second.startswith(research._REPEAT_NOTE)

    def test_cache_capped(self):
        for i in range(research._REPEAT_MAX_ENTRIES + 10):
            research._remember_result(f"pergunta numero {i} bem diferente {i*i}", "r")
        assert len(research._recent_calls) <= research._REPEAT_MAX_ENTRIES


class TestSuspiciousUrl:
    def test_injected_markup_payload_rejected(self):
        # Caso real visto em produção: injeção de SEO num domínio legítimo.
        url = (
            "https://geohereditas.igc.usp.br/passeio-virtual-anavilhanas/"
            "?xml=data:gsf,%3Ckrpano%3E%3Cinclude%20url%3D%22//yapuza.xyz/q/1%22/%3E%3C/krpano%3E"
        )
        assert research._is_suspicious_url(url)

    def test_normal_urls_pass(self):
        for url in (
            "https://g1.globo.com/economia/noticia/2026/08/24/braskem.ghtml",
            "https://bg3.wiki/wiki/Gontr_Mael",
            "https://www.reddit.com/r/BG3Builds/comments/1ikpnwg/gear/?tl=pt-br",
            "https://example.com/busca?q=data+science&page=2",
        ):
            assert not research._is_suspicious_url(url), url

    def test_filtered_out_of_merge(self):
        bad = "https://ok.com/x?xml=data:gsf,%3Cscript%3E"
        per_query = [[{"url": bad, "score": 9}, {"url": "https://ok.com/boa"}]]
        urls = [r["url"] for r in research._merge_results(per_query)]
        assert bad not in urls
        assert "https://ok.com/boa" in urls


class TestRejectIndex:
    def test_research_always_rejects_index_pages(self):
        """Toda leitura do research pede rejeição de índice: o pipeline quer
        a página que responde, nunca a vitrine de links para outras."""
        seen = {}

        def fake_read_many(urls, reject_index=False):
            seen["reject_index"] = reject_index
            return [("conteúdo " + "x" * 2000, None) for _ in urls]

        with patch.object(research._scraper, "read_many_dated", side_effect=fake_read_many), \
             patch("web_search_mcp.tools.research._collect_links",
                   return_value=[{"url": "https://bg3.wiki/wiki/Gontr_Mael"}]), \
             patch("web_search_mcp.tools.research._summarize", return_value="resumo"):
            research.research_web("melhores itens para Gloomstalker no ato 3")

        assert seen["reject_index"] is True


class TestHubPage:
    _MENU = "\n".join(f"Seção {i}" for i in range(60))
    _PROSA = "\n".join("frase longa de conteúdo real. " * 10 for _ in range(8))

    def test_section_index_is_hub(self):
        assert research._is_hub_page("https://www.cnnbrasil.com.br/tecnologia/", self._MENU)

    def test_front_page_is_hub(self):
        assert research._is_hub_page("https://www.cnnbrasil.com.br/", self._MENU)

    def test_deep_path_never_hub(self):
        # Página de item de wiki: tabela de atributos, sem prosa, mas é
        # exatamente o conteúdo que uma pergunta sobre equipamento precisa.
        assert not research._is_hub_page("https://bg3.wiki/wiki/Gontr_Mael", self._MENU)

    def test_shallow_path_with_prose_passes(self):
        # Lista de itens de wiki na raiz do domínio (fextralife.com/Rings).
        assert not research._is_hub_page("https://x.wiki.fextralife.com/Rings", self._PROSA)

    def test_short_page_on_shallow_path_passes(self):
        # Post curto publicado na raiz: poucas linhas, não é vitrine.
        assert not research._is_hub_page("https://blog.com/meu-post", "linha\nlinha\nlinha")

    def test_hub_discarded_by_read_pages(self):
        candidates = [{"url": "https://portal.com/tecnologia/"}]
        page = self._MENU + "\n" + "x" * 1000

        with patch.object(research._scraper, "read_many_dated",
                          side_effect=lambda urls, reject_index=False: [(page, None)]), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 1):
            assert research._read_pages(candidates) == []


class TestLabelCitations:
    """REGRA: marcador de referência só aparece com o link no texto."""

    _PAGES = [
        ({}, "https://g1.globo.com/economia/noticia/2026/braskem.ghtml", ""),
        ({}, "https://www.bbc.com/news/articles/c93v", ""),
    ]

    def test_citation_becomes_inline_link(self):
        out = research._label_citations("PT recorreu contra ônibus [1].", self._PAGES)
        assert out == (
            "PT recorreu contra ônibus "
            "[g1.globo.com](https://g1.globo.com/economia/noticia/2026/braskem.ghtml)."
        )

    def test_no_bare_numeric_marker_survives(self):
        out = research._label_citations("A [1]. B [2]. C [7].", self._PAGES)
        assert not re.search(r"\[\d+\]", out), out

    def test_every_marker_carries_a_url(self):
        out = research._label_citations("A [1]. B [2].", self._PAGES)
        assert out.count("](http") == 2

    def test_www_stripped_from_label(self):
        out = research._label_citations("Kiev [2].", self._PAGES)
        assert "[bbc.com](https://www.bbc.com/news/articles/c93v)" in out

    def test_invented_citation_removed(self):
        assert research._label_citations("Fato sem fonte [7].", self._PAGES) == "Fato sem fonte."

    def test_text_without_markers_untouched(self):
        texto = "Resumo sem nenhuma citação."
        assert research._label_citations(texto, self._PAGES) == texto

    def test_no_pages_strips_everything(self):
        assert research._label_citations("Alegação [1] solta [2].", []) == "Alegação solta."
