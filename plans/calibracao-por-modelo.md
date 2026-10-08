# Calibragem por modelo: o que falta

Continuação do trabalho de 07/10/2026 (branch `fix/llm-runaway-qwen36`, já na
`main`). O que entrou: `chat()` em streaming com prazo total, detector de
repetição, fusível de tokens, teto padrão de raciocínio e o `models.toml` com
o ajuste por modelo. O que ficou para depois está aqui.

## Por que isso existe

O MCP usa o modelo que estiver carregado no servidor, e cada modelo pede um
ajuste de raciocínio diferente. Medido em 07/10/2026 (6 perguntas x 2 rodadas,
nota cega de 0 a 10 pelo `deepseek-v4-pro`, tempo só do modelo):

| modelo | configuração | nota | tempo |
|---|---|---|---|
| qwen3.6:35B | sem raciocínio | 7,5 | 11 s |
| qwen3.6:35B | teto 256 (adotado) | 8,0–8,4 | 13 s |
| qwen3.6:35B | teto 2048 | 8,1 | 32 s |
| qwen3.6:35B | sem teto | 7,8 | 56 s |
| qwen3.8:27B | sem raciocínio | 7,8 | 31 s |
| qwen3.8:27B | `low` (adotado, com teto 2048) | 9,1 | 61 s |
| qwen3.8:27B | `low` + teto 256 | 8,4 | 37 s |
| qwen3.8:27B | teto 256 sem `low` | 6,9 | 41 s |

O mesmo ajuste que acelera o 3.6 (teto 256) estraga o 3.8 se faltar o `low`.
Quem baixa o MCP não tem como saber disso, e hoje nada avisa.

## 1. Aviso de modelo não calibrado

Hoje um modelo sem tabela no `models.toml` cai no `[default]` (sem raciocínio)
em silêncio.

- Logar uma vez por modelo, no primeiro `chat()`: modelo detectado, que não
  tem tabela própria, o que está valendo e como calibrar.
- Uma vez por processo e por modelo, não a cada chamada: o `_resolve_model`
  roda em toda chamada e o log viraria ruído.
- Só no log. Não entra na resposta da tool: é infraestrutura, não assunto.
- Onde: `llm._reasoning_settings` sabe se achou a tabela.

## 2. Comando `calibrate`

`web-search-mcp calibrate`, no modelo carregado (ou `--model`).

### Etapa A: sonda (segundos)

O que foi feito à mão em 07/10/2026, com uma pergunta de raciocínio curto e
`max_tokens` baixo, medindo chars de raciocínio e se a resposta sai:

1. O modelo pensa por padrão?
2. `enable_thinking: false` desliga?
3. `reasoning_effort: low` reduz o raciocínio? (no 3.6 não reduz: 7.025 chars
   sem, 6.858 com)
4. `reasoning_budget_tokens` corta? (no 3.6, 200 tokens -> 543 chars e a
   resposta sai)

Saída: a tabela pronta para colar no `models.toml`, com o `body` que o modelo
respeita. Atenção ao que a sonda NÃO diz: ela mostra qual alavanca funciona,
não se raciocinar melhora a resposta.

Também ler o chat template (`GET /props?model=` no llama.cpp, campo
`chat_template`) e procurar `reasoning_effort` / `enable_thinking`: é
confirmação barata do que a sonda mediu.

### Etapa B: eval embutido (minutos, opcional)

Hoje são scripts soltos: `evals/reasoning_ab.py` (braços) e
`evals/blind_grade.py` (nota cega). Falta:

- Gerar os braços a partir do que a sonda achou, em vez da lista fixa `_ARMS`.
- Um veredito no fim ("raciocínio compensa / não compensa neste modelo"),
  comparando a diferença entre braços com o ruído entre rodadas.
- Juiz: precisa de `EVAL_JUDGE_MODEL` / `EVAL_JUDGE_BASE_URL` /
  `EVAL_JUDGE_API_KEY`. Sem juiz externo o modelo julga a si mesmo e a nota
  absoluta é otimista.

Lições de método, para não repetir:

- Faithfulness e relevance saturam (todo braço ~1,00 e ~2,00). Só a nota cega
  lado a lado separa braços.
- O ruído entre duas rodadas do mesmo braço é de 1,0 a 1,5 ponto. Diferença
  menor que isso não é resultado.
- Timeout do juiz não é nota zero (já corrigido no `reasoning_ab`).
- O eval troca o modelo na GPU: avisar antes e recarregar o anterior no fim.

## 3. Pendências menores

- README e `.env.example` em dois níveis: "básico" (URL do servidor, modelo,
  timeout) e "ajuste fino de raciocínio", com a tabela de receitas por modelo.
- Nenhum modelo carregado no servidor e mais de um na lista: `_resolve_model`
  falha. Decidir entre mensagem clara ou cair num `MODEL` padrão.
- O detector de repetição não pega ciclo dentro de uma linha só (sem quebra)
  nem ciclo maior que 8 linhas. Esses casos ficam com o prazo e o fusível.
- "Notícias de hoje" é o tipo de pergunta mais fraco em qualquer configuração
  (notas de 4 a 8). É da seleção de fonte e do formato de lista, não do
  raciocínio.
- O sampler do card do Qwen3.6 (`presence_penalty` 1.5, `top_k` 20) foi medido
  e não ajudou (7,7–7,9 contra 8,0); não adotar sem medir de novo.
