# Plano: busca Google via Tor (dois canais) com fallback

Status (10/09/2026): aprovado. **Etapas 1-4 e 6 FEITAS**; etapa 5 (eval)
rodada, números na seção 6.1. Próxima: etapa 7 (commit/deploy, Fabio).
Decisões do Fabio na seção 2, dados medidos na seção 1.

Implementação (etapas 2-4), o que difere do texto original do plano:
- `util/tor.py`, `util/google_cse.py`, `util/search_chain.py`
  (`build_search()` escolhe pelo `SEARCH_BACKEND`); `requests[socks]` no
  `pyproject.toml`. Testes: `tests/test_tor.py`, `test_google_cse.py`,
  `test_search_chain.py`; suíte inteira verde com `SEARCH_BACKEND` nos dois
  valores (198).
- **403 no JSON não é bloqueio de IP** (medido): token inválido ou vazio
  volta HTTP 200 + `error.code 403` "Unauthorized access to internal API".
  O código renova o token e tenta de novo UMA vez no mesmo caminho; só
  então conta como falha e troca de canal. 429 no JSON = bloqueio.
- "unusual traffic" NÃO é procurado no corpo da resposta: um snippet de
  resultado que fale disso viraria failover (dependência do assunto). Só o
  campo `error` do JSON decide; resposta fora do formato (página /sorry/)
  é falha do caminho.
- Rodízio por contador no `ChannelPool`, não pelo índice da query: o
  `_collect_links` não mudou de assinatura e duas buscas simultâneas nunca
  saem do mesmo canal enquanto houver outro saudável.
- **IP de casa toma 429 no CSE hoje** (medido às 16h, 10/09/2026; no
  começo do dia passava). O fallback direto existe, mas não dá para contar
  com ele — o motor google cse do SearXNG usa o mesmo IP.
- Eval da etapa 5: `evals/search_ab.py` (novo).
- Docs (etapa 6): CLAUDE.md (Architecture + seção "Search source"), README
  (diagrama, requisitos, tabela "Busca (Google CSE via Tor)" — a tabela
  NÃO existia, o README só tinha a de serviços), `.env.example` conferido.

Estado ao encerrar a sessão de planejamento:
- `/home/fabio/services/tor`: `tor-a` e `tor-b` rodando, `healthy`,
  `restart: unless-stopped`. Aceite da seção 3 inteiro OK (exits distintos
  do IP de casa, credencial nova = circuito novo, NEWNYM com senha 250 OK,
  senha errada 515, Google CSE 20 resultados pelos dois canais, portas só em
  127.0.0.1, volta sozinho após `docker restart`). Não há README.md lá.
- Repo: `search-engine/` (compose + `searxng/` + `tor/`). Fluxo de cópia
  nova testado do zero: `cp .env.example .env` + `docker compose up -d` →
  6 containers, SearXNG JSON OK, 2 canais Tor OK; sem `.env` o compose
  recusa subir.
- `.env` de produção (`/home/fabio/services/web_search_mcp/.env`) já tem a
  seção Tor com a senha real. Inerte até o código existir.
- **PRODUÇÃO JÁ RODA O CÓDIGO TOR** (deploy às 16:45 de 10/09/2026, a
  pedido do Fabio, aceitando os 9,7 s da seção 6.1). O checkout de
  `/home/fabio/services/web_search_mcp` (branch `main`) tem copiados à mão
  do `feature/tor`: `src/` inteiro (idêntico), `pyproject.toml` e
  `uv.lock`; `uv sync` feito (PySocks). Backup do estado anterior no
  scratchpad da sessão (temporário). Verificado pelo cliente MCP na 8775:
  Go GC e queijo canastra, 6 páginas cada, todas as buscas via tor-a/tor-b,
  failover 429 tor-b → tor-a observado ao vivo. Antes do próximo
  `git pull` lá: `git checkout -- src/ pyproject.toml uv.lock` e
  `rm src/web_search_mcp/util/{tor,google_cse,search_chain}.py` (vêm no
  commit), senão o pull conflita.
