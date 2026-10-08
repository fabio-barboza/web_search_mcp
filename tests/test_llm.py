import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from web_search_mcp import config
from web_search_mcp import llm


def _sse(*events):
    lines = [b"data: " + json.dumps(e).encode() for e in events]
    return lines + [b"data: [DONE]"]


def _delta(content=None, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return {"choices": [{"delta": delta, "finish_reason": finish}]}


def _mock_response(content="ok", lines=None):
    """Resposta em streaming (SSE), como o chat() pede."""
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.ok = True
    resp.headers = {"Content-Type": "text/event-stream"}
    resp.iter_lines.return_value = lines if lines is not None else _sse(_delta(content), _delta(finish="stop"))
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
        resp = _mock_response()
        resp.ok = False
        resp.raise_for_status.side_effect = requests.HTTPError("500 error")
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=resp):
            with pytest.raises(requests.HTTPError):
                llm.chat(system="sys", user="usr")


class TestChatStream:
    def _chat(self, resp, **kwargs):
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=resp) as post:
            return llm.chat(system="sys", user="usr", **kwargs), post

    def test_asks_for_stream_and_joins_deltas(self):
        resp = _mock_response(lines=_sse(_delta("um "), _delta("dois"), _delta(finish="stop")))
        result, post = self._chat(resp)
        assert result == "um dois"
        assert post.call_args.kwargs["stream"] is True
        assert post.call_args.kwargs["json"]["stream"] is True

    def test_reasoning_deltas_stay_out_of_the_answer(self):
        resp = _mock_response(lines=_sse(_delta(reasoning="pensando..."), _delta("3\n1"), _delta(finish="stop")))
        result, _ = self._chat(resp)
        assert result == "3\n1"

    def test_total_deadline_aborts_a_generation_that_never_ends(self):
        # O caso de 07/10/2026: o modelo em loop manda token atrás de token,
        # então o timeout de leitura nunca dispara. Quem corta é o prazo total.
        def endless():
            while True:
                yield b"data: " + json.dumps(_delta("x")).encode()

        resp = _mock_response()
        resp.iter_lines.return_value = endless()
        clock = iter([0.0] + [float(n) for n in range(1, 10_000)])
        with patch.object(config, "MODEL_TIMEOUT", 5), \
             patch("web_search_mcp.llm.time.monotonic", side_effect=lambda: next(clock)):
            with pytest.raises(requests.Timeout):
                self._chat(resp)
        resp.__exit__.assert_called_once()

    def test_max_tokens_sent_by_default_and_omitted_when_zero(self):
        with patch.object(config, "MODEL_MAX_TOKENS", 4096):
            _, post = self._chat(_mock_response())
        assert post.call_args.kwargs["json"]["max_tokens"] == 4096
        with patch.object(config, "MODEL_MAX_TOKENS", 0):
            _, post = self._chat(_mock_response())
        assert "max_tokens" not in post.call_args.kwargs["json"]

    def test_non_stream_json_response_still_works(self):
        resp = _mock_response()
        resp.headers = {"Content-Type": "application/json"}
        resp.json.return_value = {"choices": [{"message": {"content": "inteira"}, "finish_reason": "stop"}]}
        result, _ = self._chat(resp)
        assert result == "inteira"


class TestGenerationLoop:
    def _chat(self, lines):
        resp = _mock_response(lines=lines)
        with patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=resp):
            return llm.chat(system="sys", user="usr"), resp

    @staticmethod
    def _endless(prefix, repeated, key="content"):
        yield from (b"data: " + json.dumps(_delta(**{key: p})).encode() for p in prefix)
        while True:
            yield b"data: " + json.dumps(_delta(**{key: repeated})).encode()

    def test_repeated_answer_line_is_cut_and_keeps_what_came_before(self):
        # O caso de 07/10/2026: a mesma linha do resumo 432 vezes, 84 s.
        line = "- A cotação comercial registrada foi de R$ 5,0138 na sessão de hoje [1].\n"
        result, resp = self._chat(self._endless(["O dólar hoje:\n", "- Fechou em R$ 5,0110 [5].\n"], line))
        assert result == "O dólar hoje:\n- Fechou em R$ 5,0110 [5].\n" + line.rstrip()
        resp.__exit__.assert_called_once()

    def test_cycle_of_several_lines_is_cut(self):
        block = "Primeira linha do ciclo.\nSegunda linha do ciclo.\n"
        result, _ = self._chat(self._endless(["Início.\n"], block))
        assert result == "Início.\n" + block.rstrip()

    def test_line_split_across_deltas_is_still_seen(self):
        def stream():
            yield b"data: " + json.dumps(_delta("Começo.\n")).encode()
            while True:
                for piece in ("A mesma frase longa ", "repetida sem parar.", "\n"):
                    yield b"data: " + json.dumps(_delta(piece)).encode()

        result, _ = self._chat(stream())
        assert result == "Começo.\nA mesma frase longa repetida sem parar."

    def test_reasoning_loop_raises(self):
        with pytest.raises(llm.GenerationLoop):
            self._chat(self._endless([], "Preciso reconsiderar os resultados da busca.\n", key="reasoning"))

    def test_short_repeated_lines_are_not_a_loop(self):
        # Triagem devolve um número por linha; lista com itens iguais curtos também existe.
        result, _ = self._chat(_sse(_delta("7\n7\n7\n7\n7\n7\n"), _delta(finish="stop")))
        assert result == "7\n7\n7\n7\n7\n7\n"

    def test_three_repeats_and_scattered_duplicates_pass(self):
        text = "Linha que se repete três vezes.\n" * 3 + "Outra coisa.\n" + "Linha que se repete três vezes.\n" * 2
        result, _ = self._chat(_sse(_delta(text), _delta(finish="stop")))
        assert result == text


class TestReasoningTemperature:
    def _temperature(self, **kwargs):
        with patch.object(config, "MODEL_TEMPERATURE", 0.0), \
             patch.object(config, "REASONING_TEMPERATURE", 0.6), \
             patch.object(config, "REASONING_BODY", {"chat_template_kwargs": {"enable_thinking": True}}), \
             patch.object(llm, "_resolve_model", return_value="my-model"), \
             patch("web_search_mcp.llm.requests.post", return_value=_mock_response()) as post:
            llm.chat(system="sys", user="usr", **kwargs)
        return post.call_args.kwargs["json"]["temperature"]

    def test_reasoning_call_uses_its_own_temperature(self):
        with patch.object(config, "USE_REASONING", True):
            assert self._temperature(reasoning=True) == 0.6

    def test_plain_call_keeps_model_temperature(self):
        with patch.object(config, "USE_REASONING", True):
            assert self._temperature() == 0.0

    def test_reasoning_off_keeps_model_temperature(self):
        with patch.object(config, "USE_REASONING", False):
            assert self._temperature(reasoning=True) == 0.0

    def test_explicit_temperature_wins(self):
        with patch.object(config, "USE_REASONING", True):
            assert self._temperature(reasoning=True, temperature=0.2) == 0.2


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
