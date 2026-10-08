"""Nota cega de 0 a 10 para os resumos de um reasoning_ab já rodado.

Uso:
    uv run python -m evals.blind_grade evals/results/reasoning_ab__....json [passadas]
    (padrão: 3 passadas; juiz = EVAL_JUDGE_MODEL em EVAL_JUDGE_BASE_URL)

Faithfulness e relevance saturam: resumo fiel a um dossiê fraco tira 1.00, e
relevance de 0 a 2 não separa braços parecidos. Aqui o juiz vê, por pergunta,
TODAS as respostas juntas (todos os braços e rodadas), embaralhadas e sem
dizer de que braço vieram, e dá uma nota a cada uma. Julgar lado a lado é o
que separa respostas próximas; julgar uma por vez devolve 8 para todas.

Cada passada embaralha de novo, para a posição na lista não favorecer um
braço. Não gasta GPU: lê os resumos do JSON.
"""

import json
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from web_search_mcp import config
from web_search_mcp.llm import chat

from evals.reasoning_ab import _JUDGE_WORKERS, _judge_config

_INSTRUCTION = (
    "Você avalia respostas de um assistente de pesquisa na web. Recebe uma "
    "pergunta e várias respostas candidatas, identificadas por letras. Dê a "
    "cada resposta uma nota inteira de 0 a 10 pelo quanto ela serve a quem "
    "fez a pergunta: responde o que foi perguntado, é correta, é completa no "
    "que importa, traz o dado concreto (valor, data, nome) quando a pergunta "
    "pede um, e não enrola nem inventa. Resposta vazia, cortada no meio ou "
    "que foge da pergunta leva nota baixa. Compare as respostas entre si e "
    "use a escala toda: respostas de qualidade diferente recebem notas "
    "diferentes. Devolva só um objeto JSON no formato "
    '{"A": nota, "B": nota, ...}, com todas as letras, sem mais nada.'
)

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# O juiz lê todas as respostas de uma pergunta de uma vez e raciocina antes
# de responder: cabe mal no prazo de uma chamada do pipeline.
_JUDGE_TIMEOUT = 600


def _grade(job: tuple[str, list[dict], int, str]) -> None:
    query, answers, seed, today = job
    order = answers[:]
    random.Random(seed).shuffle(order)
    listing = "\n\n".join(
        f"=== Resposta {_LETTERS[i]} ===\n{a['summary'] or '(vazia)'}" for i, a in enumerate(order)
    )
    try:
        content = chat(system=f"Data de hoje: {today}. {_INSTRUCTION}", user=f"Pergunta: {query}\n\n{listing}")
        grades = json.loads(re.search(r"\{.*\}", content, re.S).group(0))
    except Exception as e:
        print(f"   !! nota falhou ({query[:40]}, passada {seed}): {e}", flush=True)
        return
    for i, a in enumerate(order):
        grade = grades.get(_LETTERS[i])
        if isinstance(grade, (int, float)):
            a["grades"].append(float(grade))
    print(f"   {query[:50]:<50} passada {seed}: "
          + " ".join(f"{a['arm']}={grades.get(_LETTERS[i])}" for i, a in enumerate(order)), flush=True)


def main() -> None:
    path = Path(sys.argv[1])
    passes = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    data = json.loads(path.read_text())

    by_query: dict[str, list[dict]] = {}
    for run in data["runs"]:
        for row in run["rows"]:
            if "erro" in row:
                continue
            by_query.setdefault(row["query"], []).append(
                {"arm": run["arm"], "round": run["round"], "summary": row["summary"], "row": row, "grades": []}
            )

    today = datetime.now().strftime("%d/%m/%Y")
    jobs = [(q, answers, seed, today) for q, answers in by_query.items() for seed in range(1, passes + 1)]
    with _judge_config(data["model"]), patch.object(config, "MODEL_TIMEOUT", _JUDGE_TIMEOUT):
        print(f"juiz: {config.MODEL} | {len(by_query)} perguntas x {passes} passadas\n", flush=True)
        judge = config.MODEL
        with ThreadPoolExecutor(_JUDGE_WORKERS) as pool:
            list(pool.map(_grade, jobs))

    arms = list(dict.fromkeys(run["arm"] for run in data["runs"]))
    for answers in by_query.values():
        for a in answers:
            if a["grades"]:
                a["row"]["nota_cega"] = statistics.mean(a["grades"])

    def per_arm(arm, fn):
        return [fn(a) for answers in by_query.values() for a in answers if a["arm"] == arm and a["grades"]]

    print("\n%-44s" % "pergunta" + "".join(f" {a:>7}" for a in arms))
    for query, answers in by_query.items():
        cells = []
        for arm in arms:
            grades = [g for a in answers if a["arm"] == arm for g in a["grades"]]
            cells.append(" %7.1f" % statistics.mean(grades) if grades else "       -")
        print("%-44s" % query[:44] + "".join(cells))
    print("%-44s" % "MÉDIA" + "".join(
        " %7.2f" % statistics.mean(per_arm(a, lambda x: statistics.mean(x["grades"])) or [0]) for a in arms))
    # Diferença entre as duas rodadas do MESMO braço: é o ruído. Diferença
    # entre braços menor que isto não é resultado.
    noise = []
    for answers in by_query.values():
        for arm in arms:
            rounds = [statistics.mean(a["grades"]) for a in answers if a["arm"] == arm and a["grades"]]
            if len(rounds) == 2:
                noise.append(abs(rounds[0] - rounds[1]))
    if noise:
        print(f"\nRuído (diferença média entre rodadas do mesmo braço): {statistics.mean(noise):.2f} ponto(s)")
    print("%-44s" % "tamanho médio do resumo (chars)" + "".join(
        " %7.0f" % statistics.mean(per_arm(a, lambda x: len(x["summary"])) or [0]) for a in arms))

    data["blind_judge"] = judge
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\nNotas gravadas em {path}")


if __name__ == "__main__":
    main()
