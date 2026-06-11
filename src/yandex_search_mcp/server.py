"""FastMCP-приложение: инструменты yandex_web_search / yandex_image_search / yandex_gen_search.

Body-builders — чистые функции (тестируются без HTTP). Ошибки — единым
контрактом ТЗ §10: ToolError с JSON-телом {"error": {type, message, retryable}}.
Инструменты регистрируются по whitelist из настроек (YANDEX_MCP_ENABLED_TOOLS).
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import YandexApiError, YandexSearchClient
from .config import Settings
from .models import (
    GenSearchResponse,
    ImageSearchResponse,
    ParseError,
    WebSearchResponse,
    YandexXmlError,
)
from .parsing import decode_raw_data, parse_gen_response, parse_image_xml, parse_web_xml

SearchType = Literal["ru", "com", "tr", "kk", "be", "uz"]
Localization = Literal["ru", "uk", "be", "kk", "tr", "en"]

SEARCH_TYPE_MAP: dict[str, str] = {
    "ru": "SEARCH_TYPE_RU",
    "com": "SEARCH_TYPE_COM",
    "tr": "SEARCH_TYPE_TR",
    "kk": "SEARCH_TYPE_KK",
    "be": "SEARCH_TYPE_BE",
    "uz": "SEARCH_TYPE_UZ",
}
LOCALIZATION_MAP: dict[str, str] = {
    "ru": "LOCALIZATION_RU",
    "uk": "LOCALIZATION_UK",
    "be": "LOCALIZATION_BE",
    "kk": "LOCALIZATION_KK",
    "tr": "LOCALIZATION_TR",
    "en": "LOCALIZATION_EN",
}
# l10n следует за search_type, если не задана явно. LOCALIZATION_UZ в API нет —
# для uz поле l10n не отправляется (решение зафиксировано в CHANGELOG).
AUTO_LOCALIZATION: dict[str, str | None] = {
    "ru": "ru",
    "com": "en",
    "tr": "tr",
    "kk": "kk",
    "be": "be",
    "uz": None,
}
FAMILY_MODE_MAP: dict[str, str] = {
    "none": "FAMILY_MODE_NONE",
    "moderate": "FAMILY_MODE_MODERATE",
    "strict": "FAMILY_MODE_STRICT",
}
PERIOD_MAP: dict[str, str] = {
    "all": "PERIOD_ALL_TIME",
    "day": "PERIOD_DAY",
    "2weeks": "PERIOD_2_WEEKS",
    "month": "PERIOD_MONTH",
}
SORT_MODE_MAP: dict[str, str] = {
    "relevance": "SORT_MODE_BY_RELEVANCE",
    "time": "SORT_MODE_BY_TIME",
}
IMAGE_FORMAT_MAP: dict[str, str] = {
    "jpeg": "IMAGE_FORMAT_JPEG",
    "gif": "IMAGE_FORMAT_GIF",
    "png": "IMAGE_FORMAT_PNG",
}
IMAGE_SIZE_MAP: dict[str, str] = {
    "enormous": "IMAGE_SIZE_ENORMOUS",
    "large": "IMAGE_SIZE_LARGE",
    "medium": "IMAGE_SIZE_MEDIUM",
    "small": "IMAGE_SIZE_SMALL",
    "tiny": "IMAGE_SIZE_TINY",
    "wallpaper": "IMAGE_SIZE_WALLPAPER",
}
IMAGE_ORIENTATION_MAP: dict[str, str] = {
    "vertical": "IMAGE_ORIENTATION_VERTICAL",
    "horizontal": "IMAGE_ORIENTATION_HORIZONTAL",
    "square": "IMAGE_ORIENTATION_SQUARE",
}
IMAGE_COLOR_MAP: dict[str, str] = {
    color: f"IMAGE_COLOR_{color.upper()}"
    for color in (
        "color",
        "grayscale",
        "red",
        "orange",
        "yellow",
        "green",
        "cyan",
        "blue",
        "violet",
        "white",
        "black",
    )
}

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

WEB_SEARCH_DESCRIPTION = """Search the web with Yandex Search — the strongest engine for \
Russian-language queries; also serves yandex.com/tr/kk/be/uz indexes.

When to use: fact-checking, current information, news, research, finding sources on any topic; \
the go-to tool for anything about Russia/CIS or written in Russian.
When NOT to use: for pictures use yandex_image_search; for a single synthesized AI answer with \
citations use yandex_gen_search (much slower and more expensive).

