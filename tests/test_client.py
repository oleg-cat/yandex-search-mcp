"""Тесты HTTP-клиента (respx) и конфигурации: retry, маппинг ошибок, утечка ключа."""

import logging

import httpx
import pytest
import respx
from tenacity import wait_none

from yandex_search_mcp.client import (
    BASE_URL,
    WEB_SEARCH_PATH,
    AuthError,
    BadRequestError,
    QuotaError,
    RequestTimeoutError,
    UpstreamError,
    YandexSearchClient,
)
from yandex_search_mcp.config import ConfigError, Settings

SENTINEL_KEY = "AQVN-sentinel-key-must-never-leak-123"
WEB_URL = BASE_URL + WEB_SEARCH_PATH
ENV_OK = {"YANDEX_SEARCH_API_KEY": SENTINEL_KEY, "YANDEX_FOLDER_ID": "b1g-folder"}


@pytest.fixture
def client():
    """Клиент с нулевым backoff, чтобы retry-тесты не ждали реальные паузы."""
    c = YandexSearchClient(api_key=SENTINEL_KEY, wait_web=wait_none(), wait_gen=wait_none())
    yield c
    c.close()


# --- retry-поведение ---


@respx.mock
@pytest.mark.parametrize("status", [429, 503])
def test_retry_on_retryable_status_then_success(client, status):
    route = respx.post(WEB_URL).mock(
        side_effect=[httpx.Response(status, text="busy"), httpx.Response(200, json={"rawData": "PGE+"})]
    )
    result = client.web_search({"q": 1})
    assert result == {"rawData": "PGE+"}
    assert route.call_count == 2


@respx.mock
def test_persistent_503_exhausts_retries(client):
    route = respx.post(WEB_URL).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(UpstreamError):
        client.web_search({})
    assert route.call_count == 3


@respx.mock
def test_no_retry_on_400_and_api_text_preserved(client):
    route = respx.post(WEB_URL).mock(
        return_value=httpx.Response(400, text='{"message": "queryText is required"}')
    )
    with pytest.raises(BadRequestError) as exc_info:
        client.web_search({})
    assert "queryText is required" in str(exc_info.value)
    assert route.call_count == 1  # 4xx (кроме 429) не ретраится


# --- маппинг статусов на типы ошибок ---


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc_type"),
    [(401, AuthError), (403, AuthError), (429, QuotaError), (400, BadRequestError), (500, UpstreamError)],
)
def test_status_to_error_mapping(client, status, exc_type):
    respx.post(WEB_URL).mock(return_value=httpx.Response(status, text="err"))
    with pytest.raises(exc_type) as exc_info:
        client.web_search({})
    err = exc_info.value
    assert isinstance(err.error_type, str) and isinstance(err.retryable, bool)


@respx.mock
def test_unexpected_4xx_not_retried(client):
    """Любой 4xx кроме 429 — bad_request без retry (контракт §9)."""
    route = respx.post(WEB_URL).mock(return_value=httpx.Response(422, text="unprocessable"))
    with pytest.raises(BadRequestError):
        client.web_search({})
    assert route.call_count == 1


@respx.mock
def test_protocol_error_maps_to_upstream_and_retries(client):
    route = respx.post(WEB_URL).mock(side_effect=httpx.RemoteProtocolError("bad frame"))
    with pytest.raises(UpstreamError):
        client.web_search({})
    assert route.call_count == 3


@respx.mock
def test_network_timeout_maps_to_timeout_error(client):
    route = respx.post(WEB_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RequestTimeoutError):
        client.web_search({})
    assert route.call_count == 3  # таймауты ретраятся


# --- утечка ключа ---


@respx.mock
def test_api_key_never_leaks_in_errors_or_logs(client, caplog):
    """Даже если API echo-ит ключ в теле ошибки, наружу он не выходит."""
    respx.post(WEB_URL).mock(return_value=httpx.Response(400, text=f"bad auth header: Api-Key {SENTINEL_KEY}"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BadRequestError) as exc_info:
            client.web_search({})
    assert SENTINEL_KEY not in str(exc_info.value)
    assert SENTINEL_KEY not in repr(exc_info.value)
    assert SENTINEL_KEY not in caplog.text


# --- конфигурация ---


def test_settings_missing_required_vars_named_in_error():
    with pytest.raises(ConfigError) as exc_info:
        Settings.from_env({})
    msg = str(exc_info.value)
    assert "YANDEX_SEARCH_API_KEY" in msg and "YANDEX_FOLDER_ID" in msg


def test_settings_defaults():
    s = Settings.from_env(ENV_OK)
    assert s.default_search_type == "ru"
    assert s.enabled_tools == ("yandex_web_search", "yandex_image_search", "yandex_gen_search")
    assert s.default_region is None
    assert (s.timeout_web, s.timeout_gen) == (15.0, 120.0)


def test_settings_tools_whitelist():
    s = Settings.from_env({**ENV_OK, "YANDEX_MCP_ENABLED_TOOLS": "yandex_web_search"})
    assert s.enabled_tools == ("yandex_web_search",)
    with pytest.raises(ConfigError):
        Settings.from_env({**ENV_OK, "YANDEX_MCP_ENABLED_TOOLS": "nonexistent_tool"})


def test_settings_invalid_region_and_search_type():
    with pytest.raises(ConfigError):
        Settings.from_env({**ENV_OK, "YANDEX_MCP_DEFAULT_REGION": "moscow"})
    with pytest.raises(ConfigError):
        Settings.from_env({**ENV_OK, "YANDEX_MCP_DEFAULT_SEARCH_TYPE": "fr"})
