from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

import asyncpg

from common import settings

_pool: asyncpg.Pool | None = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database is not initialized")
    return _pool


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _first_value(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in lowered and lowered[name] not in (None, ""):
            return lowered[name]
    return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except Exception:
        try:
            return int(float(str(value).strip().replace(",", ".")))
        except Exception:
            return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value or "").strip().lower().replace("ё", "е")
    return s in {"1", "true", "yes", "y", "да", "есть", "авто", "машина"} or "авто" in s or "маш" in s


def _legacy_group(table_name: str, row: dict[str, Any]) -> str | None:
    t = table_name.lower()
    role = str(_first_value(row, ("employee_group", "group_name", "group", "role", "type", "category", "status")) or "").lower().replace("ё", "е")
    text = f"{t} {role}"
    if "brig" in text or "бриг" in text:
        return "brigadier"
    if "cashless" in text or "безнал" in text:
        return "cashless"
    if "main" in text or "основ" in text:
        return "main"
    if "reserve" in text or "резерв" in text:
        return "reserve"
    if any(x in t for x in ("employee", "staff", "worker", "personnel", "sotrud", "сотруд")):
        return "reserve"
    return None


async def recover_legacy_people(conn: asyncpg.Connection) -> int:
    """Non-destructive import from the legacy public schema into legion_v28.

    The old bot used different table names across versions. We inspect only user tables
    in public, detect common profile fields, and copy records with a Telegram id.
    Nothing is deleted or changed in the old tables.
    """
    imported = 0
    tables = await conn.fetch(
        """SELECT table_name FROM information_schema.tables
           WHERE table_schema='public' AND table_type='BASE TABLE'
           ORDER BY table_name"""
    )
    skip = {
        "schema_migrations", "alembic_version",
    }
    tg_names = ("telegram_id", "tg_id", "telegram_user_id", "user_id", "chat_id")
    name_names = ("full_name", "fio", "name", "brigadier_name", "employee_name", "username")
    for tr in tables:
        table = str(tr["table_name"])
        if table.lower() in skip:
            continue
        cols_rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=$1",
            table,
        )
        cols = {str(r["column_name"]).lower() for r in cols_rows}
        if not cols.intersection(tg_names):
            continue
        if not (
            any(x in table.lower() for x in ("brig", "employee", "staff", "worker", "personnel", "sotrud", "сотруд"))
            or cols.intersection({"employee_group", "group_name", "group", "role"})
        ):
            continue
        try:
            rows = await conn.fetch(f'SELECT * FROM public.{_qident(table)} LIMIT 5000')
        except Exception:
            continue
        for rr in rows:
            row = dict(rr)
            group = _legacy_group(table, row)
            if not group:
                continue
            tg = _to_int(_first_value(row, tg_names))
            if not tg or tg <= 0:
                continue
            full_name = str(_first_value(row, name_names) or f"Сотрудник {tg}").strip()
            metro = str(_first_value(row, ("metro", "subway", "station", "metro_station")) or "").strip()
            height = _to_int(_first_value(row, ("height_cm", "height", "rost", "рост")))
            phone = str(_first_value(row, ("phone", "phone_number", "telephone", "tel")) or "").strip()
            username = str(_first_value(row, ("telegram_username", "tg_username", "username")) or "").strip().lstrip("@")
            has_car = _to_bool(_first_value(row, ("has_car", "car", "auto", "with_car")))
            uniform = str(_first_value(row, ("uniform", "form", "uniform_size", "size")) or "").strip()
            experienced = _to_bool(_first_value(row, ("experienced", "is_experienced", "main_staff")))
            can_cash = _to_bool(_first_value(row, ("can_cash", "cash_allowed", "allow_cash")))
            result = await conn.execute(
                """INSERT INTO employees(telegram_id,full_name,metro,height_cm,phone,telegram_username,has_car,employee_group,uniform,experienced,can_cash,active,paused)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,TRUE,FALSE)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     full_name=CASE WHEN employees.full_name='' THEN EXCLUDED.full_name ELSE employees.full_name END,
                     metro=CASE WHEN employees.metro='' THEN EXCLUDED.metro ELSE employees.metro END,
                     height_cm=COALESCE(employees.height_cm,EXCLUDED.height_cm),
                     phone=CASE WHEN employees.phone='' THEN EXCLUDED.phone ELSE employees.phone END,
                     telegram_username=CASE WHEN employees.telegram_username='' THEN EXCLUDED.telegram_username ELSE employees.telegram_username END,
                     has_car=employees.has_car OR EXCLUDED.has_car,
                     employee_group=CASE WHEN EXCLUDED.employee_group='brigadier' THEN 'brigadier' ELSE employees.employee_group END,
                     uniform=CASE WHEN employees.uniform='' THEN EXCLUDED.uniform ELSE employees.uniform END,
                     experienced=employees.experienced OR EXCLUDED.experienced,
                     can_cash=employees.can_cash OR EXCLUDED.can_cash,
                     updated_at=NOW()""",
                tg, full_name, metro, height, phone, username, has_car, group, uniform, experienced, can_cash,
            )
            if result.startswith("INSERT"):
                imported += 1
    return imported


