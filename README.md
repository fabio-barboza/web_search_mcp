# web-search-mcp

**Português** | [English](README.en.md)

Servidor [MCP](https://modelcontextprotocol.io) autônomo que pesquisa na web,
abre as páginas, lê o conteúdo e devolve **um resumo com as fontes** para o
agente que chamou. Três tools — `research_web`, `analyze_urls` e `read_url`
— para qualquer agente (Claude Code, LangChain, LangGraph, etc). Sem
framework de agente embutido: só [FastMCP](https://gofastmcp.com).

**Não é um MCP para o SearXNG.** A busca é própria e tem duas fontes:

- **Google via Tor, a principal.** O servidor consulta o Google (pelo
  endpoint do Google CSE) através de quatro canais Tor, alternando as
  queries entre eles. Quando o Google barra um canal, a query refaz em outro
  na hora e o barrado troca de IP em segundo plano.
- **SearXNG, a reserva.** Só entra quando o Google falha por todos os
  caminhos — ou sempre, se você preferir, com `SEARCH_BACKEND=searxng`.

A stack inteira (quatro containers Tor + SearXNG) vem pronta num
`docker compose` em `search-engine/`. Detalhes em
[De onde vêm os links](#de-onde-vêm-os-links-google-via-tor-searxng-de-reserva).

## A ideia: pesquisar num contexto isolado

A pesquisa inteira acontece **fora da janela de contexto do agente
principal**. Ele manda uma pergunta em linguagem natural e recebe de volta um
resumo curto — nunca vê o material bruto.

```
                     ┌─────────────────────────────────────────────┐
  agente principal   │  web-search-mcp (contexto próprio)          │
  ────────────────   │                                             │
                     │  1. gera variantes de busca (LLM)           │
  "quem foi X?"  ──► │  2. busca em paralelo (Google via Tor;      │
                     │     SearXNG de reserva)                     │
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

O `analyze_urls` aplica a mesma ideia a links que o usuário já tem: "resuma
estes três artigos", "compare as duas propostas" — lê as páginas aqui dentro
e devolve só a análise. Quando você quer o texto cru mesmo — "leia este link
para mim" — é o `read_url` que serve, e aí o conteúdo vai inteiro para o
agente, sem resumo.

## De onde vêm os links: Google via Tor, SearXNG de reserva

O `research_web` busca no Google pelo endpoint do Google CSE (o mesmo que o
widget de busca embutida usa), através de quatro canais Tor. O SearXNG
continua na stack, mas como reserva.

**Por quê.** Um SearXNG raspa os buscadores e, no uso normal, toma CAPTCHA e
limite de taxa: medido em 29/08/2026 e de novo em 10/09/2026, 13 de 15
motores suspensos. O que sobra devolve casamento de nome de marca — duas
perguntas técnicas diferentes chegaram a voltar a mesma lista de homepages.
No A/B de 10/09/2026 (`evals/search_ab.py`, 12 perguntas de assuntos sem
relação entre si, mesmas variantes de busca nos dois lados):

| Fonte | Pesquisas vazias | Resultados no assunto | Páginas lidas | Busca p50 |
|---|---|---|---|---|
| SearXNG | 2 de 12 | 112 / 125 | 45 | 2,7 s |
| Google CSE via Tor | 0 de 12 | 228 / 230 | 71 | 4,6 s |

Em carga contínua (20 pesquisas seguidas, 80 consultas) 100% saíram pelo
Tor, com 5 failovers entre canais e nenhuma queda para o SearXNG; a busca
subiu para p50 9,7 s. Isso foi medido com **dois** canais — os quatro atuais
existem para repartir essa carga, e ainda não foram medidos.

**Como uma query anda.** Nenhum passo espera:

1. o canal da vez no rodízio;
2. barrado pelo Google → esse canal troca de circuito em segundo plano
   (credencial SOCKS nova + `NEWNYM`) e a query refaz **na hora** em outro canal;
3. barrado de novo → um terceiro canal (com só dois, volta ao primeiro, já
   com circuito novo);
4. o CSE pelo IP da própria máquina (`GOOGLE_CSE_DIRECT_FALLBACK`);
5. o SearXNG (`SEARXNG_FALLBACK`).

Quando a query chega ao SearXNG e a maioria dos motores dele está suspensa,
a resposta começa com um **aviso de busca degradada**, dizendo ao agente que
a cobertura está incompleta por causa da infraestrutura — não porque o
assunto não existe — e que repetir a pesquisa não vai adiantar. Sem esse
aviso o agente reformula a pergunta em loop.

Só a busca passa pelo Tor. As páginas são abertas direto, desta máquina.
`SEARCH_BACKEND=searxng` desliga o Google e volta ao SearXNG puro — rollback
é trocar a variável e reiniciar.

Ressalvas:

- **Não é API documentada.** Se o Google mudar o formato, o parse falha
  alto e a busca cai no SearXNG em vez de quebrar.
- **O CX default é público e de terceiro** (blackle.com, o mesmo que o motor
  `google cse` do SearXNG usa). Pode sumir; `GOOGLE_CSE_CX` troca sem mexer
  no código.
- **O fallback direto não é garantido.** O IP de casa também tomou 429 do
  CSE no dia da medição.
- **Termos de uso.** Consulta automatizada ao Google, ainda mais via Tor,
  contraria os termos do Google. Avalie antes de usar; com
  `SEARCH_BACKEND=searxng` nada disso acontece.

Medições, decisões e riscos em [`plans/tor.md`](plans/tor.md).

## Requisitos

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)
- A stack de busca: quatro containers Tor (a fonte de links é o Google CSE, consultado por eles) e um [SearXNG](https://docs.searxng.org/) de reserva, com formato JSON habilitado — tudo num `docker compose` pronto em [`search-engine/`](#infraestrutura-de-busca-via-docker-compose). Sem os Tor a busca ainda funciona (CSE direto, depois SearXNG); `SEARCH_BACKEND=searxng` usa só o SearXNG
- Um servidor de LLM com API compatível com OpenAI (`/v1/chat/completions` e `/v1/models`) — ex. [llama.cpp server](https://github.com/ggml-org/llama.cpp), vLLM, ou a própria OpenAI

### Infraestrutura de busca via docker compose

`search-engine/` traz a stack de busca inteira, já nas portas que são o
default do `.env.example`. Numa cópia nova do repo:

```bash
cp .env.example .env          # troque TOR_CONTROL_PASSWORD
cd search-engine
docker compose up -d --wait
```

A primeira subida compila a imagem Tor localmente (`search-engine/tor/`:
Alpine com o pacote `tor` fixado em `0.4.9.12-r0`), então demora mais e
precisa de internet. Se o Alpine tirar essa versão do repositório, a
compilação falha — atualize o número no `Dockerfile`. O `--wait` segura o
comando até os canais Tor ficarem `healthy` (o bootstrap leva até ~60 s);
sem ele, as pesquisas desse intervalo caem no CSE direto e no SearXNG.

Sobe:

| Serviço | Porta (host) | Papel |
|---|---|---|
| `searxng` + `valkey` | `8886` | SearXNG com `search-engine/searxng/settings.yml` montado por cima (JSON habilitado e curadoria de fontes já prontos) |
| `tor-a` | `127.0.0.1:9060` SOCKS, `9061` controle | canal de busca via Tor ([`plans/tor.md`](plans/tor.md)) |
| `tor-b` | `127.0.0.1:9070` SOCKS, `9071` controle | segundo canal, independente do primeiro |
| `tor-c` | `127.0.0.1:9080` SOCKS, `9081` controle | terceiro canal |
| `tor-d` | `127.0.0.1:9090` SOCKS, `9091` controle | quarto canal |
| `mcp-searxng` + `caddy` | `8887` | servidor MCP de busca de terceiros, independente deste projeto (ver abaixo) |

Os containers Tor leem o **mesmo `.env` da raiz** que configura o MCP
(`env_file: ../.env`), então a senha do ControlPort é uma só e não há como os
dois lados divergirem. Sem o `.env` o compose falha na hora. As portas Tor só
escutam em `127.0.0.1`: publicar em `0.0.0.0` transformaria a máquina num
proxy aberto. Dentro do container há uma segunda barreira: o `torrc` só
aceita SOCKS vindo de faixas privadas (`SocksPolicy`), e o ControlPort exige
a senha.

Os serviços `mcp-searxng` e `caddy` são um servidor MCP de busca de terceiros
— este projeto fala com o SearXNG direto, por HTTP. Se você não os usa, pode
removê-los do compose sem afetar em nada o `web-search-mcp`.

Antes de subir isso em qualquer lugar que não seja sua máquina, troque as
senhas. Os placeholders estão versionados neste repositório e portanto são
públicos:

- `TOR_CONTROL_PASSWORD` (no `.env`) — quem tiver a senha controla o tor
- `SEARXNG_SECRET` (em `search-engine/docker-compose.yaml`) — chave com que o
  SearXNG assina; deve ser um valor aleatório e longo, ex. `openssl rand -hex 32`
- o `Bearer password123` do `search-engine/searxng/caddy/Caddyfile` — é a
  **única** autenticação na frente do `mcp-searxng`, cuja porta `8887` é
  publicada no host. Quem souber o token usa o serviço

### Tudo de uma vez: `start.sh`

```bash
./start.sh
```

Cria o `.env` a partir do `.env.example` se ele não existir, lembra quais
variáveis conferir (`MODEL_BASE_URL`, `MODEL_API_KEY`, `TOR_CONTROL_PASSWORD`,
`MCP_HOST`/`MCP_PORT`) e espera um Enter. Depois confere se as portas e os
nomes de container estão livres, sobe o `search-engine/` e inicia o MCP em
streamable-http. No fim mostra a URL para plugar o cliente
(`http://127.0.0.1:8765/mcp` por padrão). **Ctrl+C derruba tudo**: o MCP e os
containers (`docker compose down`; volumes preservados). Se o MCP cair
sozinho, a stack cai junto. Argumentos extras vão para o `web-search-mcp`.

O LLM continua externo: se `MODEL_BASE_URL` não responder, o script avisa e
sobe assim mesmo — `read_url` funciona sem ele, `research_web` e
`analyze_urls` não.

## Instalação

### A partir do GitHub (não precisa clonar)

```bash
uvx --from git+https://github.com/fabio-barboza/web_search_mcp web-search-mcp
```

Registro no Claude Code:

```bash
claude mcp add web-search \
  -e SEARXNG_URL=http://localhost:8886 \
  -e MODEL_BASE_URL=http://localhost:8200/v1 \
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp web-search-mcp
```

Instalado assim, o `.env` não é lido: a configuração inteira entra por `-e` —
ver [Configurando o servidor instalado](#configurando-o-servidor-instalado).
Sem `@ref` na URL, instala o topo do branch `main`. Para fixar uma versão,
acrescente `@<branch, tag ou commit>` depois do nome do repositório.

Para deixar o comando fixo no PATH em vez de resolver a cada execução:

```bash
uv tool install git+https://github.com/fabio-barboza/web_search_mcp
```

### A partir do clone (desenvolvimento)

```bash
uv sync
cp .env.example .env
```

Edite o `.env`: `MODEL_BASE_URL` do seu servidor de modelo e
`TOR_CONTROL_PASSWORD` (os containers Tor leem este mesmo arquivo). Com a
stack do `search-engine/` nas portas default, `SEARXNG_URL` e `TOR_CHANNELS`
já batem. O servidor sobe com os defaults se você não copiar nada, mas sem
LLM em `MODEL_BASE_URL` o `research_web` e o `analyze_urls` não funcionam, e
sem busca nenhuma no ar (Tor, CSE direto e SearXNG) o `research_web` não
acha links — o `read_url` funciona sozinho.

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
uvx --from git+https://github.com/fabio-barboza/web_search_mcp web-search-mcp --http
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
Environment=MODEL_BASE_URL=http://localhost:8200/v1
# Busca: Google CSE pelos quatro canais Tor do search-engine/, SearXNG de reserva
Environment=SEARCH_BACKEND=google_tor
Environment=TOR_CHANNELS=127.0.0.1:9060:9061,127.0.0.1:9070:9071,127.0.0.1:9080:9081,127.0.0.1:9090:9091
Environment=SEARXNG_URL=http://localhost:8886
# TOR_CONTROL_PASSWORD fica fora da unit, num arquivo só seu (chmod 600)
EnvironmentFile=%h/.config/web-search-mcp/secrets.env
ExecStart=%h/.local/bin/uvx --from git+https://github.com/fabio-barboza/web_search_mcp web-search-mcp --http
Restart=on-failure

[Install]
WantedBy=default.target
```

A senha do ControlPort é a mesma do `.env` que os containers Tor leem.
Ela não vai na unit porque `systemctl --user cat`/`show` exibem cada
`Environment=` para quem olhar:

```bash
mkdir -p ~/.config/web-search-mcp
install -m 600 /dev/null ~/.config/web-search-mcp/secrets.env
echo 'TOR_CONTROL_PASSWORD=a-mesma-do-.env-do-search-engine' > ~/.config/web-search-mcp/secrets.env
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now web-search-mcp
```

A unit não depende dos containers: o `search-engine/` sobe sozinho no boot
(`restart: unless-stopped`), e se os canais Tor ainda não estiverem no ar
quando a primeira pesquisa chegar, ela cai no CSE direto e depois no
SearXNG em vez de falhar. As linhas `SEARCH_BACKEND` e `TOR_CHANNELS` repetem
os defaults — estão ali para ficar explícito o que mudar se os canais
estiverem em outro host ou em outras portas. Rodando do clone
(`WorkingDirectory=` apontando para ele e `uv run web-search-mcp --http`), o
`.env` da raiz é lido e nenhuma dessas linhas é necessária.

Antes de expor esse endpoint além da sua máquina, note que o servidor não tem
autenticação nenhuma. Com o default `MCP_HOST=127.0.0.1` só processos locais
alcançam, o que é seguro. Trocar para `0.0.0.0` para outra máquina consumir
deixa as tools abertas a qualquer um na rede, sem credencial e servindo de
proxy de scraping em cima do seu LLM. Nesse caso ponha um reverse proxy com
token na frente e restrinja `MCP_CORS_ALLOW_ORIGINS` às origens que você usa,
em vez de deixar `*`.

## Tools

### `research_web(query: str, recent: bool = False) -> str`

Pesquisa a pergunta na web (gera variantes de busca, roda em paralelo, lê as
páginas mais relevantes) e devolve um resumo em português, com carimbo de
data/hora. Cada fato termina com um **link markdown para a página de onde
saiu**, montado em código a partir da URL realmente lida — não pedido ao
modelo. Marcador `[n]` que o modelo inventar para uma fonte que não existe é
apagado, em vez de ficar apontando para o nada. No fim vai a lista numerada
das URLs consultadas.

Use `recent=True` só quando a resposta depende do dia de hoje (clima,
cotação, placar, notícia). Para fatos estáveis (história, biografia,
conceitos), deixe `recent=False` — filtrar por data descarta as melhores
fontes.

A mesma pergunta reescrita logo em seguida (mesmo conjunto de palavras de
conteúdo, em qualquer ordem) devolve o resultado anterior em vez de buscar
de novo: a tool já busca vários ângulos por dentro, e repetir só relê as
mesmas páginas. Busca degradada vem com o aviso descrito em
[De onde vêm os links](#de-onde-vêm-os-links-google-via-tor-searxng-de-reserva).

### `analyze_urls(urls: list[str], request: str = "Resuma o conteúdo.") -> str`

Lê de 1 a 8 URLs fornecidas e devolve só a análise pedida em linguagem
natural — resumo, parecer técnico, opinião, comparação —, feita pelo LLM
deste servidor. O texto das páginas não entra no contexto do agente. O
orçamento de caracteres do dossiê é repartido entre as páginas, para que
todas caibam juntas. A resposta lista as URLs analisadas e as que falharam.

### `read_url(url: str) -> str`

Abre uma URL específica e devolve o conteúdo principal da página em
Markdown, inteiro, sem busca nem resumo — o texto bruto volta pro agente
processar. Vem com um cabeçalho `Fonte desta página` e um link markdown
pronto, para o agente citar o que leu com endereço.

Nas duas tools que leem URLs, se o servidor redirecionar para outro
endereço (ex. uma página que não existe mais caindo na home), a resposta
avisa e usa a URL final. Sem esse aviso o agente lê a página errada sem
saber e tenta de novo em loop. As duas bloqueiam URLs que apontam para IP
privado/loopback/link-local (proteção anti-SSRF).

### Prompt `pesquisador`

O servidor também expõe um prompt MCP com a política de uso: pesquisar
antes de responder o que não se sabe com certeza, uma chamada por pergunta,
responder só com base no resumo e manter os links e as datas.

## Configuração

**Nenhuma variável é obrigatória.** Toda uma tem default no código, e o
servidor sobe sem configuração alguma. Você só declara o que desviar do
padrão — na prática, `MODEL_BASE_URL`, se o seu não estiver na porta
abaixo, e `TOR_CONTROL_PASSWORD`, que os containers Tor exigem.

A lista completa, com o default de cada uma:

### Log

| Variável | Default | O que faz |
|---|---|---|
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `ERROR`. `INFO` pega logs de info e de erro; `DEBUG` mostra cada busca, cada URL lida e o dossiê montado. Log sempre sai no stderr, nunca no stdout — um byte no stdout corromperia o protocolo stdio |

### Modelo (API compatível com OpenAI)

| Variável | Default | O que faz                                                                                                                                                                                                                                                                                                                                                                    |
|---|---|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `MODEL` | *(vazio)* | Nome do modelo a usar. **Vazio = usa o modelo default do servidor**, ou seja, o que já está carregado: o servidor consulta `GET /models` a cada chamada e adota o modelo residente, sem forçar troca nem pagar reload de GPU. Preencha só para fixar um modelo específico, aceitando o reload se ele não for o carregado. Ver [Detecção de modelo](#detecção-de-modelo) |
| `MODEL_BASE_URL` | `http://localhost:8200/v1` | Base da API compatível com OpenAI, sem o `/chat/completions` no fim. Serve llama.cpp, vLLM, Ollama, OpenAI, o que for                                                                                                                                                                                                                                                        |
| `MODEL_API_KEY` | `not-needed` | Vai como `Authorization: Bearer <valor>`. Servidor local normalmente ignora; provider pago exige a chave real                                                                                                                                                                                                                                                                |
| `MODEL_TIMEOUT` | `120` | Timeout, em segundos, de cada chamada de LLM. Modelo grande em CPU pode precisar de mais                                                                                                                                                                                                                                                                                     |
| `MODEL_TEMPERATURE` | `0` | Temperatura das chamadas. `0` porque a tarefa é resumir fonte, não criar — temperatura alta aqui vira alucinação                                                                                                                                                                                                                                                             |
| `MODEL_CONTEXT_TOKENS` | `65536` | Janela de contexto do modelo, de onde sai o orçamento do dossiê. Com llama.cpp/llama-swap o servidor lê o `--ctx-size` real do modelo carregado em `GET /models` a cada pesquisa, e esse valor ganha; esta variável é o fallback para providers que não expõem isso. Nesses, **tem que bater com a janela real**: declarar mais faz o provider recusar a chamada com HTTP 400 e a pesquisa inteira se perde, depois de já ter pago busca e scraping |
| `MODEL_RESERVE_TOKENS` | `4096` | Quanto da janela fica reservado para o que não é dossiê: instruções, pergunta e a resposta que o modelo ainda vai gerar. O orçamento do dossiê é `MODEL_CONTEXT_TOKENS - MODEL_RESERVE_TOKENS`                                                                                                                                                                               |
| `EXTRA_BODY` | *(vazio)* | JSON cru mesclado no payload do `/chat/completions`, para parâmetro que só o seu provider entende. Ex. desligar reasoning no Qwen3: `EXTRA_BODY={"chat_template_kwargs": {"enable_thinking": false}}`. JSON inválido derruba o servidor no boot, de propósito                                                                                                                |
| `EXTRA_SYSTEM_PROMPT` | *(vazio)* | Texto apenso ao fim do system prompt em toda chamada. Existe porque nem todo modelo desliga reasoning por parâmetro de API — em alguns só obedece por instrução. Ex. `EXTRA_SYSTEM_PROMPT=Reasoning strength: low`. Vale a pena: num modelo que pensa por padrão, gerar 3 linhas de busca custou 2767 tokens / 39s sem, contra 271 / 3s com                                   |

### Busca (Google CSE via Tor)

O caminho de cada query está em
[De onde vêm os links](#de-onde-vêm-os-links-google-via-tor-searxng-de-reserva).

| Variável | Default | O que faz |
|---|---|---|
| `SEARCH_BACKEND` | `google_tor` | `google_tor` (Google CSE pelos canais Tor, com CSE direto e SearXNG de reserva) ou `searxng` (só o SearXNG, como antes). Rollback é trocar e reiniciar |
| `TOR_CHANNELS` | `127.0.0.1:9060:9061,127.0.0.1:9070:9071,127.0.0.1:9080:9081,127.0.0.1:9090:9091` | Um canal por item, `host:porta_socks:porta_controle`, separados por vírgula. O default bate com os `tor-a`..`tor-d` do `search-engine/`. Aceita qualquer quantidade; com menos de três, o failover repete canal (já com circuito novo) |
| `TOR_CONTROL_PASSWORD` | *(vazio)* | Senha do ControlPort, para o `NEWNYM`. Os containers Tor não sobem sem ela; o MCP sim — vazia só desliga o `NEWNYM`, e a troca de circuito pela credencial SOCKS continua funcionando |
| `GOOGLE_CSE_CX` | CX público do blackle.com | Qual CSE consultar. Troque por um CX seu sem mexer no código |
| `GOOGLE_CSE_HL` | `pt-BR` | Idioma da interface do CSE. Não restringe o idioma dos resultados |
| `GOOGLE_CSE_TIMEOUT` | `15` | Timeout, em segundos, de cada requisição ao CSE (via Tor leva 2-4 s) |
| `GOOGLE_CSE_DIRECT_FALLBACK` | `true` | Tentar o CSE pelo IP da máquina quando os canais Tor falham. Não conte com ele: o IP de casa também toma 429 |
| `SEARXNG_FALLBACK` | `true` | Cair no SearXNG quando o Google falhou por todos os caminhos. Com `false`, a pesquisa devolve erro de busca nesse caso |

### SearXNG (reserva)

Usado quando o Google falhou por todos os caminhos, ou sempre, com
`SEARCH_BACKEND=searxng`. Só o `SEARXNG_MAX_RESULTS` vale também para o
Google.

| Variável | Default | O que faz |
|---|---|---|
| `SEARXNG_URL` | `http://localhost:8886` | Base da sua instância SearXNG. Precisa estar com o formato JSON habilitado |
| `SEARXNG_MAX_RESULTS` | `10` | Quantos resultados de cada busca entram na mescla, qualquer que seja a fonte (o Google devolve 20 por consulta; o que passa disso fica de fora). O `research_web` faz várias buscas e junta, então isso é o teto por busca, não o total |
| `SEARXNG_TIMEOUT` | `10` | Timeout, em segundos, de cada consulta ao SearXNG |
| `SEARXNG_LANGUAGE` | `auto` | Idioma passado na busca. `auto` deixa o SearXNG detectar pela query (pergunta em inglês busca página em inglês); fixar `pt-BR` empurra toda busca para página brasileira |
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
| `RESEARCH_MAX_PER_DOMAIN` | `2` | Máximo de URLs do mesmo domínio na reserva de candidatos. Sem teto, uma busca cujo top-10 é todo de um site enche o dossiê com um veículo só. `0` = sem limite |
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
à sua máquina. Trocar `MCP_HOST` para `0.0.0.0` expõe as tools para
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
  -- uvx --from git+https://github.com/fabio-barboza/web_search_mcp web-search-mcp
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
      "args": ["--from", "git+https://github.com/fabio-barboza/web_search_mcp", "web-search-mcp"],
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

A busca via Tor não pede nada no bloco `env` se a stack do
`search-engine/` estiver na mesma máquina: o default de `TOR_CHANNELS` já
aponta para os quatro canais em `127.0.0.1`. O `TOR_CONTROL_PASSWORD` é
opcional para o MCP — sem ele só o `NEWNYM` desliga — e, se você passar por
`-e`, vale a mesma ressalva da `MODEL_API_KEY`: fica em texto puro no
arquivo.

### Detecção de modelo

Quando `MODEL` fica vazio, cada chamada de LLM consulta
`GET {MODEL_BASE_URL}/models` para achar o modelo já carregado, em vez de
forçar um específico (evita pagar reload de GPU a troco de nada). Não há
cache: se outro cliente trocou o modelo no servidor, a próxima chamada já
usa o novo. Funciona nativamente com routers que expõem status de load (ex.
llama.cpp/llama-swap); em providers genéricos, cai no único modelo da lista
ou pede pra você preencher `MODEL` explicitamente se houver ambiguidade.

A mesma consulta traz, nesses routers, os argumentos com que o modelo subiu:
o `--ctx-size` dali define o orçamento do dossiê, no lugar do
`MODEL_CONTEXT_TOKENS`.

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

```bash
uv run python -m evals.search_ab [--load 20] [--hl pt-BR en]
```

A/B da fonte de links: SearXNG × Google CSE via Tor na mesma sessão, com as
mesmas variantes de busca nos dois lados, seguido de uma carga contínua que
mede taxa de bloqueio por canal, failovers e quedas para o CSE direto e o
SearXNG. Precisa dos canais Tor e do SearXNG no ar. Foi daqui que saíram os
números de [De onde vêm os links](#de-onde-vêm-os-links-google-via-tor-searxng-de-reserva).

## Estrutura

```
src/web_search_mcp/
  server.py         # FastMCP: tools, prompt, transporte, main()
  config.py         # única fonte de configuração (lê o .env inteiro)
  llm.py            # chat completion via requests, detecção de modelo e de contexto
  tools/
    research.py     # tool: pesquisa, lê páginas, resume com fontes
    analyze.py      # tool: lê URLs fornecidas e devolve só a análise
    read_url.py     # tool: lê uma URL específica
  util/
    search_chain.py # fonte de links: Google via Tor, SearXNG de reserva
    google_cse.py   # cliente do Google CSE, com failover entre canais
    tor.py          # canais Tor: rodízio e troca de circuito
    searxng.py      # cliente do SearXNG
    scraper.py      # download (curl_cffi, impressão TLS do Chrome) + extração (anti-SSRF)
tests/              # pytest, sem rede, sem LLM
evals/              # roda contra web/LLM reais, sob demanda
search-engine/      # docker compose da busca: SearXNG + canais Tor (dependência externa)
plans/              # planos de mudança aprovados
start.sh            # sobe search-engine/ + MCP em --http; Ctrl+C derruba tudo
```

Layout `src/` de propósito: o pacote instalado ocupa um único namespace
(`web_search_mcp`), em vez de despejar `server`/`config`/`util` na raiz do
site-packages e colidir com outros pacotes.
