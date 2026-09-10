#!/usr/bin/env bash
# Sobe a infraestrutura de busca (search-engine/: SearXNG + canais Tor) e o
# MCP em streamable-http, pronto para um cliente plugar. Ctrl+C derruba tudo
# o que este script subiu. Argumentos extras vão para o web-search-mcp.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK="$ROOT/search-engine"
ENV_FILE="$ROOT/.env"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# Lê KEY=valor do .env sem dar source: o EXTRA_BODY pode ter JSON com espaço
# e aspas, que quebraria o shell.
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1; }

# Porta ocupada = alguém aceita conexão nela.
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

for cmd in docker uv curl; do
  command -v "$cmd" >/dev/null || die "'$cmd' não encontrado no PATH."
done
docker compose version >/dev/null 2>&1 || die "'docker compose' (plugin v2) não encontrado."

# --- 1. .env -----------------------------------------------------------------
# Tem que existir ANTES do compose: os containers Tor leem este mesmo arquivo
# (env_file: ../.env) e o compose não sobe sem ele.
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT/.env.example" "$ENV_FILE"
  warn ".env criado a partir do .env.example."
fi

cat >&2 <<MSG

  Confira o .env antes de subir ($ENV_FILE):
    MODEL_BASE_URL        servidor de LLM compatível com OpenAI (atual: $(env_get MODEL_BASE_URL))
    MODEL_API_KEY         se o servidor exigir
    TOR_CONTROL_PASSWORD  senha do controle dos containers Tor
    MCP_HOST / MCP_PORT   onde o MCP escuta (atual: $(env_get MCP_HOST):$(env_get MCP_PORT))

MSG
if [[ "$(env_get TOR_CONTROL_PASSWORD)" == "troque-esta-senha" ]]; then
  warn "TOR_CONTROL_PASSWORD ainda é o placeholder público do repositório — troque."
fi
if [[ -t 0 ]]; then
  read -r -p "Edite o .env se precisar e pressione Enter para subir (Ctrl+C cancela)... " _
fi

MCP_HOST="$(env_get MCP_HOST)"; MCP_HOST="${MCP_HOST:-127.0.0.1}"
MCP_PORT="$(env_get MCP_PORT)"; MCP_PORT="${MCP_PORT:-8765}"

# --- 2. Conflitos --------------------------------------------------------------
# Porta ou nome de container já em uso vira um erro confuso do Docker no meio
# da subida; aqui ele sai com o nome do culpado. Portas e nomes vêm do próprio
# compose (inclui um docker-compose.override.yaml, se houver).
cd "$STACK"
docker compose config >/dev/null || die "docker-compose.yaml inválido (ver erro acima)."
own="$(docker compose ps -a --format '{{.Name}}' 2>/dev/null || true)"
busy=()
if [[ -z "$own" ]]; then
  for p in $(docker compose config | sed -n 's/^ *published: "\{0,1\}\([0-9]\{1,5\}\)"\{0,1\}$/\1/p' | sort -u); do
    port_busy "$p" && busy+=("$p")
  done
fi
port_busy "$MCP_PORT" && busy+=("$MCP_PORT (MCP_PORT)")
clash=()
for n in $(docker compose config | sed -n 's/^ *container_name: //p'); do
  if docker ps -a --format '{{.Names}}' | grep -qx "$n" && ! grep -qx "$n" <<<"$own"; then
    clash+=("$n")
  fi
done
((${#busy[@]} == 0)) || die "Portas já em uso: ${busy[*]}. Libere-as ou mude o mapeamento."
((${#clash[@]} == 0)) || die "Containers de outro projeto com o mesmo nome: ${clash[*]}."

# --- 3. Parada ---------------------------------------------------------------
# Armado ANTES de subir qualquer coisa: Ctrl+C no meio do build também limpa.
# Se o MCP cair sozinho, a stack cai junto — nada fica rodando órfão.
MCP_PID=""
cleanup() {
  trap - INT TERM EXIT
  echo >&2
  if [[ -n "$MCP_PID" ]] && kill -0 "$MCP_PID" 2>/dev/null; then
    say "Parando o MCP..."
    kill "$MCP_PID" 2>/dev/null || true
    wait "$MCP_PID" 2>/dev/null || true
  fi
  say "Derrubando a infraestrutura de busca..."
  (cd "$STACK" && docker compose down) >&2 \
    || warn "docker compose down falhou; confira com 'docker compose ps' em $STACK."
  say "Tudo parado."
}
trap 'cleanup; exit 130' INT TERM
trap cleanup EXIT

# --- 4. Infraestrutura -------------------------------------------------------
say "Subindo SearXNG e os canais Tor (a 1ª vez compila a imagem do Tor)..."
docker compose up -d --build --wait --wait-timeout 180 >&2

SEARXNG_URL="$(env_get SEARXNG_URL)"; SEARXNG_URL="${SEARXNG_URL:-http://localhost:8886}"
say "Esperando o SearXNG responder em $SEARXNG_URL..."
for _ in $(seq 60); do
  curl -sf -o /dev/null "$SEARXNG_URL/search?q=teste&format=json" && break
  sleep 1
done || true
curl -sf -o /dev/null "$SEARXNG_URL/search?q=teste&format=json" \
  || warn "SearXNG não respondeu JSON em 60 s; o research_web vai falhar até ele subir."

MODEL_BASE_URL="$(env_get MODEL_BASE_URL)"
if ! curl -sf -o /dev/null --max-time 5 "$MODEL_BASE_URL/models"; then
  warn "LLM não respondeu em $MODEL_BASE_URL/models. read_url funciona; research_web e analyze_urls não, até o LLM subir."
fi

# --- 5. MCP ------------------------------------------------------------------
cd "$ROOT"
say "Iniciando o MCP (streamable-http)..."
uv run web-search-mcp --http "$@" &
MCP_PID=$!

for _ in $(seq 60); do
  port_busy "$MCP_PORT" && break
  kill -0 "$MCP_PID" 2>/dev/null || break
  sleep 1
done

if port_busy "$MCP_PORT"; then
  connect_host="$MCP_HOST"; [[ "$connect_host" == "0.0.0.0" ]] && connect_host="127.0.0.1"
  cat >&2 <<MSG

  MCP pronto. Plugue o cliente em:
      http://$connect_host:$MCP_PORT/mcp   (streamable-http)

  Ctrl+C para parar tudo.

MSG
  if [[ "$MCP_HOST" == "0.0.0.0" ]]; then
    warn "MCP_HOST=0.0.0.0: o modo HTTP não tem autenticação e fica aberto para a rede inteira."
  fi
fi

status=0
wait "$MCP_PID" || status=$?
MCP_PID=""
warn "O MCP encerrou sozinho (código $status)."
exit "$status"
