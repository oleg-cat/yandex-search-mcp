"""Разбор ответов API в pydantic-модели: XML (web, image) и JSON (gen).

Парсер написан по живым фикстурам tests/fixtures/ (ground truth, ТЗ §12.1):
- пустая выдача приходит как <error code="15"> внутри XML, а не пустой <results>;
- исправление опечаток приходит элементом <reask> (rule=Misspell), <misspell> — резерв;
- <found priority="..."> есть и в <response>, и в <grouping> — берём только из <response>;
- gen-ответ — JSON-МАССИВ объектов (отсюда хак [1:-1] в официальном демо);
- XML — только defusedxml: содержимое приходит из недоверенного веба (ТЗ §11).
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Any
from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError as ETParseError

from defusedxml import ElementTree as DET

from .models import (
    GenSearchResponse,
    GenSource,
    ImageResult,
    ImageSearchResponse,
    ParseError,
    WebResult,
    WebSearchResponse,
    YandexXmlError,
)

EMPTY_RESULT_ERROR_CODE = "15"  # «Искомая комбинация слов нигде не встречается»
FOUND_PRIORITY_ORDER = ("phrase", "strict", "all")  # от строгого к широкому
RAW_SNIPPET_LEN = 200


def decode_raw_data(envelope: dict[str, Any]) -> bytes:
    """Достаёт XML из конверта ответа: rawData — base64-строка."""
    raw = envelope.get("rawData")
    if not raw:
        raise ParseError("API response envelope has no 'rawData' field.", str(envelope)[:RAW_SNIPPET_LEN])
    try:
        return base64.b64decode(raw)
    except (binascii.Error, ValueError) as exc:
        raise ParseError(f"rawData is not valid base64: {exc}.", str(raw)[:RAW_SNIPPET_LEN]) from exc


def _parse_xml_response(xml_bytes: bytes) -> Element:
    """Разбирает XML и возвращает узел <response>; код 15 пробрасывается как None-маркер."""
    try:
        root = DET.fromstring(xml_bytes)
    except (ETParseError, ValueError) as exc:
        raise ParseError(
            f"Response is not valid XML: {exc}.",
            xml_bytes[:RAW_SNIPPET_LEN].decode("utf-8", errors="replace"),
        ) from exc
    response = root.find("response")
    if response is None:
        raise ParseError(
            "XML has no <response> element.",
            xml_bytes[:RAW_SNIPPET_LEN].decode("utf-8", errors="replace"),
        )
    return response


def _check_xml_error(response: Element) -> bool:
    """True — выдача пуста (код 15); прочие <error> — исключение YandexXmlError."""
    error = response.find("error")
    if error is None:
        return False
    code = error.get("code")
    if code == EMPTY_RESULT_ERROR_CODE:
        return True
    raise YandexXmlError(code, (error.text or "").strip())


def _text(element: Element | None) -> str:
    """Весь текст узла с потомками (снимает <hlword>, сохраняя текст и пробелы).

    NBSP заменяется на пробел, soft hyphen удаляется — невидимый типографский
    мусор Яндекса не должен попадать в LLM-вывод. Границы слов берутся из XML
    как есть (text/tail); таймзон и не-UTF-8 кодировок API v2 не отдаёт.
    """
    if element is None:
        return ""
    return "".join(element.itertext()).replace(" ", " ").replace("­", "").strip()


def _decode_idna_domain(domain: str) -> str:
    """Punycode-домен (xn--…) → кириллица для читаемости LLM; при сбое — как есть."""
    if "xn--" not in domain:
        return domain
    try:
        return domain.encode("ascii").decode("idna")
    except (UnicodeError, ValueError):
        return domain


def _found_count(response: Element) -> int:
    """Счётчик найденного: строжайший доступный priority, только из <response>."""
    by_priority = {f.get("priority"): (f.text or "0") for f in response.findall("found")}
    for priority in FOUND_PRIORITY_ORDER:
        if priority in by_priority:
            try:
                return int(by_priority[priority])
            except ValueError:
                return 0
    return 0


def _corrected_query(response: Element) -> str | None:
    """Исправленный запрос: живой API шлёт <reask>, <misspell> поддержан как резерв."""
    for tag in ("reask", "misspell"):
        node = response.find(tag)
        if node is not None:
            corrected = _text(node.find("text"))
            if corrected:
                return corrected
    return None


def _int_prop(props: Element, tag: str) -> int | None:
    """Целочисленное поле из <image-properties> или None."""
    raw = props.findtext(tag)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _modtime_to_iso(raw: str | None) -> str | None:
    """`20150908T164904` → `2015-09-08T16:49:04Z` (API не указывает зону; считаем UTC)."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y%m%dT%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _has_more(found: int, page: int, n_results: int, returned: int) -> bool:
    """Есть ли следующая страница: страница полна И оценка found её обещает."""
    return returned == n_results and found > (page + 1) * n_results