async def init_db() -> asyncpg.Pool:
    global _pool
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required")

    # V28 keeps its tables isolated from the currently running LEGION version.
    # This lets both versions use the same Railway Postgres without overwriting
    # or depending on the old public-schema table structure.
    db_schema = "legion_v28"
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=12,
        server_settings={"search_path": f"{db_schema},public"},
    )
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    async with _pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{db_schema}"')
        await conn.execute(f'SET search_path TO "{db_schema}", public')
        await conn.execute(schema)
        # Restore registered people from the previous LEGION tables when V28 is
        # deployed over the existing Railway Postgres. This is intentionally
        # non-destructive and may safely run on every startup.
        await recover_legacy_people(conn)
    return _pool


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def parse_readiness(text: str) -> tuple[str | None, int | None, bool | None]:
    s = " ".join(text.lower().replace("ё", "е").split())
    height = None
    m = re.search(r"(?<!\d)(1[6-9]\d|20\d)(?!\d)", s)
    if m:
        height = int(m.group(1))

    has_car: bool | None = None
    if any(x in s for x in ("на авто", "+ авто", "с авто", "машина", "авто есть")):
        has_car = True
    elif any(x in s for x in ("без авто", "нет авто", "без машины")):
        has_car = False

    if any(x in s for x in ("выходной", "не готов", "не выйду", "не могу")):
        return "off", height, has_car
    if any(x in s for x in ("на основной", "основной состав", "на основном", "основа")):
        return "main", height, has_car
    if "готов" in s:
        return ("ready_car" if has_car else "ready"), height, has_car
    return None, height, has_car


async def get_employee_by_tg(telegram_id: int) -> asyncpg.Record | None:
    return await pool().fetchrow("SELECT * FROM employees WHERE telegram_id=$1 AND active=TRUE", telegram_id)


