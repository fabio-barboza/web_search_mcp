"""Google CSE como fonte de links, pelos canais Tor (plans/tor.md).

Não é API documentada: é o endpoint que o widget de busca do CSE usa, o
mesmo do searx/engines/google_cse.py. Se o formato mudar, o parse falha
alto, a query vira GoogleUnavailable e o SearchChain cai no SearXNG.
"""

import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta

import requests

from .. import config
from .tor import ChannelPool, TorChannel

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://www.google.com/cse/cse.js"
_SEARCH_URL = "https://cse.google.com/cse/element/v1"
_TOKEN_TTL_SECONDS = 3600  # o SearXNG guarda o token pelo mesmo tempo
_PAGE_SIZE = 20
_DIRECT = "direto"

# UA medido em 10/09/2026: 7 de 8 circuitos Tor novos com 20 resultados.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "*/*",
    "Referer": "https://cse.google.com/",
}
_COOKIES = {"CONSENT": "YES+"}
_TIME_RANGE_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}


class GoogleUnavailable(requests.RequestException):
    """O Google barrou a query por todos os caminhos (Tor e direto).

    É RequestException para quem já trata falha de busca por aí (o
    research_web) continuar tratando sem saber qual backend estava ativo.
    """


class _PathFailed(Exception):
    """Este caminho (canal ou direto) não serve agora: tente o próximo."""


def _session_get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """Ponto único de saída HTTP (os testes trocam este)."""
    return session.get(url, **kwargs)


class _BadToken(Exception):
    """O CSE recusou o token. Medido em 10/09/2026: token inválido ou vazio
    volta como HTTP 200 + error.code 403 "Unauthorized access to internal
    API" — por isso 403 no JSON renova o token antes de trocar de canal."""


def parse_token(js: str) -> dict[str, str]:
    """Opções do cse.js: o JSON entre o último "({" e o último "});"."""
    start, end = js.rfind("({"), js.rfind("});")
    if start < 0 or end < start:
        raise ValueError("cse.js sem o bloco de opções")
    opts = json.loads(js[start + 1 : end + 1])
    token = opts.get("cse_token")
    if not token:
        raise ValueError("cse.js sem cse_token")
    exp = opts.get("exp") or ""
    return {
        "cse_tok": token,
        "cselibv": opts.get("cselibVersion", ""),
        "exp": ",".join(exp) if isinstance(exp, list) else str(exp),
    }


