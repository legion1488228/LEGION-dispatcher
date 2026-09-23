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


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    database_url: str = os.getenv("DATABASE_URL", "")
    dispatcher_url: str = os.getenv("DISPATCHER_URL", "")
    timezone: str = os.getenv("BOT_TIMEZONE", "Europe/Moscow")
    owner_ids: set[int] = frozenset(_ids("OWNER_TELEGRAM_IDS"))
    helper_ids: set[int] = frozenset(_ids("HELPER_TELEGRAM_IDS") or _ids("ALLOWED_TELEGRAM_IDS"))
    reminder_minutes: int = int(os.getenv("REMINDER_MINUTES", "10"))
    max_orders_per_day: int = int(os.getenv("MAX_ORDERS_PER_DAY", "2"))
    order_estimate_minutes: int = int(os.getenv("ORDER_ESTIMATE_MINUTES", "120"))
    travel_buffer_minutes: int = int(os.getenv("TRAVEL_BUFFER_MINUTES", "45"))
    dev_mode: bool = os.getenv("DEV_MODE", "0") == "1"

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
