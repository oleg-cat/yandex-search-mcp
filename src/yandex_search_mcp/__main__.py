"""Точка входа: `yandex-search-mcp` или `python -m yandex_search_mcp`.

Транспорт — STDIO по умолчанию или Streamable HTTP (YANDEX_MCP_TRANSPORT=http).

Конфигурация валидируется ДО старта сервера: при отсутствии обязательных
переменных — внятная ошибка в stderr и ненулевой код выхода (ТЗ §7, §14).
"""

import sys

from .client import YandexSearchClient
from .config import ConfigError, Settings, setup_logging
from .server import build_server


def main() -> None:
    """Валидирует настройки, собирает сервер и запускает STDIO-транспорт."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"yandex-search-mcp: configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(settings.log_level)
    client = YandexSearchClient(
        api_key=settings.api_key,
        timeout_web=settings.timeout_web,
        timeout_gen=settings.timeout_gen,
    )
    server = build_server(settings, client)
    # В STDIO stdout занят протоколом, поэтому логи — только в stderr
    server.run(transport="streamable-http" if settings.transport == "http" else "stdio")


if __name__ == "__main__":
    main()
