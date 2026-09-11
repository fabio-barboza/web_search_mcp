# web-search-mcp

[Português](README.md) | **English**

A standalone [MCP](https://modelcontextprotocol.io) server that searches the
web, opens the pages, reads their content and returns **a summary with its
sources** to the calling agent. Three tools — `research_web`, `analyze_urls`
and `read_url` — for any agent (Claude Code, LangChain, LangGraph, etc). No
embedded agent framework: just [FastMCP](https://gofastmcp.com).

**It is not an MCP for SearXNG.** Search is its own, with two sources:

- **Google via Tor, the primary.** The server queries Google (through the
  Google CSE endpoint) over four Tor channels, rotating queries across them.
  When Google blocks a channel, the query retries on another one immediately
  and the blocked one gets a new IP in the background.
- **SearXNG, the fallback.** Only used when Google fails on every path — or
  always, if you prefer, with `SEARCH_BACKEND=searxng`.

The whole stack (four Tor containers + SearXNG) ships ready as a
`docker compose` in `search-engine/`. Details in
[Where the links come from](#where-the-links-come-from-google-via-tor-searxng-as-fallback).

Summaries and messages returned by the tools are in Brazilian Portuguese.

## The idea: research in an isolated context

The whole research happens **outside the main agent's context window**. It
sends a natural-language question and gets back a short summary — it never
sees the raw material.

```
                     ┌─────────────────────────────────────────────┐
  main agent         │  web-search-mcp (own context)               │
  ────────────────   │                                             │
                     │  1. generates search variants (LLM)         │
  "who was X?"   ──► │  2. searches in parallel (Google via Tor;   │
                     │     SearXNG as fallback)                    │
                     │  3. triages by title/snippet (LLM), opens   │
                     │     only the picked pages                   │
                     │  4. builds the dossier ... 15-50k chars     │
                     │  5. summarizes with sources (LLM)           │
       summary  ◄──  │                          ....... ~700 tokens│
     ~700 tokens     └─────────────────────────────────────────────┘
```

Without this, a serious search means dumping tens of thousands of tokens of
HTML and extracted text into the main conversation — material that stays
there taking up space in **every** following call, long after it was used.
Here that cost is paid in a separate process, with the LLM of your choice,
and only the answer crosses over.

In practice:

- **The agent's context doesn't bloat.** It spends ~700 tokens per search
  instead of the 6k-20k tokens of material read. Long conversations with many
  searches stay viable.
- **A cheap model can do the expensive part.** Reading 5 pages and
  summarizing them can be done by a small local model; the main agent, the
  expensive one, only gets the finished result.
- **The answer comes cited.** The summary carries the URLs actually read and
  a date/time stamp, so you can check the source instead of trusting it.
- **One call is enough.** `research_web` already searches several angles
  internally and reads in parallel — the agent doesn't need to orchestrate
  search rounds.

`analyze_urls` applies the same idea to links the user already has: "summarize
these three articles", "compare the two proposals" — it reads the pages in
here and returns only the analysis. When you really want the raw text — "read
this link for me" — `read_url` is the one, and then the whole content goes to
the agent, unsummarized.

## Where the links come from: Google via Tor, SearXNG as fallback

`research_web` searches Google through the Google CSE endpoint (the same one
the embeddable search widget uses), over four Tor channels. SearXNG stays in
the stack, but as a fallback.

**Why.** SearXNG scrapes search engines and, under normal use, gets CAPTCHAs
and rate limits: measured on 2026-08-29 and again on 2026-09-10, 13 of 15
engines suspended. What's left returns brand-name matches — two different
technical questions came back with the same list of homepages. In the A/B of
2026-09-10 (`evals/search_ab.py`, 12 questions on unrelated subjects, same
search variants on both sides):

| Source | Empty searches | On-topic results | Pages read | Search p50 |
|---|---|---|---|---|
| SearXNG | 2 of 12 | 112 / 125 | 45 | 2.7 s |
| Google CSE via Tor | 0 of 12 | 228 / 230 | 71 | 4.6 s |

Under continuous load (20 back-to-back searches, 80 queries) 100% went out
through Tor, with 5 failovers between channels and no fallback to SearXNG;
search p50 rose to 9.7 s. That was measured with **two** channels — the
current four exist to spread that load, and have not been measured yet.

**How a query flows.** No step waits:

1. the current channel in the rotation;
2. blocked by Google → that channel gets a new circuit in the background
   (new SOCKS credential + `NEWNYM`) and the query retries **immediately** on
   another channel;
3. blocked again → a third channel (with only two, back to the first one,
   already on a new circuit);
4. the CSE from the machine's own IP (`GOOGLE_CSE_DIRECT_FALLBACK`);
5. SearXNG (`SEARXNG_FALLBACK`).

When the query reaches SearXNG and most of its engines are suspended, the
answer starts with a **degraded-search notice**, telling the agent that
coverage is incomplete because of the infrastructure — not because the
subject doesn't exist — and that repeating the search won't help. Without
that notice the agent keeps rephrasing the question in a loop.

Only the search goes through Tor. Pages are opened directly, from this
machine. `SEARCH_BACKEND=searxng` turns Google off and goes back to plain
SearXNG — rollback is changing the variable and restarting.

Caveats:

- **It is not a documented API.** If Google changes the format, parsing
  fails loudly and the search falls back to SearXNG instead of breaking.
- **The default CX is public and belongs to a third party** (blackle.com, the
  same one SearXNG's `google cse` engine uses). It may disappear;
  `GOOGLE_CSE_CX` swaps it without touching the code.
- **The direct fallback is not guaranteed.** The home IP also got a 429 from
  the CSE on the day of the measurement.
- **Terms of service.** Automated queries to Google, especially via Tor, go
  against Google's terms. Weigh that before using it; with
  `SEARCH_BACKEND=searxng` none of this happens.

Measurements, decisions and risks in [`plans/tor.md`](plans/tor.md) (in
Portuguese).

## Requirements

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)
- The search stack: four Tor containers (links come from the Google CSE, queried through them) and a fallback [SearXNG](https://docs.searxng.org/) with the JSON format enabled — all in a ready-made `docker compose` in [`search-engine/`](#search-infrastructure-via-docker-compose). Without Tor, search still works (direct CSE, then SearXNG); `SEARCH_BACKEND=searxng` uses SearXNG only
- An LLM server with an OpenAI-compatible API (`/v1/chat/completions` and `/v1/models`) — e.g. [llama.cpp server](https://github.com/ggml-org/llama.cpp), vLLM, or OpenAI itself

### Search infrastructure via docker compose

`search-engine/` ships the whole search stack, already on the ports that
`.env.example` defaults to. In a fresh copy of the repo:

```bash
cp .env.example.en .env       # change TOR_CONTROL_PASSWORD
cd search-engine
docker compose up -d --wait
```

The first start builds the Tor image locally (`search-engine/tor/`: Alpine
with the `tor` package pinned to `0.4.9.12-r0`), so it takes longer and needs
internet access. If Alpine drops that version from its repository, the build
fails — bump the number in the `Dockerfile`. `--wait` holds the command until
the Tor channels are `healthy` (bootstrap takes up to ~60 s); without it,
searches during that window fall back to the direct CSE and SearXNG.

It starts:

| Service | Port (host) | Role |
|---|---|---|
| `searxng` + `valkey` | `8886` | SearXNG with `search-engine/searxng/settings.yml` mounted on top (JSON enabled and source curation ready) |
| `tor-a` | `127.0.0.1:9060` SOCKS, `9061` control | Tor search channel ([`plans/tor.md`](plans/tor.md)) |
| `tor-b` | `127.0.0.1:9070` SOCKS, `9071` control | second channel, independent of the first |
| `tor-c` | `127.0.0.1:9080` SOCKS, `9081` control | third channel |
| `tor-d` | `127.0.0.1:9090` SOCKS, `9091` control | fourth channel |
| `mcp-searxng` + `caddy` | `8887` | third-party MCP search server, independent of this project (see below) |

The Tor containers read the **same root `.env`** that configures the MCP
(`env_file: ../.env`), so there is a single ControlPort password and no way
for the two sides to diverge. Without the `.env` the compose fails right
away. Tor ports only listen on `127.0.0.1`: publishing them on `0.0.0.0`
would turn the machine into an open proxy. Inside the container there is a
second barrier: `torrc` only accepts SOCKS from private ranges
(`SocksPolicy`), and the ControlPort requires the password.

The `mcp-searxng` and `caddy` services are a third-party MCP search server —
this project talks to SearXNG directly, over HTTP. If you don't use them, you
can remove them from the compose without affecting `web-search-mcp` at all.

Before bringing this up anywhere other than your own machine, change the
passwords. The placeholders are committed to this repository and are
therefore public:

- `TOR_CONTROL_PASSWORD` (in `.env`) — whoever has the password controls tor
- `SEARXNG_SECRET` (in `search-engine/docker-compose.yaml`) — the key SearXNG
  signs with; it should be a long random value, e.g. `openssl rand -hex 32`
- the `Bearer password123` in `search-engine/searxng/caddy/Caddyfile` — it is
  the **only** authentication in front of `mcp-searxng`, whose port `8887` is
  published on the host. Anyone who knows the token can use the service

### All at once: `start.sh`

```bash
./start.sh
```

Creates `.env` from `.env.example` (Portuguese comments; copy
`.env.example.en` to `.env` beforehand if you prefer English) if it doesn't
exist, reminds you which
variables to check (`MODEL_BASE_URL`, `MODEL_API_KEY`, `TOR_CONTROL_PASSWORD`,
`MCP_HOST`/`MCP_PORT`) and waits for Enter. Then it checks that the ports and
container names are free, brings up `search-engine/` and starts the MCP in
streamable-http. At the end it prints the URL to plug the client into
(`http://127.0.0.1:8765/mcp` by default). **Ctrl+C tears everything down**:
the MCP and the containers (`docker compose down`; volumes kept). If the MCP
dies on its own, the stack goes down with it. Extra arguments go to
`web-search-mcp`.

The LLM stays external: if `MODEL_BASE_URL` doesn't answer, the script warns
and starts anyway — `read_url` works without it, `research_web` and
`analyze_urls` don't.

## Installation

### From GitHub (no clone needed)

```bash
uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0 web-search-mcp
```

Registering in Claude Code:

```bash
claude mcp add web-search \
  -e SEARXNG_URL=http://localhost:8886 \
  -e MODEL_BASE_URL=http://localhost:8200/v1 \
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0 web-search-mcp
```

Installed this way, `.env` is not read: the whole configuration comes in
through `-e` — see [Configuring the installed server](#configuring-the-installed-server).
`@v0.2.0` pins that release. Swap it for `@main` to always track the tip of
the branch, or for any other tag or commit.

To keep the command fixed on your PATH instead of resolving it on every run:

```bash
uv tool install git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0
```

### From a clone (development)

```bash
uv sync
cp .env.example.en .env
```

`.env.example.en` is the English-commented copy of `.env.example`: same keys,
same defaults.

Edit `.env`: `MODEL_BASE_URL` for your model server and
`TOR_CONTROL_PASSWORD` (the Tor containers read this same file). With the
`search-engine/` stack on the default ports, `SEARXNG_URL` and `TOR_CHANNELS`
already match. The server starts with the defaults if you copy nothing, but
without an LLM at `MODEL_BASE_URL`, `research_web` and `analyze_urls` don't
work, and with no search up at all (Tor, direct CSE and SearXNG)
`research_web` finds no links — `read_url` works on its own.

## Usage

Two transports, same tools. The difference is who starts the process — and
that changes where the configuration comes from.

| | stdio | http |
|---|---|---|
| Who starts the process | the client, every session | you, once |
| How many clients | one per process | many on the same process |
| Network port | none | `MCP_HOST:MCP_PORT` |
| Configuration comes from | client `env` block (`-e`) and/or `.env` | environment of whoever started it and/or `.env` |

### stdio (default — for Claude Code and local clients)

```bash
uv run web-search-mcp
```

In practice you don't run this by hand: the client does. Registering in
Claude Code pointing at the clone:

```bash
claude mcp add web-search -- uv --directory /path/to/web_search_mcp run web-search-mcp
```

To register the version installed from GitHub, with the configuration in the
`env` block, see [Configuring the installed server](#configuring-the-installed-server).

### HTTP (share across several agents)

Start the server. From the clone, which reads the root `.env`:

```bash
uv run web-search-mcp --http
```

Or installed, passing the configuration through the environment:

```bash
SEARXNG_URL=http://localhost:8886 \
MODEL_BASE_URL=http://localhost:8200/v1 \
uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0 web-search-mcp --http
```

It listens on `http://{MCP_HOST}:{MCP_PORT}/mcp` (default `127.0.0.1:8765`).

With it running, register the client by URL:

```bash
claude mcp add --transport http web-search http://127.0.0.1:8765/mcp
```

Which in `.mcp.json` becomes:

```json
{
  "mcpServers": {
    "web-search": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

**In http mode the client's `env` block has no effect.** With stdio the
client spawns the process, so its `-e` becomes the server's environment; with
http the process is yours and already running when the client connects, with
the environment *you* gave it at startup. All configuration moves to the
server side, and changing a variable requires restarting it — editing
`.mcp.json` does nothing.

#### Keeping it running (user systemd, no sudo)

```ini
# ~/.config/systemd/user/web-search-mcp.service
[Unit]
Description=web-search-mcp (http)
After=network.target

[Service]
Environment=MODEL_BASE_URL=http://localhost:8200/v1
# Search: Google CSE over the four Tor channels of search-engine/, SearXNG as fallback
Environment=SEARCH_BACKEND=google_tor
Environment=TOR_CHANNELS=127.0.0.1:9060:9061,127.0.0.1:9070:9071,127.0.0.1:9080:9081,127.0.0.1:9090:9091
Environment=SEARXNG_URL=http://localhost:8886
# TOR_CONTROL_PASSWORD lives outside the unit, in a file only you can read (chmod 600)
EnvironmentFile=%h/.config/web-search-mcp/secrets.env
ExecStart=%h/.local/bin/uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0 web-search-mcp --http
Restart=on-failure

[Install]
WantedBy=default.target
```

The ControlPort password is the same as in the `.env` the Tor containers
read. It doesn't go in the unit because `systemctl --user cat`/`show` display
every `Environment=` to anyone who looks:

```bash
mkdir -p ~/.config/web-search-mcp
install -m 600 /dev/null ~/.config/web-search-mcp/secrets.env
echo 'TOR_CONTROL_PASSWORD=same-as-the-search-engine-.env' > ~/.config/web-search-mcp/secrets.env
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now web-search-mcp
```

The unit doesn't depend on the containers: `search-engine/` comes up on its
own at boot (`restart: unless-stopped`), and if the Tor channels aren't up
yet when the first search arrives, it falls back to the direct CSE and then
SearXNG instead of failing. The `SEARCH_BACKEND` and `TOR_CHANNELS` lines
repeat the defaults — they are there to make explicit what to change if the
channels live on another host or other ports. Running from the clone
(`WorkingDirectory=` pointing at it and `uv run web-search-mcp --http`), the
root `.env` is read and none of these lines are needed.

Before exposing this endpoint beyond your machine, note that the server has
no authentication at all. With the default `MCP_HOST=127.0.0.1` only local
processes can reach it, which is safe. Switching to `0.0.0.0` so another
machine can use it leaves the tools open to anyone on the network, with no
credentials, serving as a scraping proxy on top of your LLM. In that case put
a reverse proxy with a token in front and restrict `MCP_CORS_ALLOW_ORIGINS`
to the origins you use, instead of leaving `*`.

## Tools

### `research_web(query: str, recent: bool = False) -> str`

Researches the question on the web (generates search variants — always
including the question as keywords, names intact —, runs them in parallel,
triages the candidates by title and snippet and reads only the picked ones)
and returns a summary in Brazilian
Portuguese, with a date/time stamp. Every fact ends with a **markdown link to
the page it came from**, built in code from the URL actually read — not asked
of the model. A `[n]` marker the model invents for a source that doesn't
exist is deleted, instead of being left pointing at nothing. The numbered
list of URLs read goes at the end.

Use `recent=True` only when the answer depends on today (weather, exchange
rate, score, news). For stable facts (history, biography, concepts), leave
`recent=False` — filtering by date throws away the best sources.

The same question rephrased right afterwards (same set of content words, in
any order) returns the previous result instead of searching again: the tool
already searches several angles internally, and repeating only rereads the
same pages. Degraded search comes with the notice described in
[Where the links come from](#where-the-links-come-from-google-via-tor-searxng-as-fallback).

An agent that searches in circles, rewording the question each time (which
dodges the cache above), is braked per session: `research_web` and
`analyze_urls` calls that start right after the previous one returned count
as the same turn; from the 3rd on the result carries a notice to stop and
ask the user for the exact name, and the 5th doesn't run.

### `analyze_urls(urls: list[str], request: str = "Resuma o conteúdo.") -> str`

Reads 1 to 8 given URLs and returns only the analysis requested in natural
language — summary, technical review, opinion, comparison — done by this
server's LLM. The page text never enters the agent's context. The dossier's
character budget is split across the pages so they all fit together. The
answer lists the URLs analyzed and the ones that failed.

### `read_url(url: str) -> str`

Opens a specific URL and returns the page's main content as Markdown, in
full, with no search or summary — the raw text goes back to the agent to
process. It comes with a `Fonte desta página` ("source of this page") header
and a ready markdown link, so the agent can cite what it read with an
address.

In both tools that read URLs, if the server redirects to another address
(e.g. a page that no longer exists landing on the homepage), the answer says
so and uses the final URL. Without that notice the agent reads the wrong page
without knowing and retries in a loop. Both block URLs pointing at
private/loopback/link-local IPs (SSRF protection).

### `pesquisador` prompt

The server also exposes an MCP prompt with the usage policy: research before
answering anything not known for certain, one call per question, answer only
from the summary and keep the links and dates.

## Configuration

**No variable is required.** Every one has a default in the code, and the
server starts with no configuration at all. You only declare what deviates
from the default — in practice, `MODEL_BASE_URL`, if yours isn't on the port
below, and `TOR_CONTROL_PASSWORD`, which the Tor containers require.

The full list, with each default:

### Log

| Variable | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `ERROR`. `INFO` includes info and error logs; `DEBUG` shows every search, every URL read and the assembled dossier. Logs always go to stderr, never stdout — a single byte on stdout would corrupt the stdio protocol |

### Model (OpenAI-compatible API)

| Variable | Default | What it does |
|---|---|---|
| `MODEL` | *(empty)* | Model name to use. **Empty = use the server's default model**, i.e. the one already loaded: the server queries `GET /models` on every call and adopts the resident model, without forcing a swap or paying for a GPU reload. Fill it in only to pin a specific model, accepting the reload if it isn't the loaded one. See [Model detection](#model-detection) |
| `MODEL_BASE_URL` | `http://localhost:8200/v1` | Base of the OpenAI-compatible API, without the trailing `/chat/completions`. Works with llama.cpp, vLLM, Ollama, OpenAI, whatever |
| `MODEL_API_KEY` | `not-needed` | Sent as `Authorization: Bearer <value>`. Local servers usually ignore it; paid providers require the real key |
| `MODEL_TIMEOUT` | `120` | Timeout, in seconds, of each LLM call. A big model on CPU may need more |
| `MODEL_TEMPERATURE` | `0` | Temperature of the calls. `0` because the task is summarizing sources, not creating — high temperature here turns into hallucination |
| `MODEL_CONTEXT_TOKENS` | `65536` | Model context window, from which the dossier budget is derived. With llama.cpp/llama-swap the server reads the loaded model's real `--ctx-size` from `GET /models` on every search, and that value wins; this variable is the fallback for providers that don't expose it. With those, **it must match the real window**: declaring more makes the provider reject the call with HTTP 400 and the whole search is lost, after search and scraping were already paid for |
| `MODEL_RESERVE_TOKENS` | `4096` | How much of the window is reserved for what isn't dossier: instructions, question and the answer the model still has to generate. The dossier budget is `MODEL_CONTEXT_TOKENS - MODEL_RESERVE_TOKENS` |
| `EXTRA_BODY` | *(empty)* | Raw JSON merged into the `/chat/completions` payload, for parameters only your provider understands. E.g. turning off reasoning on Qwen3: `EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`. Invalid JSON crashes the server at boot, on purpose |
| `EXTRA_SYSTEM_PROMPT` | *(empty)* | Text appended to the end of the system prompt on every call. It exists because not every model turns reasoning off via an API parameter — some only obey an instruction. E.g. `EXTRA_SYSTEM_PROMPT=Reasoning strength: low`. Worth it: on a model that thinks by default, generating 3 search lines cost 2767 tokens / 39 s without it, versus 271 / 3 s with it |

### Search (Google CSE via Tor)

The path of each query is described in
[Where the links come from](#where-the-links-come-from-google-via-tor-searxng-as-fallback).

| Variable | Default | What it does |
|---|---|---|
| `SEARCH_BACKEND` | `google_tor` | `google_tor` (Google CSE over the Tor channels, with direct CSE and SearXNG as fallback) or `searxng` (SearXNG only, as before). Rollback is changing it and restarting |
| `TOR_CHANNELS` | `127.0.0.1:9060:9061,127.0.0.1:9070:9071,127.0.0.1:9080:9081,127.0.0.1:9090:9091` | One channel per item, `host:socks_port:control_port`, comma-separated. The default matches `tor-a`..`tor-d` in `search-engine/`. Accepts any number; with fewer than three, failover reuses a channel (already on a new circuit) |
| `TOR_CONTROL_PASSWORD` | *(empty)* | ControlPort password, for `NEWNYM`. The Tor containers won't start without it; the MCP will — empty only disables `NEWNYM`, and circuit rotation via the SOCKS credential keeps working |
| `GOOGLE_CSE_CX` | blackle.com's public CX | Which CSE to query. Swap in your own CX without touching the code |
| `GOOGLE_CSE_HL` | `pt-BR` | CSE interface language. Does not restrict the language of results |
| `GOOGLE_CSE_TIMEOUT` | `15` | Timeout, in seconds, of each CSE request (via Tor it takes 2-4 s) |
| `GOOGLE_CSE_DIRECT_FALLBACK` | `true` | Try the CSE from the machine's IP when the Tor channels fail. Don't count on it: the home IP gets 429s too |
| `SEARXNG_FALLBACK` | `true` | Fall back to SearXNG when Google failed on every path. With `false`, the search returns a search error in that case |

### SearXNG (fallback)

Used when Google failed on every path, or always, with
`SEARCH_BACKEND=searxng`. Only `SEARXNG_MAX_RESULTS` also applies to Google.

| Variable | Default | What it does |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8886` | Base of your SearXNG instance. Must have the JSON format enabled |
| `SEARXNG_MAX_RESULTS` | `10` | How many results of each search enter the merge, whatever the source (Google returns 20 per query; anything beyond this is dropped). `research_web` runs several searches and merges them, so this is the cap per search, not the total |
| `SEARXNG_TIMEOUT` | `10` | Timeout, in seconds, of each SearXNG query |
| `SEARXNG_LANGUAGE` | `auto` | Language passed with the search. `auto` lets SearXNG detect it from the query (a question in English searches English pages); pinning `pt-BR` pushes every search towards Brazilian pages |
| `SEARXNG_CATEGORIES` | `general,news` | SearXNG categories, comma-separated, passed through as-is |

### Scraper

| Variable | Default | What it does |
|---|---|---|
| `SCRAPER_TIMEOUT` | `6` | Timeout, in seconds, for downloading each page. Low on purpose: in `research_web` a slow page isn't worth holding up the whole search, and there are backup links to take its place |
| `SCRAPER_LIMIT` | *(empty)* | Character cut of the extracted text **in `read_url`**. Empty = whole page. Doesn't affect `research_web`, which uses `RESEARCH_PAGE_CHARS` |

### Research

| Variable | Default | What it does |
|---|---|---|
| `RESEARCH_PAGE_BUDGET` | `5` | How many pages go into a search's dossier, across all searches. The main quality × latency knob |
| `RESEARCH_POOL_SIZE` | `40` | Pool of candidate links, and what the title/snippet triage sees before any page is opened. A dead, blocked or empty link doesn't use up a budget slot: it yields to the next one in the pool. A narrow pool keeps the right page out of the triage |
| `RESEARCH_MAX_WAVES` | `4` | Cap on read attempts before giving up. Without it, a bad run of links would sweep the whole pool and blow up latency |
| `RESEARCH_MAX_PER_DOMAIN` | `2` | Max URLs from the same domain in the candidate pool. Without a cap, a search whose top 10 is all one site fills the dossier with a single outlet. `0` = no limit |
| `RESEARCH_PAGE_CHARS` | `25000` | Per-page character cap in the `research_web` dossier. It exists for the outlier: a single giant page once produced 412k characters = 103k tokens against a 65k context, and the whole search was lost. Don't squeeze it too much — a dossier that's too small makes the summary worse |

### MCP server

| Variable | Default | What it does |
|---|---|---|
| `MCP_NAME` | `web-search` | Name the server announces in the MCP handshake |
| `MCP_TRANSPORT` | `stdio` | Transport used when you **don't** pass `--http`. See [Usage](#usage) |
| `MCP_HOST` | `127.0.0.1` | Listening interface in `--http` mode. Read the warning below before changing it |
| `MCP_PORT` | `8765` | Listening port in `--http` mode |
| `MCP_CORS_ALLOW_ORIGINS` | `*` | Origins allowed by CORS in `--http` mode, comma-separated. Only matters for browser clients (e.g. MCP Inspector) |
| `TZ` | *(host time zone)* | Time zone used in the summary's date stamp. A convenience for containers, which default to UTC. E.g. `America/Sao_Paulo` |

### Eval judge

| Variable | Default | What it does |
|---|---|---|
| `EVAL_JUDGE_MODEL` | *(same as `MODEL`)* | Model used as judge in the [eval](#eval). Pointing it at a different model from the one that wrote the summary makes the evaluation much less lenient |

About `MCP_HOST` and `MCP_CORS_ALLOW_ORIGINS`: the server has no
authentication at all. With the defaults it only listens on `127.0.0.1`,
which restricts access to your machine. Switching `MCP_HOST` to `0.0.0.0`
exposes the tools to anyone on the network, with no credentials — which lets
your server be used as a scraping proxy and burn your LLM. If you need to
share it on the network, put a reverse proxy with authentication in front and
restrict `MCP_CORS_ALLOW_ORIGINS` to the origins you actually use.

### Where the configuration comes from

This doesn't depend on the transport: reading happens when `config` is
imported, before stdio or http come into play. What decides is **where
`config.py` lives**.

| Source | Running from the clone | Installed (`uvx` / `uv tool install`) |
|---|---|---|
| `.env` at the project root | applies | **ignored** |
| Environment variable | applies, and overrides `.env` | the only way |
| Code default | fallback | fallback |

`load_dotenv()` looks for `.env` walking up from `config.py`'s directory. In
the clone that reaches the project root and finds the file; installed,
`config.py` lives in `site-packages` and the search finds nothing — not even
a `.env` in the directory the server was run from. Installed, therefore,
everything comes in through environment variables.

Where that environment variable comes from does depend on the transport: with
stdio, from the client's `env` block (`-e`), since the client spawns the
process; with http, from the environment of whoever started the process —
`export`, `systemd`, `compose`.

Precedence is `environment variable > .env > default`, because
`load_dotenv()` runs without `override`. Handy in the clone to try a
variation without editing a file:

```bash
MODEL_BASE_URL=http://localhost:8205/v1 uv run web-search-mcp --http
```

### Configuring the installed server

Declare it in the registration's `env` block:

```bash
claude mcp add web-search \
  -e SEARXNG_URL=http://localhost:8886 \
  -e MODEL_BASE_URL=http://localhost:8200/v1 \
  -e MODEL_CONTEXT_TOKENS=65536 \
  -e TZ=America/Sao_Paulo \
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0 web-search-mcp
```

Note that `MODEL` isn't there: left out, the server uses the model already
loaded at your `MODEL_BASE_URL`. Pass `-e MODEL=model-name` only to pin one.

The command above writes this to `.mcp.json` (project scope) or to
`~/.claude.json` (user scope, with `-s user`):

```json
{
  "mcpServers": {
    "web-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/fabio-barboza/web_search_mcp@v0.2.0", "web-search-mcp"],
      "env": {
        "SEARXNG_URL": "http://localhost:8886",
        "MODEL_BASE_URL": "http://localhost:8200/v1"
      }
    }
  }
}
```

Whatever goes into `env` is stored in plain text in that file. For
`SEARXNG_URL` and `MODEL_BASE_URL` that doesn't matter, but if your LLM
provider requires a real `MODEL_API_KEY`, it ends up in `.mcp.json` — which
is usually committed when it's in project scope. In that case register with
`claude mcp add -s user`, which writes to `~/.claude.json`, outside the
repository.

Tor search needs nothing in the `env` block if the `search-engine/` stack is
on the same machine: the `TOR_CHANNELS` default already points at the four
channels on `127.0.0.1`. `TOR_CONTROL_PASSWORD` is optional for the MCP —
without it only `NEWNYM` is disabled — and, if you pass it via `-e`, the same
caveat as `MODEL_API_KEY` applies: it sits in plain text in the file.

### Model detection

When `MODEL` is empty, every LLM call queries `GET {MODEL_BASE_URL}/models`
to find the model already loaded, instead of forcing a specific one (avoids
paying for a GPU reload for nothing). There is no cache: if another client
swapped the model on the server, the next call already uses the new one.
Works natively with routers that expose load status (e.g.
llama.cpp/llama-swap); with generic providers, it falls back to the single
model in the list or asks you to set `MODEL` explicitly if it's ambiguous.

On those routers the same query also returns the arguments the model was
started with: the `--ctx-size` from there sets the dossier budget, instead of
`MODEL_CONTEXT_TOKENS`.

## Tests

```bash
uv run --group test pytest
```

Fully deterministic — mocks network and LLM, needs no SearXNG or model
running.

## Eval

```bash
uv run python -m evals.run
```

Runs a fixed set of questions against the real web and LLM, measures
faithfulness (summary claims supported by the dossier) and relevance, and
saves the result to `evals/results/`. It isn't a CI test — the web changes
between runs — it's for comparing runs and catching blatant hallucination,
not an independent evaluation (the judge is usually the same model that
wrote the summary).

```bash
uv run python -m evals.search_ab [--load 20] [--hl pt-BR en]
```

Link-source A/B: SearXNG × Google CSE via Tor in the same session, with the
same search variants on both sides, followed by a continuous load run that
measures block rate per channel, failovers and fallbacks to the direct CSE
and SearXNG. Needs the Tor channels and SearXNG up. The numbers in
[Where the links come from](#where-the-links-come-from-google-via-tor-searxng-as-fallback)
came from here.

## Layout

```
src/web_search_mcp/
  server.py         # FastMCP: tools, prompt, transport, main()
  config.py         # single source of configuration (reads the whole .env)
  llm.py            # chat completion via requests, model and context detection
  tools/
    research.py     # tool: searches, reads pages, summarizes with sources
    analyze.py      # tool: reads given URLs and returns only the analysis
    read_url.py     # tool: reads one specific URL
  util/
    search_chain.py # link source: Google via Tor, SearXNG as fallback
    google_cse.py   # Google CSE client, with failover between channels
    tor.py          # Tor channels: rotation and circuit renewal
    searxng.py      # SearXNG client
    scraper.py      # download (curl_cffi, Chrome TLS fingerprint) + extraction (anti-SSRF)
tests/              # pytest, no network, no LLM
evals/              # runs against the real web/LLM, on demand
search-engine/      # search docker compose: SearXNG + Tor channels (external dependency)
plans/              # approved change plans (in Portuguese)
start.sh            # brings up search-engine/ + MCP in --http; Ctrl+C tears it all down
```

`src/` layout on purpose: the installed package occupies a single namespace
(`web_search_mcp`), instead of dumping `server`/`config`/`util` into the root
of site-packages and colliding with other packages.