- `.env` de desenvolvimento (raiz do repo, gitignored) também tem a seção
  Tor com a senha real, apontando para os containers live — é dele que o
  eval da etapa 5 lê.
- `start.sh` na raiz: cria o `.env`, sobe `search-engine/` e o MCP em
  `--http`, Ctrl+C derruba tudo. Testado (conflito de porta, cliente MCP
  plugando, Ctrl+C, MCP caindo sozinho). **Não usar nesta máquina**: a
  produção ocupa as mesmas portas e ele se recusa a subir (correto). Depois
  da integração ele tem que continuar funcionando sem mudança — o
  `docker compose up --wait` já espera os Tor ficarem `healthy`.
- `.env.example` e README já descrevem as variáveis Tor da seção 4.2: na
  etapa 6 é conferir, não reescrever.
- Nada do código do MCP para o Tor foi escrito ainda (seção 4).

## 0. Objetivo

Trocar a fonte de links do `research_web`: sai o SearXNG (13 de 15 motores
suspensos por CAPTCHA/rate-limit no uso normal), entra o Google, sempre, via
Tor. Dois containers Tor independentes funcionam como dois canais: quando um
é barrado, a query refaz no outro NA HORA, enquanto o barrado troca de IP em
segundo plano. Abrir, ler e resumir as páginas continua exatamente como hoje.

Isto é infraestrutura de busca — permitido pela regra MANDATORY do
CLAUDE.md. Nada aqui pode depender do assunto da pergunta: nenhum ramo,
limite ou prompt por tema.

## 1. O que foi medido (10/09/2026) e o que isso decide

| Teste | Resultado | Consequência |
|---|---|---|
| `google.com/search` via Tor, 12 circuitos | 9 bloqueados (429 `/sorry/`), 3 parede de JS, **0 com resultado** | Descartado |
| `google.com/search` pelo IP de casa (curl) | parede de JS | Google comum exige JavaScript: morto até sem Tor |
| Endpoint Google CSE (`cse.google.com/cse/element/v1`) via Tor, 12 circuitos, 12 assuntos sem relação entre si | **10 OK** com 20 resultados relevantes cada; 2 com 429, ambos da faixa `109.70.100.x`, e o circuito seguinte passou | **Viável: é por aqui** |
| Mesmo endpoint, IP de casa | OK, 20 resultados | Serve de fallback |
| Motor `google` do SearXNG | nenhum resultado; só o `google cse` responde | "Sempre Google" = CSE |

Detalhes do endpoint (do `searx/engines/google_cse.py`, que já funciona):
- token: `GET https://www.google.com/cse/cse.js?cx=<CX>` (segue redirect 301);
  o JSON entre o último `({` e o último `});` traz `cse_token`,
  `cselibVersion` e `exp`. O SearXNG guarda o token por 1 h.
- busca: `GET https://cse.google.com/cse/element/v1?rsz=filtered_cse&num=20
  &hl=..&cselibv=..&cx=..&q=..&safe=off&cse_tok=..&callback=_&rurl=
  [&exp=..][&sort=date:r:AAAAMMDD:AAAAMMDD][&start=N]`, com header
  `Referer: https://cse.google.com/`.
- resposta: JSONP `_({...})`; campos por resultado: `unescapedUrl`,
  `titleNoFormatting`, `contentNoFormatting`. Bloqueio vem como HTTP 200 com
  `{"error": {"code": 429, "message": "Our systems have detected unusual
  traffic..."}}` — **não** só como status HTTP.
- CX atual: `partner-pub-8993703457585266:4862972284` (público, do
  blackle.com — é o mesmo que o SearXNG usa).

Tor de sistema já existente: `tor@default`, SOCKS `127.0.0.1:9050`, sem
ControlPort, 0 conexões ativas, instalado junto com o `torbrowser-launcher`
em 31/03/2026. **Fica como está** e fora do caminho.

## 2. Decisões

