# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
The *live* deployment on this machine lives outside the repo, in
`/home/fabio/services/searxng` (config at `data/settings.yml`, mounted at
`/etc/searxng`; editable from the host, `docker restart searxng` after).
Its settings carry a curated `hostnames:` block (high_priority for
reference news/tech/science sources, low_priority for content farms) that
drives `research_web`'s result ranking via the SearXNG score.
First boot writes `searxng/data/settings.yml` (gitignored) with
`formats: [html]` — must be hand-edited to add `json` or `research_web`'s
`format=json` requests get a 403.
