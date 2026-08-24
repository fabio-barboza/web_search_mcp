from unittest.mock import MagicMock, patch

import pytest
import requests

from web_search_mcp import config
from web_search_mcp import llm


def _mock_response(content="ok"):
    resp = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    resp.raise_for_status.return_value = None
    return resp


def _mock_models_response(data):
    resp = MagicMock()
    resp.json.return_value = {"data": data}
    resp.raise_for_status.return_value = None
    return resp


class TestChat:
    def test_payload_shape_and_auth_header(self):
        # EXTRA_SYSTEM_PROMPT vem do .env do host; zerado aqui para o teste
        # medir só a forma do payload (o append tem teste próprio).
        with patch.object(config, "EXTRA_SYSTEM_PROMPT", ""), \
             patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=_mock_response("resposta")) as post:
            result = llm.chat(system="sys", user="usr")

        assert result == "resposta"
        args, kwargs = post.call_args
        assert args[0] == f"{config.MODEL_BASE_URL}/chat/completions"
        assert kwargs["headers"]["Authorization"] == f"Bearer {config.MODEL_API_KEY}"
        body = kwargs["json"]
        assert body["model"] == "my-model"
        assert body["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]

    def test_temperature_override(self):
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=_mock_response()) as post:
            llm.chat(system="sys", user="usr", temperature=0.7)
        assert post.call_args.kwargs["json"]["temperature"] == 0.7

    def test_default_temperature_from_config(self):
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=_mock_response()) as post:
            llm.chat(system="sys", user="usr")
        assert post.call_args.kwargs["json"]["temperature"] == config.MODEL_TEMPERATURE

    def test_http_error_propagates(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("500 error")
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=resp):
            with pytest.raises(requests.HTTPError):
                llm.chat(system="sys", user="usr")


class TestResolveModel:
    def test_explicit_model_skips_network(self):
        with patch.object(config, "MODEL", "explicit-model"), \
             patch("web_search_mcp.llm.requests.get") as get:
            assert llm._resolve_model() == "explicit-model"
        get.assert_not_called()

    def test_llamacpp_style_status_dict(self):
        data = [
            {"id": "a", "status": {"value": "unloaded"}},
            {"id": "b", "status": {"value": "loaded"}},
        ]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            assert llm._resolve_model() == "b"

    def test_generic_provider_single_model_fallback(self):
        data = [{"id": "only-model"}]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            assert llm._resolve_model() == "only-model"

    def test_generic_provider_ambiguous_raises(self):
        data = [{"id": "a"}, {"id": "b"}]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            with pytest.raises(RuntimeError):
                llm._resolve_model()

    def test_not_cached_requeries_every_call(self):
        # Sem cache de propósito: o modelo carregado no router pode mudar
        # entre chamadas (ver docstring de _resolve_model).
        data = [{"id": "a", "status": {"value": "loaded"}}]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)) as get:
            llm._resolve_model()
            llm._resolve_model()
        assert get.call_count == 2


class TestContextTokens:
    def test_detects_ctx_size_from_llamacpp_args(self):
        data = [{
            "id": "m",
            "status": {"value": "loaded", "args": ["llama-server", "--ctx-size", "131072"]},
        }]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            assert llm.context_tokens() == 131072

    def test_falls_back_when_no_args(self):
        data = [{"id": "m", "status": {"value": "loaded"}}]
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            assert llm.context_tokens() == config.MODEL_CONTEXT_TOKENS

    def test_falls_back_on_network_error(self):
        with patch.object(config, "MODEL", ""), \
             patch("web_search_mcp.llm.requests.get", side_effect=requests.ConnectionError("down")):
            assert llm.context_tokens() == config.MODEL_CONTEXT_TOKENS

    def test_explicit_model_uses_its_entry(self):
        data = [
            {"id": "outro", "status": {"value": "loaded", "args": ["--ctx-size", "8192"]}},
            {"id": "forcado", "status": {"value": "unloaded", "args": ["--ctx-size", "262144"]}},
        ]
        with patch.object(config, "MODEL", "forcado"), \
             patch("web_search_mcp.llm.requests.get", return_value=_mock_models_response(data)):
            assert llm.context_tokens() == 262144
