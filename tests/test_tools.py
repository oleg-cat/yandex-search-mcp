"""Тесты инструментов: body-builders по таблицам ТЗ §8, контракты ответов, whitelist, ошибки."""

import asyncio
import base64
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from yandex_search_mcp.client import QuotaError
from yandex_search_mcp.config import Settings
from yandex_search_mcp.server import (
    build_gen_body,
    build_image_body,
    build_server,
    build_web_body,
)

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings.from_env({"YANDEX_SEARCH_API_KEY": "test-key", "YANDEX_FOLDER_ID": "test-folder"})

WEB_DEFAULTS = dict(
    query="q",
    search_type="ru",
    n_results=10,
    page=0,
    region=None,
    localization=None,
    period="all",
    sort_by="relevance",
    family_mode="moderate",
    fix_typos=True,
    max_passages=3,
    dedupe_by_domain=False,
    folder_id="fid",
)


def _envelope(fixture_name: str) -> dict:
    """Собирает конверт API из XML-фикстуры (как отдаёт живой эндпоинт)."""
    xml = (FIXTURES / fixture_name).read_bytes()
    return {"rawData": base64.b64encode(xml).decode()}


class FakeClient:
    """Подмена YandexSearchClient: отдаёт фикстуры и записывает тела запросов."""

    def __init__(self, web=None, image=None, gen=None, error=None):
        self._web, self._image, self._gen, self._error = web, image, gen, error
        self.bodies: list[dict] = []
        self.closed = False

    def _respond(self, body, value):
        self.bodies.append(body)
        if self._error is not None:
            raise self._error
        assert value is not None, "инструмент не должен был дойти до HTTP-вызова"
        return value

    async def web_search(self, body):
        return self._respond(body, self._web)

    async def image_search(self, body):
        return self._respond(body, self._image)

    async def gen_search(self, body):
        return self._respond(body, self._gen)

    async def aclose(self):
        self.closed = True


# --- body-builders: web (ТЗ §8.1) ---


def test_web_body_search_type_and_auto_localization():
    body = build_web_body(**WEB_DEFAULTS)
    assert body["query"]["searchType"] == "SEARCH_TYPE_RU"
    assert body["l10n"] == "LOCALIZATION_RU"
    assert body["responseFormat"] == "FORMAT_XML"
    assert body["folderId"] == "fid"

    body = build_web_body(**{**WEB_DEFAULTS, "search_type": "com"})
    assert body["query"]["searchType"] == "SEARCH_TYPE_COM"
    assert body["l10n"] == "LOCALIZATION_EN"  # com → en

    body = build_web_body(**{**WEB_DEFAULTS, "search_type": "uz"})
    assert body["query"]["searchType"] == "SEARCH_TYPE_UZ"
    assert "l10n" not in body  # LOCALIZATION_UZ в API не существует


def test_web_body_explicit_localization_wins():
    body = build_web_body(**{**WEB_DEFAULTS, "localization": "en"})
    assert body["l10n"] == "LOCALIZATION_EN"


def test_web_body_period_sort_typos():
    body = build_web_body(**{**WEB_DEFAULTS, "period": "2weeks", "sort_by": "time", "fix_typos": False})
    assert body["period"] == "PERIOD_2_WEEKS"
    assert body["sortSpec"] == {"sortMode": "SORT_MODE_BY_TIME", "sortOrder": "SORT_ORDER_DESC"}
    assert body["query"]["fixTypoMode"] == "FIX_TYPO_MODE_OFF"
    assert build_web_body(**WEB_DEFAULTS)["period"] == "PERIOD_ALL_TIME"


