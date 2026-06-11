"""Тесты парсинга на живых фикстурах: web (обычная/misspell/пустая), image, gen."""

import json
from pathlib import Path

import pytest

from yandex_search_mcp.models import ParseError, YandexXmlError
from yandex_search_mcp.parsing import (
    _has_more,
    _modtime_to_iso,
    decode_raw_data,
    parse_gen_response,
    parse_image_xml,
    parse_web_xml,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- web: обычная выдача ---


def test_web_normal_results():
    resp = parse_web_xml(_load("web_ru_normal.xml"), query="ласточкино гнездо крым", page=0, n_results=10)
    assert resp.found > 1000
    assert len(resp.results) == 10
    assert resp.has_more is True
    assert [r.rank for r in resp.results] == list(range(1, 11))
    first = resp.results[0]
    assert first.url.startswith("http")
    assert first.domain
    assert first.title and first.snippet


def test_web_hlword_stripped_without_word_gluing():
    """<hlword> снимается с сохранением текста; слова не слипаются."""
    resp = parse_web_xml(_load("web_ru_normal.xml"), query="q", page=0, n_results=10)
    all_text = " ".join(f"{r.title} {r.snippet}" for r in resp.results)
    assert "hlword" not in all_text  # тегов не осталось
    assert "Ласточкино" in all_text  # кириллица как есть, не экранирована
    assert "ЛасточкиноГнездо" not in all_text  # граница слов не съедена
    assert "Ласточкино Гнездо" in all_text


def test_web_misspell_gives_corrected_query():
    resp = parse_web_xml(_load("web_ru_misspell.xml"), query="пагода в маскве", page=0, n_results=10)
    assert resp.corrected_query == "погода в москве"
    assert len(resp.results) == 10


def test_web_empty_result_is_not_an_error():
    """Код 15 внутри XML — это пустая выдача, а не исключение (ТЗ §8.1)."""
    resp = parse_web_xml(_load("web_ru_empty.xml"), query="x", page=0, n_results=10)
    assert resp.results == []
    assert resp.found == 0
    assert resp.has_more is False


def test_web_other_xml_error_raises():
    xml = (
        b'<?xml version="1.0"?><yandexsearch><response><error code="32">limit</error></response></yandexsearch>'
    )
    with pytest.raises(YandexXmlError) as exc_info:
        parse_web_xml(xml, query="x", page=0, n_results=10)
    assert exc_info.value.code == "32"


def test_broken_xml_raises_parse_error_with_snippet():
    with pytest.raises(ParseError) as exc_info:
        parse_web_xml(b"<yandexsearch><respo", query="x", page=0, n_results=10)
    assert "yandexsearch" in str(exc_info.value)  # сниппет сырого ответа на месте


def test_envelope_decoding_errors():
    with pytest.raises(ParseError):
        decode_raw_data({})
    with pytest.raises(ParseError):
        decode_raw_data({"rawData": "%%%not-base64%%%"})


def test_text_normalization_and_idna_domain():
    """NBSP → пробел, soft hyphen удалён; punycode-домен декодирован в кириллицу."""
    xml = (
        '<?xml version="1.0"?><yandexsearch><response>'
        '<found priority="all">1</found><results><grouping><group>'
        "<doc><url>http://x</url><domain>xn--80aswg.xn--p1ai</domain>"
        "<title>по­года</title>"
        "<passages><passage>П. Л. Штейнгель</passage></passages></doc>"
        "</group></grouping></results></response></yandexsearch>"
    ).encode()
    resp = parse_web_xml(xml, query="q", page=0, n_results=10)
    doc = resp.results[0]
    assert doc.title == "погода"  # soft hyphen (U+00AD) удалён
    assert "\u00a0" not in doc.snippet  # NBSP заменён обычным пробелом
    assert "П. Л. Штейнгель" in doc.snippet
    assert doc.domain == "сайт.рф"  # punycode декодирован


def test_modtime_to_iso():
    assert _modtime_to_iso("20150908T164904") == "2015-09-08T16:49:04Z"
    assert _modtime_to_iso("garbage") is None
    assert _modtime_to_iso(None) is None


def test_has_more_logic():
    assert _has_more(found=100, page=0, n_results=10, returned=10) is True
    assert _has_more(found=100, page=0, n_results=10, returned=7) is False  # неполная страница
    assert _has_more(found=10, page=0, n_results=10, returned=10) is False  # found исчерпан
    assert _has_more(found=25, page=2, n_results=10, returned=10) is False  # 25 <= 30


# --- image ---


def test_image_results_urls_and_sizes_no_base64():
    resp = parse_image_xml(_load("image_ru.xml"), query="сербский флаг", page=0, n_results=10)
    assert 0 < len(resp.results) <= 10
    dumped = resp.model_dump_json()
    assert "base64" not in dumped and ";base64," not in dumped
    first = resp.results[0]
    assert first.image_url.startswith("http")
    assert isinstance(first.width, int) and isinstance(first.height, int)
    assert first.page_url and first.page_url.startswith("http")
    assert first.domain


def test_image_doc_without_properties_keeps_rank_and_has_more():
    """Пропуск doc без image-properties не сдвигает rank и не занижает has_more."""
    xml = (
        b'<?xml version="1.0"?><yandexsearch><response>'
        b'<found priority="phrase">3</found><results><grouping>'
        b"<group><doc><url>http://a</url><domain>a.ru</domain></doc></group>"
        b"<group><doc><url>http://b</url><domain>b.ru</domain>"
        b"<image-properties><image-link>http://b/i.jpg</image-link>"
        b"<mime-type>jpg</mime-type><original-width>10</original-width>"
        b"<original-height>20</original-height></image-properties></doc></group>"
        b"</grouping></results></response></yandexsearch>"
    )
    resp = parse_image_xml(xml, query="q", page=0, n_results=2)
    assert len(resp.results) == 1
    assert resp.results[0].rank == 2  # позиция на API-странице, а не порядковый номер валидных
    assert resp.has_more is True  # страница API была полной (2 doc), found=3 > 2


def test_web_doc_without_url_skipped_with_rank_gap():
    xml = (
        '<?xml version="1.0"?><yandexsearch><response>'
        '<found priority="phrase">2</found><results><grouping>'
        "<group><doc><domain>a.ru</domain><title>без url</title></doc></group>"
        "<group><doc><url>http://b</url><domain>b.ru</domain><title>t</title></doc></group>"
        "</grouping></results></response></yandexsearch>"
    ).encode()
    resp = parse_web_xml(xml, query="q", page=0, n_results=2)
    assert [r.rank for r in resp.results] == [2]
    assert resp.results[0].url == "http://b"


# --- gen ---


def test_gen_live_fixture_array_form():
    """Живой gen-ответ — JSON-массив [{...}]; парсим без трюков вида [1:-1]."""
    raw = _load("gen_raw_response.txt").decode("utf-8")
    resp = parse_gen_response(raw)
    assert "Model Context Protocol" in resp.answer
    assert len(resp.sources) == 5
    assert sum(1 for s in resp.sources if s.used) == 3
    assert all(s.url.startswith("http") for s in resp.sources)
    assert resp.is_answer_rejected is False
    assert resp.fixed_misspell_query  # API «поправил» MCP→TCP — поле прозрачности


def test_gen_accepts_single_object_too():
    obj = {"message": {"content": "ответ"}, "sources": [], "isAnswerRejected": True}
    resp = parse_gen_response(json.dumps(obj))
    assert resp.answer == "ответ"
    assert resp.is_answer_rejected is True
    assert resp.fixed_misspell_query is None


def test_gen_invalid_payloads_raise_parse_error():
    with pytest.raises(ParseError):
        parse_gen_response("not json at all")
    with pytest.raises(ParseError):
        parse_gen_response("[]")
    with pytest.raises(ParseError):
        parse_gen_response('"just a string"')
