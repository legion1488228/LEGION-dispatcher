from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from database import Base, engine, get_db
from models import AccessUser, Agent, Assignment, Employee, ImportLog, Order, Readiness
from parsers import parse_agent_ready_form, parse_gbu_ocr_text
from telegram_auth import TelegramUser, require_admin, require_bot_key
from telegram_notify import agent_brigadier_text, brigadier_order_text, send_telegram_message

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
MOSCOW = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Moscow"))
GROUP_LABELS = {
    "brigadier": "Бригадиры",
    "main": "Основной состав",
    "cashless": "Безнал состав",
    "reserve": "Резерв",
}
STATUS_LABELS = {
    "new": "Новый",
    "assigning": "В расстановке",
    "ready": "Состав готов",
    "sent": "Отправлен бригадиру",
    "brigadier_confirmed": "Бригадир подтвердил",
    "closed": "Закрыт",
    "cancelled": "Отменён",
}
CATEGORY_LABELS = {"standard": "Стандарт", "vip": "Вип", "elite": "Элит"}
SOURCE_LABELS = {"private": "Частный", "gbu": "ГБУ"}

app = FastAPI(title="ЛЕГИОН — Диспетчерская", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- Schemas ----------

class EmployeeSync(BaseModel):
    tg_id: int | None = None
    full_name: str
    phone: str = ""
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    height_cm: int | None = Field(default=None, ge=150, le=220)
    has_car: bool = False
    active: bool = True


class AgentSync(BaseModel):
    tg_id: int
    full_name: str
    phone: str
    organization: str = ""
    active: bool = True


class ReadinessReport(BaseModel):
    tg_id: int
    full_name: str
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    work_date: date
    status: Literal["ready", "not_ready", "day_off"]
    height_cm: int | None = Field(default=None, ge=150, le=220)
    has_car: bool = False
    phone: str = ""
    raw_text: str = ""


class OrderCreate(BaseModel):
    source: Literal["private", "gbu"] = "private"
    work_date: date
    issue_time: time
    arrival_time: time | None = None
    deceased_name: str
    route: str = ""
    category: Literal["standard", "vip", "elite"] = "standard"
    team_size: Literal[4, 6] = 4
    organization: str = ""
    notes: str = ""
    kickback_rub: int = 0
    agent_tg_id: int | None = None
    agent_name: str = ""
    agent_phone: str = ""
    other_agent_phone: str = ""


class OrderPatch(BaseModel):
    work_date: date | None = None
    issue_time: time | None = None
    arrival_time: time | None = None
    deceased_name: str | None = None
    route: str | None = None
    category: Literal["standard", "vip", "elite"] | None = None
    team_size: Literal[4, 6] | None = None
    organization: str | None = None
    notes: str | None = None
    source: Literal["private", "gbu"] | None = None


class AssignPerson(BaseModel):
    employee_id: int


class BrigadierClose(BaseModel):
    contact_time: str = "08:00"


class ParseAgentText(BaseModel):
    text: str
    organization_profile: str = ""


class GbuDraft(BaseModel):
    work_date: date
    issue_time: time
    arrival_time: time | None = None
    deceased_name: str
    route: str = ""
    category: Literal["standard", "vip", "elite"] = "standard"
    team_size: Literal[4, 6] = 4
    organization: str = "ГБУ"
    notes: str = ""


# ---------- Helpers ----------

def _bot_key(x_bot_key: str | None = Header(default=None, alias="X-Bot-Key")):
    require_bot_key(x_bot_key)


def _public_id() -> str:
    return secrets.token_hex(5).upper()


def _label_category(value: str) -> str:
    return CATEGORY_LABELS.get(value, value)


def _label_source(value: str) -> str:
    return SOURCE_LABELS.get(value, value)


def _label_status(value: str) -> str:
    return STATUS_LABELS.get(value, value)


def _employee_dict(emp: Employee, workload: int = 0, readiness_status: str | None = None, brig_height: int | None = None):
    diff = abs((emp.height_cm or 0) - brig_height) if brig_height and emp.height_cm else None
    return {
        "id": emp.id,
        "tg_id": emp.tg_id,
        "full_name": emp.full_name,
        "phone": emp.phone,
        "group_code": emp.group_code,
        "group_label": GROUP_LABELS.get(emp.group_code, emp.group_code),
        "height_cm": emp.height_cm,
        "has_car": emp.has_car,
        "active": emp.active,
        "workload": workload,
        "capacity": 2,
        "blocked": workload >= 2,
        "readiness_status": readiness_status,
        "height_diff": diff,
        "height_match": diff is not None and diff <= 2,
    }


def _order_dict(order: Order, db: Session, include_candidates: bool = False):
    assignments = db.execute(
        select(Assignment)
        .where(Assignment.order_id == order.id)
        .options(selectinload(Assignment.employee))
        .order_by(Assignment.role.desc(), Assignment.id)
    ).scalars().all()
    brig = next((a.employee for a in assignments if a.role == "brigadier"), None)
    members = [a.employee for a in assignments if a.role == "member"]
    return {
        "id": order.id,
        "public_id": order.public_id,
        "source": order.source,
        "source_label": _label_source(order.source),
        "work_date": order.work_date.isoformat(),
        "issue_time": order.issue_time.strftime("%H:%M"),
        "arrival_time": order.arrival_time.strftime("%H:%M") if order.arrival_time else None,
        "deceased_name": order.deceased_name,
        "route": order.route,
        "category": order.category,
        "category_label": _label_category(order.category),
        "team_size": order.team_size,
        "organization": order.organization,
        "notes": order.notes,
        "kickback_rub": order.kickback_rub,
        "agent_tg_id": order.agent_tg_id,
        "agent_name": order.agent_name,
        "agent_phone": order.agent_phone,
        "other_agent_phone": order.other_agent_phone,
        "status": order.status,
        "status_label": _label_status(order.status),
        "brigadier": _employee_dict(brig) if brig else None,
        "members": [_employee_dict(x) for x in members],
        "member_target": max(0, order.team_size - 1),
        "member_count": len(members),
        "composition_complete": brig is not None and len(members) == order.team_size - 1,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        "contact_time": order.contact_time,
    }


def _workloads(db: Session, work_date: date) -> dict[int, int]:
    rows = db.execute(
        select(Assignment.employee_id, func.count(Assignment.id))
        .join(Order, Order.id == Assignment.order_id)
        .where(Order.work_date == work_date, Order.status != "cancelled")
        .group_by(Assignment.employee_id)
    ).all()
    return {int(emp_id): int(cnt) for emp_id, cnt in rows}


def _ready_map(db: Session, work_date: date) -> dict[int, Readiness]:
    rows = db.execute(select(Readiness).where(Readiness.work_date == work_date)).scalars().all()
    return {r.employee_id: r for r in rows}


def _cutoff_utc_naive(work_date: date) -> datetime:
    local_dt = datetime.combine(work_date - timedelta(days=1), time(15, 30), tzinfo=MOSCOW)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _order_by_id_for_update(db: Session, order_id: int) -> Order:
    order = db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return order


def _employee_for_update(db: Session, employee_id: int) -> Employee:
    emp = db.execute(select(Employee).where(Employee.id == employee_id).with_for_update()).scalar_one_or_none()
    if not emp or not emp.active:
        raise HTTPException(404, "Сотрудник не найден")
    return emp


def _check_capacity(db: Session, employee: Employee, work_date: date, exclude_order_id: int | None = None):
    q = (
        select(func.count(Assignment.id))
        .join(Order, Order.id == Assignment.order_id)
        .where(
            Assignment.employee_id == employee.id,
            Order.work_date == work_date,
            Order.status != "cancelled",
        )
    )
    if exclude_order_id:
        q = q.where(Order.id != exclude_order_id)
    count = int(db.execute(q).scalar_one())
    if count >= 2:
        raise HTTPException(409, f"{employee.full_name} уже назначен на два заказа")
    return count


def _require_ready(db: Session, employee: Employee, work_date: date):
    row = db.execute(
        select(Readiness).where(Readiness.employee_id == employee.id, Readiness.work_date == work_date)
    ).scalar_one_or_none()
    if not row or row.status != "ready":
        raise HTTPException(409, f"{employee.full_name} не отмечен как готовый на эту дату")


def _upsert_employee(db: Session, data: EmployeeSync) -> Employee:
    emp = None
    if data.tg_id:
        emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id)).scalar_one_or_none()
    if not emp:
        emp = Employee(tg_id=data.tg_id, full_name=data.full_name, group_code=data.group_code)
        db.add(emp)
    emp.full_name = data.full_name.strip()
    emp.phone = data.phone.strip()
    emp.group_code = data.group_code
    emp.height_cm = data.height_cm
    emp.has_car = data.has_car
    emp.active = data.active
    db.flush()
    return emp


