"""Точка входа: `yandex-search-mcp` или `python -m yandex_search_mcp`.

Транспорт — STDIO по умолчанию или Streamable HTTP (YANDEX_MCP_TRANSPORT=http).

Аргументы разбираются ДО чтения конфигурации: --help/--version работают без
ключей и не запускают сервер; неизвестный аргумент — ошибка argparse (код 2),
а не молчаливый старт STDIO-сервера, который ждёт JSON в терминале.
Конфигурация валидируется ДО старта сервера: при отсутствии обязательных
переменных — внятная ошибка в stderr и ненулевой код выхода (ТЗ §7, §14).
"""

import argparse
import sys

from . import __version__
from .client import YandexSearchClient
from .config import ENV_HELP, ConfigError, Settings, setup_logging
from .server import build_server

PROG = "yandex-search-mcp"


def _build_parser() -> argparse.ArgumentParser:
    """CLI без опций запуска: вся конфигурация — через env (секреты не светятся в `ps`)."""
    width = max(len(name) for name, _ in ENV_HELP)
    env_lines = "\n".join(f"  {name:<{width}}  {desc}" for name, desc in ENV_HELP)
    epilog = (
        "Configuration is read from environment variables only:\n"
        f"{env_lines}\n\n"
        "Without arguments the server starts and speaks MCP over STDIO (or HTTP),\n"
        "so run it from an MCP client, e.g.:\n"
        f"  claude mcp add yandex-search -- uvx --from git+https://github.com/oleg-cat/yandex-search-mcp {PROG}"
    )
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="MCP server for Yandex Search API v2: web, image and generative search.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    return parser


def main(argv: list[str] | None = None) -> None:
    """Разбирает CLI, валидирует настройки, собирает сервер и запускает транспорт."""
    _build_parser().parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"{PROG}: configuration error: {exc}", file=sys.stderr)
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
