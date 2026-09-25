"""Тесты HTTP-клиента (respx) и конфигурации: retry, маппинг ошибок, утечка ключа."""

import asyncio
import logging

import httpx
import pytest
import respx
from tenacity import wait_none

from yandex_search_mcp.client import (
    BASE_URL,
    GEN_SEARCH_PATH,
    MAX_RETRY_AFTER,
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
GEN_URL = BASE_URL + GEN_SEARCH_PATH
ENV_OK = {"YANDEX_SEARCH_API_KEY": SENTINEL_KEY, "YANDEX_FOLDER_ID": "b1g-folder"}


@pytest.fixture
def client():
    """Клиент с нулевым backoff, чтобы retry-тесты не ждали реальные паузы."""
    c = YandexSearchClient(api_key=SENTINEL_KEY, wait_web=wait_none(), wait_gen=wait_none())
    yield c
    asyncio.run(c.aclose())


def web(client, body=None):
    """Синхронная обёртка над async-методом для компактных тестов."""
    return asyncio.run(client.web_search(body or {}))


def gen(client):
    return asyncio.run(client.gen_search({}))


# --- retry-поведение ---


@respx.mock
@pytest.mark.parametrize("status", [429, 503])
def test_retry_on_retryable_status_then_success(client, status):
    route = respx.post(WEB_URL).mock(
        side_effect=[httpx.Response(status, text="busy"), httpx.Response(200, json={"rawData": "PGE+"})]
    )
    result = web(client, {"q": 1})
    assert result == {"rawData": "PGE+"}
    assert route.call_count == 2


@respx.mock
def test_persistent_503_exhausts_retries(client):
    route = respx.post(WEB_URL).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(UpstreamError):
        web(client)
    assert route.call_count == 3


@respx.mock
def test_no_retry_on_400_and_api_text_preserved(client):
    route = respx.post(WEB_URL).mock(
        return_value=httpx.Response(400, text='{"message": "queryText is required"}')
    )
    with pytest.raises(BadRequestError) as exc_info:
        web(client)
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
        web(client)
    err = exc_info.value
    assert isinstance(err.error_type, str) and isinstance(err.retryable, bool)


@respx.mock
def test_unexpected_4xx_not_retried(client):
    """Любой 4xx кроме 429 — bad_request без retry (контракт §9)."""
    route = respx.post(WEB_URL).mock(return_value=httpx.Response(422, text="unprocessable"))
    with pytest.raises(BadRequestError):
        web(client)
    assert route.call_count == 1


@respx.mock
def test_protocol_error_maps_to_upstream_and_retries(client):
    route = respx.post(WEB_URL).mock(side_effect=httpx.RemoteProtocolError("bad frame"))
    with pytest.raises(UpstreamError):
        web(client)
    assert route.call_count == 3


@respx.mock
def test_network_timeout_maps_to_timeout_error(client):
    route = respx.post(WEB_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(RequestTimeoutError):
        web(client)
    assert route.call_count == 3  # таймауты web ретраятся


# --- gen: не платить трижды за один вопрос ---


@respx.mock
@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped")])
def test_gen_not_retried_when_outcome_unknown(client, error):
    """Запрос ушёл, ответа нет: API мог его выполнить и выставить счёт — повтор не делаем."""
    route = respx.post(GEN_URL).mock(side_effect=error)
    with pytest.raises((RequestTimeoutError, UpstreamError)):
        gen(client)
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("error", [httpx.ConnectTimeout("no route"), httpx.ConnectError("refused")])
def test_gen_retried_when_request_never_sent(client, error):
    route = respx.post(GEN_URL).mock(side_effect=[error, httpx.Response(200, text="[]")])
    assert gen(client) == "[]"
    assert route.call_count == 2


@respx.mock
def test_gen_retried_on_5xx(client):
    route = respx.post(GEN_URL).mock(side_effect=[httpx.Response(502), httpx.Response(200, text="[]")])
    assert gen(client) == "[]"
    assert route.call_count == 2


# --- Retry-After на 429 ---


@respx.mock
def test_quota_with_long_retry_after_fails_fast(client):
    """Часовую квоту ретраить бессмысленно: одна попытка и понятное сообщение."""
    route = respx.post(WEB_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": str(int(MAX_RETRY_AFTER) + 50)})
    )
    with pytest.raises(QuotaError) as exc_info:
        web(client)
    assert route.call_count == 1
    assert "Retry after 60s" in str(exc_info.value)
    assert exc_info.value.retry_after == 60


@respx.mock
def test_short_retry_after_is_used_as_wait(client, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    respx.post(WEB_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"rawData": "x"}),
        ]
    )
    assert web(client) == {"rawData": "x"}
    assert slept == [2.0]  # пауза из заголовка, а не из backoff (в фикстуре он нулевой)


