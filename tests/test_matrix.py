import base64
import httpx
import time
import os
from app.config import get_config, BridgeConfig, override_config
import pytest

SYNAPSE_URL = "http://localhost:8008"


@pytest.fixture(scope='session')
def setup_test_config() -> BridgeConfig:
    test_config = BridgeConfig(
        AES_KEY=base64.b64encode(os.urandom(16)).decode(),
        AS_TOKEN="test_as_token",
        HS_TOKEN="test_hs_token",
        HOMESERVER_NAME="jif.one",
        gmail_client_id="shouldn't matter",
        gmail_client_secret="shouldn't matter",
        gmail_project_id="shouldn't matter",
    )
    override_config(test_config)
    return test_config


def is_synapse_running(url: str) -> bool:
    """ waits ~10s"""
    for i in range(35):
        try:
            httpx.get(url)
            return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


synapse_integration = pytest.mark.skipif(
    condition=not is_synapse_running(SYNAPSE_URL),
    reason="Synapse Should Be Running for integration tests",
)


@synapse_integration
def test_my_integration():
    assert 1 == 1