| Pergunta | Decisão |
|---|---|
| Onde o Tor entra | No MCP. O MCP chama o CSE pelo SOCKS dos containers. (SearXNG via proxy suspenderia o motor no 1º 429 em vez de trocar de IP.) |
| Distribuição com os dois saudáveis | Queries da pesquisa alternadas entre A e B, em paralelo. |
| Canal barrado | A query refaz no OUTRO canal imediatamente; o barrado troca de IP em segundo plano e volta ao rodízio. Ninguém espera. |
| Os dois barrados ao mesmo tempo | 1º CSE pelo IP de casa; se falhar, SearXNG atual (com o aviso de busca degradada). |
| Abertura das páginas | Direto, sem Tor, como hoje. |
| Tor de sistema | Deixar como está. |

## 3. Serviço Tor — `/home/fabio/services/tor`

Deploy fora do repo, como o SearXNG. O repo tem o espelho:
`search-engine/tor/` (Dockerfile, torrc, entrypoint.sh) e os serviços
`tor-a`/`tor-b` no `search-engine/docker-compose.yaml`, que também sobe o
SearXNG. Os containers leem o `.env` da raiz (`env_file: ../.env`), o mesmo
do MCP, então a senha do ControlPort é uma só: numa cópia nova basta
`cp .env.example .env` e `cd search-engine && docker compose up -d`.
Mudança num lado se espelha no outro. Não subir o compose do repo nesta
máquina junto com os de `/home/fabio/services`: nomes e portas colidem.

Arquivos do deploy:

```
/home/fabio/services/tor/
├── docker-compose.yaml
├── Dockerfile
├── torrc
├── .env            # TOR_CONTROL_PASSWORD (não versionado, chmod 600)
```

**Imagem** (`Dockerfile`): `alpine:3.23` + `apk add tor=0.4.9.12-r0 curl`.
Versão fixada. A Tor Project não publica imagem Docker oficial do cliente;
o pacote do Alpine community é o mesmo tor 0.4.9 (medido: 0.4.9.12-r0).
Roda como o usuário `tor` que o pacote cria. Não é o Tor Browser (interface
gráfica): é só o daemon, que é o que o MCP usa.

**`torrc`** (mesmo arquivo para os dois containers):
```
SocksPort 0.0.0.0:9050 IsolateSOCKSAuth
SocksPolicy accept 172.16.0.0/12
SocksPolicy accept 10.0.0.0/8
SocksPolicy accept 192.168.0.0/16
SocksPolicy reject *
ControlPort 0.0.0.0:9051
DataDirectory /var/lib/tor
Log notice stdout
```
- `IsolateSOCKSAuth`: cada usuário/senha SOCKS diferente ganha circuito
  próprio, na hora. É assim que um canal "troca de IP" sem esperar.
- `SocksPolicy`: o tráfego publicado chega pelo gateway do Docker (faixa
  privada); qualquer outra origem é recusada.
- ControlPort com senha (o tor recusa controle sem autenticação fora de
  localhost e avisa "That's bad!"). Usada só para `SIGNAL NEWNYM` e
  healthcheck.
- A senha vem do `.env`; o entrypoint roda `tor --hash-password` e passa
  `--HashedControlPassword` na linha de comando — nada é gravado no
  container.

**`docker-compose.yaml`** — dois serviços iguais, padrão do SearXNG:
```yaml
services:
  tor-a:
    build: .
    container_name: tor-a
    restart: unless-stopped
    ports:
      - "127.0.0.1:9060:9050"   # SOCKS
      - "127.0.0.1:9061:9051"   # controle
    env_file: .env
    volumes:
      - tor-a-data:/var/lib/tor
    cap_drop: [ALL]
    logging: {driver: json-file, options: {max-size: "1m", max-file: "1"}}
    healthcheck:
      test: ["CMD", "curl", "-sf", "--socks5-hostname", "127.0.0.1:9050",
             "https://check.torproject.org/api/ip"]
      interval: 5m
      timeout: 30s
      retries: 3
      start_period: 60s
  tor-b:
    # idêntico, com container_name tor-b, portas 9070/9071 e volume tor-b-data
volumes:
  tor-a-data:
  tor-b-data:
```
- Portas **só em 127.0.0.1** (verificadas livres: 9060, 9061, 9070, 9071).
  Não publicar em 0.0.0.0: viraria proxy aberto para a rede.
