from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import db
from common import settings

app = FastAPI(title="LEGION Dispatcher V28", version="28.0")
INDEX_FILE = Path(__file__).with_name("index.html")


class OrderPatch(BaseModel):
    work_date: date | None = None
    source: str | None = None
    agent_name: str | None = None
    agent_phone: str | None = None
    deceased_name: str | None = None
    issue_time: str | None = None
    contact_time: str | None = None
    route: str | None = None
    category: str | None = None
    people_count: int | None = Field(default=None)
    target_height: int | None = None
    required_uniform: str | None = None
    notes: str | None = None
    requires_cash: bool | None = None


class CompositionBody(BaseModel):
    brigadier_id: int
    member_ids: list[int]
    manual: bool = False


class EmployeePatch(BaseModel):
    telegram_id: int | None = None
    full_name: str | None = None
    metro: str | None = None
    height_cm: int | None = None
    phone: str | None = None
    telegram_username: str | None = None
    has_car: bool | None = None
    employee_group: str | None = None
    uniform: str | None = None
    experienced: bool | None = None
    remarks_count: int | None = None
    is_new: bool | None = None
    active: bool | None = None
    paused: bool | None = None
    can_cash: bool | None = None


class RatePatch(BaseModel):
    key: str
    amount: Decimal
    description: str = ""


def verify_init_data(init_data: str) -> int | None:
    if not init_data or not settings.bot_token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, their_hash):
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
        return int(user["id"])
    except Exception:
        return None


async def current_user(
    x_telegram_init_data: str | None = Header(default=None),
    x_telegram_user_id: int | None = Header(default=None),
) -> int:
    uid = verify_init_data(x_telegram_init_data or "")
    if uid is None and settings.dev_mode and x_telegram_user_id:
        uid = x_telegram_user_id
    if uid is None or uid not in settings.allowed_dispatcher_ids:
        raise HTTPException(401, "Нет доступа")
    return uid


def owner_only(uid: int) -> None:
    if uid not in settings.owner_ids:
        raise HTTPException(403, "Раздел доступен только владельцу")


async def tg_api(method: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"https://api.telegram.org/bot{settings.bot_token}/{method}", json=payload)
        r.raise_for_status()
        return r.json()


async def notify_dispatchers(text: str) -> None:
    if not settings.bot_token:
        return
    for uid in settings.allowed_dispatcher_ids:
        try:
            await tg_api("sendMessage", {"chat_id": uid, "text": text, "parse_mode": "HTML"})
        except Exception:
            pass


async def send_order_to_brigadier(order: dict) -> None:
    brig = next((x for x in order.get("assignments", []) if x["role"] == "brigadier"), None)
    if not brig or not settings.bot_token:
        return
    text = (
        f"<b>Новый заказ ЛЕГИОН</b>\n"
        f"{('ГБУ' if order['source']=='gbu' else 'Частный')} №{order['daily_number']}\n"
        f"📅 {order['work_date'].strftime('%d.%m.%Y')}\n"
        f"🕘 Выдача {order['issue_time'].strftime('%H:%M')}\n"
        f"📞 Связь {order['contact_time'].strftime('%H:%M') if order.get('contact_time') else 'не задана'}\n"
        f"📍 {order['route']}\n"
        f"👥 Состав: " + ", ".join(x["full_name"] for x in order["assignments"])
    )
    kb = {"inline_keyboard": [[{"text": "✅ Заказ принял", "callback_data": f"bacc:{order['id']}"}], [{"text": "📌 Мои заказы", "callback_data": "noop"}]]}
    await tg_api("sendMessage", {"chat_id": brig["telegram_id"], "text": text, "parse_mode": "HTML", "reply_markup": kb})
    await db.pool().execute("UPDATE orders SET status='sent',brigadier_sent_at=NOW() WHERE id=$1", order["id"])


class InternalQuery(BaseModel):
    kind: str
    sql: str
    args: list[Any] = []


class InternalCall(BaseModel):
    operation: str
    payload: dict[str, Any] = {}


