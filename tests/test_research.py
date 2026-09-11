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

    def test_keyword_form_always_second_even_without_llm(self):
        """Não depende de o LLM manter o nome: vem do código."""
        with patch("web_search_mcp.tools.research.chat", side_effect=RuntimeError("boom")):
            queries = research._generate_queries("Como inicia a chain do Pai Putrefato no ato 3 de BG3?")
        assert queries == [
            "Como inicia a chain do Pai Putrefato no ato 3 de BG3?",
            "inicia chain Pai Putrefato ato 3 BG3",
        ]


class TestNamePhrases:
    @pytest.mark.parametrize("question,phrases", [
        ("Como inicia a chain do Pai Putrefato no ato 3 de BG3?", ["Pai Putrefato", "BG3"]),
        ("O que diz a Lei do Bem sobre incentivo à inovação?", ["Lei do Bem"]),
        ("Quem foi a Tia Ciata?", ["Tia Ciata"]),
        ("Qual a diferença entre o Pix Automático e o Pix Agendado?", ["Pix Automático", "Pix Agendado"]),
        ("Como funciona o fluxo de autorização OAuth2 com PKCE?", ["OAuth2", "PKCE"]),
        ("Qual a cotação atual do dólar em reais?", []),
        ("O que é a doença cobreiro e como trata?", []),
    ])
    def test_phrases_come_from_token_shape(self, question, phrases):
        assert research._name_phrases(question) == phrases

    def test_mentions_matches_whole_phrase_folded(self):
        assert research._mentions("Pix Automático", research._fold("O PIX  automatico chegou"))
        assert not research._mentions("Pai Putrefato", research._fold("o pai dele, putrefato"))
        assert not research._mentions("BG3", research._fold("BG30 é outro"))


def _pool(*titles):
    return [{"title": t, "url": f"https://site{i}.example/p", "content": ""} for i, t in enumerate(titles)]


class TestBridgeTerms:
    """Os dois lados, em assuntos sem relação entre si (jogo, música)."""

    def test_picks_corroborated_rare_neighbour_of_the_missing_name(self):
        filler = [f"Baldur's Gate 3 guide part {i}" for i in range(20)]
        pool = _pool(
            "Baldur's Gate 3 : Pista de Trumbo, Servo do Pai Putrefato - YouTube",
            "BALDUR'S GATE 3 - WHY YOU SHOULD SAVE TRUMBO!!!",
            *filler,
        )
        terms = research._bridge_terms(["Pai Putrefato"], pool, "Como inicia a chain do Pai Putrefato?")
        # Baldur's/Gate: pool inteiro (não distingue). Pista/Servo: um candidato só.
        assert terms == ["Trumbo"]

    def test_other_subject_nickname_to_real_name(self):
        pool = _pool(
            "Luiz Gonzaga, o Rei do Baião - Wikipédia",
            "Luiz Gonzaga: biografia e discografia",
            *[f"História do forró, parte {i}" for i in range(15)],
        )
        terms = research._bridge_terms(["Rei do Baião"], pool, "Quem foi o Rei do Baião?")
        assert set(terms) == {"Luiz", "Gonzaga"}

    def test_site_name_in_title_is_not_a_bridge(self):
        pool = [
            {"title": "Pista de Trumbo, Servo do Pai Putrefato - YouTube", "url": "https://www.youtube.com/watch?v=1", "content": ""},
            {"title": "SAVE TRUMBO - YouTube", "url": "https://www.youtube.com/watch?v=2", "content": ""},
            *_pool(*[f"guia {i}" for i in range(20)]),
        ]
        assert research._bridge_terms(["Pai Putrefato"], pool, "Pai Putrefato?") == ["Trumbo"]

    def test_shouted_title_words_are_not_names_and_rarest_wins(self):
        """Medido: em título todo em maiúsculas, "SAVE"/"HELPING" ganhavam de
        "Trumbo" por serem mais frequentes no pool."""
        pool = _pool(
            "Pista de Trumbo, Servo do Pai Putrefato",
            "WHY YOU SHOULD SAVE TRUMBO!!! HELPING THE ROTTEN FATHER - Pai Putrefato",
            "Save the Gith Egg", "Save Karlach", "Helping hand guide", "Helping Dammon",
            "Seguidores de Trumbo",
            *[f"guia {i}" for i in range(40)],
        )
        assert research._bridge_terms(["Pai Putrefato"], pool, "Pai Putrefato?") == ["Trumbo"]

    def test_nothing_when_no_candidate_carries_the_name(self):
        pool = _pool("Luiz Gonzaga: biografia", "Luiz Gonzaga discografia")
        assert research._bridge_terms(["Rei do Baião"], pool, "Quem foi o Rei do Baião?") == []

    def test_neighbour_seen_only_once_is_not_corroborated(self):
        pool = _pool("Pista de Trumbo, Servo do Pai Putrefato", *[f"outro {i}" for i in range(10)])
        assert research._bridge_terms(["Pai Putrefato"], pool, "Pai Putrefato?") == []


