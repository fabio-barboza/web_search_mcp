import threading

import requests

from .. import config


class SearXNG:
    """Cliente do SearXNG. Não é uma tool: é chamado direto pelo Python."""

    def __init__(
        self,
        base_url: str = config.SEARXNG_URL,
        max_results: int = config.SEARXNG_MAX_RESULTS,
        timeout: int = config.SEARXNG_TIMEOUT,
        language: str = config.SEARXNG_LANGUAGE,
        categories: str = config.SEARXNG_CATEGORIES,
    ):
        self.base_url = base_url
        self.max_results = max_results
        self.timeout = timeout
        self.language = language
        self.categories = categories
        self._lock = threading.Lock()
        self._unresponsive: dict[str, str] = {}

    def search(self, query: str, time_range: str | None = None) -> list[dict]:
        """Devolve os resultados crus do SearXNG (título, url, content).

        time_range: day, week, month ou year. None = sem filtro de data.
        Filtrar por data zera a busca em temas históricos, então só use
        quando a pergunta pedir dado recente.
        """
        params = {
            "q": query,
            "format": "json",
            "language": self.language,
            "safesearch": 0,
            "categories": self.categories,
        }
        if time_range:
            params["time_range"] = time_range

        response = requests.get(
            f"{self.base_url}/search",
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        self._note_health(payload)
        return payload.get("results", [])[: self.max_results]

    def _note_health(self, payload: dict) -> None:
        """Registra os motores que não responderam nesta busca.

        Motor suspenso (CAPTCHA, limite de taxa) não é erro: o SearXNG
        responde 200 com o que sobrou. Quando sobra pouco, a busca devolve
        ruído de marca em vez de resultado, e quem chamou não tem como
        distinguir isso de "o assunto não existe na web" — então repete a
        busca sem parar. O acúmulo aqui é o que permite dizer a verdade.
        """
        with self._lock:
            for entry in payload.get("unresponsive_engines") or []:
                if isinstance(entry, (list, tuple)) and entry:
                    reason = str(entry[1]) if len(entry) > 1 else ""
                    self._unresponsive[str(entry[0])] = reason

    def health(self) -> dict[str, str]:
        """Motores fora do ar desde o último reset_health(): nome -> motivo."""
        with self._lock:
            return dict(self._unresponsive)

    def reset_health(self) -> None:
        with self._lock:
            self._unresponsive.clear()