- Volume por container: guarda o estado de guard node, e o boot seguinte
  conecta em segundos em vez de refazer tudo. Volumes separados = guards
  diferentes = canais independentes.
- `restart: unless-stopped` para subir sozinho no boot. As portas escolhidas
  não colidem com nada que também sobe no boot (lição do conflito
  open-webui/minio na 9000).

**Aceite do serviço**:
1. `docker compose up -d --build`, os dois `healthy`.
2. `curl --socks5-hostname 127.0.0.1:9060 https://api.ipify.org` e o mesmo
   na 9070 → dois IPs de exit, nenhum igual ao IP de casa.
3. Mesmo curl com `-U a:1` e depois `-U a:2` → IPs diferentes (isolamento
   por credencial funcionando).
4. `SIGNAL NEWNYM` pela 9061 com a senha → `250 OK`.
5. `docker restart tor-a` e reboot da máquina → volta sozinho e `healthy`.

## 4. MCP — mudanças no código

### 4.1 Dependência
`requests[socks]` no `pyproject.toml` (PySocks). Hoje não está instalado.

### 4.2 Config (só em `config.py`, regra do projeto)
| Variável | Default | Papel |
|---|---|---|
| `SEARCH_BACKEND` | `google_tor` | `google_tor` ou `searxng`. Rollback = trocar para `searxng` e reiniciar. |
| `TOR_CHANNELS` | `127.0.0.1:9060:9061,127.0.0.1:9070:9071` | `host:socks:controle` por canal, separados por vírgula. |
| `TOR_CONTROL_PASSWORD` | — (obrigatória) | Senha do ControlPort. Os containers do `search-engine/` leem o mesmo `.env` e não sobem sem ela. No código: falha de NEWNYM não pode quebrar a busca (a credencial SOCKS nova já troca o circuito). |
| `GOOGLE_CSE_CX` | CX do blackle | Configurável para trocar por um CX próprio sem mudar código. |
| `GOOGLE_CSE_HL` | `pt-BR` | Idioma da interface do CSE. Sem `lr`: não restringe o idioma do resultado (mesma lição do `SEARXNG_LANGUAGE=auto`). Validar no eval (seção 6). |
| `GOOGLE_CSE_TIMEOUT` | `15` | Segundos por requisição via Tor. |
| `GOOGLE_CSE_DIRECT_FALLBACK` | `true` | Permite o CSE pelo IP de casa quando os dois canais falham. |
| `SEARXNG_FALLBACK` | `true` | Permite cair no SearXNG quando o Google falhou por todos os caminhos. |

### 4.3 Módulos novos

**`util/tor.py` — `TorChannel` e `ChannelPool`**
- `TorChannel(host, socks_port, control_port, password)`:
  - `proxies()` → `socks5h://<cred>:x@host:port`. O `h` resolve DNS
    dentro do Tor. `<cred>` é um contador por canal; trocar a credencial =
    circuito novo, na hora.
  - `renew()`: incrementa a credencial (imediato) e, em thread de fundo,
    manda `AUTHENTICATE` + `SIGNAL NEWNYM` pelo ControlPort. O tor limita
    NEWNYM a 1 a cada 10 s: chamada dentro da janela é ignorada sem erro. A
    credencial nova já basta; o NEWNYM é reforço.
  - estado: `ok` ou `renovando` (com prazo). Um canal em `renovando` não
    recebe query nova até o prazo vencer ou a próxima tentativa passar.
- `ChannelPool`:
  - `pick(i)` → canal da query `i` (rodízio A, B, A, B…) entre os `ok`.
  - `other(ch)` → o outro canal, para o failover imediato.
  - thread-safe (as queries rodam em `ThreadPoolExecutor`).

**`util/google_cse.py` — `GoogleCSE`**
Mesma interface pública do `SearXNG`, para o `research.py` trocar a
instância sem mexer no pipeline: `search(query, time_range=None) ->
list[dict]`, `max_results`, `health()`, `reset_health()`.
- Token: `cse.js` pego pelo mesmo canal da busca, cache em memória por 1 h
  (igual ao SearXNG). Resposta de token inválido/expirado → descarta o cache
  e busca de novo uma vez.
