from unittest.mock import MagicMock, patch

import pytest

from web_search_mcp import config
from web_search_mcp.util import tor
from web_search_mcp.util.tor import ChannelPool, TorChannel


def _channel(name="tor-a", password=""):
    return TorChannel(name, "127.0.0.1", 9060, 9061, password)


def _fake_control(replies):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.recv.side_effect = replies
    return conn


class TestConfig:
    def test_parse_channels(self):
        assert config._parse_tor_channels(" 127.0.0.1:9060:9061, tor-b:9050:9051 ,") == [
            ("127.0.0.1", 9060, 9061),
            ("tor-b", 9050, 9051),
        ]

    def test_bad_channel_raises(self):
        with pytest.raises(ValueError, match="host:porta_socks:porta_controle"):
            config._parse_tor_channels("127.0.0.1:9060")

    def test_env_bool(self, monkeypatch):
        monkeypatch.setenv("X_FLAG", "false")
        assert config._env_bool("X_FLAG", True) is False
        monkeypatch.setenv("X_FLAG", "")
        assert config._env_bool("X_FLAG", True) is True


class TestTorChannel:
    def test_proxies_resolve_dns_inside_tor(self):
        p = _channel().proxies()
        assert p["http"] == p["https"]
        assert p["https"].startswith("socks5h://tor-a-")
        assert p["https"].endswith("@127.0.0.1:9060")

    def test_renew_switches_socks_credential(self):
        # Credencial nova = circuito novo na hora (IsolateSOCKSAuth).
        ch = _channel()
        before = ch.proxies()
        ch.renew()
        assert ch.proxies() != before

    def test_renewing_channel_returns_after_cooldown(self):
        ch = _channel()
        with patch.object(tor.time, "monotonic", return_value=100.0):
            ch.renew()
            assert not ch.healthy()
        with patch.object(tor.time, "monotonic", return_value=100.0 + tor._RENEW_COOLDOWN_SECONDS):
            assert ch.healthy()

    def test_mark_ok_returns_to_rotation_early(self):
        ch = _channel()
        ch.renew()
        ch.mark_ok()
        assert ch.healthy()

    def test_newnym_authenticates_with_password_then_signals(self):
        ch = _channel(password='s3"cr\\et')
        conn = _fake_control([b"250 OK\r\n", b"250 OK\r\n"])
        with patch.object(tor.socket, "create_connection", return_value=conn) as connect:
            assert ch.newnym() is True
        assert connect.call_args.args[0] == ("127.0.0.1", 9061)
        sent = [c.args[0] for c in conn.sendall.call_args_list]
        assert sent[0] == b'AUTHENTICATE "s3\\"cr\\\\et"\r\n'
        assert sent[1] == b"SIGNAL NEWNYM\r\n"

    def test_wrong_password_stops_before_newnym(self):
        ch = _channel(password="errada")
        conn = _fake_control([b"515 Authentication failed\r\n"])
        with patch.object(tor.socket, "create_connection", return_value=conn):
            assert ch.newnym() is False
        assert len(conn.sendall.call_args_list) == 1

    def test_control_port_down_does_not_raise(self):
        ch = _channel(password="x")
        with patch.object(tor.socket, "create_connection", side_effect=ConnectionRefusedError()):
            assert ch.newnym() is False

    def test_renew_with_dead_control_port_still_switches_circuit(self):
        ch = _channel(password="x")
        before = ch.proxies()
        with patch.object(tor.socket, "create_connection", side_effect=OSError("down")):
            thread = ch.renew()
            thread.join(2)
        assert not thread.is_alive()
        assert ch.proxies() != before

    def test_renew_without_password_skips_newnym(self):
        ch = _channel()
        with patch.object(tor.socket, "create_connection") as connect:
            assert ch.renew() is None
        connect.assert_not_called()


class TestChannelPool:
    def test_round_robin_between_healthy_channels(self):
        pool = ChannelPool([_channel("tor-a"), _channel("tor-b")])
        assert [pool.pick().name for _ in range(5)] == ["tor-a", "tor-b", "tor-a", "tor-b", "tor-a"]

    def test_renewing_channel_gets_no_new_query(self):
        a, b = _channel("tor-a"), _channel("tor-b")
        pool = ChannelPool([a, b])
        a.renew()
        assert {pool.pick().name for _ in range(4)} == {"tor-b"}

    def test_all_renewing_still_serves(self):
        a, b = _channel("tor-a"), _channel("tor-b")
        pool = ChannelPool([a, b])
        a.renew()
        b.renew()
        assert pool.pick() is not None

    def test_other_prefers_healthy(self):
        a, b, c = _channel("tor-a"), _channel("tor-b"), _channel("tor-c")
        pool = ChannelPool([a, b, c])
        b.renew()
        assert pool.other(a) is c

    def test_other_starts_at_neighbour_not_first(self):
        """Com quatro canais, o failover não pode cair sempre no tor-a."""
        chans = [_channel(f"tor-{x}") for x in "abcd"]
        pool = ChannelPool(chans)
        assert [pool.other(c).name for c in chans] == ["tor-b", "tor-c", "tor-d", "tor-a"]

    def test_other_excludes(self):
        a, b, c = _channel("tor-a"), _channel("tor-b"), _channel("tor-c")
        pool = ChannelPool([a, b, c])
        assert pool.other(a, exclude=(b,)) is c
        assert ChannelPool([a, b]).other(a, exclude=(b,)) is None

    def test_other_with_single_channel(self):
        a = _channel()
        assert ChannelPool([a]).other(a) is None

    def test_empty_pool(self):
        assert ChannelPool([]).pick() is None

    def test_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "TOR_CHANNELS", [("h", 1, 2), ("h", 3, 4)])
        monkeypatch.setattr(config, "TOR_CONTROL_PASSWORD", "pw")
        pool = ChannelPool.from_config()
        assert [c.name for c in pool.channels] == ["tor-a", "tor-b"]
        assert [(c.socks_port, c.control_port) for c in pool.channels] == [(1, 2), (3, 4)]
        assert all(c.password == "pw" for c in pool.channels)