async def set_readiness(telegram_id: int, raw_text: str, work_date: date | None = None) -> dict[str, Any]:
    work_date = work_date or settings.now().date()
    status, height, car = parse_readiness(raw_text)
    emp = await get_employee_by_tg(telegram_id)
    if not emp:
        return {"ok": False, "reason": "employee_not_found"}
    if not status:
        return {"ok": False, "reason": "status_not_recognized", "employee": dict(emp)}

    async with pool().acquire() as conn, conn.transaction():
        prev = await conn.fetchrow(
            "SELECT status FROM readiness_current WHERE employee_id=$1 AND work_date=$2",
            emp["id"], work_date,
        )
        await conn.execute(
            """
            INSERT INTO readiness_current(employee_id, work_date, status, raw_text, height_reported, has_car_reported, updated_at)
            VALUES($1,$2,$3,$4,$5,$6,NOW())
            ON CONFLICT(employee_id, work_date) DO UPDATE SET
              status=EXCLUDED.status, raw_text=EXCLUDED.raw_text,
              height_reported=EXCLUDED.height_reported,
              has_car_reported=EXCLUDED.has_car_reported, updated_at=NOW()
            """,
            emp["id"], work_date, status, raw_text, height, car,
        )
        await conn.execute(
            """INSERT INTO readiness_history(employee_id, work_date, previous_status, new_status, raw_text)
               VALUES($1,$2,$3,$4,$5)""",
            emp["id"], work_date, prev["status"] if prev else None, status, raw_text,
        )
        if height or car is not None:
            await conn.execute(
                """UPDATE employees SET
                     height_cm=COALESCE($2,height_cm),
                     has_car=COALESCE($3,has_car), updated_at=NOW()
                   WHERE id=$1""",
                emp["id"], height, car,
            )
    return {"ok": True, "employee": dict(emp), "status": status, "work_date": str(work_date)}


async def readiness_summary(work_date: date) -> list[dict[str, Any]]:
    rows = await pool().fetch(
        """
        SELECT e.employee_group,
               COUNT(*) FILTER (WHERE e.active AND NOT e.paused) AS total,
               COUNT(rc.id) AS responded,
               COUNT(*) FILTER (WHERE rc.status='ready') AS ready,
               COUNT(*) FILTER (WHERE rc.status='ready_car') AS ready_car,
               COUNT(*) FILTER (WHERE rc.status='main') AS main,
               COUNT(*) FILTER (WHERE rc.status='off') AS off
        FROM employees e
        LEFT JOIN readiness_current rc ON rc.employee_id=e.id AND rc.work_date=$1
        WHERE e.active=TRUE
        GROUP BY e.employee_group
        ORDER BY e.employee_group
        """,
        work_date,
    )
    return [dict(r) | {"not_responded": int(r["total"] or 0) - int(r["responded"] or 0)} for r in rows]


async def readiness_people(work_date: date, group_name: str, bucket: str) -> list[dict[str, Any]]:
    if bucket == "not_responded":
        rows = await pool().fetch(
            """
            SELECT e.* FROM employees e
            LEFT JOIN readiness_current rc ON rc.employee_id=e.id AND rc.work_date=$1
            WHERE e.active=TRUE AND e.paused=FALSE AND e.employee_group=$2 AND rc.id IS NULL
            ORDER BY e.full_name
            """, work_date, group_name,
        )
    elif bucket == "ready":
        rows = await pool().fetch(
            """
            SELECT e.*, rc.status, rc.updated_at AS readiness_updated_at FROM employees e
            JOIN readiness_current rc ON rc.employee_id=e.id AND rc.work_date=$1
            WHERE e.active=TRUE AND e.paused=FALSE AND e.employee_group=$2 AND rc.status IN ('ready','ready_car')
            ORDER BY e.height_cm NULLS LAST, e.full_name
            """, work_date, group_name,
        )
    else:
        rows = await pool().fetch(
            """
            SELECT e.*, rc.status, rc.updated_at AS readiness_updated_at FROM employees e
            JOIN readiness_current rc ON rc.employee_id=e.id AND rc.work_date=$1
            WHERE e.active=TRUE AND e.paused=FALSE AND e.employee_group=$2 AND rc.status=$3
            ORDER BY e.full_name
            """, work_date, group_name, bucket,
        )
    return [dict(r) for r in rows]


async def audit(actor: int | None, action: str, entity_type: str, entity_id: Any, payload: dict[str, Any] | None = None) -> None:
    await pool().execute(
        "INSERT INTO audit_log(actor_telegram_id,action,entity_type,entity_id,payload) VALUES($1,$2,$3,$4,$5::jsonb)",
        actor, action, entity_type, str(entity_id), json.dumps(payload or {}, ensure_ascii=False),
    )


