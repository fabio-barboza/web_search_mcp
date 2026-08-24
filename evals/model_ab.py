"""A/B de modelos: qualidade x latência do resumo, com o MESMO dossiê.

Uso:
    uv run python -m evals.model_ab [modeloA] [modeloB]
    (padrão: qwen3.8:27B qwen3.6:35B)

Fases:
1. Dossiês — um por pergunta, construídos UMA vez (variantes de busca
   geradas pelo modelo A). Os dois modelos resumem o mesmo material: a
   diferença medida é do modelo, não do estado da web entre os braços.
2. Braços — por modelo: warmup primeiro (paga o load do router fora do
   relógio), depois, por pergunta, mede a geração de variantes de busca
   (proxy de chamada curta) e o resumo do dossiê (chamada longa, prefill
   pesado — o trabalho real deste MCP).
3. Julgamento — um único juiz (EVAL_JUDGE_MODEL, ou o modelo A) avalia
   todos os resumos. Juiz fixo para os dois braços: a nota absoluta é
   otimista para o braço do próprio juiz, mas a comparação relativa vale.

Troca de modelo: config.MODEL é patchado por braço; o llama-swap carrega o
modelo na primeira chamada (o warmup). São 3 loads no total (A, B, juiz).
"""

import json
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from web_search_mcp import config
from web_search_mcp.llm import chat
from web_search_mcp.tools.research import _build_dossier, _generate_queries, _summarize

from evals.judge import faithfulness, relevance
from evals.questions import QUESTIONS

_RESULTS_DIR = Path(__file__).parent / "results"

# Afirmações julgadas por resumo. Uma chamada de LLM por afirmação; 8 é
# amostra suficiente para comparar braços sem levar meia hora.
_MAX_CLAIMS = 8


@contextmanager
def _model(name: str):
    with patch.object(config, "MODEL", name):
        yield


def _warmup(model: str) -> float:
    """Primeira chamada paga o load do modelo no router; devolve o tempo."""
    t0 = time.perf_counter()
    with _model(model):
        chat(system="Responda apenas: ok", user="ok")
    return time.perf_counter() - t0


def _build_dossiers(model: str) -> list[dict]:
    """Um dossiê por pergunta, com as variantes de busca do modelo dado."""
    items = []
    with _model(model):
        for n, q in enumerate(QUESTIONS, 1):
            print(f"[dossiê {n}/{len(QUESTIONS)}] {q['query']}", flush=True)
            dossier, pages = _build_dossier(q["query"], q["recent"])
            items.append({**q, "dossier": dossier, "paginas": len(pages)})
    return items


def _run_arm(model: str, dossiers: list[dict]) -> dict:
    load_s = _warmup(model)
    print(f"== {model}: warmup/load {load_s:.1f}s", flush=True)
    rows = []
    with _model(model):
        for item in dossiers:
            if not item["dossier"]:
                continue
            t0 = time.perf_counter()
            _generate_queries(item["query"])
            qg_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            summary = _summarize(item["query"], item["dossier"], item["recent"])
            sum_s = time.perf_counter() - t0

            print(f"   {item['query'][:50]:<50} queries {qg_s:5.1f}s  resumo {sum_s:5.1f}s",
                  flush=True)
            rows.append({
                "query": item["query"],
                "queries_s": qg_s,
                "resumo_s": sum_s,
                "resumo_chars": len(summary),
                "summary": summary,
            })
    return {"model": model, "load_s": load_s, "rows": rows}


def _judge(arms: list[dict], dossiers: list[dict], judge_model: str) -> None:
    """Anota faithfulness/relevance em cada linha, com um juiz só."""
    by_query = {d["query"]: d["dossier"] for d in dossiers}
    with _model(judge_model):
        chat(system="Responda apenas: ok", user="ok")  # paga o load
        for arm in arms:
            for row in arm["rows"]:
                faith, sup, tot = faithfulness(
                    row["summary"], by_query[row["query"]], max_claims=_MAX_CLAIMS
                )
                row["faithfulness"] = faith
                row["claims"] = f"{sup}/{tot}"
                row["relevance"] = relevance(row["query"], row["summary"])
                print(f"   juiz [{arm['model']}] {row['query'][:40]:<40} "
                      f"faith {faith:.2f} ({row['claims']}) rel {row['relevance']}",
                      flush=True)


def _summary_table(arms: list[dict]) -> None:
    print("\n%-22s" % "métrica" + "".join(f" {a['model']:>16}" for a in arms))

    def line(label, fn, fmt="%16.2f"):
        print("%-22s" % label + "".join(" " + fmt % fn(a) for a in arms))

    line("load/warmup (s)", lambda a: a["load_s"])
    line("queries média (s)", lambda a: statistics.mean(r["queries_s"] for r in a["rows"]))
    line("resumo média (s)", lambda a: statistics.mean(r["resumo_s"] for r in a["rows"]))
    line("resumo chars médio", lambda a: statistics.mean(r["resumo_chars"] for r in a["rows"]))
    line("faithfulness média", lambda a: statistics.mean(r["faithfulness"] for r in a["rows"]))
    line("relevance média", lambda a: statistics.mean(r["relevance"] for r in a["rows"]))


def main() -> None:
    models = sys.argv[1:3] if len(sys.argv) >= 3 else ["qwen3.8:27B", "qwen3.6:35B"]
    judge_model = config.EVAL_JUDGE_MODEL or models[0]

    print(f"Modelos: {models[0]} x {models[1]} | juiz: {judge_model}\n", flush=True)
    dossiers = _build_dossiers(models[0])

    arms = [_run_arm(m, dossiers) for m in models]

    print(f"\n== julgamento (juiz: {judge_model})", flush=True)
    _judge(arms, dossiers, judge_model)

    _summary_table(arms)

    _RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = _RESULTS_DIR / f"model_ab__{stamp}.json"
    out.write_text(json.dumps(
        {"judge": judge_model, "arms": arms}, ensure_ascii=False, indent=2
    ))
    print(f"\nSalvo em {out}")


if __name__ == "__main__":
    main()