Usage hints: n_results=5 for quick fact checks, 15-20 for research/comparison tasks. The query \
supports Yandex search operators: site:example.com (limit to domain), host:, date:YYYYMMDD / \
date:>YYYYMMDD, "quoted phrase" for exact match, word1 | word2 for OR, -word to exclude.

Returns JSON: ranked results (url, domain, title, snippet, modified_at), estimated total 'found', \
corrected_query when Yandex fixed a typo, and has_more — pass page=1,2,... to paginate."""

IMAGE_SEARCH_DESCRIPTION = """Search images by text query via Yandex Images.

When to use: finding pictures of people, places, things, diagrams, art or design references — \
especially for Russian-language queries.
When NOT to use: for web pages and facts use yandex_web_search; for synthesized answers use \
yandex_gen_search.

Returns image URLs and metadata only — never binary/base64 data; fetch image_url separately if \
pixels are needed. Usage hints: n_results=5 to pick one image, 15-20 to offer a choice. Filters: \
image_format, image_size, orientation, color, site (limit to a domain). Each result has direct \
image_url, width/height, and the hosting page_url."""

GEN_SEARCH_DESCRIPTION = """Generative search (Yandex AI): one synthesized answer with cited web \
sources.

Expensive and SLOW endpoint (tens of seconds; strict 1 request/second quota) — prefer \
yandex_web_search unless the user explicitly needs a single synthesized, citation-backed answer \
rather than a list of results.

When to use: complex questions where a digest of several sources should be returned as final text.
When NOT to use: quick fact checks, link/page/image lookup — use yandex_web_search or \
yandex_image_search.

