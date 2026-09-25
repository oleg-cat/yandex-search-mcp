# yandex-search-mcp

[In English → README.md](README.md)

Self-hosted MCP-сервер для **Yandex Search API v2**: веб-поиск, поиск изображений и генеративный поиск (AI-ответ с источниками). Ориентирован на русскоязычный поиск (все 6 индексов Яндекса: ru/com/tr/kk/be/uz), STDIO или Streamable HTTP, типизированные параметры, structured output.

Замена устаревшего официального `yandex/yandex-search-mcp-server` (только tr/en, XML-парсинг регэкспами, несуществующий пин зависимостей). Эталон качества — `brave/brave-search-mcp-server`.

## Инструменты

| Инструмент | Что делает | Когда использовать |
|---|---|---|
| `yandex_web_search` | Классический веб-поиск: список документов (url, title, snippet) | Основной инструмент: факты, новости, исследование |
| `yandex_image_search` | Поиск изображений по тексту: URL и метаданные (без base64) | Картинки, диаграммы, референсы |
| `yandex_gen_search` | AI-ответ с источниками (Yandex generative search) | Дорогой/медленный; только когда нужен синтезированный ответ |

## Получение ключей

1. [Создайте API-ключ](https://yandex.cloud/ru/docs/iam/operations/authentication/manage-api-keys) для сервисного аккаунта с **scope** `yc.search-api.execute`.
2. Сервисному аккаунту нужна роль **`search-api.editor`** на каталоге (folder).
3. **Folder ID** — идентификатор каталога Yandex Cloud ([где найти](https://yandex.cloud/ru/docs/resource-manager/operations/folder/get-id)).

Документация API: [Search API v2](https://yandex.cloud/ru/docs/search-api/) · [REST-справка](https://aistudio.yandex.ru/docs/ru/search-api/api-ref/).

## Установка

Требуется Python ≥ 3.11.

**Быстрее всего — без клонирования, через [uv](https://docs.astral.sh/uv/):**

```bash
YANDEX_SEARCH_API_KEY=<key> YANDEX_FOLDER_ID=<folder> uvx --from git+https://github.com/oleg-cat/yandex-search-mcp yandex-search-mcp
```

В клиенте: `claude mcp add yandex-search -e YANDEX_SEARCH_API_KEY=<key> -e YANDEX_FOLDER_ID=<folder> -- uvx --from git+https://github.com/oleg-cat/yandex-search-mcp yandex-search-mcp`.

**Из исходников:**

```bash
git clone https://github.com/oleg-cat/yandex-search-mcp.git yandex-search-mcp
cd yandex-search-mcp
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -e .
```

Проверка (ключи передаются только через env, не через аргументы CLI):

```bash
YANDEX_SEARCH_API_KEY=<key> YANDEX_FOLDER_ID=<folder> .venv/bin/python -m yandex_search_mcp
# сервер слушает STDIO; Ctrl+C для выхода
```

## Подключение в Claude Code

```bash
claude mcp add yandex-search \
  -e YANDEX_SEARCH_API_KEY=<key> \
  -e YANDEX_FOLDER_ID=<folder> \
  -- /abs/path/to/yandex-search-mcp/.venv/bin/python -m yandex_search_mcp
```

Используйте **абсолютный путь** к python внутри venv. Проверить: `claude mcp list`.

## Подключение в Codex CLI

```bash
codex mcp add yandex-search \
  --env YANDEX_SEARCH_API_KEY=<key> \
  --env YANDEX_FOLDER_ID=<folder> \
  -- /abs/path/to/yandex-search-mcp/.venv/bin/python -m yandex_search_mcp
```

Или в `~/.codex/config.toml` вручную:

```toml
[mcp_servers.yandex-search]
command = "/abs/path/to/yandex-search-mcp/.venv/bin/python"
args = ["-m", "yandex_search_mcp"]
tool_timeout_sec = 180  # дефолт 60s мал для yandex_gen_search

[mcp_servers.yandex-search.env]
YANDEX_SEARCH_API_KEY = "<key>"
YANDEX_FOLDER_ID = "<folder>"
```

**Важно:** дефолтный `tool_timeout_sec` в Codex — 60 секунд; для `yandex_gen_search` (десятки секунд на ответ) поднимите до 180. Проверить подключение: `/mcp` внутри Codex TUI.

## Подключение в opencode

`opencode.json` в корне проекта (секреты — через `{file:...}`, не инлайном):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "yandex-search": {
      "type": "local",
      "command": ["/abs/path/to/yandex-search-mcp/.venv/bin/python", "-m", "yandex_search_mcp"],
      "environment": {
        "YANDEX_SEARCH_API_KEY": "{file:~/.secrets/yandex_search_api_key}",
        "YANDEX_FOLDER_ID": "{file:~/.secrets/yandex_folder_id}"
      }
    }
  }
}
```

## HTTP-транспорт

`YANDEX_MCP_TRANSPORT=http` поднимает MCP Streamable HTTP на `http://<host>:<port>/mcp` (по умолчанию `127.0.0.1:8000`). На localhost защита от DNS rebinding включается автоматически. **Аутентификации у эндпоинта нет** — слушайте `0.0.0.0` только за reverse proxy или во внутренней сети: любой, кто до него дотянется, тратит вашу квоту Яндекса.

## Переменные окружения

| Переменная | Обяз. | Дефолт | Описание |
|---|---|---|---|
| `YANDEX_SEARCH_API_KEY` | да* | — | Api-Key (scope `yc.search-api.execute`) |
| `YANDEX_SEARCH_API_KEY_FILE` | нет | — | *Альтернатива: путь к файлу с ключом (Docker/K8s secrets) |
| `YANDEX_FOLDER_ID` | да | — | Folder ID (роль `search-api.editor`) |
| `YANDEX_MCP_ENABLED_TOOLS` | нет | все | Whitelist инструментов через пробел или запятую, например `"yandex_web_search"` |
| `YANDEX_MCP_DISABLED_TOOLS` | нет | — | Blacklist, применяется после whitelist, например `"yandex_gen_search"` |
| `YANDEX_MCP_DEFAULT_SEARCH_TYPE` | нет | `ru` | Индекс по умолчанию: `ru/com/tr/kk/be/uz` |
| `YANDEX_MCP_DEFAULT_REGION` | нет | — | Geo-id региона по умолчанию (225 — Россия, 213 — Москва) |
| `YANDEX_MCP_TIMEOUT_WEB` | нет | `15` | Таймаут web/image-запросов, сек |
| `YANDEX_MCP_TIMEOUT_GEN` | нет | `120` | Таймаут gen-запросов, сек |
| `YANDEX_MCP_LOG_LEVEL` | нет | `INFO` | Уровень логирования (строго в stderr) |
| `YANDEX_MCP_TRANSPORT` | нет | `stdio` | `stdio` или `http` (Streamable HTTP) |
| `YANDEX_MCP_HOST` / `YANDEX_MCP_PORT` | нет | `127.0.0.1` / `8000` | Адрес для HTTP-режима |

Старт без обязательных переменных завершается ошибкой в stderr и ненулевым кодом выхода.

## Параметры инструментов

### `yandex_web_search`

| Параметр | Тип | Дефолт | Описание |
|---|---|---|---|
| `query` | str, 1–400 | — | Поддерживает операторы Яндекса: `site:`, `host:`, `date:`, `"точная фраза"`, `-минус-слово`, `\|` |
| `search_type` | `ru/com/tr/kk/be/uz` | из env | Индекс поиска |
| `n_results` | int, 1–20 | 10 | 5 — быстрая проверка факта, 15–20 — рисёрч |
| `page` | int ≥ 0 | 0 | Страница (пагинация по `has_more`) |
| `region` | int | из env | Geo-id (влияет на ранжирование): 225 РФ, 213 Москва, 2 СПб |
| `localization` | `ru/uk/be/kk/tr/en` | = search_type | Язык уведомлений выдачи |
| `period` | `all/day/2weeks/month` | all | Свежесть документов |
| `sort_by` | `relevance/time` | relevance | `time` + `period` — для новостей |
| `family_mode` | `none/moderate/strict` | moderate | Фильтрация взрослого контента |
| `fix_typos` | bool | true | Автоисправление опечаток |
| `max_passages` | int, 1–5 | 3 | Пассажей в сниппете |
| `dedupe_by_domain` | bool | false | Максимум один результат с домена |

Ответ: `{query, corrected_query, found, page, has_more, results[{rank, url, domain, title, snippet, modified_at}]}`.

### `yandex_image_search`

| Параметр | Тип | Дефолт |
|---|---|---|
| `query`, `search_type`, `n_results`, `page`, `family_mode` | как выше | — |
| `image_format` | `jpeg/gif/png` | — |
| `image_size` | `enormous/large/medium/small/tiny/wallpaper` | — |
| `orientation` | `horizontal/vertical/square` | — |
| `color` | `color/grayscale/red/…/black` | — |
| `site` | str | — |

Ответ: `{query, found, page, has_more, results[{rank, image_url, format, width, height, page_url, domain}]}` — только URL и метаданные, **без base64**.

### `yandex_gen_search`

| Параметр | Тип | Описание |
|---|---|---|
| `query` | str | Вопрос |
| `search_type` | как выше | Индекс |
| `site` / `host` | str или список, ≤ 5 | Ограничить источники сайтами (с поддоменами) / точными хостами |
| `url` | str или список, ≤ 10 | Ограничить конкретными страницами. `site`, `host`, `url` взаимоисключающие |
| `fix_typos` | bool, по умолч. true | Исправление опечаток; `false` для кода, названий продуктов и жаргона (однажды оно превратило «MCP» в «TCP») |
| `date` | str | Фильтр по дате в синтаксисе оператора `date:` без префикса, например `>20250101` |
| `lang` | str | Язык документов, ISO 639-1 (`ru`, `en`) |
| `doc_format` | `pdf/doc/rtf/xls/ods/ppt/odp/odt/odg/swf` | Формат файла документа |

Ответ: `{answer, sources[{url, title, used}], search_queries, is_answer_rejected, is_bullet_answer, problematic_answer, fixed_misspell_query}`. Лимит — **1 запрос/сек**; ответ занимает десятки секунд.

## Примеры

```
> Используй yandex_web_search: "ласточкино гнездо крым", n_results=5
> Найди свежие новости: query="курс рубля", period="day", sort_by="time"
> Картинки: yandex_image_search "сербский флаг", image_size="large"
> Синтез: yandex_gen_search "Что такое протокол MCP?", site="habr.com"
```

## Квоты

По умолчанию ([актуальные лимиты](https://aistudio.yandex.ru/docs/ru/search-api/concepts/limits)):

| Эндпоинт | RPS | В час |
|---|---|---|
| web / image | 10 | 10 000 |
| gen | 1 | 1 000 |

Сервер ретраит 429 и 5xx (3 попытки, экспоненциальный backoff, учитывает `Retry-After` до 10 с), но квоты не обходит. Генеративный поиск **не повторяется** после таймаута чтения или обрыва соединения — запрос мог уже обработаться и оплатиться.

## Troubleshooting

| Симптом | Причина | Что делать |
|---|---|---|
| `auth` (401/403) | Невалидный ключ, нет scope `yc.search-api.execute` или роли `search-api.editor` | Проверить ключ и роли сервисного аккаунта на каталоге |
| `quota` (429) | Превышен RPS или часовая квота | Подождать; gen — максимум 1 rps |
| `bad_request` (400) | Некорректные параметры (например, query > 400 символов) | Текст ошибки API включён в сообщение |
| Старт падает сразу | Не заданы `YANDEX_SEARCH_API_KEY`/`YANDEX_FOLDER_ID` | См. сообщение в stderr |
| Инструмент не виден | `YANDEX_MCP_ENABLED_TOOLS` скрывает его | Убрать переменную или добавить имя инструмента |

## Разработка

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest             # 67 тестов на живых фикстурах
npx @modelcontextprotocol/inspector .venv/bin/python -m yandex_search_mcp  # ручной smoke
```

Фикстуры снимаются заново скриптом `scripts/capture_fixtures.py` (читает `keys.json`, в git не попадает).