- `time_range` (`day/week/month/year`) → `sort=date:r:início:fim`, mesma
  conta do `google_cse.py`.
- Mapeamento de cada resultado para o formato que o pipeline já consome:
  `url=unescapedUrl`, `title=titleNoFormatting`,
  `content=contentNoFormatting`, `engines=["google cse (tor-a)"]` (ou
  `tor-b` / `direto`), `score` decrescente pela posição (o
  `_merge_results` usa score como desempate).
- **Detecção de bloqueio** (qualquer um): HTTP 429/403 (ou outro não-2xx);
  JSON com `error.code` 429; resposta fora do formato; timeout/erro SOCKS.
  JSON com `error.code` 403 = token recusado: renova o token e tenta mais
  uma vez no mesmo caminho antes de contar como falha (ver Status).
  Resposta 200 com `results` vazio **não** é bloqueio: é "nada achado".
- **Sequência por query** (o que o Fabio descreveu):
  1. canal da vez (rodízio);
  2. barrou → `renew()` do canal barrado (em fundo) e **a mesma query no
     outro canal, imediatamente**;
  3. o outro também barrou → `renew()` nele e uma última tentativa no
     primeiro, já com circuito novo;
  4. ainda barrado → CSE pelo IP de casa (se `GOOGLE_CSE_DIRECT_FALLBACK`);
  5. ainda falhou → devolve vazio e marca "google indisponível"; a camada
     de cima cai no SearXNG.
  Teto: 3 tentativas Tor + 1 direta por query. Sem `sleep` em nenhum
  passo.
- Log por query: canal usado, número de tentativas, motivo de cada
  bloqueio, caminho final (tor-a/tor-b/direto/searxng).

**`util/search_chain.py` — `SearchChain`** (ou dentro de `research.py`,
decidir pelo tamanho)
- `search()` → `GoogleCSE.search()`; se voltou "google indisponível" e
  `SEARXNG_FALLBACK`, chama `SearXNG.search()`.
- `health()` → quando o Google respondeu, vazio (sem aviso); quando caiu no
  SearXNG, os motores mortos do SearXNG **mais** a entrada
  `google cse: indisponível via Tor e direto`. Assim o
  `_search_health_note` existente continua funcionando sem mudar sua regra
  (mortos > vivos → aviso), e a regra MANDATORY "busca degradada tem que ser
  dita" continua valendo no novo caminho.

### 4.4 Mudanças em `research.py`
- `_search` passa a ser construído por uma fábrica que lê `SEARCH_BACKEND`:
  `SearchChain(GoogleCSE(pool), SearXNG())` ou `SearXNG()`.
- A distribuição alternada A/B precisa do índice da query: `_collect_links`
  passa `i` para `_search_one`/`_search_one_safe` (hoje recebem só
  `(query, recent)`). Com `SEARCH_BACKEND=searxng`, o índice é ignorado.
- Nada mais muda: `_merge_results`, a rejeição de hub, `_read_pages`, o
  resumo e as citações inline ficam iguais. A leitura das páginas continua
  sem Tor.

### 4.5 Testes (`uv run --group test pytest`, sem rede)
`tests/test_tor.py`, `tests/test_google_cse.py`, ajustes em
`test_research.py`:
1. Parse do `cse.js` (token, cselibv, exp) e do JSONP de resultados.
2. Mapeamento de campos e score decrescente.
3. `time_range` → `sort=date:r:...` correto.
4. 429 no canal A → mesma query sai no B **sem espera** (assert de tempo
   baixo / nenhum sleep) e `renew()` de A foi chamado.
5. A e B barrados → última tentativa em A com credencial nova → direto.
6. Tudo barrado → `SearchChain` cai no SearXNG e o `health()` inclui o
   Google indisponível → `_search_health_note` gera o aviso.