Returns JSON: answer (markdown), sources[] with 'used' flags, is_answer_rejected (true when \
Yandex refused to answer), fixed_misspell_query (note: typo correction is aggressive and may \
distort technical terms — check this field if the answer looks off-topic). Use site/host to \
restrict sources to specific domains."""


def _resolve_localization(search_type: str, localization: str | None) -> str | None:
    """Явная l10n приоритетнее; иначе авто-следование за search_type (uz — без l10n)."""
    return localization if localization is not None else AUTO_LOCALIZATION[search_type]


def build_web_body(
    *,
    query: str,
    search_type: str,
    n_results: int,
    page: int,
    region: int | None,
    localization: str | None,
    period: str,
    sort_by: str,
    family_mode: str,
    fix_typos: bool,
    max_passages: int,
    dedupe_by_domain: bool,
    folder_id: str,
) -> dict[str, Any]:
    """Тело POST /v2/web/search по маппингу ТЗ §8.1."""
    body: dict[str, Any] = {
        "query": {
            "searchType": SEARCH_TYPE_MAP[search_type],
            "queryText": query,
            "familyMode": FAMILY_MODE_MAP[family_mode],
            "page": page,
            "fixTypoMode": "FIX_TYPO_MODE_ON" if fix_typos else "FIX_TYPO_MODE_OFF",
        },
        "sortSpec": {
            "sortMode": SORT_MODE_MAP[sort_by],
            "sortOrder": "SORT_ORDER_DESC",
        },
        "groupSpec": {
            # dedupe_by_domain: DEEP + 1 док на группу = один документ на домен.
            # docsInGroup=1 намеренно и для FLAT: 1 группа = 1 документ = 1 результат
            "groupMode": "GROUP_MODE_DEEP" if dedupe_by_domain else "GROUP_MODE_FLAT",
            "groupsOnPage": n_results,
            "docsInGroup": 1,
        },
        "maxPassages": max_passages,
        "period": PERIOD_MAP[period],
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }
    l10n = _resolve_localization(search_type, localization)
    if l10n is not None:
        body["l10n"] = LOCALIZATION_MAP[l10n]
    if region is not None:
        body["region"] = str(region)
    return body


def build_image_body(
    *,
    query: str,
    search_type: str,
    n_results: int,
    page: int,
    family_mode: str,
    image_format: str | None,
    image_size: str | None,
    orientation: str | None,
    color: str | None,
    site: str | None,
    folder_id: str,
) -> dict[str, Any]:
    """Тело POST /v2/image/search по маппингу ТЗ §8.2."""
    body: dict[str, Any] = {
        "query": {
            "searchType": SEARCH_TYPE_MAP[search_type],
            "queryText": query,
            "familyMode": FAMILY_MODE_MAP[family_mode],
            "page": page,
        },
        "docsOnPage": n_results,
        "folderId": folder_id,
    }
    image_spec: dict[str, str] = {}
    if image_format is not None:
        image_spec["format"] = IMAGE_FORMAT_MAP[image_format]
    if image_size is not None:
        image_spec["size"] = IMAGE_SIZE_MAP[image_size]
    if orientation is not None:
        image_spec["orientation"] = IMAGE_ORIENTATION_MAP[orientation]
    if color is not None:
        image_spec["color"] = IMAGE_COLOR_MAP[color]
    if image_spec:
        body["imageSpec"] = image_spec
    if site is not None:
        body["site"] = site
    return body


def build_gen_body(
    *,
    query: str,
    search_type: str,
    site: str | None,
    host: str | None,
    folder_id: str,
) -> dict[str, Any]:
    """Тело POST /v2/gen/search. site и host — взаимоисключающие (oneof в API)."""
    if site is not None and host is not None:
        raise ToolError(_error_json("bad_request", "Pass either 'site' or 'host', not both.", False))
    body: dict[str, Any] = {
        "messages": [{"content": query, "role": "ROLE_USER"}],
        "searchType": SEARCH_TYPE_MAP[search_type],
        "fixMisspell": True,
        "folderId": folder_id,
    }
    if site is not None:
        body["site"] = {"site": [site]}
    if host is not None:
        body["host"] = {"host": [host]}
    return body


def _error_json(error_type: str, message: str, retryable: bool) -> str:
    """JSON-тело контракта ошибок ТЗ §10."""
    return json.dumps(
        {"error": {"type": error_type, "message": message, "retryable": retryable}},
        ensure_ascii=False,
    )


def _as_tool_error(exc: Exception) -> ToolError:
    """YandexApiError / ParseError / YandexXmlError → единый ToolError-контракт."""
    if isinstance(exc, ToolError):
        return exc  # уже отформатирован — не оборачивать повторно
    if isinstance(exc, (YandexApiError, ParseError, YandexXmlError)):
        return ToolError(_error_json(exc.error_type, str(exc), exc.retryable))
    return ToolError(_error_json("upstream", f"Unexpected error: {exc}", False))


def build_server(settings: Settings, client: YandexSearchClient) -> FastMCP:
    """Собирает FastMCP-сервер, регистрируя инструменты по whitelist настроек."""

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            client.close()

    mcp = FastMCP("yandex-search", lifespan=lifespan)

    QueryParam = Annotated[
        str,
        Field(
            min_length=1,
            max_length=400,
            description="Search query, max 400 chars / 40 words. Supports Yandex operators "
            '(site:, host:, date:, "exact phrase", -minus-word, |).',
        ),
    ]
    SearchTypeParam = Annotated[
        SearchType | None,
        Field(
            description="Yandex index to search: ru (yandex.ru, best for Russian), com "
            "(yandex.com, international/English), tr/kk/be/uz (Turkey/Kazakhstan/Belarus/"
            f"Uzbekistan). Default: '{settings.default_search_type}' (server-configured)."
        ),
    ]
    PageParam = Annotated[int, Field(ge=0, description="Zero-based page number for pagination.")]

    if "yandex_web_search" in settings.enabled_tools:

        @mcp.tool(description=WEB_SEARCH_DESCRIPTION, annotations=READ_ONLY)
        def yandex_web_search(
            query: QueryParam,
            search_type: SearchTypeParam = None,
            n_results: Annotated[
                int, Field(ge=1, le=20, description="Results per page: 5 for quick checks, 15-20 for research.")
            ] = 10,
            page: PageParam = 0,
            region: Annotated[
                int | None,
                Field(
                    description="Yandex geo-id influencing ranking: 225=Russia, 213=Moscow, "
                    "2=Saint Petersburg, 187=Ukraine, 159=Kazakhstan. Default: server-configured or none."
                ),
            ] = None,
            localization: Annotated[
                Localization | None,
                Field(
                    description="UI language of the search (notifications, etc). Defaults to match search_type."
                ),
            ] = None,
            period: Annotated[
                Literal["all", "day", "2weeks", "month"],
                Field(description="Limit results by document freshness."),
            ] = "all",
            sort_by: Annotated[
                Literal["relevance", "time"],
                Field(description="'time' = newest first (use with period for news)."),
            ] = "relevance",
            family_mode: Annotated[
                Literal["none", "moderate", "strict"],
                Field(description="Adult-content filtering; 'moderate' is the API default."),
            ] = "moderate",
            fix_typos: Annotated[bool, Field(description="Let Yandex auto-correct typos in the query.")] = True,
            max_passages: Annotated[int, Field(ge=1, le=5, description="Max snippet passages per result.")] = 3,
            dedupe_by_domain: Annotated[
                bool, Field(description="True = at most one result per domain (diverse sources).")
            ] = False,
        ) -> WebSearchResponse:
            body = build_web_body(
                query=query,
                search_type=search_type or settings.default_search_type,
                n_results=n_results,
                page=page,
                region=region if region is not None else settings.default_region,
                localization=localization,
                period=period,
                sort_by=sort_by,
                family_mode=family_mode,
                fix_typos=fix_typos,
                max_passages=max_passages,
                dedupe_by_domain=dedupe_by_domain,
                folder_id=settings.folder_id,
            )
            try:
                envelope = client.web_search(body)
                return parse_web_xml(decode_raw_data(envelope), query=query, page=page, n_results=n_results)
            except Exception as exc:
                raise _as_tool_error(exc) from exc

    if "yandex_image_search" in settings.enabled_tools:

        @mcp.tool(description=IMAGE_SEARCH_DESCRIPTION, annotations=READ_ONLY)
        def yandex_image_search(
            query: QueryParam,
            search_type: SearchTypeParam = None,
            n_results: Annotated[
                int, Field(ge=1, le=20, description="Images per page: 5 to pick one, 15-20 to offer a choice.")
            ] = 10,
            page: PageParam = 0,
            family_mode: Annotated[
                Literal["none", "moderate", "strict"],
                Field(description="Adult-content filtering; 'moderate' is the API default."),
            ] = "moderate",
            image_format: Annotated[
                Literal["jpeg", "gif", "png"] | None, Field(description="Restrict by file format.")
            ] = None,
            image_size: Annotated[
                Literal["enormous", "large", "medium", "small", "tiny", "wallpaper"] | None,
                Field(
                    description="enormous >1600x1200; large 800x600-1600x1200; medium 150x150-800x600; "
                    "small 32x32-150x150; tiny <=32x32; wallpaper = desktop sizes."
                ),
            ] = None,
            orientation: Annotated[
                Literal["horizontal", "vertical", "square"] | None,
                Field(description="Restrict by image orientation."),
            ] = None,
            color: Annotated[
                Literal[
                    "color",
                    "grayscale",
                    "red",
                    "orange",
                    "yellow",
                    "green",
                    "cyan",
                    "blue",
                    "violet",
                    "white",
                    "black",
                ]
                | None,
                Field(description="'color'/'grayscale' or a dominant color."),
            ] = None,
            site: Annotated[
                str | None, Field(description="Limit results to images hosted on this domain.")
            ] = None,
        ) -> ImageSearchResponse:
            body = build_image_body(
                query=query,
                search_type=search_type or settings.default_search_type,
                n_results=n_results,
                page=page,
                family_mode=family_mode,
                image_format=image_format,
                image_size=image_size,
                orientation=orientation,
                color=color,
                site=site,
                folder_id=settings.folder_id,
            )
            try:
                envelope = client.image_search(body)
                return parse_image_xml(decode_raw_data(envelope), query=query, page=page, n_results=n_results)
            except Exception as exc:
                raise _as_tool_error(exc) from exc

    if "yandex_gen_search" in settings.enabled_tools:

        @mcp.tool(description=GEN_SEARCH_DESCRIPTION, annotations=READ_ONLY)
        def yandex_gen_search(
            query: Annotated[str, Field(min_length=1, max_length=16384, description="Question to answer.")],
            search_type: SearchTypeParam = None,
            site: Annotated[
                str | None, Field(description="Restrict sources to this site (mutually exclusive with host).")
            ] = None,
            host: Annotated[
                str | None, Field(description="Restrict sources to this host (mutually exclusive with site).")
            ] = None,
        ) -> GenSearchResponse:
            body = build_gen_body(
                query=query,
                search_type=search_type or settings.default_search_type,
                site=site,
                host=host,
                folder_id=settings.folder_id,
            )
            try:
                raw = client.gen_search(body)
                return parse_gen_response(raw)
            except Exception as exc:
                raise _as_tool_error(exc) from exc

    return mcp
