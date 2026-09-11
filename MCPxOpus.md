# MCP x Opus

Execute o comparativo abaixo do começo ao fim, sem pedir confirmação, e
termine gravando `report-versus.md` na raiz do projeto.

## Perguntas

| # | Pergunta | recent |
|---|---|---|
| 1 | Como inicia a chain do Pai Putrefato no ato 3 de BG3? | False |
| 2 | O que diz a Lei do Bem sobre incentivo à inovação? | False |
| 3 | O que é a doença cobreiro e como trata? | False |
| 4 | Quem foi a Tia Ciata? | False |
| 5 | Como usar o git worktree para trabalhar em duas branches ao mesmo tempo? | False |
| 6 | Qual a diferença entre o Pix Automático e o Pix Agendado? | False |
| 7 | Como funciona o fluxo de autorização OAuth2 com PKCE? | False |
| 8 | Qual a cotação atual do dólar em reais? | True |
| 9 | Quais as principais noticias no Brasil e no mundo hoje? | True |
| 10 | Como limitar as portas que um usuário pode acessar com ACL no Tailscale? | False |

## 1. Lado MCP

Rode em segundo plano (`run_in_background: true`), na raiz do projeto. Grava
resposta e tempo de cada pergunta no scratchpad da sessão:

```bash
uv run python - <<'EOF' > SCRATCHPAD/mcp.json
import json, time
from web_search_mcp.tools import research
Q = [("Como inicia a chain do Pai Putrefato no ato 3 de BG3?", False),
     ("O que diz a Lei do Bem sobre incentivo à inovação?", False),
     ("O que é a doença cobreiro e como trata?", False),
     ("Quem foi a Tia Ciata?", False),
     ("Como usar o git worktree para trabalhar em duas branches ao mesmo tempo?", False),
     ("Qual a diferença entre o Pix Automático e o Pix Agendado?", False),
     ("Como funciona o fluxo de autorização OAuth2 com PKCE?", False),
     ("Qual a cotação atual do dólar em reais?", True),
     ("Quais as principais noticias no Brasil e no mundo hoje?", True),
     ("Como limitar as portas que um usuário pode acessar com ACL no Tailscale?", False)]
out = []
for n, (q, recent) in enumerate(Q, 1):
    research._recent_calls.clear()
    t = time.monotonic()
    try:
        a = research.research_web(q, recent)
    except Exception as e:
        a = f"ERRO: {e!r}"
    out.append({"n": n, "seconds": round(time.monotonic() - t, 1), "answer": a})
print(json.dumps(out, ensure_ascii=False, indent=1))
EOF
```

(`SCRATCHPAD` = caminho literal do scratchpad desta sessão.) Precisa do LLM
em `MODEL_BASE_URL` e da busca (SearXNG/Tor) no ar; se o comando falhar por
isso, pare e avise o usuário.

## 2. Lado Opus (você)

Enquanto o MCP roda, responda você mesmo às 10 perguntas, **uma por vez, em
ordem**, com WebSearch e WebFetch, como faria para um usuário de verdade:

- Antes de começar cada pergunta rode `date +%s.%N`; ao terminar a resposta,
  rode de novo. A diferença é o seu tempo naquela pergunta.
- Proibido usar as ferramentas deste MCP (`research_web`, `read_url`,
  `analyze_urls`) e proibido olhar `mcp.json` ou relatórios anteriores antes
  de terminar as suas 10 respostas.
- Pelo menos uma busca por pergunta; nada respondido só de memória.
- Resposta em pt-BR, direta, até ~400 palavras, cada fato com link markdown
  inline para a página lida. Nas perguntas 8 e 9, diga data e hora do dado.

Guarde as respostas no scratchpad (`opus_q01.md` … `opus_q10.md`).

## 3. Notas

Quando o MCP terminar, dê nota de 1 a 100 a cada resposta dos dois lados:
**correção (50) + completude (30) + fontes (20)**.

- Correção: erro factual tira de 3 (detalhe) a 15 (fato central). Resposta
  que não encontra ou nega o que foi perguntado, quando a resposta existe:
  no máximo 15 em correção. Dado de outro dia nas perguntas 8 e 9 é erro
  central.
- Completude: aspectos que o usuário esperaria (passos, requisitos,
  exceções, datas); na 9, Brasil e mundo cobertos. Enchimento tira pontos.
- Fontes: link inline que abre e sustenta o fato; oficial quando existe.
  Marcador `[1]` sem link ou link de outro assunto: −3 a −8. Sem link: máx. 3.

Confira na web os fatos em que as duas respostas divergem antes de dar a
nota. Seja igualmente rigoroso com as suas respostas.

**Vencedor**: maior nota. Diferença de até 2 pontos é empate técnico — vence
o mais rápido, marcado "(tempo)".

## 4. report-versus.md

Grave na raiz do projeto (sobrescreve o anterior):

```markdown
# MCP x Opus — DD/MM/AAAA HH:MM

Commit `<git rev-parse --short HEAD>` · modelo do MCP `<modelo carregado>`

| # | Pergunta | MCP nota | MCP tempo | Opus nota | Opus tempo | Vencedor |
|---|---|---:|---:|---:|---:|---|
| 1 | ... | 94 | 45,7 s | 97 | 23,8 s | Opus |
| ... |
| | **Média** | **..** | **.. s** | **..** | **.. s** | MCP X · Opus Y · empate Z |

## Justificativas

### 1. <pergunta>
- **MCP (nota)**: o que acertou, errou ou faltou, com o fato.
- **Opus (nota)**: idem.
...
```

No fim, mostre a tabela ao usuário e diga em 3-5 linhas onde o MCP perdeu e
por quê (busca, escolha de página, extração ou resumo).
