import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request


@dataclass
class TelegramUser:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(x for x in [self.first_name, self.last_name] if x).strip() or self.username or str(self.id)


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> TelegramUser:
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData missing")

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram hash missing")

    auth_date = int(data.get("auth_date", "0") or 0)
    if not auth_date or abs(int(time.time()) - auth_date) > max_age_seconds:
        raise HTTPException(status_code=401, detail="Telegram session expired")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")

    try:
        raw_user = json.loads(data.get("user", "{}"))
        return TelegramUser(
            id=int(raw_user["id"]),
            first_name=raw_user.get("first_name", ""),
            last_name=raw_user.get("last_name", ""),
            username=raw_user.get("username", ""),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Telegram user missing") from exc


def allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_TELEGRAM_IDS", "")
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


async def require_admin(
    request: Request,
    x_telegram_init_data: str | None = Header(default=None),
    x_dev_telegram_id: str | None = Header(default=None),
) -> TelegramUser:
    dev_mode = os.getenv("DEV_MODE", "0") == "1"
    bot_token = os.getenv("BOT_TOKEN", "").strip()

    if dev_mode and x_dev_telegram_id:
        user = TelegramUser(id=int(x_dev_telegram_id), first_name="DEV")
    else:
        if not bot_token:
            raise HTTPException(status_code=500, detail="BOT_TOKEN is not configured")
        user = validate_init_data(x_telegram_init_data or "", bot_token)

    ids = allowed_ids()
    if not ids:
        raise HTTPException(status_code=403, detail="ALLOWED_TELEGRAM_IDS is empty")
    if user.id not in ids:
        raise HTTPException(status_code=403, detail="No access to dispatcher")
    return user


def require_bot_key(x_bot_key: str | None = None) -> None:
    expected = os.getenv("BOT_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=500, detail="BOT_API_KEY is not configured")
    if not x_bot_key or not hmac.compare_digest(x_bot_key, expected):
        raise HTTPException(status_code=403, detail="Invalid bot API key")
