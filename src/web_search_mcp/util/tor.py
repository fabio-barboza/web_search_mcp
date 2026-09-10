"""Canais Tor da busca (plans/tor.md).

Cada canal é um container tor independente (tor-a, tor-b). O que troca o IP
de saída na hora é a credencial SOCKS: os containers rodam com
IsolateSOCKSAuth, então usuário/senha novos = circuito novo, sem esperar
nada. O SIGNAL NEWNYM pelo ControlPort é só reforço — o tor ignora o
segundo pedido dentro de 10 s, e falha nele nunca pode quebrar a busca.
"""

import logging
import secrets
import socket
import threading
import time

from .. import config

logger = logging.getLogger(__name__)

# Canal barrado sai do rodízio por esta janela, a mesma em que o tor ignora
# NEWNYM repetido. Não é espera de ninguém: a query barrada já refez no outro
# canal, e se todos estiverem nesta janela o rodízio usa todos mesmo assim.
_RENEW_COOLDOWN_SECONDS = 10.0
_CONTROL_TIMEOUT_SECONDS = 5.0


class TorChannel:
    def __init__(
        self,
        name: str,
        host: str,
        socks_port: int,
        control_port: int,
        password: str = "",
    ):
        self.name = name
        self.host = host
        self.socks_port = socks_port
        self.control_port = control_port
        self.password = password
        # O nonce separa este processo de um anterior: sem ele, o "tor-a-0"
        # de depois de um restart reusaria o circuito que já estava barrado.
        self._nonce = secrets.token_hex(4)
        self._generation = 0
        self._renewing_until = 0.0
        self._lock = threading.Lock()

    def proxies(self) -> dict[str, str]:
        """Proxies para o requests. socks5h: o DNS também resolve no Tor."""
        with self._lock:
            cred = f"{self.name}-{self._nonce}-{self._generation}"
        url = f"socks5h://{cred}:x@{self.host}:{self.socks_port}"
        return {"http": url, "https": url}

    def healthy(self) -> bool:
        """False durante a janela de renovação depois de um bloqueio."""
        with self._lock:
            return time.monotonic() >= self._renewing_until

    def mark_ok(self) -> None:
        """Uma tentativa passou: o canal volta ao rodízio antes do prazo."""
        with self._lock:
            self._renewing_until = 0.0

    def renew(self) -> threading.Thread | None:
        """Circuito novo agora (credencial nova) e NEWNYM em segundo plano.

        Devolve a thread do NEWNYM (None sem senha) só para teste poder
        esperar por ela; quem busca nunca espera.
        """
        with self._lock:
            self._generation += 1
            self._renewing_until = time.monotonic() + _RENEW_COOLDOWN_SECONDS
        if not self.password:
            return None
        thread = threading.Thread(target=self.newnym, name=f"newnym-{self.name}", daemon=True)
        thread.start()
        return thread

    def newnym(self) -> bool:
        """AUTHENTICATE + SIGNAL NEWNYM pelo ControlPort. Nunca levanta."""
        secret = self.password.replace("\\", "\\\\").replace('"', '\\"')
        try:
            with socket.create_connection(
                (self.host, self.control_port), timeout=_CONTROL_TIMEOUT_SECONDS
            ) as sock:
                for verb, command in (
                    ("AUTHENTICATE", f'AUTHENTICATE "{secret}"'),
                    ("SIGNAL NEWNYM", "SIGNAL NEWNYM"),
                ):
                    sock.sendall(command.encode() + b"\r\n")
                    reply = sock.recv(1024).decode(errors="replace").strip()
                    if not reply.startswith("250"):
                        # Só o verbo vai para o log: o comando carrega a senha.
                        logger.warning("tor %s: %s recusado: %s", self.name, verb, reply)
                        return False
                sock.sendall(b"QUIT\r\n")
            return True
        except OSError as e:
            logger.warning("tor %s: ControlPort %s:%d falhou: %s",
                           self.name, self.host, self.control_port, e)
            return False


class ChannelPool:
    """Rodízio dos canais entre as queries. Thread-safe: as queries de uma
    pesquisa rodam em paralelo num ThreadPoolExecutor."""

    def __init__(self, channels: list[TorChannel]):
        self.channels = list(channels)
        self._next = 0
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls) -> "ChannelPool":
        if config.TOR_CHANNELS and not config.TOR_CONTROL_PASSWORD:
            logger.warning(
                "TOR_CONTROL_PASSWORD vazia: NEWNYM desligado; o canal barrado "
                "troca de circuito só pela credencial SOCKS nova"
            )
        return cls([
            TorChannel(f"tor-{chr(ord('a') + i)}", host, socks, control,
                       config.TOR_CONTROL_PASSWORD)
            for i, (host, socks, control) in enumerate(config.TOR_CHANNELS)
        ])

    def __len__(self) -> int:
        return len(self.channels)

    def pick(self) -> TorChannel | None:
        """Próximo canal do rodízio (A, B, A, B...) entre os saudáveis.

        Um contador só, e não o índice da query: o pipeline não precisa saber
        que existe canal, e duas buscas simultâneas nunca saem do mesmo canal
        enquanto houver outro saudável.
        """
        with self._lock:
            if not self.channels:
                return None
            live = [c for c in self.channels if c.healthy()] or self.channels
            channel = live[self._next % len(live)]
            self._next += 1
            return channel

    def other(self, channel: TorChannel) -> TorChannel | None:
        """Outro canal para o failover, saudável de preferência."""
        rest = [c for c in self.channels if c is not channel]
        if not rest:
            return None
        return ([c for c in rest if c.healthy()] or rest)[0]