class TestBridge:
    QUESTION = "Como inicia a chain do Pai Putrefato no ato 3 de BG3?"
    POOL = _pool(
        "Baldur's Gate 3 : Pista de Trumbo, Servo do Pai Putrefato - YouTube",
        "BALDUR'S GATE 3 - WHY YOU SHOULD SAVE TRUMBO!!!",
        *[f"Baldur's Gate 3 guide part {i}" for i in range(20)],
    )

    def test_missing_name_triggers_short_search_with_confirmed_names(self):
        read = [({"title": "Act 3"}, "https://wiki.example/act3", "BG3 Act 3 walkthrough, Lower City quests")]
        found = {"title": "Thrumbo - BG3 Wiki", "url": "https://bg3.example/Thrumbo", "content": ""}
        with patch.object(research, "_search_one_safe", return_value=[found]) as search, \
             patch.object(research, "_rerank", return_value=None), \
             patch.object(research, "_read_pages", return_value=[(found, found["url"], "texto")]) as reader:
            extra = research._bridge(self.QUESTION, False, self.POOL, read)
        search.assert_called_once_with(("Trumbo BG3", False))
        assert extra == [(found, found["url"], "texto")]
        assert reader.call_args.kwargs["page_budget"] == research._BRIDGE_PAGES
        assert reader.call_args.kwargs["char_budget"] > 0

    def test_bridge_pages_lead_the_dossier(self):
        page = ({"title": "a"}, "https://a.example", "texto")
        extra = ({"title": "b"}, "https://b.example", "ponte")
        with patch.object(research, "_rerank", return_value=[page[0]]), \
             patch.object(research, "_read_pages", return_value=[page]), \
             patch.object(research, "_bridge", return_value=[extra]):
            pages, _ = research._select_and_read(self.QUESTION, [{"url": "https://a.example"}], False)
        assert pages == [extra, page]

    def test_no_search_when_every_name_was_read(self):
        read = [({"title": "x"}, "https://x.example", "No BG3, o Pai Putrefato fica na mansão")]
        with patch.object(research, "_search_one_safe") as search:
            assert research._bridge(self.QUESTION, False, self.POOL, read) == []
        search.assert_not_called()

    def test_no_search_for_question_without_names(self):
        read = [({"title": "x"}, "https://x.example", "cotação")]
        with patch.object(research, "_search_one_safe") as search:
            assert research._bridge("qual a cotação do dólar hoje", True, self.POOL, read) == []
        search.assert_not_called()


class TestKeywordQuery:
    @pytest.mark.parametrize("question,expected", [
        ("Quem foi a Tia Ciata?", "Tia Ciata"),
        ("O que diz a Lei do Bem sobre incentivo à inovação?", "diz Lei Bem incentivo inovação"),
        ("Como usar o git worktree para trabalhar em duas branches ao mesmo tempo?",
         "usar git worktree trabalhar duas branches tempo"),
        ("How does OAuth2 PKCE work with a SPA?", "OAuth2 PKCE work SPA"),
        ("Qual a versão do C++ e do C# no .NET 8?", "versão C++ C# .NET 8"),
    ])
    def test_drops_function_words_keeps_names(self, question, expected):
        assert research._keyword_query(question) == expected

    def test_empty_when_nothing_to_drop(self):
        assert research._keyword_query("Santos Dumont") == ""
        assert research._keyword_query("dólar?") == ""


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

        def fake_read_many(urls, mark_index=False):
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
            return [(t, None, False) for t in out]

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
            m.side_effect = lambda urls, mark_index=False: [("(sem conteúdo extraível)", None, False) for _ in urls]
            pages = research._read_pages(candidates)

        assert m.call_count == 2
        assert pages == []

    def test_stops_at_context_budget(self):
        candidates = [{"url": f"http://x.com/{i}"} for i in range(10)]

        # 12288 tokens úteis * _CHARS_PER_TOKEN = orçamento em caracteres.
        # Páginas de 12000 + cabeçalho (~79): duas cabem, a terceira não.
        with patch.object(research._scraper, "read_many_dated", side_effect=lambda urls, mark_index=False: [("c" * 12000, None, False) for _ in urls]), \
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
        with patch.object(research._scraper, "read_many_dated", side_effect=lambda urls, mark_index=False: [("c" * 500000, None, False) for _ in urls]), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 1), \
             patch.object(config, "MODEL_CONTEXT_TOKENS", 16384), \
             patch.object(config, "MODEL_RESERVE_TOKENS", 4096):
            pages = research._read_pages([{"url": "http://x.com/gigante"}])
            assert len(pages) == 1
            assert len(research._render_dossier(pages)) == research._dossier_char_budget()

    def test_explicit_char_budget_applies_from_first_page(self):
        """A leitura da ponte complementa um dossiê que já existe: página que
        não cabe no que sobrou fica de fora, mesmo sendo a primeira."""
        with patch.object(research._scraper, "read_many_dated", side_effect=lambda urls, mark_index=False: [("c" * 5000, None, False) for _ in urls]), \
             patch.object(config, "RESEARCH_MAX_WAVES", 1):
            assert research._read_pages([{"url": "http://x.com/a"}], page_budget=1, char_budget=1000) == []
            assert len(research._read_pages([{"url": "http://x.com/a"}], page_budget=1, char_budget=10000)) == 1


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


