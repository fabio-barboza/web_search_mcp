"""Roda o eval contra a web e o LLM reais. Não é teste de CI: a web muda
entre execuções, não existe verde/vermelho estável."""

import json
from datetime import datetime
from pathlib import Path

from web_search_mcp import config
from evals.judge import faithfulness, relevance
from evals.questions import QUESTIONS
from web_search_mcp.tools.research import _build_dossier, _summarize

_RESULTS_DIR = Path(__file__).parent / "results"

# Abaixo disto a página não tem conteúdo aproveitável: é login, muro de
# cookie ou casca que sobreviveu à extração. Ela ainda assim gastou uma
# vaga do RESEARCH_PAGE_BUDGET.
_MIN_USEFUL_CHARS = 600


def run() -> list[dict]:
    rows = []
    for n, item in enumerate(QUESTIONS, 1):
        query, recent = item["query"], item["recent"]
        # O eval leva minutos (uma chamada de LLM por afirmação julgada) e só
        # imprimia a tabela no fim: sem isto não dá para saber se está andando
        # ou travado, e uma execução interrompida não deixava nada.
        print(f"[{n}/{len(QUESTIONS)}] {query}", flush=True)
        dossier, pages_read = _build_dossier(query, recent)
        if not dossier:
            rows.append({"query": query, "recent": recent, "erro": "sem dossiê"})
            continue

        summary = _summarize(query, dossier, recent)
        score, supported, total = faithfulness(summary, dossier)
        rel = relevance(query, summary)

        # Faithfulness mede o resumo contra o dossiê, então um dossiê ruim
        # com resumo fiel tira nota alta. Estas medidas olham o dossiê em si:
        # quantas páginas vieram vazias e quanto do material o modelo teve
        # que ler. Sem elas, melhorar a seleção de fonte é invisível no eval.
        sizes = [len(page) for _, _, page in pages_read]
        rows.append(
            {
                "query": query,
                "recent": recent,
                "faithfulness": score,
                "supported": supported,
                "total_claims": total,
                "relevance": rel,
                "paginas": len(pages_read),
                "paginas_vazias": sum(1 for n in sizes if n < _MIN_USEFUL_CHARS),
                "dossie_chars": len(dossier),
                "sources": [url for _, url, _ in pages_read],
            }
        )
    return rows


def _print_table(rows: list[dict]) -> None:
    print(f"{'pergunta':<44} {'faith':>6} {'rel':>4} {'pgs':>4} {'vazias':>7} {'dossiê':>8}")
    faith_scores, empty, total_pages = [], 0, 0
    for r in rows:
        if "erro" in r:
            print(f"{r['query'][:44]:<44} {'ERRO':>6}")
            continue
        faith_scores.append(r["faithfulness"])
        empty += r["paginas_vazias"]
        total_pages += r["paginas"]
        print(f"{r['query'][:44]:<44} {r['faithfulness']:>6.2f} {r['relevance']:>4} "
              f"{r['paginas']:>4} {r['paginas_vazias']:>7} {r['dossie_chars']:>8}")
    if faith_scores:
        print(f"\nMédia faithfulness: {sum(faith_scores) / len(faith_scores):.2f}")
        print(f"Páginas vazias: {empty}/{total_pages} "
              f"({100 * empty / max(total_pages, 1):.0f}% do orçamento desperdiçado)")


def main() -> None:
    rows = run()
    _print_table(rows)

    _RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _RESULTS_DIR / f"{config.MODEL}__{timestamp}.json"
    out_path.write_text(json.dumps({"model": config.MODEL, "results": rows}, ensure_ascii=False, indent=2))
    print(f"\nSalvo em {out_path}")


if __name__ == "__main__":
    main()