def parse_web_xml(xml_bytes: bytes, query: str, page: int, n_results: int) -> WebSearchResponse:
    """XML веб-выдачи → WebSearchResponse (контракт ТЗ §8.1)."""
    response = _parse_xml_response(xml_bytes)
    if _check_xml_error(response):
        return WebSearchResponse(query=query, found=0, page=page, has_more=False, results=[])

    # rank — позиция на API-странице (пропуск битого doc оставляет «дыру», а не
    # сдвигает ранги); has_more — от сырого числа doc, а не от числа валидных.
    docs = response.findall("results/grouping/group/doc")
    results: list[WebResult] = []
    for position, doc in enumerate(docs, start=1):
        url = doc.findtext("url", default="")
        if not url:
            continue  # результат без URL бесполезен агенту
        snippet_parts = [_text(p) for p in doc.findall("passages/passage")]
        snippet = " ".join(part for part in snippet_parts if part)
        if not snippet:
            # Фолбэк по живой выдаче: расширенный текст из properties, затем headline
            snippet = _text(doc.find("properties/extended-text")) or _text(doc.find("headline"))
        results.append(
            WebResult(
                rank=page * n_results + position,
                url=url,
                domain=_decode_idna_domain(doc.findtext("domain", default="")),
                title=_text(doc.find("title")),
                snippet=snippet,
                modified_at=_modtime_to_iso(doc.findtext("modtime")),
            )
        )

    found = _found_count(response)
    return WebSearchResponse(
        query=query,
        corrected_query=_corrected_query(response),
        found=found,
        page=page,
        has_more=_has_more(found, page, n_results, len(docs)),
        results=results,
    )


def parse_image_xml(xml_bytes: bytes, query: str, page: int, n_results: int) -> ImageSearchResponse:
    """XML image-выдачи → ImageSearchResponse. Только URL и метаданные, без base64."""
    response = _parse_xml_response(xml_bytes)
    if _check_xml_error(response):
        return ImageSearchResponse(query=query, found=0, page=page, has_more=False, results=[])

    docs = response.findall("results/grouping/group/doc")
    results: list[ImageResult] = []
    for position, doc in enumerate(docs, start=1):
        props = doc.find("image-properties")
        if props is None:
            continue  # doc без image-properties — не изображение; rank сохраняет позицию
        results.append(
            ImageResult(
                rank=page * n_results + position,
                image_url=props.findtext("image-link") or doc.findtext("url", default=""),
                format=props.findtext("mime-type") or None,
                width=_int_prop(props, "original-width"),
                height=_int_prop(props, "original-height"),
                page_url=props.findtext("html-link") or None,
                domain=_decode_idna_domain(doc.findtext("domain", default="")),
            )
        )

    found = _found_count(response)
    return ImageSearchResponse(
        query=query,
        found=found,
        page=page,
        has_more=_has_more(found, page, n_results, len(docs)),
        results=results,
    )


def parse_gen_response(raw_text: str) -> GenSearchResponse:
    """Сырое тело /v2/gen/search → GenSearchResponse.

    Живой API возвращает JSON-МАССИВ `[{...}]`; берём последний элемент
    (на случай чанков частичных результатов). Единый объект тоже принимается.
    Никаких трюков вида `raw[1:-1]` (антипаттерн официального демо).
    """
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"Gen search response is not valid JSON: {exc}.", raw_text[:RAW_SNIPPET_LEN]) from exc

    if isinstance(payload, list):
        if not payload:
            raise ParseError("Gen search response is an empty JSON array.", raw_text[:RAW_SNIPPET_LEN])
        payload = payload[-1]
    if not isinstance(payload, dict):
        raise ParseError(
            f"Gen search response has unexpected type {type(payload).__name__}.",
            raw_text[:RAW_SNIPPET_LEN],
        )

    message = payload.get("message") or {}
    sources = [
        GenSource(url=s.get("url", ""), title=s.get("title") or None, used=bool(s.get("used", False)))
        for s in payload.get("sources", [])
        if s.get("url")
    ]
    return GenSearchResponse(
        answer=message.get("content", ""),
        sources=sources,
        is_answer_rejected=bool(payload.get("isAnswerRejected", False)),
        fixed_misspell_query=payload.get("fixedMisspellQuery") or None,
    )
