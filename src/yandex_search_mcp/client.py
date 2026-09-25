"""HTTP-клиент Yandex Search API v2: auth, таймауты, retry, типизированные ошибки.

Клиент асинхронный: sync-инструмент FastMCP выполняется прямо в event loop и
на время запроса (gen — до минут) блокирует ping, cancel и параллельные вызовы.

Retry (tenacity): только 429 / 5xx / сетевые ошибки / таймауты, 3 попытки,
экспоненциальный backoff с jitter. На 4xx (кроме 429) retry нет. Для gen
сбои с неизвестным исходом (таймаут чтения, обрыв после отправки) не
ретраятся: запрос мог уже обработаться и оплатиться, повтор утроит счёт.
Retry-After от 429 учитывается; если он дольше MAX_RETRY_AFTER — не ждём.
Api-Key никогда не попадает в сообщения ошибок и логи (ТЗ §11).
"""

import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
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
MAX_RETRY_AFTER = 10.0  # дольше ждать внутри одного вызова инструмента бессмысленно


class YandexApiError(Exception):
    """Базовая ошибка API; type/retryable идут в контракт ошибок MCP (ТЗ §10)."""

    error_type: str = "upstream"
    retryable: bool = False

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        # True — запрос ушёл, но ответа нет: API мог его выполнить (и выставить счёт)
        self.outcome_unknown = outcome_unknown


class AuthError(YandexApiError):
    """401/403 — невалидный ключ, нет scope или роли."""

    error_type = "auth"
    retryable = False


class QuotaError(YandexApiError):
    """429 — превышена квота или rps-лимит; retry_after — из заголовка, если был."""

    error_type = "quota"
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BadRequestError(YandexApiError):
    """400 — некорректное тело запроса; включает текст ответа API."""

    error_type = "bad_request"
    retryable = False


class UpstreamError(YandexApiError):
    """5xx и неожиданные статусы на стороне API."""

    error_type = "upstream"
    retryable = True


class RequestTimeoutError(YandexApiError):
    """Таймаут соединения или чтения ответа."""

    error_type = "timeout"
    retryable = True


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After в секундах (HTTP-date не поддерживаем — Яндекс его не шлёт)."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _should_retry(exc: BaseException, *, retry_unknown_outcome: bool) -> bool:
    """Ретраим только retryable; квоту с долгим Retry-After и (для gen) неизвестный исход — нет."""
    if not isinstance(exc, YandexApiError) or not exc.retryable:
        return False
    if isinstance(exc, QuotaError) and exc.retry_after is not None and exc.retry_after > MAX_RETRY_AFTER:
        return False
    if exc.outcome_unknown and not retry_unknown_outcome:
        return False
    return True


class _WaitRespectingRetryAfter:
    """Пауза перед повтором: Retry-After из 429, если он был, иначе базовая стратегия."""

    def __init__(self, fallback: Any) -> None:
        self._fallback = fallback

    def __call__(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, QuotaError) and exc.retry_after is not None:
            return exc.retry_after
        return self._fallback(retry_state)


class YandexSearchClient:
    """Один переиспользуемый httpx.AsyncClient на процесс; методы по эндпоинтам API."""

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
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        )
        # Wait-стратегии инжектируются, чтобы тесты не ждали реальный backoff
        self._wait_web = wait_web
        self._wait_gen = wait_gen

    async def aclose(self) -> None:
        """Закрывает пул соединений (для встраивания и тестов; сервер держит пул весь процесс)."""
        await self._http.aclose()

    def _redact(self, text: str) -> str:
        """Вычищает Api-Key из любого текста, который может уйти наружу."""
        return text.replace(self._api_key, "***")

    async def _post(self, path: str, body: dict[str, Any], timeout: float) -> httpx.Response:
        """Один POST без retry: маппит статусы и сетевые ошибки на типизированные."""
        try:
            response = await self._http.post(path, json=body, timeout=timeout)
        except httpx.ConnectTimeout as exc:
            raise RequestTimeoutError(f"Could not connect to {path} within {timeout}s.") from exc
        except httpx.TimeoutException as exc:
            raise RequestTimeoutError(
                f"Request to {path} timed out after {timeout}s.", outcome_unknown=True
            ) from exc
        except httpx.HTTPError as exc:
            # Не-таймаутные сетевые/протокольные сбои — upstream (тоже retryable).
            # Ошибка соединения — запрос точно не ушёл; прочие — исход неизвестен.
            # Текст исключения httpx может содержать URL, но не заголовки; чистим защитно
            raise UpstreamError(
                f"Network error on {path}: {self._redact(str(exc))}",
                outcome_unknown=not isinstance(exc, httpx.ConnectError),
            ) from exc

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
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            hint = f" Retry after {retry_after:g}s." if retry_after is not None else ""
            raise QuotaError(
                f"Yandex Search API quota/rate limit exceeded (429).{hint} API said: {snippet}",
                retry_after=retry_after,
            )
        if 400 <= status < 500:
            # Любой 4xx, кроме 429, — ошибка запроса: retry бессмыслен (контракт §9)
            raise BadRequestError(f"Yandex Search API rejected the request ({status}). API said: {snippet}")
        raise UpstreamError(f"Yandex Search API server error ({status}). API said: {snippet}")

    async def _post_with_retry(
        self, path: str, body: dict[str, Any], timeout: float, wait: Any, *, retry_unknown_outcome: bool
    ) -> httpx.Response:
        retrying = AsyncRetrying(
            retry=retry_if_exception(
                lambda exc: _should_retry(exc, retry_unknown_outcome=retry_unknown_outcome)
            ),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_WaitRespectingRetryAfter(wait),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._post(path, body, timeout)
        raise AssertionError("unreachable: tenacity either returns or reraises")

    async def web_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /v2/web/search → конверт ответа (JSON с base64 rawData)."""
        logger.debug("POST %s", WEB_SEARCH_PATH)
        response = await self._post_with_retry(
            WEB_SEARCH_PATH, body, self._timeout_web, self._wait_web, retry_unknown_outcome=True
        )
        return response.json()

    async def image_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /v2/image/search → конверт ответа (JSON с base64 rawData)."""
        logger.debug("POST %s", IMAGE_SEARCH_PATH)
        response = await self._post_with_retry(
            IMAGE_SEARCH_PATH, body, self._timeout_web, self._wait_web, retry_unknown_outcome=True
        )
        return response.json()

    async def gen_search(self, body: dict[str, Any]) -> str:
        """POST /v2/gen/search → сырое тело ответа (форму разбирает parsing).

        Сбой с неизвестным исходом не ретраится: дорогой запрос мог уже оплатиться.
        """
        logger.debug("POST %s", GEN_SEARCH_PATH)
        response = await self._post_with_retry(
            GEN_SEARCH_PATH, body, self._timeout_gen, self._wait_gen, retry_unknown_outcome=False
        )
        return response.text