def test_web_body_dedupe_and_paging():
    body = build_web_body(**{**WEB_DEFAULTS, "dedupe_by_domain": True, "n_results": 7, "page": 2})
    assert body["groupSpec"] == {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": 7, "docsInGroup": 1}
    assert body["query"]["page"] == 2
    assert build_web_body(**WEB_DEFAULTS)["groupSpec"]["groupMode"] == "GROUP_MODE_FLAT"


def test_web_body_region_stringified_or_absent():
    assert build_web_body(**{**WEB_DEFAULTS, "region": 213})["region"] == "213"
    assert "region" not in build_web_body(**WEB_DEFAULTS)


# --- body-builders: image (ТЗ §8.2) ---


def test_image_body_spec_enums_and_site():
    body = build_image_body(
        query="q",
        search_type="ru",
        n_results=5,
        page=1,
        family_mode="strict",
        image_format="png",
        image_size="large",
        orientation="horizontal",
        color="red",
        site="example.ru",
        folder_id="fid",
    )
    assert body["imageSpec"] == {
        "format": "IMAGE_FORMAT_PNG",
        "size": "IMAGE_SIZE_LARGE",
        "orientation": "IMAGE_ORIENTATION_HORIZONTAL",
        "color": "IMAGE_COLOR_RED",
    }
    assert body["docsOnPage"] == 5
    assert body["site"] == "example.ru"
    assert body["query"]["familyMode"] == "FAMILY_MODE_STRICT"


def test_image_body_no_spec_when_no_filters():
    body = build_image_body(
        query="q",
        search_type="ru",
        n_results=10,
        page=0,
        family_mode="moderate",
        image_format=None,
        image_size=None,
        orientation=None,
        color=None,
        site=None,
        folder_id="fid",
    )
    assert "imageSpec" not in body and "site" not in body


# --- body-builders: gen (ТЗ §8.3) ---


def test_gen_body_structure_and_site_host_oneof():
    body = build_gen_body(query="вопрос", search_type="ru", site="habr.com", folder_id="fid")
    assert body["messages"] == [{"content": "вопрос", "role": "ROLE_USER"}]
    assert body["searchType"] == "SEARCH_TYPE_RU"
    assert body["site"] == {"site": ["habr.com"]}
    assert body["fixMisspell"] is True
    assert "searchFilters" not in body

    body = build_gen_body(query="q", search_type="com", host=["h.ru", "g.ru"], folder_id="fid")
    assert body["host"] == {"host": ["h.ru", "g.ru"]}

    body = build_gen_body(query="q", search_type="ru", url="https://a.ru/p", folder_id="fid")
    assert body["url"] == {"url": ["https://a.ru/p"]}

    for kwargs in ({"site": "a", "host": "b"}, {"site": "a", "url": "b"}, {"host": "a", "url": "b"}):
        with pytest.raises(ToolError):
            build_gen_body(query="q", search_type="ru", folder_id="fid", **kwargs)


def test_gen_body_scope_limits():
    build_gen_body(query="q", search_type="ru", folder_id="fid", site=[f"s{i}.ru" for i in range(5)])
    with pytest.raises(ToolError, match="at most 5"):
        build_gen_body(query="q", search_type="ru", folder_id="fid", site=[f"s{i}.ru" for i in range(6)])
    with pytest.raises(ToolError, match="at most 10"):
        build_gen_body(query="q", search_type="ru", folder_id="fid", url=[f"https://a/{i}" for i in range(11)])


def test_gen_body_typos_and_filters():
    body = build_gen_body(
        query="q",
        search_type="ru",
        folder_id="fid",
        fix_typos=False,
        date=">20250101",
        lang="en",
        doc_format="pdf",
    )
    assert body["fixMisspell"] is False
    assert body["searchFilters"] == [{"date": ">20250101"}, {"lang": "en"}, {"format": "DOC_FORMAT_PDF"}]


# --- сервер: whitelist, контракты, ошибки ---


def test_whitelist_hides_tools():
    settings = Settings.from_env(
        {"YANDEX_SEARCH_API_KEY": "k", "YANDEX_FOLDER_ID": "f", "YANDEX_MCP_ENABLED_TOOLS": "yandex_web_search"}
    )
    mcp = build_server(settings, FakeClient())
    tools = asyncio.run(mcp.list_tools())
    assert [t.name for t in tools] == ["yandex_web_search"]


def test_all_tools_registered_with_schemas_by_default():
    mcp = build_server(SETTINGS, FakeClient())
    tools = asyncio.run(mcp.list_tools())
    assert [t.name for t in tools] == ["yandex_web_search", "yandex_image_search", "yandex_gen_search"]
    for tool in tools:
        assert tool.outputSchema, f"{tool.name}: нет outputSchema"
        assert tool.annotations.readOnlyHint is True
        assert "When to use" in tool.description or "When NOT" in tool.description


def test_web_tool_returns_contract_with_unescaped_cyrillic():
    client = FakeClient(web=_envelope("web_ru_normal.xml"))
    mcp = build_server(SETTINGS, client)
    content, structured = asyncio.run(
        mcp.call_tool("yandex_web_search", {"query": "ласточкино гнездо крым", "n_results": 10})
    )
    assert structured["found"] > 0 and len(structured["results"]) == 10
    assert structured["results"][0]["rank"] == 1
    assert "Ласточкино" in content[0].text  # кириллица не экранирована
    # дефолтный search_type из настроек попал в тело запроса
    assert client.bodies[0]["query"]["searchType"] == "SEARCH_TYPE_RU"


def test_image_tool_no_base64_in_output():
    mcp = build_server(SETTINGS, FakeClient(image=_envelope("image_ru.xml")))
    content, structured = asyncio.run(mcp.call_tool("yandex_image_search", {"query": "сербский флаг"}))
    assert structured["results"][0]["image_url"].startswith("http")
    assert ";base64," not in content[0].text


def test_gen_tool_parses_live_array_fixture():
    raw = (FIXTURES / "gen_raw_response.txt").read_text(encoding="utf-8")
    mcp = build_server(SETTINGS, FakeClient(gen=raw))
    _, structured = asyncio.run(mcp.call_tool("yandex_gen_search", {"query": "Что такое MCP?"}))
    assert "Model Context Protocol" in structured["answer"]
    assert structured["sources"] and structured["is_answer_rejected"] is False


def test_error_contract_json_in_tool_error():
    mcp = build_server(SETTINGS, FakeClient(error=QuotaError("quota exceeded (429)")))
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(mcp.call_tool("yandex_web_search", {"query": "q"}))
    msg = str(exc_info.value)
    payload = json.loads(msg[msg.index("{") :])  # SDK добавляет префикс "Error executing tool..."
    assert payload["error"]["type"] == "quota"
    assert payload["error"]["retryable"] is True
    assert "quota exceeded" in payload["error"]["message"]


def test_gen_site_host_conflict_via_tool_is_single_bad_request():
    """ToolError из body-builder не оборачивается повторно (passthrough)."""
    client = FakeClient()
    mcp = build_server(SETTINGS, client)
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(mcp.call_tool("yandex_gen_search", {"query": "q", "site": "a.ru", "host": "b.ru"}))
    msg = str(exc_info.value)
    payload = json.loads(msg[msg.index("{") :])
    assert payload["error"]["type"] == "bad_request"
    assert "Unexpected error" not in msg  # не завернулось в generic upstream
    assert client.bodies == []


def test_query_length_validated_before_http_call():
    client = FakeClient()  # без ответов: дойти до HTTP — упасть в assert
    mcp = build_server(SETTINGS, client)
    with pytest.raises(ToolError):
        asyncio.run(mcp.call_tool("yandex_web_search", {"query": "х" * 401}))
    assert client.bodies == []  # fail-fast: сетевого вызова не было


def test_gen_tool_passes_new_params_to_body():
    raw = (FIXTURES / "gen_raw_response.txt").read_text(encoding="utf-8")
    client = FakeClient(gen=raw)
    mcp = build_server(SETTINGS, client)
    _, structured = asyncio.run(
        mcp.call_tool(
            "yandex_gen_search",
            {"query": "Что такое MCP?", "fix_typos": False, "site": ["habr.com", "vc.ru"], "lang": "ru"},
        )
    )
    assert client.bodies[0]["fixMisspell"] is False
    assert client.bodies[0]["site"] == {"site": ["habr.com", "vc.ru"]}
    assert client.bodies[0]["searchFilters"] == [{"lang": "ru"}]
    assert structured["search_queries"]


def test_unexpected_error_is_logged_and_wrapped(caplog):
    mcp = build_server(SETTINGS, FakeClient(error=RuntimeError("boom")))
    with pytest.raises(ToolError) as exc_info:
        asyncio.run(mcp.call_tool("yandex_web_search", {"query": "q"}))
    assert '"type": "upstream"' in str(exc_info.value)
    assert "Unexpected error in tool call" in caplog.text
    assert "RuntimeError: boom" in caplog.text  # traceback попал в лог


class SlowClient(FakeClient):
    """Имитирует медленный API без блокировки event loop."""

    async def web_search(self, body):
        await asyncio.sleep(0.3)
        return _envelope("web_ru_normal.xml")


def test_tool_calls_run_concurrently():
    """Инструменты async: три параллельных вызова идут одновременно, а не по очереди."""
    mcp = build_server(SETTINGS, SlowClient())

    async def run_three():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(*(mcp.call_tool("yandex_web_search", {"query": "q"}) for _ in range(3)))
        return loop.time() - start

    assert asyncio.run(run_three()) < 0.6  # последовательно было бы >= 0.9


def test_client_survives_session_lifespans():
    """В HTTP-режиме lifespan входится на каждую сессию: общий клиент не должен закрываться."""
    client = FakeClient(web=_envelope("web_ru_normal.xml"))
    mcp = build_server(SETTINGS, client)
    lowlevel = mcp._mcp_server

    async def two_sessions():
        for _ in range(2):
            async with lowlevel.lifespan(lowlevel):
                await mcp.call_tool("yandex_web_search", {"query": "q"})

    asyncio.run(two_sessions())
    assert client.closed is False
    assert len(client.bodies) == 2
