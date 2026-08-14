"""A/B do eval: comportamento antigo de seleção de fonte contra o novo.

Roda o mesmo conjunto de perguntas duas vezes na mesma sessão, para que as
duas medidas peguem a web no mesmo estado — comparar com um resultado salvo
de outro dia não diz nada, porque as páginas mudam.

"Antes" restaura por patch as duas decisões que foram trocadas:
  - página curta contava como leitura boa (sem piso de tamanho)
  - merge usava a ordem crua do SearXNG (sem concordância nem score)
"""

import statistics
from unittest.mock import patch

from evals.run import _MIN_USEFUL_CHARS, run
from web_search_mcp.util.scraper import WebScraper

from web_search_mcp import config
from web_search_mcp.tools import research


def _merge_antigo(per_query: list[list[dict]]) -> list[dict]:
    """Round-robin puro por posição, como era antes."""
    merged: list[dict] = []
    seen: set[str] = set()
    for i in range(research._search.max_results):
        for results in per_query:
            if i >= len(results):
                continue
            url = results[i].get("url", "")
            if url and url not in seen:
                merged.append(results[i])
                seen.add(url)
    return merged[: config.RESEARCH_POOL_SIZE]


def _resumo(rows: list[dict]) -> dict:
    ok = [r for r in rows if "erro" not in r]
    if not ok:
        return {}
    paginas = sum(r["paginas"] for r in ok)
    return {
        "faithfulness": statistics.mean(r["faithfulness"] for r in ok),
        "relevance": statistics.mean(r["relevance"] for r in ok),
        "paginas": paginas,
        "vazias": sum(r["paginas_vazias"] for r in ok),
        "dossie_medio": statistics.mean(r["dossie_chars"] for r in ok),
    }


def _braco(nome: str, rodar) -> dict:
    """Roda um braço e imprime o resultado dele na hora.

    Na hora, e não no fim, porque cada braço leva minutos: guardar tudo para
    imprimir junto significa que uma falha no segundo braço apaga também o
    resultado do primeiro, que já estava pronto e pago.
    """
    print("=" * 70 + f"\n{nome}\n" + "=" * 70, flush=True)
    try:
        resumo = _resumo(rodar())
    except Exception as e:
        print(f"!! braço {nome!r} falhou: {e}", flush=True)
        return {}
    print(f"\n>> {nome}: " + ", ".join(f"{k}={v:.2f}" for k, v in resumo.items()), flush=True)
    return resumo


def main() -> None:
    def antes_run():
        with patch.object(WebScraper, "unusable", staticmethod(WebScraper.failed)), \
             patch.object(research, "_merge_results", _merge_antigo):
            return run()

    antes = _braco("ANTES (sem piso de tamanho, merge por posição)", antes_run)
    depois = _braco(
        f"DEPOIS (piso de {_MIN_USEFUL_CHARS} chars, merge por concordância+score)", run
    )

    print("\n%-16s %10s %10s" % ("métrica", "antes", "depois"))
    for k in ("faithfulness", "relevance", "paginas", "vazias", "dossie_medio"):
        print("%-16s %10.2f %10.2f" % (k, antes.get(k, 0), depois.get(k, 0)))


if __name__ == "__main__":
    main()
