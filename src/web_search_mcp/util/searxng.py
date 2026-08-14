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
        return response.json().get("results", [])[: self.max_results]