def _wire_pack(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__legion_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__legion_type__": "date", "value": value.isoformat()}
    from datetime import time as dt_time
    if isinstance(value, dt_time):
        return {"__legion_type__": "time", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__legion_type__": "decimal", "value": str(value)}
    if hasattr(value, "items"):
        return {str(k): _wire_pack(v) for k, v in dict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_wire_pack(v) for v in value]
    return value


def _wire_unpack(value: Any) -> Any:
    if isinstance(value, dict):
        marker = value.get("__legion_type__")
        if marker == "datetime":
            return datetime.fromisoformat(value["value"])
        if marker == "date":
            return date.fromisoformat(value["value"])
        if marker == "time":
            from datetime import time as dt_time
            return dt_time.fromisoformat(value["value"])
        if marker == "decimal":
            return Decimal(value["value"])
        return {k: _wire_unpack(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_wire_unpack(v) for v in value]
    return value


def _internal_auth(x_bot_api_key: str | None) -> None:
    if not settings.bot_api_key or not x_bot_api_key or not hmac.compare_digest(x_bot_api_key, settings.bot_api_key):
        raise HTTPException(401, "Internal API access denied")


@app.post("/api/internal/db/query")
async def internal_db_query(body: InternalQuery, x_bot_api_key: str | None = Header(default=None)) -> dict:
    _internal_auth(x_bot_api_key)
    sql = body.sql.strip()
    if not sql or ";" in sql.rstrip(";"):
        raise HTTPException(400, "Invalid SQL")
    args = _wire_unpack(body.args)
    try:
        if body.kind == "fetchrow":
            result = await db.pool().fetchrow(sql, *args)
            result = dict(result) if result else None
        elif body.kind == "fetch":
            rows = await db.pool().fetch(sql, *args)
            result = [dict(r) for r in rows]
        elif body.kind == "fetchval":
            result = await db.pool().fetchval(sql, *args)
        elif body.kind == "execute":
            result = await db.pool().execute(sql, *args)
        else:
            raise HTTPException(400, "Unknown query kind")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"DB query failed: {type(exc).__name__}: {exc}")
    return {"result": _wire_pack(result)}


@app.post("/api/internal/call")
async def internal_call(body: InternalCall, x_bot_api_key: str | None = Header(default=None)) -> dict:
    _internal_auth(x_bot_api_key)
    p = _wire_unpack(body.payload)
    op = body.operation
    try:
        if op == "ping":
            result = {"ok": True, "version": "28.2"}
        elif op == "get_employee_by_tg":
            row = await db.get_employee_by_tg(int(p["telegram_id"]))
            result = dict(row) if row else None
        elif op == "set_readiness":
            result = await db.set_readiness(int(p["telegram_id"]), str(p["raw_text"]), p.get("work_date"))
        elif op == "create_order":
            result = await db.create_order(dict(p["data"]), p.get("actor"))
        elif op == "get_order":
            result = await db.get_order(int(p["order_id"]))
        elif op == "update_order":
            row, invalidated = await db.update_order(int(p["order_id"]), dict(p["patch"]), p.get("actor"))
            result = {"order": row, "assignments_invalidated": invalidated}
        elif op == "cancel_order":
            result = await db.cancel_order(int(p["order_id"]), p.get("actor"))
        else:
            raise HTTPException(400, "Unknown internal operation")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Internal call failed: {type(exc).__name__}: {exc}")
    return {"result": _wire_pack(result)}


@app.on_event("startup")
async def startup() -> None:
    await db.init_db()


@app.on_event("shutdown")
async def shutdown() -> None:
    await db.close_db()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX_FILE)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "version": "28.0"}


@app.get("/api/me")
async def me(uid: int = Depends(current_user)) -> dict:
    return {"telegram_id": uid, "role": "owner" if uid in settings.owner_ids else "helper", "cash_visible": uid in settings.owner_ids}


@app.get("/api/dashboard")
async def dashboard(day: date = Query(default_factory=lambda: settings.now().date()), uid: int = Depends(current_user)):
    readiness = await db.readiness_summary(day)
    private = await db.list_orders(day, "private")
    gbu = await db.list_orders(day, "gbu")
    control = await db.owner_control(day)
    return jsonable_encoder({"date": day, "readiness": readiness, "private": private, "gbu": gbu, "control": control})


@app.get("/api/readiness")
async def readiness(day: date, group: str, bucket: str, uid: int = Depends(current_user)):
    return jsonable_encoder(await db.readiness_people(day, group, bucket))


@app.get("/api/employees")
async def employees(group: str | None = None, uid: int = Depends(current_user)):
    if group:
        rows = await db.pool().fetch("SELECT * FROM employees WHERE active=TRUE AND employee_group=$1 ORDER BY full_name", group)
    else:
        rows = await db.pool().fetch("SELECT * FROM employees WHERE active=TRUE ORDER BY employee_group,full_name")
    return jsonable_encoder([dict(x) for x in rows])


