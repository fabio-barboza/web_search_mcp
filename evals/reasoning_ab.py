"""A/B de raciocínio no MESMO modelo: qualidade x latência por configuração.

Uso:
    uv run python -m evals.reasoning_ab [rodadas] [braço ...]
    (padrão: 1 rodada, braços "sem", os quatro "bN" e "t0.6"; modelo = config.MODEL ou o
    que estiver carregado no servidor)

Braços:
    sem    nenhuma chamada raciocina (USE_REASONING=false)
    t0.6   raciocínio sem teto na triagem e no resumo, temperatura 0.6
    t0     raciocínio com temperatura 0, o que rodava até 07/10/2026 e entrou
           em loop no qwen3.6:35B; aqui o prazo do chat() corta se repetir
    bN     raciocínio a 0.6 com teto de N tokens (b256, b512, b1024, b2048),
           para modelo cujo template não tem nível de esforço

Fases:
1. Links — uma busca por pergunta, feita UMA vez. Todos os braços fazem a
   triagem do mesmo pool, e o download de cada página é guardado em memória:
   a diferença medida é da configuração, não do estado da web entre braços.
2. Braços — por pergunta mede triagem+leitura e resumo, separados, porque o
   raciocínio custa nas duas e a calibragem pode querer ligar só uma.
3. Julgamento — juiz fixo (temperatura 0) para todos os braços:
   EVAL_JUDGE_MODEL em EVAL_JUDGE_BASE_URL/EVAL_JUDGE_API_KEY. Sem eles o
   juiz é o próprio modelo avaliado, que tende a se dar razão: aí a nota
   absoluta é otimista e só a comparação entre braços vale. Os resumos ficam
   no JSON para nota cega.

Com mais de uma rodada a ordem dos braços alterna, para o cache de prompt do
servidor não favorecer sempre o mesmo.
"""

import json
import logging
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from web_search_mcp import config
from web_search_mcp.llm import _resolve_model, chat
from web_search_mcp.tools.research import (
    _collect_links,
    _render_dossier,
    _select_and_read,
    _snippet_sources,
    _summarize,
)
from web_search_mcp.util.scraper import WebScraper

from evals.judge import faithfulness, relevance
from evals.questions import QUESTIONS

_RESULTS_DIR = Path(__file__).parent / "results"
_MAX_CLAIMS = 8

_THINK = {"chat_template_kwargs": {"enable_thinking": True}}


def _budget(tokens: int) -> dict:
    """Raciocínio com teto de tokens imposto pelo llama.cpp, por pedido.

    Medido em 07/10/2026 no qwen3.6:35B: o chat template só tem
    enable_thinking, então reasoning_effort (kwarg ou campo de topo) não muda
    nada (7 mil chars de raciocínio com ou sem "low"); reasoning_budget_tokens
    corta de verdade (200 -> 543 chars e a resposta sai).
    """
    return {**_THINK, "reasoning_budget_tokens": tokens}


# use_reasoning, temperatura das chamadas com raciocínio, REASONING_BODY
# (None = o do config)
_ARMS = {
    "sem": (False, None, None),
    "t0.6": (True, 0.6, None),
    "t0": (True, 0.0, None),
    "b256": (True, 0.6, _budget(256)),
    "b512": (True, 0.6, _budget(512)),
    "b1024": (True, 0.6, _budget(1024)),
    "b2048": (True, 0.6, _budget(2048)),
}
_DEFAULT_ARMS = ["sem", "b256", "b512", "b1024", "b2048", "t0.6"]


@contextmanager
def _arm(name: str):
    use_reasoning, temperature, body = _ARMS[name]
    with ExitStack() as stack:
        stack.enter_context(patch.object(config, "USE_REASONING", use_reasoning))
        stack.enter_context(patch.object(config, "REASONING_TEMPERATURE", temperature))
        if body is not None:
            stack.enter_context(patch.object(config, "REASONING_BODY", body))
        yield


