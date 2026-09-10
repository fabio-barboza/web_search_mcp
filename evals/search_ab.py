"""A/B da fonte de links (plans/tor.md, seção 6): SearXNG x Google CSE via Tor.

Os braços rodam na mesma sessão, pergunta a pergunta, com as MESMAS
variantes de busca (geradas uma vez por pergunta): a única diferença medida
é a fonte de links. Depois vem a carga contínua, só a fase de busca, sem
pausa, para medir bloqueio e failover. Não é teste de CI.

    uv run python -m evals.search_ab [--load 20] [--hl pt-BR en]
"""

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from evals.questions import QUESTIONS
from web_search_mcp import config
from web_search_mcp.tools import research
from web_search_mcp.util.google_cse import GoogleCSE
from web_search_mcp.util.search_chain import SearchChain
from web_search_mcp.util.searxng import SearXNG
from web_search_mcp.util.tor import ChannelPool

_RESULTS_DIR = Path(__file__).parent / "results"

# As 6 perguntas da medição de 10/09/2026, somadas às do eval: 12 assuntos
# sem relação entre si (câmbio, notícia, biografia, protocolos, saúde, rede,
# culinária, jogo).
_EXTRA = [
    {"query": "Como implementar retry com backoff exponencial em Python?", "recent": False},
    {"query": "Como funciona o algoritmo de consenso Raft?", "recent": False},
    {"query": "Quais os sintomas da deficiência de vitamina B12?", "recent": False},
    {"query": "Como limitar as portas que um usuário pode acessar com ACL no Tailscale?", "recent": False},
    {"query": "Como fazer pão de fermentação natural em casa?", "recent": False},
    {"query": "Como abrir a porta do Sorcerous Vault em Baldur's Gate 3?", "recent": False},
]
_ALL = QUESTIONS + _EXTRA


def _on_topic(query: str, results: list[dict]) -> int:
    """Resultados que têm alguma palavra de conteúdo da pergunta no título
    ou na URL. Métrica aproximada: página certa com título em outras palavras
    conta como fora (medido em 10/09/2026 no google cse: bcb.gov.br/conversao
    para cotação do dólar, auth0 para OAuth2)."""
    tokens = frozenset(t for t in research._content_tokens(query) if len(t) > 1)
    return sum(
        1 for r in results
        if tokens & research._content_tokens(f"{r.get('title', '')} {r.get('url', '')}")
    )


def _measure(search, query: str, recent: bool, queries: list[str], read: bool) -> dict:
    with patch.object(research, "_search", search), \
         patch.object(research, "_generate_queries", return_value=queries):
        start = time.monotonic()
        try:
            results = research._collect_links(query, recent)
        except Exception as e:
            return {"erro": f"{type(e).__name__}: {e}"}
        t_search = time.monotonic() - start
        row = {
            "pool": len(results),
            "on_topic": _on_topic(query, results),
            "t_search": round(t_search, 1),
        }
        if read:
            pages = research._read_pages(results) if results else []
            row["paginas"] = len(pages)
            row["dominios"] = len({urlsplit(u).netloc.lower() for _, u, _ in pages})
            row["t_total"] = round(time.monotonic() - start, 1)
            row["fontes"] = [u for _, u, _ in pages]
    return row


def _summary(rows: list[dict]) -> dict:
    ok = [r for r in rows if "erro" not in r]
    out = {"erros": len(rows) - len(ok), "vazias": sum(1 for r in ok if not r["pool"])}
    if not ok:
        return out
    out["on_topic"] = sum(r["on_topic"] for r in ok)
    out["pool"] = sum(r["pool"] for r in ok)
    out["t_search_p50"] = statistics.median(r["t_search"] for r in ok)
    if "paginas" in ok[0]:
        out["paginas"] = sum(r["paginas"] for r in ok)
        out["dominios"] = sum(r["dominios"] for r in ok)
        out["t_total_p50"] = statistics.median(r["t_total"] for r in ok)
    return out


def _delta(after: dict, before: dict) -> dict:
    return {k: v - before.get(k, 0) for k, v in after.items() if v - before.get(k, 0)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--load", type=int, default=20, help="pesquisas na carga contínua")
    parser.add_argument("--hl", nargs="+", default=["pt-BR", "en"], help="GOOGLE_CSE_HL a comparar")
    args = parser.parse_args()

    print(f"Gerando variantes para {len(_ALL)} perguntas...", flush=True)
    variants = {q["query"]: research._generate_queries(q["query"]) for q in _ALL}

    # Um pool só para os braços Google: é o rodízio real de produção.
    pool = ChannelPool.from_config()
    arms = {"searxng": SearXNG()}
    for hl in args.hl:
        arms[f"google/{hl}"] = SearchChain(GoogleCSE(pool, hl=hl), SearXNG())

    report: dict = {"ab": {name: [] for name in arms}}
    for n, q in enumerate(_ALL, 1):
        for name, search in arms.items():
            row = _measure(search, q["query"], q["recent"], variants[q["query"]], read=True)
            row["query"] = q["query"]
            report["ab"][name].append(row)
            print(f"[{n}/{len(_ALL)}] {name:<12} {q['query'][:50]:<50} "
                  + " ".join(f"{k}={v}" for k, v in row.items() if k not in ("query", "fontes")),
                  flush=True)

    print("\n== A/B de qualidade ==")
    report["ab_resumo"] = {name: _summary(rows) for name, rows in report["ab"].items()}
    for name, s in report["ab_resumo"].items():
        print(f"{name:<12} " + " ".join(f"{k}={v}" for k, v in s.items()))

    # Carga contínua: só busca, sem pausa entre as pesquisas, no HL de produção.
    chain = SearchChain(GoogleCSE(pool, hl=config.GOOGLE_CSE_HL), SearXNG())
    before = chain.stats()
    load_rows = []
    print(f"\n== Carga contínua: {args.load} pesquisas ==", flush=True)
    for i in range(args.load):
        q = _ALL[i % len(_ALL)]
        row = _measure(chain, q["query"], q["recent"], variants[q["query"]], read=False)
        load_rows.append(row)
        print(f"[{i + 1}/{args.load}] {q['query'][:50]:<50} "
              + " ".join(f"{k}={v}" for k, v in row.items()), flush=True)
    stats = _delta(chain.stats(), before)
    report["carga"] = {"linhas": load_rows, "resumo": _summary(load_rows), "stats": stats}

    total = stats.get("consultas", 0)
    via_tor = sum(v for k, v in stats.items() if k.startswith("caminho:tor-"))
    print("\nstats:", stats)
    print(f"consultas={total} via_tor={via_tor} ({100 * via_tor / max(total, 1):.0f}%) "
          f"direto={stats.get('caminho:direto', 0)} searxng={stats.get('caminho:searxng', 0)} "
          f"failover={stats.get('failover', 0)}")
    for ch in pool.channels:
        blocks = stats.get(f"bloqueio:{ch.name}", 0)
        tries = blocks + stats.get(f"caminho:{ch.name}", 0)
        print(f"{ch.name}: {blocks}/{tries} tentativas barradas ({100 * blocks / max(tries, 1):.0f}%)")
    print("resumo carga:", report["carga"]["resumo"])

    _RESULTS_DIR.mkdir(exist_ok=True)
    out = _RESULTS_DIR / f"search_ab__{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nSalvo em {out}")


if __name__ == "__main__":
    main()