def parse_jsonp(text: str) -> dict:
    """`_({...});` -> dict."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("resposta sem JSON")
    return json.loads(text[start : end + 1])


def date_sort(time_range: str, now: datetime | None = None) -> str | None:
    """day/week/month/year -> "date:r:AAAAMMDD:AAAAMMDD" (conta do google_cse.py)."""
    days = _TIME_RANGE_DAYS.get(time_range)
    if days is None:
        return None
    end = now or datetime.now()
    start = end - timedelta(days=days)
    return f"date:r:{start:%Y%m%d}:{end:%Y%m%d}"


class GoogleCSE:
    """Mesma interface pública do SearXNG (search, max_results, health,
    reset_health): o research_web troca a instância sem mexer no pipeline."""

    def __init__(
        self,
        pool: ChannelPool,
        cx: str = config.GOOGLE_CSE_CX,
        hl: str = config.GOOGLE_CSE_HL,
        timeout: int = config.GOOGLE_CSE_TIMEOUT,
        direct_fallback: bool = config.GOOGLE_CSE_DIRECT_FALLBACK,
        max_results: int = config.SEARXNG_MAX_RESULTS,
        hedge_seconds: float = config.GOOGLE_CSE_HEDGE_SECONDS,
    ):
        self.pool = pool
        self.cx = cx
        self.hl = hl
        self.timeout = timeout
        self.direct_fallback = direct_fallback
        self.max_results = max_results
        self._lock = threading.Lock()
        self._token: dict[str, str] | None = None
        self._token_expires = 0.0
        self._down: str | None = None
        self._stats: Counter = Counter()
        self.hedge_seconds = hedge_seconds
        # Uma sessão por caminho, trocada quando a credencial SOCKS muda: a
        # conexão (circuito + TLS) fica aberta entre as buscas.
        self._sessions: dict[str, tuple[str, requests.Session]] = {}
        # Tentativas rodam aqui, não na thread de quem busca: a irmã de uma
        # tentativa lenta precisa sair sem esperar a primeira terminar, e a
        # perdedora termina em segundo plano sem segurar a resposta.
        self._attempts = ThreadPoolExecutor(max_workers=32, thread_name_prefix="cse")
        self._warm_seconds = 0.0

    def search(self, query: str, time_range: str | None = None) -> list[dict]:
        """Sequência por query, sem sleep em nenhum passo:

        1. canal da vez; 2. barrou -> renova ele (em fundo) e refaz no OUTRO,
        na hora; 3. o outro também barrou -> renova e última tentativa no
        primeiro, já com circuito novo; 4. CSE pelo IP da máquina (se
        permitido); 5. GoogleUnavailable, e quem chamou cai no SearXNG.

        Tentativa Tor que passa de hedge_seconds sem responder ganha a
        próxima do roteiro em paralelo, e vale a primeira que voltar com
        resultado. O direto nunca corre em paralelo: só entra quando todas as
        tentativas Tor falharam.

        200 com zero resultados é "nada achado", não bloqueio: devolve [].
        """
        tor_route: list[TorChannel] = []
        first = self.pool.pick()
        if first is not None:
            # Três tentativas Tor em canais distintos quando há canais para
            # isso; com dois, a terceira volta ao primeiro, já renovado.
            second = self.pool.other(first) or first
            third = self.pool.other(first, exclude=(second,)) or first
            tor_route = [first, second, third]

        self._count("consultas")
        failures: list[str] = []
        pending: dict = {}
        queue: list[TorChannel | None] = list(tor_route)
        direct_left = self.direct_fallback

        def launch(channel: TorChannel | None) -> None:
            pending[self._attempts.submit(self._attempt, channel, query, time_range)] = channel

        while True:
            if not pending:
                if queue:
                    launch(queue.pop(0))
                elif direct_left:
                    direct_left = False
                    launch(None)
                else:
                    break
            hedge = self.hedge_seconds if (queue and self.hedge_seconds > 0) else None
            done, _ = wait(list(pending), timeout=hedge, return_when=FIRST_COMPLETED)
            if not done:
                # Ninguém respondeu no prazo: a próxima do roteiro corre junto.
                self._count("hedge")
                launch(queue.pop(0))
                continue
            for fut in done:
                channel = pending.pop(fut)
                name = channel.name if channel else _DIRECT
                try:
                    results = fut.result()
                except _PathFailed as e:
                    failures.append(f"{name}: {e}")
                    if pending and queue:
                        # Outra ainda corre; a vaga da que barrou não espera o prazo.
                        launch(queue.pop(0))
                    continue
                self._count(f"caminho:{name}")
                if failures:
                    self._count("failover")
                logger.info(
                    "google_cse: query=%r caminho=%s tentativas=%d bloqueios=%s resultados=%d",
                    query, name, len(failures) + 1, failures or "-", len(results),
                )
                return results

        reason = "; ".join(failures) or "nenhum caminho configurado"
        with self._lock:
            self._down = "indisponível via Tor e direto"
        self._count("indisponivel")
        logger.error("google_cse: indisponível para query=%r: %s", query, reason)
        raise GoogleUnavailable(f"Google CSE indisponível ({reason})")

    def _attempt(self, channel: TorChannel | None, query: str, time_range: str | None) -> list[dict]:
        """Uma tentativa num caminho, com o efeito no canal: passou -> volta
        ao rodízio; barrou -> circuito novo (e aquecido, se o aquecedor
        estiver ligado). Roda também para a perdedora de uma corrida."""
        name = channel.name if channel else _DIRECT
        try:
            results = self._search_via(channel, query, time_range)
        except _PathFailed:
            self._count(f"bloqueio:{name}")
            if channel is not None:
                channel.renew()
                if self._warm_seconds > 0:
                    self._attempts.submit(self._warm, channel)
            raise
        if channel is not None:
            channel.mark_ok()
        return results

    def _search_via(self, channel: TorChannel | None, query: str, time_range: str | None) -> list[dict]:
        proxies = channel.proxies() if channel else None
        name = channel.name if channel else _DIRECT
        token = self._get_token(proxies, force=False)
        try:
            return self._request(proxies, token, query, time_range, name)
        except _BadToken as e:
            self._drop_token(token)
            logger.info("google_cse: token recusado via %s, buscando outro: %s", name, e)
        token = self._get_token(proxies, force=True)
        try:
            return self._request(proxies, token, query, time_range, name)
        except _BadToken as e:
            self._drop_token(token)
            raise _PathFailed(f"token recusado mesmo renovado ({e})") from e

    def _session(self, proxies: dict | None) -> requests.Session:
        """Sessão do caminho. Credencial nova (canal renovado) = sessão nova:
        a conexão velha saía pelo circuito barrado."""
        key_url = proxies["https"] if proxies else _DIRECT
        path = key_url.split("@", 1)[-1]  # host:porta do canal, sem a credencial
        with self._lock:
            current = self._sessions.get(path)
            if current and current[0] == key_url:
                return current[1]
            session = requests.Session()
            if proxies:
                session.proxies.update(proxies)
            self._sessions[path] = (key_url, session)
        if current:
            current[1].close()
        return session

    def _get(self, url: str, params: dict, proxies: dict | None) -> requests.Response:
        try:
            resp = _session_get(
                self._session(proxies), url, params=params, headers=_HEADERS, cookies=_COOKIES,
                proxies=proxies, timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise _PathFailed(f"rede: {type(e).__name__}") from e
        if not resp.ok:
            raise _PathFailed(f"HTTP {resp.status_code}")
        return resp

    def _warm(self, channel: TorChannel) -> None:
        """Abre circuito e TLS do canal até o Google pedindo só o cse.js.
        Nunca levanta: aquecer é otimização, não pode quebrar nada."""
        proxies = channel.proxies()
        try:
            text = self._get(_TOKEN_URL, {"cx": self.cx}, proxies).text
            token = parse_token(text)
        except Exception as e:  # noqa: BLE001
            logger.info("google_cse: aquecimento de %s falhou: %s", channel.name, e)
            return
        with self._lock:
            self._token, self._token_expires = token, time.monotonic() + _TOKEN_TTL_SECONDS

    def start_warmer(self, every_seconds: float = config.GOOGLE_CSE_WARM_SECONDS) -> threading.Thread | None:
        """Aquece todos os canais agora e de novo a cada every_seconds, e
        liga o aquecimento do canal logo depois de cada renovação."""
        if every_seconds <= 0 or not self.pool.channels:
            return None
        self._warm_seconds = every_seconds

        def loop() -> None:
            while True:
                for channel in self.pool.channels:
                    self._attempts.submit(self._warm, channel)
                time.sleep(every_seconds)

        thread = threading.Thread(target=loop, name="cse-warmer", daemon=True)
        thread.start()
        return thread

    def _get_token(self, proxies: dict | None, force: bool) -> dict[str, str]:
        with self._lock:
            if not force and self._token and time.monotonic() < self._token_expires:
                return self._token
        # Pego pelo mesmo caminho da busca: se o caminho está barrado, o
        # cse.js também vem barrado, e isso conta como tentativa dele.
        text = self._get(_TOKEN_URL, {"cx": self.cx}, proxies).text
        try:
            token = parse_token(text)
        except ValueError as e:
            raise _PathFailed(_unparseable(text, e)) from e
        with self._lock:
            self._token, self._token_expires = token, time.monotonic() + _TOKEN_TTL_SECONDS
        return token

    def _drop_token(self, token: dict[str, str]) -> None:
        # Só se ainda for o mesmo: outra thread pode já ter trazido um novo.
        with self._lock:
            if self._token is token:
                self._token = None

    def _request(self, proxies, token, query, time_range, name) -> list[dict]:
        params = {
            "rsz": "filtered_cse",
            "num": str(_PAGE_SIZE),
            "hl": self.hl,
            "cselibv": token["cselibv"],
            "cx": self.cx,
            "q": query,
            "safe": "off",
            "cse_tok": token["cse_tok"],
            "callback": "_",
            "rurl": "",
        }
        if token["exp"]:
            params["exp"] = token["exp"]
        if time_range and (sort := date_sort(time_range)):
            params["sort"] = sort

        text = self._get(_SEARCH_URL, params, proxies).text
        try:
            data = parse_jsonp(text)
        except ValueError as e:
            raise _PathFailed(_unparseable(text, e)) from e

        # O bloqueio vem como HTTP 200 com o erro no JSON, não só como status.
        # Só o campo de erro decide: procurar "unusual traffic" no corpo
        # inteiro confundiria bloqueio com resultado cujo snippet fala disso.
        if error := data.get("error"):
            if not isinstance(error, dict):
                error = {"message": error}
            code = error.get("code")
            message = str(error.get("message", ""))[:120]
            if code == 403:
                raise _BadToken(message)
            raise _PathFailed(f"erro {code}: {message}")

        results = []
        for item in data.get("results") or []:
            url = item.get("unescapedUrl")
            if not url:
                continue
            results.append({
                "url": url,
                "title": item.get("titleNoFormatting", ""),
                "content": item.get("contentNoFormatting", ""),
                "engines": [f"google cse ({name})"],
                # Decrescente pela posição: o _merge_results usa o score como
                # desempate dentro de cada busca.
                "score": 1.0 / (len(results) + 1),
            })
        return results

    def _count(self, key: str) -> None:
        with self._lock:
            self._stats[key] += 1

    def stats(self) -> dict[str, int]:
        """Contadores desde o início do processo, para o eval de carga."""
        with self._lock:
            return dict(self._stats)

    def health(self) -> dict[str, str]:
        with self._lock:
            return {"google cse": self._down} if self._down else {}

    def reset_health(self) -> None:
        with self._lock:
            self._down = None


def _unparseable(text: str, error: Exception) -> str:
    """Motivo legível para resposta fora do formato (página /sorry/, HTML)."""
    if "unusual traffic" in text.lower():
        return "unusual traffic"
    return f"resposta fora do formato: {error}"
