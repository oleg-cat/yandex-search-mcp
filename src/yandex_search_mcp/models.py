"""Pydantic-модели ответов инструментов и исключения разбора.

Модели возвращаются из инструментов напрямую: FastMCP генерирует по ним
outputSchema и structuredContent. Поля, не встречающиеся в живой выдаче
(например page_title у image-документов), в контракт не включены.
"""

from pydantic import BaseModel, Field


class ParseError(Exception):
    """Не удалось разобрать ответ API; несёт первые ~200 символов сырого ответа."""

    error_type = "parse"
    retryable = False

    def __init__(self, message: str, raw_snippet: str = "") -> None:
        suffix = f" Raw response starts with: {raw_snippet!r}" if raw_snippet else ""
        super().__init__(message + suffix)


class YandexXmlError(Exception):
    """`<error code=N>` внутри XML-ответа (кроме кода 15 — «ничего не найдено»)."""

    error_type = "bad_request"
    retryable = False

    def __init__(self, code: str | None, message: str) -> None:
        self.code = code
        super().__init__(f"Yandex Search XML error (code={code}): {message}")


class WebResult(BaseModel):
    """Один документ веб-выдачи."""

    rank: int = Field(description="1-based global rank: page * n_results + position")
    url: str
    domain: str
    title: str
    snippet: str
    modified_at: str | None = Field(default=None, description="ISO 8601 or null if unknown")


class WebSearchResponse(BaseModel):
    """Контракт ответа yandex_web_search (ТЗ §8.1)."""

    query: str
    corrected_query: str | None = Field(
        default=None, description="Query as corrected by Yandex (misspell/reask), null if not corrected"
    )
    found: int = Field(description="Estimated total number of documents")
    page: int
    has_more: bool
    results: list[WebResult]


class ImageResult(BaseModel):
    """Одно изображение из выдачи. Только URL и метаданные — никакого base64."""

    rank: int
    image_url: str
    format: str | None = Field(default=None, description="Image format, e.g. jpg/png/gif")
    width: int | None = None
    height: int | None = None
    page_url: str | None = Field(default=None, description="URL of the page hosting the image")
    domain: str


class ImageSearchResponse(BaseModel):
    """Контракт ответа yandex_image_search (ТЗ §8.2)."""

    query: str
    found: int
    page: int
    has_more: bool
    results: list[ImageResult]


class GenSource(BaseModel):
    """Источник, использованный генеративным поиском."""

    url: str
    title: str | None = None
    used: bool


class GenSearchResponse(BaseModel):
    """Контракт ответа yandex_gen_search (ТЗ §8.3, приведён к фактическому ответу API)."""

    answer: str
    sources: list[GenSource]
    is_answer_rejected: bool = Field(
        default=False, description="True when Yandex refused to generate an answer"
    )
    fixed_misspell_query: str | None = Field(
        default=None, description="Query after Yandex typo correction, null if unchanged"
    )
