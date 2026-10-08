from unittest.mock import patch

import pytest

from web_search_mcp import config


@pytest.fixture(autouse=True)
def _no_host_model_settings():
    # models.toml e REASONING_BUDGET_TOKENS vêm do host: com eles, o que cada
    # teste afirma sobre o raciocínio dependeria da máquina onde roda. Teste
    # que precisa deles define os seus.
    with patch.object(config, "MODEL_SETTINGS", {}), patch.object(config, "REASONING_BUDGET_TOKENS", 0):
        yield
