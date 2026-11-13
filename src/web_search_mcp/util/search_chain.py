"""Fonte de links do research_web: Google CSE via Tor, SearXNG de reserva."""

import copy
import logging
import threading
import time

from .. import config
from .google_cse import GoogleCSE, GoogleUnavailable
from .searxng import SearXNG
from .tor import ChannelPool

logger = logging.getLogger(__name__)


class SearchChain:
    """Google primeiro; SearXNG só quando o Google falhou por todos os caminhos.

    health() segue o contrato do SearXNG para o _search_health_note não
    mudar de regra: com o Google respondendo, vazio; quando uma query caiu no
    SearXNG, os motores mortos dele MAIS o Google indisponível. A busca
    degradada continua sendo dita no caminho novo.
    """

    def __init__(
        self,
        google: GoogleCSE,
        fallback: SearXNG | None = None,
        cache_seconds: float = config.SEARCH_CACHE_SECONDS,
    ):
        self.google = google
        self.fallback = fallback
        self.max_results = google.max_results
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._fallbacks = 0
        self._cache: dict[tuple[str, str | None], tuple[float, list[dict]]] = {}

    def search(self, query: str, time_range: str | None = None) -> list[dict]:
        """Busca com cache curto: a mesma query (e o mesmo filtro de data)
        dentro de cache_seconds não sai de novo. Só guarda resposta do
        Google; o que veio do SearXNG de reserva não entra no cache."""
        key = (" ".join(query.lower().split()), time_range)
        if self.cache_seconds > 0:
            with self._lock:
                hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.cache_seconds:
                logger.info("search_chain: query=%r caminho=cache resultados=%d", query, len(hit[1]))
                return copy.deepcopy(hit[1])
        return self._search(query, time_range)

    def _search(self, query: str, time_range: str | None) -> list[dict]:
        key = (" ".join(query.lower().split()), time_range)
        try:
            results = self.google.search(query, time_range=time_range)
            if self.cache_seconds > 0:
                now = time.monotonic()
                with self._lock:
                    self._cache = {k: v for k, v in self._cache.items() if now - v[0] < self.cache_seconds}
                    self._cache[key] = (now, copy.deepcopy(results))
            return results
        except GoogleUnavailable:
            if self.fallback is None:
                raise
            with self._lock:
                self._fallbacks += 1
            logger.warning("search_chain: query=%r caminho=searxng (google indisponível)", query)
            return self.fallback.search(query, time_range=time_range)

    def health(self) -> dict[str, str]:
        down = self.google.health()
        if not down:
            return {}
        if self.fallback is not None:
            down = {**self.fallback.health(), **down}
        return down

    def reset_health(self) -> None:
        self.google.reset_health()
        if self.fallback is not None:
            self.fallback.reset_health()

    def stats(self) -> dict[str, int]:
        with self._lock:
            fallbacks = self._fallbacks
        return {**self.google.stats(), "caminho:searxng": fallbacks}


def start_background(search) -> None:
    """Liga o aquecedor dos canais Tor (só o servidor chama; teste não)."""
    if isinstance(search, SearchChain):
        search.google.start_warmer()


def build_search() -> SearchChain | SearXNG:
    """Instância de busca conforme SEARCH_BACKEND."""
    if config.SEARCH_BACKEND == "searxng":
        return SearXNG()
    return SearchChain(
        GoogleCSE(ChannelPool.from_config()),
        SearXNG() if config.SEARXNG_FALLBACK else None,
    )
