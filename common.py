from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "")
    out: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(int(part))
    return out


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class Settings:
    bot_token: str = _first_env("BOT_TOKEN")
    database_url: str = _first_env("DATABASE_URL")
    dispatcher_api_url: str = _first_env("DISPATCHER_API_URL", "DISPATCHER_URL", "MINI_APP_URL")
    dispatcher_url: str = _first_env("MINI_APP_URL", "DISPATCHER_URL", "DISPATCHER_API_URL")
    bot_api_key: str = _first_env("BOT_API_KEY", "BOT_TOKEN")
    timezone: str = _first_env("BOT_TIMEZONE", default="Europe/Moscow")
    owner_ids: set[int] = frozenset(_ids("OWNER_TELEGRAM_IDS") or _ids("MINI_APP_ADMIN_IDS"))
    helper_ids: set[int] = frozenset(_ids("HELPER_TELEGRAM_IDS") or _ids("ALLOWED_TELEGRAM_IDS"))
    reminder_minutes: int = int(_first_env("REMINDER_MINUTES", default="10"))
    max_orders_per_day: int = int(_first_env("MAX_ORDERS_PER_DAY", default="2"))
    order_estimate_minutes: int = int(_first_env("ORDER_ESTIMATE_MINUTES", default="120"))
    travel_buffer_minutes: int = int(_first_env("TRAVEL_BUFFER_MINUTES", default="45"))
    dev_mode: bool = _first_env("DEV_MODE", default="0") == "1"

    @property
    def allowed_dispatcher_ids(self) -> set[int]:
        return set(self.owner_ids) | set(self.helper_ids)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)


settings = Settings()

READINESS_STATUSES = {
    "ready": "Готов",
    "ready_car": "Готов + авто",
    "main": "На основной",
    "off": "Выходной",
}

GROUP_LABELS = {
    "brigadier": "Бригадиры",
    "main": "Основной",
    "cashless": "Безнал",
    "reserve": "Резерв",
}

CATEGORY_LABELS = {
    "standard": "Стандарт",
    "vip": "Вип",
    "elite": "Элит",
}

SOURCE_LABELS = {
    "private": "Частный",
    "gbu": "ГБУ",
}