async def next_daily_number(conn: asyncpg.Connection, work_date: date, source: str) -> int:
    key = f"{work_date}:{source}"
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
    n = await conn.fetchval("SELECT COALESCE(MAX(daily_number),0)+1 FROM orders WHERE work_date=$1 AND source=$2", work_date, source)
    return int(n)


async def create_order(data: dict[str, Any], actor: int | None = None) -> dict[str, Any]:
    async with pool().acquire() as conn, conn.transaction():
        n = await next_daily_number(conn, data["work_date"], data["source"])
        row = await conn.fetchrow(
            """
            INSERT INTO orders(work_date,source,daily_number,agent_telegram_id,agent_name,agent_phone,
              deceased_name,issue_time,contact_time,route,category,people_count,target_height,required_uniform,
              other_agent_name,other_agent_phone,notes,requires_cash,status,created_by,updated_by)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,'new',$19,$19)
            RETURNING *
            """,
            data["work_date"], data["source"], n, data.get("agent_telegram_id"), data.get("agent_name", ""),
            data.get("agent_phone", ""), data.get("deceased_name", ""), data["issue_time"], data.get("contact_time"),
            data.get("route", ""), data.get("category", "standard"), int(data.get("people_count", 4)),
            data.get("target_height"), data.get("required_uniform", ""), data.get("other_agent_name", ""),
            data.get("other_agent_phone", ""), data.get("notes", ""), bool(data.get("requires_cash", False)), actor,
        )
    await audit(actor, "create", "order", row["id"], dict(row))
    return dict(row)


async def get_order(order_id: int) -> dict[str, Any] | None:
    row = await pool().fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    if not row:
        return None
    d = dict(row)
    ass = await pool().fetch(
        """SELECT a.*, e.full_name,e.metro,e.height_cm,e.phone,e.telegram_id,e.telegram_username,e.has_car,e.employee_group
           FROM assignments a JOIN employees e ON e.id=a.employee_id
           WHERE a.order_id=$1 ORDER BY CASE WHEN a.role='brigadier' THEN 0 ELSE 1 END, e.height_cm, e.full_name""", order_id
    )
    d["assignments"] = [dict(x) for x in ass]
    return d


async def list_orders(work_date: date, source: str | None = None) -> list[dict[str, Any]]:
    if source:
        rows = await pool().fetch("SELECT * FROM orders WHERE work_date=$1 AND source=$2 ORDER BY issue_time,daily_number", work_date, source)
    else:
        rows = await pool().fetch("SELECT * FROM orders WHERE work_date=$1 ORDER BY issue_time,daily_number", work_date)
    result = []
    for r in rows:
        d = dict(r)
        d["assigned_count"] = await pool().fetchval("SELECT COUNT(*) FROM assignments WHERE order_id=$1", r["id"])
        result.append(d)
    return result


INVALIDATING_FIELDS = {"work_date", "issue_time", "people_count", "route", "target_height"}


async def update_order(order_id: int, patch: dict[str, Any], actor: int | None = None) -> tuple[dict[str, Any] | None, bool]:
    current = await get_order(order_id)
    if not current:
        return None, False
    allowed = {
        "work_date", "source", "agent_name", "agent_phone", "deceased_name", "issue_time", "contact_time", "route",
        "category", "people_count", "target_height", "required_uniform", "other_agent_name", "other_agent_phone",
        "notes", "requires_cash", "status",
    }
    patch = {k: v for k, v in patch.items() if k in allowed}
    invalidate = any(k in INVALIDATING_FIELDS and str(current.get(k)) != str(v) for k, v in patch.items())
    if not patch:
        return current, False

    async with pool().acquire() as conn, conn.transaction():
        if "work_date" in patch or "source" in patch:
            new_date = patch.get("work_date", current["work_date"])
            new_source = patch.get("source", current["source"])
            if new_date != current["work_date"] or new_source != current["source"]:
                patch["daily_number"] = await next_daily_number(conn, new_date, new_source)
                allowed.add("daily_number")
        sets = []
        values: list[Any] = []
        for k, v in patch.items():
            if k not in allowed:
                continue
            values.append(v)
            sets.append(f"{k}=${len(values)}")
        values.extend([actor, order_id])
        sql = f"UPDATE orders SET {', '.join(sets)}, updated_by=${len(values)-1}, updated_at=NOW() WHERE id=${len(values)} RETURNING *"
        row = await conn.fetchrow(sql, *values)
        if invalidate:
            await conn.execute("DELETE FROM assignments WHERE order_id=$1", order_id)
            await conn.execute("UPDATE orders SET status='needs_review', brigadier_sent_at=NULL WHERE id=$1 AND status<>'cancelled'", order_id)
    await audit(actor, "update", "order", order_id, {"patch": patch, "assignments_invalidated": invalidate})
    return dict(row), invalidate


