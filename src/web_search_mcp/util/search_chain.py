"""Fonte de links do research_web: Google CSE via Tor, SearXNG de reserva."""

import logging
import threading

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

    def __init__(self, google: GoogleCSE, fallback: SearXNG | None = None):
        self.google = google
        self.fallback = fallback
        self.max_results = google.max_results
        self._lock = threading.Lock()
        self._fallbacks = 0

    def search(self, query: str, time_range: str | None = None) -> list[dict]:
        try:
            return self.google.search(query, time_range=time_range)
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


def build_search() -> SearchChain | SearXNG:
    """Instância de busca conforme SEARCH_BACKEND."""
    if config.SEARCH_BACKEND == "searxng":
        return SearXNG()
    return SearchChain(
        GoogleCSE(ChannelPool.from_config()),
        SearXNG() if config.SEARXNG_FALLBACK else None,
    )