7. Google OK → nenhum aviso de degradação.
8. Bloqueio vindo como HTTP 200 + `error.code 429` é reconhecido.
9. 200 com `results: []` NÃO dispara failover.
10. Rodízio: 5 queries saudáveis → A,B,A,B,A.
11. `renew()` troca a credencial SOCKS (proxies diferentes antes/depois).
12. NEWNYM: comando enviado com a senha; falha no ControlPort não quebra a
    busca (a credencial nova já resolve).
13. `SEARCH_BACKEND=searxng` → comportamento idêntico ao atual (testes
    existentes passam sem mudança).

## 5. Ordem de execução

1. ~~Serviço Tor (seção 3) + aceite do serviço.~~ FEITO em 10/09/2026.
2. ~~Dependência + config + `util/tor.py` + testes.~~ FEITO.
3. ~~`util/google_cse.py` + testes.~~ FEITO.
4. ~~`SearchChain` + integração no `research.py` + testes. Suíte inteira
   verde.~~ FEITO.
5. ~~Eval (seção 6) contra a web real, **antes** de trocar produção.~~
   FEITO, seção 6.1.
6. ~~Docs~~ FEITO: CLAUDE.md (Architecture: fonte de links = Google CSE via Tor, com
   SearXNG de fallback; nova seção sobre os canais), README (tabela de
   config), `.env.example`.
7. Fabio faz commit/push → deploy: `git checkout -- src/` no checkout de
   produção (ver Status), `git pull`, `uv sync`, `.env` de
   produção com `SEARCH_BACKEND=google_tor`, `TOR_CHANNELS`,
   `TOR_CONTROL_PASSWORD`; `systemctl --user restart web-search-mcp`.
8. Verificação em produção pelo serviço rodando (cliente MCP na 8775) com
   perguntas de assuntos sem relação entre si.

## 6. Eval e critério de sucesso

Rodado na mesma sessão para os dois lados (padrão do `evals/ab.py`: a web
muda entre dias, só comparação lado a lado vale).

- **A/B de qualidade**: `evals/questions.py` + as 6 perguntas usadas hoje
  (Python retry/backoff, Raft, vitamina B12, Tailscale ACL, pão de
  fermentação natural, a pergunta das portas do BG3) — 12 assuntos sem
  relação entre si. Medir por pesquisa: resultados com o assunto no
  título/URL, páginas úteis lidas, domínios distintos, tempo da fase de
  busca, tempo total.
- **Carga contínua** (a incógnita da medição de hoje, que foi de só 12
  buscas espaçadas): 20 pesquisas seguidas, sem pausa (~100 buscas CSE).
  Medir: taxa de 429 por canal, quantas queries precisaram de failover,
  quantas caíram para o direto, quantas para o SearXNG.
- **`GOOGLE_CSE_HL`**: repetir o A/B com `pt-BR` e `en` e manter o que der
  mais resultados relevantes nas perguntas em inglês sem piorar as em
  português.

Go para produção se, na carga contínua:
- ≥ 80% das queries servidas via Tor (A ou B) sem cair para o direto;
- 0 pesquisas terminando sem nenhum resultado quando o direto está OK;
- relevância ≥ à do SearXNG no mesmo A/B;
- fase de busca p50 ≤ 8 s.

Se não passar: não vai para produção; registrar os números e decidir com o
Fabio (ex.: CSE direto como primário e Tor como reserva).

### 6.1 Resultado (10/09/2026, 16:26-16:34, `evals/search_ab.py`)

Saída completa: `evals/results/search_ab__20260910_163404.json`. Mesmas
variantes de busca nos três braços (geradas uma vez por pergunta,
qwen3.8:27B), 12 perguntas, páginas lidas de verdade.

| Braço | Pesquisas vazias | Resultados no assunto / pool | Páginas lidas | Domínios | Busca p50 | Total p50 |
|---|---|---|---|---|---|---|
| SearXNG | 2 (OAuth2/PKCE, retry/backoff) | 112 / 125 | 45 | 39 | 2,7 s | 4,5 s |
| Google CSE Tor, `hl=pt-BR` | 0 | 228 / 230 | 71 | 66 | 4,6 s | 8,2 s |
| Google CSE Tor, `hl=en` | 0 | 228 / 232 | 72 | 66 | 4,2 s | 7,6 s |

