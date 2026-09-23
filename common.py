from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


def _ids_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value not in out:
            out.append(value)
    return out


def _ids(name: str) -> set[int]:
    return set(_ids_list(name))




def _combined_ids(*names: str) -> set[int]:
    out: set[int] = set()
    for name in names:
        out.update(_ids(name))
    return out


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


_allowed_ids_ordered = _ids_list("ALLOWED_TELEGRAM_IDS")
_admin_ids_ordered = _ids_list("MINI_APP_ADMIN_IDS")
_legacy_access_ids_ordered = _allowed_ids_ordered or _admin_ids_ordered
_explicit_owner_ids = _ids("OWNER_TELEGRAM_IDS")
# Backward compatibility with the old LEGION setup: the access list was
# stored in ALLOWED_TELEGRAM_IDS (dispatcher) / MINI_APP_ADMIN_IDS (bot)
# in owner, helper1, helper2 order. The first id is therefore the owner
# when no separate OWNER_TELEGRAM_IDS variable exists.
_fallback_owner_ids = set(_legacy_access_ids_ordered[:1])
_owner_ids = _explicit_owner_ids or _fallback_owner_ids
_explicit_helper_ids = _ids("HELPER_TELEGRAM_IDS")
_helper_ids = _explicit_helper_ids or (set(_legacy_access_ids_ordered) - set(_owner_ids))
_photo_report_chat_ids = _combined_ids(
    "PHOTO_REPORT_CHAT_IDS",
    "PHOTO_REPORT_CHAT_ID",
    "PHOTO_REPORTS_CHAT_ID",
    "PHOTO_REPORT_GROUP_ID",
    "PHOTO_GROUP_ID",
    "PHOTO_CHAT_ID",
    "PHOTOS_CHAT_ID",
    "REPORT_CHAT_ID",
    "REPORT_GROUP_CHAT_ID",
)


@dataclass(frozen=True)
class Settings:
    bot_token: str = _first_env("BOT_TOKEN")
    database_url: str = _first_env("DATABASE_URL")
    dispatcher_api_url: str = _first_env("DISPATCHER_API_URL", "DISPATCHER_URL", "MINI_APP_URL")
    dispatcher_url: str = _first_env("MINI_APP_URL", "DISPATCHER_URL", "DISPATCHER_API_URL")
    bot_api_key: str = _first_env("BOT_API_KEY", "BOT_TOKEN")
    timezone: str = _first_env("BOT_TIMEZONE", default="Europe/Moscow")
    owner_ids: set[int] = frozenset(_owner_ids)
    helper_ids: set[int] = frozenset(_helper_ids)
    photo_report_chat_ids: set[int] = frozenset(_photo_report_chat_ids)
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