@app.post("/api/employees")
async def employee_create(body: EmployeePatch, uid: int = Depends(current_user)):
    if not body.telegram_id or not body.full_name:
        raise HTTPException(422, "telegram_id и full_name обязательны")
    group = body.employee_group or "reserve"
    row = await db.pool().fetchrow(
        """INSERT INTO employees(telegram_id,full_name,metro,height_cm,phone,telegram_username,has_car,employee_group,uniform,experienced,remarks_count,is_new,active,paused,can_cash)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,COALESCE($13,TRUE),COALESCE($14,FALSE),COALESCE($15,FALSE)) RETURNING *""",
        body.telegram_id, body.full_name, body.metro or "", body.height_cm, body.phone or "", body.telegram_username or "",
        body.has_car or False, group, body.uniform or "", body.experienced or False, body.remarks_count or 0, body.is_new or False,
        body.active, body.paused, body.can_cash,
    )
    await db.audit(uid, "create", "employee", row["id"], {"telegram_id": row["telegram_id"], "name": row["full_name"]})
    return jsonable_encoder(dict(row))


@app.patch("/api/employees/{employee_id}")
async def employee_update(employee_id: int, body: EmployeePatch, uid: int = Depends(current_user)):
    patch = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not patch:
        raise HTTPException(422, "Нет изменений")
    allowed = set(EmployeePatch.model_fields)
    sets, vals = [], []
    for k, v in patch.items():
        if k not in allowed:
            continue
        vals.append(v)
        sets.append(f"{k}=${len(vals)}")
    vals.append(employee_id)
    row = await db.pool().fetchrow(f"UPDATE employees SET {', '.join(sets)},updated_at=NOW() WHERE id=${len(vals)} RETURNING *", *vals)
    if not row:
        raise HTTPException(404, "Сотрудник не найден")
    await db.audit(uid, "update", "employee", employee_id, patch)
    return jsonable_encoder(dict(row))


@app.get("/api/employees/{employee_id}/history")
async def employee_history(employee_id: int, uid: int = Depends(current_user)):
    return jsonable_encoder(await db.employee_history(employee_id, 30))


@app.get("/api/orders")
async def orders(day: date, source: str | None = None, uid: int = Depends(current_user)):
    return jsonable_encoder(await db.list_orders(day, source))


@app.get("/api/orders/{order_id}")
async def order_get(order_id: int, uid: int = Depends(current_user)):
    row = await db.get_order(order_id)
    if not row:
        raise HTTPException(404, "Заказ не найден")
    return jsonable_encoder(row)


@app.patch("/api/orders/{order_id}")
async def order_patch(order_id: int, body: OrderPatch, uid: int = Depends(current_user)):
    patch = body.model_dump(exclude_none=True)
    from datetime import datetime as dt
    if "issue_time" in patch:
        patch["issue_time"] = dt.strptime(patch["issue_time"], "%H:%M").time()
    if "contact_time" in patch:
        patch["contact_time"] = dt.strptime(patch["contact_time"], "%H:%M").time()
    row, invalidated = await db.update_order(order_id, patch, uid)
    if not row:
        raise HTTPException(404, "Заказ не найден")
    if invalidated:
        await notify_dispatchers(f"⚠️ Изменён заказ №{row['daily_number']}. Состав снят: нужна повторная расстановка.")
    return jsonable_encoder({"order": row, "assignments_invalidated": invalidated})


@app.delete("/api/orders/{order_id}")
async def order_delete(order_id: int, uid: int = Depends(current_user)):
    ok = await db.cancel_order(order_id, uid)
    if not ok:
        raise HTTPException(404, "Заказ не найден")
    return {"ok": True}


@app.post("/api/orders/{order_id}/copy")
async def order_copy(order_id: int, uid: int = Depends(current_user)):
    return jsonable_encoder(await db.copy_order_with_composition(order_id, uid))


@app.get("/api/orders/{order_id}/suggest")
async def order_suggest(order_id: int, manual: bool = False, uid: int = Depends(current_user)):
    return jsonable_encoder(await db.suggest_composition(order_id, manual=manual))


@app.post("/api/orders/{order_id}/autoassign")
async def order_autoassign(order_id: int, uid: int = Depends(current_user)):
    suggestion = await db.suggest_composition(order_id, manual=False)
    if not suggestion["ok"]:
        return jsonable_encoder(suggestion)
    saved = await db.save_composition(order_id, suggestion["brigadier"]["id"], [m["id"] for m in suggestion["members"]], uid, manual=False)
    await send_order_to_brigadier(saved)
    return jsonable_encoder({"ok": True, "order": saved})