Carga contínua logo em seguida (20 pesquisas sem pausa, só busca, 80
consultas CSE): **100% servidas via Tor**, 0 direto, 0 SearXNG, 5
failovers; tor-a barrado em 2/44 tentativas (5%), tor-b em 4/42 (10%);
388/389 resultados no assunto; **busca p50 9,7 s** (mín 3,8, máx 16,0).

Critérios de go:
- ≥ 80% via Tor: **passou** (100%).
- 0 pesquisas sem resultado com o direto OK: **passou** (0 vazias; o
  direto nem estava OK — IP de casa com 429).
- relevância ≥ SearXNG: **passou** (o dobro de resultados no assunto;
  SearXNG com 13/15 motores suspensos zerou duas perguntas técnicas).
- busca p50 ≤ 8 s: **passou no A/B (4,6 s), falhou na carga (9,7 s)**.
  A latência sobe com as consultas seguidas, não com bloqueio (só 5
  failovers em 80); cada canal atende 2-3 consultas simultâneas.

`GOOGLE_CSE_HL`: empate (228 × 228 no assunto). `en` um pouco mais
rápido; por pergunta, `pt-BR` melhor na do Tailscale (18/18 × 15/19) e
`en` na do pão (20/20 × 18/20). Mantido `pt-BR` (default). Todas as 12
perguntas são em português: o critério "perguntas em inglês" não foi
medido.

**Veredito pela regra acima: não vai para produção sem decisão do
Fabio** — 3 dos 4 critérios passaram com folga, o de latência só sob carga
contínua. Opções: aceitar os 9,7 s (a busca de hoje é mais rápida porque
devolve pouco ou nada); mais canais (`tor-c`, `TOR_CHANNELS` já aceita N);
ou medir de novo com carga realista, com pausa de leitura entre as
pesquisas como no A/B.

## 7. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Endpoint do CSE não é API documentada; o Google pode mudar token/formato sem aviso | Fallback para o SearXNG mantém a busca de pé; teste de parse falha alto; o log mostra o caminho final de cada query. Acompanhar mudanças no `searx/engines/google_cse.py` upstream. |
| CX é de terceiro (blackle.com) e pode sumir | `GOOGLE_CSE_CX` configurável. Tarefa futura: criar um CX próprio e testar se ele aceita "pesquisar na web inteira". |
| Taxa de 429 em uso contínuo desconhecida | Carga contínua no eval com critério de go/no-go. |
| Fonte única (Google) = ponto único de falha | Cadeia Tor A → Tor B → direto → SearXNG. |
| CSE ≠ google.com (ranking `filtered_cse`, pode ser menos atual em notícia do dia) | Medir no A/B com perguntas `recent=True`. |
| Latência do Tor (1–3 s por requisição) | Queries em paralelo nos dois canais; failover sem espera; timeout de 15 s. |
| Termos de uso: consulta automatizada ao Google via Tor viola os termos do Google | Uso pessoal, decisão do Fabio. Registrado aqui uma vez. |
| Proxy exposto | Portas só em 127.0.0.1, `SocksPolicy` só para faixas privadas, ControlPort com senha. |

## 8. Fora do escopo

- Leitura das páginas via Tor (decidido: direto).
- Mudar ou desligar o `tor@default` do sistema (decidido: fica).
- Remover o SearXNG (continua como fallback e para `SEARCH_BACKEND=searxng`).
- Qualquer mudança em prompt, resumo ou citações.
- **Em aberto, NÃO implementar sem decisão do Fabio:** MCP como container
  dentro do `search-engine/docker-compose.yaml` (Dockerfile do MCP, serviço
  `mcp` com `depends_on` nos Tor/SearXNG, `SEARXNG_URL`/`TOR_CHANNELS` por
  nome de serviço, LLM via `host.docker.internal`). Faltam duas decisões:
  bind em `127.0.0.1` (seguro, sem auth) ou `0.0.0.0`; e se a produção
  migra para esse compose ou continua no systemd (porta 8775).