class TestIndexReserve:
    """Capa/índice/hub não some: vai para a reserva e só ocupa vaga que a
    matéria deixou vazia. Os dois desfechos: com matéria suficiente a capa
    não entra; sem ela, a capa preenche."""

    _ARTIGO = "texto corrido de matéria. " * 100
    _CAPA = "\n".join(f"Manchete número {i} do dia" for i in range(60))

    def _fake(self, index_urls):
        def read(urls, mark_index=False):
            assert mark_index is True
            return [
                (self._CAPA, None, True) if u in index_urls else (self._ARTIGO, None, False)
                for u in urls
            ]
        return read

    def test_capa_nao_toma_vaga_de_materia(self):
        candidates = [{"url": "https://portal.com/"}] + [
            {"url": f"https://site{i}.com/materia/{i}"} for i in range(4)
        ]
        with patch.object(research._scraper, "read_many_dated",
                          side_effect=self._fake({"https://portal.com/"})), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4):
            pages = research._read_pages(candidates)
        assert [u for _, u, _ in pages] == [f"https://site{i}.com/materia/{i}" for i in range(3)]

    def test_capa_preenche_vaga_quando_falta_materia(self):
        capas = {"https://g1.com/", "https://estadao.com/"}
        candidates = [{"url": "https://g1.com/"}, {"url": "https://site.com/materia/1"},
                      {"url": "https://estadao.com/"}]
        with patch.object(research._scraper, "read_many_dated", side_effect=self._fake(capas)), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4):
            pages = research._read_pages(candidates)
        # matéria primeiro, capas depois, na ordem do ranking
        assert [u for _, u, _ in pages] == [
            "https://site.com/materia/1", "https://g1.com/", "https://estadao.com/"]
        assert [bool(r.get("_index")) for r, _, _ in pages] == [False, True, True]

    def test_recent_capa_entra_na_ordem_do_ranking(self):
        """recent=True: índice tem o dado de agora (manchete, cotação) e
        entra na ordem do ranking, marcado; recent=False o mesmo conjunto
        põe a matéria na frente."""
        capas = {"https://g1.com/", "https://cotacoes.com/"}
        candidates = [{"url": "https://g1.com/"}, {"url": "https://site.com/materia/1"},
                      {"url": "https://cotacoes.com/"}, {"url": "https://site.com/materia/2"}]
        with patch.object(research._scraper, "read_many_dated", side_effect=self._fake(capas)), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 4):
            recent = research._read_pages([dict(c) for c in candidates], index_first_class=True)
            timeless = research._read_pages([dict(c) for c in candidates])
        assert [u for _, u, _ in recent] == [
            "https://g1.com/", "https://site.com/materia/1", "https://cotacoes.com/"]
        assert [bool(r.get("_index")) for r, _, _ in recent] == [True, False, True]
        assert [u for _, u, _ in timeless] == [
            "https://site.com/materia/1", "https://site.com/materia/2", "https://g1.com/"]

    def test_research_web_passa_recent(self):
        seen = {}

        def fake_read(candidates, page_budget=None, index_first_class=False):
            seen["flag"] = index_first_class
            return [({"title": "t"}, "https://a.com/x", "conteúdo " * 100)]

        for recent in (True, False):
            research._recent_calls.clear()
            with patch.object(research, "_read_pages", side_effect=fake_read), \
                 patch.object(research, "_collect_links", return_value=[{"url": "https://a.com/x"}]), \
                 patch.object(research, "_summarize", return_value="resumo"):
                research.research_web(f"pergunta {recent}", recent=recent)
            assert seen["flag"] is recent

    def test_dossie_marca_capa(self):
        pages = [({"title": "t", "_index": True}, "https://g1.com/", "manchetes"),
                 ({"title": "t"}, "https://site.com/m", "matéria")]
        dossier = research._render_dossier(pages)
        assert dossier.count("Tipo: capa/índice") == 1
        assert dossier.index("Tipo: capa/índice") < dossier.index("FONTE [2]")


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

    def test_hub_only_enters_from_reserve(self):
        """Hub (sem densidade de link alta) também vai para a reserva: sem
        matéria nenhuma, ele entra marcado como capa."""
        candidates = [{"url": "https://portal.com/tecnologia/"}]
        page = self._MENU + "\n" + "x" * 1000

        with patch.object(research._scraper, "read_many_dated",
                          side_effect=lambda urls, mark_index=False: [(page, None, False)]), \
             patch.object(config, "RESEARCH_PAGE_BUDGET", 3), \
             patch.object(config, "RESEARCH_MAX_WAVES", 1):
            pages = research._read_pages(candidates)
        assert [u for _, u, _ in pages] == ["https://portal.com/tecnologia/"]
        assert pages[0][0]["_index"] is True


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


