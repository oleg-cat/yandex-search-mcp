"""Снятие живых фикстур Yandex Search API v2 — ground truth для парсера.

Standalone-скрипт: делает по одному вызову web (обычный / с опечаткой /
пустая выдача), image и gen, сохраняет ответы в tests/fixtures/. Для gen
сохраняется сырое тело byte-for-byte: API возвращает JSON-массив `[{...}]`.

Ключи берутся из env (YANDEX_SEARCH_API_KEY / YANDEX_FOLDER_ID); как
fallback читается keys.json в корне проекта (файл в git не попадает).

Запуск: .venv/bin/python scripts/capture_fixtures.py

Self-check: перед завершением проверяет, что ApiKey/FolderId не встречаются
ни в одной фикстуре (фикстуры идут в git).
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
KEYS_PATH = PROJECT_ROOT / "keys.json"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"

BASE_URL = "https://searchapi.api.cloud.yandex.net"
TIMEOUT_WEB = 30.0
TIMEOUT_GEN = 180.0
GEN_SLEEP_BEFORE = 1.5  # лимит gen — 1 rps

WEB_QUERIES: dict[str, str] = {
    "web_ru_normal": "ласточкино гнездо крым",
    "web_ru_misspell": "пагода в маскве",
    # site: на несуществующий домен — единственный надёжный способ получить пустую
    # выдачу (на гибберише и фразах в кавычках срабатывает reask и находит документы).
    # Пустая выдача приходит как <error code="15"> внутри XML, не как пустой <results>.
    "web_ru_empty": "обнубиляция site:nonexistent-domain-8472xq.ru",
}
IMAGE_QUERY = "сербский флаг"
GEN_QUERY = "Что такое протокол MCP (Model Context Protocol)?"


def load_keys() -> tuple[str, str]:
    """Берёт ApiKey и FolderId из env; fallback — keys.json в корне проекта."""
    api_key = os.environ.get("YANDEX_SEARCH_API_KEY", "").strip()
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if api_key and folder_id:
        return api_key, folder_id
    if KEYS_PATH.exists():
        data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        return data["ApiKey"], data["FolderId"]
    print(
        "Set YANDEX_SEARCH_API_KEY and YANDEX_FOLDER_ID env vars "
        "(or put keys.json with ApiKey/FolderId next to pyproject.toml).",
        file=sys.stderr,
    )
    sys.exit(1)


def web_body(query: str, folder_id: str) -> dict:
    """Тело POST /v2/web/search: RU-поиск, XML-ответ, 10 результатов."""
    return {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "familyMode": "FAMILY_MODE_MODERATE",
            "fixTypoMode": "FIX_TYPO_MODE_ON",
        },
        "groupSpec": {
            "groupMode": "GROUP_MODE_FLAT",
            "groupsOnPage": 10,
            "docsInGroup": 1,
        },
        "maxPassages": 3,
        "l10n": "LOCALIZATION_RU",
        "folderId": folder_id,
        "responseFormat": "FORMAT_XML",
    }


def image_body(query: str, folder_id: str) -> dict:
    """Тело POST /v2/image/search: RU-поиск картинок, 10 результатов."""
    return {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "familyMode": "FAMILY_MODE_MODERATE",
        },
        "docsOnPage": 10,
        "folderId": folder_id,
    }


def gen_body(query: str, folder_id: str) -> dict:
    """Тело POST /v2/gen/search."""
    return {
        "messages": [{"content": query, "role": "ROLE_USER"}],
        "folderId": folder_id,
        "fixMisspell": True,
    }


def save_xml_from_envelope(name: str, resp: httpx.Response) -> None:
    """Декодирует rawData (base64 → XML) и сохраняет фикстуру."""
    envelope = resp.json()
    xml_bytes = base64.b64decode(envelope["rawData"])
    out = FIXTURES_DIR / f"{name}.xml"
    out.write_bytes(xml_bytes)
    print(f"  -> {out.name}: {len(xml_bytes)} bytes XML")


def main() -> None:
    """Снимает все фикстуры и прогоняет self-check на утечку ключей."""
    api_key, folder_id = load_keys()
    headers = {"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"}
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(base_url=BASE_URL, headers=headers) as client:
        # --- web x3 ---
        envelope_saved = False
        for name, query in WEB_QUERIES.items():
            print(f"web: {query!r}")
            resp = client.post("/v2/web/search", json=web_body(query, folder_id), timeout=TIMEOUT_WEB)
            print(f"  status={resp.status_code}")
            resp.raise_for_status()
            if not envelope_saved:
                # Конверт ответа с усечённым rawData — для документации формы
                env = resp.json()
                env["rawData"] = env["rawData"][:80] + "...TRUNCATED"
                (FIXTURES_DIR / "web_envelope.json").write_text(
                    json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                envelope_saved = True
            save_xml_from_envelope(name, resp)
            time.sleep(0.3)

        # --- image ---
        print(f"image: {IMAGE_QUERY!r}")
        resp = client.post("/v2/image/search", json=image_body(IMAGE_QUERY, folder_id), timeout=TIMEOUT_WEB)
        print(f"  status={resp.status_code}")
        resp.raise_for_status()
        save_xml_from_envelope("image_ru", resp)

        # --- gen: сырое тело byte-for-byte ---
        print(f"gen: {GEN_QUERY!r} (ждём, лимит 1 rps)")
        time.sleep(GEN_SLEEP_BEFORE)
        resp = client.post("/v2/gen/search", json=gen_body(GEN_QUERY, folder_id), timeout=TIMEOUT_GEN)
        print(f"  status={resp.status_code}, content-type={resp.headers.get('content-type')}")
        (FIXTURES_DIR / "gen_raw_response.txt").write_bytes(resp.content)
        meta = (
            f"status: {resp.status_code}\n"
            f"content-type: {resp.headers.get('content-type')}\n"
            f"content-length: {len(resp.content)}\n"
            f"first-bytes: {resp.content[:60]!r}\n"
            f"last-bytes: {resp.content[-60:]!r}\n"
        )
        (FIXTURES_DIR / "gen_meta.txt").write_text(meta, encoding="utf-8")
        print(f"  -> gen_raw_response.txt: {len(resp.content)} bytes")
        resp.raise_for_status()

    # --- self-check: секреты не должны попасть в фикстуры ---
    leaks: list[str] = []
    for path in FIXTURES_DIR.iterdir():
        content = path.read_bytes()
        if api_key.encode() in content:
            leaks.append(f"{path.name}: содержит ApiKey")
        if folder_id.encode() in content:
            leaks.append(f"{path.name}: содержит FolderId")
    if leaks:
        print("\nLEAK DETECTED — фикстуры НЕ коммитить:", *leaks, sep="\n  ", file=sys.stderr)
        sys.exit(1)
    print("\nSelf-check OK: ApiKey/FolderId в фикстурах не найдены.")


if __name__ == "__main__":
    main()
