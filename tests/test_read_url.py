from unittest.mock import patch

import pytest

import importlib

# tools/__init__.py reexporta a função read_url, e o nome dela sombreia o do
# módulo: o import normal devolveria a função.
mod = importlib.import_module("web_search_mcp.tools.read_url")


def _mock(text, final):
    return patch.object(mod._scraper, "read_many_located", return_value=[(text, final)])


class TestSourceHeader:
    """Todo texto entregue sai com o link da página de onde veio.

    read_url era o caminho por onde a atribuição escapava: o agente lia 21
    páginas e escrevia "ACL policy examples - Tailscale Docs", nome sem
    endereço (observado em 29/08/2026). Sem link no material não há link na
    resposta.
    """

    @pytest.mark.parametrize("url,host", [
        ("https://tailscale.com/docs/features/access-control", "tailscale.com"),
        ("https://www.bbc.com/news/world-123", "bbc.com"),
        ("http://bg3.wiki/wiki/Gloomstalker", "bg3.wiki"),
        ("https://pt.wikipedia.org/wiki/Santos_Dumont", "pt.wikipedia.org"),
    ])
    def test_link_pronto_para_citar(self, url, host):
        with _mock("conteúdo da página " * 50, url):
            out = mod.read_url(url)
        assert f"Fonte desta página: {url}" in out
        assert f"[{host}]({url})" in out
        assert "conteúdo da página" in out

    def test_cabecalho_usa_a_url_final_apos_redirect(self):
        pedida = "https://tailscale.com/kb/9999/nao-existe"
        final = "https://tailscale.com/docs"
        with _mock("conteúdo " * 50, final):
            out = mod.read_url(pedida)
        assert "Fonte desta página: https://tailscale.com/docs" in out
        assert f"[tailscale.com]({final})" in out
        # o aviso de redirect continua, e cita a pedida só para dizer que caiu
        assert "não existe ou foi movida" in out
        assert f"Fonte desta página: {pedida}" not in out

    def test_falha_nao_ganha_cabecalho(self):
        """Página que não abriu não vira fonte citável."""
        with _mock("(não foi possível ler a página: 403)", "http://x.com"):
            out = mod.read_url("http://x.com")
        assert "Fonte desta página" not in out
        assert out.startswith("(não foi possível ler")