# Teto de saída do juiz: modelo de API que raciocina gasta bem mais que os
# 4096 do fusível local antes de responder uma palavra.
_JUDGE_MAX_TOKENS = 16384
# Julgamentos em paralelo quando o juiz está em outro servidor. No mesmo
# servidor dos braços (uma GPU, parallel = 1) paralelismo só enfileira.
_JUDGE_WORKERS = 6


def _remote_judge() -> bool:
    return config.EVAL_JUDGE_BASE_URL != config.MODEL_BASE_URL


@contextmanager
def _judge_config(model: str):
    """Juiz fixo para todos os braços: sem raciocínio pedido, temperatura 0.

    EXTRA_BODY e EXTRA_SYSTEM_PROMPT são do modelo avaliado (ex.
    chat_template_kwargs do Qwen) e não valem para um juiz de outro provider.
    """
    # Antes dos patches: depois deles MODEL_BASE_URL já é a do juiz.
    remote = _remote_judge()
    with ExitStack() as stack:
        for name, value in (
            ("MODEL", config.EVAL_JUDGE_MODEL or model),
            ("MODEL_BASE_URL", config.EVAL_JUDGE_BASE_URL),
            ("MODEL_API_KEY", config.EVAL_JUDGE_API_KEY),
            ("USE_REASONING", False),
            ("MODEL_TEMPERATURE", 0.0),
        ):
            stack.enter_context(patch.object(config, name, value))
        if remote:
            stack.enter_context(patch.object(config, "EXTRA_BODY", {}))
            stack.enter_context(patch.object(config, "EXTRA_SYSTEM_PROMPT", ""))
            stack.enter_context(patch.object(config, "MODEL_MAX_TOKENS", _JUDGE_MAX_TOKENS))
        yield


