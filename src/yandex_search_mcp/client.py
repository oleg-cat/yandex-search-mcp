"""HTTP-клиент Yandex Search API v2: auth, таймауты, retry, типизированные ошибки.

Retry (tenacity): только 429 / 5xx / сетевые ошибки / таймауты, 3 попытки,
экспоненциальный backoff с jitter. На 4xx (кроме 429) retry нет.
Api-Key никогда не попадает в сообщения ошибок и логи (ТЗ §11).
"""

import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://searchapi.api.cloud.yandex.net"
WEB_SEARCH_PATH = "/v2/web/search"
IMAGE_SEARCH_PATH = "/v2/image/search"
GEN_SEARCH_PATH = "/v2/gen/search"

MAX_ATTEMPTS = 3
# Для gen базовая пауза выше: лимит эндпоинта — 1 rps (web/image — 10 rps)
WAIT_WEB = wait_exponential_jitter(initial=1.0, max=8.0)
WAIT_GEN = wait_exponential_jitter(initial=2.0, max=8.0)
ERROR_BODY_SNIPPET_LEN = 300


class YandexApiError(Exception):
    """Базовая ошибка API; type/retryable идут в контракт ошибок MCP (ТЗ §10)."""

    error_type: str = "upstream"
    retryable: bool = False


class AuthError(YandexApiError):
    """401/403 — невалидный ключ, нет scope или роли."""

    error_type = "auth"
    retryable = False


class QuotaError(YandexApiError):
    """429 — превышена квота или rps-лимит."""

    error_type = "quota"
    retryable = True


class BadRequestError(YandexApiError):
    """400 — некорректное тело запроса; включает текст ответа API."""

    error_type = "bad_request"
    retryable = False


class UpstreamError(YandexApiError):
    """5xx и неожиданные статусы на стороне API."""

    error_type = "upstream"
    retryable = True


class RequestTimeoutError(YandexApiError):
    """Сетевая ошибка или таймаут запроса."""

    error_type = "timeout"
    retryable = True


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, YandexApiError) and exc.retryable


class YandexSearchClient:
    """Один переиспользуемый httpx.Client на процесс; методы по эндпоинтам API."""

    def __init__(
        self,
        api_key: str,
        timeout_web: float = 15.0,
        timeout_gen: float = 120.0,
        base_url: str = BASE_URL,
        wait_web: Any = WAIT_WEB,
        wait_gen: Any = WAIT_GEN,
    ) -> None:
        self._api_key = api_key
        self._timeout_web = timeout_web
        self._timeout_gen = timeout_gen
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        )
        # Wait-стратегии инжектируются, чтобы тесты не ждали реальный backoff
        self._wait_web = wait_web
        self._wait_gen = wait_gen

    def close(self) -> None:
        """Закрывает пул соединений (вызывается из lifespan сервера)."""
        self._http.close()

    def _redact(self, text: str) -> str:
        """Вычищает Api-Key из любого текста, который может уйти наружу."""
        return text.replace(self._api_key, "***")

    def _post(self, path: str, body: dict[str, Any], timeout: float) -> httpx.Response:
        """Один POST без retry: маппит статусы и сетевые ошибки на типизированные."""
        try:
            response = self._http.post(path, json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise RequestTimeoutError(f"Request to {path} timed out after {timeout}s.") from exc
        except httpx.HTTPError as exc:
            # Не-таймаутные сетевые/протокольные сбои — upstream (тоже retryable).
            # Текст исключения httpx может содержать URL, но не заголовки; чистим защитно
            raise UpstreamError(f"Network error on {path}: {self._redact(str(exc))}") from exc

        if response.status_code == 200:
            return response

        snippet = self._redact(response.text[:ERROR_BODY_SNIPPET_LEN])
        status = response.status_code
        if status in (401, 403):
            raise AuthError(
                f"Yandex Search API auth failed ({status}). Check the Api-Key "
                f"(scope yc.search-api.execute) and folder roles (search-api.editor). API said: {snippet}"
            )
        if status == 429:
            raise QuotaError(f"Yandex Search API quota/rate limit exceeded (429). API said: {snippet}")
        if 400 <= status < 500:
            # Любой 4xx, кроме 429, — ошибка запроса: retry бессмыслен (контракт §9)
            raise BadRequestError(f"Yandex Search API rejected the request ({status}). API said: {snippet}")
        raise UpstreamError(f"Yandex Search API server error ({status}). API said: {snippet}")

    def _post_with_retry(self, path: str, body: dict[str, Any], timeout: float, wait: Any) -> httpx.Response:
        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait,
            reraise=True,
        )
        def _call() -> httpx.Response:
            return self._post(path, body, timeout)

        return _call()

    def web_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /v2/web/search → конверт ответа (JSON с base64 rawData)."""
        logger.debug("POST %s", WEB_SEARCH_PATH)
        return self._post_with_retry(WEB_SEARCH_PATH, body, self._timeout_web, self._wait_web).json()

    def image_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /v2/image/search → конверт ответа (JSON с base64 rawData)."""
        logger.debug("POST %s", IMAGE_SEARCH_PATH)
        return self._post_with_retry(IMAGE_SEARCH_PATH, body, self._timeout_web, self._wait_web).json()

    def gen_search(self, body: dict[str, Any]) -> str:
        """POST /v2/gen/search → сырое тело ответа (форму разбирает parsing)."""
        logger.debug("POST %s", GEN_SEARCH_PATH)
        return self._post_with_retry(GEN_SEARCH_PATH, body, self._timeout_gen, self._wait_gen).text