async def cancel_order(order_id: int, actor: int | None = None) -> bool:
    row = await pool().fetchrow("UPDATE orders SET status='cancelled',updated_by=$2,updated_at=NOW() WHERE id=$1 RETURNING id", order_id, actor)
    if row:
        await audit(actor, "cancel", "order", order_id)
    return bool(row)


async def copy_order_with_composition(order_id: int, actor: int | None = None) -> dict[str, Any]:
    src = await get_order(order_id)
    if not src:
        raise ValueError("Order not found")
    data = {k: src[k] for k in (
        "work_date", "source", "agent_telegram_id", "agent_name", "agent_phone", "deceased_name", "issue_time", "contact_time",
        "route", "category", "people_count", "target_height", "required_uniform", "other_agent_name", "other_agent_phone", "notes", "requires_cash"
    )}
    new = await create_order(data, actor)
    for a in src["assignments"]:
        count = await employee_orders_count(a["employee_id"], new["work_date"])
        if count >= settings.max_orders_per_day:
            continue
        await pool().execute(
            "INSERT INTO assignments(order_id,employee_id,role,assigned_by) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            new["id"], a["employee_id"], a["role"], actor,
        )
    count = await pool().fetchval("SELECT COUNT(*) FROM assignments WHERE order_id=$1", new["id"])
    await pool().execute("UPDATE orders SET status=$2 WHERE id=$1", new["id"], "ready" if count == new["people_count"] else "staffing")
    return await get_order(new["id"])


async def employee_orders_count(employee_id: int, work_date: date, exclude_order: int | None = None) -> int:
    return int(await pool().fetchval(
        """SELECT COUNT(*) FROM assignments a JOIN orders o ON o.id=a.order_id
           WHERE a.employee_id=$1 AND o.work_date=$2 AND o.status<>'cancelled' AND ($3::bigint IS NULL OR o.id<>$3)""",
        employee_id, work_date, exclude_order,
    ) or 0)


async def has_time_conflict(employee_id: int, order: dict[str, Any], exclude_order: int | None = None) -> bool:
    rows = await pool().fetch(
        """SELECT o.* FROM assignments a JOIN orders o ON o.id=a.order_id
           WHERE a.employee_id=$1 AND o.work_date=$2 AND o.status NOT IN ('cancelled') AND ($3::bigint IS NULL OR o.id<>$3)""",
        employee_id, order["work_date"], exclude_order,
    )
    new_dt = datetime.combine(order["work_date"], order["issue_time"])
    reserve = timedelta(minutes=settings.order_estimate_minutes + settings.travel_buffer_minutes)
    for r in rows:
        old_dt = datetime.combine(r["work_date"], r["issue_time"])
        if abs(new_dt - old_dt) < reserve:
            return True
    return False


