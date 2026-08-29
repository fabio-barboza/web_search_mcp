# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## MANDATORY: this is a general-purpose web search tool

`research_web` is a **generic** pipeline: the model decides it needs a fact,
the tool searches, opens URLs, reads them, summarizes, and returns the
summary to the calling agent. Nothing about it is supposed to know what the
question is *about*.

When a bad result is reported, the example is always just an example. A
question about a game, about today's headlines, about a library's API — all
of them are the same request wearing different clothes. **Never fix the
example.** Ask what generic property of the pipeline failed (search quality,
page selection, extraction, budget, attribution) and fix that.

FORBIDDEN, without exception:

- Adding a topic, domain, product, or vertical to any prompt
  (`_QUERIES_INSTRUCTION`, `_BASE_INSTRUCTION`, tool docstrings, MCP
  `instructions`, the `pesquisador` prompt). No "for games…", "for news…",
  "for AI models…", no naming outlets, sites, or franchises.
- Branching the pipeline on what the question is about — regexes over the
  query text that detect a subject, per-subject page budgets, per-subject
  seed URLs.
- Tuning any threshold so that one reported example passes.

ALLOWED, and the right shape of a fix:

- Structural signals that hold for any page in any language: link density,
  path depth, prose ratio, text length, HTTP status, URL well-formedness.
- Search-infrastructure work (SearXNG engines, ranking, dedupe, domain caps).
- Budget, concurrency, caching, and error handling.
- Comments citing the pages a threshold was measured against — measurement
  evidence is documentation, not a topic dependency. Whether code is generic
  is decided by what it *branches on*, never by what its comments mention.

Test any new heuristic against a corpus spanning unrelated subjects and
BOTH outcomes (pages it must reject AND pages it must keep), and record the
numbers in a comment. A rule validated on one subject is not validated.

The news-panorama path that used to live here (`_SEEDS_BR`, `_SEEDS_WORLD`,
`_panorama_seeds`, `_expand_seeds`, `_article_links`,
`RESEARCH_PANORAMA_PAGES`, the `evals/headlines.py` eval and the panorama
sentences in `_QUERIES_INSTRUCTION`/`_BASE_INSTRUCTION`) was DELETED on
2026-08-29, and with it the last subject-specific code in the pipeline. It
seeded newspaper front pages ahead of every search result whenever a query
mentioned news and `recent=True`, gated by a blacklist of topics
(`tecnolog|game|esport|...`). The blacklist failed exactly as this rule
predicts: "Alguma notícia relevante de que será lançado o Qwen3.8:35B?"
matched none of its words, so the whole page budget went to Brazilian
election coverage and the answer contained zero facts about what was asked.
The same question without the seeds answers correctly from 6 pages.

Do not bring it back, in any shape — not seed URLs, not a per-subject page
budget, not a gate that asks what a question is about. If broad "what
happened today" questions ever need better coverage, the fix goes in search
infrastructure (engines, ranking, fan-out), which is generic by
construction.

## MANDATORY: a citation marker requires its link in the text

Never emit a reference marker that the reader cannot resolve from the text
alone. `[1]`, `[2]` and friends depend on a numbered legend at the end of
the answer — and the calling agent routinely drops that legend when it
rewrites the summary (observed in Open WebUI: markers arrive, the URL list
does not). What reaches the user is a number pointing at nothing, next to a
fact they cannot verify.

So every citation carries its own URL inline, as a markdown link built in
code from the page that was actually read (`_label_citations`). A marker
with no link must not exist: a number the model invented for a source that
is not in the list is deleted from the summary, not left dangling.

This holds for anything the tool returns, now and later. If a future format
change makes attribution depend on a separate block again, it is wrong.

## MANDATORY: quem lê uma página tem que saber qual página leu

Um 3xx que troca de endereço é invisível quando só o texto volta: o
servidor responde 200, a extração dá conteúdo, e nada denuncia que a URL
pedida não existe. Sem esse sinal não há condição de parada — observado em
29/08/2026: `tailscale.com/kb/9999999/nao-existe` responde 308 para
`/docs`, o agente leu a página genérica, concluiu que era a errada, chutou
outro número de KB, caiu na MESMA página, e repetiu isso por mais de dez
chamadas até desistir sem responder nada.

Então `_download` devolve a URL final e `read_url`/`analyze_urls` avisam
quando ela difere da pedida (`WebScraper.redirect_notice`). A comparação
ignora esquema, `www.`, barra final e query — canonicalização não é o
agente ter caído em outro lugar. Vale para qualquer formato futuro: o
texto de uma página nunca deve ser entregue sob um endereço que não é o
dele.

## MANDATORY: busca degradada tem que ser dita, não disfarçada

Motor de busca suspenso (CAPTCHA, limite de taxa) não é erro: o SearXNG
responde 200 com o que sobrou. Quando sobra pouco, o resultado é
casamento de nome de marca, e quem chamou não distingue isso de "o assunto
não existe" — então reformula a pergunta para sempre. Medido em
29/08/2026, com 13 de 15 motores suspensos, duas queries técnicas
completamente diferentes devolveram a mesma lista de homepages.

`_search_health_note` compara os motores mortos com os que responderam
naquela busca (sem constante mágica, ajusta sozinho a qualquer instância)
e, quando os mortos são maioria, prefixa a resposta com o aviso e a
instrução explícita de NÃO repetir. Isso é infraestrutura, não assunto:
o aviso não sabe o que foi perguntado.

## Commands

