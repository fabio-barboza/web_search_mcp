"""Recall de manchetes contra uma baseline fixa e reprodutível.

Uso:
    uv run python -m evals.headlines

A régua não é outro LLM: são os feeds RSS do Google News (top Brasil e
World). Mede quantas das manchetes do momento o panorama do research_web
recupera. Reprodutível a qualquer hora, sem depender de comparação com um
assistente externo — mede o que dá para melhorar (fan-out, ranking de
fonte), não diferença de orçamento de hardware.

A nota absoluta importa menos que a comparação entre rodadas: os feeds
mudam ao longo do dia, então compare execuções próximas no tempo.
"""

import xml.etree.ElementTree as ET

import requests

from web_search_mcp.llm import chat
from web_search_mcp.tools.research import research_web

_FEEDS = {
    "Brasil": "https://news.google.com/rss?hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "Mundo": "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
}
_PER_FEED = 8

_QUERY = "Faça um resumo das principais noticias no Brasil e no mundo hoje?"

_JUDGE = (
    "Você compara uma manchete com um resumo de notícias. Responda apenas "
    "SIM se o resumo cobre o MESMO acontecimento da manchete (mesmo fato, "
    "ainda que com outras palavras ou menos detalhe), ou NÃO caso "
    "contrário. Nada além de SIM ou NÃO."
)


def _headlines(url: str, n: int) -> list[str]:
    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    titles = [item.findtext("title", "") for item in root.iter("item")]
    # Google News anexa " - Veículo" ao título; fica, ajuda o juiz a situar.
    return [t for t in titles if t][:n]


def main() -> None:
    baseline = {name: _headlines(url, _PER_FEED) for name, url in _FEEDS.items()}
    for name, hs in baseline.items():
        print(f"[baseline {name}] {len(hs)} manchetes")

    print(f"\nrodando research_web: {_QUERY!r}", flush=True)
    summary = research_web(_QUERY, recent=True)

    total_hit = total = 0
    for name, hs in baseline.items():
        hits = []
        for h in hs:
            verdict = chat(system=_JUDGE, user=f"Manchete: {h}\n\nResumo:\n{summary}")
            ok = "SIM" in verdict.upper() and "NÃO" not in verdict.upper()
            hits.append(ok)
            print(f"  [{'✓' if ok else '✗'}] {name}: {h[:90]}")
        total_hit += sum(hits)
        total += len(hits)
        print(f">> recall {name}: {sum(hits)}/{len(hits)}")
    print(f"\nRECALL TOTAL: {total_hit}/{total} ({100 * total_hit / max(total, 1):.0f}%)")


if __name__ == "__main__":
    main()