@app.post("/api/orders/{order_id}/composition")
async def order_composition(order_id: int, body: CompositionBody, uid: int = Depends(current_user)):
    try:
        saved = await db.save_composition(order_id, body.brigadier_id, body.member_ids, uid, manual=body.manual)
    except ValueError as e:
        raise HTTPException(422, str(e))
    await send_order_to_brigadier(saved)
    return jsonable_encoder(saved)


@app.get("/api/control")
async def control(day: date = Query(default_factory=lambda: settings.now().date()), uid: int = Depends(current_user)):
    return jsonable_encoder(await db.owner_control(day))


@app.get("/api/cash")
async def cash(day: date, uid: int = Depends(current_user)):
    owner_only(uid)
    rows = await db.pool().fetch(
        """SELECT c.*,o.work_date,o.source,o.daily_number,o.category,e.full_name AS brigadier_name,e.metro
           FROM cash_entries c JOIN orders o ON o.id=c.order_id LEFT JOIN employees e ON e.id=c.brigadier_employee_id
           WHERE o.work_date=$1 ORDER BY o.issue_time""", day,
    )
    return jsonable_encoder([dict(x) for x in rows])


@app.get("/api/salary/rates")
async def salary_rates(uid: int = Depends(current_user)):
    owner_only(uid)
    rows = await db.pool().fetch("SELECT * FROM salary_rates WHERE active=TRUE ORDER BY key")
    return jsonable_encoder([dict(x) for x in rows])


@app.put("/api/salary/rates")
async def salary_rate_put(body: RatePatch, uid: int = Depends(current_user)):
    owner_only(uid)
    row = await db.pool().fetchrow(
        """INSERT INTO salary_rates(key,amount,description) VALUES($1,$2,$3)
           ON CONFLICT(key) DO UPDATE SET amount=EXCLUDED.amount,description=EXCLUDED.description,updated_at=NOW() RETURNING *""",
        body.key, body.amount, body.description,
    )
    return jsonable_encoder(dict(row))


@app.get("/api/salary/period")
async def salary_period(start: date, end: date, uid: int = Depends(current_user)):
    owner_only(uid)
    # Rates are configurable because exact employee rates were not fixed in the source requirements.
    rates_rows = await db.pool().fetch("SELECT key,amount FROM salary_rates WHERE active=TRUE")
    rates = {r["key"]: Decimal(r["amount"]) for r in rates_rows}
    ass = await db.pool().fetch(
        """SELECT a.employee_id,a.role,e.full_name,e.metro,e.has_car,o.work_date,o.category,o.id AS order_id,o.issue_time,
                  ROW_NUMBER() OVER(PARTITION BY a.employee_id,o.work_date ORDER BY o.issue_time,o.id) AS order_no
           FROM assignments a JOIN orders o ON o.id=a.order_id JOIN employees e ON e.id=a.employee_id
           WHERE o.work_date BETWEEN $1 AND $2 AND o.status<>'cancelled' ORDER BY e.full_name,o.work_date,o.issue_time""", start, end,
    )
    totals: dict[int, dict] = {}
    for r in ass:
        item = totals.setdefault(r["employee_id"], {"employee_id": r["employee_id"], "full_name": r["full_name"], "metro": r["metro"], "earned": Decimal("0"), "orders": []})
        ordinal = "first" if r["order_no"] == 1 else "second"
        key = f"{r['role']}:{r['category']}:{ordinal}"
        base = rates.get(key, Decimal("0"))
        car_bonus = rates.get(f"car:{ordinal}", Decimal("0")) if r["has_car"] else Decimal("0")
        amount = base + car_bonus
        item["earned"] += amount
        item["orders"].append({"order_id": r["order_id"], "date": r["work_date"], "category": r["category"], "ordinal": ordinal, "rate_key": key, "amount": amount})
    adjustments = await db.pool().fetch(
        "SELECT employee_id,SUM(amount) amount FROM salary_adjustments WHERE adjustment_date BETWEEN $1 AND $2 GROUP BY employee_id", start, end,
    )
    adj = {r["employee_id"]: Decimal(r["amount"] or 0) for r in adjustments}
    for eid, item in totals.items():
        item["adjustments"] = adj.get(eid, Decimal("0"))
        item["total"] = item["earned"] + item["adjustments"]
    return jsonable_encoder({"start": start, "end": end, "rates_configured": bool(rates), "employees": list(totals.values())})