# ---------- Auth / bootstrap ----------

@app.get("/api/me")
def api_me(user: TelegramUser = Depends(require_admin)):
    return {"id": user.id, "name": user.full_name, "username": user.username}


@app.get("/api/bootstrap")
def bootstrap(work_date: date | None = None, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    selected = work_date or (datetime.now(MOSCOW).date() + timedelta(days=1))
    dates = _date_folders(db, selected)
    summary = _day_summary(db, selected)
    return {
        "user": {"id": user.id, "name": user.full_name},
        "selected_date": selected.isoformat(),
        "dates": dates,
        "summary": summary,
        "groups": GROUP_LABELS,
    }


def _date_folders(db: Session, selected: date):
    today = datetime.now(MOSCOW).date()
    base = {today + timedelta(days=i) for i in range(0, 8)}
    order_dates = db.execute(select(Order.work_date).where(Order.work_date >= today - timedelta(days=2)).distinct()).scalars().all()
    base.update(order_dates)
    base.add(selected)

    counts = dict(db.execute(
        select(Order.work_date, func.count(Order.id))
        .where(Order.work_date.in_(list(base)), Order.status != "cancelled")
        .group_by(Order.work_date)
    ).all()) if base else {}

    items = []
    for d in sorted(base):
        label = d.strftime("%d.%m")
        if d == today:
            label = "Сегодня"
        elif d == today + timedelta(days=1):
            label = "Завтра"
        items.append({"date": d.isoformat(), "label": label, "count": int(counts.get(d, 0))})
    return items


def _day_summary(db: Session, work_date: date):
    orders = db.execute(select(Order).where(Order.work_date == work_date)).scalars().all()
    active = [o for o in orders if o.status != "cancelled"]
    readiness = readiness_summary_data(db, work_date)
    return {
        "orders_total": len(active),
        "private_count": sum(1 for o in active if o.source == "private"),
        "gbu_count": sum(1 for o in active if o.source == "gbu"),
        "complete_count": sum(1 for o in active if o.status in {"ready", "sent", "brigadier_confirmed", "closed"}),
        "unassigned_count": sum(1 for o in active if not any(a.role == "brigadier" for a in o.assignments)),
        "ready_now": readiness["totals"]["ready_now"],
        "ready_cutoff": readiness["totals"]["ready_cutoff"],
        "no_response_cutoff": readiness["totals"]["no_response_cutoff"],
    }


# ---------- Orders ----------

@app.get("/api/orders")
def list_orders(
    work_date: date,
    source: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_admin),
):
    q = select(Order).where(Order.work_date == work_date)
    if source and source != "all":
        q = q.where(Order.source == source)
    if status and status != "all":
        q = q.where(Order.status == status)
    q = q.order_by(Order.issue_time, Order.id)
    rows = db.execute(q).scalars().all()
    return [_order_dict(o, db) for o in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return _order_dict(order, db)


@app.post("/api/orders")
def create_order(data: OrderCreate, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _create_order(db, data)
    db.commit()
    db.refresh(order)
    return _order_dict(order, db)


def _create_order(db: Session, data: OrderCreate) -> Order:
    kickback = data.kickback_rub
    if not kickback and "марин" in data.organization.lower():
        kickback = 1000 if data.team_size == 4 else 1500 if data.team_size == 6 else 0
    order = Order(
        public_id=_public_id(),
        source=data.source,
        work_date=data.work_date,
        issue_time=data.issue_time,
        arrival_time=data.arrival_time,
        deceased_name=data.deceased_name.strip(),
        route=data.route.strip(),
        category=data.category,
        team_size=data.team_size,
        organization=data.organization.strip(),
        notes=data.notes.strip(),
        kickback_rub=kickback,
        agent_tg_id=data.agent_tg_id,
        agent_name=data.agent_name.strip(),
        agent_phone=data.agent_phone.strip(),
        other_agent_phone=data.other_agent_phone.strip(),
        status="new",
    )
    db.add(order)
    db.flush()
    return order


@app.patch("/api/orders/{order_id}")
def patch_order(order_id: int, data: OrderPatch, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    payload = data.model_dump(exclude_unset=True)
    if "team_size" in payload:
        current_members = db.execute(select(func.count(Assignment.id)).where(Assignment.order_id == order_id, Assignment.role == "member")).scalar_one()
        if current_members > payload["team_size"] - 1:
            raise HTTPException(409, "Сначала снимите лишних сотрудников из состава")
    for key, value in payload.items():
        setattr(order, key, value)
    if order.status == "new" and db.execute(select(func.count(Assignment.id)).where(Assignment.order_id == order.id)).scalar_one():
        order.status = "assigning"
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.get("/api/orders/{order_id}/brigadiers")
def brigadier_candidates(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    ready = _ready_map(db, order.work_date)
    loads = _workloads(db, order.work_date)
    employees = db.execute(
        select(Employee).where(Employee.active.is_(True), Employee.group_code == "brigadier").order_by(Employee.height_cm, Employee.full_name)
    ).scalars().all()
    result = []
    for emp in employees:
        r = ready.get(emp.id)
        if not r or r.status != "ready":
            continue
        result.append(_employee_dict(emp, loads.get(emp.id, 0), r.status))
    result.sort(key=lambda x: (x["blocked"], x["height_cm"] or 999, x["full_name"]))
    return result


@app.post("/api/orders/{order_id}/brigadier")
def assign_brigadier(order_id: int, data: AssignPerson, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    emp = _employee_for_update(db, data.employee_id)
    if emp.group_code != "brigadier":
        raise HTTPException(409, "Назначить бригадиром можно только сотрудника из группы Бригадиры")
    _require_ready(db, emp, order.work_date)
    _check_capacity(db, emp, order.work_date, exclude_order_id=order.id)

    old = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier")).scalar_one_or_none()
    if old and old.employee_id == emp.id:
        return _order_dict(order, db)
    if old:
        db.delete(old)
        db.flush()

    # If employee was already selected as member of the same order, promote instead of duplicate.
    member = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.employee_id == emp.id)).scalar_one_or_none()
    if member:
        member.role = "brigadier"
        member.created_by_tg_id = user.id
    else:
        db.add(Assignment(order_id=order.id, employee_id=emp.id, role="brigadier", created_by_tg_id=user.id))
    order.status = "assigning"
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.get("/api/orders/{order_id}/members/candidates")
def member_candidates(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    current = db.execute(
        select(Assignment).where(Assignment.order_id == order.id).options(selectinload(Assignment.employee))
    ).scalars().all()
    brig = next((a.employee for a in current if a.role == "brigadier"), None)
    current_ids = {a.employee_id for a in current}
    brig_height = brig.height_cm if brig else None
    ready = _ready_map(db, order.work_date)
    loads = _workloads(db, order.work_date)
    employees = db.execute(select(Employee).where(Employee.active.is_(True))).scalars().all()

    group_order = {"main": 0, "cashless": 1, "reserve": 2, "brigadier": 3}
    result = []
    for emp in employees:
        r = ready.get(emp.id)
        if not r or r.status != "ready":
            continue
        item = _employee_dict(emp, loads.get(emp.id, 0), r.status, brig_height)
        item["selected"] = emp.id in current_ids and (not brig or emp.id != brig.id)
        item["is_brigadier"] = bool(brig and emp.id == brig.id)
        result.append(item)

    def key(x):
        match_rank = 0 if x["height_match"] else 1
        diff = x["height_diff"] if x["height_diff"] is not None else 999
        return (x["blocked"] and not x["selected"], match_rank, diff, group_order.get(x["group_code"], 9), x["full_name"])

    result.sort(key=key)
    return {"brigadier": _employee_dict(brig) if brig else None, "target": order.team_size - 1, "candidates": result}


@app.post("/api/orders/{order_id}/members")
def add_member(order_id: int, data: AssignPerson, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    emp = _employee_for_update(db, data.employee_id)
    _require_ready(db, emp, order.work_date)
    existing = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.employee_id == emp.id)).scalar_one_or_none()
    if existing:
        if existing.role == "member":
            return _order_dict(order, db)
        raise HTTPException(409, "Этот сотрудник уже назначен бригадиром")
    member_count = int(db.execute(select(func.count(Assignment.id)).where(Assignment.order_id == order.id, Assignment.role == "member")).scalar_one())
    if member_count >= order.team_size - 1:
        raise HTTPException(409, "Состав уже полностью набран")
    _check_capacity(db, emp, order.work_date, exclude_order_id=order.id)
    db.add(Assignment(order_id=order.id, employee_id=emp.id, role="member", created_by_tg_id=user.id))
    order.status = "assigning"
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.delete("/api/orders/{order_id}/members/{employee_id}")
def remove_member(order_id: int, employee_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    row = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.employee_id == employee_id, Assignment.role == "member")).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Сотрудник не входит в состав")
    db.delete(row)
    order.status = "assigning"
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.post("/api/orders/{order_id}/reset-composition")
def reset_composition(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    if order.status in {"closed", "cancelled"}:
        raise HTTPException(409, "Закрытый или отменённый заказ нельзя сбросить")
    rows = db.execute(select(Assignment).where(Assignment.order_id == order.id)).scalars().all()
    for row in rows:
        db.delete(row)
    order.status = "new"
    order.finalized_at = None
    order.sent_to_brigadier_at = None
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.post("/api/orders/{order_id}/finalize")
async def finalize_order(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    assignments = db.execute(
        select(Assignment).where(Assignment.order_id == order.id).options(selectinload(Assignment.employee))
    ).scalars().all()
    brig_assignment = next((a for a in assignments if a.role == "brigadier"), None)
    members = [a.employee for a in assignments if a.role == "member"]
    if not brig_assignment:
        raise HTTPException(409, "Сначала назначьте бригадира")
    if len(members) != order.team_size - 1:
        raise HTTPException(409, f"Нужно выбрать {order.team_size - 1} сотрудников")
    brig = brig_assignment.employee
    order.status = "ready"
    order.finalized_at = datetime.utcnow()
    db.commit(); db.refresh(order)

    sent = False
    warning = None
    if brig.tg_id:
        try:
            sent = await send_telegram_message(
                brig.tg_id,
                brigadier_order_text(order, brig, members),
                reply_markup={
                    "inline_keyboard": [[{
                        "text": "✅ Закрыл заказ / указать время связи",
                        "callback_data": f"mini_brig_close:{order.public_id}",
                    }]]
                },
            )
        except Exception as exc:
            warning = f"Не удалось отправить бригадиру: {exc}"
    else:
        warning = "У бригадира не сохранён Telegram ID"

    if sent:
        order.status = "sent"
        order.sent_to_brigadier_at = datetime.utcnow()
        db.commit(); db.refresh(order)
    return {"order": _order_dict(order, db), "telegram_sent": sent, "warning": warning}


# ---------- Readiness ----------

@app.get("/api/readiness/summary")
def readiness_summary(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    return readiness_summary_data(db, work_date)


def readiness_summary_data(db: Session, work_date: date):
    employees = db.execute(select(Employee).where(Employee.active.is_(True)).order_by(Employee.group_code, Employee.full_name)).scalars().all()
    ready_rows = _ready_map(db, work_date)
    cutoff = _cutoff_utc_naive(work_date)

    groups = {code: {"label": label, "ready": [], "not_ready": [], "day_off": [], "no_response": []} for code, label in GROUP_LABELS.items()}
    total_ready_now = 0
    total_ready_cutoff = 0
    total_no_response = 0

    for emp in employees:
        row = ready_rows.get(emp.id)
        current = row.status if row else "no_response"
        cutoff_status = row.status if row and row.reported_at <= cutoff else "no_response"
        if current == "ready":
            total_ready_now += 1
        if cutoff_status == "ready":
            total_ready_cutoff += 1
        if cutoff_status == "no_response":
            total_no_response += 1
        item = _employee_dict(emp, readiness_status=current)
        item["cutoff_status"] = cutoff_status
        item["reported_at"] = row.reported_at.isoformat() if row else None
        groups.setdefault(emp.group_code, {"label": emp.group_code, "ready": [], "not_ready": [], "day_off": [], "no_response": []})
        groups[emp.group_code].setdefault(current, groups[emp.group_code]["no_response"] if current == "no_response" else [])
        if current in {"ready", "not_ready", "day_off"}:
            groups[emp.group_code][current].append(item)
        else:
            groups[emp.group_code]["no_response"].append(item)

    for g in groups.values():
        g["counts"] = {k: len(g[k]) for k in ["ready", "not_ready", "day_off", "no_response"]}
        # Official no-response at 15:30 can include a person who reported late.
        g["no_response_cutoff"] = [
            x for bucket in [g["ready"], g["not_ready"], g["day_off"], g["no_response"]]
            for x in bucket if x["cutoff_status"] == "no_response"
        ]

    return {
        "work_date": work_date.isoformat(),
        "cutoff_label": "15:30",
        "totals": {
            "employees": len(employees),
            "ready_now": total_ready_now,
            "ready_cutoff": total_ready_cutoff,
            "no_response_cutoff": total_no_response,
        },
        "groups": groups,
    }


@app.get("/api/employees")
def list_employees(group: str | None = None, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    q = select(Employee).where(Employee.active.is_(True))
    if group and group != "all":
        q = q.where(Employee.group_code == group)
    rows = db.execute(q.order_by(Employee.full_name)).scalars().all()
    return [_employee_dict(x) for x in rows]


@app.post("/api/employees/bulk")
def bulk_employees(items: list[EmployeeSync], db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    result = []
    for item in items:
        emp = _upsert_employee(db, item)
        result.append(emp)
    db.commit()
    return [_employee_dict(x) for x in result]


# ---------- BOT integration ----------

@app.post("/api/bot/employees/sync", dependencies=[Depends(_bot_key)])
def bot_employee_sync(data: EmployeeSync, db: Session = Depends(get_db)):
    emp = _upsert_employee(db, data)
    db.commit(); db.refresh(emp)
    return _employee_dict(emp)


@app.post("/api/bot/agents/sync", dependencies=[Depends(_bot_key)])
def bot_agent_sync(data: AgentSync, db: Session = Depends(get_db)):
    row = db.execute(select(Agent).where(Agent.tg_id == data.tg_id)).scalar_one_or_none()
    if not row:
        row = Agent(tg_id=data.tg_id, full_name=data.full_name, phone=data.phone)
        db.add(row)
    row.full_name = data.full_name
    row.phone = data.phone
    row.organization = data.organization
    row.active = data.active
    db.commit(); db.refresh(row)
    return {"ok": True, "id": row.id}


@app.post("/api/bot/readiness", dependencies=[Depends(_bot_key)])
def bot_readiness(data: ReadinessReport, db: Session = Depends(get_db)):
    sync = EmployeeSync(
        tg_id=data.tg_id,
        full_name=data.full_name,
        phone=data.phone,
        group_code=data.group_code,
        height_cm=data.height_cm,
        has_car=data.has_car,
        active=True,
    )
    emp = _upsert_employee(db, sync)
    row = db.execute(select(Readiness).where(Readiness.employee_id == emp.id, Readiness.work_date == data.work_date)).scalar_one_or_none()
    if not row:
        row = Readiness(employee_id=emp.id, work_date=data.work_date, status=data.status)
        db.add(row)
    row.status = data.status
    row.raw_text = data.raw_text
    row.reported_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "employee_id": emp.id, "status": row.status}


@app.post("/api/bot/orders", dependencies=[Depends(_bot_key)])
def bot_create_order(data: OrderCreate, db: Session = Depends(get_db)):
    order = _create_order(db, data)
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.post("/api/bot/parse-agent-order", dependencies=[Depends(_bot_key)])
def bot_parse_agent_order(data: ParseAgentText, db: Session = Depends(get_db)):
    result = parse_agent_ready_form(data.text, data.organization_profile)
    db.add(ImportLog(kind="agent_text", raw_text=data.text, result_json=json.dumps(result, ensure_ascii=False, default=str)))
    db.commit()
    return result


@app.post("/api/bot/orders/{public_id}/brigadier-close", dependencies=[Depends(_bot_key)])
async def bot_brigadier_close(public_id: str, data: BrigadierClose, db: Session = Depends(get_db)):
    order = db.execute(select(Order).where(Order.public_id == public_id)).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден")
    brig_assignment = db.execute(
        select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier").options(selectinload(Assignment.employee))
    ).scalar_one_or_none()
    if not brig_assignment:
        raise HTTPException(409, "Бригадир не назначен")
    order.status = "brigadier_confirmed"
    order.brigadier_confirmed_at = datetime.utcnow()
    order.contact_time = data.contact_time
    db.commit(); db.refresh(order)

    sent = False
    if order.agent_tg_id:
        sent = await send_telegram_message(
            order.agent_tg_id,
            agent_brigadier_text(order, brig_assignment.employee, data.contact_time),
        )
    return {"ok": True, "agent_notified": sent, "order": _order_dict(order, db)}


# ---------- GBU import ----------

@app.post("/api/gbu/ocr")
async def gbu_ocr(file: UploadFile = File(...), db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    try:
        from PIL import Image
        import pytesseract
    except Exception as exc:
        raise HTTPException(503, "OCR dependencies are not installed") from exc

    try:
        image = Image.open(file.file)
        raw = pytesseract.image_to_string(image, lang="rus+eng")
    except Exception as exc:
        raise HTTPException(422, f"Не удалось распознать изображение: {exc}") from exc
    drafts = parse_gbu_ocr_text(raw)
    db.add(ImportLog(kind="gbu_ocr", created_by_tg_id=user.id, raw_text=raw, result_json=json.dumps(drafts, ensure_ascii=False, default=str)))
    db.commit()
    return {"raw_text": raw, "drafts": drafts, "warning": "Проверьте каждую строку перед сохранением — OCR может ошибаться."}


@app.post("/api/gbu/drafts")
def create_gbu_drafts(items: list[GbuDraft], db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    created = []
    for item in items:
        order = _create_order(db, OrderCreate(source="gbu", **item.model_dump()))
        created.append(order)
    db.commit()
    return [_order_dict(x, db) for x in created]


# ---------- Control ----------

@app.get("/api/control")
def control(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    orders = db.execute(select(Order).where(Order.work_date == work_date, Order.status != "cancelled").order_by(Order.issue_time)).scalars().all()
    order_items = [_order_dict(x, db) for x in orders]
    readiness = readiness_summary_data(db, work_date)
    loads = _workloads(db, work_date)
    overloaded = []
    for emp_id, cnt in sorted(loads.items(), key=lambda x: -x[1]):
        emp = db.get(Employee, emp_id)
        if emp:
            overloaded.append(_employee_dict(emp, cnt))
    no_response = []
    for code, group in readiness["groups"].items():
        for item in group["no_response_cutoff"]:
            no_response.append(item)
    return {
        "orders": {
            "total": len(order_items),
            "not_complete": [x for x in order_items if not x["composition_complete"] and x["status"] not in {"closed", "cancelled"}],
            "ready": [x for x in order_items if x["composition_complete"]],
        },
        "no_response_cutoff": no_response,
        "workload": overloaded,
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "LEGION Dispatcher"}
