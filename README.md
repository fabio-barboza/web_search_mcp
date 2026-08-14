# web-search-mcp

Servidor [MCP](https://modelcontextprotocol.io) autônomo que pesquisa na web,
abre as páginas, lê o conteúdo e devolve **um resumo com as fontes** para o
agente que chamou. Duas tools — `read_url` e `research_web` — para qualquer
agente (Claude Code, LangChain, LangGraph, etc). Sem framework de agente
embutido: só [FastMCP](https://gofastmcp.com).

## A ideia: pesquisar num contexto isolado

A pesquisa inteira acontece **fora da janela de contexto do agente
principal**. Ele manda uma pergunta em linguagem natural e recebe de volta um
resumo curto — nunca vê o material bruto.

```
                     ┌─────────────────────────────────────────────┐
  agente principal   │  web-search-mcp (contexto próprio)          │
  ────────────────   │                                             │
                     │  1. gera variantes de busca (LLM)           │
  "quem foi X?"  ──► │  2. busca em paralelo no SearXNG            │
                     │  3. abre as N melhores páginas              │
                     │  4. monta o dossiê ....... 15-50k chars     │
                     │  5. resume com as fontes (LLM)              │
       resumo   ◄──  │                          ....... ~700 tokens│
     ~700 tokens     └─────────────────────────────────────────────┘
```

Sem isso, uma pesquisa séria significa despejar dezenas de milhares de tokens
de HTML e texto extraído na conversa principal — material que fica lá
ocupando espaço em **todas** as chamadas seguintes, mesmo depois de já ter
sido usado. Aqui esse custo é pago num processo separado, com o LLM que você
escolher, e o que atravessa é só a resposta.

Na prática:

- **O contexto do agente não incha.** Ele gasta ~700 tokens por pesquisa em vez
  dos 6k-20k tokens do material lido. Conversas longas com muitas pesquisas
  continuam viáveis.
- **Dá para usar um modelo barato na parte cara.** Quem lê 5 páginas e resume
  pode ser um modelo local pequeno; o agente principal, o caro, só recebe o
  resultado pronto.
- **A resposta vem citada.** O resumo traz as URLs realmente consultadas e um
  carimbo de data/hora, então dá para conferir a fonte em vez de confiar.
- **Uma chamada resolve.** O `research_web` já busca vários ângulos por dentro
  e lê em paralelo — o agente não precisa orquestrar rodadas de busca.

Quando você quer o texto cru mesmo — "leia este link para mim" — é o `read_url`
que serve, e aí o conteúdo vai inteiro para o agente, sem resumo.

## Requisitos

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)
- Um servidor de busca [SearXNG](https://docs.searxng.org/) rodando (formato JSON habilitado) — há um `docker compose` pronto em [`searxng/`](#searxng-via-docker-compose)
- Um servidor de LLM com API compatível com OpenAI (`/v1/chat/completions` e `/v1/models`) — ex. [llama.cpp server](https://github.com/ggml-org/llama.cpp), vLLM, ou a própria OpenAI

### SearXNG via docker compose

O diretório `searxng/` traz a stack que eu uso, já na porta que é o default do
`SEARXNG_URL`:

```bash
cd searxng
docker compose up -d
```

Sobe SearXNG em `http://localhost:8886` mais um Valkey de cache. Na primeira
subida o SearXNG gera `searxng/data/settings.yml` (ignorado pelo git) com
`formats: [html]` — **o `research_web` não funciona assim**, porque o cliente
pede `format=json` (`util/searxng.py:32`) e o SearXNG responde 403. Habilite o
JSON e reinicie:

```yaml
# searxng/data/settings.yml
search:
  formats:
    - html
    - json
```

```bash
docker compose restart searxng
```

Os serviços `mcp-searxng` e `caddy` do compose são um servidor MCP de busca
de terceiros, independente deste projeto — este aqui fala com o SearXNG
direto, por HTTP. Se você não os usa, pode removê-los do compose sem afetar em
nada o `web-search-mcp`.

Antes de subir isso em qualquer lugar que não seja sua máquina, troque as duas
senhas do compose. Elas vêm com o placeholder `password123`, que está
versionado neste repositório e portanto é público:

- `SEARXNG_SECRET` (em `searxng/docker-compose.yaml`) — chave com que o SearXNG
  assina; deve ser um valor aleatório e longo, ex. `openssl rand -hex 32`
- o `Bearer password123` do `searxng/caddy/Caddyfile` — é a **única**
  autenticação na frente do `mcp-searxng`, cuja porta `8887` é publicada no
  host. Quem souber o token usa o serviço

## Instalação

### A partir do GitHub (não precisa clonar)

```bash
uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0 web-search-mcp
```

Registro no Claude Code:

```bash
claude mcp add web-search \
  -e SEARXNG_URL=http://localhost:8886 \
  -e MODEL_BASE_URL=http://localhost:8200/v1 \
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0 web-search-mcp
```

Instalado assim, o `.env` não é lido: a configuração inteira entra por `-e` —
ver [Configurando o servidor instalado](#configurando-o-servidor-instalado).
Trocar `@v0.1.0` por `@main` pega o topo do branch.

Para deixar o comando fixo no PATH em vez de resolver a cada execução:

```bash
uv tool install git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0
```

### A partir do clone (desenvolvimento)

```bash
uv sync
cp .env.example .env
```

Edite o `.env` com as URLs do seu SearXNG e do seu servidor de modelo. O
servidor sobe com os defaults do `.env.example` se você não copiar nada, mas
sem `SEARXNG_URL`/`MODEL_BASE_URL` corretos as tools não vão funcionar.

## Uso

Dois transportes, mesmas tools. A diferença é quem sobe o processo — e isso
muda de onde vem a configuração.

| | stdio | http |
|---|---|---|
| Quem sobe o processo | o cliente, a cada sessão | você, uma vez |
| Quantos clientes | um por processo | vários no mesmo processo |
| Porta de rede | nenhuma | `MCP_HOST:MCP_PORT` |
| Configuração vem de | bloco `env` do cliente (`-e`) e/ou `.env` | ambiente de quem subiu e/ou `.env` |

### stdio (padrão — para Claude Code e clientes locais)

```bash
uv run web-search-mcp
```

Na prática você não roda isso à mão: quem executa é o cliente. Registro no
Claude Code apontando para o clone:

```bash
claude mcp add web-search -- uv --directory /caminho/para/web_search_mcp run web-search-mcp
```

Para registrar a versão instalada do GitHub, com a configuração no bloco
`env`, ver [Configurando o servidor instalado](#configurando-o-servidor-instalado).

### HTTP (compartilhar entre vários agentes)

Suba o servidor. Do clone, que lê o `.env` da raiz:

```bash
uv run web-search-mcp --http
```

Ou instalado, passando a configuração pelo ambiente:

```bash
SEARXNG_URL=http://localhost:8886 \
MODEL_BASE_URL=http://localhost:8200/v1 \
uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0 web-search-mcp --http
```

Sobe em `http://{MCP_HOST}:{MCP_PORT}/mcp` (padrão `127.0.0.1:8765`).

Com ele no ar, registre o cliente pela URL:

```bash
claude mcp add --transport http web-search http://127.0.0.1:8765/mcp
```

Que no `.mcp.json` fica:

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

**No modo http o bloco `env` do cliente não tem efeito.** No stdio o cliente
spawna o processo, então o `-e` dele vira o ambiente do servidor; no http o
processo é seu e já está rodando quando o cliente conecta, com o ambiente que
*você* deu na hora de subir. Toda a configuração migra para o lado do
servidor, e trocar uma variável exige reiniciá-lo — editar o `.mcp.json` não
adianta.

#### Deixando rodando (systemd de usuário, sem sudo)

```ini
# ~/.config/systemd/user/web-search-mcp.service
[Unit]
Description=web-search-mcp (http)
After=network.target

[Service]
Environment=SEARXNG_URL=http://localhost:8886
Environment=MODEL_BASE_URL=http://localhost:8200/v1
ExecStart=%h/.local/bin/uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0 web-search-mcp --http
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now web-search-mcp
```

Antes de expor esse endpoint além da sua máquina, note que o servidor não tem
autenticação nenhuma. Com o default `MCP_HOST=127.0.0.1` só processos locais
alcançam, o que é seguro. Trocar para `0.0.0.0` para outra máquina consumir
deixa as duas tools abertas a qualquer um na rede, sem credencial e servindo de
proxy de scraping em cima do seu LLM. Nesse caso ponha um reverse proxy com
token na frente e restrinja `MCP_CORS_ALLOW_ORIGINS` às origens que você usa,
em vez de deixar `*`.

## Tools

### `read_url(url: str) -> str`

Abre uma URL específica e devolve o conteúdo principal da página em
Markdown, sem busca nem resumo — o texto bruto volta pro agente processar.
Bloqueia URLs que apontam para IP privado/loopback/link-local (proteção
anti-SSRF).

### `research_web(query: str, recent: bool = False) -> str`

Pesquisa a pergunta na web (gera variantes de busca, roda em paralelo, lê as
páginas mais relevantes) e devolve um resumo em português citando as fontes,
com carimbo de data/hora e a lista de URLs realmente consultadas.

Use `recent=True` só quando a resposta depende do dia de hoje (clima,
cotação, placar, notícia). Para fatos estáveis (história, biografia,
conceitos), deixe `recent=False` — filtrar por data descarta as melhores
fontes.

## Configuração

**Nenhuma variável é obrigatória.** Toda uma tem default no código, e o
servidor sobe sem configuração alguma. Você só declara o que desviar do
padrão — na prática, `SEARXNG_URL` e `MODEL_BASE_URL`, se os seus não
estiverem nas portas abaixo.

A lista completa, com o default de cada uma:

### Log

| Variável | Default | O que faz |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `ERROR`. `INFO` pega logs de info e de erro; `DEBUG` mostra cada busca, cada URL lida e o dossiê montado. Log sempre sai no stderr, nunca no stdout — um byte no stdout corromperia o protocolo stdio |

### Modelo (API compatível com OpenAI)

| Variável | Default | O que faz |
|---|---|---|
| `MODEL` | *(vazio)* | Nome do modelo a usar. **Vazio = usa o modelo default do servidor**, ou seja, o que já está carregado: o servidor consulta `GET /models` na primeira chamada e adota o modelo residente, sem forçar troca nem pagar reload de GPU. Preencha só para fixar um modelo específico, aceitando o reload se ele não for o carregado. Ver [Detecção de modelo](#detecção-de-modelo) |
| `MODEL_BASE_URL` | `http://localhost:8200/v1` | Base da API compatível com OpenAI, sem o `/chat/completions` no fim. Serve llama.cpp, vLLM, Ollama, OpenAI, o que for |
| `MODEL_API_KEY` | `not-needed` | Vai como `Authorization: Bearer <valor>`. Servidor local normalmente ignora; provider pago exige a chave real |
| `MODEL_TIMEOUT` | `120` | Timeout, em segundos, de cada chamada de LLM. Modelo grande em CPU pode precisar de mais |
| `MODEL_TEMPERATURE` | `0` | Temperatura das chamadas. `0` porque a tarefa é resumir fonte, não criar — temperatura alta aqui vira alucinação |
| `MODEL_CONTEXT_TOKENS` | `65536` | Janela de contexto do modelo. **Tem que bater com o `--ctx-size` que você subiu no servidor**: é daqui que sai o orçamento do dossiê. Declarar mais do que o servidor tem faz o provider recusar a chamada com HTTP 400 e a pesquisa inteira se perde, depois de já ter pago busca e scraping |
| `MODEL_RESERVE_TOKENS` | `4096` | Quanto da janela fica reservado para o que não é dossiê: instruções, pergunta e a resposta que o modelo ainda vai gerar. O orçamento do dossiê é `MODEL_CONTEXT_TOKENS - MODEL_RESERVE_TOKENS` |
| `EXTRA_BODY` | *(vazio)* | JSON cru mesclado no payload do `/chat/completions`, para parâmetro que só o seu provider entende. Ex. desligar reasoning no Qwen3: `EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`. JSON inválido derruba o servidor no boot, de propósito |
| `EXTRA_SYSTEM_PROMPT` | *(vazio)* | Texto apenso ao fim do system prompt em toda chamada. Existe porque nem todo modelo desliga reasoning por parâmetro de API — em alguns só obedece por instrução. Ex. `EXTRA_SYSTEM_PROMPT=Reasoning: low`. Vale a pena: num modelo que pensa por padrão, gerar 3 linhas de busca custou 2767 tokens / 39s sem, contra 271 / 3s com |

### SearXNG

| Variável | Default | O que faz |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8886` | Base da sua instância SearXNG. Precisa estar com o formato JSON habilitado |
| `SEARXNG_MAX_RESULTS` | `10` | Quantos resultados são pedidos por busca. O `research_web` faz várias buscas e junta, então isso é o teto por busca, não o total |
| `SEARXNG_TIMEOUT` | `10` | Timeout, em segundos, de cada consulta ao SearXNG |
| `SEARXNG_LANGUAGE` | `pt-BR` | Idioma passado na busca. Muda que fontes aparecem — para pesquisar em inglês, `en-US` |
| `SEARXNG_CATEGORIES` | `general,news` | Categorias do SearXNG, separadas por vírgula, repassadas cruas |

### Scraper

| Variável | Default | O que faz |
|---|---|---|
| `SCRAPER_TIMEOUT` | `6` | Timeout, em segundos, do download de cada página. Baixo de propósito: no `research_web` uma página lenta não vale segurar a pesquisa inteira, e há links de reserva para tomar o lugar dela |
| `SCRAPER_LIMIT` | *(vazio)* | Corte de caracteres do texto extraído **no `read_url`**. Vazio = página inteira. Não afeta o `research_web`, que usa o `RESEARCH_PAGE_CHARS` |

### Research

| Variável | Default | O que faz |
|---|---|---|
| `RESEARCH_PAGE_BUDGET` | `5` | Quantas páginas entram no dossiê de uma pesquisa, somando todas as buscas. É o principal botão de qualidade × latência |
| `RESEARCH_POOL_SIZE` | `20` | Reserva de links candidatos. Link morto, bloqueado ou sem texto não gasta vaga do orçamento: cede o lugar para o próximo da reserva |
| `RESEARCH_MAX_WAVES` | `4` | Teto de tentativas de leitura antes de desistir. Sem ele, uma sequência ruim de links varreria a reserva inteira e estouraria a latência |
| `RESEARCH_PAGE_CHARS` | `25000` | Teto de caracteres por página no dossiê do `research_web`. Existe para o outlier: uma única página gigante já rendeu 412k caracteres = 103k tokens contra 65k de contexto, e a pesquisa inteira se perdeu. Não aperte muito — dossiê pequeno demais piora o resumo |

### Servidor MCP

| Variável | Default | O que faz |
|---|---|---|
| `MCP_NAME` | `web-search` | Nome que o servidor anuncia no handshake MCP |
| `MCP_TRANSPORT` | `stdio` | Transporte usado quando você **não** passa `--http`. Ver [Uso](#uso) |
| `MCP_HOST` | `127.0.0.1` | Interface de escuta no modo `--http`. Ver o aviso abaixo antes de trocar |
| `MCP_PORT` | `8765` | Porta de escuta no modo `--http` |
| `MCP_CORS_ALLOW_ORIGINS` | `*` | Origens liberadas no CORS do modo `--http`, separadas por vírgula. Só importa para cliente de navegador (ex. MCP Inspector) |
| `TZ` | *(fuso do host)* | Fuso usado no carimbo de data do resumo. Conveniência para container, cujo padrão é UTC. Ex. `America/Sao_Paulo` |

### Juiz do eval

| Variável | Default | O que faz |
|---|---|---|
| `EVAL_JUDGE_MODEL` | *(mesmo do `MODEL`)* | Modelo usado como juiz no [eval](#eval). Apontar para um modelo diferente do que escreveu o resumo torna a avaliação bem menos complacente |

Sobre `MCP_HOST` e `MCP_CORS_ALLOW_ORIGINS`: o servidor não tem autenticação
nenhuma. Com os defaults ele só escuta em `127.0.0.1`, o que restringe o acesso
à sua máquina. Trocar `MCP_HOST` para `0.0.0.0` expõe as duas tools para
qualquer um na rede, sem credencial — o que permite usar seu servidor como
proxy de scraping e queimar seu LLM. Se precisar compartilhar na rede, ponha um
reverse proxy com autenticação na frente e restrinja
`MCP_CORS_ALLOW_ORIGINS` às origens que você de fato usa.

### De onde a configuração vem

Isto não depende do transporte: a leitura acontece no import do `config`, antes
de stdio ou http entrarem em cena. O que decide é **onde o `config.py` está**.

| Origem | Rodando do clone | Instalado (`uvx` / `uv tool install`) |
|---|---|---|
| `.env` na raiz do projeto | vale | **ignorado** |
| Variável de ambiente | vale, e sobrescreve o `.env` | único caminho |
| Default do código | fallback | fallback |

`load_dotenv()` procura o `.env` subindo a partir do diretório do
`config.py`. No clone isso chega na raiz do projeto e acha o arquivo; instalado,
o `config.py` mora no `site-packages` e a busca não encontra nada — nem mesmo um
`.env` que exista no diretório de onde o servidor foi executado. Instalado,
portanto, tudo entra por variável de ambiente.

De onde sai essa variável de ambiente, aí sim depende do transporte: no stdio,
do bloco `env` do cliente (`-e`), que é quem spawna o processo; no http, do
ambiente de quem subiu o processo — `export`, `systemd`, `compose`.

A precedência é `variável de ambiente > .env > default`, porque o
`load_dotenv()` roda sem `override`. Útil no clone para testar uma variação sem
editar arquivo:

```bash
MODEL_BASE_URL=http://localhost:8205/v1 uv run web-search-mcp --http
```

### Configurando o servidor instalado

Declare no bloco `env` do registro:

```bash
claude mcp add web-search \
  -e SEARXNG_URL=http://localhost:8886 \
  -e MODEL_BASE_URL=http://localhost:8200/v1 \
  -e MODEL_CONTEXT_TOKENS=65536 \
  -e TZ=America/Sao_Paulo \
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0 web-search-mcp
```

Note que `MODEL` não aparece: deixado de fora, o servidor usa o modelo já
carregado no seu `MODEL_BASE_URL`. Passe `-e MODEL=nome-do-modelo` só para
fixar um.

O comando acima grava isto no `.mcp.json` (escopo de projeto) ou no
`~/.claude.json` (escopo de usuário, com `-s user`):

```json
{
  "mcpServers": {
    "web-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/fabio-barboza/web_search_mcp@v0.1.0", "web-search-mcp"],
      "env": {
        "SEARXNG_URL": "http://localhost:8886",
        "MODEL_BASE_URL": "http://localhost:8200/v1"
      }
    }
  }
}
```

O que entra em `env` fica em texto puro nesse arquivo. Para `SEARXNG_URL` e
`MODEL_BASE_URL` isso não tem consequência, mas se o seu provedor de LLM exigir
uma `MODEL_API_KEY` real, ela vai parar no `.mcp.json` — que costuma ser
versionado quando está no escopo de projeto. Nesse caso registre com
`claude mcp add -s user`, que grava em `~/.claude.json`, fora do repositório.

### Detecção de modelo

Quando `MODEL` fica vazio, a primeira chamada de LLM consulta
`GET {MODEL_BASE_URL}/models` para achar o modelo já carregado, em vez de
forçar um específico (evita pagar reload de GPU a troco de nada). Funciona
nativamente com routers que expõem status de load (ex. llama.cpp/llama-swap);
em providers genéricos, cai no único modelo da lista ou pede pra você
preencher `MODEL` explicitamente se houver ambiguidade.

## Testes

```bash
uv run --group test pytest
```

Tudo determinístico — mocka rede e LLM, não precisa de SearXNG nem de modelo
rodando.

## Eval

```bash
uv run python -m evals.run
```

Roda um conjunto fixo de perguntas contra a web e o LLM reais, mede
faithfulness (afirmações do resumo suportadas pelo dossiê) e relevância, e
salva o resultado em `evals/results/`. Não é teste de CI — a web muda entre
execuções — serve para comparar rodadas e pegar alucinação escancarada, não
como avaliação independente (o juiz costuma ser o mesmo modelo que escreveu o
resumo).

## Estrutura

```
src/web_search_mcp/
  server.py      # FastMCP: tools, prompt, transporte, main()
  config.py      # única fonte de configuração (lê o .env inteiro)
  llm.py         # chat completion via requests, detecção de modelo
  tools/
    read_url.py  # tool: lê uma URL específica
    research.py  # tool: pesquisa, lê páginas, resume com fontes
  util/
    scraper.py   # download + extração de conteúdo principal (anti-SSRF)
    searxng.py   # cliente do SearXNG
tests/           # pytest, sem rede, sem LLM
evals/           # roda contra web/LLM reais, sob demanda
searxng/         # docker compose do SearXNG (dependência externa, opcional)
```

Layout `src/` de propósito: o pacote instalado ocupa um único namespace
(`web_search_mcp`), em vez de despejar `server`/`config`/`util` na raiz do
site-packages e colidir com outros pacotes.
