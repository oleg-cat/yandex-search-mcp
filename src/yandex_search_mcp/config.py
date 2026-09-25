"""Конфигурация из переменных окружения, валидация при старте.

Все логи — строго в stderr: stdout зарезервирован под протокол MCP (STDIO).
Секреты передаются только через env (ТЗ §11): CLI-аргументы видны в `ps`.
Ключ можно положить в файл (*_FILE) — так работают Docker/K8s secrets.
"""

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ENV_API_KEY = "YANDEX_SEARCH_API_KEY"
ENV_API_KEY_FILE = "YANDEX_SEARCH_API_KEY_FILE"
ENV_FOLDER_ID = "YANDEX_FOLDER_ID"
ENV_ENABLED_TOOLS = "YANDEX_MCP_ENABLED_TOOLS"
ENV_DISABLED_TOOLS = "YANDEX_MCP_DISABLED_TOOLS"
ENV_DEFAULT_SEARCH_TYPE = "YANDEX_MCP_DEFAULT_SEARCH_TYPE"
ENV_DEFAULT_REGION = "YANDEX_MCP_DEFAULT_REGION"
ENV_TIMEOUT_WEB = "YANDEX_MCP_TIMEOUT_WEB"
ENV_TIMEOUT_GEN = "YANDEX_MCP_TIMEOUT_GEN"
ENV_LOG_LEVEL = "YANDEX_MCP_LOG_LEVEL"
ENV_TRANSPORT = "YANDEX_MCP_TRANSPORT"
ENV_HOST = "YANDEX_MCP_HOST"
ENV_PORT = "YANDEX_MCP_PORT"

ALL_TOOLS = ("yandex_web_search", "yandex_image_search", "yandex_gen_search")
VALID_SEARCH_TYPES = ("ru", "com", "tr", "kk", "be", "uz")
VALID_TRANSPORTS = ("stdio", "http")


class ConfigError(Exception):
    """Невалидная или отсутствующая конфигурация (выводится в stderr при старте)."""


@dataclass(frozen=True)
class Settings:
    """Настройки сервера, собранные из переменных окружения (ТЗ §7)."""

    api_key: str
    folder_id: str
    enabled_tools: tuple[str, ...] = ALL_TOOLS
    default_search_type: str = "ru"
    default_region: int | None = None
    timeout_web: float = 15.0
    timeout_gen: float = 120.0
    log_level: str = "INFO"
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 8000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Читает настройки из env; кидает ConfigError с внятным сообщением."""
        env = os.environ if env is None else env

        api_key = env.get(ENV_API_KEY, "").strip()
        key_file = env.get(ENV_API_KEY_FILE, "").strip()
        if not api_key and key_file:
            try:
                api_key = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"{ENV_API_KEY_FILE}={key_file!r} cannot be read: {exc.strerror}.") from None
        folder_id = env.get(ENV_FOLDER_ID, "").strip()
        missing = [name for name, val in ((ENV_API_KEY, api_key), (ENV_FOLDER_ID, folder_id)) if not val]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"The key may also be given as a file path in {ENV_API_KEY_FILE}. "
                f"{ENV_API_KEY} is an Api-Key with scope yc.search-api.execute; "
                f"{ENV_FOLDER_ID} is a Yandex Cloud folder id with role search-api.editor."
            )

        def _tool_list(name: str) -> list[str]:
            # Разделители — пробелы и/или запятые; пустая строка = переменная не задана
            tools = env.get(name, "").replace(",", " ").split()
            unknown = [t for t in tools if t not in ALL_TOOLS]
            if unknown:
                raise ConfigError(
                    f"{name} contains unknown tool(s): {', '.join(unknown)}. "
                    f"Valid tools: {', '.join(ALL_TOOLS)}."
                )
            return tools

        allowed = _tool_list(ENV_ENABLED_TOOLS) or list(ALL_TOOLS)
        denied = _tool_list(ENV_DISABLED_TOOLS)
        enabled_tools = tuple(t for t in ALL_TOOLS if t in allowed and t not in denied)
        if not enabled_tools:
            raise ConfigError(f"{ENV_ENABLED_TOOLS}/{ENV_DISABLED_TOOLS} leave no tools enabled.")

        default_search_type = env.get(ENV_DEFAULT_SEARCH_TYPE, "ru").strip().lower()
        if default_search_type not in VALID_SEARCH_TYPES:
            raise ConfigError(
                f"{ENV_DEFAULT_SEARCH_TYPE}={default_search_type!r} is invalid. "
                f"Valid values: {', '.join(VALID_SEARCH_TYPES)}."
            )

        default_region: int | None = None
        raw_region = env.get(ENV_DEFAULT_REGION, "").strip()
        if raw_region:
            try:
                default_region = int(raw_region)
            except ValueError:
                raise ConfigError(
                    f"{ENV_DEFAULT_REGION}={raw_region!r} must be an integer geo-id (e.g. 213)."
                ) from None

        def _timeout(name: str, default: float) -> float:
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                raise ConfigError(f"{name}={raw!r} must be a number of seconds.") from None
            if value <= 0:
                raise ConfigError(f"{name} must be positive, got {value}.")
            return value

        transport = env.get(ENV_TRANSPORT, "stdio").strip().lower() or "stdio"
        if transport not in VALID_TRANSPORTS:
            raise ConfigError(
                f"{ENV_TRANSPORT}={transport!r} is invalid. Valid values: {', '.join(VALID_TRANSPORTS)}."
            )

        raw_port = env.get(ENV_PORT, "").strip()
        http_port = 8000
        if raw_port:
            try:
                http_port = int(raw_port)
            except ValueError:
                raise ConfigError(f"{ENV_PORT}={raw_port!r} must be an integer.") from None
            if not 0 < http_port < 65536:
                raise ConfigError(f"{ENV_PORT} must be in 1..65535, got {http_port}.")

        return cls(
            api_key=api_key,
            folder_id=folder_id,
            enabled_tools=enabled_tools,
            default_search_type=default_search_type,
            default_region=default_region,
            timeout_web=_timeout(ENV_TIMEOUT_WEB, 15.0),
            timeout_gen=_timeout(ENV_TIMEOUT_GEN, 120.0),
            log_level=env.get(ENV_LOG_LEVEL, "INFO").strip().upper() or "INFO",
            transport=transport,
            http_host=env.get(ENV_HOST, "").strip() or "127.0.0.1",
            http_port=http_port,
        )


def setup_logging(level: str) -> None:
    """Настраивает логирование в stderr (stdout — транспорт MCP)."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