# --- утечка ключа ---


@respx.mock
def test_api_key_never_leaks_in_errors_or_logs(client, caplog):
    """Даже если API echo-ит ключ в теле ошибки, наружу он не выходит."""
    respx.post(WEB_URL).mock(return_value=httpx.Response(400, text=f"bad auth header: Api-Key {SENTINEL_KEY}"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BadRequestError) as exc_info:
            web(client)
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


def test_settings_tools_blacklist_and_comma_separator():
    s = Settings.from_env({**ENV_OK, "YANDEX_MCP_DISABLED_TOOLS": "yandex_gen_search"})
    assert s.enabled_tools == ("yandex_web_search", "yandex_image_search")
    s = Settings.from_env(
        {
            **ENV_OK,
            "YANDEX_MCP_ENABLED_TOOLS": "yandex_web_search,yandex_gen_search",
            "YANDEX_MCP_DISABLED_TOOLS": "yandex_gen_search",
        }
    )
    assert s.enabled_tools == ("yandex_web_search",)
    with pytest.raises(ConfigError, match="leave no tools"):
        Settings.from_env(
            {
                **ENV_OK,
                "YANDEX_MCP_ENABLED_TOOLS": "yandex_web_search",
                "YANDEX_MCP_DISABLED_TOOLS": "yandex_web_search",
            }
        )
    with pytest.raises(ConfigError):
        Settings.from_env({**ENV_OK, "YANDEX_MCP_DISABLED_TOOLS": "brave_web_search"})


def test_settings_api_key_from_file(tmp_path):
    key_file = tmp_path / "key"
    key_file.write_text(SENTINEL_KEY + "\n", encoding="utf-8")
    env = {"YANDEX_SEARCH_API_KEY_FILE": str(key_file), "YANDEX_FOLDER_ID": "b1g-folder"}
    assert Settings.from_env(env).api_key == SENTINEL_KEY
    # явная переменная приоритетнее файла
    assert Settings.from_env({**env, "YANDEX_SEARCH_API_KEY": "direct"}).api_key == "direct"
    with pytest.raises(ConfigError, match="cannot be read"):
        Settings.from_env({**env, "YANDEX_SEARCH_API_KEY_FILE": str(tmp_path / "missing")})


def test_settings_transport_host_port():
    s = Settings.from_env(ENV_OK)
    assert (s.transport, s.http_host, s.http_port) == ("stdio", "127.0.0.1", 8000)
    s = Settings.from_env(
        {**ENV_OK, "YANDEX_MCP_TRANSPORT": "HTTP", "YANDEX_MCP_HOST": "0.0.0.0", "YANDEX_MCP_PORT": "9000"}
    )
    assert (s.transport, s.http_host, s.http_port) == ("http", "0.0.0.0", 9000)
    for bad in ({"YANDEX_MCP_TRANSPORT": "sse"}, {"YANDEX_MCP_PORT": "http"}, {"YANDEX_MCP_PORT": "70000"}):
        with pytest.raises(ConfigError):
            Settings.from_env({**ENV_OK, **bad})


def test_entry_point_exits_1_without_config(monkeypatch, capsys):
    from yandex_search_mcp.__main__ import main

    for name in ("YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_API_KEY_FILE", "YANDEX_FOLDER_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code == 1
    assert "YANDEX_SEARCH_API_KEY" in capsys.readouterr().err


# --- CLI: --version / --help не требуют ключей и не запускают сервер ---


@pytest.fixture
def no_credentials(monkeypatch):
    for name in ("YANDEX_SEARCH_API_KEY", "YANDEX_SEARCH_API_KEY_FILE", "YANDEX_FOLDER_ID"):
        monkeypatch.delenv(name, raising=False)


def test_cli_version(no_credentials, capsys):
    from yandex_search_mcp import __version__
    from yandex_search_mcp.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"yandex-search-mcp {__version__}"


def test_cli_help_lists_every_env_variable(no_credentials, capsys):
    from yandex_search_mcp import config
    from yandex_search_mcp.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    env_names = [
        value for name, value in vars(config).items() if name.startswith("ENV_") and isinstance(value, str)
    ]
    assert env_names, "ENV_*-константы не найдены"
    missing = [env for env in env_names if env not in out]
    assert not missing, f"--help не упоминает: {missing}"


def test_cli_unknown_argument_does_not_start_server(no_credentials, capsys):
    from yandex_search_mcp.__main__ import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--bogus"])
    assert exc_info.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err