class _ChatLog(logging.Handler):
    """Soma o que o chat() registrou: chars de raciocínio e cortes por prazo."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.reset()

    def reset(self):
        self.thought = 0
        self.failures = 0

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("chat: model="):
            self.thought += record.args[4]
        elif msg.startswith("chat: falha"):
            self.failures += 1


def _collect() -> list[dict]:
    items = []
    with _arm("sem"):
        for n, q in enumerate(QUESTIONS, 1):
            print(f"[links {n}/{len(QUESTIONS)}] {q['query']}", flush=True)
            items.append({**q, "results": _collect_links(q["query"], q["recent"])})
    return items


def _run_arm(name: str, items: list[dict], log: _ChatLog) -> list[dict]:
    rows = []
    with _arm(name):
        for item in items:
            if not item["results"]:
                continue
            log.reset()
            t0 = time.perf_counter()
            pages_read, unread = _select_and_read(item["query"], item["results"], item["recent"])
            select_s = time.perf_counter() - t0
            if not pages_read:
                rows.append({"query": item["query"], "erro": "sem página lida"})
                continue
            dossier = _render_dossier(pages_read + _snippet_sources(unread))

            t0 = time.perf_counter()
            try:
                summary = _summarize(item["query"], dossier, item["recent"])
            except Exception as e:
                summary = ""
                print(f"   !! resumo falhou: {e}", flush=True)
            summary_s = time.perf_counter() - t0

            print(f"   [{name}] {item['query'][:44]:<44} triagem+leitura {select_s:5.1f}s  "
                  f"resumo {summary_s:5.1f}s  pensou {log.thought:6d} chars  falhas {log.failures}",
                  flush=True)
            rows.append({
                "query": item["query"],
                "triagem_s": select_s,
                "resumo_s": summary_s,
                "total_s": select_s + summary_s,
                "pensou_chars": log.thought,
                "falhas": log.failures,
                "paginas": len(pages_read),
                "sources": [url for _, url, _ in pages_read],
                "dossier": dossier,
                "summary": summary,
            })
    return rows


def _judge_row(job: tuple[dict, dict]) -> None:
    run, row = job
    faith, sup, tot = faithfulness(row["summary"], row["dossier"], max_claims=_MAX_CLAIMS)
    row["faithfulness"] = faith
    row["claims"] = f"{sup}/{tot}"
    row["relevance"] = relevance(row["query"], row["summary"])
    print(f"   juiz [{run['arm']} r{run['round']}] {row['query'][:40]:<40} "
          f"faith {faith:.2f} ({row['claims']}) rel {row['relevance']}", flush=True)


def _judge(runs: list[dict], model: str) -> str:
    jobs = [(run, row) for run in runs for row in run["rows"] if row.get("summary")]
    workers = _JUDGE_WORKERS if _remote_judge() else 1
    with _judge_config(model):
        judge = config.MODEL
        print(f"juiz: {judge} em {config.MODEL_BASE_URL}", flush=True)
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(_judge_row, jobs))
    return judge


def _table(runs: list[dict], arms: list[str]) -> None:
    def rows_of(arm):
        return [r for run in runs if run["arm"] == arm for r in run["rows"]]

    def mean(arm, key):
        values = [r[key] for r in rows_of(arm) if key in r]
        return statistics.mean(values) if values else float("nan")

    print("\n%-26s" % "métrica" + "".join(f" {a:>10}" for a in arms))
    for label, key in (
        ("triagem+leitura média (s)", "triagem_s"),
        ("resumo média (s)", "resumo_s"),
        ("total média (s)", "total_s"),
        ("raciocínio médio (chars)", "pensou_chars"),
        ("faithfulness média", "faithfulness"),
        ("relevance média (0-2)", "relevance"),
    ):
        print("%-26s" % label + "".join(" %10.2f" % mean(a, key) for a in arms))
    print("%-26s" % "pior total (s)"
          + "".join(" %10.2f" % max((r.get("total_s", 0) for r in rows_of(a)), default=0) for a in arms))
    print("%-26s" % "chamadas que falharam"
          + "".join(" %10d" % sum(r.get("falhas", 0) for r in rows_of(a)) for a in arms))
    print("%-26s" % "perguntas sem resumo"
          + "".join(" %10d" % sum(1 for r in rows_of(a) if not r.get("summary")) for a in arms))


def main() -> None:
    args = sys.argv[1:]
    rounds = int(args.pop(0)) if args and args[0].isdigit() else 1
    arms = args or _DEFAULT_ARMS
    unknown = [a for a in arms if a not in _ARMS]
    if unknown:
        sys.exit(f"braço desconhecido: {unknown}; opções: {list(_ARMS)}")

    model = _resolve_model()
    print(f"Modelo: {model} | braços: {arms} | rodadas: {rounds}\n", flush=True)

    log = _ChatLog()
    logging.getLogger("web_search_mcp.llm").addHandler(log)
    logging.getLogger("web_search_mcp.llm").setLevel(logging.INFO)

    downloads: dict[str, tuple] = {}
    download = WebScraper._download

    def cached_download(self, url):
        if url not in downloads:
            downloads[url] = download(self, url)
        return downloads[url]

    runs = []
    with patch.object(config, "MODEL", model), patch.object(WebScraper, "_download", cached_download):
        chat(system="Responda apenas: ok", user="ok")  # paga o load fora do relógio
        items = _collect()
        for rnd in range(1, rounds + 1):
            order = arms if rnd % 2 else arms[::-1]
            for name in order:
                print(f"\n== rodada {rnd}, braço {name}", flush=True)
                runs.append({"arm": name, "round": rnd, "rows": _run_arm(name, items, log)})
        print("\n== julgamento", flush=True)
        judge = _judge(runs, model)

    _table(runs, arms)

    _RESULTS_DIR.mkdir(exist_ok=True)
    out = _RESULTS_DIR / f"reasoning_ab__{model.replace(':', '_')}__{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"model": model, "judge": judge, "rounds": rounds, "runs": runs}, ensure_ascii=False, indent=2))
    print(f"\nSalvo em {out}")


if __name__ == "__main__":
    main()