async def candidate_rows(order: dict[str, Any], role: str, brigadier_id: int | None = None, manual: bool = False) -> list[dict[str, Any]]:
    group_filter = "AND e.employee_group='brigadier'" if role == "brigadier" else "AND e.employee_group IN ('main','cashless','reserve')"
    ready_filter = "" if manual else "AND rc.status IN ('ready','ready_car','main')"
    rows = await pool().fetch(
        f"""
        SELECT e.*, rc.status AS readiness_status,
               COALESCE((SELECT bp.priority FROM brigadier_preferences bp WHERE bp.brigadier_employee_id=$2 AND bp.employee_id=e.id),9999) AS pref_priority,
               (SELECT COUNT(*) FROM assignments a JOIN orders o2 ON o2.id=a.order_id WHERE a.employee_id=e.id AND o2.work_date=$1 AND o2.status<>'cancelled') AS orders_today
        FROM employees e
        LEFT JOIN readiness_current rc ON rc.employee_id=e.id AND rc.work_date=$1
        WHERE e.active=TRUE AND e.paused=FALSE {group_filter} {ready_filter}
        """, order["work_date"], brigadier_id,
    )
    out = []
    for r in rows:
        d = dict(r)
        if int(d["orders_today"] or 0) >= settings.max_orders_per_day:
            continue
        if await has_time_conflict(d["id"], order, exclude_order=order["id"]):
            continue
        out.append(d)
    return out


def _group_priority(group: str) -> int:
    return {"main": 0, "cashless": 1, "reserve": 2, "brigadier": 0}.get(group, 9)


def _candidate_key(c: dict[str, Any], target_height: int | None) -> tuple:
    height_delta = abs((c.get("height_cm") or 999) - target_height) if target_height else 0
    return (
        int(c.get("pref_priority") or 9999),
        _group_priority(c.get("employee_group", "reserve")),
        0 if c.get("experienced") else 1,
        int(c.get("remarks_count") or 0),
        int(c.get("orders_today") or 0),
        height_delta,
        0 if c.get("has_car") else 1,
        c.get("full_name", ""),
    )


async def suggest_composition(order_id: int, manual: bool = False) -> dict[str, Any]:
    order = await get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    brigadiers = await candidate_rows(order, "brigadier", manual=manual)
    brigadiers.sort(key=lambda c: _candidate_key(c, order.get("target_height")))
    if not brigadiers:
        return {"ok": False, "reason": "no_brigadier", "order": order, "brigadiers": [], "members": []}
    brig = brigadiers[0]
    target = order.get("target_height") or brig.get("height_cm")
    members = await candidate_rows(order, "member", brigadier_id=brig["id"], manual=manual)
    if target:
        preferred = [m for m in members if m.get("height_cm") and abs(m["height_cm"] - target) <= 2]
        fallback = [m for m in members if m not in preferred]
        preferred.sort(key=lambda c: _candidate_key(c, target))
        fallback.sort(key=lambda c: _candidate_key(c, target))
        members = preferred + fallback
    else:
        members.sort(key=lambda c: _candidate_key(c, None))
    need = int(order["people_count"]) - 1
    selected = members[:need]
    return {
        "ok": len(selected) == need,
        "reason": None if len(selected) == need else "not_enough_members",
        "order": order,
        "brigadier": brig,
        "brigadiers": brigadiers[:20],
        "members": selected,
        "alternatives": members[:50],
    }


