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

This holds for every tool, not just `research_web`. `read_url` returns raw
text, so there is nothing in the material for the model to preserve — it
prefixes a `Fonte desta página` header with a ready markdown link
(`_source_header`, built from the URL actually delivered). Observed on
2026-08-29, before that header existed: an answer built from 21 `read_url`
calls cited "ACL policy examples - Tailscale Docs", a page name with no
address anywhere. That was not disobedience; there was no link to keep.

This holds for anything the tools return, now and later. If a future format
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
./start.sh                           # search-engine/ stack + MCP --http; Ctrl+C stops all

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
   against the search source (`_search`: Google CSE over Tor with SearXNG
   fallback, see "Search source" below), merges results ranked by
   cross-query agreement first, then result score (`_merge_results`). The
   merge walks every position each search returned (20 on Google CSE), not
   `_search.max_results` (SearXNG's 10): positions 11-20 held the decisive
   page in 3 of 10 measured questions.
   Variants must keep the question's names/terms verbatim — a translated
   or guessed name searches for something else (observed: "Pai Putrefato"
   became "Rotfather", a name that does not exist). The prompt alone doesn't
   hold that on small models, so `_keyword_query` always adds, in code, the
   question minus function words (`_STOPWORDS`), names/case/accents intact;
   a mid-sentence capital is kept even if it's a function word ("Lei do
   Bem"). The natural-language question as a query matched keyword PDFs;
   its keyword form matched the subject. Parentheticals are left out of the
   keyword form: they are asides, often the calling agent's own guess
   ("Pai Putrefato (Rotting Father)" searched both names together and no
   page has both). `_names_query` adds, in code, a search with only the
   question's name phrases (≥2 name words): the keyword form still ANDs
   every content word, and on Google "…iniciar quest cadeia Pai Putrefato
   Ato 3" gave 3 results, 0 with the name, vs 11/20 for the names alone.
   It doesn't take an LLM variant's slot. `_name_phrases` splits names at
   punctuation (the parenthesis used to glue "Pai Putrefato Rotting
   Father" into one phrase no page contains) and takes the first word when
   it runs straight into a name ("Baldur's Gate").
2. `_select_and_read` — `_rerank` triages the pool by title+site+snippet in
   one short LLM call (~2 s) and only the picks are read; the merge order
   comes back only if the triage fails or no pick opens. Round-robin alone
   gave a bad query's junk (keyword-matching PDFs, 25k chars each) the same
   slots as a good query's hits — most of the summary prefill and the wrong
   answer. Picks that don't open (video, login wall) enter the dossier as
   snippet-only sources (`_snippet_sources`): their title is sometimes the
   only link between the question's name and the name the pages use.
   `RESEARCH_MAX_PER_DOMAIN` is applied AFTER the triage, in triage order
   (`_cap_per_domain`), never in the merge: in the merge it cut unjudged —
   a wiki's 2 slots went to a character page and the wiki's homepage, and
   the page that answered (same site) never reached the triage.
   `_read_pages` — downloads/extracts candidates in waves
   (`RESEARCH_MAX_WAVES`) until `RESEARCH_PAGE_BUDGET` usable pages are
   collected or the char budget (`_dossier_char_budget`, derived from
   `MODEL_CONTEXT_TOKENS - MODEL_RESERVE_TOKENS`) runs out. A dead/blocked/
   too-short page doesn't consume a budget slot — the pool (`RESEARCH_POOL_SIZE`)
   backfills it.
   `_bridge` — when a name phrase of the question (`_name_phrases`:
   mid-sentence capitals or letter+digit tokens, joined across
   `_NAME_CONNECTORS` like the "do" in "Lei do Bem") appears in no page read,
   `_bridge_terms` picks title-case words that co-occur with it in the
   pool's titles/snippets, sit in ≥2 candidates and ≤10% of the pool (rarest
   first; the candidate's own site name and ALL-CAPS title words excluded).
   Up to `_BRIDGE_CANDIDATES` of them go to `_pick_bridge_terms`, one short
   LLM call that reads the snippets carrying the name and answers only with
   numbers from that list (it can't put a word that isn't in the pool into
   a search; "0" = none fits; failure = statistic order). Counting alone
   tied the linking name with site chrome and title-case words ("Trumbo" vs
   "Comments", "Act", "Encontre") and lost 7 of 10 real pools; the picker
   chose the linking name in 10/10, plus Luiz Gonzaga for "Rei do Baião",
   Herpes Zoster for "cobreiro", and nothing when only chrome co-occurred.
   The pool results citing the question's name together with a picked term
   enter the dossier as snippet sources right after the bridge pages
   (`_BRIDGE_EVIDENCE`): without that proof the summary read the right
   pages, which only use the other name, and denied the question 3/3,
   because it may only assert an identity the material shows. The anchor
   (the question's names that pages did confirm) drops a single-word,
   digit-free phrase the read pages write in lowercase — a common word the
   asker capitalized ("no Ato 3"): "Trumbo Baldur's Gate Ato" gave 5/20
   on-subject results, "Trumbo Baldur's Gate" 18/20. It runs one
   short search per term: "term + confirmed names" (one extra
   common word took "Trumbo BG3" from 11 relevant results to 0). Up to
   `_BRIDGE_PAGES` new pages are read within the remaining char budget and
   go FIRST in the dossier: at the end, behind 6 pages that "don't mention
   the quest", the summary denied the question with the right pages in hand.
   Measured 10/09/2026 on "Pai Putrefato" (a localized name no English page
   uses): v0.2.1 0/3, bridge 3/3; it fired on none of the other 9 eval
   questions. The answering page was already in the pool — nothing linked
   the two names for the triage. A filter dropping LLM variants without the
   question's names was measured first and rejected (0/3, −6.7 on the news
   question: a variant covering another facet of the question has no name).
3. `_render_dossier` — concatenates page content with source headers.
4. `_summarize` — one more LLM call, cites URLs, dated, in pt-BR.
5. Final answer appends the source URL list assembled in code (not asked of
   the model) — the model unreliably keeps URLs verbatim in prose.

Reasoning is spent only where it decides the answer: `_rerank`, `_summarize`
and `analyze_urls` call `chat(..., reasoning=True)`, which merges
`REASONING_BODY` over `EXTRA_BODY` when `USE_REASONING=true` (default
false; empty `REASONING_BODY` = the Qwen3.x/llama.cpp format); query
variants and the bridge picker stay on `EXTRA_BODY` alone. Measured 11/09/2026 (qwen3.8:27B, 10 questions
x 2 alternating rounds, blind grading): "low" on EVERY call gave +4.5
points (18 of 20 pairs) for +34 s per search (40 -> 74 s), and the gain was
in triage too (the official doc was read only with reasoning), so don't move
`_rerank` back to the short calls without measuring. "low" only on these
three calls, same design: +5.25 points (86.0 -> 91.25, 18 of 20) for +30 s
(40.0 -> 69.7 s). The cost lives where the gain is (Q2 log: triage +10 s,
summary +21 s); the short calls were ~4 s of it.

`server._ChainGuard` (FastMCP middleware) bounds a calling agent that
searches in circles, rewriting the question each time (which dodges
`research_web`'s same-question cache): `research_web`/`analyze_urls` calls
in one MCP session that start within `_CHAIN_GAP_SECONDS` of the previous
return are one agent turn; from the 3rd on the result carries a stop note,
the 5th doesn't run. Structural (timing + session), never the subject.
It also keeps the turn's earlier `research_web` questions and hands them
to the tool through the `research.chain_questions` contextvar (FastMCP runs
sync tools via `anyio.to_thread`, which carries context). When a call
shares a name phrase with an earlier one of the turn and dropped another
(`_carried_from_chain`: the same question rewritten, the user's name swapped
for the agent's guess — observed: "…Pai Putrefato (Rotting Father)…" then
"…quest Rotting Father chain how to start"), the earlier question's names
query joins the searches and the earlier question goes to triage, bridge
and summary as context. A call about something else in the same turn shares
no name and runs untouched.

`research_web` takes a REQUIRED `user_message`: the user's latest message,
copied verbatim. The chain carry above only helps from the 2nd call on;
the agent often loses the user's name before the FIRST one (measured
11/09/2026, qwen3.8:27B with Open WebUI's params, first call only: the
query kept "Pai Putrefato" in 7/20 — "Rotting Bride", "Rotten Brain"
instead). `_carried_from_user` fires when the query lost a name phrase of
the message — no shared name required, the message is this turn's by
definition — and does what the chain carry does: the message's names
query joins the searches and the message goes to triage, bridge and summary
as context (chain carry is the fallback). A message that just repeats the
query changes nothing. The bridge then hunts only the message's own name
phrases (`names` through `_select_and_read` → `_bridge`): a name that is in
the query and not in the message is the agent's guess, the same rule as the
parenthetical. Measured before that rule, over 40 simulated conversations:
the bridge hunted "Rotting Bride"/"The Rotfather" too, picked "Bhaal",
"Warhammer", "Mizora" as links and read those pages first, and the quoted
message's first word ("Como") counted as a name. After it (10 vs 10):
clean answer 9/10 (was 7), hedged 1 (was 3), wrong 0, 1.2 searches and
114 s per conversation (were 1.7 and 138 s). Cut at `_USER_MESSAGE_MAX_CHARS` (a size guard for
pasted text, not measured). Measured: optional, the agent filled it 9/20;
required, 20/20 verbatim. End to end (simulated Open WebUI agent, 10
conversations per arm, alternating): right answer 10/10 vs 6/10, 1.7 vs
2.3 searches per conversation, 138 vs 174 s; with reasoning off, 10/10 vs
7/10. Keep it required and keep its description short and literal: an
optional field with a sentence about "when the query needs rephrasing"
dropped the query's own name retention to 3/20.

`read_url` is the plain counterpart: single URL, full text, no LLM, no
budget truncation (`WebScraper(limit=None)`).

`util/scraper.py` (`WebScraper`) downloads through `curl_cffi` impersonating
Chrome's TLS/HTTP2 fingerprint (a Chrome UA on top of `requests`' fingerprint
still got 403 from UOL, Glassdoor, Britannica; JS challenges like Cloudflare's
still fail — that needs a real browser). Index pages (link density ≥ 0.65) and
hubs are *marked*, not dropped: `_read_pages` keeps them in a reserve that only
fills leftover slots, except with `recent=True`, where they enter in rank order
(the headline/quote/forecast of "now" lives on them). It extracts main content via
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

## Search source: Google CSE over Tor

`research._search` comes from `util/search_chain.py::build_search()`.
`SEARCH_BACKEND=google_tor` (default) gives `SearchChain(GoogleCSE, SearXNG)`;
`searxng` gives the plain `SearXNG()` (rollback). Both expose the same
`search`/`max_results`/`health`/`reset_health`, so the pipeline never knows
which one it has. Pages are still read directly, never through Tor.

- `util/google_cse.py` calls the endpoint the CSE widget uses (the same as
  `searx/engines/google_cse.py`); not a documented API. Token from `cse.js`,
  cached 1 h. Only the JSON `error` field decides: `code 429` = this path is
  blocked (arrives as HTTP 200); `code 403` = token rejected, refetch once
  on the same path. Never scan the body for "unusual traffic": a result
  snippet that talks about it would trigger failover — a dependency on what
  the question is about. 200 with no `results` is "nothing found", not a
  block.
- Route per query, no sleep anywhere: rotation channel → the other channel
  → the first again with a fresh credential → CSE direct from this machine
  (`GOOGLE_CSE_DIRECT_FALLBACK`) → `GoogleUnavailable`, and `SearchChain`
  falls back to SearXNG.
- `util/tor.py`: a channel changes exit IP by changing its SOCKS credential
  (containers run `IsolateSOCKSAuth`); `NEWNYM` over the ControlPort is
  best-effort and its failure never breaks a search. `ChannelPool` rotates
  with its own counter, so `_collect_links` passes no channel index.
- `SearchChain.health()` is empty while Google answers; after a fallback it
  returns SearXNG's dead engines plus `google cse`, so
  `_search_health_note` keeps announcing degraded search with its rule
  unchanged.
- `uv run python -m evals.search_ab` compares SearXNG and Google side by
  side in one session and runs a continuous-load pass (block rate per
  channel, failovers, direct/SearXNG fallbacks). See `plans/tor.md`.

## search-engine/

Docker compose stack for the search infrastructure the server depends on.
`search-engine/docker-compose.yaml` brings up SearXNG (`search-engine/searxng/`)
and the four Tor search channels `tor-a`..`tor-d` (`search-engine/tor/`, see
`plans/tor.md`). A fresh clone runs with:

```bash
cp .env.example .env
(cd search-engine && docker compose up -d)
uv run web-search-mcp
```

The Tor containers read the ROOT `.env` (`env_file: ../.env`): one file
configures both the MCP and the stack, so `TOR_CONTROL_PASSWORD` can never
diverge between them. Tor ports are published on 127.0.0.1 only and must stay
that way — `0.0.0.0` would turn them into an open proxy.

`search-engine/searxng/settings.yml` is tracked and mounted read-only over
`/etc/searxng/settings.yml`, so a fresh clone boots ready: `formats: [html,
json]` (without json, `research_web` gets 403) and a curated `hostnames:`
block (high_priority for reference news/tech/science/games/AI sources,
low_priority for content farms) that drives result ranking via the SearXNG
score. `search-engine/searxng/data/` stays gitignored for the runtime files
SearXNG writes.

The *live* deployment on this machine lives outside the repo:
`/home/fabio/services/searxng` (own compose, config at `data/settings.yml`,
editable from the host; `docker restart searxng` after edits) and
`/home/fabio/services/tor` (tor-a..tor-d only, password in its own `.env`,
chmod 600, same value as the production MCP's `TOR_CONTROL_PASSWORD`).
Changes made there should be mirrored into `search-engine/` and vice-versa.
Never bring up the repo's compose on this machine alongside the live ones:
container names and ports collide.

## Quando chamar as tools é decisão do cliente, não do servidor

O `instructions=` do FastMCP (`server.py`) não chega ao modelo no Open WebUI:
o cliente usa a descrição digitada à mão na conexão, e a política de chamada
vem do system prompt do usuário (`user.settings.ui.system` em
`/home/fabio/services/open-webui/data/webui.db`, editável via
`docker exec -u 0 open-webui`; backup antes, F5 depois — o localStorage da
aba sobrescreve). Antes de culpar o pipeline por uma chamada que não devia
ter existido — ou por uma que não existiu —, verifique no cliente se a
chamada ocorreu e o que o prompt manda.

Duas falhas medidas ali, nenhuma delas do pipeline: 11/09/2026, sem âncora de
data nem seção de pesquisa, uma pergunta sobre hoje foi respondida de memória
com data inventada e zero chamadas; 12/09/2026, com a seção de pesquisa mas
sem portão de tarefa, "revise este texto" disparou `research_web` porque o
texto continha um termo verificável. Uma regra de pesquisa que só olha para
"eu sei este fato?" não distingue **tarefa sobre o texto já colado** (revisar,
reescrever, traduzir, resumir) de **pergunta por fato externo**; a primeira
nunca precisa de ferramenta. A distinção é estrutural (de onde vem o
material), não do assunto do texto.
