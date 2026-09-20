"""Drop-in HTTP client for the existing aiogram bot.

The Mini App is a separate Railway service. The bot calls this API whenever
an agent/order/employee/readiness status changes.
"""
import os
from datetime import date

import httpx

BASE_URL = os.getenv("DISPATCHER_API_URL", "").rstrip("/")
API_KEY = os.getenv("BOT_API_KEY", "")


async def _post(path: str, payload: dict):
    if not BASE_URL or not API_KEY:
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{BASE_URL}{path}",
            json=payload,
            headers={"X-Bot-Key": API_KEY},
        )
        r.raise_for_status()
        return r.json()


async def sync_agent(tg_id: int, full_name: str, phone: str, organization: str = ""):
    return await _post("/api/bot/agents/sync", {
        "tg_id": tg_id,
        "full_name": full_name,
        "phone": phone,
        "organization": organization,
        "active": True,
    })


async def sync_employee(
    tg_id: int | None,
    full_name: str,
    phone: str,
    group_code: str,
    height_cm: int | None = None,
    has_car: bool = False,
):
    return await _post("/api/bot/employees/sync", {
        "tg_id": tg_id,
        "full_name": full_name,
        "phone": phone,
        "group_code": group_code,
        "height_cm": height_cm,
        "has_car": has_car,
        "active": True,
    })


async def report_readiness(
    tg_id: int,
    full_name: str,
    group_code: str,
    work_date: date,
    status: str,
    height_cm: int | None = None,
    has_car: bool = False,
    phone: str = "",
    raw_text: str = "",
):
    return await _post("/api/bot/readiness", {
        "tg_id": tg_id,
        "full_name": full_name,
        "group_code": group_code,
        "work_date": work_date.isoformat(),
        "status": status,
        "height_cm": height_cm,
        "has_car": has_car,
        "phone": phone,
        "raw_text": raw_text,
    })


async def push_order(payload: dict):
    """payload uses Mini App fields: work_date, issue_time, deceased_name, etc."""
    return await _post("/api/bot/orders", payload)


async def parse_ready_form(text: str, organization_profile: str = ""):
    return await _post("/api/bot/parse-agent-order", {
        "text": text,
        "organization_profile": organization_profile,
    })


async def brigadier_close(public_id: str, contact_time: str):
    return await _post(f"/api/bot/orders/{public_id}/brigadier-close", {
        "contact_time": contact_time,
    })