```bash
uv sync                              # install deps (Python >= 3.13)
cp .env.example .env                 # local config: SEARXNG_URL, MODEL_BASE_URL etc.

uv run web-search-mcp                # run server, stdio transport
uv run web-search-mcp --http         # run server, streamable-http (uvicorn)

uv run --group test pytest           # full test suite (deterministic, mocks network + LLM)
uv run --group test pytest tests/test_research.py::test_name  # single test

uv run python -m evals.run           # eval against real web + real LLM, not CI — see evals/
```

Tests need no SearXNG/LLM running. Evals and manual server runs do.

## Architecture

MCP server (FastMCP) exposing three tools: `read_url`, `research_web` and
`analyze_urls`. The point of the whole project: `research_web` does its
search/scrape/summarize pipeline in its **own** context (its own LLM call),
and only a ~700-token summary crosses back to the calling agent — never the
raw HTML/text. `analyze_urls` (`tools/analyze.py`) applies the same
philosophy to user-supplied URLs: reads 1-8 pages, one LLM call with the
user's request (summary/technical opinion/comparison), returns only the
analysis; per-page char budget = dossier budget split across the URLs.

Pipeline in `tools/research.py::research_web`:
1. `_collect_links` — generates 3 search-query variants via LLM
   (`_generate_queries`), runs the original query + variants in parallel
   against SearXNG (`util/searxng.py`), merges results ranked by
   cross-query agreement first, then SearXNG score (`_merge_results`).
2. `_read_pages` — downloads/extracts candidates in waves
   (`RESEARCH_MAX_WAVES`) until `RESEARCH_PAGE_BUDGET` usable pages are
   collected or the char budget (`_dossier_char_budget`, derived from
   `MODEL_CONTEXT_TOKENS - MODEL_RESERVE_TOKENS`) runs out. A dead/blocked/
   too-short page doesn't consume a budget slot — the pool (`RESEARCH_POOL_SIZE`)
   backfills it.
3. `_render_dossier` — concatenates page content with source headers.
4. `_summarize` — one more LLM call, cites URLs, dated, in pt-BR.
5. Final answer appends the source URL list assembled in code (not asked of
   the model) — the model unreliably keeps URLs verbatim in prose.

`read_url` is the plain counterpart: single URL, full text, no LLM, no
budget truncation (`WebScraper(limit=None)`).

`util/scraper.py` (`WebScraper`) downloads with a real browser UA (bare
`Mozilla/5.0` gets 406'd by some sites), extracts main content via
trafilatura with a structural DOM-cleaning fallback (`_clean`) for pages
where "main content" extraction misses short but relevant text (bios,
headline aggregators). Blocks SSRF (private/loopback/link-local IPs,
non-http schemes) in `_is_safe_url`. Distinguishes `failed()` (explicit
download/extraction failure) from `unusable()` (failed OR too short to be
real content, < `_MIN_USEFUL_CHARS`) — `read_url` wants whatever came back
however small; `research_web`'s budgeted pipeline needs to reject short
pages so the slot passes to the next pooled candidate.

`llm.py` talks to any OpenAI-compatible `/chat/completions` endpoint via
plain `requests` (no SDK). `_resolve_model` re-queries `GET /models` on
every call (no caching) when `MODEL` is unset in config, adopting whatever
model the server already has loaded — this avoids fighting another client
(e.g. a webui) for the model slot and forcing reloads. Only picks up a
`status: loaded/ready` field (llama.cpp/llama-swap router format); falls
back to the single model in the list; raises if ambiguous.

`config.py` is the single source of all env/config reads (`os.getenv` lives
only here) — read it for the meaning and defaults of every tunable rather
than grepping call sites. Notably: the dossier char budget uses
`llm.context_tokens()`, which detects the real `--ctx-size` of the loaded
model from `GET /models` (`status.args`, llama.cpp/llama-swap format) on
every research call; `MODEL_CONTEXT_TOKENS` is only the fallback for
providers that don't expose it. A too-high value there causes the *whole*
search to be thrown away on an HTTP 400 after search+scrape already
happened.

Installed via `uvx`/`uv tool install`, `.env` is not read at all (config.py
sits in site-packages, dotenv's upward search never finds a project root) —
everything must come through the client's `env`/`-e` block (stdio) or the
shell environment of whoever starts the process (`--http`). Running from a
clone, `.env` at the repo root is picked up normally.

`--http` mode has no authentication; default `MCP_HOST=127.0.0.1` keeps it
local-only. Don't suggest binding `0.0.0.0` without flagging that it exposes
unauthenticated scraping/LLM-proxying to the whole network.

## searxng/

Docker compose stack for a local SearXNG instance the server depends on.
`searxng/settings.yml` is tracked and mounted read-only over
`/etc/searxng/settings.yml`, so a fresh clone boots ready: `formats: [html,
json]` (without json, `research_web` gets 403) and a curated `hostnames:`
block (high_priority for reference news/tech/science/games/AI sources,
low_priority for content farms) that drives result ranking via the SearXNG
score. `data/` stays gitignored for the runtime files SearXNG writes.

The *live* deployment on this machine lives outside the repo, in
`/home/fabio/services/searxng` (own compose, config at `data/settings.yml`,
editable from the host; `docker restart searxng` after edits). Changes made
there should be mirrored into the repo's `searxng/settings.yml` and
vice-versa.
First boot writes `searxng/data/settings.yml` (gitignored) with
`formats: [html]` — must be hand-edited to add `json` or `research_web`'s
`format=json` requests get a 403.
