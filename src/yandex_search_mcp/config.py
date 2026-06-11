"""Конфигурация из переменных окружения, валидация при старте.

Все логи — строго в stderr: stdout зарезервирован под протокол MCP (STDIO).
Секреты передаются только через env (ТЗ §11): CLI-аргументы видны в `ps`.
"""

import logging
import os
import sys
from dataclasses import dataclass

ENV_API_KEY = "YANDEX_SEARCH_API_KEY"
ENV_FOLDER_ID = "YANDEX_FOLDER_ID"
ENV_ENABLED_TOOLS = "YANDEX_MCP_ENABLED_TOOLS"
ENV_DEFAULT_SEARCH_TYPE = "YANDEX_MCP_DEFAULT_SEARCH_TYPE"
ENV_DEFAULT_REGION = "YANDEX_MCP_DEFAULT_REGION"
ENV_TIMEOUT_WEB = "YANDEX_MCP_TIMEOUT_WEB"
ENV_TIMEOUT_GEN = "YANDEX_MCP_TIMEOUT_GEN"
ENV_LOG_LEVEL = "YANDEX_MCP_LOG_LEVEL"

ALL_TOOLS = ("yandex_web_search", "yandex_image_search", "yandex_gen_search")
VALID_SEARCH_TYPES = ("ru", "com", "tr", "kk", "be", "uz")


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

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """Читает настройки из env; кидает ConfigError с внятным сообщением."""
        env = os.environ if env is None else env

        api_key = env.get(ENV_API_KEY, "").strip()
        folder_id = env.get(ENV_FOLDER_ID, "").strip()
        missing = [name for name, val in ((ENV_API_KEY, api_key), (ENV_FOLDER_ID, folder_id)) if not val]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                f"{ENV_API_KEY} is an Api-Key with scope yc.search-api.execute; "
                f"{ENV_FOLDER_ID} is a Yandex Cloud folder id with role search-api.editor."
            )

        # Пустая строка эквивалентна отсутствию переменной: включены все инструменты
        raw_tools = env.get(ENV_ENABLED_TOOLS, "").split()
        if raw_tools:
            unknown = [t for t in raw_tools if t not in ALL_TOOLS]
            if unknown:
                raise ConfigError(
                    f"{ENV_ENABLED_TOOLS} contains unknown tool(s): {', '.join(unknown)}. "
                    f"Valid tools: {', '.join(ALL_TOOLS)}."
                )
            enabled_tools = tuple(t for t in ALL_TOOLS if t in raw_tools)
        else:
            enabled_tools = ALL_TOOLS

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

        return cls(
            api_key=api_key,
            folder_id=folder_id,
            enabled_tools=enabled_tools,
            default_search_type=default_search_type,
            default_region=default_region,
            timeout_web=_timeout(ENV_TIMEOUT_WEB, 15.0),
            timeout_gen=_timeout(ENV_TIMEOUT_GEN, 120.0),
            log_level=env.get(ENV_LOG_LEVEL, "INFO").strip().upper() or "INFO",
        )


def setup_logging(level: str) -> None:
    """Настраивает логирование в stderr (stdout — транспорт MCP)."""
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