async def save_composition(order_id: int, brigadier_id: int, member_ids: Iterable[int], actor: int | None, manual: bool = False) -> dict[str, Any]:
    order = await get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    ids = [brigadier_id] + [int(x) for x in member_ids]
    if len(ids) != int(order["people_count"]):
        raise ValueError(f"Нужно {order['people_count']} человек вместе с бригадиром")
    if len(set(ids)) != len(ids):
        raise ValueError("Один сотрудник выбран дважды")
    async with pool().acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM assignments WHERE order_id=$1", order_id)
        for i, employee_id in enumerate(ids):
            emp = await conn.fetchrow("SELECT * FROM employees WHERE id=$1 AND active=TRUE AND paused=FALSE", employee_id)
            if not emp:
                raise ValueError(f"Сотрудник {employee_id} недоступен")
            if await employee_orders_count(employee_id, order["work_date"], exclude_order=order_id) >= settings.max_orders_per_day:
                raise ValueError(f"{emp['full_name']}: уже {settings.max_orders_per_day} заказа за день")
            if await has_time_conflict(employee_id, order, exclude_order=order_id):
                raise ValueError(f"{emp['full_name']}: пересечение по времени")
            if not manual:
                rc = await conn.fetchrow("SELECT status FROM readiness_current WHERE employee_id=$1 AND work_date=$2", employee_id, order["work_date"])
                if not rc or rc["status"] not in ("ready", "ready_car", "main"):
                    raise ValueError(f"{emp['full_name']}: нет подходящей готовности")
            await conn.execute(
                "INSERT INTO assignments(order_id,employee_id,role,assigned_by) VALUES($1,$2,$3,$4)",
                order_id, employee_id, "brigadier" if i == 0 else "member", actor,
            )
        await conn.execute("UPDATE orders SET status='ready', updated_by=$2, updated_at=NOW() WHERE id=$1", order_id, actor)
    await audit(actor, "save_composition", "order", order_id, {"brigadier_id": brigadier_id, "member_ids": list(member_ids), "manual": manual})
    return await get_order(order_id)


async def employee_history(employee_id: int, days: int = 30) -> dict[str, Any]:
    emp = await pool().fetchrow("SELECT * FROM employees WHERE id=$1", employee_id)
    if not emp:
        raise ValueError("Employee not found")
    readiness = await pool().fetch(
        "SELECT * FROM readiness_current WHERE employee_id=$1 AND work_date >= CURRENT_DATE-$2::int ORDER BY work_date DESC",
        employee_id, days,
    )
    orders = await pool().fetch(
        """SELECT o.*, a.role, a.accepted_at, a.contact_confirmed_at
           FROM assignments a JOIN orders o ON o.id=a.order_id
           WHERE a.employee_id=$1 AND o.work_date >= CURRENT_DATE-$2::int ORDER BY o.work_date DESC,o.issue_time DESC""",
        employee_id, days,
    )
    photos_count = await pool().fetchval("SELECT COUNT(*) FROM photos WHERE employee_id=$1 AND created_at >= NOW()-($2::text||' days')::interval", employee_id, days)
    return {"employee": dict(emp), "readiness": [dict(x) for x in readiness], "orders": [dict(x) for x in orders], "photos_count": int(photos_count or 0)}


async def owner_control(work_date: date) -> dict[str, Any]:
    today = await list_orders(work_date)
    tomorrow = await list_orders(work_date + timedelta(days=1))
    problems: list[dict[str, Any]] = []
    for o in today:
        if o["status"] not in ("completed", "cancelled") and datetime.combine(work_date, o["issue_time"]).time() < settings.now().time():
            problems.append({"kind": "not_completed", "order": o})
        required_photos = 2 if o["source"] == "gbu" else 1
        photo_count = await pool().fetchval("SELECT COUNT(*) FROM photos WHERE order_id=$1", o["id"])
        if o["status"] == "completed" and int(photo_count or 0) < required_photos:
            problems.append({"kind": "missing_photo", "order": o, "have": int(photo_count or 0), "need": required_photos})
        cash = await pool().fetchrow("SELECT id FROM cash_entries WHERE order_id=$1", o["id"])
        if o["status"] == "completed" and o["requires_cash"] and not cash:
            problems.append({"kind": "missing_cash", "order": o})
    for o in tomorrow:
        if int(o["assigned_count"] or 0) != int(o["people_count"]):
            problems.append({"kind": "no_composition", "order": o})
        if o["contact_time"] is None:
            problems.append({"kind": "no_contact_time", "order": o})
    return {"ok": not problems, "problems": problems, "today": today, "tomorrow": tomorrow}
