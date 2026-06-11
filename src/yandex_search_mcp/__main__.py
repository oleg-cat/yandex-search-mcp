"""Точка входа: python -m yandex_search_mcp (STDIO-транспорт).

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
    server.run()  # STDIO по умолчанию; stdout занят протоколом, логи — в stderr


if __name__ == "__main__":
    main()