class TestSearchHealthNote:
    """Aviso de infraestrutura: só quando os motores mortos são maioria.

    Sem constante mágica — a comparação é contra os motores que de fato
    responderam naquela busca, então o aviso se ajusta sozinho a qualquer
    instância do SearXNG, com 3 ou com 30 motores.
    """

    def _results(self, *engines_per_result):
        return [{"url": f"http://x.com/{i}", "engines": list(e)}
                for i, e in enumerate(engines_per_result)]

    def test_maioria_morta_avisa(self):
        down = {f"e{i}": "Suspended: CAPTCHA" for i in range(10)}
        with patch.object(research._search, "health", return_value=down):
            note = research._search_health_note(self._results(["bing"], ["bing"]))
        assert "AVISO DE INFRAESTRUTURA" in note
        assert "10 motores" in note
        assert "respondeu apenas: bing" in note
        # o ponto do aviso: cortar a re-chamada em círculo
        assert "NÃO vai melhorar" in note

    def test_degradacao_menor_nao_avisa(self):
        down = {"yahoo": "HTTP protocol error"}
        results = self._results(["bing"], ["duckduckgo"], ["brave", "qwant"])
        with patch.object(research._search, "health", return_value=down):
            assert research._search_health_note(results) == ""

    def test_tudo_saudavel_nao_avisa(self):
        with patch.object(research._search, "health", return_value={}):
            assert research._search_health_note(self._results(["bing"])) == ""

    def test_zero_resultados_com_motores_mortos_avisa(self):
        down = {"a": "", "b": ""}
        with patch.object(research._search, "health", return_value=down):
            assert "AVISO DE INFRAESTRUTURA" in research._search_health_note([])


class TestFunctionWordsAreNotSubject:
    """Palavra de função não distingue uma pergunta de outra (anti-repetição).

    Corpus de assuntos não relacionados, nos DOIS desfechos: o token de
    função some, o token de assunto fica.
    """

    @pytest.mark.parametrize("query,esperado_fora", [
        ("Tem como limitar as portas de um usuário no tailscale?", "tem"),
        ("Você pode me dizer quem venceu a corrida de ontem?", "pode"),
        ("Não sei se ele deve tomar a segunda dose da vacina", "deve"),
        ("Can I have a refund for this order?", "have"),
        ("Vamos precisar de mais memória para rodar o modelo?", "vamos"),
    ])
    def test_funcao_sai(self, query, esperado_fora):
        assert esperado_fora not in research._content_tokens(query)

    @pytest.mark.parametrize("query,esperado_dentro", [
        ("Tem como limitar as portas de um usuário no tailscale?", "tailscale"),
        ("Você pode me dizer quem venceu a corrida de ontem?", "corrida"),
        ("Não sei se ele deve tomar a segunda dose da vacina", "vacina"),
        ("Can I have a refund for this order?", "refund"),
        ("Vamos precisar de mais memória para rodar o modelo?", "memoria"),
    ])
    def test_assunto_fica(self, query, esperado_dentro):
        assert esperado_dentro in research._content_tokens(query)
