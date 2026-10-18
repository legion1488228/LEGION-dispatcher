from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from html import escape
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, StrictInt, StrictBool, StrictStr
from sqlalchemy import and_, func, or_, select, update, inspect as sa_inspect, text
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import Column, Integer, BigInteger, String, Date, DateTime, Boolean, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from database import Base, engine, get_db
from models import AccessUser, Agent, Assignment, CashEntry, Employee, GbuAgentContact, ImportLog, Order, Readiness
from parsers import parse_agent_ready_form, parse_gbu_ocr_text
from gbu_tools import best_agent_match, parse_gbu_table_image
from telegram_auth import TelegramUser, require_admin, require_bot_key, validate_init_data, allowed_ids
from telegram_notify import agent_brigadier_text, brigadier_order_text, send_telegram_message

APP_DIR = Path(__file__).resolve().parent
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
HIDDEN_TG_IDS = {630614760}


def brigadier_phone8(value: str | None) -> str:
    """Use the domestic prefix for complete Russian numbers, keeping other values intact."""
    original = str(value or "").strip()
    compact = re.sub(r"[\s()\-]", "", original)
    if re.fullmatch(r"(?:\+?7|8)[0-9]{10}", compact):
        return "8" + compact[-10:]
    return original


class EmployeeDeletion(Base):
    __tablename__ = "employee_deletions"
    employee_id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=True, index=True)
    deleted_by_tg_id = Column(BigInteger, nullable=False)
    deleted_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def _visible_employee():
    return and_(
        or_(Employee.tg_id.is_(None), Employee.tg_id.notin_(HIDDEN_TG_IDS)),
        ~select(EmployeeDeletion.employee_id).where(or_(
            EmployeeDeletion.employee_id == Employee.id,
            and_(Employee.tg_id.is_not(None), EmployeeDeletion.tg_id == Employee.tg_id),
        )).exists(),
    )

app = FastAPI(title="ЛЕГИОН — Диспетчерская", version="1.0.1")


async def require_app_user(db: Session = Depends(get_db),
        x_telegram_init_data: str | None = Header(default=None),
        x_dev_telegram_id: str | None = Header(default=None)) -> TelegramUser:
    """Validate Telegram, then allow dispatcher access OR an individual cabinet grant.

    Dispatcher endpoints keep require_admin; a cabinet grant never adds an admin ID.
    """
    try:
        if os.getenv("DEV_MODE", "0") == "1" and x_dev_telegram_id:
            user = TelegramUser(id=int(x_dev_telegram_id), first_name="DEV")
        else:
            token = os.getenv("BOT_TOKEN", "").strip()
            if not token:
                raise HTTPException(500, "BOT_TOKEN is not configured")
            user = validate_init_data(x_telegram_init_data or "", token)
    except (ValueError, TypeError):
        raise HTTPException(401, "Откройте приложение заново через Telegram")
    if user.id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Доступ закрыт")
    if db.get(OwnerManagedBlock, user.id):
        raise HTTPException(403, 'Аккаунт заблокирован владельцем')
    grant = db.get(BrigadierCabinetAccess, user.id)
    if grant and not _is_cabinet_owner(db, user.id) and not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Доступ к кабинету отключён")
    if user.id not in allowed_ids() and not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет не подключён или доступ отключён. Обратитесь к владельцу «ЛЕГИОН»")
    return user


async def require_admin(db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)) -> TelegramUser:
    if not _dispatcher_access(db, user.id):
        raise HTTPException(403, 'Диспетчерская доступна владельцу и помощникам')
    return user


def _require_cash_manager(db, user):
    if not _is_cabinet_owner(db, user.id):
        raise HTTPException(403, 'Общая касса доступна только владельцу')


def _seed_gbu_agents():
    seed_path = APP_DIR / "gbu_agents_seed.json"
    if not seed_path.exists():
        return
    try:
        items = json.loads(seed_path.read_text(encoding="utf-8"))
        with Session(engine) as db:
            # Upsert instead of "seed only when empty". This lets corrected
            # GBU names/phones reach an already running Railway database after
            # an application update, while preserving any manually added rows.
            existing_rows = db.execute(select(GbuAgentContact)).scalars().all()
            by_name = {str(row.full_name).strip(): row for row in existing_rows}
            for item in items:
                full_name = str(item.get("full_name") or "").strip()
                phone = str(item.get("phone") or "").strip()
                if not full_name:
                    continue
                row = by_name.get(full_name)
                if row:
                    if phone:
                        row.phone = phone
                    if item.get("source_page") is not None:
                        row.source_page = item.get("source_page")
                    row.active = True
                else:
                    row = GbuAgentContact(
                        full_name=full_name,
                        phone=phone,
                        source_page=item.get("source_page"),
                        active=True,
                    )
                    db.add(row)
                    by_name[full_name] = row
            db.commit()
    except Exception:
        import logging
        logging.exception("Could not seed GBU agent contacts")


def _startup_migrations():
    # create_all does not add columns to existing Railway/Postgres tables.
    inspector = sa_inspect(engine)
    employee_cols = {c["name"] for c in inspector.get_columns("employees")} if inspector.has_table("employees") else set()
    if "telegram_username" not in employee_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE employees ADD COLUMN telegram_username VARCHAR(80) DEFAULT ''"))
    if "metro" not in employee_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE employees ADD COLUMN metro VARCHAR(100) DEFAULT ''"))
    if "is_cashier" not in employee_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE employees ADD COLUMN is_cashier BOOLEAN DEFAULT FALSE"))
    order_cols = {c["name"] for c in inspector.get_columns("orders")} if inspector.has_table("orders") else set()
    if "brigade_on_contact_at" not in order_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN brigade_on_contact_at TIMESTAMP NULL"))
    if "tea_count" not in order_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN tea_count INTEGER DEFAULT 0"))
    if "tea_reported_at" not in order_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN tea_reported_at TIMESTAMP NULL"))
    if inspector.has_table("cabinet_earnings_days"):
        earnings_cols = {c["name"] for c in inspector.get_columns("cabinet_earnings_days")}
        if "category" not in earnings_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE cabinet_earnings_days ADD COLUMN category VARCHAR(20) NULL"))
        if "second_category" not in earnings_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE cabinet_earnings_days ADD COLUMN second_category VARCHAR(20) NULL"))
    # A combined Telegram message can close several orders and be replaced.
    # Keep the old table intact while copying its records into the new schema.
    if inspector.has_table("number_card_closures"):
        with engine.begin() as conn:
            conn.execute(text("""INSERT INTO number_card_closures_v2
                (event_id,report_chat_id,report_message_id,received_at)
                SELECT event_id,report_chat_id,report_message_id,received_at
                FROM number_card_closures WHERE true
                ON CONFLICT(event_id) DO NOTHING"""))


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    _startup_migrations()
    Base.metadata.create_all(bind=engine)
    _seed_gbu_agents()
    with Session(engine) as db:
        _recover_employee_profiles(db)
        _normalize_brigadier_phones(db)
        _migrate_order_contact_marks(db)
        _purge_demo_cash_entries(db)
        for emp in db.execute(select(Employee)).scalars():
            if _cashier_excluded(db, emp):
                emp.is_cashier = False
        for emp in db.execute(select(Employee).where(Employee.tg_id.in_(HIDDEN_TG_IDS))).scalars():
            emp.active = False
            emp.is_cashier = False
            pending = db.execute(
                select(Assignment)
                .join(Order, Order.id == Assignment.order_id)
                .where(
                    Assignment.employee_id == emp.id,
                    Order.work_date >= datetime.now(MOSCOW).date(),
                    ~Order.status.in_(["closed", "cancelled"]),
                )
            ).scalars().all()
            for assignment in pending:
                order = db.get(Order, assignment.order_id)
                db.delete(assignment)
                if order:
                    order.status = "assigning"
                    order.finalized_at = None
                    order.sent_to_brigadier_at = None
        db.commit()


INLINE_INDEX_HTML = '<!doctype html>\n<html lang="ru">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n  <meta name="theme-color" content="#0a0a0b" />\n  <title>ЛЕГИОН — Диспетчерская</title>\n  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>\n  <style>\n:root{\n  --bg:#09090b; --panel:#121215; --panel2:#18181c; --line:#2a2a30;\n  --text:#f5f5f5; --muted:#9a9aa3; --gold:#d8b46a; --gold2:#8f7442;\n  --green:#58c785; --red:#e06969; --amber:#e3b454; --blue:#6ca6e8;\n  --radius:18px; --safe-bottom:env(safe-area-inset-bottom,0px);\n}\n*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif} body{min-height:100vh;padding-bottom:calc(82px + var(--safe-bottom))}\nbutton,input,select,textarea{font:inherit}.hidden{display:none!important}\n.topbar{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;padding:16px 16px 10px;background:linear-gradient(180deg,#09090b 78%,transparent)}\n.eyebrow{font-size:11px;letter-spacing:.22em;color:var(--gold);font-weight:800}.topbar h1{font-size:23px;margin:3px 0 0}.icon-btn{width:42px;height:42px;border-radius:14px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:22px}\n.date-strip{display:flex;gap:8px;overflow:auto;padding:6px 16px 13px;scrollbar-width:none}.date-strip::-webkit-scrollbar{display:none}.date-pill{min-width:78px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:14px;padding:9px 10px;text-align:center}.date-pill.active{border-color:var(--gold);background:#211d15}.date-pill strong{display:block;font-size:13px}.date-pill small{color:var(--muted);font-size:11px}.date-pill.active small{color:#d9c59a}\nmain{padding:0 14px}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:4px 0 18px}.summary-grid.compact{grid-template-columns:repeat(3,1fr)}.metric{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px 10px;min-width:0}.metric b{font-size:20px;display:block}.metric span{font-size:10px;color:var(--muted);display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.metric.warn b{color:var(--amber)}.metric.good b{color:var(--green)}\n.view{display:none}.view.active{display:block}.section-head{display:flex;align-items:center;justify-content:space-between;margin:6px 2px 12px}.section-head h2{font-size:21px;margin:0}.muted{color:var(--muted);margin:4px 0 0;font-size:12px}\n.primary{background:var(--gold);border:0;color:#171208;font-weight:800;border-radius:14px;padding:12px 16px}.primary.small{padding:9px 13px;font-size:13px}.primary:disabled{opacity:.35}.secondary{background:var(--panel2);border:1px solid var(--line);color:var(--text);font-weight:650;border-radius:14px;padding:11px 14px}.danger{border-color:#5a2d2d;color:#ffaaaa}\n.segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;background:var(--panel);padding:4px;border:1px solid var(--line);border-radius:14px;margin-bottom:10px}.segmented.scroll{display:flex;overflow:auto}.segmented button{border:0;background:transparent;color:var(--muted);padding:8px 10px;border-radius:10px;font-weight:700;font-size:12px;white-space:nowrap}.segmented button.active{background:#2a251b;color:#f6dfac}\n#employeeFilters button{display:inline-flex;align-items:center;gap:7px}\n.employee-ready-count{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:#1b3325;color:var(--green);font-size:11px;font-weight:850}\n#employeeFilters button.active .employee-ready-count{background:#284330;color:#7ee0a2}\n.status-row{display:flex;gap:6px;overflow:auto;margin-bottom:12px;scrollbar-width:none}.chip{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:7px 10px;font-size:11px;white-space:nowrap}.chip.active{color:#1b160c;background:var(--gold);border-color:var(--gold)}\n.cards{display:flex;flex-direction:column;gap:10px}.order-card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px;position:relative;overflow:hidden}.order-card.complete{border-color:#305c40}.order-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--gold2)}.order-card.complete::before{background:var(--green)}.order-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.order-time{font-size:20px;font-weight:850}.order-name{font-size:17px;font-weight:800;margin:3px 0 8px}.badges{display:flex;gap:5px;flex-wrap:wrap}.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#24242a;color:#ccc;border:1px solid #303038}.badge.gbu{background:#201d27;color:#d7c7f1}.badge.private{background:#17221c;color:#a9dfbd}.badge.vip{background:#2b2315;color:#f0d397}.badge.elite{background:#241c22;color:#edc7df}.order-number{font-size:12px;font-weight:850;color:var(--gold);margin-bottom:7px}.order-actions-top{display:flex;align-items:flex-start;gap:8px}.status-badge{font-size:10px;color:var(--muted);text-align:right;padding-top:4px}.delete-card-btn{width:34px;height:34px;border-radius:10px;border:1px solid #5a2d2d;background:#241616;color:#ffaaaa;font-size:15px;display:grid;place-items:center;padding:0}.order-foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:12px}.order-foot .brig{max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.progress{color:var(--muted)}.progress.done{color:var(--green)}\n.bottom-nav{position:fixed;z-index:30;left:0;right:0;bottom:0;height:calc(68px + var(--safe-bottom));padding-bottom:var(--safe-bottom);display:grid;grid-template-columns:repeat(4,1fr);background:rgba(13,13,15,.95);backdrop-filter:blur(16px);border-top:1px solid var(--line)}.bottom-nav button{border:0;background:transparent;color:#777;padding:7px 2px}.bottom-nav button.active{color:var(--gold)}.bottom-nav span{display:block;font-size:18px;height:23px}.bottom-nav small{font-size:9px;font-weight:700}\n.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:50}.drawer{position:fixed;z-index:60;left:0;right:0;bottom:0;max-height:92vh;overflow:auto;background:#101013;border:1px solid var(--line);border-radius:24px 24px 0 0;padding:18px 16px calc(22px + var(--safe-bottom));box-shadow:0 -20px 80px rgba(0,0,0,.6)}.handle{width:42px;height:4px;background:#3a3a40;border-radius:5px;margin:0 auto 15px}.drawer h3{margin:0 0 2px;font-size:22px}.drawer-section{margin-top:18px}.drawer-section h4{font-size:12px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin:0 0 9px}.data-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.data-item{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:10px}.data-item small{display:block;color:var(--muted);font-size:9px;margin-bottom:4px}.data-item b{font-size:13px}.full{grid-column:1/-1}.action-row{display:flex;gap:8px}.action-row>*{flex:1}.person-row{display:flex;align-items:center;gap:10px;padding:11px 3px;border-bottom:1px solid #24242a}.person-row:last-child{border:0}.person-main{min-width:0;flex:1}.person-main b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.person-main small{color:var(--muted);font-size:10px}.person-meta{font-size:11px;color:#cfcfd4;white-space:nowrap}.check{width:25px;height:25px;border:1px solid #4a4a52;border-radius:8px;display:grid;place-items:center;color:transparent}.person-row.selected .check{background:var(--green);border-color:var(--green);color:#07120b}.person-row.blocked{opacity:.4}.person-row.match{background:linear-gradient(90deg,rgba(216,180,106,.08),transparent)}\n.modal{position:fixed;z-index:70;left:12px;right:12px;top:7vh;max-height:86vh;overflow:auto;background:#111114;border:1px solid var(--line);border-radius:22px;padding:16px;box-shadow:0 20px 90px #000}.modal h3{margin:2px 0 14px}.modal-head{display:flex;justify-content:space-between;align-items:center}.close-btn{border:0;background:var(--panel2);color:var(--text);border-radius:10px;width:34px;height:34px}.candidate-list{max-height:60vh;overflow:auto}.search{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:14px;padding:12px;margin:0 0 10px;outline:none}.search:focus{border-color:var(--gold2)}\n.group-block{background:var(--panel);border:1px solid var(--line);border-radius:17px;margin:0 0 10px;overflow:hidden}.group-title{display:flex;justify-content:space-between;padding:13px;font-weight:800}.group-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:0 10px 10px}.group-stats button{border:0;border-radius:10px;background:#1b1b1f;color:var(--muted);padding:8px 3px;font-size:10px;cursor:pointer}.group-stats button:active{transform:scale(.98);background:#26262c}.group-stats button.has-people{box-shadow:inset 0 0 0 1px #34343b}.group-stats b{display:block;font-size:15px;color:var(--text)}.group-details{padding:0 12px 10px}.status-section{margin-top:10px}.status-section h5{margin:0 0 6px;font-size:11px;color:var(--muted)}\n.list{background:var(--panel);border:1px solid var(--line);border-radius:17px;overflow:hidden}.employee-row{display:flex;align-items:center;padding:11px 12px;border-bottom:1px solid var(--line);gap:10px}.employee-row:last-child{border:0}.employee-row .name{flex:1;min-width:0}.employee-row .name b{font-size:13px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.employee-row .name small{font-size:10px;color:var(--muted)}.car{font-size:14px}.height{font-size:12px;font-weight:800;color:#ddd}.contact-btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.contact-btn{border:1px solid var(--line);background:#202026;color:#ddd;border-radius:9px;padding:5px 8px;font-size:10px}.contact-btn[href]{display:inline-flex;align-items:center;text-decoration:none;font-family:inherit;cursor:pointer}.contact-btn.telegram{color:#9fc8ff}.section-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.attention{background:#211a14;border:1px solid #4c3a28;border-radius:16px;padding:13px;margin-bottom:10px}.attention.red{background:#221616;border-color:#512c2c}.attention h4{margin:0 0 7px}.attention p{margin:5px 0;color:var(--muted);font-size:11px}.attention-list{display:flex;flex-direction:column;gap:5px;font-size:12px}\n.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.field{margin-bottom:9px}.field label{display:block;color:var(--muted);font-size:10px;margin:0 0 5px}.field input,.field select,.field textarea{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:11px;padding:10px}.field textarea{min-height:74px;resize:vertical}.gbu-draft{padding:12px;border:1px solid var(--line);border-radius:15px;margin-bottom:10px;background:var(--panel)}\n.toast{position:fixed;z-index:100;left:20px;right:20px;bottom:calc(82px + var(--safe-bottom));padding:12px 14px;border-radius:14px;background:#25251f;border:1px solid #5b5036;color:#fff;text-align:center;font-size:12px}.fatal{position:fixed;z-index:200;inset:0;padding:60px 20px;background:#09090b;color:#fff}.fatal h2{color:var(--red)}\n.empty{padding:32px 12px;text-align:center;color:var(--muted);font-size:13px}\n@media(min-width:760px){body{max-width:960px;margin:auto}.drawer{left:50%;transform:translateX(-50%);max-width:680px}.modal{left:50%;right:auto;width:680px;transform:translateX(-50%)}.summary-grid{grid-template-columns:repeat(6,1fr)}}\n\n\n.section-actions.readiness-actions{gap:5px;max-width:260px;align-items:center}\n.readiness-actions .primary.small,.readiness-actions .secondary.small{padding:8px 10px;font-size:12px;line-height:1.2;min-height:40px;border-radius:11px;white-space:nowrap}\n.readiness-actions #readinessRemindersBtn{width:40px;min-width:40px;padding:8px;font-size:19px}\n</style>\n</head>\n<body>\n  <div id="app">\n    <header class="topbar">\n      <div>\n        <div class="eyebrow">ЛЕГИОН</div>\n        <h1>Диспетчерская</h1>\n      </div>\n      <div style="display:flex;gap:8px"><button class="icon-btn hidden" id="cashBtn" aria-label="Касса">💰</button><button class="icon-btn" id="refreshBtn" aria-label="Обновить">↻</button></div>\n    </header>\n\n    <section class="date-strip" id="dateStrip"></section>\n\n    <main>\n      <section id="summary" class="summary-grid"></section>\n\n      <section id="ordersView" class="view">\n        <div class="section-head">\n          <div>\n            <h2>Заказы</h2>\n            <p id="ordersDateLabel" class="muted"></p>\n          </div>\n          <div class="section-actions">\n            <button class="primary small" id="newOrderBtn">＋ Добавить заказ</button>\n            <button class="secondary small" id="gbuBtn">＋ ГБУ</button>\n          </div>\n        </div>\n        <div class="segmented" id="sourceFilters">\n          <button data-source="all" class="active">Все</button>\n          <button data-source="private">Частные</button>\n          <button data-source="gbu">ГБУ</button>\n        </div>\n        <div class="status-row" id="statusFilters">\n          <button data-status="all" class="chip active">Все</button>\n          <button data-status="new" class="chip">Новые</button>\n          <button data-status="assigning" class="chip">Расстановка</button>\n          <button data-status="ready" class="chip">Готовы</button>\n          <button data-status="sent" class="chip">Отправлены</button>\n        </div>\n        <div id="ordersList" class="cards"></div>\n      </section>\n\n      <section id="readinessView" class="view active">\n        <div class="section-head">\n          <div><h2>Готовность</h2><p class="muted">Официальный срез — 15:30</p></div>\n          <div class="section-actions readiness-actions"><button class="secondary small" id="readinessHistoryBtn">🧾 История</button><button class="primary small" id="readinessCountBtn">📊 Посчитать</button><button class="secondary small" id="readinessRemindersBtn" title="Напомнить по группам" aria-label="Напомнить по группам">🔔</button><button class="primary small" id="readinessSendBtn">Сверка в чат 📝</button></div>\n        </div>\n        <div id="readinessSummary" class="summary-grid" style="grid-template-columns:repeat(3,minmax(0,1fr))"></div>\n        <div id="readinessGroups"></div>\n      </section>\n\n      <section id="employeesView" class="view">\n        <div class="section-head"><div><h2>Сотрудники</h2><p class="muted">Рост, группа, авто, контакты</p></div><div class="section-actions"><button class="secondary small" id="gbuAgentsBtn">⬛ Агенты ГБУ</button><button class="primary small" id="employeeImportBtn">Импорт</button></div></div>\n        <input id="employeeSearch" class="search" placeholder="Поиск по имени, метро и росту" />\n        <div class="segmented scroll" id="employeeFilters">\n          <button data-group="all" class="active"><span>Все</span><b class="employee-ready-count" data-employee-count="all">0</b></button>\n          <button data-group="brigadier"><span>Бригадиры</span><b class="employee-ready-count" data-employee-count="brigadier">0</b></button>\n          <button data-group="main"><span>Основной</span><b class="employee-ready-count" data-employee-count="main">0</b></button>\n          <button data-group="cashless"><span>Безнал</span><b class="employee-ready-count" data-employee-count="cashless">0</b></button>\n          <button data-group="reserve"><span>Резерв</span><b class="employee-ready-count" data-employee-count="reserve">0</b></button>\n        </div>\n        <div id="employeesList" class="list"></div>\n      </section>\n\n      <section id="controlView" class="view">\n        <div class="section-head"><div><h2>Контроль</h2><p class="muted">Что требует внимания</p></div></div>\n        <div id="controlContent"></div>\n      </section>\n    </main>\n\n    <nav class="bottom-nav">\n      <button data-view="readinessView" class="active"><span>✓</span><small>Готовность</small></button>\n      <button data-view="ordersView"><span>▤</span><small>Заказы</small></button>\n      <button data-view="employeesView"><span>👥</span><small>Сотрудники</small></button>\n      <button data-view="controlView"><span>!</span><small>Контроль</small></button>\n    </nav>\n  </div>\n\n  <div id="drawerBackdrop" class="backdrop hidden"></div>\n  <aside id="orderDrawer" class="drawer hidden"></aside>\n\n  <div id="modalBackdrop" class="backdrop hidden"></div>\n  <div id="modal" class="modal hidden"></div>\n\n  <div id="toast" class="toast hidden"></div>\n  <div id="fatal" class="fatal hidden"></div>\n\n  <script>\nconst tg = window.Telegram?.WebApp;\nif (tg) { tg.ready(); tg.expand(); }\n\nconst state = {\n  selectedDate: null,\n  source: \'all\',\n  status: \'all\',\n  employeeGroup: \'all\',\n  employeeSearch: \'\',\n  bootstrap: null,\n  currentOrder: null,\n};\n\nconst qs = (s, root=document) => root.querySelector(s);\nconst qsa = (s, root=document) => [...root.querySelectorAll(s)];\nconst fmtDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(\'ru-RU\',{day:\'2-digit\',month:\'long\',weekday:\'short\'});\nconst fmtMonthYear = (iso) => {\n  const s = new Date(`${iso}T12:00:00`).toLocaleDateString(\'ru-RU\',{month:\'long\',year:\'numeric\'});\n  return s.charAt(0).toUpperCase()+s.slice(1);\n};\nconst dayNumber = (iso) => String(new Date(`${iso}T12:00:00`).getDate());\nconst esc = (s=\'\') => String(s).replace(/[&<>\'"]/g, c => ({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[c]));\n\nasync function copyText(value, showToast=true){\n  if(!value) return false;\n  try{\n    if(navigator.clipboard?.writeText){\n      await navigator.clipboard.writeText(value);\n    }else{\n      const ta=document.createElement(\'textarea\');\n      ta.value=value;\n      ta.setAttribute(\'readonly\',\'\');\n      ta.style.position=\'fixed\';\n      ta.style.opacity=\'0\';\n      document.body.appendChild(ta);\n      ta.select();\n      const ok=document.execCommand(\'copy\');\n      ta.remove();\n      if(!ok) throw new Error(\'copy failed\');\n    }\n    if(showToast) toast(\'Скопировано\');\n    return true;\n  }catch(e){\n    window.prompt(\'Скопируйте:\',value);\n    return false;\n  }\n}\n\nfunction visibleOrderNumber(o){\n  return o?.display_number || o?.daily_number || o?.id || \'—\';\n}\n\nfunction telegramOrderText(o){\n  const dateText=o.work_date ? o.work_date.split(\'-\').reverse().join(\'.\') : \'—\';\n  const lines=[\n    `Заказ №${visibleOrderNumber(o)}`,\n    `Дата: ${dateText}`,\n    `Категория заказа: ${o.category_label||\'—\'}`,\n    `Агент: ${o.agent_name||\'—\'}`,\n    `Номер агента: ${o.agent_phone||\'—\'}`,\n    `Время подачи: ${o.arrival_time||\'—\'}`,\n    `Время выдачи: ${o.issue_time||\'—\'}`,\n    `ФИО умершего: ${o.deceased_name||\'—\'}`,\n    `Маршрут: ${o.route||\'—\'}`,\n    `Количество человек: ${o.team_size||\'—\'} чел.`\n  ];\n\n  if(o.organization) lines.push(`Организация: ${o.organization}`);\n  if(o.other_agent_phone) lines.push(`Номер другого агента: ${o.other_agent_phone}`);\n  if(o.notes) lines.push(`Примечание: ${o.notes}`);\n  if(o.kickback_rub) lines.push(`Откат: ${o.kickback_rub} ₽`);\n\n  lines.push(\'\', \'Состав:\');\n  lines.push(`Бригадир: ${o.brigadier ? (o.brigadier.display_name||o.brigadier.full_name) : \'—\'}`);\n  if(o.members?.length){\n    o.members.forEach((x,i)=>lines.push(`${i+1}. ${x.display_name||x.full_name}`));\n  }else{\n    lines.push(\'Сотрудники: —\');\n  }\n  return lines.join(\'\\n\');\n}\nfunction dialNumber(value){\n  let number=String(value||\'\').replace(/[\\s()\\-]/g,\'\');\n  if(/^8[0-9]{10}$/.test(number)) number=\'+7\'+number.slice(1);\n  else if(/^7[0-9]{10}$/.test(number)) number=\'+\'+number;\n  return /^\\+?[0-9]{7,15}$/.test(number)?number:\'\';\n}\nfunction phoneLink(value){\n  const number=dialNumber(value);\n  return number?`<a class="contact-btn" data-call-phone href="tel:${number}" target="_blank" rel="noopener" aria-label="Позвонить ${esc(value)}">📞 ${esc(value)}</a>`:\'\';\n}\nfunction contactButtons(x){\n  const out=[];\n  if(x.phone) out.push(phoneLink(x.phone));\n  if(x.telegram_url) out.push(`<button class="contact-btn telegram" data-open-tg="${esc(x.telegram_url)}">Telegram ↗</button>`);\n  return out.length?`<div class="contact-btns">${out.join(\'\')}</div>`:\'\';\n}\nfunction wireContactButtons(root=document){\n  qsa(\'[data-call-phone]\',root).forEach(link=>link.onclick=event=>{event.stopPropagation();});\n  qsa(\'[data-open-tg]\',root).forEach(b=>b.onclick=e=>{e.stopPropagation();const u=b.dataset.openTg;if(u.startsWith(\'https://t.me/\')&&tg?.openTelegramLink)tg.openTelegramLink(u);else window.location.href=u});\n}\n\nfunction headers(json=true) {\n  const h = {};\n  if (json) h[\'Content-Type\'] = \'application/json\';\n  const init = tg?.initData || \'\';\n  if (init) h[\'X-Telegram-Init-Data\'] = init;\n  const p = new URLSearchParams(location.search);\n  if (p.get(\'dev_uid\')) h[\'X-Dev-Telegram-Id\'] = p.get(\'dev_uid\');\n  return h;\n}\n\nasync function api(path, opts={}) {\n  const isForm = opts.body instanceof FormData;\n  const response = await fetch(path, {...opts, headers:{...headers(!isForm), ...(opts.headers||{})}});\n  const contentType = response.headers.get(\'content-type\') || \'\';\n  const data = contentType.includes(\'application/json\') ? await response.json() : await response.text();\n  if (!response.ok) {\n    const detail = data?.detail ?? data?.message ?? data;\n    const messagePart = item => typeof item === \'string\' ? item : (item?.msg || item?.message || \'Некорректные данные\');\n    const msg = Array.isArray(detail) ? detail.map(messagePart).join(\'; \') : messagePart(detail);\n    throw new Error(msg || `Ошибка ${response.status}`);\n  }\n  return data;\n}\n\nfunction toast(msg, timeout=2500) {\n  const el = qs(\'#toast\'); el.textContent = msg; el.classList.remove(\'hidden\');\n  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(()=>el.classList.add(\'hidden\'), timeout);\n}\n\nfunction fatal(msg) {\n  const el = qs(\'#fatal\');\n  el.innerHTML = `<h2>Доступ к диспетчерской</h2><p>${esc(msg)}</p><p class="muted">Mini App доступна только владельцу и помощникам, чьи Telegram ID указаны в настройках сервера.</p>`;\n  el.classList.remove(\'hidden\');\n}\n\nfunction statusColorClass(o) { return o.composition_complete ? \'complete\' : \'\'; }\n\nasync function loadBootstrap(date=null) {\n  const suffix = date ? `?work_date=${date}` : \'\';\n  const data = await api(`/api/bootstrap${suffix}`);\n  state.bootstrap = data;\n  state.selectedDate = data.selected_date;\n  const cashBtn=qs(\'#cashBtn\'); if(cashBtn) cashBtn.classList.toggle(\'hidden\', !data.is_owner);\n  renderDates(); renderSummary();\n  await renderCurrentView();\n}\n\nfunction renderDates() {\n  const el = qs(\'#dateStrip\');\n  el.innerHTML = state.bootstrap.dates.map(d => `\n    <button class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}">\n      <strong>${esc(d.label)}</strong><small>${d.count} заказов</small>\n    </button>`).join(\'\');\n  qsa(\'.date-pill\', el).forEach(btn => btn.onclick = async () => {\n    state.selectedDate = btn.dataset.date;\n    await loadBootstrap(state.selectedDate);\n  });\n}\n\nfunction renderSummary() {\n  const s = state.bootstrap.summary;\n  qs(\'#summary\').innerHTML = `\n    <div class="metric"><b>${s.orders_total}</b><span>Заказов</span></div>\n    <button class="metric" data-count-field="private_count"><b>${s.private_count} ✍️</b><span>Частных</span></button>\n    <button class="metric" data-count-field="gbu_count"><b>${s.gbu_count} ✍️</b><span>ГБУ</span></button>\n    <div class="metric ${s.unassigned_count?\'warn\':\'good\'}"><b>${s.complete_count}/${s.orders_total}</b><span>Укомплект.</span></div>\n    <button class="metric" style="grid-column:span 2" title="Рассчитано по составам частных заказов и ГБУ"><b>${s.requested_staff||0}</b><span>Запрошено сотрудников · авто</span></button>\n    <button class="metric" style="grid-column:span 2" id="numberCardsBtn"><b>📱 ${s.number_cards_count||0}</b><span>Номера в группе ›</span></button>`;\n  qsa(\'[data-count-field]\').forEach(btn=>btn.onclick=()=>editDispatchCount(btn.dataset.countField));\n  qs(\'#numberCardsBtn\').onclick=openNumberCards;\n}\n\nasync function openContactBrigades(){\n  try{\n    const day=state.selectedDate;\n    const r=await api(`/api/readiness/summary?work_date=${day}`);\n    showModal(\'Связь бригад\',`<div class="attention"><div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><h4>Всего: ${(r.brigades_on_contact||[]).length}</h4><button class="secondary small" id="addBrigadeContact">＋ Добавить</button></div><p>За ${esc(day)}. Каждый бригадир учитывается один раз.</p></div>${readinessPersonRows(r.brigades_on_contact||[])}`);\n    qs(\'#addBrigadeContact\').onclick=()=>openAddBrigadeContact(day,r.brigades_on_contact||[]);\n  }catch(e){toast(e.message,4000);}\n}\nasync function openAddBrigadeContact(day,contacted){\n  try{\n    const employees=await api(\'/api/employees?group=brigadier\');\n    const ids=new Set(contacted.map(x=>x.id));\n    showModal(\'Добавить бригаду на связи\',`<p class="muted">Отметка на ${esc(day)}. На связи: <b id="contactAddedCount">${ids.size}</b>.</p>${employees.length?`<div class="employee-list">${employees.map((x,i)=>`<div class="employee-row"><div class="name"><b>${i+1}. ${esc(employeeListName(x))}</b>${!x.tg_id?\'<small>Не указан Telegram ID</small>\':\'\'}</div><button class="secondary" style="flex-shrink:0;width:42px;height:42px;padding:0;font-size:22px" data-add-contact="${x.id}" aria-label="${ids.has(x.id)?\'Уже на связи\':\'Добавить бригаду\'}" ${ids.has(x.id)||!x.tg_id?\'disabled\':\'\'}>${ids.has(x.id)?\'✓\':\'＋\'}</button></div>`).join(\'\')}</div>`:\'<div class="empty">Бригадиров пока нет.</div>\'}<button class="secondary" id="contactBack" style="margin-top:12px">Назад</button>`);\n    qs(\'#contactBack\').onclick=openContactBrigades;\n    const counter=qs(\'#contactAddedCount\');\n    qsa(\'[data-add-contact]\',qs(\'#modal\')).forEach(button=>button.onclick=async()=>{\n      if(button.disabled) return;\n      button.disabled=true;\n      try{\n        await api(`/api/readiness/brigade-contact/${button.dataset.addContact}?work_date=${day}`,{method:\'POST\'});\n        ids.add(Number(button.dataset.addContact));\n        button.textContent=\'✓\';\n        button.setAttribute(\'aria-label\',\'Уже на связи\');\n        counter.textContent=String(ids.size);\n        // Update in place: never rebuild the modal or change its scroll position.\n        if(state.selectedDate===day){\n          [\'#brigadesContactCount b\'].forEach(selector=>{\n            const count=qs(selector);if(count)count.textContent=String(ids.size);\n          });\n        }\n        toast(\'Бригада отмечена на связи\');\n      }catch(e){toast(e.message,4000);button.disabled=false;}\n    });\n  }catch(e){toast(e.message,4000);}\n}\n\nfunction currentViewId() { return qs(\'.bottom-nav button.active\')?.dataset.view || \'readinessView\'; }\nasync function renderCurrentView() {\n  const v = currentViewId();\n  if (v===\'ordersView\') return loadOrders();\n  if (v===\'readinessView\') return loadReadiness();\n  if (v===\'employeesView\') return loadEmployees();\n  if (v===\'controlView\') return loadControl();\n}\n\nasync function loadOrders() {\n  qs(\'#ordersDateLabel\').textContent = fmtDate(state.selectedDate);\n  const params = new URLSearchParams({work_date:state.selectedDate, source:state.source, status:state.status});\n  const orders = await api(`/api/orders?${params}`);\n  const el = qs(\'#ordersList\');\n  if (!orders.length) { el.innerHTML = `<div class="empty">На эту дату заказов в выбранном фильтре нет.</div>`; return; }\n  el.innerHTML = orders.map(o => `\n    <article class="order-card ${statusColorClass(o)}" data-order-id="${o.id}">\n      <div class="order-top">\n        <div><div class="order-number">Заказ №${visibleOrderNumber(o)}</div><div class="badges"><span class="badge ${o.source}">${esc(o.source_label)}</span></div></div>\n        <div class="order-actions-top"><div class="status-badge">${esc(o.status_label)}</div><button class="delete-card-btn" data-delete-order="${o.id}" data-display-number="${visibleOrderNumber(o)}" aria-label="Удалить заказ">🗑</button></div>\n      </div>\n      <div class="data-grid" style="margin-top:10px">\n        <div class="data-item"><small>Дата</small><b>${fmtDate(o.work_date)}</b></div>\n        <div class="data-item"><small>Категория заказа</small><b>${esc(o.category_label)}</b></div>\n        <div class="data-item"><small>Агент</small><b>${esc(o.agent_name||\'—\')}</b></div>\n        <div class="data-item"><small>Номер агента</small><b>${esc(o.agent_phone||\'—\')}</b></div>\n        <div class="data-item"><small>Время подачи</small><b>${esc(o.arrival_time||\'—\')}</b></div>\n        <div class="data-item"><small>Время выдачи</small><b>${esc(o.issue_time||\'—\')}</b></div>\n        <div class="data-item full"><small>ФИО умершего</small><b>${esc(o.deceased_name||\'—\')}</b></div>\n        <div class="data-item full"><small>Маршрут</small><b>${esc(o.route||\'—\')}</b></div>\n        <div class="data-item full"><small>Количество человек</small><b>${o.team_size} чел.</b></div>\n      </div>\n      <div class="order-foot">\n        <div class="brig">${o.brigadier ? `🧑\u200d✈️ ${esc(employeeListName(o.brigadier))}` : \'⚠️ Бригадир не назначен\'}</div>\n        <div class="progress ${o.member_count===o.member_target?\'done\':\'\'}">👥 ${o.member_count}/${o.member_target}</div>\n      </div>\n    </article>`).join(\'\');\n  qsa(\'[data-delete-order]\', el).forEach(btn => btn.onclick = async (e) => {\n    e.stopPropagation();\n    await deleteOrderCompletely(Number(btn.dataset.deleteOrder), btn.dataset.displayNumber);\n  });\n  qsa(\'.order-card\', el).forEach(card => card.onclick = () => openOrder(Number(card.dataset.orderId)));\n}\n\nasync function deleteOrderCompletely(id, displayNumber=null) {\n  const shown=displayNumber || id;\n  if (!confirm(`Удалить заказ №${shown} полностью?\\n\\nЗаказ и назначенный состав будут удалены без возможности восстановления.`)) return false;\n  try {\n    await api(`/api/orders/${id}`, {method:\'DELETE\'});\n    if (state.currentOrder && Number(state.currentOrder.id) === Number(id)) closeDrawer();\n    toast(`Заказ №${shown} удалён`);\n    await loadBootstrap(state.selectedDate);\n    return true;\n  } catch(e) {\n    toast(e.message, 4000);\n    return false;\n  }\n}\n\nasync function openOrder(id) {\n  const o = await api(`/api/orders/${id}`); state.currentOrder = o;\n  const d = qs(\'#orderDrawer\'); const b = qs(\'#drawerBackdrop\');\n  d.innerHTML = orderDrawerHtml(o); d.classList.remove(\'hidden\'); b.classList.remove(\'hidden\');\n  wireOrderDrawer(o);\n}\n\nfunction orderDrawerHtml(o) {\n  const members = o.members.length ? o.members.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(employeeListName(x))}</b><small>${esc(x.group_label)}</small>${contactButtons(x)}</div><div class="person-meta">${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\') : `<div class="empty">Состав пока не выбран</div>`;\n  return `\n    <div class="handle"></div>\n    <div class="order-top"><div><h3>Заказ №${visibleOrderNumber(o)}</h3><p class="muted">${esc(o.source_label)} · ${esc(o.status_label)}</p></div><button class="close-btn" id="closeDrawer">×</button></div>\n    <div class="drawer-section data-grid">\n      <div class="data-item"><small>Дата</small><b>${fmtDate(o.work_date)}</b></div>\n      <div class="data-item"><small>Категория заказа</small><b>${esc(o.category_label)}</b></div>\n      <div class="data-item"><small>Агент</small><b>${esc(o.agent_name||\'—\')}</b></div>\n      <div class="data-item"><small>Номер агента</small><b>${esc(o.agent_phone||\'—\')}</b></div>\n      <div class="data-item"><small>Время подачи</small><b>${esc(o.arrival_time||\'—\')}</b></div>\n      <div class="data-item"><small>Время выдачи</small><b>${esc(o.issue_time||\'—\')}</b></div>\n      <div class="data-item full"><small>ФИО умершего</small><b>${esc(o.deceased_name||\'—\')}</b></div>\n      <div class="data-item full"><small>Маршрут</small><b>${esc(o.route||\'—\')}</b></div>\n      <div class="data-item full"><small>Количество человек</small><b>${o.team_size} чел.</b></div>\n      ${o.organization?`<div class="data-item full"><small>Организация</small><b>${esc(o.organization)}</b></div>`:\'\'}\n      ${o.other_agent_phone?`<div class="data-item full"><small>Номер другого агента</small><b>${esc(o.other_agent_phone)}</b></div>`:\'\'}\n      ${o.notes?`<div class="data-item full"><small>Примечание</small><b>${esc(o.notes)}</b></div>`:\'\'}\n      ${o.kickback_rub?`<div class="data-item full"><small>Дополнительно для бригадира</small><b>Откат ${o.kickback_rub} ₽</b></div>`:\'\'}\n      ${o.brigade_on_contact_at?`<div class="data-item"><small>Бригада на связи</small><b>✅ Отмечено</b></div>`:\'\'}\n      ${o.contact_time?`<div class="data-item"><small>Время связи</small><b>${esc(o.contact_time)}</b></div>`:\'\'}\n      ${o.tea_reported_at?`<div class="data-item full"><small>Отчёт по заказу · количество чая</small><b>${Number(o.tea_count||0)}</b></div>`:\'\'}\n    </div>\n\n    <div class="drawer-section"><h4>Бригадир</h4>\n      ${o.brigadier ? `<div class="person-row"><div class="person-main"><b>${esc(employeeListName(o.brigadier))}</b><small>${esc(o.brigadier.group_label)}</small>${contactButtons(o.brigadier)}</div><div class="person-meta">${o.brigadier.has_car?\'🚗\':\'\'}</div></div>` : `<div class="empty">Не назначен</div>`}\n      <button class="secondary" id="assignBrigBtn" style="width:100%;margin-top:8px">${o.brigadier?\'Сменить бригадира\':\'Назначить бригадира\'}</button>\n    </div>\n\n    <div class="drawer-section"><h4>Состав · ${o.member_count}/${o.member_target}</h4><div>${members}</div>\n      <button class="secondary" id="selectMembersBtn" style="width:100%;margin-top:8px" ${o.brigadier?\'\':\'disabled\'}>Выбрать состав по галочкам</button>\n      <button class="secondary" id="manualCompositionBtn" style="width:100%;margin-top:8px">✍️ Заполнить состав вручную</button>\n    </div>\n\n    <div class="drawer-section action-row">\n      <button class="secondary" id="editOrderBtn">✏️ Изменить заказ</button>\n      <button class="secondary danger" id="cancelOrderBtn" ${[\'cancelled\',\'closed\'].includes(o.status)?\'disabled\':\'\'}>🚫 Отменить</button>\n    </div>\n    <div class="drawer-section action-row">\n      <button class="secondary danger" id="resetCompositionBtn">Снять состав</button>\n      <button class="primary" id="finalizeBtn" ${o.composition_complete?\'\':\'disabled\'}>Состав готов</button>\n    </div>\n    <div class="drawer-section">\n      <button class="secondary" id="copyOrderBtn" style="width:100%">📋 Копировать для Telegram</button>\n      <button class="secondary" id="addOrderBtn" style="width:100%;margin-top:8px">＋ Добавить заказ</button>\n      <p class="muted" style="margin:8px 2px 0">«Копировать» — только текст для отправки бригадиру в Telegram. «Добавить заказ» — открывает пустой шаблон нового заказа без копирования данных и состава.</p>\n      <button class="secondary danger" id="deleteOrderBtn" style="width:100%;margin-top:8px">🗑 Удалить заказ полностью</button>\n    </div>\n    <p class="muted" style="margin-top:10px">Заказ отправится бригадиру только после полного состава.</p>`;\n}\n\nfunction wireOrderDrawer(o) {\n  wireContactButtons(qs(\'#orderDrawer\'));\n  qs(\'#closeDrawer\').onclick = closeDrawer;\n  qs(\'#assignBrigBtn\').onclick = ()=>openBrigadierModal(o.id);\n  qs(\'#selectMembersBtn\').onclick = ()=>openMembersModal(o.id);\n  qs(\'#manualCompositionBtn\').onclick = ()=>openManualCompositionModal(o.id);\n  qs(\'#copyOrderBtn\').onclick = async ()=>{\n    const ok=await copyText(telegramOrderText(o), false);\n    toast(ok ? \'Заказ скопирован для Telegram\' : \'Текст подготовлен для копирования\', 3000);\n  };\n  qs(\'#addOrderBtn\').onclick = ()=>{\n    closeDrawer();\n    openManualOrder();\n  };\n  qs(\'#editOrderBtn\').onclick = ()=>openEditOrderModal(o);\n  qs(\'#cancelOrderBtn\').onclick = async ()=>{\n    if ([\'cancelled\',\'closed\'].includes(o.status)) return;\n    if (!confirm(`Отменить заказ ${o.deceased_name}? Состав будет освобождён.`)) return;\n    try {await api(`/api/orders/${o.id}/cancel`,{method:\'POST\'});toast(\'Заказ отменён\');closeDrawer();await loadBootstrap(state.selectedDate)} catch(e){toast(e.message,3500)}\n  };\n  qs(\'#deleteOrderBtn\').onclick = async ()=>{ await deleteOrderCompletely(o.id); };\n  qs(\'#resetCompositionBtn\').onclick = async ()=>{\n    if (!confirm(\'Снять бригадира и весь состав с этого заказа?\')) return;\n    await api(`/api/orders/${o.id}/reset-composition`,{method:\'POST\'}); toast(\'Состав снят\'); await refreshOrder(o.id);\n  };\n  qs(\'#finalizeBtn\').onclick = async ()=>{\n    try {\n      const r = await api(`/api/orders/${o.id}/finalize`,{method:\'POST\'});\n      toast(r.telegram_sent?\'Заказ отправлен бригадиру\':\'Состав сохранён. \'+(r.warning||\'\'),3500);\n      await refreshOrder(o.id);\n    } catch(e){toast(e.message,3500)}\n  };\n}\n\nasync function refreshOrder(id) {\n  state.bootstrap = await api(`/api/bootstrap?work_date=${state.selectedDate}`); renderSummary(); renderDates();\n  await loadOrders(); await openOrder(id);\n}\n\nfunction closeDrawer(){qs(\'#orderDrawer\').classList.add(\'hidden\');qs(\'#drawerBackdrop\').classList.add(\'hidden\');state.currentOrder=null}\n\nfunction editOrderFormHtml(o){return `\n  <div class="form-grid">\n    <div class="field"><label>Дата</label><input id="editWorkDate" type="date" value="${o.work_date||\'\'}"></div>\n    <div class="field"><label>Категория заказа</label><select id="editCategory"><option value="standard" ${o.category===\'standard\'?\'selected\':\'\'}>Стандарт</option><option value="vip" ${o.category===\'vip\'?\'selected\':\'\'}>Вип</option><option value="elite" ${o.category===\'elite\'?\'selected\':\'\'}>Элит</option></select></div>\n    <div class="field"><label>Агент</label><input id="editAgentName" value="${esc(o.agent_name||\'\')}"></div>\n    <div class="field"><label>Номер агента</label><input id="editAgentPhone" value="${esc(o.agent_phone||\'\')}"></div>\n    <div class="field"><label>Время подачи</label><input id="editArrivalTime" type="time" value="${o.arrival_time||\'\'}"></div>\n    <div class="field"><label>Время выдачи</label><input id="editIssueTime" type="time" value="${o.issue_time||\'\'}"></div>\n    <div class="field full"><label>ФИО умершего</label><input id="editDeceased" value="${esc(o.deceased_name||\'\')}"></div>\n    <div class="field full"><label>Маршрут</label><textarea id="editRoute">${esc(o.route||\'\')}</textarea></div>\n    <div class="field full"><label>Количество человек</label><select id="editTeamSize"><option value="4" ${Number(o.team_size)===4?\'selected\':\'\'}>4</option><option value="6" ${Number(o.team_size)===6?\'selected\':\'\'}>6</option></select></div>\n    <div class="field"><label>Источник</label><select id="editSource"><option value="private" ${o.source===\'private\'?\'selected\':\'\'}>Частный</option><option value="gbu" ${o.source===\'gbu\'?\'selected\':\'\'}>ГБУ</option></select></div>\n    <div class="field"><label>Организация</label><input id="editOrganization" value="${esc(o.organization||\'\')}"></div>\n    <div class="field full"><label>Номер другого агента</label><input id="editOtherAgentPhone" value="${esc(o.other_agent_phone||\'\')}"></div>\n    <div class="field full"><label>Примечание</label><textarea id="editNotes">${esc(o.notes||\'\')}</textarea></div>\n    <div class="field"><label>Откат, ₽</label><input id="editKickback" inputmode="numeric" type="number" min="0" value="${Number(o.kickback_rub||0)}"></div>\n  </div>\n  <p class="muted">Если изменить дату или количество человек, текущий состав будет автоматически снят.</p>\n  <button class="primary" id="saveOrderEdit" style="width:100%">Сохранить изменения</button>`}\n\nfunction openEditOrderModal(o){\n  showModal(\'Изменить заказ\', editOrderFormHtml(o));\n  qs(\'#saveOrderEdit\').onclick=async()=>{\n    const payload={work_date:qs(\'#editWorkDate\').value,issue_time:qs(\'#editIssueTime\').value,arrival_time:qs(\'#editArrivalTime\').value||null,deceased_name:qs(\'#editDeceased\').value.trim(),route:qs(\'#editRoute\').value.trim(),category:qs(\'#editCategory\').value,team_size:Number(qs(\'#editTeamSize\').value),source:qs(\'#editSource\').value,organization:qs(\'#editOrganization\').value.trim(),agent_name:qs(\'#editAgentName\').value.trim(),agent_phone:qs(\'#editAgentPhone\').value.trim(),other_agent_phone:qs(\'#editOtherAgentPhone\').value.trim(),notes:qs(\'#editNotes\').value.trim(),kickback_rub:Number(qs(\'#editKickback\').value||0)};\n    if(!payload.work_date||!payload.issue_time||!payload.deceased_name){toast(\'Заполните дату, время и умершего\');return}\n    try{const updated=await api(`/api/orders/${o.id}`,{method:\'PATCH\',body:JSON.stringify(payload)});closeModal();toast(\'Заказ обновлён\');state.selectedDate=updated.work_date;await loadBootstrap(state.selectedDate);await openOrder(updated.id)}catch(e){toast(e.message,4000)}\n  };\n}\n\nasync function openManualCompositionModal(orderId) {\n  const data = await api(`/api/orders/${orderId}/manual-composition`);\n  if(!data.brigadiers.length){toast(\'В списке нет зарегистрированных бригадиров\');return}\n  const selected = new Set((data.member_ids||[]).map(Number));\n  const brigOptions = data.brigadiers.map(x=>`<option value="${x.id}" ${Number(data.brigadier_id)===Number(x.id)?\'selected\':\'\'}>${esc(employeeListName(x))} · ${x.workload}/2</option>`).join(\'\');\n  showModal(\'✍️ Ручное заполнение состава\', `\n    <p class="muted">Можно выбрать сотрудников вручную, даже если они не отмечали «Готов». Ограничение — не более 2 заказов на человека в день.</p>\n    <div class="field full"><label>Бригадир</label><select id="manualBrigSelect">${brigOptions}</select></div>\n    <div class="drawer-section"><h4>Сотрудники · нужно <span id="manualNeedCount">${data.target}</span></h4>\n      <input class="search" id="manualMemberSearch" placeholder="Поиск сотрудника">\n      <div class="candidate-list" id="manualMemberList"></div>\n    </div>\n    <div class="attention"><p>Выбрано: <b id="manualSelectedCount">0</b> / ${data.target}</p></div>\n    <button class="primary" id="saveManualComposition" style="width:100%">Сохранить состав</button>`);\n\n  const brigSelect=qs(\'#manualBrigSelect\');\n  const memberList=qs(\'#manualMemberList\');\n  const selectedCount=qs(\'#manualSelectedCount\');\n  const search=qs(\'#manualMemberSearch\');\n\n  function renderManualMembers(){\n    const brigId=Number(brigSelect.value);\n    if(selected.has(brigId)) selected.delete(brigId);\n    const q=(search.value||\'\').trim().toLowerCase();\n    const rows=(data.members||[]).filter(x=>Number(x.id)!==brigId && (!q || `${x.display_name||x.full_name} ${x.group_label}`.toLowerCase().includes(q)));\n    memberList.innerHTML=rows.length?rows.map(x=>`<div class="person-row ${selected.has(Number(x.id))?\'selected\':\'\'} ${x.blocked&&!selected.has(Number(x.id))?\'blocked\':\'\'}" data-manual-member="${x.id}">\n      <div class="check">✓</div><div class="person-main"><b>${esc(employeeListName(x))}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа${x.readiness_status===\'ready\'?\' · ✅ готов\':\'\'}</small>${contactButtons(x)}</div><div class="person-meta">${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\'):\'<div class="empty">Не найдено</div>\';\n    selectedCount.textContent=String(selected.size);\n    wireContactButtons(memberList);\n    qsa(\'[data-manual-member]\',memberList).forEach(row=>row.onclick=e=>{\n      if(e.target.closest(\'.contact-btn\')) return;\n      const id=Number(row.dataset.manualMember);\n      if(selected.has(id)){selected.delete(id);renderManualMembers();return}\n      if(selected.size>=Number(data.target)){toast(`Нужно выбрать ${data.target} сотрудников`);return}\n      if(row.classList.contains(\'blocked\')){toast(\'У сотрудника уже 2 заказа\');return}\n      selected.add(id);renderManualMembers();\n    });\n  }\n  brigSelect.onchange=renderManualMembers;\n  search.oninput=renderManualMembers;\n  renderManualMembers();\n\n  qs(\'#saveManualComposition\').onclick=async()=>{\n    if(selected.size!==Number(data.target)){toast(`Выберите ровно ${data.target} сотрудников`);return}\n    try{\n      await api(`/api/orders/${orderId}/manual-composition`,{method:\'POST\',body:JSON.stringify({brigadier_id:Number(brigSelect.value),member_ids:[...selected]})});\n      closeModal();toast(\'Состав сохранён вручную\');await refreshOrder(orderId);\n    }catch(e){toast(e.message,4500)}\n  };\n}\n\nasync function openBrigadierModal(orderId) {\n  const list = await api(`/api/orders/${orderId}/brigadiers`);\n  showModal(`Назначить бригадира`, `\n    <p class="muted">Показаны только бригадиры, которые отметились «готов» на эту дату.</p>\n    <div class="candidate-list">${list.length?list.map(personRowBrig).join(\'\'):`<div class="empty">Готовых бригадиров нет</div>`}</div>`);\n  wireContactButtons(qs(\'#modal\'));\n  qsa(\'[data-brig-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    if (row.classList.contains(\'blocked\')) {toast(\'У бригадира уже 2 заказа\'); return;}\n    try {await api(`/api/orders/${orderId}/brigadier`,{method:\'POST\',body:JSON.stringify({employee_id:Number(row.dataset.brigId)})}); closeModal(); toast(\'Бригадир назначен\'); await refreshOrder(orderId)} catch(e){toast(e.message,3500)}\n  });\n}\n\nfunction personRowBrig(x){return `<div class="person-row ${x.blocked?\'blocked\':\'\'}" data-brig-id="${x.id}"><div class="person-main"><b>${esc(employeeListName(x))}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small>${contactButtons(x)}</div><div class="person-meta">${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nasync function openMembersModal(orderId) {\n  const data = await api(`/api/orders/${orderId}/members/candidates`);\n  if(!data.brigadier){toast(\'Сначала назначьте бригадира\');return}\n  showModal(`Состав · нужно ${data.target}`, `\n    <p class="muted">Сверху сотрудники ±2 см от бригадира ${data.brigadier.height_cm||\'—\'} см. Можно максимум 2 заказа на человека в день.</p>\n    <input class="search" id="candidateSearch" placeholder="Поиск сотрудника">\n    <div class="candidate-list" id="candidateList">${data.candidates.map(personRowCandidate).join(\'\')}</div>`);\n  const wire = ()=>{wireContactButtons(qs(\'#modal\'));qsa(\'[data-member-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    const id=Number(row.dataset.memberId); if(row.dataset.brig===\'1\') return;\n    if(row.classList.contains(\'blocked\') && row.dataset.selected!==\'1\'){toast(\'У сотрудника уже 2 заказа\');return}\n    try{\n      if(row.dataset.selected===\'1\') await api(`/api/orders/${orderId}/members/${id}`,{method:\'DELETE\'});\n      else await api(`/api/orders/${orderId}/members`,{method:\'POST\',body:JSON.stringify({employee_id:id})});\n      const fresh=await api(`/api/orders/${orderId}/members/candidates`); qs(\'#candidateList\').innerHTML=fresh.candidates.map(personRowCandidate).join(\'\'); wire(); await loadOrders();\n    }catch(e){toast(e.message,3500)}\n  });};\n  wire();\n  qs(\'#candidateSearch\').oninput = e => {const v=e.target.value.toLowerCase(); qsa(\'[data-member-id]\',qs(\'#candidateList\')).forEach(r=>r.style.display=r.textContent.toLowerCase().includes(v)?\'flex\':\'none\')};\n}\n\nfunction personRowCandidate(x){return `<div class="person-row ${x.selected?\'selected\':\'\'} ${x.blocked&&!x.selected?\'blocked\':\'\'} ${x.height_match?\'match\':\'\'}" data-member-id="${x.id}" data-selected="${x.selected?\'1\':\'0\'}" data-brig="${x.is_brigadier?\'1\':\'0\'}">\n  <div class="check">✓</div><div class="person-main"><b>${esc(employeeListName(x))} ${x.is_brigadier?\'(бригадир)\':\'\'}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small>${contactButtons(x)}</div><div class="person-meta">${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nfunction showModal(title, body) {qs(\'#modal\').innerHTML=`<div class="modal-head"><h3>${esc(title)}</h3><button id="closeModal" class="close-btn">×</button></div>${body}`;qs(\'#modal\').classList.remove(\'hidden\');qs(\'#modalBackdrop\').classList.remove(\'hidden\');qs(\'#closeModal\').onclick=closeModal}\nlet cashRefreshTimer=null;\nfunction closeModal(){if(cashRefreshTimer){clearInterval(cashRefreshTimer);cashRefreshTimer=null}qs(\'#modal\').classList.add(\'hidden\');qs(\'#modalBackdrop\').classList.add(\'hidden\')}\n\nfunction readinessPersonRows(people,editable=false){\n  const rows=(people||[]).slice().sort((a,b)=>\n    String(a.display_name||a.full_name||\'\').localeCompare(\n      String(b.display_name||b.full_name||\'\'),\'ru\',{sensitivity:\'base\'}\n    )\n  );\n  if(!rows.length) return \'<div class="empty">В этой категории сотрудников нет</div>\';\n  return `<div class="list">${rows.map((x,i)=>`\n    <div class="employee-row">\n      <div class="name">\n        <b>${i+1}. ${esc(employeeListName(x))}</b>\n      </div>\n      \n      <div class="car">${x.has_car?\'🚗\':\'\'}</div>\n      ${editable?`<button type="button" class="secondary" data-subgroup-edit="${i}" aria-label="Изменить статус сотрудника" style="flex-shrink:0;min-width:44px;min-height:44px;padding:6px;font-size:22px">✍🏻</button>`:\'\'}\n    </div>\n  `).join(\'\')}</div>`;\n}\n\nfunction openReadinessPeople(groupLabel,statusLabel,people,code,status,workDate){\n  const rows=(people||[]).slice().sort((a,b)=>String(a.display_name||a.full_name||\'\').localeCompare(String(b.display_name||b.full_name||\'\'),\'ru\',{sensitivity:\'base\'}));\n  showModal(\n    `${groupLabel} · ${statusLabel}`,\n    `<div class="attention"><h4>Всего: ${rows.length}</h4><p>Список сотрудников на выбранную дату.</p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button id="manualReadyAdd" class="secondary" style="font-size:12px;padding:8px 10px;min-height:40px" aria-label="Добавить сотрудника вручную">✍️ Добавить</button></div></div>${readinessPersonRows(rows,true)}`\n  );\n  qs(\'#manualReadyAdd\').onclick=()=>openManualReadiness(code,status,groupLabel,statusLabel,workDate);\n  const reopen=async()=>{\n    const summary=await api(`/api/readiness/summary?work_date=${workDate}`);\n    const group=summary.groups?.[code]||{};\n    const fresh=status===\'wrote\'?[\'ready\',\'responded\',\'not_ready\',\'day_off\'].flatMap(key=>group[key]||[]):group[status]||[];\n    openReadinessPeople(group.label||groupLabel,statusLabel,fresh,code,status,workDate);\n  };\n  qsa(\'[data-subgroup-edit]\',qs(\'#modal\')).forEach(button=>button.onclick=()=>{\n    const employee=rows[Number(button.dataset.subgroupEdit)];\n    openManualReadiness(code,employee.readiness_status||(status===\'wrote\'?\'responded\':status),groupLabel,statusLabel,workDate,employee,reopen);\n  });\n\n}\n\nfunction chooseReadinessEmployee(rows,code,status,groupLabel,statusLabel,workDate){\n  const people=rows.slice().sort((a,b)=>String(a.display_name||a.full_name).localeCompare(String(b.display_name||b.full_name),\'ru\'));\n  showModal(\'Выберите сотрудника\', `<p class="muted">${esc(groupLabel)} · ${esc(statusLabel)} · ${esc(workDate)}</p><div style="display:grid;gap:8px;margin-top:12px">${people.map((x,i)=>`<button class="secondary" style="text-align:left;min-height:48px" data-ready-edit="${i}"><b>${esc(employeeListName(x))}</b></button>`).join(\'\')}</div><button id="readyEditBack" class="secondary" style="margin-top:12px">Назад</button>`);\n  qsa(\'[data-ready-edit]\',qs(\'#modal\')).forEach(button=>button.onclick=()=>{\n    const employee=people[Number(button.dataset.readyEdit)];\n    openManualReadiness(code,employee.readiness_status||status,groupLabel,statusLabel,workDate,employee);\n  });\n  qs(\'#readyEditBack\').onclick=()=>openReadinessPeople(groupLabel,statusLabel,rows,code,status,workDate);\n}\n\nfunction captureEmployeeScroll(){\n  return {page:window.scrollY,modal:qs(\'#modal\').scrollTop};\n}\nfunction restoreEmployeeScroll(position){\n  requestAnimationFrame(()=>{window.scrollTo(0,position.page);qs(\'#modal\').scrollTop=position.modal;});\n}\nfunction openEmployeeProfile(employee,onSaved){\n  const position=captureEmployeeScroll();\n  showModal(\'✍🏻 Изменить данные сотрудника\',`<form id="employeeProfileForm">\n    <div class="field full"><label>Имя</label><input id="profileName" required maxlength="120"></div>\n    <div class="field full"><label>Метро</label><input id="profileMetro" maxlength="100"></div>\n    <div class="field full"><label>Рост, см</label><input id="profileHeight" type="number" min="100" max="250" step="1" inputmode="numeric"></div>\n    <div class="field full"><label>Телефон</label><input id="profilePhone" type="tel" inputmode="tel" maxlength="40" placeholder="+7 999 123-45-67"></div>\n    <p class="muted">Данные сохранятся в общем списке и во всех списках готовности.</p>\n    <button type="submit" class="primary">Сохранить</button></form>`);\n  qs(\'#profileName\').value=employee.full_name||employee.display_name||\'\';\n  qs(\'#profileMetro\').value=employee.metro||\'\';\n  qs(\'#profileHeight\').value=employee.height_cm||\'\';\n  qs(\'#profilePhone\').value=employee.phone||\'\';\n  qs(\'#employeeProfileForm\').onsubmit=async event=>{\n    event.preventDefault();const button=event.target.querySelector(\'button\');button.disabled=true;\n    try{\n      await api(`/api/employees/${employee.id}/profile`,{method:\'PATCH\',body:JSON.stringify({phone:qs(\'#profilePhone\').value.trim(),full_name:qs(\'#profileName\').value.trim(),metro:qs(\'#profileMetro\').value.trim(),height_cm:qs(\'#profileHeight\').value?Number(qs(\'#profileHeight\').value):null})});\n      closeModal();await loadReadiness();await onSaved();restoreEmployeeScroll(position);toast(\'Данные сохранены\');\n    }catch(error){toast(error.message,4500);button.disabled=false;}\n  };\n}\n\nfunction openManualReadiness(code,status,groupLabel,statusLabel,workDate,employee=null,onSaved=null){\n  const position=captureEmployeeScroll();\n  const options={ready:\'Готов\',not_ready:\'На основной\',day_off:\'Выходной\'};\n  if(employee) options.no_response=\'Не отписался\';\n  showModal(`${employee?\'✏️ Изменить сотрудника\':\'✍️ \'+groupLabel}`, `\n    <p class="muted">Дата: ${esc(workDate)}. Статус учитывается на эту дату. Имя, метро и рост сохраняются во всех списках.</p>\n    <form id="manualReadinessForm">\n      <div class="field full"><label for="manualReadyName">Имя сотрудника</label><input id="manualReadyName" required maxlength="120" autocomplete="off" placeholder="Иван"></div>\n      <div class="field full"><label for="manualReadyMetro">Метро</label><input id="manualReadyMetro" required maxlength="100" placeholder="Марьино"></div>\n      <div class="field full"><label for="manualReadyHeight">Рост, см</label><input id="manualReadyHeight" required type="number" inputmode="numeric" min="100" max="250" step="1" placeholder="185"></div>\n      <div class="field full"><label for="manualReadyPhone">Телефон</label><input id="manualReadyPhone" type="tel" inputmode="tel" maxlength="40" placeholder="+7 999 123-45-67"></div>\n      <div class="field full"><label for="manualReadyStatus">Статус</label><select id="manualReadyStatus" required>${!Object.prototype.hasOwnProperty.call(options,status)?\'<option value="" disabled selected>Выберите статус</option>\':\'\'}${Object.entries(options).map(([key,label])=>`<option value="${key}" ${key===status?\'selected\':\'\'}>${label}</option>`).join(\'\')}</select></div>\n      <button class="primary" style="width:100%;min-height:48px" type="submit">${employee?\'Сохранить изменения\':\'Добавить в подсчёт\'}</button>\n      <p class="muted">Исправленные данные сохранятся в общей карточке сотрудника.</p>\n    </form>`);\n  if(employee){\n    qs(\'#manualReadyName\').value=employee.full_name||employee.display_name||\'\';\n    qs(\'#manualReadyMetro\').value=employee.metro||\'\';\n    qs(\'#manualReadyHeight\').value=employee.height_cm||\'\';\n    qs(\'#manualReadyPhone\').value=employee.phone||\'\';\n  }\n  qs(\'#manualReadinessForm\').onsubmit=async e=>{\n    e.preventDefault();const button=e.target.querySelector(\'button[type="submit"]\');button.disabled=true;\n    try{\n      const path=employee?(employee.manual_entry_id?`/api/readiness/manual/${employee.manual_entry_id}`:`/api/readiness/employee/${employee.id}`):\'/api/readiness/manual\';\n      await api(path,{method:employee?\'PATCH\':\'POST\',body:JSON.stringify({\n        work_date:workDate,group_code:code,status:qs(\'#manualReadyStatus\').value,\n        full_name:qs(\'#manualReadyName\').value.trim(),metro:qs(\'#manualReadyMetro\').value.trim(),\n        height_cm:Number(qs(\'#manualReadyHeight\').value),phone:qs(\'#manualReadyPhone\').value.trim()\n      })});\n      closeModal();await loadReadiness();if(onSaved) await onSaved();restoreEmployeeScroll(position);toast(employee?\'Изменения сохранены\':\'Сотрудник учтён\');\n    }catch(e){toast(e.message,4500);button.disabled=false;}\n  };\n}\n\nfunction chooseUnansweredEmployee(groups,day){\n  const choices=[];\n  const sections=groups.filter(g=>g.people.length).map(g=>`<section class="drawer-section"><h4>${esc(g.label)}</h4><div style="display:grid;gap:8px">${g.people.map(employee=>{\n    const index=choices.push({employee,code:g.code,label:g.label})-1;\n    return `<button class="secondary" style="text-align:left;min-height:48px" data-unanswered-edit="${index}"><b>${esc(employeeListName(employee))}</b></button>`;\n  }).join(\'\')}</div></section>`).join(\'\');\n  showModal(\'Выберите сотрудника\',`<p class="muted">Не отписались на ${esc(day)}</p>${sections}<button id="unansweredEditBack" class="secondary">Назад</button>`);\n  qsa(\'[data-unanswered-edit]\',qs(\'#modal\')).forEach(button=>button.onclick=()=>{\n    const {employee,code,label}=choices[Number(button.dataset.unansweredEdit)];\n    openManualReadiness(code,\'no_response\',label,\'Не отписались\',day,employee,()=>openReadinessGroupedList(\'no_response\',day));\n  });\n  qs(\'#unansweredEditBack\').onclick=()=>openReadinessGroupedList(\'no_response\',day);\n}\n\nasync function openReadinessGroupedList(mode,day=state.selectedDate){\n  try{\n    const r=await api(`/api/readiness/summary?work_date=${day}`);\n    const employees=mode===\'employees\'?await api(\'/api/employees\'):[];\n    const statusNames={ready:\'Готов\',not_ready:\'На основной\',day_off:\'Выходной\',responded:\'Отписался / свободен\',no_response:\'Не отписался\'};\n    const groupNames={brigadier:\'Бригадиры\',main:\'Основной состав\',cashless:\'Безнал состав\',reserve:\'Резерв\'};\n    const readinessById=new Map(Object.values(r.groups||{}).flatMap(g=>Object.keys(statusNames).flatMap(status=>(g[status]||[]).map(x=>[x.id,{...x,readiness_status:status}]))));\n    let total=0;\n    const editGroups=[];\n    const sections=Object.entries(groupNames).map(([code,label])=>{\n      const g=r?.groups?.[code]||{};\n      const people=mode===\'employees\'?employees.filter(x=>x.group_code===code).map(x=>{const current=readinessById.get(x.id);return {...x,...current,responseLabel:statusNames[current?.readiness_status||\'no_response\']};}):(g[mode]||[]).map(x=>({...x,responseLabel:statusNames[mode]}));\n      people.sort((a,b)=>String(a.display_name||a.full_name||\'\').localeCompare(String(b.display_name||b.full_name||\'\'),\'ru\',{sensitivity:\'base\'}));\n      total+=people.length;\n      editGroups.push({code,label:g.label||label,people});\n      return `<section class="drawer-section"><h4>${esc(g.label||label)} — ${people.length}</h4>${people.length?`<div class="list">${people.map((x,i)=>`<div class="employee-row"><div class="name"><b>${i+1}. ${esc(employeeListName(x))}</b><small>${esc(x.responseLabel)}</small></div><div class="car">${x.has_car?\'🚗\':\'\'}</div><button type="button" class="secondary" data-person-status="${code}:${i}" aria-label="Изменить статус ${esc(x.display_name||x.full_name||\'сотрудника\')}" style="flex-shrink:0;min-width:44px;min-height:44px;padding:6px;font-size:22px">✍🏻</button></div>`).join(\'\')}</div>`:\'<p class="muted">В этой группе список пуст</p>\'}</section>`;\n    }).join(\'\');\n    showModal(mode===\'employees\'?\'Все сотрудники\':mode===\'ready\'?\'Готовые сотрудники\':\'Не отписались\',`<div class="attention"><h4>Всего: ${total}</h4><p>${mode===\'employees\'?\'Действующие сотрудники из базы. Статус на \'+esc(day)+\'.\':\'На \'+esc(day)+\'.\'}</p></div>${sections}`);\n    qsa(\'[data-person-status]\',qs(\'#modal\')).forEach(button=>button.onclick=()=>{\n      const [code,index]=button.dataset.personStatus.split(\':\');\n      const group=editGroups.find(g=>g.code===code);\n      const employee=group.people[Number(index)];\n      if(mode===\'employees\'){openEmployeeProfile(employee,()=>openReadinessGroupedList(mode,day));return;}\n      openManualReadiness(employee.group_code||code,employee.readiness_status||(mode===\'ready\'?\'ready\':\'no_response\'),group.label,statusNames[employee.readiness_status]||\'\',day,employee,()=>openReadinessGroupedList(mode,day));\n    });\n  }catch(e){toast(e.message,4000);}\n}\n\nasync function loadReadiness(){\n  const r=await api(`/api/readiness/summary?work_date=${state.selectedDate}`);\n\n  const groupEntries=Object.entries(r.groups||{});\n  const currentNoResponse=groupEntries.reduce((sum,[,g])=>sum+Number(g?.counts?.no_response||0),0);\n  const respondedNow=groupEntries.reduce((sum,[,g])=>\n    sum\n    + Number(g?.counts?.ready||0)\n    + Number(g?.counts?.not_ready||0)\n    + Number(g?.counts?.day_off||0)\n    + Number(g?.counts?.responded||0)\n  ,0);\n\n  qs(\'#readinessSummary\').innerHTML=`\n    <button id="brigadesContactCount" class="metric good" style="text-align:left;color:var(--text)"><b>${r.totals.brigades_on_contact||0}</b><span>Связь бригад</span></button>\n    <button id="allRespondedCount" class="metric" style="text-align:left;color:var(--text)"><b>${r.totals.ready_now}</b><span>Готовы</span></button>\n    <button id="noResponseCount" class="metric ${currentNoResponse?\'warn\':\'\'}" style="text-align:left;color:var(--text)"><b>${currentNoResponse}</b><span>Не отписались</span></button>`;\n\n  qs(\'#brigadesContactCount\').onclick=openContactBrigades;\n  qs(\'#allRespondedCount\').onclick=()=>openReadinessGroupedList(\'ready\');\n  qs(\'#noResponseCount\').onclick=()=>openReadinessGroupedList(\'no_response\');\n  qs(\'#readinessGroups\').innerHTML=groupEntries.map(([code,g])=>{\n    const ready=Number(g.counts.ready||0);\n    const mainJob=Number(g.counts.not_ready||0);\n    const dayOff=Number(g.counts.day_off||0);\n    const free=Number(g.counts.responded||0);\n    const noResponse=Number(g.counts.no_response||0);\n    const answered=ready+mainJob+dayOff+free;\n    const total=answered+noResponse;\n\n    if(code===\'reserve\'){\n      const wrotePeople=[\n        ...(g.ready||[]),\n        ...(g.responded||[]),\n        ...(g.not_ready||[]),\n        ...(g.day_off||[])\n      ];\n      return `<div class="group-block">\n        <div class="group-title"><span>${esc(g.label)}</span><span>${answered}/${total}</span></div>\n        <div class="group-stats">\n          <button class="${wrotePeople.length?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="wrote"><b>${wrotePeople.length}</b>Отписались</button>\n          <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готовы</button>\n          <button class="${free?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="responded"><b>${free}</b>Свободная</button>\n          <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n        </div>\n      </div>`;\n    }\n\n    return `<div class="group-block">\n      <div class="group-title"><span>${esc(g.label)}</span><span>${answered}/${total}</span></div>\n      <div class="group-stats">\n        <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готов</button>\n        <button class="${mainJob?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="not_ready"><b>${mainJob}</b>На основной</button>\n        <button class="${dayOff?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="day_off"><b>${dayOff}</b>Выходной</button>\n        <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n      </div>\n    </div>`;\n  }).join(\'\');\n\n  qsa(\'[data-rgroup]\',qs(\'#readinessGroups\')).forEach(btn=>{\n    btn.onclick=()=>{\n      const code=btn.dataset.rgroup;\n      const status=btn.dataset.rstatus;\n      const g=r.groups?.[code]||{};\n      let people=[];\n      let label=\'\';\n\n      if(status===\'ready\'){people=g.ready||[];label=\'Готов\';}\n      else if(status===\'not_ready\'){people=g.not_ready||[];label=\'На основной\';}\n      else if(status===\'day_off\'){people=g.day_off||[];label=\'Выходной\';}\n      else if(status===\'responded\'){people=g.responded||[];label=\'Свободная\';}\n      else if(status===\'no_response\'){people=g.no_response||[];label=\'Не отписались\';}\n      else if(status===\'wrote\'){\n        people=[\n          ...(g.ready||[]),\n          ...(g.responded||[]),\n          ...(g.not_ready||[]),\n          ...(g.day_off||[])\n        ];\n        label=\'Отписались\';\n      }\n\n      openReadinessPeople(g.label||code,label,people,code,status,state.selectedDate);\n    };\n  });\n}\n\nfunction employeeListName(employee){\n  let name=String(employee.display_name||employee.full_name||\'—\').trim();\n  const metro=String(employee.metro||\'\').trim();\n  const hasHeight=Object.prototype.hasOwnProperty.call(employee,\'height_cm\');\n  if(hasHeight) name=name.replace(/\\s+[12]\\d{2}(?:\\s*см)?\\s*$/i,\'\').trim();\n  const normalized=value=>value.toLocaleLowerCase(\'ru\').replace(/ё/g,\'е\').replace(/[^а-яa-z0-9]+/g,\' \').trim();\n  if(metro&&!(` ${normalized(name)} `).includes(` ${normalized(metro)} `)) name+=` ${metro}`;\n  return hasHeight?`${name} ${Number(employee.height_cm)||\'—\'} см`:name;\n}\n\nasync function loadEmployees(){\n  const list = await api(\'/api/employees\');\n  const counts = {all:list.length,brigadier:0,main:0,cashless:0,reserve:0};\n  for(const employee of list){\n    if(employee.group_code !== \'all\' && Object.hasOwn(counts,employee.group_code)) counts[employee.group_code]++;\n  }\n  qsa(\'[data-employee-count]\').forEach(el=>{\n    el.textContent=String(counts[el.dataset.employeeCount] ?? 0);\n  });\n\n  const v=(state.employeeSearch||\'\').trim().toLowerCase();\n  const ownerTgId=Number(state.bootstrap?.user?.id||0);\n\n  const filtered=list\n    .filter(x=>state.employeeGroup===\'all\'||x.group_code===state.employeeGroup)\n    .filter(x=>{\n      if(!v) return true;\n      const haystack=[\n        x.full_name,\n        x.display_name,\n        x.metro,\n        x.height_cm,\n      ].filter(Boolean).join(\' \').toLowerCase();\n      return haystack.includes(v);\n    })\n    .sort((a,b)=>{\n      const aOwner=ownerTgId && Number(a.tg_id||0)===ownerTgId;\n      const bOwner=ownerTgId && Number(b.tg_id||0)===ownerTgId;\n      if(aOwner!==bOwner) return aOwner ? -1 : 1;\n      return String(a.display_name||a.full_name||\'\').localeCompare(\n        String(b.display_name||b.full_name||\'\'),\n        \'ru\',\n        {sensitivity:\'base\'}\n      );\n    });\n\n  qs(\'#employeesList\').innerHTML=filtered.length\n    ? filtered.map((x,i)=>`\n      <div class="employee-row">\n        <div class="name">\n          <b>${i+1}. ${esc(employeeListName(x))}</b>\n          <small>${esc(x.group_label)}${x.telegram_username?` · @${esc(x.telegram_username)}`:\'\'}</small>\n          ${contactButtons(x)}\n        </div>\n        \n        <div class="car">${x.has_car?\'🚗\':\'\'}</div><button class="secondary" data-profile-edit="${i}" aria-label="Изменить данные сотрудника" style="flex-shrink:0;min-width:44px;min-height:44px;padding:6px;font-size:22px">✍🏻</button>\n      </div>\n    `).join(\'\')\n    : `<div class="empty">Ничего не найдено</div>`;\n\n  wireContactButtons(qs(\'#employeesList\'));\n  qsa(\'[data-profile-edit]\',qs(\'#employeesList\')).forEach(button=>button.onclick=()=>openEmployeeProfile(filtered[Number(button.dataset.profileEdit)],loadEmployees));\n}\n\nasync function loadControl(){\n  const c=await api(`/api/control?work_date=${state.selectedDate}`); const no=c.no_response_cutoff; const incomplete=c.orders.not_complete;\n  qs(\'#controlContent\').innerHTML=`\n    <div class="attention ${incomplete.length?\'red\':\'\'}"><h4>Не укомплектованы · ${incomplete.length}</h4><div class="attention-list">${incomplete.length?incomplete.map(o=>`<div>${o.issue_time} · ${esc(o.deceased_name)} · ${o.brigadier?\'состав \'+o.member_count+\'/\'+o.member_target:\'без бригадира\'}</div>`).join(\'\'):\'Все заказы укомплектованы\'}</div></div>\n    <div class="attention ${no.length?\'red\':\'\'}"><h4>Не отписались к 15:30 · ${no.length}</h4><div class="attention-list">${no.length?no.map(x=>`<div>${esc(employeeListName(x))} · ${esc(x.group_label)}</div>`).join(\'\'):\'Все сотрудники отписались\'}</div></div>\n    <div class="attention"><h4>Нагрузка</h4><div class="attention-list">${c.workload.length?c.workload.map(x=>`<div>${esc(employeeListName(x))} · ${x.workload}/2</div>`).join(\'\'):\'Назначений пока нет\'}</div></div>`;\n}\n\n\nasync function openReadinessHistory(){\n  try{\n    const rows=await api(`/api/readiness/history?work_date=${state.selectedDate}&limit=60`);\n    showModal(\'🧾 История готовности\', `\n      <p class="muted">Все изменения статусов сохраняются в базе. Удаление старого сообщения в Telegram не удаляет отметку готовности.</p>\n      <div class="list">${rows.length?rows.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(employeeListName(x))}</b><small>${esc(x.group_label)} · ${esc(x.changed_at_label)}</small></div><div class="person-meta">${esc(x.old_label)} → <b>${esc(x.new_label)}</b></div></div>`).join(\'\'):\'<div class="empty">Изменений пока нет</div>\'}</div>`);\n  }catch(e){toast(e.message,4000)}\n}\n\nasync function sendReadinessToChats(kind,button){\n  button.disabled=true;\n  try{\n    const result=await api(`/api/readiness/chat/${kind}`,{method:\'POST\'});\n    const text=result.empty?\'Все отписались — уведомления не нужны.\':`На ${result.work_date}: отправлено в чатов: ${result.sent_chats}`;\n    toast(text+(result.failed_chats?`. Не удалось отправить: ${result.failed_chats}. Проверьте права бота.`:\'\'),6500);\n  }catch(error){toast(error.message,6000);}\n  finally{button.disabled=false;}\n}\n\nasync function refreshReadinessCount(){\n  try{await loadReadiness();toast(\'Количество обновлено\')}catch(e){toast(e.message,4000)}\n}\n\nasync function openEmployeeImport(){\n  showModal(\'Импорт сотрудников\', `\n    <p class="muted">Одна строка — один сотрудник. Формат: ФИО; группа; рост; авто; телефон; @telegram; Telegram ID</p>\n    <p class="muted">Группа: бригадиры / основной / безнал / резерв. Телефон, @telegram и Telegram ID можно оставить пустыми.</p>\n    <div class="field"><label>Список</label><textarea id="employeeImportText" placeholder="Иван Иванов; основной; 186; авто; +79990000000; @ivanov; 123456789\nПетр Петров; резерв; 184; ; ; ;"></textarea></div>\n    <button class="primary" id="employeeImportSave" style="width:100%">Импортировать</button>`);\n  qs(\'#employeeImportSave\').onclick=async()=>{\n    const raw=qs(\'#employeeImportText\').value.trim(); if(!raw){toast(\'Вставьте список\');return}\n    const groupMap={\'бригадиры\':\'brigadier\',\'бригадир\':\'brigadier\',\'основной\':\'main\',\'основной состав\':\'main\',\'безнал\':\'cashless\',\'безнал состав\':\'cashless\',\'резерв\':\'reserve\'};\n    const items=[];\n    for(const line of raw.split(/\\n+/)){\n      const p=line.split(\';\').map(x=>x.trim()); if(!p[0]) continue; const group=groupMap[(p[1]||\'\').toLowerCase()]; if(!group){toast(`Не понял группу: ${p[1]||\'—\'}`,3500);return}\n      items.push({full_name:p[0],group_code:group,height_cm:p[2]?Number(p[2]):null,has_car:/авто|да|yes|\\+/.test((p[3]||\'\').toLowerCase()),phone:p[4]||\'\',telegram_username:(p[5]||\'\').replace(\'@\',\'\'),tg_id:p[6]?Number(p[6]):null,active:true});\n    }\n    try{await api(\'/api/employees/bulk\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Импортировано: ${items.length}`);await loadEmployees()}catch(e){toast(e.message,4000)}\n  };\n}\n\nfunction manualOrderFormHtml(){\n  return `\n    <p class="muted">Номер присвоится автоматически после сохранения и останется постоянным.</p>\n    <div class="form-grid">\n      <div class="field"><label>Тип заказа</label><select id="manualSource"><option value="private">Частный</option><option value="gbu">ГБУ</option></select></div>\n      <div class="field"><label>Дата</label><input id="manualWorkDate" type="date" value="${state.selectedDate}"></div>\n      <div class="field full"><label>Категория заказа</label><select id="manualCategory"><option value="standard">Стандарт</option><option value="vip">Вип</option><option value="elite">Элит</option></select></div>\n      <div class="field"><label>Агент</label><input id="manualAgentName" autocomplete="off" placeholder="ФИО агента"></div>\n      <div class="field"><label>Номер агента</label><input id="manualAgentPhone" inputmode="tel" autocomplete="tel" placeholder="+7 ..."></div>\n      <div class="field"><label>Время подачи</label><input id="manualArrivalTime" type="time"></div>\n      <div class="field"><label>Время выдачи</label><input id="manualIssueTime" type="time"></div>\n      <div class="field full"><label>ФИО умершего</label><input id="manualDeceasedName" autocomplete="off"></div>\n      <div class="field full"><label>Маршрут</label><textarea id="manualRoute" placeholder="Морг → ... → конечная точка"></textarea></div>\n      <div class="field full"><label>Количество человек</label><select id="manualTeamSize"><option value="4">4 человека</option><option value="6">6 человек</option></select></div>\n    </div>\n    <button class="primary" id="saveManualOrderBtn" style="width:100%;margin-top:12px">Сохранить новый заказ</button>`;\n}\n\nasync function openManualOrder(){\n  showModal(\'＋ Добавить заказ\', manualOrderFormHtml());\n  const source = qs(\'#manualSource\');\n  const category = qs(\'#manualCategory\');\n  const syncSourceRules = ()=>{\n    if(source.value===\'gbu\'){\n      category.value=\'standard\';\n      category.disabled=true;\n    }else{\n      category.disabled=false;\n    }\n  };\n  source.onchange=syncSourceRules;\n  syncSourceRules();\n  qs(\'#saveManualOrderBtn\').onclick=saveManualOrder;\n}\n\nasync function saveManualOrder(){\n  const get=id=>qs(id)?.value?.trim()||\'\';\n  const source=get(\'#manualSource\')||\'private\';\n  const payload={\n    source,\n    work_date:get(\'#manualWorkDate\'),\n    category:source===\'gbu\'?\'standard\':get(\'#manualCategory\'),\n    agent_name:get(\'#manualAgentName\'),\n    agent_phone:get(\'#manualAgentPhone\'),\n    arrival_time:get(\'#manualArrivalTime\')||null,\n    issue_time:get(\'#manualIssueTime\'),\n    deceased_name:get(\'#manualDeceasedName\'),\n    route:get(\'#manualRoute\'),\n    team_size:Number(get(\'#manualTeamSize\')||4),\n    organization:source===\'gbu\'?\'ГБУ\':\'\',\n    notes:\'\'\n  };\n  const missing=[];\n  if(!payload.work_date) missing.push(\'дата\');\n  if(!payload.agent_name) missing.push(\'агент\');\n  if(!payload.agent_phone) missing.push(\'номер агента\');\n  if(!payload.arrival_time) missing.push(\'время подачи\');\n  if(!payload.issue_time) missing.push(\'время выдачи\');\n  if(!payload.deceased_name) missing.push(\'ФИО умершего\');\n  if(!payload.route) missing.push(\'маршрут\');\n  if(missing.length){toast(\'Заполните: \'+missing.join(\', \'),4500);return}\n  const btn=qs(\'#saveManualOrderBtn\');\n  btn.disabled=true;btn.textContent=\'Сохраняю…\';\n  try{\n    const created=await api(\'/api/orders\',{method:\'POST\',body:JSON.stringify(payload)});\n    closeModal();\n    state.selectedDate=created.work_date||payload.work_date;\n    toast(`Создан заказ №${created.id}`);\n    await loadBootstrap(state.selectedDate);\n  }catch(e){\n    toast(e.message,4500);\n    btn.disabled=false;btn.textContent=\'Сохранить новый заказ\';\n  }\n}\n\nasync function openGbuImport(){\n  showModal(\'Импорт ГБУ\', `\n    <p class="muted">Первые два столбца игнорируются. Время в таблице — <b>подача</b>; выдача автоматически +30 минут. 4 и 6 человек — категория Стандарт.</p>\n    <div class="field"><label>Дата заказов</label><input type="date" id="gbuWorkDate" value="${state.selectedDate}"></div>\n    <div class="field"><label>Фото таблицы</label><input type="file" id="gbuFile" accept="image/*"></div>\n    <button class="primary" id="ocrBtn" style="width:100%">Распознать фото</button>\n    <div id="gbuResult" style="margin-top:12px"></div>`);\n  qs(\'#ocrBtn\').onclick=async()=>{\n    const f=qs(\'#gbuFile\').files[0]; if(!f){toast(\'Выберите фото\');return}\n    const workDate=qs(\'#gbuWorkDate\').value; if(!workDate){toast(\'Укажите дату\');return}\n    const fd=new FormData();fd.append(\'file\',f);fd.append(\'work_date\',workDate); qs(\'#ocrBtn\').disabled=true; qs(\'#ocrBtn\').textContent=\'Распознаю…\';\n    try{const r=await api(\'/api/gbu/ocr\',{method:\'POST\',body:fd}); renderGbuDrafts(r.drafts,r.raw_text,r.warning)}catch(e){toast(e.message,5000)}finally{qs(\'#ocrBtn\').disabled=false;qs(\'#ocrBtn\').textContent=\'Распознать фото\'}\n  };\n}\n\nfunction renderGbuDrafts(drafts, raw, warning=\'\'){\n  const el=qs(\'#gbuResult\'); const rows=(drafts||[]).map((d,i)=>gbuDraftHtml(d,i)).join(\'\');\n  el.innerHTML=`${warning?`<div class="attention"><p>${esc(warning)}</p></div>`:\'\'}<details><summary class="muted">Показать распознанный текст</summary><div class="field"><textarea readonly>${esc(raw)}</textarea></div></details><h4>Черновики заказов</h4><div id="gbuDrafts">${rows||\'<div class="empty">Строки автоматически не найдены. Добавьте заказ вручную.</div>\'}</div><button class="secondary" id="addGbuRow" style="width:100%;margin-bottom:8px">＋ Добавить строку</button><button class="primary" id="saveGbuDrafts" style="width:100%">Сохранить заказы ГБУ</button>`;\n  qs(\'#addGbuRow\').onclick=()=>{const c=qs(\'#gbuDrafts\');const i=qsa(\'.gbu-draft\',c).length;c.insertAdjacentHTML(\'beforeend\',gbuDraftHtml({work_date:state.selectedDate,arrival_time:\'\',issue_time:\'\',deceased_name:\'\',route:\'\',team_size:4,category:\'standard\',agent_name:\'\',agent_phone:\'\'},i))};\n  qs(\'#saveGbuDrafts\').onclick=saveGbuDrafts;\n}\n\nfunction gbuDraftHtml(d,i){return `<div class="gbu-draft" data-i="${i}"><div class="form-grid"><div class="field"><label>Дата</label><input data-k="work_date" type="date" value="${d.work_date||state.selectedDate}"></div><div class="field"><label>Категория заказа</label><input value="Стандарт" disabled></div><div class="field"><label>Агент</label><input data-k="agent_name" value="${esc(d.agent_name||\'\')}"></div><div class="field"><label>Номер агента</label><input data-k="agent_phone" value="${esc(d.agent_phone||\'\')}"></div><div class="field"><label>Время подачи</label><input data-k="arrival_time" type="time" value="${d.arrival_time||\'\'}"></div><div class="field"><label>Время выдачи</label><input data-k="issue_time" type="time" value="${d.issue_time||\'\'}"></div><div class="field full"><label>ФИО умершего</label><input data-k="deceased_name" value="${esc(d.deceased_name||\'\')}"></div><div class="field full"><label>Маршрут</label><textarea data-k="route">${esc(d.route||\'\')}</textarea></div><div class="field full"><label>Количество человек</label><select data-k="team_size"><option value="4" ${Number(d.team_size)!==6?\'selected\':\'\'}>4</option><option value="6" ${Number(d.team_size)===6?\'selected\':\'\'}>6</option></select></div>${d.agent_match_confidence!==undefined?`<div class="field full"><label>Совпадение со справочником</label><div class="muted">${d.agent_phone?\'✅ номер найден\':\'⚠️ номер не найден — проверьте вручную\'}</div></div>`:\'\'}</div></div>`}\n\nasync function saveGbuDrafts(){\n  const items=qsa(\'.gbu-draft\').map(row=>{const get=k=>qs(`[data-k="${k}"]`,row).value;return {work_date:get(\'work_date\'),arrival_time:get(\'arrival_time\')||null,issue_time:get(\'issue_time\'),deceased_name:get(\'deceased_name\'),route:get(\'route\'),category:\'standard\',team_size:Number(get(\'team_size\')),organization:\'ГБУ\',agent_name:get(\'agent_name\'),agent_phone:get(\'agent_phone\'),notes:\'\'}}).filter(x=>x.work_date&&x.issue_time&&x.deceased_name);\n  if(!items.length){toast(\'Нет заполненных строк\');return}\n  try{await api(\'/api/gbu/drafts\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Добавлено: ${items.length}`);await loadBootstrap(state.selectedDate)}catch(e){toast(e.message,4000)}\n}\n\nasync function openGbuAgents(){\n  showModal(\'⬛ Агенты ГБУ\', `<p class="muted">Справочник номеров из загруженного списка ГБУ. Нажмите номер, чтобы скопировать.</p><input class="search" id="gbuAgentSearch" placeholder="ФИО или номер"><div id="gbuAgentsList" class="list"></div>`);\n  async function draw(){\n    const q=encodeURIComponent(qs(\'#gbuAgentSearch\').value.trim()); const list=await api(`/api/gbu/agents?search=${q}`);\n    qs(\'#gbuAgentsList\').innerHTML=list.length?list.map(x=>`<div class="employee-row"><div class="name"><b>${esc(employeeListName(x))}</b><small>ГБУ${x.source_page?` · стр. ${x.source_page}`:\'\'}</small><div class="contact-btns">${phoneLink(x.phone)}</div></div></div>`).join(\'\'):\'<div class="empty">Не найдено</div>\'; wireContactButtons(qs(\'#gbuAgentsList\'));\n  }\n  qs(\'#gbuAgentSearch\').oninput=draw; await draw();\n}\n\nasync function openCashTable(){\n  if(!state.bootstrap?.is_owner){toast(\'Касса доступна только владельцу\');return}\n\n  showModal(\'💰 Касса\', `\n    <p class="muted">В столбцах указаны имя и метро бригадира. Откат считается отдельно. Суммы можно исправлять вручную.</p>\n    <div id="cashTableBody"><div class="empty">Загрузка…</div></div>\n  `);\n\n  const rub=n=>new Intl.NumberFormat(\'ru-RU\').format(Number(n||0))+\' ₽\';\n\n  async function editCashEntry(entry){\n    const raw=prompt(\n      `Новая сумма кассы\\n${entry.brigadier_name} · ${fmtDate(entry.work_date)}`,\n      String(Number(entry.commission_rub||0))\n    );\n    if(raw===null) return;\n\n    const value=Number(String(raw).replace(/\\s/g,\'\').replace(/[^\\d]/g,\'\'));\n    if(!Number.isFinite(value) || value<0){\n      toast(\'Введите корректную сумму\');\n      return;\n    }\n\n    await api(`/api/cash/${entry.id}`,{\n      method:\'PATCH\',\n      body:{commission_rub:Math.round(value)}\n    });\n    toast(\'Касса изменена\');\n    await drawCash();\n  }\n\n  async function drawCash(){\n    const body=qs(\'#cashTableBody\');\n    if(!body) return;\n\n    // Запоминаем положение окна и горизонтальную прокрутку таблицы.\n    // Автообновление больше не возвращает владельца в начало.\n    const modal=qs(\'#modal\');\n    const savedModalScroll=modal ? modal.scrollTop : 0;\n    const oldScroller=qs(\'#cashMatrixScroller\');\n    const savedTableScroll=oldScroller ? oldScroller.scrollLeft : 0;\n\n    try{\n      const [ledger, selectedDay]=await Promise.all([\n        api(`/api/cash/ledger?on_date=${state.selectedDate}`),\n        api(`/api/cash?work_date=${state.selectedDate}`)\n      ]);\n      if(!qs(\'#cashTableBody\')) return;\n\n      const brigadiers=ledger.brigadiers||[];\n      const days=ledger.days||[];\n      const selectedRows=selectedDay.rows||[];\n      const totalsByBrig=new Map((ledger.brigadier_totals||[]).map(x=>[String(x.brigadier_tg_id),x]));\n\n      const matrix=brigadiers.length ? `\n        <div id="cashMatrixScroller" style="overflow:auto;border:1px solid var(--line);border-radius:16px;margin:10px 0 12px">\n          <table style="border-collapse:separate;border-spacing:0;min-width:max-content;width:100%;font-size:11px">\n            <thead>\n              <tr>\n                <th style="position:sticky;left:0;z-index:3;background:#18181c;padding:10px 6px;border-bottom:1px solid var(--line);text-align:center;min-width:42px">День</th>\n                ${brigadiers.map(b=>`<th style="background:#18181c;padding:10px;border-bottom:1px solid var(--line);min-width:120px">${esc(b.name)}</th>`).join(\'\')}\n                <th style="position:sticky;right:0;z-index:3;background:#211d15;color:var(--gold);padding:10px;border-bottom:1px solid var(--line);min-width:130px">Итого за день</th>\n              </tr>\n            </thead>\n            <tbody>\n              ${days.map(day=>`\n                <tr>\n                  <td style="position:sticky;left:0;z-index:2;background:#121215;padding:9px 6px;border-bottom:1px solid var(--line);font-weight:900;text-align:center;min-width:42px">${dayNumber(day.work_date)}</td>\n                  ${(day.cells||[]).map(cell=>`\n                    <td style="padding:8px 10px;border-bottom:1px solid var(--line);text-align:center">\n                      <b>${cell.commission_rub?rub(cell.commission_rub):\'—\'}</b>\n                      ${cell.kickback_rub?`<small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(cell.kickback_rub)}</small>`:\'\'}\n                    </td>\n                  `).join(\'\')}\n                  <td style="position:sticky;right:0;z-index:2;background:#19170f;padding:8px 10px;border-bottom:1px solid var(--line);text-align:center">\n                    <b style="color:var(--gold)">${rub(day.day?.commission_rub||0)}</b>\n                    <small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(day.day?.kickback_rub||0)}</small>\n                  </td>\n                </tr>\n              `).join(\'\')}\n              <tr>\n                <td style="position:sticky;left:0;z-index:2;background:#1b1b1f;padding:10px;font-weight:900">Итого</td>\n                ${brigadiers.map(b=>{\n                  const t=totalsByBrig.get(String(b.tg_id))||{};\n                  return `<td style="background:#1b1b1f;padding:10px;text-align:center"><b>${rub(t.commission_rub||0)}</b><small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(t.kickback_rub||0)}</small></td>`\n                }).join(\'\')}\n                <td style="position:sticky;right:0;z-index:3;background:#2a251b;padding:10px;text-align:center"><b style="color:var(--gold)">${rub(ledger.period?.commission_rub||0)}</b><small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(ledger.period?.kickback_rub||0)}</small></td>\n              </tr>\n            </tbody>\n          </table>\n        </div>\n      ` : \'<div class="empty">Кассовые бригадиры пока не добавлены</div>\';\n\n      const dayRows=selectedRows.map(x=>`\n        <div class="employee-row">\n          <div class="name">\n            <b>${esc(x.brigadier_name)}</b>\n            <small>${esc(x.category_label)} · ${x.team_size} чел${x.reserve_count?` · резерв ${x.reserve_count}`:\'\'}</small>\n            <div class="muted">Касса ${rub(x.commission_rub)} · откат отдельно ${rub(x.kickback_rub)}</div>\n          </div>\n          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">\n            <button class="contact-btn" data-cash-edit="${x.id}">✏️ Изменить</button>\n            <button class="contact-btn danger" data-cash-del="${x.id}">Удалить</button>\n          </div>\n        </div>\n      `).join(\'\');\n\n      body.innerHTML=`\n        <div class="summary-grid compact">\n          <div class="metric good"><b>${rub(ledger.period?.commission_rub||0)}</b><span>Касса периода</span></div>\n          <div class="metric"><b>${rub(ledger.period?.kickback_rub||0)}</b><span>Откат отдельно</span></div>\n        </div>\n        <div class="attention">\n          <h4>${fmtMonthYear(ledger.start)}</h4>\n          <p>Период: ${dayNumber(ledger.start)}–${dayNumber(ledger.end)}. Каждый столбец — имя и метро бригадира.</p>\n        </div>\n        ${matrix}\n        <div class="section-head" style="margin-top:16px">\n          <div><h2 style="font-size:16px">Записи за ${fmtDate(state.selectedDate)}</h2><p class="muted">Здесь можно вручную изменить кассу сотрудника</p></div>\n          <button class="secondary" id="cashRefreshNow">↻</button>\n        </div>\n        <div class="list">${dayRows||\'<div class="empty">На выбранную дату записей нет</div>\'}</div>\n      `;\n\n      qsa(\'[data-cash-edit]\',body).forEach(btn=>btn.onclick=async()=>{\n        const entry=selectedRows.find(x=>Number(x.id)===Number(btn.dataset.cashEdit));\n        if(entry) await editCashEntry(entry);\n      });\n\n      qsa(\'[data-cash-del]\',body).forEach(btn=>btn.onclick=async()=>{\n        if(!confirm(\'Удалить запись кассы?\')) return;\n        await api(`/api/cash/${btn.dataset.cashDel}`,{method:\'DELETE\'});\n        toast(\'Удалено\');\n        await drawCash();\n      });\n\n      const refresh=qs(\'#cashRefreshNow\');\n      if(refresh) refresh.onclick=drawCash;\n\n      // Возвращаем пользователя ровно туда, где он смотрел таблицу.\n      requestAnimationFrame(()=>{\n        const currentModal=qs(\'#modal\');\n        const currentScroller=qs(\'#cashMatrixScroller\');\n        if(currentModal) currentModal.scrollTop=savedModalScroll;\n        if(currentScroller) currentScroller.scrollLeft=savedTableScroll;\n      });\n    }catch(e){\n      if(qs(\'#cashTableBody\')) qs(\'#cashTableBody\').innerHTML=`<div class="empty">${esc(e.message)}</div>`;\n    }\n  }\n\n  await drawCash();\n  cashRefreshTimer=setInterval(()=>{\n    if(qs(\'#cashTableBody\')) drawCash();\n  },10000);\n}\n\n// Events\nqs(\'#cashBtn\').onclick=openCashTable;\nqs(\'#refreshBtn\').onclick=()=>loadBootstrap(state.selectedDate).catch(e=>toast(e.message));\nqs(\'#drawerBackdrop\').onclick=closeDrawer; qs(\'#modalBackdrop\').onclick=closeModal; qs(\'#newOrderBtn\').onclick=openManualOrder; qs(\'#gbuBtn\').onclick=openGbuImport;\nqsa(\'.bottom-nav button\').forEach(btn=>btn.onclick=async()=>{qsa(\'.bottom-nav button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');qsa(\'.view\').forEach(x=>x.classList.remove(\'active\'));qs(`#${btn.dataset.view}`).classList.add(\'active\');await renderCurrentView()});\nqsa(\'#sourceFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#sourceFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.source=btn.dataset.source;await loadOrders()});\nqsa(\'#statusFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#statusFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.status=btn.dataset.status;await loadOrders()});\nqsa(\'#employeeFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#employeeFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.employeeGroup=btn.dataset.group;await loadEmployees()});\nqs(\'#employeeSearch\').oninput=e=>{state.employeeSearch=e.target.value;loadEmployees()};\nqs(\'#employeeImportBtn\').onclick=openEmployeeImport;\nqs(\'#gbuAgentsBtn\').onclick=openGbuAgents;\nqs(\'#readinessCountBtn\').onclick=refreshReadinessCount;\nqs(\'#readinessSendBtn\').onclick=event=>sendReadinessToChats(\'summary\',event.currentTarget);\nqs(\'#readinessRemindersBtn\').onclick=event=>sendReadinessToChats(\'reminders\',event.currentTarget);\nqs(\'#readinessHistoryBtn\').onclick=openReadinessHistory;\n\nasync function autoRefresh(){\n  if(document.hidden) return;\n  if(!qs(\'#modal\').classList.contains(\'hidden\')) return;\n  try{\n    state.bootstrap=await api(`/api/bootstrap?work_date=${state.selectedDate}`); const cashBtn=qs(\'#cashBtn\'); if(cashBtn) cashBtn.classList.toggle(\'hidden\', !state.bootstrap.is_owner); renderSummary(); renderDates();\n    await renderCurrentView();\n    if(state.currentOrder && !qs(\'#orderDrawer\').classList.contains(\'hidden\')){\n      const fresh=await api(`/api/orders/${state.currentOrder.id}`); state.currentOrder=fresh; qs(\'#orderDrawer\').innerHTML=orderDrawerHtml(fresh); wireOrderDrawer(fresh);\n    }\n  }catch(e){}\n}\nsetInterval(autoRefresh,10000);\n\nloadBootstrap().catch(e=>fatal(e.message));\n\n\nasync function editDispatchCount(field){\n const day=state.selectedDate, names={private_count:\'Частных заказов\',gbu_count:\'Заказов ГБУ\',requested_staff:\'Запрошено сотрудников\'};\n const value=prompt(`${names[field]} · ${day}`,String(state.bootstrap.summary[field]||0));\n if(value===null)return;\n if(!/^\\d+$/.test(value.trim())||Number(value)>100000){toast(\'Введите целое число от 0 до 100000\');return}\n try{await api(`/api/dispatch-counts/${day}`,{method:\'PATCH\',body:JSON.stringify({field,value:Number(value)})});\n  if(state.selectedDate===day)await loadBootstrap(day);toast(\'Сохранено\');\n }catch(e){toast(e.message,5000)}\n}\nasync function openNumberCards(){\n const day=state.selectedDate;\n showModal(\'📱 Номера в группе\',\'<div id="numberCardsBody" class="empty">Загрузка…</div>\');\n const body=qs(\'#numberCardsBody\');\n try{const r=await api(`/api/number-cards?work_date=${day}`);if(qs(\'#numberCardsBody\')!==body)return;\n body.className=\'\';body.innerHTML=`<div class="attention"><h4>Отправлено: ${r.count}</h4><p>За ${esc(day)}. Кто выставил номер через бота.</p></div>${r.items.map((x,i)=>`<div class="employee-row"><div class="name"><b>${i+1}. ${esc(employeeListName(x.employee))}</b><small>${x.source===\'gbu\'?\'ГБУ\':\'Частный\'} · ${esc(x.surname)} · ${esc(new Date(x.sent_at).toLocaleTimeString(\'ru-RU\',{timeZone:\'Europe/Moscow\',hour:\'2-digit\',minute:\'2-digit\'}))}</small><small>📞 ${esc(x.phone)} · Номер отправлен</small></div></div>`).join(\'\')||\'<div class="empty">Номера пока не отправляли</div>\'}`;\n }catch(e){if(qs(\'#numberCardsBody\')===body)body.textContent=e.message}\n}\n</script>\n</body>\n</html>\n'

# Both employee editors share the same persistent Telegram contact field.
for _phone_id, _telegram_id in (("profilePhone", "profileTelegram"),
                                 ("manualReadyPhone", "manualReadyTelegram")):
    _marker = 'placeholder="+7 999 123-45-67"></div>'
    _phone_start = INLINE_INDEX_HTML.index('<input id="' + _phone_id + '"')
    _phone_end = INLINE_INDEX_HTML.index(_marker, _phone_start) + len(_marker)
    _field = ('\n<div class="field full"><label for="' + _telegram_id + '">Telegram</label>'
              '<input id="' + _telegram_id + '" type="text" maxlength="100" '
              'autocapitalize="none" autocorrect="off" spellcheck="false" '
              'placeholder="@username или https://t.me/username"></div>')
    _phone_field = INLINE_INDEX_HTML[_phone_start:_phone_end].replace(
        _marker, 'placeholder="8 999 123-45-67"></div>')
    INLINE_INDEX_HTML = (INLINE_INDEX_HTML[:_phone_start] + _phone_field + _field
                         + INLINE_INDEX_HTML[_phone_end:])
    _value = "qs('#" + _phone_id + "').value=employee.phone||'';"
    INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
        _value, _value + "\nqs('#" + _telegram_id + "').value=employee.telegram_username||'';", 1)
    _payload = "phone:qs('#" + _phone_id + "').value.trim()"
    INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
        _payload, _payload + ",telegram_username:qs('#" + _telegram_id + "').value.trim()", 1)


# Cash reminder control shares the existing owner-only cash modal.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'async function openCashTable(){',
    "let cashReminderSending=false;\nasync function openCashTable(){", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<span>Откат отдельно</span></div>\n        </div>',
    '<span>Откат отдельно</span></div>\n'
    '          <button class="secondary" id="cashReminder" style="align-self:center;padding:12px 8px" ${cashReminderSending ? "disabled" : ""}>🔔 Сверка</button>\n'
    '        </div>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "      const refresh=qs('#cashRefreshNow');",
    """      const reminder=qs('#cashReminder');
      if(reminder) reminder.onclick=async()=>{
        if(cashReminderSending) return;
        cashReminderSending=true;
        reminder.disabled=true;
        try{
          const result=await api('/api/cash/reminder',{method:'POST'});
          toast(`Отправлено: ${result.sent_chats}. Уже напомнили сегодня: ${result.already_sent_chats||0}.${result.failed_chats?' Не отправлено: '+result.failed_chats+'.':''}${result.unconfirmed_chats?' Требуют проверки: '+result.unconfirmed_chats+'. Повтор отключён, чтобы не создать дубль.':''}`,7000);
        }catch(error){toast(error.message || 'Не удалось отправить напоминание')}
        finally{
          cashReminderSending=false;
          const current=qs('#cashReminder');
          if(current) current.disabled=false;
        }
      };
      const refresh=qs('#cashRefreshNow');""", 1)

# Owner cash access is read-only; the reminder action remains available.
INLINE_INDEX_HTML = re.sub(r'<button[^>]*data-cash-(?:edit|del)="[^"\n]*"[^>]*>.*?</button>', '', INLINE_INDEX_HTML)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('Суммы можно исправлять вручную.', 'Доступен просмотр кассы.')
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('Здесь можно вручную изменить кассу сотрудника', 'Записи кассиров за выбранную дату')

# Independent cash archive navigation: never changes the dispatcher's working date.
_cash_start = INLINE_INDEX_HTML.index('async function openCashTable(){')
_cash_end = INLINE_INDEX_HTML.index('// Events', _cash_start)
_cash_html = INLINE_INDEX_HTML[_cash_start:_cash_end]
_cash_html = _cash_html.replace("  showModal('💰 Касса', `", """  let cashMonth=new Date().toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'}).slice(0,7);
  let cashRange='month', cashRequest=0;
  showModal('💰 Касса', `""", 1)
_cash_html = _cash_html.replace('<div id="cashTableBody">', """<div style="display:flex;gap:8px;align-items:center;margin:12px 0">
      <button class="secondary" id="cashPrevMonth" aria-label="Предыдущий месяц">‹</button>
      <label style="flex:1;min-width:0">Месяц кассы<input id="cashMonth" type="month" value="${cashMonth}" style="width:100%;box-sizing:border-box"></label>
      <button class="secondary" id="cashNextMonth" aria-label="Следующий месяц">›</button>
    </div>
    <div id="cashMonthTabs" style="display:flex;gap:6px;overflow-x:auto;margin-bottom:10px"></div>
    <div style="display:flex;gap:6px;margin-bottom:12px">
      <button class="primary" data-cash-range="month">Весь месяц</button>
      <button class="secondary" data-cash-range="first">1–15</button>
      <button class="secondary" data-cash-range="second">16–конец</button>
    </div>
    <div id="cashTableBody">""", 1)
_cash_html = _cash_html.replace("    if(!body) return;", """    if(!body) return;
    const request=++cashRequest;
    const cashDate=cashMonth+(cashRange==='second'?'-16':'-01');""", 1)
_cash_html = _cash_html.replace("""      const [ledger, selectedDay]=await Promise.all([
        api(`/api/cash/ledger?on_date=${state.selectedDate}`),
        api(`/api/cash?work_date=${state.selectedDate}`)
      ]);
      if(!qs('#cashTableBody')) return;""", """      const ledger=await api(`/api/cash/ledger?on_date=${cashDate}&period=${cashRange==='month'?'month':'half'}`);
      const selectedDay={rows:[]};
      if(request!==cashRequest || qs('#cashTableBody')!==body) return;""", 1)
# The archive is a complete daily matrix, not a second list for dispatcher's selected day.
_details_start = _cash_html.index('        <div class="section-head" style="margin-top:16px">')
_details_end = _cash_html.index('      `;', _details_start)
_cash_html = _cash_html[:_details_start] + _cash_html[_details_end:]
_cash_html = _cash_html.replace("if(qs('#cashTableBody')) qs('#cashTableBody').innerHTML=", "if(request===cashRequest && qs('#cashTableBody')===body) body.innerHTML=", 1)
_cash_html = _cash_html.replace('  await drawCash();\n  cashRefreshTimer=', """  function selectMonth(value){
    if(!/^[0-9]{4}-[0-9]{2}$/.test(value) || Number(value.slice(5))<1 || Number(value.slice(5))>12 || Number(value.slice(0,4))<1) return;
    cashMonth=value;qs('#cashMonth').value=value;
    qsa('[data-cash-month]').forEach(btn=>btn.className=btn.dataset.cashMonth===value?'primary':'secondary');
    drawCash();
  }
  qs('#cashMonth').onchange=event=>selectMonth(event.target.value);
  function shiftMonth(offset){
    const [year,month]=cashMonth.split('-').map(Number);
    const next=new Date(Date.UTC(year,month-1+offset,1));
    selectMonth(next.toISOString().slice(0,7));
  }
  qs('#cashPrevMonth').onclick=()=>shiftMonth(-1);
  qs('#cashNextMonth').onclick=()=>shiftMonth(1);
  qsa('[data-cash-range]').forEach(btn=>btn.onclick=()=>{
    cashRange=btn.dataset.cashRange;
    qsa('[data-cash-range]').forEach(item=>item.className=item===btn?'primary':'secondary');
    drawCash();
  });
  const tabs=qs('#cashMonthTabs');
  api('/api/cash/months').then(data=>{
    if(qs('#cashMonthTabs')!==tabs) return;
    tabs.innerHTML=data.months.map(month=>`<button style="white-space:nowrap" class="${month===cashMonth?'primary':'secondary'}" data-cash-month="${esc(month)}">${fmtMonthYear(month+'-01')}</button>`).join('');
    qsa('[data-cash-month]',tabs).forEach(btn=>btn.onclick=()=>selectMonth(btn.dataset.cashMonth));
  }).catch(()=>{if(qs('#cashMonthTabs')===tabs) tabs.textContent='Выберите месяц выше';});
  await drawCash();
  cashRefreshTimer=""", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_cash_start] + _cash_html + INLINE_INDEX_HTML[_cash_end:]

# Each modal opening owns its refresh timer and outstanding requests.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('function showModal(title, body) {',
    'let cashModalGeneration=0;\nfunction showModal(title, body) {rememberCashTablePosition();cashModalGeneration++;if(cashRefreshTimer){clearInterval(cashRefreshTimer);cashRefreshTimer=null}', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('function closeModal(){',
    'function closeModal(){rememberCashTablePosition();cashModalGeneration++;', 1)
_cash_start = INLINE_INDEX_HTML.index('async function openCashTable(){')
_cash_end = INLINE_INDEX_HTML.index('// Events', _cash_start)
_cash_html = INLINE_INDEX_HTML[_cash_start:_cash_end]
_cash_html = _cash_html.replace("  const rub=n=>", "  const cashSession=cashModalGeneration;\n  const cashBody=qs('#cashTableBody');\n  const cashActive=()=>cashSession===cashModalGeneration && qs('#cashTableBody')===cashBody && !qs('#modal').classList.contains('hidden');\n  const rub=n=>", 1)
_cash_html = _cash_html.replace("    if(!body) return;", "    if(!body || !cashActive()) return;", 1)
_cash_html = _cash_html.replace("    const request=++cashRequest;", "    const request=++cashRequest;\n    const requestedMonth=cashMonth, requestedRange=cashRange;", 1)
_cash_html = _cash_html.replace("if(request!==cashRequest || qs('#cashTableBody')!==body) return;", "if(!cashActive() || request!==cashRequest || requestedMonth!==cashMonth || requestedRange!==cashRange) return;\n      if(String(ledger.start).slice(0,7)!==requestedMonth || String(ledger.end).slice(0,7)!==requestedMonth) throw new Error('Не удалось обновить выбранный месяц. Повторите попытку.');", 1)
_cash_html = _cash_html.replace("if(request===cashRequest && qs('#cashTableBody')===body)", "if(cashActive() && request===cashRequest)", 1)
_cash_html = _cash_html.replace("      requestAnimationFrame(()=>{", "      requestAnimationFrame(()=>{\n        if(!cashActive() || request!==cashRequest) return;", 1)
_cash_html = _cash_html.replace("    cashMonth=value;qs('#cashMonth').value=value;", "    if(!cashActive()) return;\n    if(cashMonth!==value) cashBody.innerHTML='<div class=empty>Загрузка выбранного месяца…</div>';\n    cashMonth=value;qs('#cashMonth').value=value;", 1)
_cash_html = _cash_html.replace("  qs('#cashMonth').onchange=event=>selectMonth(event.target.value);", "  qs('#cashMonth').oninput=event=>selectMonth(event.target.value);\n  qs('#cashMonth').onchange=event=>{if(event.target.value!==cashMonth) selectMonth(event.target.value)};", 1)
_cash_html = _cash_html.replace("    cashRange=btn.dataset.cashRange;", "    if(!cashActive()) return;\n    cashBody.innerHTML='<div class=empty>Загрузка выбранного периода…</div>';\n    cashRange=btn.dataset.cashRange;", 1)
_cash_html = _cash_html.replace("  cashRefreshTimer=setInterval(()=>{\n    if(qs('#cashTableBody')) drawCash();\n  },10000);", "  if(cashActive()){\n    if(cashRefreshTimer) clearInterval(cashRefreshTimer);\n    cashRefreshTimer=setInterval(()=>{if(cashActive()) drawCash()},10000);\n  }", 1)
_cash_html = _cash_html.replace('async function openCashTable(){', r'''let cashTableReturnState=null;
function rememberCashTablePosition(){
  const body=qs('#cashTableBody'),modal=qs('#modal'),table=qs('#cashMatrixScroller');
  if(!body||!table||!modal||modal.classList.contains('hidden'))return;
  cashTableReturnState={month:body.dataset.cashMonth,range:body.dataset.cashRange,
    modalScroll:modal.scrollTop,tableScroll:table.scrollLeft,tableTop:table.scrollTop};
}
async function openCashTable(){
  const resume=cashTableReturnState;
  let pendingRestore=resume;
''', 1)
_cash_html = _cash_html.replace("let cashMonth=new Date().toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'}).slice(0,7);",
    "let cashMonth=resume?.month||new Date().toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'}).slice(0,7);", 1)
_cash_html = _cash_html.replace("let cashRange='month', cashRequest=0;", "let cashRange=resume?.range||'month', cashRequest=0;", 1)
_cash_html = _cash_html.replace('    const requestedMonth=cashMonth, requestedRange=cashRange;',
    "    const requestedMonth=cashMonth, requestedRange=cashRange;\n    body.dataset.cashMonth=cashMonth;body.dataset.cashRange=cashRange;", 1)
_cash_html = _cash_html.replace('''    // Запоминаем положение окна и горизонтальную прокрутку таблицы.
    // Автообновление больше не возвращает владельца в начало.
    const modal=qs('#modal');
    const savedModalScroll=modal ? modal.scrollTop : 0;
    const oldScroller=qs('#cashMatrixScroller');
    const savedTableScroll=oldScroller ? oldScroller.scrollLeft : 0;
''', '', 1)
_cash_html = _cash_html.replace('''      body.innerHTML=`
        <div class="summary-grid compact">''', '''      const modal=qs('#modal'),oldScroller=qs('#cashMatrixScroller');
      const savedModalScroll=pendingRestore?.modalScroll??modal.scrollTop;
      const savedTableScroll=pendingRestore?.tableScroll??oldScroller?.scrollLeft??0;
      const savedTableTop=pendingRestore?.tableTop??oldScroller?.scrollTop??0;
      pendingRestore=null;
      body.innerHTML=`
        <div class="summary-grid compact">''', 1)
_cash_html = _cash_html.replace('if(currentScroller) currentScroller.scrollLeft=savedTableScroll;',
    'if(currentScroller){currentScroller.scrollLeft=savedTableScroll;currentScroller.scrollTop=savedTableTop}\n        rememberCashTablePosition();', 1)
_cash_html = _cash_html.replace('    cashMonth=value;', '    pendingRestore=null;cashMonth=value;', 1)
_cash_html = _cash_html.replace('    cashRange=btn.dataset.cashRange;', '    pendingRestore=null;cashRange=btn.dataset.cashRange;', 1)
_cash_html = _cash_html.replace('  await drawCash();\n  if(cashActive()){',
    "  qsa('[data-cash-range]').forEach(btn=>btn.className=btn.dataset.cashRange===cashRange?'primary':'secondary');\n  await drawCash();\n  if(cashActive()){", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_cash_start] + _cash_html + INLINE_INDEX_HTML[_cash_end:]


# Owner-only entry point inside the cash modal.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('${dayNumber(day.work_date)}</td>',
    '<button type="button" class="secondary" style="padding:6px;min-width:32px" onclick="openCashDayCheck(\'${day.work_date}\')" aria-label="Проверить кассу за ${day.work_date}">${dayNumber(day.work_date)}</button></td>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<style>
.cash-day-edit-row{padding:14px 0;border-bottom:1px solid var(--line)}
.cash-day-edit-row b{display:block;font-size:13px;overflow-wrap:anywhere}
.cash-day-edit-row small{display:block;margin-top:5px;color:var(--muted);font-size:11px}
.cash-day-edit-controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
.cash-day-edit-controls input{width:74px;padding:8px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px;font-size:16px;text-align:center}
.cash-day-edit-controls button{padding:8px;font-size:11px}
</style>
<script>
let cashDayReturnAction=()=>openCashTable(),cashDayReturnLabel='← К таблице';
async function openCashDayCheck(day){
  if(qs('#personalGrid')){
    const grid=qs('#personalGrid'),modal=qs('#modal');
    const resume={month:qs('#personalMonth').value,kind:qs('[data-personal-kind].primary')?.dataset.personalKind||'earnings',left:grid.scrollLeft,top:grid.scrollTop,modalTop:modal.scrollTop};
    cashDayReturnAction=()=>openPersonalTables(resume);cashDayReturnLabel='← К моим таблицам';
  }else if(qs('#cashTableBody')){
    cashDayReturnAction=()=>openCashTable();cashDayReturnLabel='← К таблице';
  }
  rememberCashTablePosition();
  showModal('Касса · '+fmtDate(day), '<div class="cash-day-check-toolbar"><button class="secondary" onclick="openCashTable()">← К таблице</button><button class="secondary" id="cashDayEdit">✏️ Редактировать</button></div><div id="cashDayCheckBody" class="empty">Загрузка…</div>');
  const body=qs('#cashDayCheckBody');
  const back=qs('#modal button[onclick="openCashTable()"]');
  back.removeAttribute('onclick');back.textContent=cashDayReturnLabel;back.onclick=()=>cashDayReturnAction();
  const active=()=>qs('#cashDayCheckBody')===body&&!qs('#modal').classList.contains('hidden');
  qs('#cashDayEdit').onclick=()=>openCashDayEditor(day);
  async function refresh(){
    if(!active())return;
    try{
      const r=await api(`/api/cash/day-check?work_date=${encodeURIComponent(day)}`);
      if(!active())return;
      const scroll=qs('#modal').scrollTop;
      body.className='';
      body.innerHTML=`<div class="attention"><h4>Касса выставлена ${r.submitted}/${r.expected}</h4><p>Частные заказы кассовых бригадиров. На каждый заказ нужна отдельная запись.</p>${r.manual_count?'<p>Есть ручные изменения за этот день. Их можно изменить или отменить через «Редактировать».</p>':''}</div>${r.items.map((x,i)=>`<div class="employee-row"><div class="name"><b>${i+1}. ${esc(x.name)}</b><small>Записей в кассе: ${x.submitted}/${x.expected} <span style="font-size:11px;margin-left:6px;color:var(--muted)">${(x.amounts||[]).map(amount=>esc(String(amount))).join('; ')}</span>${x.manual_expected!==null?' · вручную':''}</small></div><span aria-label="${x.complete?'Касса выставлена':'Ожидаем кассу'}" style="font-size:22px">${x.complete?'✅':'⬜'}</span></div>`).join('')||'<div class="empty">На этот день кассу не ожидаем</div>'}`;
      qs('#modal').scrollTop=scroll;
    }catch(e){if(active())body.textContent=e.message}
    if(active())setTimeout(refresh,5000);
  }
  await refresh();
}

async function openCashDayEditor(day){
  showModal('Редактировать кассу · '+fmtDate(day), '<button class="secondary" id="cashDayEditBack">← К списку дня</button><p class="muted">Укажите, сколько записей кассы ожидаем. 0 — касса не требуется. «Авто» — считать по частным заказам. Изменения действуют только на этот день. Сданные суммы сохраняются.</p><div id="cashDayEditBody" class="empty">Загрузка…</div>');
  const body=qs('#cashDayEditBody'),back=qs('#cashDayEditBack');
  const active=()=>qs('#cashDayEditBody')===body&&!qs('#modal').classList.contains('hidden');
  let data=null,busy=false,drafts=new Map(),included=new Set();
  back.onclick=()=>{if(!busy)openCashDayCheck(day)};
  const changed=()=>data&&data.brigadiers.some(x=>drafts.get(x.tg_id)!==x.manual_expected);
  function lock(value){busy=value;back.disabled=value;qsa('button,input,select',body).forEach(x=>x.disabled=value)}
  function render(){
    const shown=data.brigadiers.filter(x=>included.has(x.tg_id)),available=data.brigadiers.filter(x=>!included.has(x.tg_id));
    body.className='';
    body.innerHTML=`<form id="cashDayEditForm">${shown.map(x=>{const manual=drafts.get(x.tg_id),count=manual===null?x.automatic_expected:manual;return `<div class="cash-day-edit-row"><b>${esc(x.name)}</b><small>По заказам: ${x.automatic_expected} · Сдано: ${x.submitted}${x.amounts.length?' · '+x.amounts.map(a=>esc(String(a))).join('; '):''}</small><div class="cash-day-edit-controls"><label>Ждём <input type="number" min="0" max="1000" step="1" inputmode="numeric" required value="${esc(String(count))}" data-cash-expected="${x.tg_id}" aria-label="Сколько записей кассы ожидаем: ${esc(x.name)}"></label><button type="button" class="secondary" data-cash-exclude="${x.tg_id}">Не ждать</button><button type="button" class="secondary" data-cash-auto="${x.tg_id}">Авто</button><small data-cash-mode="${x.tg_id}">${manual===null?'По заказам':'Вручную'}</small></div></div>`}).join('')||'<div class="empty">Бригадиров в списке пока нет</div>'}${available.length?`<label style="display:block;margin-top:16px">Добавить бригадира<select id="cashDayAdd" style="display:block;width:100%;margin-top:8px;padding:10px;font-size:16px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px"><option value="">Выберите бригадира</option>${available.map(x=>`<option value="${x.tg_id}">${esc(x.name)}</option>`).join('')}</select></label>`:''}<p id="cashDayEditError" role="status" style="color:var(--red)"></p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px"><button type="submit" class="primary">Сохранить</button><button type="button" class="secondary" id="cashDayEditCancel">Отмена</button><button type="button" class="secondary" id="cashDayEditReload">Обновить</button></div></form>`;
    qsa('[data-cash-expected]',body).forEach(input=>input.oninput=()=>{
      const id=Number(input.dataset.cashExpected);drafts.set(id,input.value);
      qs(`[data-cash-mode="${id}"]`,body).textContent='Вручную';
    });
    qsa('[data-cash-exclude]',body).forEach(button=>button.onclick=()=>{drafts.set(Number(button.dataset.cashExclude),0);render()});
    qsa('[data-cash-auto]',body).forEach(button=>button.onclick=()=>{drafts.set(Number(button.dataset.cashAuto),null);render()});
    const add=qs('#cashDayAdd');
    if(add)add.onchange=()=>{const id=Number(add.value);if(!id)return;included.add(id);drafts.set(id,1);render()};
    qs('#cashDayEditCancel').onclick=()=>openCashDayCheck(day);
    qs('#cashDayEditReload').onclick=()=>{if(!changed()||confirm('Обновить список? Несохранённые изменения будут отменены.'))load()};
    qs('#cashDayEditForm').onsubmit=save;
  }
  async function load(){
    if(busy)return;lock(true);
    try{
      const result=await api(`/api/cash/day-check?work_date=${encodeURIComponent(day)}`);if(!active())return;
      data=result;drafts=new Map(data.brigadiers.map(x=>[x.tg_id,x.manual_expected]));
      included=new Set(data.brigadiers.filter(x=>x.automatic_expected>0||x.manual_expected!==null).map(x=>x.tg_id));
      render();
    }catch(e){if(active()){const error=qs('#cashDayEditError');if(error)error.textContent=e.message;else body.textContent=e.message}}
    finally{if(active())lock(false)}
  }
  async function save(event){
    event.preventDefault();if(busy||!data)return;
    const error=qs('#cashDayEditError');error.textContent='';
    const changes=[];
    for(const x of data.brigadiers){
      const raw=drafts.get(x.tg_id),expected=raw===null?null:Number(raw);
      if(raw!==null&&(String(raw).trim()===''||!Number.isInteger(expected)||expected<0||expected>1000)){error.textContent='Укажите целое количество от 0 до 1000';return}
      if(expected!==x.manual_expected)changes.push({tg_id:x.tg_id,expected});
    }
    if(!changes.length){openCashDayCheck(day);return}
    lock(true);
    try{
      await api(`/api/cash/day-check/${encodeURIComponent(day)}`,{method:'PUT',body:JSON.stringify({version:data.version,changes})});
      if(!active())return;toast('Список кассы сохранён');await openCashDayCheck(day);
    }catch(e){if(active())error.textContent=e.message}
    finally{if(active())lock(false)}
  }
  await load();
}
</script>
</body>''',1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('showModal(\'💰 Касса\', `', 'showModal(\'💰 Касса\', `<button class="secondary" style="margin-bottom:12px" onclick="openPersonalTables()">📒 Мои таблицы</button>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("</script>\n</body>", 'async function openPersonalTables(){\n if(!state.bootstrap?.is_owner){toast(\'Доступно только владельцу\');return}\n let month=new Date().toLocaleDateString(\'sv-SE\',{timeZone:\'Europe/Moscow\'}).slice(0,7),kind=\'earnings\',data=null,busy=false,request=0;\n showModal(\'📒 Мои таблицы\',`<p class="muted">Касса и «Марине» — из общей кассы. «Владу» — количество запрошенных сотрудников × 100 ₽ за день. «Марине»: − и + меняют сумму на 250 ₽. Остальные суммы можно редактировать. Сохранение после каждой правки.</p><label>Месяц<input type="month" id="personalMonth" value="${month}" style="width:100%;box-sizing:border-box"></label><div style="display:flex;gap:6px;overflow:auto;margin:12px 0"><button class="primary" data-personal-kind="earnings">Заработок</button><button class="secondary" data-personal-kind="kickbacks">Откаты</button></div><button class="secondary" id="personalAdd">＋ Столбец</button><div id="personalStatus" class="muted" role="status"></div><div id="personalGrid" style="overflow:auto;margin-top:12px;max-height:60vh"></div>`);\n const grid=qs(\'#personalGrid\'),status=qs(\'#personalStatus\'),session=cashModalGeneration;\n const active=()=>session===cashModalGeneration && qs(\'#personalGrid\')===grid;\n const money=cents=>new Intl.NumberFormat(\'ru-RU\',{maximumFractionDigits:2}).format(cents/100);\n function lock(value){busy=value;qsa(\'[data-personal-kind],#personalMonth,#personalAdd\').forEach(e=>e.disabled=value)}\n function render(){\n  const left=grid.scrollLeft,top=grid.scrollTop;\n  const totalRow=(title,values)=>`<tr style="color:var(--gold);background:#211d14"><th>${title}</th>${values.map(v=>`<td>${money(v)}</td>`).join(\'\')}<td><b>${money(values.reduce((a,b)=>a+b,0))}</b></td></tr>`;\n  let rows=\'\';\n  for(let day=1;day<=data.days;day++){\n   const values=data.columns.map((_,col)=>data.cells[`${day}:${col}`]||0);\n   rows+=`<tr><th>${day}</th>${values.map((v,col)=>kind===\'kickbacks\'&&col===0?`<td><div style="display:flex;align-items:center;gap:6px;white-space:nowrap"><button class="secondary" data-marina-day="${day}" data-marina-delta="-250" aria-label="Убавить 250 рублей за ${day} число">−</button><span style="min-width:70px;text-align:center">${money(v)}</span><button class="secondary" data-marina-day="${day}" data-marina-delta="250" aria-label="Прибавить 250 рублей за ${day} число">+</button></div></td>`:`<td><button class="contact-btn" style="min-width:80px" ${data.automatic_columns.includes(col)?\'disabled title="Из общей кассы"\':\'\'} data-personal-cell="${day}:${col}" aria-label="${day}, ${esc(data.columns[col])}">${Object.hasOwn(data.cells,`${day}:${col}`)?money(v):\'—\'}</button></td>`).join(\'\')}<td><b>${money(values.reduce((a,b)=>a+b,0))}</b></td></tr>`;\n   if(day===15) rows+=totalRow(\'Итого 1–15\',data.first);\n  }\n  rows+=totalRow(\'Итого 16–\'+data.days,data.second)+totalRow(\'За месяц\',data.total);\n  grid.innerHTML=`<table style="border-collapse:separate;border-spacing:8px;min-width:100%;text-align:right"><thead><tr><th>День</th>${data.columns.map((name,col)=>`<th><button class="secondary" style="white-space:nowrap" ${data.automatic_columns.includes(col)?\'disabled\':\'\'} data-personal-col="${col}">${esc(name)} ${data.automatic_columns.includes(col)?\'· авто\':\'✍️\'}</button></th>`).join(\'\')}<th>Итого ₽</th></tr></thead><tbody>${rows}</tbody></table>`;\n  grid.scrollLeft=left;grid.scrollTop=top;\n  qsa(\'[data-marina-day]\',grid).forEach(btn=>btn.onclick=()=>{\n   if(busy)return;\n   save({action:\'adjust\',day:Number(btn.dataset.marinaDay),column:0,value:btn.dataset.marinaDelta});\n  });\n  qsa(\'[data-personal-cell]\',grid).forEach(btn=>btn.onclick=()=>{\n   if(busy)return;const [day,column]=btn.dataset.personalCell.split(\':\').map(Number),key=`${day}:${column}`;\n   const value=prompt(`${data.columns[column]} · ${day}.${month.slice(5)}\\nСумма ₽ (пусто — очистить)`,Object.hasOwn(data.cells,key)?String(data.cells[key]/100):\'\');\n   if(value!==null) save({action:\'cell\',day,column,value});\n  });\n  qsa(\'[data-personal-col]\',grid).forEach(btn=>btn.onclick=()=>{\n   if(busy)return;const column=Number(btn.dataset.personalCol),value=prompt(\'Название столбца\',data.columns[column]);\n   if(value!==null)save({action:\'rename\',column,value});\n  });\n }\n async function load(){\n  const id=++request;lock(true);grid.innerHTML=\'<div class="empty">Загрузка…</div>\';status.textContent=\'\';\n  try{const result=await api(`/api/personal-tables/${kind}/${month}`);if(!active()||id!==request)return;data=result;render()}\n  catch(e){if(active()&&id===request){data=null;grid.innerHTML=\'\';status.textContent=e.message}}\n  finally{if(active()&&id===request)lock(false)}\n }\n async function save(change){\n  if(busy||!data||!active())return;++request;lock(true);status.textContent=\'Сохранение…\';\n  try{const result=await api(`/api/personal-tables/${kind}/${month}`,{method:\'PATCH\',body:JSON.stringify({...change,version:data.version})});if(!active())return;data=result;render();status.textContent=\'✓ Сохранено\'}\n  catch(e){if(active())status.textContent=\'Не сохранено: \'+e.message}\n  finally{if(active())lock(false)}\n }\n qs(\'#personalAdd\').onclick=()=>{if(busy||!data)return;const value=prompt(\'Название нового столбца\');if(value!==null)save({action:\'add\',value})};\n qs(\'#personalMonth\').onchange=e=>{if(!e.target.value)return;month=e.target.value;load()};\n qsa(\'[data-personal-kind]\').forEach(btn=>btn.onclick=()=>{if(busy)return;kind=btn.dataset.personalKind;qsa(\'[data-personal-kind]\').forEach(x=>x.className=x===btn?\'primary\':\'secondary\');load()});\n await load();\n const timer=setInterval(async()=>{\n  if(!active()){clearInterval(timer);return}\n  if(busy||!data)return;\n  const id=request,loadedKind=kind,loadedMonth=month;\n  try{const result=await api(`/api/personal-tables/${loadedKind}/${loadedMonth}`);\n   if(active()&&!busy&&id===request&&kind===loadedKind&&month===loadedMonth&&data.version===result.version){data=result;render()}\n  }catch(e){}\n },15000);\n}\n\n' + "\n</script>\n</body>", 1)

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INLINE_INDEX_HTML, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})


# ---------- Schemas ----------

class EmployeeSync(BaseModel):
    preserve_identity: bool = False
    tg_id: int | None = None
    full_name: str
    phone: str = ""
    telegram_username: str = ""
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    height_cm: int | None = Field(default=None, ge=150, le=220)
    has_car: bool = False
    metro: str = ""
    is_cashier: bool | None = None
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
    telegram_username: str = ""
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    work_date: date
    status: Literal["ready", "not_ready", "day_off", "responded"]
    height_cm: int | None = Field(default=None, ge=150, le=220)
    has_car: bool = False
    phone: str = ""
    raw_text: str = ""


class ReadinessMessageReport(ReadinessReport):
    source_chat_id: int = Field(lt=0)
    source_message_id: int = Field(gt=0)
    source_sent_at: int = Field(gt=0)
    source_event_at: int = Field(gt=0)
    source_event_key: str = Field(min_length=64, max_length=64)
    is_edit: bool = False


class EmployeeMembership(BaseModel):
    tg_id: int
    full_name: str
    telegram_username: str = ""
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    active: bool = True


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
    kickback_rub: int | None = Field(default=None, ge=0)
    agent_name: str | None = None
    agent_phone: str | None = None
    other_agent_phone: str | None = None


class AssignPerson(BaseModel):
    employee_id: int


class ManualComposition(BaseModel):
    brigadier_id: int
    member_ids: list[int] = Field(default_factory=list)


class BrigadierClose(BaseModel):
    contact_time: str = "08:00"


class BrigadeContactReport(BaseModel):
    tg_id: int
    work_date: date
    raw_text: str = ""
    report_key: str | None = Field(default=None, pattern=r'^card:-\d+:\d+$', max_length=180)
    request_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class BrigadierTeaReport(BaseModel):
    tg_id: int
    tea_count: int = Field(default=0, ge=0)


class BrigadierCashPatch(BaseModel):
    tg_id: int
    category: Literal["standard", "vip", "elite"] | None = None
    team_size: Literal[4, 6] | None = None
    commission_rub: int | None = Field(default=None, ge=0)
    kickback_rub: int | None = Field(default=None, ge=0)
    reserve_count: int | None = Field(default=None, ge=0, le=10)


class BrigadierCashDayDelete(BaseModel):
    tg_id: int
    work_date: date


class BrigadierCashEntryDelete(BaseModel):
    tg_id: int


class BrigadierCashDeleteRequest(BaseModel):
    tg_id: int
    entry_id: int


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
    agent_name: str = ""
    agent_phone: str = ""
    notes: str = ""


class OwnerSync(BaseModel):
    tg_id: int = Field(gt=0)
    full_name: str = ""
    bot_username: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class CashierToggle(BaseModel):
    is_cashier: bool


class CashEntryCreate(BaseModel):
    tg_id: int
    work_date: date
    category: Literal["standard", "vip", "elite"]
    team_size: Literal[4, 6]
    commission_rub: int = Field(default=0, ge=0)
    kickback_rub: int = Field(default=0, ge=0)
    reserve_count: int = Field(default=0, ge=0, le=10)
    notes: str = ""
    request_id: str | None = Field(default=None, min_length=16, max_length=64)


class CashEntrySubmission(Base):
    __tablename__ = 'cash_entry_submissions'
    tg_id = Column(BigInteger, primary_key=True)
    request_id = Column(String(64), primary_key=True)
    entry_id = Column(Integer, nullable=False)
    payload_hash = Column(String(64), nullable=False)


class CashDayCheckPlan(Base):
    __tablename__ = 'cash_day_check_plans'
    work_date = Column(Date, primary_key=True)
    overrides_json = Column(String, nullable=False, default='{}')
    version = Column(Integer, nullable=False, default=1)
    updated_by_tg_id = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class CashDayExpectedChange(BaseModel):
    tg_id: int = Field(gt=0, strict=True)
    expected: int | None = Field(ge=0, le=1000, strict=True)


class CashDayCheckChange(BaseModel):
    version: int = Field(ge=0, strict=True)
    changes: list[CashDayExpectedChange] = Field(min_length=1, max_length=200)


class CashEntryPatch(BaseModel):
    work_date: date | None = None
    category: Literal["standard", "vip", "elite"] | None = None
    team_size: Literal[4, 6] | None = None
    commission_rub: int | None = Field(default=None, ge=0)
    kickback_rub: int | None = Field(default=None, ge=0)
    reserve_count: int | None = Field(default=None, ge=0, le=10)
    notes: str | None = None


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


def _period_bounds(day: date) -> tuple[date, date]:
    import calendar
    if day.day <= 15:
        return day.replace(day=1), day.replace(day=15)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=16), day.replace(day=last)


def _is_owner(db: Session, tg_id: int) -> bool:
    row = db.execute(select(AccessUser).where(AccessUser.tg_id == tg_id, AccessUser.active.is_(True), AccessUser.role == "owner")).scalar_one_or_none()
    if row:
        return True
    raw = os.getenv("OWNER_TELEGRAM_ID", "").strip() or os.getenv("OWNER_TELEGRAM_IDS", "").strip()
    ids = {int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    return int(tg_id) in ids


def _can_view_vlad_allowance(db: Session, user: TelegramUser) -> bool:
    if _is_owner(db, user.id):
        return True
    # Require an existing dispatcher session and the registered brigadier's
    # Telegram identity, not a match on an editable display name.
    employee = db.scalar(select(Employee).where(
        Employee.tg_id == user.id, Employee.active.is_(True),
        Employee.group_code == "brigadier", _visible_employee()))
    if employee is None:
        return False
    handles = {str(value or "").strip().lstrip("@").casefold() for value in
               (employee.telegram_username, getattr(user, "username", ""))}
    return "vlad_ryazanski" in handles


def _require_owner(db: Session, user: TelegramUser) -> None:
    if not _is_owner(db, user.id):
        raise HTTPException(status_code=403, detail="Касса доступна только владельцу")


def _purge_demo_cash_entries(db: Session) -> int:
    """Remove only the two demo cashiers' entries; caller commits atomically."""
    def demo_label(name: str, metro: str = "") -> bool:
        name = re.sub(r"\s+", " ", (name or "").casefold()).strip()
        metro = re.sub(r"\s+", " ", (metro or "").casefold()).strip()
        return bool(re.fullmatch(r"иван (марьино|волжская)(?: \d{3}(?: см)?)?", name)
                    or (name == "иван" and metro in {"марьино", "волжская"}))

    target_ids = {8038387894}  # Иван Волжская, existing retired profile.
    for emp in db.execute(select(Employee)).scalars():
        if emp.tg_id and (demo_label(emp.full_name, emp.metro)
                         or str(emp.telegram_username or "").lstrip("@").casefold() == "legionritual"):
            target_ids.add(int(emp.tg_id))
    entries = db.execute(select(CashEntry)).scalars().all()
    # Historical labels also identify the account when its employee profile is absent.
    target_ids.update(int(row.brigadier_tg_id) for row in entries
                      if row.brigadier_tg_id and demo_label(row.brigadier_name))
    removed = 0
    for row in entries:
        if row.brigadier_tg_id in target_ids:
            db.delete(row)
            removed += 1
    return removed


def _cashier_excluded(db: Session, emp) -> bool:
    if emp.tg_id and (int(emp.tg_id) == 8038387894 or _is_owner(db, int(emp.tg_id))):
        return True
    label = re.sub(r"\s+", " ", f"{emp.full_name or ''} {emp.metro or ''}".casefold()).strip()
    return bool(re.search(r"\bиван\s+(марьино|волжская)\b", label))


def _cash_excluded_ids(db: Session) -> set[int]:
    return set(HIDDEN_TG_IDS) | {int(emp.tg_id) for emp in db.execute(select(Employee)).scalars()
                               if emp.tg_id and _cashier_excluded(db, emp)}


def _cash_brigadier_label(db: Session, tg_id: int, fallback: str = "") -> str:
    if int(tg_id) in HIDDEN_TG_IDS:
        return "Скрытая архивная запись"
    emp = db.execute(
        select(Employee).where(Employee.tg_id == int(tg_id))
    ).scalar_one_or_none()

    if emp:
        first_name = (emp.full_name or fallback or str(tg_id)).strip().split()[0]
        metro = (emp.metro or "").strip()
        return f"{first_name} {metro}".strip()

    value = (fallback or str(tg_id)).strip()
    # Если в старой записи сохранён внутренний формат "Имя Метро 185",
    # убираем рост в конце и оставляем имя + метро.
    value = re.sub(r"\s+(?:1[5-9]\d|2[0-1]\d)\s*$", "", value).strip()
    return value


def _cash_row_dict(x: CashEntry, db: Session | None = None) -> dict:
    display_name = (
        _cash_brigadier_label(db, x.brigadier_tg_id, x.brigadier_name)
        if db is not None
        else x.brigadier_name
    )
    return {
        "id": x.id, "work_date": x.work_date.isoformat(), "brigadier_tg_id": x.brigadier_tg_id,
        "brigadier_name": display_name, "category": x.category, "category_label": _label_category(x.category),
        "team_size": x.team_size, "commission_rub": x.commission_rub, "kickback_rub": x.kickback_rub,
        "reserve_count": x.reserve_count, "cash_rub": int(x.commission_rub or 0), "total_rub": int(x.commission_rub or 0),
        "notes": x.notes or "", "created_at": x.created_at.isoformat() if x.created_at else None,
        "source": getattr(x, 'source', 'bot'),
    }


def _all_cash_rows(db, start, end, tg_id=None):
    """Read both ledgers without copying entries or merging equal amounts."""
    from types import SimpleNamespace
    excluded = _cash_excluded_ids(db) if tg_id is None else set()
    rows = list(db.scalars(select(CashEntry).where(CashEntry.work_date.between(start, end),
        CashEntry.brigadier_tg_id.notin_(excluded),
        CashEntry.brigadier_tg_id == tg_id if tg_id is not None else True)))
    for item in db.scalars(select(CabinetOrderCash).where(CabinetOrderCash.work_date.between(start, end),
            CabinetOrderCash.tg_id.notin_(excluded),
            CabinetOrderCash.tg_id == tg_id if tg_id is not None else True)):
        rows.append(SimpleNamespace(id=f'cabinet:{item.event_id}:{item.tg_id}', source='cabinet',
            work_date=item.work_date, brigadier_tg_id=item.tg_id,
            brigadier_name=_cash_brigadier_label(db, item.tg_id), category=item.category,
            team_size=item.team_size, commission_rub=item.commission, kickback_rub=item.kickback,
            reserve_count=item.reserve, notes='ум: ' + item.surname, created_at=item.updated_at))
    return sorted(rows, key=lambda x: (x.work_date, str(x.brigadier_name).casefold(), str(x.id)))


def _normalize_brigadier_phones(db):
    for emp in db.scalars(select(Employee).where(Employee.group_code == "brigadier")):
        override = db.get(EmployeePhoneOverride, emp.id)
        if override is not None:
            override.phone = brigadier_phone8(override.phone)
            emp.phone = override.phone
        else:
            emp.phone = brigadier_phone8(emp.phone)
    for card in db.scalars(select(NumberCardEvent)):
        card.phone = brigadier_phone8(card.phone)


def _employee_dict(emp: Employee, workload: int = 0, readiness_status: str | None = None, brig_height: int | None = None):
    diff = abs((emp.height_cm or 0) - brig_height) if brig_height and emp.height_cm else None
    return {
        "id": emp.id,
        "tg_id": emp.tg_id,
        "full_name": emp.full_name,
        "display_name": ((emp.full_name.split()[0] if emp.full_name else "") + (f" {emp.metro.strip()}" if emp.group_code == "brigadier" and (emp.metro or "").strip() else "") + (f" {emp.height_cm}" if emp.group_code == "brigadier" and emp.height_cm else "")).strip() or emp.full_name,
        "phone": brigadier_phone8(emp.phone),
        "telegram_username": emp.telegram_username or "",
        "telegram_url": (f"https://t.me/{(emp.telegram_username or '').lstrip('@')}" if emp.telegram_username else (f"tg://user?id={emp.tg_id}" if emp.tg_id else "")),
        "group_code": emp.group_code,
        "group_label": GROUP_LABELS.get(emp.group_code, emp.group_code),
        "height_cm": emp.height_cm,
        "has_car": emp.has_car,
        "metro": emp.metro or "",
        "is_cashier": bool(emp.is_cashier),
        "active": emp.active,
        "workload": workload,
        "capacity": 2,
        "blocked": workload >= 2,
        "readiness_status": readiness_status,
        "height_diff": diff,
        "height_match": diff is not None and diff <= 2,
    }


def _order_dict(order: Order, db: Session, include_candidates: bool = False):
    daily_number = None
    if order.source == "gbu":
        daily_number = int(db.execute(
            select(func.count(Order.id)).where(
                Order.source == "gbu",
                Order.work_date == order.work_date,
                or_(
                    Order.issue_time < order.issue_time,
                    and_(Order.issue_time == order.issue_time, Order.id <= order.id),
                ),
            )
        ).scalar_one())

    assignments = db.execute(
        select(Assignment)
        .where(Assignment.order_id == order.id)
        .options(selectinload(Assignment.employee))
        .order_by(Assignment.role.desc(), Assignment.id)
    ).scalars().all()
    brig = next((a.employee for a in assignments if a.role == "brigadier" and a.employee and a.employee.tg_id not in HIDDEN_TG_IDS), None)
    members = [a.employee for a in assignments if a.role == "member" and a.employee and a.employee.tg_id not in HIDDEN_TG_IDS]
    return {
        "id": order.id,
        "public_id": order.public_id,
        "source": order.source,
        "daily_number": daily_number,
        "display_number": daily_number if order.source == "gbu" else order.id,
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
        "brigade_on_contact_at": order.brigade_on_contact_at.isoformat() if order.brigade_on_contact_at else None,
        "tea_count": int(order.tea_count or 0),
        "tea_reported_at": order.tea_reported_at.isoformat() if order.tea_reported_at else None,
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


def _readiness_cutoff_label(db: Session) -> str:
    # The bot publishes its applied settings, including the first summary time.
    snapshot = db.get(OwnerBotSnapshot, 1)
    try:
        options = json.loads(snapshot.payload).get('options', {}) if snapshot else {}
        clock = options.get('first_summary')
    except (ValueError, TypeError, AttributeError):
        clock = None
    return clock if isinstance(clock, str) and re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', clock) else '14:00'


def _cutoff_utc_naive(work_date: date, cutoff_label: str = '14:00') -> datetime:
    hour, minute = map(int, cutoff_label.split(':'))
    local_dt = datetime.combine(work_date - timedelta(days=1), time(hour, minute), tzinfo=MOSCOW)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _order_by_id_for_update(db: Session, order_id: int) -> Order:
    order = db.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден")
    return order


def _employee_for_update(db: Session, employee_id: int) -> Employee:
    emp = db.execute(select(Employee).where(Employee.id == employee_id).with_for_update()).scalar_one_or_none()
    if not emp or not emp.active or emp.tg_id in HIDDEN_TG_IDS:
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
    if not emp and data.telegram_username.strip():
        username = data.telegram_username.strip().lstrip("@").lower()
        candidates = db.execute(select(Employee).where(func.lower(Employee.telegram_username) == username)).scalars().all()
        if len(candidates) == 1 and (not data.tg_id or not candidates[0].tg_id or candidates[0].tg_id == data.tg_id):
            emp = candidates[0]
    if not emp and data.phone.strip():
        phone_key = _reference_phone(data.phone)
        candidates = [x for x in db.execute(select(Employee)).scalars().all()
                      if phone_key and _reference_phone(x.phone) == phone_key]
        if len(candidates) == 1 and (not data.tg_id or not candidates[0].tg_id or candidates[0].tg_id == data.tg_id):
            emp = candidates[0]
    # Names alone cannot safely bind a Telegram account to another person's contact.
    if not emp and not data.tg_id and data.full_name.strip():
        candidates = db.execute(select(Employee).where(func.lower(Employee.full_name) == data.full_name.strip().lower())).scalars().all()
        if len(candidates) == 1:
            emp = candidates[0]
    if not emp:
        emp = Employee(tg_id=data.tg_id, full_name=data.full_name, group_code=data.group_code)
        db.add(emp)
    elif data.tg_id and not emp.tg_id:
        emp.tg_id = data.tg_id
    deleted = db.get(EmployeeDeletion, emp.id) if emp.id else None
    if not deleted and emp.tg_id:
        deleted = db.execute(select(EmployeeDeletion).where(EmployeeDeletion.tg_id == emp.tg_id)).scalars().first()
    if deleted:
        if emp.tg_id and not deleted.tg_id:
            deleted.tg_id = emp.tg_id
        emp.active = False
        emp.is_cashier = False
        db.flush()
        return emp
    if not data.preserve_identity or not emp.full_name:
        emp.full_name = data.full_name.strip()
    if data.phone.strip():
        emp.phone = data.phone.strip()
    if data.telegram_username.strip():
        emp.telegram_username = data.telegram_username.strip().lstrip("@")
    group_override = db.get(OwnerEmployeeGroup, emp.id) if emp.id else None
    emp.group_code = group_override.group_code if group_override else data.group_code
    if data.height_cm is not None:
        emp.height_cm = data.height_cm
    if 'has_car' in getattr(data, 'model_fields_set', {'has_car'}):
        emp.has_car = data.has_car
    if data.metro.strip():
        emp.metro = data.metro.strip()
    if data.is_cashier is not None:
        emp.is_cashier = bool(data.is_cashier)
    emp.active = data.active and emp.tg_id not in HIDDEN_TG_IDS
    if emp.tg_id in HIDDEN_TG_IDS:
        emp.is_cashier = False
    _apply_profile_override(db, emp)
    if _cashier_excluded(db, emp):
        emp.is_cashier = False
    db.flush()
    return emp


# ---------- Auth / bootstrap ----------

class DispatchDayComment(Base):
    __tablename__ = "dispatch_day_comments"
    work_date = Column(Date, primary_key=True)
    text = Column(String(500), nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    updated_by_tg_id = Column(BigInteger, nullable=False)
    updated_by_name = Column(String(200), nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DayCommentChange(BaseModel):
    text: str = Field(max_length=500)
    version: int = Field(ge=0, strict=True)


def day_comment_data(db: Session, work_date: date):
    row = db.get(DispatchDayComment, work_date)
    updated = row.updated_at if row else None
    if updated and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return {"work_date": work_date.isoformat(), "text": row.text if row else "",
            "version": row.version if row else 0,
            "updated_by": row.updated_by_name if row else "",
            "updated_at": updated.isoformat() if updated else None}


@app.get("/api/day-comments/{work_date}")
def get_day_comment(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    return day_comment_data(db, work_date)


@app.put("/api/day-comments/{work_date}")
def save_day_comment(work_date: date, change: DayCommentChange,
                     db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    values = {"text": change.text.strip(), "version": change.version + 1,
              "updated_by_tg_id": user.id, "updated_by_name": user.full_name[:200],
              "updated_at": datetime.now(timezone.utc)}
    conflict = "Комментарий уже изменён. Обновите его перед сохранением. Ваш текст остался в поле."
    try:
        if change.version == 0:
            db.add(DispatchDayComment(work_date=work_date, **values))
        else:
            result = db.execute(update(DispatchDayComment).where(
                DispatchDayComment.work_date == work_date,
                DispatchDayComment.version == change.version).values(**values))
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(409, conflict)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, conflict)
    return day_comment_data(db, work_date)


@app.get("/api/me")
def api_me(user: TelegramUser = Depends(require_admin)):
    return {"id": user.id, "name": user.full_name, "username": user.username}


@app.get("/api/bootstrap")
def bootstrap(work_date: date | None = None, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    selected = work_date or datetime.now(MOSCOW).date()
    kind = cabinet_kind(db, user.id)
    if not _dispatcher_access(db, user.id):
        # Never calculate or expose dispatcher summaries to a brigadier.
        return JSONResponse({"user": {"id": user.id, "name": user.full_name},
            "cabinet_only": True, "is_owner": False, "can_manage_cabinets": False,
            "can_use_owner_cabinet": True, "can_view_vlad_allowance": False,
            "cabinet_kind": kind, "can_record_cash": cabinet_can_cash(db, user.id),
            "can_manage_cash": False,
            "selected_date": selected.isoformat(), "today": datetime.now(MOSCOW).date().isoformat()},
            headers={"Cache-Control": "private, no-store"})
    dates = _date_folders(db, selected)
    summary = _day_summary(db, selected)
    return {
        "user": {"id": user.id, "name": user.full_name},
        "is_owner": _is_owner(db, user.id),
        "cabinet_only": False,
        "cabinet_kind": kind,
        "can_record_cash": cabinet_can_cash(db, user.id),
        "can_manage_cash": True,
        "can_manage_cabinets": _is_cabinet_owner(db, user.id),
        "can_use_owner_cabinet": _can_use_owner_cabinet(db, user.id),
        "can_view_vlad_allowance": _can_view_vlad_allowance(db, user),
        "selected_date": selected.isoformat(),
        "today": datetime.now(MOSCOW).date().isoformat(),
        "dates": dates,
        "summary": summary,
        "day_comment": day_comment_data(db, selected),
        "groups": GROUP_LABELS,
    }


# Employee GBU payroll is separate from company earnings and cash tables.
class GbuPayrollRoster(Base):
    __tablename__ = 'gbu_payroll_rosters'
    report_key = Column(String(180), primary_key=True)
    work_date = Column(Date, nullable=False, index=True)
    payload = Column(String, nullable=False)
    created_by = Column(BigInteger, nullable=False)


class GbuOrderCrewSeat(Base):
    __tablename__ = 'gbu_order_crew_seats'
    # One confirmed order per employee per date, enforced across concurrent requests.
    work_date = Column(Date, primary_key=True)
    employee_id = Column(Integer, primary_key=True)
    report_key = Column(String(180), nullable=False, index=True)


class GbuPayrollRosterInput(BaseModel):
    card_id: StrictInt
    category: Literal['standard', 'elite']
    employee_ids: list[StrictInt] = Field(min_length=4, max_length=6)
    expected_revision: StrictStr | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


def gbu_staff_pay(category, is_brigadier, name='', metro=''):
    if category not in {'standard', 'elite'}:
        raise ValueError('Не указана категория')
    if not is_brigadier:
        return 1700 if category == 'standard' else 2600
    normalized = f'{name} {metro}'.casefold()
    vlad = bool(re.search(r'\b(влад|владислав)\b', normalized) and 'рязанск' in normalized)
    return (3000 if category == 'standard' else 3400) if vlad else (2500 if category == 'standard' else 3000)


def gbu_payroll_cards(db, month):
    start = date.fromisoformat(month+'-01')
    last = date(start.year, 12, 31) if start.month == 12 else start.replace(month=start.month+1)-timedelta(days=1)
    return list(db.scalars(select(NumberCardEvent).where(NumberCardEvent.source=='gbu',
        NumberCardEvent.work_date>=start, NumberCardEvent.work_date<=last, _visible_number_card()).order_by(NumberCardEvent.work_date,NumberCardEvent.id)))


@app.get('/api/gbu-staff-payroll')
def gbu_staff_payroll(month: str, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _dispatcher_access(db,user.id):
        raise HTTPException(403,'Доступ только диспетчеру')
    if not re.fullmatch(r'[1-9]\d{3}-(0[1-9]|1[0-2])',month):
        raise HTTPException(422,'Выберите месяц')
    people={};orders=[];missing=[]
    for card in gbu_payroll_cards(db,month):
        key=f'card:{card.chat_id}:{card.message_id}'
        if not effective_order_photo(db,card).get('first_received'):
            continue
        record=db.get(GbuPayrollRoster,key)
        item={'card_id':card.id,'date':card.work_date.isoformat(),'surname':card.surname,'brigadier':card.full_name,'tg_id':card.tg_id}
        if not record:
            missing.append(item);continue
        roster=json.loads(record.payload)
        orders.append(dict(item,category=roster['category'],crew=roster['crew']))
        for member in roster['crew']:
            row=people.setdefault(member['id'],{'id':member['id'],'name':member['name'],'exits':0,'amount':0,'orders':[]})
            row['exits']+=1;row['amount']+=member['amount'];row['orders'].append(dict(item,amount=member['amount'],role=member['role'],category=roster['category']))
    return JSONResponse({'month':month,'employees':sorted(people.values(),key=lambda x:x['name'].casefold()),
        'orders':orders,'missing':missing,'total':sum(x['amount'] for x in people.values())},headers={'Cache-Control':'private, no-store'})


@app.post('/api/gbu-staff-payroll/roster')
def save_gbu_staff_roster(data: GbuPayrollRosterInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _dispatcher_access(db,user.id):
        raise HTTPException(403,'Доступ только диспетчеру')
    card=db.scalar(select(NumberCardEvent).where(NumberCardEvent.id==data.card_id,NumberCardEvent.source=='gbu',_visible_number_card()).with_for_update())
    if not card or not effective_order_photo(db,card).get('first_received'):
        raise HTTPException(409,'Для учёта выхода нужно первое фото заказа')
    ids=set(data.employee_ids)
    if len(ids)!=len(data.employee_ids) or len(ids) not in {4,6}:
        raise HTTPException(422,'Выберите состав из 4 или 6 разных сотрудников')
    key=f'card:{card.chat_id}:{card.message_id}'
    record=db.get(GbuPayrollRoster,key)
    previous = json.loads(record.payload) if record else None
    if previous and previous['category']==data.category and {m['id'] for m in previous['crew']}==ids:
        return {'ok':True,'already_saved':True}
    revision = hashlib.sha256(record.payload.encode()).hexdigest() if record else None
    if getattr(data, 'expected_revision', None) != revision:
        raise HTTPException(409,'Состав изменён другим диспетчером. Откройте его заново')
    context = gbu_order_roster_context(db, card)
    detail = context['detail']
    if not detail:
        raise HTTPException(409,'Сначала сохраните категорию и количество человек в заработке ГБУ')
    if data.category != detail['category'] or len(ids) != detail['team_size']:
        raise HTTPException(409,'Категория или состав изменились в заработке. Откройте выбор заново')
    leader = context['leader']
    available = {e['id'] for e in context['employees'] if not e['busy']}
    if not leader or leader['id'] not in ids or not ids <= available:
        raise HTTPException(409,'Сотрудник уже занят или не готов на эту дату. Обновите состав')
    employees=list(db.scalars(select(Employee).where(Employee.id.in_(ids),Employee.active.is_(True),_visible_employee())))
    if len(employees)!=len(ids):
        raise HTTPException(409,'Список сотрудников изменился. Обновите состав')
    crew=[{'id':e.id,'name':_employee_dict(e)['display_name'],'role':'brigadier' if e.id==leader['id'] else 'member',
        'amount':gbu_staff_pay(data.category,e.id==leader['id'],e.full_name,getattr(e,'metro',''))} for e in employees]
    # Preserve historical rates for unchanged members of the same category.
    if previous and previous['category'] == data.category:
        old_members = {m['id']: m for m in previous['crew']}
        for member in crew:
            old_member = old_members.get(member['id'])
            if old_member and old_member['role'] == member['role']:
                member['amount'] = old_member['amount']
    payload = json.dumps({'category':data.category,'crew':crew},ensure_ascii=False)
    # Acquire new seats before releasing removed seats; commit the roster atomically.
    new_seats = []
    for employee_id in sorted(ids):
        seat = db.get(GbuOrderCrewSeat, (card.work_date, employee_id))
        if seat and seat.report_key != key:
            raise HTTPException(409,'Сотрудник уже занят другим заказом')
        if not seat:
            new_seats.append(GbuOrderCrewSeat(work_date=card.work_date,employee_id=employee_id,report_key=key))
    if previous:
        for employee_id in {m['id'] for m in previous['crew']} - ids:
            seat = db.get(GbuOrderCrewSeat, (card.work_date, employee_id))
            if seat and seat.report_key == key:
                db.delete(seat)
        record.payload = payload
    else:
        db.add(GbuPayrollRoster(report_key=key,work_date=card.work_date,payload=payload,created_by=user.id))
    for seat in new_seats:
        db.add(seat)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Состав уже сохранён или сотрудник занят другим заказом. Обновите страницу')
    return {'ok':True}



def gbu_order_roster_context(db, card):
    key = f'card:{card.chat_id}:{card.message_id}'
    detail = gbu_control_earning_details(db, card.work_date).get(f'card:{card.id}')
    employees = list(db.scalars(select(Employee).where(Employee.active.is_(True), _visible_employee())))
    leaders = [e for e in employees if e.tg_id == card.tg_id]
    leader_id = leaders[0].id if len(leaders) == 1 else None
    summary = readiness_summary_data(db, card.work_date)
    reported = {item['id']: item.get('readiness_status', status)
        for group in summary['groups'].values()
        for status in ('ready',)
        for item in group.get(status, []) if item.get('id', 0) > 0}
    busy = set()
    # Include rosters confirmed before the date/employee uniqueness table existed.
    for roster in db.scalars(select(GbuPayrollRoster).where(GbuPayrollRoster.work_date == card.work_date)):
        if roster.report_key != key:
            busy.update(m['id'] for m in json.loads(roster.payload)['crew'])
    for seat in db.scalars(select(GbuOrderCrewSeat).where(GbuOrderCrewSeat.work_date == card.work_date)):
        if seat.report_key != key:
            busy.add(seat.employee_id)
    record = db.get(GbuPayrollRoster, key)
    saved = json.loads(record.payload) if record else None
    saved_ids = {m['id'] for m in saved['crew']} if saved else set()
    people = [{**_employee_dict(e), 'busy': e.id in busy,
        'selected': e.id in saved_ids, 'leader': e.id == leader_id,
        'readiness_status': reported.get(e.id)} for e in employees
        if e.id in reported or e.id == leader_id or e.id in saved_ids]
    people.sort(key=lambda e: (not e['leader'], str(e['display_name']).casefold(), e['id']))
    return {'card_id': card.id, 'date': card.work_date.isoformat(), 'surname': card.surname,
        'detail': detail, 'leader': next((e for e in people if e['leader']), None),
        'employees': people, 'confirmed': saved is not None,
        'revision': hashlib.sha256(record.payload.encode()).hexdigest() if record else None,
        'confirmed_crew': [{'id':m['id'], 'name':m['name'], 'role':m['role'],
            'employee':next((e for e in people if e['id']==m['id']),None)} for m in saved['crew']] if saved else [],
        'confirmed_detail': {'category': saved['category'], 'team_size': len(saved['crew'])} if saved else None,
        'photo_received': bool(effective_order_photo(db, card).get('first_received'))}


@app.get('/api/gbu-staff-payroll/roster/{card_id}')
def get_gbu_order_roster(card_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _dispatcher_access(db, user.id):
        raise HTTPException(403, 'Доступ только диспетчеру')
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.id == card_id,
        NumberCardEvent.source == 'gbu', _visible_number_card()))
    if not card:
        raise HTTPException(404, 'Заказ ГБУ не найден')
    return JSONResponse(gbu_order_roster_context(db, card), headers={'Cache-Control':'private, no-store'})


def _date_folders(db: Session, selected: date):
    today = datetime.now(MOSCOW).date()
    # The date ribbon is bounded by the selected calendar month.
    first = selected.replace(day=1)
    if selected.month == 12:
        last = date(selected.year, 12, 31)
    else:
        last = selected.replace(month=selected.month+1, day=1) - timedelta(days=1)
    base = {date.fromordinal(n) for n in range(first.toordinal(), last.toordinal()+1)}

    counts = dict(db.execute(
        select(Order.work_date, func.count(Order.id))
        .where(Order.work_date.in_(list(base)), Order.status != "cancelled")
        .group_by(Order.work_date)
    ).all()) if base else {}

    for manual in db.scalars(select(DispatchDayCounts).where(DispatchDayCounts.work_date.in_(list(base)))):
        original = db.scalars(select(Order).where(Order.work_date == manual.work_date, Order.status != "cancelled")).all()
        counts[manual.work_date] = (manual.private_count if manual.private_count is not None else sum(o.source == "private" for o in original)) + (manual.gbu_count if manual.gbu_count is not None else sum(o.source == "gbu" for o in original))
    items = []
    for d in sorted(base):
        label = d.strftime("%d.%m")
        if d == today:
            label = "Сегодня"
        elif d == today + timedelta(days=1):
            label = "Завтра"
        items.append({"date": d.isoformat(), "label": label, "count": int(counts.get(d, 0))})
    return items



@app.get('/api/date-folders')
def dispatch_date_folders(anchor: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _dispatcher_access(db, user.id):
        raise HTTPException(403, 'Диспетчерская недоступна')
    return JSONResponse({'dates': _date_folders(db, anchor)}, headers={'Cache-Control': 'private, no-store'})


def _day_summary(db: Session, work_date: date):
    orders = db.execute(select(Order).where(Order.work_date == work_date)).scalars().all()
    active = [o for o in orders if o.status != "cancelled"]
    readiness = readiness_summary_data(db, work_date)
    completion = order_completion_data(db, work_date)
    private = completion["sources"]["private"]["expected"]
    gbu = completion["sources"]["gbu"]["expected"]
    requested_by_source = _requested_staff_by_source(db, work_date)
    return {
        "requested_staff": sum(requested_by_source.values()),
        "requested_staff_by_source": requested_by_source,
        "requested_staff_without_carryouts": _requested_staff_from_orders(db, work_date, include_carryouts=False),
        "number_cards_count": db.scalar(select(func.count(NumberCardEvent.id)).where(NumberCardEvent.work_date == work_date, _visible_number_card())),
        "order_completion": completion,
        "photo_reports": photo_report_counts(db, work_date),
        "orders_total": private + gbu,
        "private_count": private,
        "gbu_count": gbu,
        "complete_count": sum(1 for o in active if o.status in {"ready", "sent", "brigadier_confirmed", "closed"}),
        "unassigned_count": sum(1 for o in active if not any(a.role == "brigadier" for a in o.assignments)),
        "ready_now": readiness["totals"]["ready_now"],
        "ready_cutoff": readiness["totals"]["ready_cutoff"],
        "readiness_cutoff_label": readiness["cutoff_label"],
        "brigades_on_contact": readiness["totals"]["brigades_on_contact"],
        "no_response_cutoff": readiness["totals"]["no_response_cutoff"],
    }


async def _notify_order_changed_to_brigadier(db: Session, order: Order, prefix: str = "⚠️ <b>ЗАКАЗ ИЗМЕНЁН</b>"):
    assignment = db.execute(
        select(Assignment)
        .where(Assignment.order_id == order.id, Assignment.role == "brigadier")
        .options(selectinload(Assignment.employee))
    ).scalar_one_or_none()
    if not assignment or not assignment.employee.tg_id:
        return False
    text = (
        f"{prefix}\n\n"
        f"<b>Умерший:</b> {escape(order.deceased_name)}\n"
        f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n"
        f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}\n"
        f"<b>Подача:</b> {order.arrival_time.strftime('%H:%M') if order.arrival_time else '—'}\n"
        f"<b>Категория:</b> {escape(_label_category(order.category))}\n"
        f"<b>Количество:</b> {order.team_size} чел.\n"
        f"<b>Маршрут:</b> {escape(order.route or '—')}\n\n"
        "Проверьте обновлённые данные заказа."
    )
    return await send_telegram_message(assignment.employee.tg_id, text)


def _reset_order_composition(db: Session, order: Order):
    rows = db.execute(select(Assignment).where(Assignment.order_id == order.id)).scalars().all()
    for row in rows:
        db.delete(row)
    order.status = "new"
    order.finalized_at = None
    order.sent_to_brigadier_at = None
    order.brigadier_confirmed_at = None
    order.contact_time = ""


def _apply_order_values(order: Order, payload: dict):
    for key, value in payload.items():
        setattr(order, key, value)
    if "organization" in payload or "team_size" in payload:
        if "марин" in (order.organization or "").lower():
            order.kickback_rub = 1000 if order.team_size == 4 else 1500 if order.team_size == 6 else 0


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


@app.delete("/api/orders/{order_id}")
def delete_order_completely(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    db.delete(order)
    db.commit()
    return {"ok": True, "deleted_id": order_id}


@app.post("/api/orders")
def create_order(data: OrderCreate, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _create_order(db, data)
    db.commit()
    db.refresh(order)
    return _order_dict(order, db)


@app.post("/api/orders/{order_id}/copy-with-composition")
def copy_order_with_composition(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    source = db.execute(
        select(Order).where(Order.id == order_id).options(
            selectinload(Order.assignments).selectinload(Assignment.employee)
        )
    ).scalar_one_or_none()
    if not source:
        raise HTTPException(404, "Заказ не найден")

    assignments = list(source.assignments or [])
    # A copied order is a new job on the same date. Keep the 2-orders-per-day rule.
    blocked = []
    for row in assignments:
        emp = row.employee
        if not emp or not emp.active:
            blocked.append(emp.full_name if emp else f"ID {row.employee_id}")
            continue
        q = (
            select(func.count(Assignment.id))
            .join(Order, Order.id == Assignment.order_id)
            .where(
                Assignment.employee_id == emp.id,
                Order.work_date == source.work_date,
                Order.status != "cancelled",
            )
        )
        count = int(db.execute(q).scalar_one())
        if count >= 2:
            blocked.append(emp.full_name)
    if blocked:
        raise HTTPException(409, "Нельзя скопировать состав: уже 2 заказа у " + ", ".join(blocked))

    new_order = Order(
        public_id=_public_id(),
        source=source.source,
        work_date=source.work_date,
        issue_time=source.issue_time,
        arrival_time=source.arrival_time,
        deceased_name=source.deceased_name,
        route=source.route,
        category=source.category,
        team_size=source.team_size,
        organization=source.organization,
        notes=source.notes,
        kickback_rub=source.kickback_rub,
        agent_tg_id=source.agent_tg_id,
        agent_name=source.agent_name,
        agent_phone=source.agent_phone,
        other_agent_phone=source.other_agent_phone,
        status="assigning" if assignments else "new",
        contact_time="",
    )
    db.add(new_order)
    db.flush()
    for row in assignments:
        db.add(Assignment(
            order_id=new_order.id,
            employee_id=row.employee_id,
            role=row.role,
            created_by_tg_id=user.id,
        ))
    db.commit()
    db.refresh(new_order)
    return _order_dict(new_order, db)


def _phone_key(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _registered_agent_tg_id_by_phone(db: Session, phone: str):
    wanted = _phone_key(phone)
    if not wanted:
        return None
    rows = db.execute(select(Agent).where(Agent.active.is_(True))).scalars().all()
    for row in rows:
        if _phone_key(row.phone) == wanted:
            return row.tg_id
    return None


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
        agent_tg_id=(
            _registered_agent_tg_id_by_phone(
                db,
                data.other_agent_phone.strip() or data.agent_phone.strip(),
            )
            or data.agent_tg_id
        ),
        agent_name=data.agent_name.strip(),
        agent_phone=data.agent_phone.strip(),
        other_agent_phone=data.other_agent_phone.strip(),
        status="new",
    )
    db.add(order)
    db.flush()
    return order


@app.patch("/api/orders/{order_id}")
async def patch_order(order_id: int, data: OrderPatch, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    if order.status in {"closed", "cancelled"}:
        raise HTTPException(409, "Закрытый или отменённый заказ нельзя изменить")
    payload = data.model_dump(exclude_unset=True)
    had_brigadier = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier")).scalar_one_or_none() is not None
    reset_needed = (("work_date" in payload and payload["work_date"] != order.work_date) or ("team_size" in payload and payload["team_size"] != order.team_size))
    if reset_needed:
        _reset_order_composition(db, order)
    _apply_order_values(order, payload)
    assignment_count = int(db.execute(select(func.count(Assignment.id)).where(Assignment.order_id == order.id)).scalar_one())
    if order.status == "new" and assignment_count:
        order.status = "assigning"
    db.commit(); db.refresh(order)
    if had_brigadier and not reset_needed:
        try:
            await _notify_order_changed_to_brigadier(db, order)
        except Exception:
            pass
    return _order_dict(order, db)


@app.post("/api/orders/{order_id}/cancel")
async def cancel_order_admin(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    if order.status == "cancelled":
        return _order_dict(order, db)
    assignment = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier").options(selectinload(Assignment.employee))).scalar_one_or_none()
    brig_tg_id = assignment.employee.tg_id if assignment and assignment.employee else None
    _reset_order_composition(db, order)
    order.status = "cancelled"
    db.commit(); db.refresh(order)
    if brig_tg_id:
        try:
            await send_telegram_message(brig_tg_id, "🚫 <b>ЗАКАЗ ОТМЕНЁН</b>\n\n" f"<b>Умерший:</b> {escape(order.deceased_name)}\n" f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n" f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}")
        except Exception:
            pass
    return _order_dict(order, db)


@app.get("/api/orders/{order_id}/manual-composition")
def manual_composition_data(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    assignments = db.execute(
        select(Assignment).where(Assignment.order_id == order.id).options(selectinload(Assignment.employee))
    ).scalars().all()
    current_brigadier_id = next((a.employee_id for a in assignments if a.role == "brigadier" and a.employee and a.employee.tg_id not in HIDDEN_TG_IDS), None)
    current_member_ids = [a.employee_id for a in assignments if a.role == "member" and a.employee and a.employee.tg_id not in HIDDEN_TG_IDS]
    loads = _workloads(db, order.work_date)
    employees = db.execute(
        select(Employee).where(Employee.active.is_(True), _visible_employee()).order_by(Employee.group_code, Employee.full_name)
    ).scalars().all()
    brigadiers = [
        _employee_dict(emp, loads.get(emp.id, 0))
        for emp in employees if emp.group_code == "brigadier"
    ]
    members = [_employee_dict(emp, loads.get(emp.id, 0)) for emp in employees]
    return {
        "target": max(0, order.team_size - 1),
        "brigadier_id": current_brigadier_id,
        "member_ids": current_member_ids,
        "brigadiers": brigadiers,
        "members": members,
    }


@app.post("/api/orders/{order_id}/manual-composition")
def save_manual_composition(order_id: int, data: ManualComposition, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = _order_by_id_for_update(db, order_id)
    if order.status in {"closed", "cancelled"}:
        raise HTTPException(409, "Закрытый или отменённый заказ нельзя менять")

    member_ids = list(dict.fromkeys(int(x) for x in data.member_ids))
    if len(member_ids) != order.team_size - 1:
        raise HTTPException(409, f"Для заказа на {order.team_size} человек нужно выбрать {order.team_size - 1} сотрудников")
    if data.brigadier_id in member_ids:
        raise HTTPException(409, "Бригадир не может одновременно быть участником состава")

    brig = _employee_for_update(db, data.brigadier_id)
    if brig.group_code != "brigadier":
        raise HTTPException(409, "Бригадир должен быть из группы Бригадиры")

    selected = [brig]
    for emp_id in member_ids:
        selected.append(_employee_for_update(db, emp_id))

    # Manual mode intentionally does not require a readiness mark, but still
    # protects the daily 2-order limit for every selected person.
    for emp in selected:
        _check_capacity(db, emp, order.work_date, exclude_order_id=order.id)

    old_rows = db.execute(select(Assignment).where(Assignment.order_id == order.id)).scalars().all()
    for row in old_rows:
        db.delete(row)
    db.flush()

    db.add(Assignment(order_id=order.id, employee_id=brig.id, role="brigadier", created_by_tg_id=user.id))
    for emp_id in member_ids:
        db.add(Assignment(order_id=order.id, employee_id=emp_id, role="member", created_by_tg_id=user.id))
    order.status = "assigning"
    order.finalized_at = None
    order.sent_to_brigadier_at = None
    db.commit()
    db.refresh(order)
    return _order_dict(order, db)


@app.get("/api/orders/{order_id}/brigadiers")
def brigadier_candidates(order_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Заказ не найден")
    ready = _ready_map(db, order.work_date)
    loads = _workloads(db, order.work_date)
    employees = db.execute(
        select(Employee).where(Employee.active.is_(True), _visible_employee(), Employee.group_code == "brigadier").order_by(Employee.height_cm, Employee.full_name)
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
    brig = next((a.employee for a in current if a.role == "brigadier" and a.employee and a.employee.tg_id not in HIDDEN_TG_IDS), None)
    current_ids = {a.employee_id for a in current}
    brig_height = brig.height_cm if brig else None
    ready = _ready_map(db, order.work_date)
    loads = _workloads(db, order.work_date)
    employees = db.execute(select(Employee).where(Employee.active.is_(True), _visible_employee())).scalars().all()

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
    brig_assignment = next((a for a in assignments if a.role == "brigadier" and a.employee and a.employee.active and a.employee.tg_id not in HIDDEN_TG_IDS), None)
    members = [a.employee for a in assignments if a.role == "member" and a.employee and a.employee.active and a.employee.tg_id not in HIDDEN_TG_IDS]
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

class ReadinessMessageSource(Base):
    __tablename__ = "readiness_message_sources"
    employee_id = Column(Integer, primary_key=True)
    work_date = Column(Date, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    sent_at = Column(BigInteger, nullable=False)
    event_at = Column(BigInteger, nullable=False)
    event_key = Column(String(64), nullable=False)
    applied_at = Column(DateTime, nullable=False)


class ManualReadiness(Base):
    __tablename__ = "manual_readiness_entries"
    id = Column(Integer, primary_key=True)
    work_date = Column(Date, nullable=False, index=True)
    identity_key = Column(String(64), nullable=False)
    full_name = Column(String(120), nullable=False)
    metro = Column(String(120), nullable=False)
    height_cm = Column(Integer, nullable=False)
    group_code = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    reported_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_by_tg_id = Column(BigInteger, nullable=False)
    __table_args__ = (UniqueConstraint("work_date", "identity_key", name="uq_manual_readiness_identity"),)


def manual_readiness_identity(name, metro, height):
    values = [" ".join(str(x or "").casefold().replace("ё", "е").split()) for x in (name, metro, height)]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


class EmployeeProfileOverride(Base):
    __tablename__ = "employee_profile_overrides"
    employee_id = Column(Integer, primary_key=True)
    full_name = Column(String(120), nullable=False)
    metro = Column(String(120), nullable=False)
    height_cm = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EmployeePhoneOverride(Base):
    __tablename__ = "employee_phone_overrides"
    employee_id = Column(Integer, primary_key=True)
    phone = Column(String(40), nullable=False)


class EmployeeTelegramOverride(Base):
    __tablename__ = "employee_telegram_overrides"
    employee_id = Column(Integer, primary_key=True)
    telegram_username = Column(String(80), nullable=False)


def _normalize_employee_telegram(value):
    value = value.strip()
    value = re.sub(r"^(?:https?://)?(?:www\.)?t\.me/", "", value, flags=re.I)
    value = value.removeprefix("@").rstrip("/")
    if value and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", value):
        raise HTTPException(422, "Укажите Telegram в формате @username или https://t.me/username")
    return value


def _apply_profile_override(db, emp):
    override = db.get(EmployeeProfileOverride, emp.id) if emp.id else None
    if override:
        emp.full_name, emp.metro, emp.height_cm = override.full_name, override.metro, override.height_cm
    phone_override = db.get(EmployeePhoneOverride, emp.id) if emp.id else None
    if phone_override is not None:
        emp.phone = phone_override.phone
    telegram_override = db.get(EmployeeTelegramOverride, emp.id) if emp.id else None
    if telegram_override is not None:
        emp.telegram_username = telegram_override.telegram_username
    if emp.group_code == "brigadier":
        emp.phone = brigadier_phone8(emp.phone)
        if phone_override is not None:
            phone_override.phone = emp.phone


def _save_employee_profile(db, emp, name, metro, height, phone=None, telegram_username=None):
    if emp.tg_id in HIDDEN_TG_IDS or not emp.active:
        raise HTTPException(404, "Сотрудник не найден")
    telegram_value = _normalize_employee_telegram(telegram_username) if telegram_username is not None else None
    override = db.get(EmployeeProfileOverride, emp.id)
    if override is None:
        override = EmployeeProfileOverride(employee_id=emp.id)
        db.add(override)
    override.full_name, override.metro, override.height_cm = name, metro, height
    override.updated_at = datetime.utcnow()
    emp.full_name, emp.metro, emp.height_cm = name, metro, height
    if phone is not None:
        value = re.sub(r"[\s()\-]", "", phone)
        if value and not re.fullmatch(r"\+?[0-9]{7,15}", value):
            raise HTTPException(422, "Укажите корректный номер телефона")
        value = brigadier_phone8(value)
        saved = db.get(EmployeePhoneOverride, emp.id)
        if saved is None:
            saved = EmployeePhoneOverride(employee_id=emp.id)
            db.add(saved)
        saved.phone = emp.phone = value
    if telegram_value is not None:
        saved_telegram = db.get(EmployeeTelegramOverride, emp.id)
        if saved_telegram is None:
            saved_telegram = EmployeeTelegramOverride(employee_id=emp.id)
            db.add(saved_telegram)
        saved_telegram.telegram_username = emp.telegram_username = telegram_value


def _correction_employee(db, row):
    if row.identity_key.startswith("employee:"):
        try:
            return db.get(Employee, int(row.identity_key.split(":", 1)[1]))
        except ValueError:
            return None
    candidates = [emp for emp in db.execute(select(Employee)).scalars()
                  if emp.active and emp.tg_id not in HIDDEN_TG_IDS and
                  manual_readiness_identity(emp.full_name, emp.metro, emp.height_cm) == row.identity_key]
    if len(candidates) != 1:
        return None
    emp = candidates[0]
    conflict = db.execute(select(ManualReadiness).where(
        ManualReadiness.work_date == row.work_date,
        ManualReadiness.identity_key == f"employee:{emp.id}")).scalar_one_or_none()
    if conflict is not None and conflict.id != row.id:
        return None
    row.identity_key = f"employee:{emp.id}"
    return emp


def _recover_employee_profiles(db):
    # Latest linked correction wins once. Never guess a Telegram identity by name.
    corrections = db.execute(select(ManualReadiness).order_by(
        ManualReadiness.reported_at.desc(), ManualReadiness.id.desc())).scalars().all()
    for row in corrections:
        emp = _correction_employee(db, row)
        if emp and emp.active and emp.tg_id not in HIDDEN_TG_IDS and db.get(EmployeeProfileOverride, emp.id) is None:
            _save_employee_profile(db, emp, row.full_name, row.metro, row.height_cm)
    db.flush()


class EmployeeProfileInput(BaseModel):
    telegram_username: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=40)
    full_name: str = Field(min_length=1, max_length=120)
    metro: str = Field(max_length=100)
    height_cm: int | None = Field(default=None, ge=100, le=250)


@app.patch("/api/employees/{employee_id}/profile")
def edit_employee_profile(employee_id: int, data: EmployeeProfileInput,
                          db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    emp = db.get(Employee, employee_id)
    if emp is None or not emp.active or emp.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(404, "Сотрудник не найден")
    name, metro = " ".join(data.full_name.split()), " ".join(data.metro.split())
    if not name:
        raise HTTPException(422, "Укажите имя сотрудника")
    _save_employee_profile(db, emp, name, metro, data.height_cm, data.phone, data.telegram_username)
    db.commit()
    return _employee_dict(emp)


class ManualReadinessInput(BaseModel):
    telegram_username: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=40)
    work_date: date
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    status: Literal["ready", "not_ready", "day_off", "responded", "no_response"]
    full_name: str = Field(min_length=1, max_length=120)
    metro: str = Field(min_length=1, max_length=100)
    height_cm: int = Field(ge=100, le=250)


@app.post("/api/readiness/manual")
def save_manual_readiness(data: ManualReadinessInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    name, metro = " ".join(data.full_name.split()), " ".join(data.metro.split())
    if not name or not metro:
        raise HTTPException(422, "Укажите имя и метро сотрудника")
    key = manual_readiness_identity(name, metro, data.height_cm)
    candidates = [emp for emp in db.execute(select(Employee)).scalars()
                  if emp.active and emp.tg_id not in HIDDEN_TG_IDS and
                  manual_readiness_identity(emp.full_name, emp.metro, emp.height_cm) == key]
    if len(candidates) > 1:
        raise HTTPException(409, "Есть несколько сотрудников с такими данными. Измените нужного через кнопку справа.")
    if candidates:
        key = f"employee:{candidates[0].id}"
    row = db.execute(select(ManualReadiness).where(
        ManualReadiness.work_date == data.work_date, ManualReadiness.identity_key == key
    ).with_for_update()).scalar_one_or_none()
    if row is None:
        row = ManualReadiness(work_date=data.work_date, identity_key=key)
        db.add(row)
    return _update_readiness_correction(db, row, data, user)


@app.delete("/api/readiness/manual/{entry_id}")
def delete_manual_readiness(entry_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    row = db.get(ManualReadiness, entry_id)
    if row is None:
        raise HTTPException(404, "Ручная запись уже удалена")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.patch("/api/readiness/manual/{entry_id}")
def edit_manual_readiness(entry_id: int, data: ManualReadinessInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    row = db.get(ManualReadiness, entry_id)
    if row is None or row.work_date != data.work_date:
        raise HTTPException(404, "Запись на выбранную дату не найдена")
    return _update_readiness_correction(db, row, data, user)


@app.patch("/api/readiness/employee/{employee_id}")
def edit_employee_readiness(employee_id: int, data: ManualReadinessInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    emp = db.get(Employee, employee_id)
    if emp is None or not emp.active or emp.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(404, "Сотрудник не найден")
    key = f"employee:{emp.id}"
    row = db.execute(select(ManualReadiness).where(
        ManualReadiness.work_date == data.work_date, ManualReadiness.identity_key == key
    )).scalar_one_or_none()
    if row is None:
        row = ManualReadiness(work_date=data.work_date, identity_key=key)
        db.add(row)
    return _update_readiness_correction(db, row, data, user)


def _update_readiness_correction(db, row, data, user):
    name, metro = " ".join(data.full_name.split()), " ".join(data.metro.split())
    if not name or not metro:
        raise HTTPException(422, "Укажите имя и метро сотрудника")
    row.full_name, row.metro, row.height_cm = name, metro, data.height_cm
    row.group_code, row.status = data.group_code, data.status
    row.reported_at, row.created_by_tg_id = datetime.utcnow(), user.id
    emp = _correction_employee(db, row)
    if emp is None:
        if row.identity_key.startswith("employee:"):
            raise HTTPException(404, "Сотрудник не найден")
        emp = Employee(full_name=name, metro=metro, height_cm=data.height_cm,
                       group_code=data.group_code, active=True, tg_id=None)
        db.add(emp)
        db.flush()
        row.identity_key = f"employee:{emp.id}"
    _save_employee_profile(db, emp, name, metro, data.height_cm, data.phone, data.telegram_username)
    # Identity persists; readiness status remains date-specific.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Запись уже изменена. Обновите список и повторите.")
    return {"ok": True, "id": row.id}


def apply_manual_readiness(db, work_date, groups, cutoff):
    """Merge date-scoped statuses using permanent employee profiles."""
    statuses = ("ready", "not_ready", "day_off", "responded", "no_response")
    manual_rows = db.execute(select(ManualReadiness).where(ManualReadiness.work_date == work_date)).scalars().all()
    for row in manual_rows:
        matches = []
        for group in groups.values():
            for status in statuses:
                for item in group[status]:
                    if (f"employee:{item.get('id')}" == row.identity_key or
                        manual_readiness_identity(item.get("full_name"), item.get("metro"), item.get("height_cm")) == row.identity_key):
                        matches.append((group[status], item))
        # Do not silently collapse two registered people with identical details.
        if len(matches) > 1:
            continue
        item = dict(matches[0][1]) if matches else {
            "id": -row.id, "tg_id": None, "full_name": row.full_name,
            "display_name": row.full_name, "metro": row.metro,
            "height_cm": row.height_cm, "has_car": False,
        }
        if matches:
            reported = item.get("reported_at")
            if reported and datetime.fromisoformat(reported) > row.reported_at:
                continue
            matches[0][0].remove(matches[0][1])
        item.update(full_name=row.full_name, display_name=row.full_name,
                    metro=row.metro, height_cm=row.height_cm,
                    group_code=row.group_code, group_label=GROUP_LABELS[row.group_code],
                    readiness_status=row.status, manual_entry_id=row.id,
                    reported_at=row.reported_at.isoformat())
        linked = _correction_employee(db, row)
        if linked:
            item.update(full_name=linked.full_name, display_name=_employee_dict(linked)["display_name"],
                        metro=linked.metro, height_cm=linked.height_cm,
                        phone=_employee_dict(linked)["phone"], telegram_username=linked.telegram_username or "",
                        telegram_url=_employee_dict(linked)["telegram_url"])
        item["cutoff_status"] = row.status if row.reported_at <= cutoff else item.get("cutoff_status", "no_response")
        groups[row.group_code][row.status].append(item)
    people = [x for group in groups.values() for status in statuses for x in group[status]]
    return {
        "employees": len(people),
        "ready_now": sum(x["readiness_status"] == "ready" for x in people),
        "ready_cutoff": sum(x["cutoff_status"] == "ready" for x in people),
        "no_response_cutoff": sum(x["cutoff_status"] == "no_response" for x in people),
    }




def _readiness_history_data(db: Session, work_date: date, limit: int = 60):
    rows = db.execute(
        select(ImportLog)
        .where(ImportLog.kind == "readiness_change")
        .order_by(ImportLog.created_at.desc())
        .limit(max(1, min(int(limit), 200)))
    ).scalars().all()
    labels = {"ready": "Готов", "not_ready": "На основной", "day_off": "Выходной", "responded": "Отписался", "no_response": "Не отписался"}
    result = []
    for row in rows:
        try:
            data = json.loads(row.result_json or "{}")
        except Exception:
            continue
        if data.get("work_date") != work_date.isoformat():
            continue
        if str(data.get("tg_id") or "") in {str(tg_id) for tg_id in HIDDEN_TG_IDS}:
            continue
        changed = row.created_at
        try:
            changed_label = changed.replace(tzinfo=timezone.utc).astimezone(MOSCOW).strftime("%d.%m %H:%M")
        except Exception:
            changed_label = changed.strftime("%d.%m %H:%M") if changed else ""
        old_status = data.get("old_status") or "no_response"
        new_status = data.get("new_status") or "no_response"
        result.append({
            "tg_id": data.get("tg_id"),
            "full_name": data.get("full_name") or "—",
            "group_code": data.get("group_code") or "",
            "group_label": GROUP_LABELS.get(data.get("group_code") or "", data.get("group_code") or "—"),
            "old_status": old_status,
            "new_status": new_status,
            "old_label": labels.get(old_status, old_status),
            "new_label": labels.get(new_status, new_status),
            "source": data.get("source") or "",
            "changed_at": row.created_at.isoformat() if row.created_at else None,
            "changed_at_label": changed_label,
        })
    return result


@app.get("/api/readiness/history")
def readiness_history(work_date: date, limit: int = 60, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    return _readiness_history_data(db, work_date, limit)


@app.get("/api/readiness/summary")
def readiness_summary(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    return readiness_summary_data(db, work_date)


@app.post("/api/readiness/brigade-contact/{employee_id}")
def manual_brigade_contact(employee_id: int, work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    employee = db.execute(select(Employee).where(
        Employee.id == employee_id, Employee.active.is_(True),
        Employee.group_code == "brigadier", _visible_employee(),
    )).scalar_one_or_none()
    if not employee:
        raise HTTPException(404, "Бригадир не найден")
    if not employee.tg_id:
        raise HTTPException(400, "У бригадира не указан Telegram ID")
    cards = db.scalars(select(NumberCardEvent).where(NumberCardEvent.tg_id == employee.tg_id,
        NumberCardEvent.work_date == work_date, _visible_number_card())).all()
    if len(cards) != 1:
        raise HTTPException(409, "Выберите конкретный заказ для отметки связи")
    return manual_order_contact(cards[0].id, work_date, db, user)


class OrderBrigadeContact(Base):
    __tablename__ = "order_brigade_contacts"
    event_id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    confirmed = Column(Boolean, nullable=False, default=False)
    updated_by_tg_id = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def _order_contact_states(db, work_date):
    return {row.event_id: bool(row.confirmed) for row in db.scalars(
        select(OrderBrigadeContact).join(NumberCardEvent,
            NumberCardEvent.id == OrderBrigadeContact.event_id).where(
            NumberCardEvent.work_date == work_date,
            NumberCardEvent.tg_id == OrderBrigadeContact.tg_id,
            NumberCardEvent.tg_id.notin_(HIDDEN_TG_IDS), _visible_number_card()))}


def _set_order_contact(db, card, confirmed, actor):
    row = db.get(OrderBrigadeContact, card.id)
    if row is None:
        row = OrderBrigadeContact(event_id=card.id)
        db.add(row)
    row.tg_id = card.tg_id
    row.confirmed = confirmed
    row.updated_by_tg_id = actor
    row.updated_at = datetime.utcnow()
    return row


def _migrate_order_contact_marks(db):
    # Preserve checks already shown by the old person/day rule, once, on the
    # existing cards. A later card must never inherit another order's check-in.
    kind = "order_contact_migration_v1"
    if db.scalar(select(ImportLog.id).where(ImportLog.kind == kind).limit(1)):
        return
    grouped = defaultdict(list)
    for card in db.scalars(select(NumberCardEvent).where(_visible_number_card())):
        grouped[(card.work_date, card.tg_id)].append(card)
    old = {day: brigade_contact_states(db, day) for day in {day for day, _ in grouped}}
    for (day, uid), cards in grouped.items():
        for card in cards:
            if db.get(OrderBrigadeContact, card.id) is None:
                _set_order_contact(db, card, bool(old[day].get(uid)), uid)
    db.add(ImportLog(kind=kind, created_by_tg_id=0, raw_text="Связь переведена на отдельные заказы",
                     result_json="{}"))


@app.post("/api/readiness/order-contact/{event_id}")
def manual_order_contact(event_id: int, work_date: date, db: Session = Depends(get_db),
                         user: TelegramUser = Depends(require_admin)):
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.id == event_id,
        NumberCardEvent.work_date == work_date, _visible_number_card()).with_for_update())
    if not card or card.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(404, "Заказ не найден")
    job = db.get(NumberReassignment, card.id)
    if job and job.status == "pending":
        raise HTTPException(409, "Дождитесь завершения замены бригадира")
    _set_order_contact(db, card, True, user.id)
    db.add(ImportLog(kind="order_contact_manual", created_by_tg_id=user.id,
        raw_text="Ручная отметка связи по заказу",
        result_json=json.dumps({"event_id":card.id,"work_date":work_date.isoformat()})))
    db.commit()
    return {"ok":True,"event_id":card.id,"on_contact":True}


def brigade_contact_states(db: Session, work_date: date):
    states = {}
    logs = db.execute(select(ImportLog).where(ImportLog.kind == "brigade_contact")
                      .order_by(ImportLog.created_at.desc(), ImportLog.id.desc())).scalars().all()
    for log in logs:
        try:
            record = json.loads(log.result_json or "{}")
            if record.get("work_date") == work_date.isoformat():
                tg_id = int(record.get("tg_id") or 0)
                if tg_id and tg_id not in states:
                    states[tg_id] = not record.get("deleted", False)
        except (ValueError, TypeError, AttributeError):
            continue
    return states


class BrigadeContactDelete(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=1000)


@app.post("/api/readiness/brigade-contacts/delete")
def delete_brigade_contacts(data: BrigadeContactDelete, work_date: date,
                           db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    cards = db.scalars(select(NumberCardEvent).where(NumberCardEvent.id.in_(set(data.event_ids)),
        NumberCardEvent.work_date == work_date, _visible_number_card()).order_by(NumberCardEvent.id)
        .with_for_update()).all()
    if len(cards) != len(set(data.event_ids)):
        raise HTTPException(409, "Список заказов изменился. Откройте его заново")
    if any((job := db.get(NumberReassignment, c.id)) and job.status == "pending" for c in cards):
        raise HTTPException(409, "Дождитесь завершения замены бригадира")
    for card in cards:
        _set_order_contact(db, card, False, user.id)
        db.add(ImportLog(kind="order_contact_removed", created_by_tg_id=user.id,
            raw_text="Удаление отметки связи владельцем",
            result_json=json.dumps({"event_id":card.id,"tg_id":card.tg_id,"work_date":work_date.isoformat(),
                                    "deleted": True}, ensure_ascii=False)))
    db.commit()
    return {"ok":True,"deleted":len(cards),"remaining":sum(_order_contact_states(db, work_date).values())}


def brigades_contact_data(db: Session, work_date: date):
    states = _order_contact_states(db, work_date)
    counts = defaultdict(int)
    for card in db.scalars(select(NumberCardEvent).where(
            NumberCardEvent.work_date == work_date, _visible_number_card())):
        if states.get(card.id) and card.tg_id not in HIDDEN_TG_IDS:
            counts[card.tg_id] += 1
    ids = set(counts)
    if not ids:
        return []
    employees = db.execute(select(Employee).where(
        Employee.tg_id.in_(ids), Employee.active.is_(True), Employee.group_code == "brigadier",
        _visible_employee(),
    ).order_by(Employee.full_name)).scalars().all()
    # The people list aggregates only independently confirmed orders.
    unique = {emp.tg_id: _employee_dict(emp) for emp in employees}
    for tg_id, person in unique.items():
        person["contact_order_count"] = int(counts.get(tg_id, 0))
    return list(unique.values())


def readiness_summary_data(db: Session, work_date: date):
    employees = db.execute(select(Employee).where(Employee.active.is_(True), _visible_employee()).order_by(Employee.group_code, Employee.full_name)).scalars().all()
    ready_rows = _ready_map(db, work_date)
    cutoff_label = _readiness_cutoff_label(db)
    cutoff = _cutoff_utc_naive(work_date, cutoff_label)

    groups = {code: {"label": label, "ready": [], "not_ready": [], "day_off": [], "responded": [], "no_response": []} for code, label in GROUP_LABELS.items()}
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
        groups.setdefault(emp.group_code, {"label": emp.group_code, "ready": [], "not_ready": [], "day_off": [], "responded": [], "no_response": []})
        groups[emp.group_code].setdefault(current, groups[emp.group_code]["no_response"] if current == "no_response" else [])
        if current in {"ready", "not_ready", "day_off", "responded"}:
            groups[emp.group_code][current].append(item)
        else:
            groups[emp.group_code]["no_response"].append(item)

    manual_totals = apply_manual_readiness(db, work_date, groups, cutoff)
    on_contact = brigades_contact_data(db, work_date)
    manual_totals["brigades_on_contact"] = sum(_order_contact_states(db, work_date).values())
    manual_totals["brigades_expected"] = sum(
        x["expected"] for x in order_completion_data(db, work_date)["sources"].values())

    for g in groups.values():
        g["counts"] = {k: len(g[k]) for k in ["ready", "not_ready", "day_off", "responded", "no_response"]}
        # A person can have reported now but still have missed the first summary.
        g["no_response_cutoff"] = [
            x for bucket in [g["ready"], g["not_ready"], g["day_off"], g["responded"], g["no_response"]]
            for x in bucket if x["cutoff_status"] == "no_response"
        ]

    return {
        "work_date": work_date.isoformat(),
        "cutoff_label": cutoff_label,
        "totals": manual_totals,
        "brigades_on_contact": on_contact,
        "groups": groups,
    }


@app.get("/api/employees")
def list_employees(group: str | None = None, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    q = select(Employee).where(Employee.active.is_(True), _visible_employee())
    if group and group != "all":
        q = q.where(Employee.group_code == group)
    rows = db.execute(q.order_by(Employee.full_name)).scalars().all()
    manage = _is_cabinet_owner(db, user.id)
    return [{**_employee_dict(x), **({"cabinet_access": cabinet_access_info(db, x)} if manage else {})}
            for x in rows]


@app.post("/api/employees/bulk")
def bulk_employees(items: list[EmployeeSync], db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    result = []
    for item in items:
        emp = _upsert_employee(db, item)
        result.append(emp)
    db.commit()
    return [_employee_dict(x) for x in result if x.tg_id not in HIDDEN_TG_IDS]


@app.delete("/api/employees/{employee_id}")
def delete_employee(employee_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _is_owner(db, user.id):
        raise HTTPException(403, "Удаление сотрудников доступно только владельцу")
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(404, "Сотрудник не найден")
    if emp.tg_id == user.id:
        raise HTTPException(400, "Свой аккаунт удалить нельзя")
    if db.get(EmployeeDeletion, employee_id) is None:
        db.add(EmployeeDeletion(employee_id=employee_id, tg_id=emp.tg_id, deleted_by_tg_id=user.id))
    # Keep accounting and previous assignments intact; remove only from active staff.
    emp.active = False
    emp.is_cashier = False
    db.commit()
    return {"ok": True, "employee_id": employee_id}


# ---------- BOT integration ----------

class StaffReferenceItem(BaseModel):
    full_name: str
    phone: str = ""
    telegram_username: str = ""
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    metro: str = ""


def _reference_phone(value: str | None) -> str:
    digits = "".join(c for c in str(value or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


@app.post("/api/bot/employees/reference-import", dependencies=[Depends(_bot_key)])
def bot_import_staff_reference(items: list[StaffReferenceItem], db: Session = Depends(get_db)):
    if len(items) > 500:
        raise HTTPException(400, "Слишком большой справочник")
    stats = {"created": 0, "updated": 0, "skipped": 0}
    employees = list(db.execute(select(Employee)).scalars().all())
    for item in items:
        username = item.telegram_username.strip().lstrip("@").lower()
        phone = _reference_phone(item.phone)
        if username == "simachok_1" or phone == "79154967383":
            stats["skipped"] += 1
            continue
        matches = [x for x in employees if
            (username and str(x.telegram_username or "").lstrip("@").lower() == username)
            or (phone and _reference_phone(x.phone) == phone)]
        if len(matches) > 1:
            stats["skipped"] += 1
            continue
        if matches:
            emp = matches[0]
            if emp.tg_id in HIDDEN_TG_IDS or emp.tg_id == 8038387894:
                stats["skipped"] += 1
                continue
            # The PDF is an older directory: never overwrite current role or activation.
            if not emp.phone:
                emp.phone = item.phone
            if not emp.telegram_username:
                emp.telegram_username = item.telegram_username.strip().lstrip("@")
            if not emp.metro:
                emp.metro = item.metro
            _apply_profile_override(db, emp)
            stats["updated"] += 1
        else:
            emp = Employee(
                tg_id=None, full_name=item.full_name.strip(), phone=brigadier_phone8(item.phone) if item.group_code == "brigadier" else item.phone,
                telegram_username=item.telegram_username.strip().lstrip("@"),
                group_code=item.group_code, metro=item.metro, active=False,
                is_cashier=False,
            )
            db.add(emp)
            employees.append(emp)
            stats["created"] += 1
    db.commit()
    return {"ok": True, **stats}


@app.post("/api/bot/employees/sync", dependencies=[Depends(_bot_key)])
def bot_employee_sync(data: EmployeeSync, db: Session = Depends(get_db)):
    emp = _upsert_employee(db, data)
    db.commit(); db.refresh(emp)
    return _employee_dict(emp)


@app.get("/api/bot/employees/known", dependencies=[Depends(_bot_key)])
def bot_known_employees(db: Session = Depends(get_db)):
    rows = db.execute(select(Employee).where(Employee.tg_id.is_not(None), _visible_employee()).order_by(Employee.full_name)).scalars().all()
    return [{
        "tg_id": x.tg_id,
        "full_name": x.full_name,
        "telegram_username": x.telegram_username or "",
        "group_code": x.group_code,
        "metro": x.metro or "",
        "height_cm": x.height_cm,
        "is_cashier": bool(x.is_cashier),
        "active": x.active,
    } for x in rows]


@app.post("/api/bot/access/owner", dependencies=[Depends(_bot_key)])
def bot_sync_owner(data: OwnerSync, db: Session = Depends(get_db)):
    row = db.execute(select(AccessUser).where(AccessUser.tg_id == data.tg_id)).scalar_one_or_none()
    if not row:
        row = AccessUser(tg_id=data.tg_id, full_name=data.full_name, role="owner", active=True); db.add(row)
    else:
        row.full_name = data.full_name or row.full_name; row.role = "owner"; row.active = True
    # This singleton follows the bot's actual owner, not the list of admins.
    cabinet = db.get(OwnerCabinetConfig, 1)
    if cabinet is None:
        cabinet = OwnerCabinetConfig(id=1, owner_tg_id=data.tg_id)
        db.add(cabinet)
    cabinet.owner_tg_id = data.tg_id
    if data.bot_username:
        cabinet.bot_username = data.bot_username
    db.commit(); return {"ok": True}


@app.post("/api/bot/brigadiers/{tg_id}/cashier", dependencies=[Depends(_bot_key)])
def bot_toggle_cashier(tg_id: int, data: CashierToggle, db: Session = Depends(get_db)):
    if tg_id in HIDDEN_TG_IDS:
        raise HTTPException(404, "Бригадир не найден")
    emp = db.execute(select(Employee).where(Employee.tg_id == tg_id, Employee.group_code == "brigadier")).scalar_one_or_none()
    if not emp: raise HTTPException(404, "Бригадир не найден в Диспетчерской")
    emp.is_cashier = bool(data.is_cashier) and not _cashier_excluded(db, emp); db.commit()
    return {"ok": True, "tg_id": tg_id, "is_cashier": bool(emp.is_cashier)}


@app.post("/api/bot/cash", dependencies=[Depends(_bot_key)])
def bot_create_cash_entry(data: CashEntryCreate, db: Session = Depends(get_db)):
    emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id, Employee.group_code == "brigadier").with_for_update()).scalar_one_or_none()
    if not emp or not emp.active: raise HTTPException(404, "Бригадир не найден")
    if not emp.is_cashier or _cashier_excluded(db, emp): raise HTTPException(403, "Этот бригадир не назначен кассовым")
    fingerprint = hashlib.sha256(data.model_dump_json(exclude={'request_id'}).encode()).hexdigest()
    previous = db.get(CashEntrySubmission, (data.tg_id, data.request_id)) if data.request_id else None
    if previous:
        if previous.payload_hash != fingerprint:
            raise HTTPException(409, 'Эта запись уже сохранена с другими данными')
        row = db.get(CashEntry, previous.entry_id)
        if row is None:
            raise HTTPException(409, 'Эта запись уже была сохранена и затем удалена')
    else:
        row = CashEntry(work_date=data.work_date, brigadier_tg_id=data.tg_id, brigadier_name=emp.full_name, category=data.category, team_size=data.team_size, commission_rub=data.commission_rub, kickback_rub=data.kickback_rub, reserve_count=data.reserve_count, notes=data.notes.strip())
        db.add(row)
        db.flush()
        if data.request_id:
            db.add(CashEntrySubmission(tg_id=data.tg_id, request_id=data.request_id,
                entry_id=row.id, payload_hash=fingerprint))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            previous = db.get(CashEntrySubmission, (data.tg_id, data.request_id)) if data.request_id else None
            row = db.get(CashEntry, previous.entry_id) if previous else None
            if not previous or previous.payload_hash != fingerprint or row is None:
                raise HTTPException(409, 'Не удалось сохранить запись. Повторите попытку.')
        db.refresh(row)
    start, end = _period_bounds(data.work_date)
    period_rows = db.execute(select(CashEntry).where(CashEntry.brigadier_tg_id == data.tg_id, CashEntry.work_date >= start, CashEntry.work_date <= end)).scalars().all()
    return {"ok": True, "entry": _cash_row_dict(row), "period": {"start": start.isoformat(), "end": end.isoformat(), "commission_rub": sum(x.commission_rub or 0 for x in period_rows), "kickback_rub": sum(x.kickback_rub or 0 for x in period_rows), "cash_rub": sum(x.commission_rub or 0 for x in period_rows), "total_rub": sum(x.commission_rub or 0 for x in period_rows), "entries": len(period_rows)}}


@app.post("/api/bot/cash/day/delete", dependencies=[Depends(_bot_key)])
def bot_delete_own_cash_day(data: BrigadierCashDayDelete, db: Session = Depends(get_db)):
    emp = db.execute(
        select(Employee).where(
            Employee.tg_id == data.tg_id,
            Employee.group_code == "brigadier",
        )
    ).scalar_one_or_none()
    if not emp or not emp.active or not emp.is_cashier or _cashier_excluded(db, emp):
        raise HTTPException(403, "Удаление кассы недоступно")

    rows = db.execute(
        select(CashEntry).where(
            CashEntry.brigadier_tg_id == data.tg_id,
            CashEntry.work_date == data.work_date,
        )
    ).scalars().all()

    deleted = len(rows)
    commission_rub = sum(int(row.commission_rub or 0) for row in rows)
    kickback_rub = sum(int(row.kickback_rub or 0) for row in rows)

    for row in rows:
        db.delete(row)
    db.commit()

    return {
        "ok": True,
        "work_date": data.work_date.isoformat(),
        "deleted": deleted,
        "commission_rub": commission_rub,
        "kickback_rub": kickback_rub,
    }


@app.post("/api/bot/cash/delete-entry", dependencies=[Depends(_bot_key)])
def bot_delete_cash_entry_simple(data: BrigadierCashDeleteRequest, db: Session = Depends(get_db)):
    row = db.get(CashEntry, int(data.entry_id))
    if not row:
        raise HTTPException(404, "Запись кассы не найдена")
    if int(row.brigadier_tg_id) != int(data.tg_id):
        raise HTTPException(403, "Нельзя удалить чужую запись кассы")

    payload = {
        "id": int(row.id),
        "work_date": row.work_date.isoformat(),
        "commission_rub": int(row.commission_rub or 0),
        "kickback_rub": int(row.kickback_rub or 0),
    }
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": payload}


@app.post("/api/bot/cash/{entry_id}/delete", dependencies=[Depends(_bot_key)])
def bot_delete_own_cash_entry(entry_id: int, data: BrigadierCashEntryDelete, db: Session = Depends(get_db)):
    row = db.get(CashEntry, entry_id)
    if not row or int(row.brigadier_tg_id) != int(data.tg_id):
        raise HTTPException(404, "Запись кассы не найдена")

    emp = db.execute(
        select(Employee).where(
            Employee.tg_id == data.tg_id,
            Employee.group_code == "brigadier",
        )
    ).scalar_one_or_none()
    if not emp or not emp.active or not emp.is_cashier or _cashier_excluded(db, emp):
        raise HTTPException(403, "Удаление кассы недоступно")

    payload = {
        "id": row.id,
        "work_date": row.work_date.isoformat(),
        "commission_rub": int(row.commission_rub or 0),
        "kickback_rub": int(row.kickback_rub or 0),
    }
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": payload}


@app.get("/api/bot/cash/period", dependencies=[Depends(_bot_key)])
def bot_cash_period(tg_id: int, on_date: date, db: Session = Depends(get_db)):
    start, end = _period_bounds(on_date)
    rows = _all_cash_rows(db, start, end, tg_id=tg_id)
    return {"start": start.isoformat(), "end": end.isoformat(), "entries": [_cash_row_dict(x) for x in rows], "commission_rub": sum(x.commission_rub or 0 for x in rows), "kickback_rub": sum(x.kickback_rub or 0 for x in rows), "cash_rub": sum(x.commission_rub or 0 for x in rows), "total_rub": sum(x.commission_rub or 0 for x in rows)}


@app.get("/api/bot/cash/{entry_id}", dependencies=[Depends(_bot_key)])
def bot_cash_entry(entry_id: int, tg_id: int, db: Session = Depends(get_db)):
    row = db.get(CashEntry, entry_id)
    if not row or int(row.brigadier_tg_id) != int(tg_id):
        raise HTTPException(404, "Запись кассы не найдена")
    return _cash_row_dict(row)


@app.post("/api/bot/cash/{entry_id}/edit", dependencies=[Depends(_bot_key)])
def bot_edit_own_cash(entry_id: int, data: BrigadierCashPatch, db: Session = Depends(get_db)):
    row = db.get(CashEntry, entry_id)
    if not row or int(row.brigadier_tg_id) != int(data.tg_id):
        raise HTTPException(404, "Запись кассы не найдена")
    emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id, Employee.group_code == "brigadier")).scalar_one_or_none()
    if not emp or not emp.active or not emp.is_cashier or _cashier_excluded(db, emp):
        raise HTTPException(403, "Изменение кассы недоступно")
    payload = data.model_dump(exclude_unset=True)
    payload.pop("tg_id", None)
    for key, value in payload.items():
        if value is not None:
            setattr(row, key, value)
    db.commit(); db.refresh(row)
    return {"ok": True, "entry": _cash_row_dict(row)}


@app.get("/api/cash")
def owner_cash_day(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    rows = _all_cash_rows(db, work_date, work_date)
    start, end = _period_bounds(work_date)
    period_rows = _all_cash_rows(db, start, end)
    return {"work_date": work_date.isoformat(), "rows": [_cash_row_dict(x, db) for x in rows], "day": {"commission_rub": sum(x.commission_rub or 0 for x in rows), "kickback_rub": sum(x.kickback_rub or 0 for x in rows), "cash_rub": sum(x.commission_rub or 0 for x in rows), "total_rub": sum(x.commission_rub or 0 for x in rows)}, "period": {"start": start.isoformat(), "end": end.isoformat(), "commission_rub": sum(x.commission_rub or 0 for x in period_rows), "kickback_rub": sum(x.kickback_rub or 0 for x in period_rows), "cash_rub": sum(x.commission_rub or 0 for x in period_rows), "total_rub": sum(x.commission_rub or 0 for x in period_rows), "entries": len(period_rows)}}


def _cash_day_check_data(db: Session, work_date: date) -> dict:
    plan = db.get(CashDayCheckPlan, work_date)
    overrides = json.loads(plan.overrides_json) if plan else {}
    cashiers = db.scalars(select(Employee).where(
        Employee.group_code == 'brigadier', Employee.is_cashier.is_(True),
        _visible_employee())).all()
    cashiers = {int(e.tg_id): e for e in cashiers if e.tg_id and not _cashier_excluded(db, e)}
    expected = defaultdict(int)
    for card in db.scalars(select(NumberCardEvent).where(
            NumberCardEvent.work_date == work_date, NumberCardEvent.source == 'private',
            _visible_number_card())):
        if int(card.tg_id) in cashiers:
            expected[int(card.tg_id)] += 1
    submitted = defaultdict(int)
    amounts = defaultdict(list)
    total_rub = 0
    for entry in _all_cash_rows(db, work_date, work_date):
        total_rub += int(entry.commission_rub or 0)
        submitted[int(entry.brigadier_tg_id)] += 1
        amounts[int(entry.brigadier_tg_id)].append(int(entry.commission_rub or 0))
    brigadiers = []
    for tg_id, employee in cashiers.items():
        manual = overrides.get(str(tg_id))
        count = manual if manual is not None else expected[tg_id]
        received = submitted[tg_id]
        brigadiers.append({'tg_id': tg_id, 'name': _cash_brigadier_label(db, tg_id, employee.full_name),
                           'expected': count, 'automatic_expected': expected[tg_id],
                           'manual_expected': manual, 'submitted': received, 'amounts': amounts[tg_id],
                           'complete': count > 0 and received >= count})
    brigadiers.sort(key=lambda x: (x['name'].casefold(), x['tg_id']))
    items = [x for x in brigadiers if x['expected'] > 0]
    return {'work_date': work_date.isoformat(), 'items': items, 'total_rub': total_rub,
            'brigadiers': brigadiers, 'version': plan.version if plan else 0,
            'manual_count': sum(x['manual_expected'] is not None for x in brigadiers),
            'expected': sum(x['expected'] for x in items),
            'submitted': sum(min(x['submitted'], x['expected']) for x in items)}


@app.get("/api/cash/day-check")
def owner_cash_day_check(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    return _cash_day_check_data(db, work_date)


@app.put("/api/cash/day-check/{work_date}")
def owner_save_cash_day_check(work_date: date, change: CashDayCheckChange,
                             db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    data = _cash_day_check_data(db, work_date)
    conflict = 'Список кассы уже изменён. Обновите его перед сохранением.'
    if data['version'] != change.version:
        raise HTTPException(409, conflict)
    tg_ids = [x.tg_id for x in change.changes]
    if len(set(tg_ids)) != len(tg_ids):
        raise HTTPException(422, 'Каждый бригадир должен быть указан один раз')
    allowed = {x['tg_id'] for x in data['brigadiers']}
    if not set(tg_ids).issubset(allowed):
        raise HTTPException(422, 'Выберите кассового бригадира из списка')
    plan = db.get(CashDayCheckPlan, work_date)
    overrides = json.loads(plan.overrides_json) if plan else {}
    for item in change.changes:
        if item.expected is None:
            overrides.pop(str(item.tg_id), None)
        else:
            overrides[str(item.tg_id)] = item.expected
    values = {'overrides_json': json.dumps(overrides, sort_keys=True), 'version': change.version + 1,
              'updated_by_tg_id': user.id, 'updated_at': datetime.now(timezone.utc)}
    try:
        if change.version == 0:
            db.add(CashDayCheckPlan(work_date=work_date, **values))
        else:
            result = db.execute(update(CashDayCheckPlan).where(
                CashDayCheckPlan.work_date == work_date, CashDayCheckPlan.version == change.version).values(**values))
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(409, conflict)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, conflict)
    return _cash_day_check_data(db, work_date)


@app.get("/api/cash/ledger")
def owner_cash_ledger(on_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin), period: Literal["half", "month"] = "half"):
    _require_cash_manager(db, user)
    start, end = _period_bounds(on_date)
    if period == "month":
        import calendar
        start = on_date.replace(day=1)
        end = on_date.replace(day=calendar.monthrange(on_date.year, on_date.month)[1])

    entries = _all_cash_rows(db, start, end)

    current_cashiers = db.execute(
        select(Employee)
        .where(Employee.group_code == "brigadier", Employee.is_cashier.is_(True), _visible_employee())
        .order_by(Employee.full_name)
    ).scalars().all()

    brig_by_tg = {
        int(emp.tg_id): _cash_brigadier_label(db, int(emp.tg_id), emp.full_name)
        for emp in current_cashiers
        if emp.tg_id and not _cashier_excluded(db, emp)
    }
    for row in entries:
        brig_by_tg.setdefault(
            int(row.brigadier_tg_id),
            _cash_brigadier_label(
                db,
                int(row.brigadier_tg_id),
                row.brigadier_name or str(row.brigadier_tg_id),
            ),
        )

    brigadiers = [
        {"tg_id": tg_id, "name": name}
        for tg_id, name in sorted(
            brig_by_tg.items(),
            key=lambda item: (str(item[1]).casefold(), item[0]),
        )
    ]

    by_day_brig = defaultdict(
        lambda: defaultdict(
            lambda: {"commission_rub": 0, "kickback_rub": 0, "entries": 0}
        )
    )
    for row in entries:
        cell = by_day_brig[row.work_date][int(row.brigadier_tg_id)]
        cell["commission_rub"] += int(row.commission_rub or 0)
        cell["kickback_rub"] += int(row.kickback_rub or 0)
        cell["entries"] += 1

    days = []
    cursor = start
    while cursor <= end:
        cells = []
        day_commission = 0
        day_kickback = 0
        for brig in brigadiers:
            raw = by_day_brig[cursor][brig["tg_id"]]
            cell = {
                "brigadier_tg_id": brig["tg_id"],
                "commission_rub": int(raw["commission_rub"]),
                "kickback_rub": int(raw["kickback_rub"]),
                "entries": int(raw["entries"]),
            }
            cells.append(cell)
            day_commission += cell["commission_rub"]
            day_kickback += cell["kickback_rub"]

        days.append(
            {
                "work_date": cursor.isoformat(),
                "cells": cells,
                "day": {
                    "commission_rub": day_commission,
                    "kickback_rub": day_kickback,
                },
            }
        )
        cursor += timedelta(days=1)

    brigadier_totals = []
    for brig in brigadiers:
        commission = 0
        kickback = 0
        entry_count = 0
        for day in days:
            for cell in day["cells"]:
                if cell["brigadier_tg_id"] == brig["tg_id"]:
                    commission += cell["commission_rub"]
                    kickback += cell["kickback_rub"]
                    entry_count += cell["entries"]
                    break
        brigadier_totals.append(
            {
                "brigadier_tg_id": brig["tg_id"],
                "commission_rub": commission,
                "kickback_rub": kickback,
                "entries": entry_count,
            }
        )

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "brigadiers": brigadiers,
        "days": days,
        "brigadier_totals": brigadier_totals,
        "period": {
            "commission_rub": sum(int(x.commission_rub or 0) for x in entries),
            "kickback_rub": sum(int(x.kickback_rub or 0) for x in entries),
            "entries": len(entries),
        },
    }


@app.patch("/api/cash/{entry_id}")
def owner_patch_cash(entry_id: int, data: CashEntryPatch, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    raise HTTPException(403, "Владельцу доступен только просмотр кассы")


@app.delete("/api/cash/{entry_id}")
def owner_delete_cash(entry_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    raise HTTPException(403, "Владельцу доступен только просмотр кассы")


@app.post("/api/bot/employees/membership", dependencies=[Depends(_bot_key)])
def bot_employee_membership(data: EmployeeMembership, db: Session = Depends(get_db)):
    emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id)).scalar_one_or_none()
    if data.active:
        sync = EmployeeSync(
            preserve_identity=True,
            tg_id=data.tg_id,
            full_name=data.full_name,
            telegram_username=data.telegram_username,
            group_code=data.group_code,
            active=True,
        )
        emp = _upsert_employee(db, sync)
    elif emp and emp.group_code == data.group_code:
        # The person left the currently recorded LEGION staff group.
        # Keep historical assignments but hide the person from the active staff list.
        emp.active = False
    db.commit()
    return {"ok": True, "active": bool(emp and emp.active), "employee_id": emp.id if emp else None}


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


@app.get("/api/bot/readiness/summary", dependencies=[Depends(_bot_key)])
def bot_readiness_summary(work_date: date, db: Session = Depends(get_db)):
    return readiness_summary_data(db, work_date)


@app.get("/api/bot/readiness/history", dependencies=[Depends(_bot_key)])
def bot_readiness_history(work_date: date, limit: int = 30, db: Session = Depends(get_db)):
    return _readiness_history_data(db, work_date, limit)


@app.get("/api/bot/brigadier/orders", dependencies=[Depends(_bot_key)])
def bot_brigadier_orders(tg_id: int, work_date: date, db: Session = Depends(get_db)):
    """Return active orders assigned to a brigadier for the requested work date."""
    if tg_id in HIDDEN_TG_IDS:
        return {"ok": True, "count": 0, "orders": []}
    rows = db.execute(
        select(Order)
        .join(Assignment, Assignment.order_id == Order.id)
        .join(Employee, Employee.id == Assignment.employee_id)
        .where(
            Employee.tg_id == tg_id,
            Assignment.role == "brigadier",
            Order.work_date == work_date,
            ~Order.status.in_(["closed", "cancelled"]),
        )
        .order_by(Order.issue_time, Order.id)
    ).scalars().all()
    return {
        "ok": True,
        "count": len(rows),
        "orders": [
            {
                "public_id": o.public_id,
                "issue_time": o.issue_time.strftime("%H:%M"),
                "arrival_time": o.arrival_time.strftime("%H:%M") if o.arrival_time else None,
                "deceased_name": o.deceased_name,
                "route": o.route or "",
                "source": o.source,
                "source_label": _label_source(o.source),
                "category": o.category,
                "team_size": o.team_size,
                "status": o.status,
                "contact_time": o.contact_time or "",
                "tea_count": int(o.tea_count or 0),
                "tea_reported_at": o.tea_reported_at.isoformat() if o.tea_reported_at else None,
                "brigade_on_contact_at": o.brigade_on_contact_at.isoformat() if o.brigade_on_contact_at else None,
            }
            for o in rows
        ],
    }


@app.post("/api/bot/brigade/contact", dependencies=[Depends(_bot_key)])
def bot_brigade_contact(data: BrigadeContactReport, db: Session = Depends(get_db)):
    if data.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Бригадир недоступен")
    if not data.report_key or not data.request_id:
        return {"ok":True,"count":0,"needs_selection":True}
    _, chat, message = data.report_key.split(":")
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == int(chat),
        NumberCardEvent.message_id == int(message), NumberCardEvent.work_date == data.work_date,
        NumberCardEvent.tg_id == data.tg_id, _visible_number_card()).with_for_update())
    if not card:
        return {"ok":True,"count":0,"discarded":True,"error":"Заказ удалён или передан другому бригадиру"}
    job = db.get(NumberReassignment, card.id)
    if job and job.status == "pending":
        return {"ok":False,"error":"Выполняется замена бригадира"}
    # A retry carries the same ID. Replaying it after a manual removal or
    # reassignment must not bring the old check-in back.
    receipt = db.scalar(select(ImportLog.id).where(
        ImportLog.kind == "order_contact_confirm", ImportLog.raw_text == data.request_id).limit(1))
    if receipt:
        return {"ok":True,"event_id":card.id,"replayed":True,
                "on_contact":bool(_order_contact_states(db, data.work_date).get(card.id))}
    _set_order_contact(db, card, True, data.tg_id)
    db.add(ImportLog(
        kind="order_contact_confirm",
        created_by_tg_id=data.tg_id,
        raw_text=data.request_id,
        result_json=json.dumps({
            "event_id":card.id,"tg_id":data.tg_id,"work_date":data.work_date.isoformat(),
            "report_key":data.report_key,"raw_text":data.raw_text,
        }, ensure_ascii=False),
    ))
    db.commit()
    return {"ok":True,"count":1,"event_id":card.id,"on_contact":True}


@app.post("/api/bot/readiness", dependencies=[Depends(_bot_key)])
def bot_readiness(data: ReadinessReport, db: Session = Depends(get_db)):
    if data.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Участник недоступен")
    if data.raw_text == 'reconcile:local_ready_staff':
        # A repair may fill a missing entry, but may not replace a later status,
        # comment, car option or the original time of a real check-in.
        emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id)
                         .with_for_update()).scalar_one_or_none()
        if emp:
            existing = db.execute(select(Readiness).where(
                Readiness.employee_id == emp.id, Readiness.work_date == data.work_date
            )).scalar_one_or_none()
            if existing:
                return {"ok": True, "employee_id": emp.id, "status": existing.status}
    sync = EmployeeSync(
        preserve_identity=True,
        tg_id=data.tg_id,
        full_name=data.full_name,
        phone=data.phone,
        telegram_username=data.telegram_username,
        group_code=data.group_code,
        height_cm=data.height_cm,
        has_car=data.has_car,
        active=True,
    )
    emp = _upsert_employee(db, sync)
    row = db.execute(select(Readiness).where(Readiness.employee_id == emp.id, Readiness.work_date == data.work_date)).scalar_one_or_none()
    row = _record_bot_readiness(db, data, emp, row)
    db.commit()
    return {"ok": True, "employee_id": emp.id, "status": row.status}


def _record_bot_readiness(db, data, emp, row, source="message"):
    old_status = row.status if row else "no_response"
    if not row:
        row = Readiness(employee_id=emp.id, work_date=data.work_date, status=data.status)
        db.add(row)
    row.status = data.status
    row.raw_text = data.raw_text
    row.reported_at = datetime.utcnow()
    if old_status != data.status:
        db.add(ImportLog(
            kind="readiness_change",
            created_by_tg_id=data.tg_id,
            raw_text=data.raw_text or "",
            result_json=json.dumps({
                "tg_id": data.tg_id,
                "full_name": data.full_name,
                "group_code": data.group_code,
                "work_date": data.work_date.isoformat(),
                "old_status": old_status,
                "new_status": data.status,
                "source": "button" if str(data.raw_text or "").startswith("button:") else source,
            }, ensure_ascii=False),
        ))
    return row


@app.post("/api/bot/readiness/message", dependencies=[Depends(_bot_key)])
def bot_readiness_message(data: ReadinessMessageReport, db: Session = Depends(get_db)):
    if data.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Участник недоступен")
    if data.work_date < datetime.now(MOSCOW).date():
        return {"ok": False, "reason": "expired"}
    # Assignment endpoints lock the same employee. A withdrawal and a new
    # assignment therefore cannot both pass their checks concurrently.
    emp = db.scalar(select(Employee).where(Employee.tg_id == data.tg_id).with_for_update())
    if emp is None:
        emp = _upsert_employee(db, EmployeeSync(**{
            k: getattr(data, k) for k in ("tg_id", "full_name", "phone", "telegram_username", "group_code", "height_cm", "has_car")
        }, preserve_identity=True, active=True))
    row = db.scalar(select(Readiness).where(Readiness.employee_id == emp.id, Readiness.work_date == data.work_date))
    state = db.get(ReadinessMessageSource, (emp.id, data.work_date))
    if state:
        same_message = (state.chat_id, state.message_id) == (data.source_chat_id, data.source_message_id)
        newer_source = (data.source_sent_at, data.source_message_id, data.source_chat_id) > (state.sent_at, state.message_id, state.chat_id)
        if (not same_message and not newer_source) or (same_message and data.source_event_at < state.event_at):
            return {"ok": False, "reason": "stale"}
        if (data.is_edit or state.event_key == data.source_event_key) and (not row or row.reported_at != state.applied_at):
            return {"ok": False, "reason": "stale"}
    manual = db.scalar(select(ManualReadiness).where(
        ManualReadiness.work_date == data.work_date,
        ManualReadiness.identity_key.in_([f"employee:{emp.id}", manual_readiness_identity(emp.full_name, emp.metro, emp.height_cm)]),
    ).order_by(ManualReadiness.reported_at.desc()).limit(1))
    if manual and (
        manual.reported_at >= datetime.fromtimestamp(data.source_event_at, timezone.utc).replace(tzinfo=None)
        or (data.is_edit and (not row or manual.reported_at >= row.reported_at))
    ):
        return {"ok": False, "reason": "stale"}
    if state and state.event_key == data.source_event_key:
        return {"ok": True, "employee_id": emp.id, "status": row.status, "replayed": True}
    if data.status != "ready":
        assigned = db.scalar(select(Assignment.id).join(Order, Order.id == Assignment.order_id).where(
            Assignment.employee_id == emp.id, Order.work_date == data.work_date,
            Order.status.notin_(["closed", "cancelled"])).limit(1))
        # Number cards are also actual brigade orders, even when they were
        # entered directly in Telegram rather than assigned in the mini app.
        card = db.scalar(select(NumberCardEvent.id).where(
            NumberCardEvent.tg_id == data.tg_id, NumberCardEvent.work_date == data.work_date,
            _visible_number_card()).limit(1))
        if assigned is not None or card is not None:
            return {"ok": False, "reason": "assigned"}
    emp = _upsert_employee(db, EmployeeSync(**{
        k: getattr(data, k) for k in ("tg_id", "full_name", "phone", "telegram_username", "group_code", "height_cm", "has_car")
    }, preserve_identity=True, active=True))
    row = _record_bot_readiness(db, data, emp, row, source="message_edit" if data.is_edit else "message")
    if state is None:
        state = ReadinessMessageSource(employee_id=emp.id, work_date=data.work_date)
        db.add(state)
    state.chat_id, state.message_id = data.source_chat_id, data.source_message_id
    state.sent_at, state.event_at = data.source_sent_at, data.source_event_at
    state.event_key, state.applied_at = data.source_event_key, row.reported_at
    db.commit()
    return {"ok": True, "employee_id": emp.id, "status": row.status}


@app.post("/api/bot/orders", dependencies=[Depends(_bot_key)])
def bot_create_order(data: OrderCreate, db: Session = Depends(get_db)):
    order = _create_order(db, data)
    db.commit(); db.refresh(order)
    return _order_dict(order, db)


@app.post("/api/bot/orders/{public_id}/sync", dependencies=[Depends(_bot_key)])
async def bot_sync_order(public_id: str, data: OrderCreate, db: Session = Depends(get_db)):
    order = db.execute(select(Order).where(Order.public_id == public_id).with_for_update()).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден в диспетчерской")
    if order.status in {"closed", "cancelled"}:
        raise HTTPException(409, "Закрытый или отменённый заказ нельзя изменить")
    assignment = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier").options(selectinload(Assignment.employee))).scalar_one_or_none()
    old_brig_tg_id = assignment.employee.tg_id if assignment and assignment.employee else None
    reset_needed = data.work_date != order.work_date or data.team_size != order.team_size
    if reset_needed:
        _reset_order_composition(db, order)
    _apply_order_values(order, data.model_dump())
    db.commit(); db.refresh(order)
    if reset_needed and old_brig_tg_id:
        try:
            await send_telegram_message(old_brig_tg_id, "⚠️ <b>ЗАКАЗ ИЗМЕНЁН — СОСТАВ СНЯТ</b>\n\n" f"<b>Умерший:</b> {escape(order.deceased_name)}\n" f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n" f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}\n\n" "Диспетчер назначит состав заново.")
        except Exception:
            pass
    elif old_brig_tg_id:
        try:
            await _notify_order_changed_to_brigadier(db, order)
        except Exception:
            pass
    return _order_dict(order, db)


@app.post("/api/bot/orders/{public_id}/cancel", dependencies=[Depends(_bot_key)])
async def bot_cancel_order(public_id: str, db: Session = Depends(get_db)):
    order = db.execute(select(Order).where(Order.public_id == public_id).with_for_update()).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден в диспетчерской")
    if order.status == "cancelled":
        return _order_dict(order, db)
    assignment = db.execute(select(Assignment).where(Assignment.order_id == order.id, Assignment.role == "brigadier").options(selectinload(Assignment.employee))).scalar_one_or_none()
    brig_tg_id = assignment.employee.tg_id if assignment and assignment.employee else None
    _reset_order_composition(db, order)
    order.status = "cancelled"
    db.commit(); db.refresh(order)
    if brig_tg_id:
        try:
            await send_telegram_message(brig_tg_id, "🚫 <b>ЗАКАЗ ОТМЕНЁН АГЕНТОМ</b>\n\n" f"<b>Умерший:</b> {escape(order.deceased_name)}\n" f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n" f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}")
        except Exception:
            pass
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
    if not brig_assignment or brig_assignment.employee.tg_id in HIDDEN_TG_IDS:
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


@app.post("/api/bot/orders/{public_id}/tea-report", dependencies=[Depends(_bot_key)])
def bot_order_tea_report(public_id: str, data: BrigadierTeaReport, db: Session = Depends(get_db)):
    if data.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Бригадир недоступен")
    order = db.execute(select(Order).where(Order.public_id == public_id)).scalar_one_or_none()
    if not order:
        raise HTTPException(404, "Заказ не найден")
    assignment = db.execute(
        select(Assignment)
        .join(Employee, Employee.id == Assignment.employee_id)
        .where(
            Assignment.order_id == order.id,
            Assignment.role == "brigadier",
            Employee.tg_id == data.tg_id,
        )
    ).scalar_one_or_none()
    if not assignment:
        raise HTTPException(403, "Этот заказ не назначен бригадиру")
    order.tea_count = int(data.tea_count)
    order.tea_reported_at = datetime.utcnow()
    db.add(ImportLog(
        kind="order_tea_report",
        created_by_tg_id=data.tg_id,
        raw_text=str(data.tea_count),
        result_json=json.dumps({
            "public_id": public_id,
            "order_id": order.id,
            "tea_count": int(data.tea_count),
        }, ensure_ascii=False),
    ))
    db.commit(); db.refresh(order)
    return {"ok": True, "order": _order_dict(order, db)}


# ---------- GBU import ----------

@app.post("/api/gbu/ocr")
async def gbu_ocr(
    file: UploadFile = File(...),
    work_date: str = Form(...),
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_admin),
):
    try:
        datetime.strptime(work_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(422, "Укажите дату заказов ГБУ") from exc

    try:
        content = await file.read()
        raw, drafts = parse_gbu_table_image(content, work_date, lang="rus+eng")
    except Exception as exc:
        raise HTTPException(422, f"Не удалось распознать таблицу ГБУ: {exc}") from exc

    contacts = db.execute(
        select(GbuAgentContact.full_name, GbuAgentContact.phone).where(GbuAgentContact.active.is_(True))
    ).all()
    contact_pairs = [(str(name), str(phone)) for name, phone in contacts]
    for draft in drafts:
        match = best_agent_match(draft.get("agent_name", ""), contact_pairs)
        if match and match[2] >= 0.78:
            draft["agent_name"] = match[0]
            draft["agent_phone"] = match[1]
            draft["agent_match_confidence"] = round(min(match[2], 1.0), 2)
        else:
            draft["agent_match_confidence"] = round(match[2], 2) if match else 0

    db.add(ImportLog(
        kind="gbu_ocr",
        created_by_tg_id=user.id,
        raw_text=raw,
        result_json=json.dumps(drafts, ensure_ascii=False, default=str),
    ))
    db.commit()
    return {
        "raw_text": raw,
        "drafts": drafts,
        "warning": "Таблица прочитана по отдельным ячейкам: первые 2 столбца и правый служебный код игнорируются; время = быть, выдача = +30 минут; белый состав = 4, зелёный = 6. Проверьте черновики перед сохранением.",
    }


@app.post("/api/gbu/drafts")
def create_gbu_drafts(items: list[GbuDraft], db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    created = []
    for item in items:
        order = _create_order(db, OrderCreate(source="gbu", **item.model_dump()))
        created.append(order)
    db.commit()
    return [_order_dict(x, db) for x in created]


# ---------- GBU agent contacts ----------

class GbuAgentContactCreate(BaseModel):
    full_name: StrictStr = Field(min_length=1, max_length=200)
    phone: StrictStr = Field(min_length=1, max_length=40)


def _gbu_contact_phone(value: str) -> str:
    raw = value.strip()
    if not re.fullmatch(r"[+0-9\s()\-]+", raw):
        raise HTTPException(422, "Введите корректный телефон агента")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 10 and not raw.startswith("+"):
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8") and not raw.startswith("+"):
        digits = "7" + digits[1:]
    if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
        raise HTTPException(422, "Введите корректный телефон агента с кодом страны")
    if "+" in raw and (not raw.startswith("+") or raw.count("+") != 1):
        raise HTTPException(422, "Введите корректный телефон агента")
    return "+" + digits


@app.post("/api/gbu/agents", status_code=201)
def create_gbu_agent(data: GbuAgentContactCreate, db: Session = Depends(get_db),
                     user: TelegramUser = Depends(require_admin)):
    if cabinet_kind(db, user.id) not in {"owner", "assistant"}:
        raise HTTPException(403, "Добавлять агентов могут только владелец и диспетчеры")
    full_name = " ".join(data.full_name.split())
    if not full_name or any(ord(ch) < 32 for ch in full_name):
        raise HTTPException(422, "Введите ФИО агента")
    phone = _gbu_contact_phone(data.phone)
    # Serialize duplicate checks across dispatcher sessions on the production DB.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext('legion:gbu-agent-create'))"))
    for row in db.scalars(select(GbuAgentContact)):
        same_name = " ".join(str(row.full_name or "").split()).casefold() == full_name.casefold()
        try:
            same_phone = _gbu_contact_phone(str(row.phone or "")) == phone
        except HTTPException:
            same_phone = False
        if same_name or same_phone:
            raise HTTPException(409, "Агент с таким ФИО или телефоном уже есть в справочнике")
    row = GbuAgentContact(full_name=full_name, phone=phone, active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "full_name": row.full_name, "phone": row.phone, "source_page": row.source_page}


@app.get("/api/gbu/agents")
def gbu_agents(
    search: str = "",
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_admin),
):
    q = select(GbuAgentContact).where(GbuAgentContact.active.is_(True))
    if search.strip():
        value = f"%{search.strip()}%"
        q = q.where(or_(GbuAgentContact.full_name.ilike(value), GbuAgentContact.phone.ilike(value)))
    rows = db.execute(q.order_by(GbuAgentContact.full_name)).scalars().all()
    return [{"id": x.id, "full_name": x.full_name, "phone": x.phone, "source_page": x.source_page} for x in rows]


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
        if emp and emp.tg_id not in HIDDEN_TG_IDS:
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
        "cutoff_label": readiness["cutoff_label"],
        "workload": overloaded,
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "LEGION Dispatcher"}


# Destinations are supplied by the bot from its existing group settings.
class ReadinessChatConfig(Base):
    __tablename__ = "readiness_chat_config"
    id = Column(Integer, primary_key=True)
    payload = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    summary_sent_at = Column(DateTime, nullable=True)
    reminders_sent_at = Column(DateTime, nullable=True)


class ReadinessChatGroup(BaseModel):
    chat_id: int
    group_code: Literal["brigadier", "main", "cashless", "reserve"]


class FreeStaffDispatch(Base):
    __tablename__ = "free_staff_dispatches"
    request_id = Column(String(80), primary_key=True)
    payload = Column(String, nullable=False)
    created_by = Column(BigInteger, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FreeStaffSelection(BaseModel):
    work_date: date
    employee_ids: list[StrictInt] = Field(max_length=1000)
    snapshot: str = Field(max_length=64)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")


def free_staff_snapshot(db, day):
    summary = readiness_summary_data(db, day)
    groups = []
    for code, label in GROUP_LABELS.items():
        people = []
        for p in summary["groups"].get(code, {}).get("ready", []):
            if int(p.get("id") or 0) <= 0:
                continue
            people.append({"id": p["id"], "name": str(p.get("full_name") or "—").split()[0],
                           "metro": p.get("metro") or "—", "height": p.get("height_cm") or "—",
                           "phone": p.get("phone") or "", "has_car": bool(p.get("has_car"))})
        people.sort(key=lambda p: (p["name"].casefold(), p["metro"].casefold(), p["id"]))
        groups.append({"code": code, "label": label, "people": people})
    signature = hashlib.sha256(json.dumps(groups, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"work_date": day.isoformat(), "groups": groups, "snapshot": signature}


def free_staff_messages(snapshot, ids, settings, day):
    selected = set(ids)
    valid = {p["id"] for g in snapshot["groups"] for p in g["people"]}
    if not selected or not selected <= valid:
        raise HTTPException(409, "Отметьте готовых сотрудников из актуального списка.")
    targets = {}
    for target in settings["groups"]:
        targets.setdefault(int(target["chat_id"]), set()).add(target["group_code"])
    messages = []
    for group in snapshot["groups"]:
        people = [p for p in group["people"] if p["id"] in selected]
        if not people:
            continue
        chats = [chat for chat, codes in targets.items() if group["code"] in codes]
        if not chats or any(len(targets[chat]) != 1 for chat in chats):
            raise HTTPException(409, "Проверьте отдельный чат группы: " + group["label"])
        lines = ["<b>Заказы на завтра закрыты❗️</b>", day.strftime("%d.%m.%Y"), "", "<b>Без заказа:👇</b>"]
        lines.extend(f"{i}. {escape(p['name'])} · {escape(p['metro'])} · {p['height']}" for i, p in enumerate(people, 1))
        for chat in sorted(chats):
            messages.append({"chat_id": chat, "label": group["label"],
                             "chunks": readiness_chat_chunks(lines), "done": 0})
    # The brigadier chat gets one combined list, not a second brigadier-only list.
    brigadier_chats = [chat for chat, codes in targets.items() if 'brigadier' in codes]
    if not brigadier_chats or any(targets[chat] != {'brigadier'} for chat in brigadier_chats):
        raise HTTPException(409, 'Не настроен отдельный чат бригадиров')
    people = sorted((p for g in snapshot['groups'] for p in g['people'] if p['id'] in selected),
                    key=lambda p: (p['name'].casefold(), p['metro'].casefold(), p['id']))
    lines = [f"<b>📋 Свободные на {day.strftime('%d.%m')}</b>",
             'Для замены утром — можно связаться напрямую.', '']
    for i, person in enumerate(people, 1):
        car = ' 🚗' if person.get('has_car') else ''
        lines.append(f"{i}. {escape(person['name'])} {escape(person['metro'])} {escape(str(person['height']))}{car}")
        phone = re.sub(r'[^+0-9]', '', person.get('phone') or '')
        lines.append(f"📞 {escape(phone)}" if phone else 'Телефон не указан')
    lines.extend(['', f'<b>Всего: {len(people)}</b>'])
    messages = [m for m in messages if m['chat_id'] not in brigadier_chats]
    for chat in sorted(brigadier_chats):
        messages.append({'chat_id': chat, 'label': 'Бригадиры — общий список',
                         'chunks': readiness_chat_chunks(lines), 'done': 0, 'combined_free_staff': True})
    return messages


class FreeStaffGroupList(Base):
    __tablename__ = 'free_staff_group_lists'
    work_date = Column(Date, primary_key=True)
    chat_id = Column(BigInteger, primary_key=True)
    payload = Column(String, nullable=False, default='[]')


async def free_staff_telegram_call(method, payload):
    import httpx
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token:
        raise ValueError('Бот не настроен')
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f'https://api.telegram.org/bot{token}/{method}', json=payload)
        data = response.json()
    if not data.get('ok'):
        if method == 'editMessageText' and 'message is not modified' in str(data.get('description', '')):
            return True
        raise ValueError('Telegram не подтвердил обновление списка')
    return data.get('result')


async def publish_free_staff_group_list(db, day, message):
    row = db.get(FreeStaffGroupList, (day, message['chat_id']))
    if row is None:
        row = FreeStaffGroupList(work_date=day, chat_id=message['chat_id'], payload='[]')
        db.add(row)
        db.flush()
    parts = json.loads(row.payload)
    for index, chunk in enumerate(message['chunks']):
        if index < len(parts) and parts[index].get('status') == 'unknown':
            raise ValueError('Отправка списка не подтверждена. Проверьте бригадирскую группу')
        part = parts[index] if index < len(parts) else None
        if part and part.get('text') == chunk:
            continue
        payload = {'chat_id': message['chat_id'], 'text': chunk, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if part and part.get('id'):
            payload['message_id'] = part['id']
            await free_staff_telegram_call('editMessageText', payload)
            part['text'] = chunk
        else:
            # Persist uncertainty before sending; a lost response must not cause a duplicate.
            part = {'status': 'unknown'}
            parts.append(part)
            row.payload = json.dumps(parts, ensure_ascii=False)
            db.commit()
            db.execute(select(ReadinessChatConfig).where(ReadinessChatConfig.id == 1).with_for_update()).scalar_one_or_none()
            result = await free_staff_telegram_call('sendMessage', payload)
            if not isinstance(result, dict) or not result.get('message_id'):
                raise ValueError('Отправка списка не подтверждена')
            part.update(id=result['message_id'], text=chunk, status='sent')
        row.payload = json.dumps(parts, ensure_ascii=False)
        db.commit()
        db.execute(select(ReadinessChatConfig).where(ReadinessChatConfig.id == 1).with_for_update()).scalar_one_or_none()
    while len(parts) > len(message['chunks']):
        part = parts[-1]
        if not part.get('id'):
            raise ValueError('Проверьте отправку списка в группе')
        await free_staff_telegram_call('deleteMessage', {'chat_id': message['chat_id'], 'message_id': part['id']})
        parts.pop()
        row.payload = json.dumps(parts, ensure_ascii=False)
        db.commit()


@app.get("/api/readiness/free-tomorrow")
def get_free_staff(db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    day = datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
    return free_staff_snapshot(db, day)


@app.post("/api/readiness/free-tomorrow")
async def send_free_staff(data: FreeStaffSelection, db: Session = Depends(get_db),
                          user: TelegramUser = Depends(require_admin)):
    day = datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
    if data.work_date != day:
        raise HTTPException(409, "Дата изменилась. Откройте список заново.")
    # Serialize creation and retries, including simultaneous dispatcher clicks.
    config = db.execute(select(ReadinessChatConfig).where(ReadinessChatConfig.id == 1).with_for_update()).scalar_one_or_none()
    if config is None or datetime.utcnow() - config.updated_at > timedelta(minutes=5):
        raise HTTPException(409, "Связь с ботом не обновлена. Повторите через минуту.")
    fingerprint = {"day": day.isoformat(), "ids": sorted(set(data.employee_ids)), "snapshot": data.snapshot}
    job = db.get(FreeStaffDispatch, data.request_id)
    if job:
        saved = json.loads(job.payload)
        if saved["selection"] != fingerprint or job.created_by != user.id:
            raise HTTPException(409, "Запрос уже использован. Откройте список заново.")
    else:
        snapshot = free_staff_snapshot(db, day)
        if snapshot["snapshot"] != data.snapshot:
            raise HTTPException(409, "Готовность сотрудников изменилась. Откройте список заново.")
        saved = {"selection": fingerprint, "messages": free_staff_messages(snapshot, data.employee_ids, json.loads(config.payload), day)}
        job = FreeStaffDispatch(request_id=data.request_id, payload=json.dumps(saved), created_by=user.id)
        db.add(job)
        db.flush()
    for message in saved["messages"]:
        if message.get('combined_free_staff'):
            if message['done'] >= len(message['chunks']):
                continue
            try:
                await publish_free_staff_group_list(db, day, message)
                message['done'] = len(message['chunks'])
            except Exception:
                message['done'] = 0
            continue
        while message["done"] < len(message["chunks"]):
            try:
                ok = await send_telegram_message(message["chat_id"], message["chunks"][message["done"]])
            except Exception:
                ok = False
            if not ok:
                break
            message["done"] += 1
    job.payload = json.dumps(saved, ensure_ascii=False)
    db.commit()
    failed = [m["label"] for m in saved["messages"] if m["done"] < len(m["chunks"])]
    return {"sent_chats": len(saved["messages"]) - len(failed), "failed_groups": failed}


class ReadinessChatSettings(BaseModel):
    admin_chat_ids: list[int] = Field(max_length=100)
    groups: list[ReadinessChatGroup] = Field(max_length=100)


@app.post("/api/bot/readiness/chat-settings", dependencies=[Depends(_bot_key)])
def sync_readiness_chat_settings(data: ReadinessChatSettings, db: Session = Depends(get_db)):
    row = db.get(ReadinessChatConfig, 1)
    if row is None:
        row = ReadinessChatConfig(id=1)
        db.add(row)
    row.payload = data.model_dump_json()
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


def readiness_chat_person(person):
    name = str(person.get("display_name") or person.get("full_name") or "—").strip()
    name = re.sub(r"\s+[12]\d{2}(?:\s*см)?\s*$", "", name, flags=re.I)
    metro = str(person.get("metro") or "").strip()
    normal = lambda value: " ".join(re.sub(r"[^а-яa-z0-9]+", " ", value.casefold().replace("ё", "е")).split())
    if metro and f" {normal(metro)} " not in f" {normal(name)} ":
        name += " " + metro
    return escape(f"{name} {person.get('height_cm') or '—'} см")


def readiness_chat_chunks(lines):
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > 3500:
            chunks.append(current)
            current = ""
        current += ("\n" if current else "") + line
    if current:
        chunks.append(current)
    return chunks


def readiness_chat_messages(summary, settings, kind, work_date):
    groups = summary["groups"]
    date_label = work_date.strftime("%d.%m.%Y")
    sort_people = lambda people: sorted(people, key=lambda p: str(p.get("display_name") or p.get("full_name") or "").casefold())
    result = []
    staff_chats = {int(g["chat_id"]) for g in settings["groups"]}
    if kind == "summary":
        ready_counts = {code: len(groups.get(code, {}).get("ready", [])) for code in GROUP_LABELS}
        lines = [
            f"<b>📝 Сверка готовности на {date_label}</b>",
            "",
            f"<b>Всего готовы: {sum(ready_counts.values())}</b>",
        ]
        lines.extend(f"{escape(label)}: <b>{ready_counts[code]}</b>" for code, label in GROUP_LABELS.items())
        chunks = readiness_chat_chunks(lines)
        for chat_id in sorted(set(settings["admin_chat_ids"]) - staff_chats):
            result.append((chat_id, chunks))
    else:
        labels = {"brigadier": "Бригадирская группа", "main": "Основной состав", "cashless": "Безнал состав"}
        sent_chats = set()
        for target in settings["groups"]:
            code, chat_id = target["group_code"], int(target["chat_id"])
            if code not in labels or chat_id in sent_chats:
                continue
            people = sort_people(groups.get(code, {}).get("no_response", []))
            if not people:
                continue
            sent_chats.add(chat_id)
            lines = [f"<b>{labels[code]}</b>", f"На {date_label}", "<b>Не отписались:</b>"]
            lines.extend(f"{i}. {readiness_chat_person(p)}" for i, p in enumerate(people, 1))
            result.append((chat_id, readiness_chat_chunks(lines)))
    return result


@app.post("/api/readiness/chat/{kind}")
async def send_readiness_chat_action(kind: Literal["summary", "reminders", "roshcha", "avral"],
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if kind == "roshcha":
        raise HTTPException(409, "Откройте «Выходной» и отметьте сотрудников без заказа.")
    now = datetime.utcnow()
    config = db.execute(select(ReadinessChatConfig).where(ReadinessChatConfig.id == 1).with_for_update()).scalar_one_or_none()
    if config is None or now - config.updated_at > timedelta(minutes=5):
        raise HTTPException(409, "Связь с ботом не обновлена. Проверьте, что новый bot.py запущен, и повторите через минуту.")
    field = "summary_sent_at" if kind == "summary" else "reminders_sent_at"
    last = getattr(config, field)
    if last and now - last < timedelta(seconds=30):
        raise HTTPException(429, "Отправка уже запускалась. Повторите через 30 секунд.")
    settings = json.loads(config.payload)
    day = datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=1)
    if kind in ("roshcha", "avral"):
        required = {"brigadier", "main", "cashless", "reserve"}
        configured = {g["group_code"] for g in settings["groups"]}
        missing = required - configured
        if missing:
            raise HTTPException(409, "Не подключены группы: " + ", ".join(GROUP_LABELS[c] for c in sorted(missing)))
        broadcast_text = ("Завтра ❗️АВРАЛ❗️ОТПИСЫВАЕМСЯ О ГОТОВНОСТИ"
                          if kind == "avral" else "❗️Кто не получил заказ-выходной❗️")
        messages = [(chat_id, [broadcast_text])
                    for chat_id in sorted({int(g["chat_id"]) for g in settings["groups"]
                                           if g["group_code"] in required})]
    else:
        summary = readiness_summary_data(db, day)
        messages = readiness_chat_messages(summary, settings, kind, day)
    if kind == "summary" and not messages:
        raise HTTPException(409, "У бота не настроен административный чат для сверки.")
    if kind == "reminders":
        configured = {g["group_code"] for g in settings["groups"]}
        missing = [GROUP_LABELS[c] for c in ("brigadier", "main", "cashless") if summary["groups"].get(c, {}).get("no_response") and c not in configured]
        if missing:
            raise HTTPException(409, "Не подключены группы: " + ", ".join(missing))
    setattr(config, field, now)
    db.commit()
    sent, failed = 0, 0
    for chat_id, chunks in messages:
        complete = True
        for chunk in chunks:
            try:
                if not await send_telegram_message(chat_id, chunk):
                    complete = False
                    break
            except Exception:
                complete = False
                break
        if complete:
            sent += 1
        else:
            failed += 1
    return {"sent_chats": sent, "failed_chats": failed, "work_date": day.isoformat(), "empty": not messages}


class CashReminderConfig(Base):
    __tablename__ = "cash_reminder_config"
    id = Column(Integer, primary_key=True)
    payload = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, nullable=True)


class CashReminderSettings(BaseModel):
    chat_ids: list[int] = Field(max_length=1000)


class CashReminderDelivery(Base):
    __tablename__ = 'cash_reminder_delivery'
    work_date = Column(Date, primary_key=True)
    chat_id = Column(BigInteger, primary_key=True)
    status = Column(String(20), nullable=False, default='pending')
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


@app.post("/api/bot/cash/chat-settings", dependencies=[Depends(_bot_key)])
def sync_cash_reminder_settings(data: CashReminderSettings, db: Session = Depends(get_db)):
    # Only group destinations, deduplicated even when cashiers share a group.
    chat_ids = sorted({chat_id for chat_id in data.chat_ids if chat_id < 0})
    row = db.get(CashReminderConfig, 1)
    if row is None:
        row = CashReminderConfig(id=1)
        db.add(row)
    row.payload = json.dumps(chat_ids)
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/cash/reminder")
async def send_cash_reminder(db: Session = Depends(get_db),
                             user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    now = datetime.utcnow()
    row = db.execute(select(CashReminderConfig).where(
        CashReminderConfig.id == 1).with_for_update()).scalar_one_or_none()
    if row is None or now - row.updated_at > timedelta(minutes=5):
        raise HTTPException(409, "Связь с ботом не обновлена. Проверьте, что новый bot.py запущен, и повторите через минуту.")
    chat_ids = sorted({int(chat_id) for chat_id in json.loads(row.payload) if int(chat_id) < 0})
    if not chat_ids:
        raise HTTPException(409, "Нет подключённых групп действующих кассиров.")
    today = datetime.now(MOSCOW).date()
    sent, failed, already, unconfirmed = 0, 0, 0, 0
    claimed = []
    legacy_sent_today = bool(row.sent_at and
        row.sent_at.replace(tzinfo=timezone.utc).astimezone(MOSCOW).date() == today and
        db.scalar(select(CashReminderDelivery.chat_id).where(
            CashReminderDelivery.work_date == today).limit(1)) is None)
    # The config row lock serializes owners/assistants, across browser windows
    # and service replicas. Commit each destination's claim before any send.
    for chat_id in chat_ids:
        delivery = db.get(CashReminderDelivery, (today, chat_id))
        if delivery is None and legacy_sent_today:
            # The previous version stored only a bulk attempt timestamp. Its
            # individual results are unknown; installing this version must not
            # cause that same day's reminder to be broadcast again.
            db.add(CashReminderDelivery(work_date=today,chat_id=chat_id,status='unknown',updated_at=now))
            unconfirmed += 1
            continue
        if delivery and delivery.status == 'sent':
            already += 1
            continue
        if delivery and delivery.status != 'failed':
            unconfirmed += 1
            continue
        if delivery is None:
            delivery = CashReminderDelivery(work_date=today, chat_id=chat_id)
            db.add(delivery)
        delivery.status = 'pending'
        delivery.updated_at = now
        claimed.append(chat_id)
    if claimed:
        row.sent_at = now
    db.commit()
    message = ('напоминаю о сверке кассы сегодня 🔔\n\n'
        'Кнопка подсчёта кассы находится в разделе «Касса» в личном кабинете бригадира 👍')
    for chat_id in claimed:
        outcome = 'unknown'
        try:
            delivered = await send_telegram_message(chat_id, message)
            outcome = 'sent' if delivered else 'failed'
        except Exception as exc:
            # An explicit Telegram 4xx response rejected the send. A timeout or
            # dropped response may follow delivery: never automatically resend.
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if isinstance(code, int) and 400 <= code < 500 and code != 408:
                outcome = 'failed'
        delivery = db.get(CashReminderDelivery, (today, chat_id))
        delivery.status = outcome
        delivery.updated_at = datetime.utcnow()
        db.commit()
        if outcome == 'sent':
            sent += 1
        elif outcome == 'failed':
            failed += 1
        else:
            unconfirmed += 1
    return {"sent_chats": sent, "failed_chats": failed,
        "already_sent_chats": already, "unconfirmed_chats": unconfirmed}


@app.get("/api/cash/months")
def owner_cash_months(db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    dates = db.execute(select(CashEntry.work_date).where(
        CashEntry.brigadier_tg_id.notin_(_cash_excluded_ids(db))).distinct()).scalars().all()
    dates += db.scalars(select(CabinetOrderCash.work_date).where(
        CabinetOrderCash.tg_id.notin_(_cash_excluded_ids(db))).distinct()).all()
    months = {day.strftime("%Y-%m") for day in dates}
    months.add(datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m"))
    return {"months": sorted(months, reverse=True)}

# Earnings columns are computed display-only fields (no DB schema change):
# Итого + (negative Vlad allowance) = Чистыми.
# Read the existing backend net_after_vlad result to prevent double deductions.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'const totalRow=(title,values)=>',
    'const totalRow=(title,values,net)=>',
).replace(
    '${money(values.reduce((a,b)=>a+b,0))}</b></td></tr>',
    '${money(values.reduce((a,b)=>a+b,0))}</b></td>${kind===\'earnings\'?`<td class="vlad-deduction"><b>${money(net-values.reduce((a,b)=>a+b,0))}</b></td><td style="color:var(--gold)"><b>${money(net)}</b></td>`:""}</tr>',
    1,
).replace(
    '${money(values.reduce((a,b)=>a+b,0))}</b></td></tr>',
    '${money(values.reduce((a,b)=>a+b,0))}</b></td>${kind===\'earnings\'?`<td class="vlad-deduction"><b>${money(data.net_after_vlad.daily[String(day)]-values.reduce((a,b)=>a+b,0))}</b></td><td style="color:var(--gold)"><b>${money(data.net_after_vlad.daily[String(day)])}</b></td>`:""}</tr>',
    1,
).replace(
    "totalRow('Итого 1–15',data.first)",
    "totalRow('Итого 1–15',data.first,data.net_after_vlad?.first)",
).replace(
    "totalRow('Итого 16–'+data.days,data.second)+totalRow('За месяц',data.total)",
    "totalRow('Итого 16–'+data.days,data.second,data.net_after_vlad?.second)+totalRow('За месяц',data.total,data.net_after_vlad?.total)",
).replace(
    '<th>Итого ₽</th></tr></thead>',
    '<th>Итого ₽</th>${kind===\'earnings\'?\'<th>Надбавка<br>Владу</th><th style="color:var(--gold)">Чистыми</th>\':""}</tr></thead>',
).replace(
    '«Владу» — количество запрошенных сотрудников × 100 ₽ за день.',
    '«Владу» — количество запрошенных сотрудников без выносов × 100 ₽ за день. «Чистыми» — итог дня минус надбавка Владу.',
).replace(
    'grid.innerHTML=`<table style="border-collapse:separate;border-spacing:8px;min-width:100%;text-align:right"><thead>',
    'grid.innerHTML=`<style>#personalGrid thead th{position:sticky;top:0;z-index:3;background:var(--panel2);box-shadow:0 2px 0 var(--line);padding:8px 5px;vertical-align:middle;white-space:nowrap}#personalGrid thead th:first-child{text-align:left}#personalGrid td.vlad-deduction{color:#ff5555;white-space:nowrap;font-weight:700}#personalGrid table{min-width:max-content}#personalGrid{overscroll-behavior:contain}</style><table style="border-collapse:separate;border-spacing:8px;min-width:100%;text-align:right"><thead>',
)

# Personal manual ledgers are separate from cash reported by brigadiers.
PERSONAL_TABLE_COLUMNS = {
    "earnings": ["Касса", "ГБУ", "Наличка"],
    "cash": ["Влад Ряз", "Игорь", "Леха", "Роща", "Лев", "Арсений", "Лухман", "Перово", "Давыд", "Донской", "Макс", "Рома"],
    "kickbacks": ["Марине", "Владу"],
}

class PersonalMonthlyTable(Base):
    __tablename__ = "personal_monthly_tables"
    id = Column(Integer, primary_key=True)
    owner_id = Column(BigInteger, nullable=False)
    month = Column(String(7), nullable=False)
    kind = Column(String(20), nullable=False)
    payload = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    __table_args__ = (UniqueConstraint("owner_id", "month", "kind", name="uq_personal_month_table"),)


class PersonalGbuComment(Base):
    __tablename__ = "personal_gbu_comments"
    owner_id = Column(BigInteger, primary_key=True)
    month = Column(String(7), primary_key=True)
    text = Column(String(500), nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)


class PersonalGbuCommentChange(BaseModel):
    text: str = Field(max_length=500)
    version: int = Field(ge=0, strict=True)


def personal_gbu_comment_data(db: Session, owner_id: int, month: str):
    row = db.get(PersonalGbuComment, (owner_id, month))
    return {"month": month, "text": row.text if row else "", "version": row.version if row else 0}


@app.get("/api/personal-gbu-comment/{month}")
def get_personal_gbu_comment(month: str, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    _personal_month_days(month)
    return personal_gbu_comment_data(db, user.id, month)


@app.put("/api/personal-gbu-comment/{month}")
def save_personal_gbu_comment(month: str, change: PersonalGbuCommentChange,
                              db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    _personal_month_days(month)
    values = {"text": change.text.strip(), "version": change.version + 1}
    conflict = "Комментарий изменён в другом окне. Ваш текст остался в поле. Скопируйте его и откройте комментарий заново."
    try:
        if change.version == 0:
            db.add(PersonalGbuComment(owner_id=user.id, month=month, **values))
        else:
            result = db.execute(update(PersonalGbuComment).where(
                PersonalGbuComment.owner_id == user.id,
                PersonalGbuComment.month == month,
                PersonalGbuComment.version == change.version).values(**values))
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(409, conflict)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, conflict)
    return personal_gbu_comment_data(db, user.id, month)


class PersonalCashDay(Base):
    __tablename__ = 'personal_cash_days'
    owner_id = Column(BigInteger, primary_key=True)
    work_date = Column(Date, primary_key=True)
    payload = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)


class PersonalCashDayInput(BaseModel):
    version: int = Field(ge=0, strict=True)
    amounts: dict[str, int]
    own: int = Field(ge=0, le=100000000000, strict=True)
    legacy: int = Field(ge=-100000000000, le=100000000000, strict=True)
    checked: dict[str, StrictBool] | None = None


def personal_cash_day_data(db, owner_id, work_date):
    saved = db.get(PersonalCashDay, (owner_id, work_date))
    month = work_date.strftime('%Y-%m')
    table = db.scalar(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == owner_id,
        PersonalMonthlyTable.month == month, PersonalMonthlyTable.kind == 'earnings'))
    old = json.loads(table.payload).get('cells', {}).get(f'{work_date.day}:2', 0) if table else 0
    values = json.loads(saved.payload) if saved else {'amounts': {}, 'own': 0, 'legacy': old}
    employees = {e.tg_id: e for e in db.scalars(select(Employee).where(_visible_employee()))}
    items = []
    for card in db.scalars(select(NumberCardEvent).where(NumberCardEvent.work_date == work_date,
            NumberCardEvent.source == 'private', _visible_number_card()).order_by(NumberCardEvent.id)):
        employee = employees.get(card.tg_id)
        if not employee or employee.group_code != 'brigadier' or employee.is_cashier:
            continue
        items.append({'key': str(card.id), 'name': _employee_dict(employee)['display_name'], 'surname': card.surname})
    active = {x['key'] for x in items}
    labels = values.get('labels', {})
    for key in values.get('amounts', {}):
        if key not in active:
            items.append({'key': key, 'name': labels.get(key, 'Ранее сохранённый заказ'),
                          'surname': 'Заказ изменён или удалён — проверьте сумму'})
    checked = values.get('checked', {})
    return {**values, 'checked': {x['key']: checked.get(x['key']) is True for x in items},
            'items': items, 'version': saved.version if saved else 0,
            'total': sum(values.get('amounts', {}).values()) + values.get('own', 0) + values.get('legacy', 0)}


@app.get('/api/personal-cash-day/{work_date}')
def get_personal_cash_day(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    return personal_cash_day_data(db, user.id, work_date)


@app.put('/api/personal-cash-day/{work_date}')
def save_personal_cash_day(work_date: date, change: PersonalCashDayInput,
                          db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    current = personal_cash_day_data(db, user.id, work_date)
    if current['version'] != change.version:
        raise HTTPException(409, 'Данные изменены. Откройте день заново.')
    allowed = {x['key'] for x in current['items']}
    if set(change.amounts) != allowed or any(type(v) is not int or not 0 <= v <= 100000000000 for v in change.amounts.values()):
        raise HTTPException(422, 'Список заказов изменился или сумма некорректна. Откройте день заново.')
    if change.checked is not None and not set(change.checked).issubset(allowed):
        raise HTTPException(422, 'Список отметок изменился. Откройте день заново.')
    checked = current['checked'] if change.checked is None else {
        key: change.checked.get(key, False) for key in allowed}
    payload = json.dumps({'amounts': change.amounts, 'own': change.own, 'legacy': change.legacy,
        'checked': checked,
        'labels': {x['key']: x['name'] for x in current['items']}}, ensure_ascii=False)
    if change.version:
        result = db.execute(update(PersonalCashDay).where(PersonalCashDay.owner_id == user.id,
            PersonalCashDay.work_date == work_date, PersonalCashDay.version == change.version)
            .values(payload=payload, version=change.version + 1))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, 'Данные изменены. Откройте день заново.')
    else:
        db.add(PersonalCashDay(owner_id=user.id, work_date=work_date, payload=payload, version=1))
    month = work_date.strftime('%Y-%m')
    table = db.scalar(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == user.id,
        PersonalMonthlyTable.month == month, PersonalMonthlyTable.kind == 'earnings'))
    if table is None:
        db.add(PersonalMonthlyTable(owner_id=user.id, month=month, kind='earnings', version=1,
            payload=json.dumps({'columns': list(PERSONAL_TABLE_COLUMNS['earnings']), 'cells': {}})))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Данные изменены. Откройте день заново.')
    return personal_cash_day_data(db, user.id, work_date)


class PersonalTableChange(BaseModel):
    version: int = Field(ge=0)
    action: Literal["cell", "rename", "add", "adjust", "manual_kickback"]
    day: int = Field(default=1, ge=1, le=31)
    column: int = Field(default=0, ge=0, le=39)
    value: str = Field(default="", max_length=100)


def _personal_month_days(month: str) -> int:
    import calendar
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise HTTPException(422, "Выберите месяц")
    try:
        first = date.fromisoformat(month + "-01")
    except ValueError:
        raise HTTPException(422, "Некорректный месяц")
    return calendar.monthrange(first.year, first.month)[1]


def _earnings_after_vlad(data, days, month, db):
    daily = {}
    for day in range(1, days + 1):
        gross = sum(data['cells'].get(f'{day}:{col}', 0) for col in range(len(data['columns'])))
        work_date = date.fromisoformat(month + '-01').replace(day=day)
        allowance = _vlad_allowance_cents(db, work_date) if db is not None else 0
        daily[str(day)] = gross - allowance
    return {'daily': daily,
            'first': sum(daily[str(day)] for day in range(1, 16)),
            'second': sum(daily[str(day)] for day in range(16, days + 1)),
            'total': sum(daily.values())}


def _personal_table_result(row, kind: str, month: str, db=None) -> dict:
    days = _personal_month_days(month)
    data = json.loads(row.payload) if row else {"columns": list(PERSONAL_TABLE_COLUMNS[kind]), "cells": {}}
    # Project legacy columns without deleting stored history during a read.
    keep = [i for i, name in enumerate(data["columns"])
            if not (kind == "kickbacks" and name.strip().casefold() == "заказы")]
    gbu_details = data.get("gbu_details", {})
    adjustments = data.get("marina_adjustments", {})
    old_cells = data["cells"]
    data = {"gbu_details": gbu_details, "marina_adjustments": adjustments, "columns": [data["columns"][i] for i in keep],
            "cells": {f"{day}:{col}": old_cells[f"{day}:{old}"]
                      for col, old in enumerate(keep) for day in range(1, days + 1)
                      if f"{day}:{old}" in old_cells}}
    automatic = [0, 1] if kind == "kickbacks" else ([0] if kind == "earnings" else [])
    if automatic:
        data["columns"][0] = "Касса" if kind == "earnings" else "Марине"
        for day in range(1, days + 1):
            data["cells"].pop(f"{day}:0", None)
        if db is not None:
            start = date.fromisoformat(month + "-01")
            entries = _all_cash_rows(db, start, start.replace(day=days))
            field = "commission_rub" if kind == "earnings" else "kickback_rub"
            for entry in entries:
                key = f"{entry.work_date.day}:0"
                data["cells"][key] = data["cells"].get(key, 0) + int(getattr(entry, field) or 0) * 100
    if kind == "kickbacks":
        data["columns"][1] = "Владу"
        for day in range(1, days + 1):
            key = f"{day}:1"
            data["cells"].pop(key, None)
            if db is not None:
                work_date = date.fromisoformat(month + "-01").replace(day=day)
                data["cells"][key] = _vlad_allowance_cents(db, work_date)
    if kind == "kickbacks" and db is not None:
        for day in range(1, days + 1):
            delta = int(adjustments.get(str(day), 0))
            if delta:
                key = f"{day}:0"
                data["cells"][key] = data["cells"].get(key, 0) + delta
    if kind == 'earnings':
        if db is not None:
            for day in range(1, days + 1):
                key = f'{day}:1'
                if str(day) in gbu_details or key not in data['cells']:
                    detail = gbu_earnings_day(db, date.fromisoformat(month + '-01').replace(day=day), gbu_details.get(str(day)))
                    if detail['items'] or str(day) in gbu_details:
                        data['cells'][key] = detail['total']
        if db is not None and row is not None:
            start = date.fromisoformat(month + '-01')
            for detail in db.scalars(select(PersonalCashDay).where(
                    PersonalCashDay.owner_id == row.owner_id,
                    PersonalCashDay.work_date >= start,
                    PersonalCashDay.work_date <= start.replace(day=days))):
                values = json.loads(detail.payload)
                data['cells'][f'{detail.work_date.day}:2'] = sum(values.get('amounts', {}).values()) + values.get('own', 0) + values.get('legacy', 0)
        data['net_after_vlad'] = _earnings_after_vlad(data, days, month, db)
    sums = lambda first, last: [sum(data["cells"].get(f"{day}:{col}", 0) for day in range(first, last + 1)) for col in range(len(data["columns"]))]
    return {**data, "automatic_columns": automatic, "month": month, "kind": kind, "days": days, "version": row.version if row else 0,
            "first": sums(1, 15), "second": sums(16, days), "total": sums(1, days)}


@app.get("/api/personal-tables/{kind}/{month}")
def personal_table_get(kind: Literal["earnings", "cash", "kickbacks"], month: str,
                       db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    _personal_month_days(month)
    row = db.execute(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == user.id,
        PersonalMonthlyTable.month == month, PersonalMonthlyTable.kind == kind)).scalar_one_or_none()
    result = _personal_table_result(row, kind, month, db)
    if kind == "earnings":
        result["gbu_comment"] = personal_gbu_comment_data(db, user.id, month)
    return result


@app.patch("/api/personal-tables/{kind}/{month}")
def personal_table_edit(kind: Literal["earnings", "cash", "kickbacks"], month: str, change: PersonalTableChange,
                        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    from decimal import Decimal, InvalidOperation
    _require_owner(db, user)
    days = _personal_month_days(month)
    if kind == 'earnings' and change.action == 'cell' and change.column == 2 and change.day <= days:
        if db.get(PersonalCashDay, (user.id, date.fromisoformat(month + '-01').replace(day=change.day))):
            raise HTTPException(409, 'Редактируйте наличку в списке заказов за день.')
    row = db.execute(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == user.id,
        PersonalMonthlyTable.month == month, PersonalMonthlyTable.kind == kind).with_for_update()).scalar_one_or_none()
    if change.version != (row.version if row else 0):
        raise HTTPException(409, "Таблица изменена в другом окне. Откройте её заново и повторите правку.")
    data = _personal_table_result(row, kind, month)
    columns, cells = data["columns"], data["cells"]
    if change.action not in ("add", "adjust", "manual_kickback") and change.column in data["automatic_columns"]:
        raise HTTPException(422, "Этот столбец рассчитывается автоматически")
    if change.action != "add" and change.column >= len(columns):
        raise HTTPException(422, "Столбец не найден")
    if change.action == "manual_kickback":
        if kind != "kickbacks" or change.column != 0 or change.day > days:
            raise HTTPException(422, "Выберите день отката")
        try:
            amount = Decimal(change.value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".") or "0")
            if not amount.is_finite() or abs(amount) > Decimal("1000000000") or amount != amount.quantize(Decimal("0.01")):
                raise InvalidOperation
        except InvalidOperation:
            raise HTTPException(422, "Введите сумму с точностью до копеек")
        data["marina_adjustments"][str(change.day)] = int(amount * 100)
    elif change.action == "adjust":
        if kind != "kickbacks" or change.column != 0 or change.value not in ("250", "-250") or change.day > days:
            raise HTTPException(422, "Поправка Марине: только +250 или −250 ₽ за выбранный день")
        key = str(change.day)
        data["marina_adjustments"][key] = int(data["marina_adjustments"].get(key, 0)) + int(change.value) * 100
    elif change.action == "cell":
        if change.day > days:
            raise HTTPException(422, "В выбранном месяце нет такого дня")
        raw = change.value.strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        key = f"{change.day}:{change.column}"
        if not raw:
            cells.pop(key, None)
        else:
            try:
                amount = Decimal(raw)
                if not amount.is_finite() or abs(amount) > Decimal("1000000000") or amount != amount.quantize(Decimal("0.01")):
                    raise InvalidOperation
            except InvalidOperation:
                raise HTTPException(422, "Введите сумму с точностью до копеек")
            cells[key] = int(amount * 100)
    else:
        name = change.value.strip()
        if kind == "kickbacks" and name.casefold() == "заказы":
            raise HTTPException(422, "Заказы учитываются на главном экране")
        if not name:
            raise HTTPException(422, "Введите название столбца")
        if change.action == "add":
            if len(columns) >= 40:
                raise HTTPException(422, "Допускается не более 40 столбцов")
            columns.append(name)
        else:
            columns[change.column] = name
    if row is None:
        row = PersonalMonthlyTable(owner_id=user.id, month=month, kind=kind, version=0)
        db.add(row)
    row.payload = json.dumps({"columns": columns, "cells": cells, "marina_adjustments": data["marina_adjustments"], "gbu_details": data.get("gbu_details", {})}, ensure_ascii=False)
    row.version += 1
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Таблица уже создана в другом окне. Откройте её заново.")
    result = _personal_table_result(row, kind, month, db)
    if kind == "earnings":
        result["gbu_comment"] = personal_gbu_comment_data(db, user.id, month)
    return result


class DispatchDayCounts(Base):
    __tablename__ = "dispatch_day_counts"
    work_date = Column(Date, primary_key=True)
    private_count = Column(Integer, nullable=True)
    gbu_count = Column(Integer, nullable=True)
    requested_staff = Column(Integer, nullable=False, default=0)

class DispatchCountChange(BaseModel):
    field: Literal["private_count", "gbu_count", "requested_staff"]
    value: int = Field(ge=0, le=100000, strict=True)

@app.patch("/api/dispatch-counts/{work_date}")
def edit_dispatch_count(work_date: date, change: DispatchCountChange,
                        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    raise HTTPException(422, "Количество сотрудников рассчитывается по категориям частных заказов и ГБУ")


ORDER_COUNT_TYPES = {
    'private': ['standard:3', 'standard:4', 'standard:5', 'standard:6', 'vip:4', 'vip:6', 'elite:4', 'elite:6', 'carryout:2', 'carryout:3', 'carryout:4'],
    'gbu': ['standard:4', 'standard:6', 'elite:4', 'elite:6'],
}


class DispatchCategoryCounts(Base):
    __tablename__ = 'dispatch_category_counts'
    work_date = Column(Date, primary_key=True)
    source = Column(String(20), primary_key=True)
    payload = Column(String, nullable=False)
    unallocated = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)


class CategoryCountsChange(BaseModel):
    counts: dict[str, StrictInt]
    unallocated: int = Field(default=0, ge=0, le=100000, strict=True)
    version: int = Field(ge=0, strict=True)


def valid_order_count_key(source, key):
    if key in ORDER_COUNT_TYPES[source]:
        return True
    return source == 'private' and bool(re.fullmatch(r'(standard|vip|elite):(?:[1-9]|[1-9][0-9]|100)', key))


def _category_counts(db, work_date, source):
    row = db.get(DispatchCategoryCounts, (work_date, source))
    counts = dict.fromkeys(ORDER_COUNT_TYPES[source], 0)
    if row:
        counts.update(json.loads(row.payload))
        return {'counts': counts, 'unallocated': row.unallocated, 'total': sum(counts.values())+row.unallocated, 'version': row.version}
    manual = db.get(DispatchDayCounts, work_date)
    old_total = getattr(manual, source+'_count') if manual else None
    unallocated = 0
    if old_total is not None:
        # Old numeric overrides have no reliable category breakdown.
        unallocated = old_total
    else:
        for order in db.scalars(select(Order).where(Order.work_date==work_date, Order.source==source, Order.status!='cancelled')):
            key=f'{order.category}:{order.team_size}'
            if valid_order_count_key(source, key): counts[key]=counts.get(key,0)+1
            else: unallocated+=1
    return {'counts':counts,'unallocated':unallocated,'total':sum(counts.values())+unallocated,'version':0}


def _requested_staff_by_source(db, work_date, *, include_carryouts=True):
    return {source: sum(int(key.rsplit(":", 1)[1]) * count
                        for key, count in _category_counts(db, work_date, source)["counts"].items()
                        if include_carryouts or not key.startswith("carryout:"))
            for source in ORDER_COUNT_TYPES}


def _requested_staff_from_orders(db, work_date, *, include_carryouts=True):
    return sum(_requested_staff_by_source(db, work_date, include_carryouts=include_carryouts).values())


VLAD_ALLOWANCE_RATE_CENTS = 10000  # 100 ₽ per requested staff place, excluding carryouts.


def _vlad_allowance_cents(db, work_date):
    return _requested_staff_from_orders(db, work_date, include_carryouts=False) * VLAD_ALLOWANCE_RATE_CENTS


def _vlad_allowance_month(db, month):
    days = _personal_month_days(month)
    first = date.fromisoformat(month + "-01")
    daily = []
    for day in range(1, days + 1):
        work_date = first.replace(day=day)
        amount = _vlad_allowance_cents(db, work_date)
        daily.append({"work_date": work_date.isoformat(), "day": day,
                      "staff_count": amount // VLAD_ALLOWANCE_RATE_CENTS, "amount": amount})
    return {"month": month, "days": days, "rate": VLAD_ALLOWANCE_RATE_CENTS, "daily": daily,
            "first": sum(x["amount"] for x in daily if x["day"] <= 15),
            "second": sum(x["amount"] for x in daily if x["day"] >= 16),
            "total": sum(x["amount"] for x in daily)}


@app.get('/api/vlad-allowance/{month}')
def get_vlad_allowance(month: str, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _can_view_vlad_allowance(db, user):
        raise HTTPException(403, "Надбавка доступна Владу Рязанскому и владельцу")
    return _vlad_allowance_month(db, month)


@app.get('/api/order-counts/{work_date}/{source}')
def get_category_counts(work_date: date, source: Literal['private','gbu'], db: Session=Depends(get_db), user: TelegramUser=Depends(require_admin)):
    return _category_counts(db,work_date,source)


@app.put('/api/order-counts/{work_date}/{source}')
def save_category_counts(work_date: date, source: Literal['private','gbu'], change: CategoryCountsChange,
                         db: Session=Depends(get_db), user: TelegramUser=Depends(require_admin)):
    if (not set(ORDER_COUNT_TYPES[source]).issubset(change.counts)
            or any(not valid_order_count_key(source,k) for k in change.counts)
            or any(type(v) is not int or not 0<=v<=100000 for v in change.counts.values())):
        raise HTTPException(422,'Введите целые неотрицательные количества для допустимых категорий')
    total=sum(change.counts.values())+change.unallocated
    if total>100000: raise HTTPException(422,'Слишком большое количество заказов')
    row=db.execute(select(DispatchCategoryCounts).where(DispatchCategoryCounts.work_date==work_date, DispatchCategoryCounts.source==source).with_for_update()).scalar_one_or_none()
    if (row.version if row else 0)!=change.version:
        raise HTTPException(409,'Данные изменены в другом окне. Откройте категории заново.')
    if row is None:
        row=DispatchCategoryCounts(work_date=work_date,source=source,version=0)
        db.add(row)
    row.payload=json.dumps(change.counts);row.unallocated=change.unallocated;row.version+=1
    try:
        db.flush()
        # Keep existing day cards and date strip compatible with the new breakdown.
        manual=db.execute(select(DispatchDayCounts).where(DispatchDayCounts.work_date==work_date).with_for_update()).scalar_one_or_none()
        if manual is None:
            manual=DispatchDayCounts(work_date=work_date,requested_staff=0);db.add(manual)
        setattr(manual,source+'_count',total)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'Данные изменены в другом окне. Откройте категории заново.')
    return {'saved':True,'total':total,'version':row.version}


@app.get('/api/order-count-period')
def order_count_period(month: str, half: Literal['first','second']='first', db: Session=Depends(get_db), user: TelegramUser=Depends(require_admin)):
    days=_personal_month_days(month)
    first=date.fromisoformat(month+'-01').replace(day=1 if half=='first' else 16)
    last=first.replace(day=15 if half=='first' else days)
    result={src:{'counts':dict.fromkeys(keys,0),'unallocated':0,'total':0} for src,keys in ORDER_COUNT_TYPES.items()}
    daily=[]
    for offset in range((last-first).days+1):
        day=first+timedelta(days=offset);item={'date':day.isoformat()}
        for src in ORDER_COUNT_TYPES:
            values=_category_counts(db,day,src);item[src]=values['total']
            for key,value in values['counts'].items():result[src]['counts'][key]=result[src]['counts'].get(key,0)+value
            result[src]['unallocated']+=values['unallocated'];result[src]['total']+=values['total']
        item['total']=item['private']+item['gbu'];daily.append(item)
    return {'start':first.isoformat(),'end':last.isoformat(),'days':days,'sources':result,'daily':daily,'total':sum(x['total'] for x in result.values())}

class NumberCardEvent(Base):
    __tablename__ = "number_card_events"
    id = Column(Integer, primary_key=True)
    chat_id = Column(BigInteger, nullable=False)
    message_id = Column(BigInteger, nullable=False)
    work_date = Column(Date, nullable=False, index=True)
    tg_id = Column(BigInteger, nullable=False)
    full_name = Column(String(200), nullable=False)
    phone = Column(String(50), nullable=False)
    source = Column(String(20), nullable=False)
    surname = Column(String(100), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("chat_id", "message_id", name="uq_number_card_message"),)

class NumberCardInput(BaseModel):
    chat_id: int = Field(lt=0)
    message_id: int = Field(gt=0)
    work_date: date
    tg_id: int = Field(gt=0)
    full_name: str = Field(max_length=200)
    phone: str = Field(max_length=50)
    source: Literal["private", "gbu"]
    surname: str = Field(max_length=100)
    sent_at: datetime



# Durable transfer requests: card identity and all completion records stay intact.
class NumberReassignment(Base):
    __tablename__ = 'number_reassignments'
    event_id = Column(Integer, primary_key=True)
    token = Column(String(64), nullable=False, index=True)
    payload = Column(String, nullable=False)
    status = Column(String(20), nullable=False, default='pending')
    error = Column(String, nullable=False, default='')


class NumberReassignInput(BaseModel):
    event_ids: list[int] = Field(min_length=1, max_length=50)
    old_tg_id: int = Field(gt=0)
    new_tg_id: int = Field(gt=0)
    keep_contact: bool = False


@app.get('/api/number-reassign/brigadiers')
def reassignment_candidates(db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    return {'items': [_employee_dict(e) for e in db.scalars(select(Employee).where(
        Employee.active.is_(True), Employee.group_code=='brigadier',
        Employee.tg_id.is_not(None), _visible_employee()).order_by(Employee.full_name))]}


@app.post('/api/number-reassign')
def request_number_reassignment(data: NumberReassignInput, db: Session=Depends(get_db), user: TelegramUser=Depends(require_admin)):
    ids=sorted(set(data.event_ids))
    rows=db.scalars(select(NumberCardEvent).where(NumberCardEvent.id.in_(ids)).order_by(NumberCardEvent.id).with_for_update()).all()
    target=db.scalar(select(Employee).where(Employee.tg_id==data.new_tg_id, Employee.active.is_(True),
        Employee.group_code=='brigadier', _visible_employee()))
    if not target or not target.phone or data.new_tg_id==data.old_tg_id:
        raise HTTPException(422,'Выберите другого действующего бригадира с номером телефона')
    if len(rows)!=len(ids) or len({r.work_date for r in rows})!=1 or any(
            r.tg_id!=data.old_tg_id or db.get(NumberCardDeletion,r.id) for r in rows):
        raise HTTPException(409,'Заказы изменились. Откройте список заново')
    if any((job:=db.get(NumberReassignment,r.id)) and job.status=='pending' for r in rows):
        raise HTTPException(409,'Замена этих заказов уже выполняется')
    token=secrets.token_hex(16)
    payload={'token':token,'old_tg_id':data.old_tg_id,'new_tg_id':data.new_tg_id,
        'label':_employee_dict(target)['display_name'],'phone':brigadier_phone8(target.phone),
        'keep_contact':data.keep_contact,'requested_by':user.id,
        'work_date':rows[0].work_date.isoformat(),'cards':[{
            'event_id':r.id,'chat_id':r.chat_id,'message_id':r.message_id,'source':r.source,
            'surname':r.surname} for r in rows]}
    for r in rows:
        job=db.get(NumberReassignment,r.id)
        if not job: job=NumberReassignment(event_id=r.id);db.add(job)
        job.token=token;job.payload=json.dumps(payload,ensure_ascii=False);job.status='pending';job.error=''
    db.commit()
    return {'token':token,'status':'pending'}


@app.get('/api/number-reassign/{token}')
def number_reassignment_status(token:str, db:Session=Depends(get_db), user:TelegramUser=Depends(require_admin)):
    job=db.scalar(select(NumberReassignment).where(NumberReassignment.token==token))
    if not job: raise HTTPException(404,'Замена не найдена')
    return {'status':job.status,'error':job.error}


@app.get('/api/bot/number-reassign', dependencies=[Depends(_bot_key)])
def pending_number_reassignments(db:Session=Depends(get_db)):
    jobs=db.scalars(select(NumberReassignment).where(NumberReassignment.status=='pending')).all()
    return {'items':list({j.token:json.loads(j.payload) for j in jobs}.values())}


class NumberReassignAck(BaseModel):
    error: str = Field(default='',max_length=1000)


@app.post('/api/bot/number-reassign/{token}', dependencies=[Depends(_bot_key)])
def acknowledge_number_reassignment(token:str, data:NumberReassignAck, db:Session=Depends(get_db)):
    jobs=db.scalars(select(NumberReassignment).where(NumberReassignment.token==token).order_by(NumberReassignment.event_id).with_for_update()).all()
    if not jobs: raise HTTPException(404,'Замена не найдена')
    if all(j.status=='done' for j in jobs): return {'ok':True}
    if any(j.status!='pending' for j in jobs): return {'ok':False}
    payload=json.loads(jobs[0].payload)
    if data.error:
        for j in jobs:j.status='failed';j.error=data.error
    else:
        day=date.fromisoformat(payload['work_date'])
        for j in jobs:
            r=db.get(NumberCardEvent,j.event_id)
            contact=db.get(OrderBrigadeContact,r.id)
            keep_contact=bool(payload['keep_contact'] and contact and contact.tg_id==r.tg_id and contact.confirmed)
            r.tg_id=payload['new_tg_id'];r.full_name=payload['label'];r.phone=payload['phone']
            _set_order_contact(db,r,keep_contact,payload['requested_by'])
            photo=db.get(PhotoReportEvent,f'card:{r.chat_id}:{r.message_id}')
            if photo:photo.tg_id=payload['new_tg_id']
            j.status='done'
        db.add(ImportLog(kind='number_reassignment',created_by_tg_id=payload['requested_by'],
            raw_text='Замена бригадира',result_json=jobs[0].payload))
    db.commit()
    return {'ok':True}




class NumberCardDeletion(Base):
    __tablename__ = 'number_card_deletions'
    event_id = Column(Integer, primary_key=True)
    deleted_by_tg_id = Column(BigInteger, nullable=False)
    deleted_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class NumberCardClosure(Base):
    __tablename__ = 'number_card_closures_v2'
    event_id = Column(Integer, primary_key=True)
    report_chat_id = Column(BigInteger, nullable=False)
    report_message_id = Column(BigInteger, nullable=False)
    received_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class NumberCardClosureInput(BaseModel):
    card: NumberCardInput
    report_chat_id: int = Field(lt=0)
    report_message_id: int = Field(gt=0)


class PhotoReportEvent(Base):
    __tablename__ = 'photo_report_events'
    report_key = Column(String(180), primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    work_date = Column(Date, nullable=False, index=True)
    source = Column(String(20), nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class PhotoReportInput(BaseModel):
    report_key: str = Field(min_length=1, max_length=180)
    tg_id: int = Field(gt=0)
    work_date: date
    source: Literal['private', 'gbu']
    active: bool = True


class OrderPhotoControl(Base):
    __tablename__ = 'order_photo_control'
    report_key = Column(String(180), primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    photo_time = Column(String(5), nullable=True)
    first_received = Column(Boolean, nullable=False, default=False)
    second_received = Column(Boolean, nullable=False, default=False)


class OrderReportMedia(Base):
    # Separate table: existing photo-control tables need no column migration.
    __tablename__ = 'order_report_media'
    report_key = Column(String(180), primary_key=True)
    stage = Column(Integer, primary_key=True)
    media = Column(String, nullable=False)
    fingerprint = Column(String(64), nullable=False)


class OrderReportFraming(Base):
    __tablename__ = 'order_report_framing'
    report_key = Column(String(180), primary_key=True)
    stage = Column(Integer, primary_key=True)
    original_image = Column(String, nullable=False)


class OrderReportMediaInput(BaseModel):
    report_key: str = Field(pattern=r'^card:-\d+:\d+$', max_length=180)
    tg_id: int = Field(gt=0)
    stage: Literal[1, 2]
    media: str = Field(min_length=1, max_length=5600000)


def _report_media_bytes(media):
    import base64
    if not media.startswith('data:image/jpeg;base64,'):
        return None
    try:
        raw = base64.b64decode(media.split(',', 1)[1], validate=True)
    except ValueError:
        raise HTTPException(422, 'Не удалось прочитать фото')
    if not 100 <= len(raw) <= 4 * 1024 * 1024 or not raw.startswith(b'\xff\xd8\xff'):
        raise HTTPException(422, 'Нужен JPEG размером до 4 МБ')
    return raw


@app.post('/api/bot/order-report-media', dependencies=[Depends(_bot_key)])
def save_order_report_media(data: OrderReportMediaInput, db: Session = Depends(get_db)):
    _, chat, message = data.report_key.split(':')
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == int(chat),
        NumberCardEvent.message_id == int(message), NumberCardEvent.tg_id == data.tg_id,
        NumberCardEvent.tg_id.notin_(HIDDEN_TG_IDS), _visible_number_card()))
    if not card or (data.stage == 2 and card.source != 'gbu'):
        raise HTTPException(404, 'Заказ не найден')
    raw = _report_media_bytes(data.media)
    if raw is None and not re.fullmatch(r'[A-Za-z0-9_-]{10,512}', data.media):
        raise HTTPException(422, 'Некорректная ссылка на фото')
    fingerprint = hashlib.sha256(data.media.encode()).hexdigest()
    row = db.get(OrderReportMedia, (data.report_key, data.stage))
    if row is None:
        row = OrderReportMedia(report_key=data.report_key, stage=data.stage)
        db.add(row)
    row.media, row.fingerprint = data.media, fingerprint
    db.commit()
    return {'ok': True}


@app.get('/api/owner/order-report-media/{card_id}/{stage}')
async def read_order_report_media(card_id: int, stage: int,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if stage not in (1, 2):
        raise HTTPException(422, 'Номер фото должен быть 1 или 2')
    # Never expose bot credentials, Telegram paths, or public photo URLs.
    if not _dispatcher_access(db, user.id) or cabinet_kind(db, user.id) not in {'owner', 'assistant'}:
        raise HTTPException(403, 'Фото доступны владельцу и диспетчерам')
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.id == card_id,
        NumberCardEvent.tg_id.notin_(HIDDEN_TG_IDS), _visible_number_card()))
    if not card or (stage == 2 and card.source != 'gbu'):
        raise HTTPException(404, 'Фото не найдено')
    row = db.get(OrderReportMedia, (f'card:{card.chat_id}:{card.message_id}', stage))
    if not row:
        raise HTTPException(404, 'Фото ещё не синхронизировано')
    raw = _report_media_bytes(row.media)
    if raw is None:
        import httpx
        token = os.getenv('BOT_TOKEN', '').strip()
        if not token:
            raise HTTPException(503, 'Просмотр фото временно недоступен')
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                info = await client.get(f'https://api.telegram.org/bot{token}/getFile',
                    params={'file_id': row.media})
                info.raise_for_status()
                result = info.json()
                path = (result.get('result') or {}).get('file_path', '')
                if not result.get('ok') or not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path or path.startswith('/'):
                    raise ValueError('file unavailable')
                chunks, size = [], 0
                async with client.stream('GET', f'https://api.telegram.org/file/bot{token}/{path}') as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 8 * 1024 * 1024:
                            raise ValueError('file too large')
                        chunks.append(chunk)
                raw = b''.join(chunks)
                if not raw.startswith(b'\xff\xd8\xff'):
                    raise ValueError('not a photo')
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(502, 'Не удалось загрузить фото. Попробуйте ещё раз') from None
    import base64
    return JSONResponse({'image': 'data:image/jpeg;base64,' + base64.b64encode(raw).decode(),
        'fingerprint': row.fingerprint}, headers={'Cache-Control': 'private, no-store'})


class OrderPhotoOverride(Base):
    __tablename__ = 'order_photo_overrides'
    event_id = Column(Integer, primary_key=True)
    # NULL follows the bot; explicit True/False is an owner's correction.
    first_received = Column(Boolean, nullable=True)
    second_received = Column(Boolean, nullable=True)
    updated_by_tg_id = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OrderPhotoOverrideInput(BaseModel):
    stage: Literal[1, 2]
    received: bool | None = Field(..., strict=True)


def _canonical_cabinet_clock(value):
    raw = str(value or '').strip()
    if re.fullmatch(r'\d{3,4}', raw):
        raw = raw[:-2] + ':' + raw[-2:]
    match = re.fullmatch(r'(\d{1,2})[:.](\d{2})', raw)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    return f'{int(match[1]):02d}:{int(match[2]):02d}'


def effective_order_photo(db: Session, card):
    key = f'card:{card.chat_id}:{card.message_id}'
    control = db.get(OrderPhotoControl, key)
    photo = db.get(PhotoReportEvent, key)
    override = db.get(OrderPhotoOverride, card.id)
    first = bool(control.first_received if control else photo and photo.active)
    second = bool(control and control.second_received) if card.source == 'gbu' else False
    manual_first = override.first_received if override else None
    manual_second = override.second_received if override and card.source == 'gbu' else None
    return {'first_received': first if manual_first is None else manual_first,
            'second_received': second if manual_second is None else manual_second,
            'first_manual': manual_first is not None, 'second_manual': manual_second is not None,
            'photo_time': _canonical_cabinet_clock(control.photo_time) if control else None}


@app.patch('/api/photo-control/{event_id}')
def set_photo_override(event_id: int, data: OrderPhotoOverrideInput,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _is_owner(db, user.id):
        raise HTTPException(403, 'Ручные отметки фото доступны только владельцу')
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.id == event_id,
        _visible_number_card()).with_for_update())
    if card is None:
        raise HTTPException(404, 'Заказ не найден или удалён')
    if card.source not in ('private', 'gbu') or (data.stage == 2 and card.source != 'gbu'):
        raise HTTPException(400, 'У частного заказа одна отметка фото')
    job = db.get(NumberReassignment, event_id)
    if job and job.status == 'pending':
        raise HTTPException(409, 'Дождитесь завершения замены бригадира')
    override = db.get(OrderPhotoOverride, event_id)
    if override is None:
        override = OrderPhotoOverride(event_id=event_id, updated_by_tg_id=user.id)
        db.add(override)
    field = 'first_received' if data.stage == 1 else 'second_received'
    previous = getattr(override, field)
    setattr(override, field, data.received)
    override.updated_by_tg_id = user.id
    override.updated_at = datetime.utcnow()
    db.add(ImportLog(kind='photo_override', created_by_tg_id=user.id,
        raw_text='Ручная отметка фото', result_json=json.dumps({
            'event_id': event_id, 'work_date': card.work_date.isoformat(),
            'source': card.source, 'stage': data.stage, 'previous': previous,
            'received': data.received})))
    db.commit()
    return {'ok': True, 'id': card.id, **effective_order_photo(db, card),
            'counts': photo_report_counts(db, card.work_date)}


class OrderContactSchedule(Base):
    __tablename__ = 'order_contact_schedule'
    report_key = Column(String(180), primary_key=True)
    contact_time = Column(String(5), nullable=True)


class OrderPhotoControlInput(BaseModel):
    report_key: str = Field(pattern=r'^card:-\d+:\d+$', max_length=180)
    tg_id: int = Field(gt=0)
    photo_time: str | None = Field(default=None, pattern=r'^([01]?\d|2[0-3]):[0-5]\d$')
    first_received: bool = False
    second_received: bool = False
    contact_time: str | None = Field(default=None, pattern=r'^([01]?\d|2[0-3]):[0-5]\d$')


@app.post('/api/bot/photo-control', dependencies=[Depends(_bot_key)])
def save_photo_control(data: list[OrderPhotoControlInput], db: Session=Depends(get_db)):
    if len(data)>500: raise HTTPException(400, 'Слишком много записей')
    for item in data:
        _, chat, message = item.report_key.split(':')
        card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id==int(chat),
            NumberCardEvent.message_id==int(message), _visible_number_card()))
        if not card or card.tg_id != item.tg_id: continue
        row=db.get(OrderPhotoControl,item.report_key)
        if row is None:
            row=OrderPhotoControl(report_key=item.report_key);db.add(row)
        row.tg_id=item.tg_id
        row.photo_time=item.photo_time
        row.first_received=item.first_received
        row.second_received=item.second_received
        if item.contact_time:
            schedule=db.get(OrderContactSchedule,item.report_key)
            if schedule is None:
                schedule=OrderContactSchedule(report_key=item.report_key);db.add(schedule)
            schedule.contact_time=item.contact_time
    db.commit()
    return {'ok':True}


@app.get('/api/brigade-contact-orders')
def list_brigade_contact_orders(work_date:date, db:Session=Depends(get_db),
        user:TelegramUser=Depends(require_admin)):
    cards=db.scalars(select(NumberCardEvent).where(
        NumberCardEvent.work_date==work_date,_visible_number_card())).all()
    states=_order_contact_states(db,work_date)
    employees={e.tg_id:e for e in db.scalars(select(Employee).where(
        Employee.tg_id.in_([c.tg_id for c in cards]),_visible_employee()))} if cards else {}
    items=[]
    for card in cards:
        if card.tg_id in HIDDEN_TG_IDS: continue
        schedule=db.get(OrderContactSchedule,f'card:{card.chat_id}:{card.message_id}')
        items.append({'id':card.id,'employee':_employee_dict(employees[card.tg_id]) if card.tg_id in employees else {'full_name':card.full_name},
            'source':card.source,'surname':card.surname,
            'contact_time':schedule.contact_time if schedule else None,
            'on_contact':bool(states.get(card.id))})
    def sort_key(item):
        clock=item['contact_time']
        return (sum(int(v)*m for v,m in zip(clock.split(':'),(60,1))) if clock else 1440,item['id'])
    items.sort(key=sort_key)
    return {'work_date':work_date.isoformat(),'items':items}


@app.get('/api/photo-control')
def list_photo_control(work_date:date, source:Literal['private','gbu'],
        db:Session=Depends(get_db), user:TelegramUser=Depends(require_admin)):
    cards=db.scalars(select(NumberCardEvent).where(NumberCardEvent.work_date==work_date,
        NumberCardEvent.source==source,_visible_number_card())).all()
    employees={e.tg_id:e for e in db.scalars(select(Employee).where(Employee.tg_id.in_([c.tg_id for c in cards])))} if cards else {}
    result=[]
    for card in cards:
        result.append({'id':card.id,'employee':_employee_dict(employees[card.tg_id]) if card.tg_id in employees else {'full_name':card.full_name},
            'surname':card.surname, **effective_order_photo(db, card)})
    def sort_key(item):
        clock=item['photo_time']
        minutes=sum(int(v)*m for v,m in zip(clock.split(':'),(60,1))) if clock else 1440
        return minutes,item['id']
    result.sort(key=sort_key)
    return {'work_date':work_date.isoformat(),'source':source,'items':result,
            'can_edit': _is_owner(db, user.id)}


@app.post('/api/bot/photo-reports', dependencies=[Depends(_bot_key)])
def bot_photo_report(data: PhotoReportInput, db: Session = Depends(get_db)):
    row = db.get(PhotoReportEvent, data.report_key)
    if row and (row.tg_id, row.work_date, row.source) != (data.tg_id, data.work_date, data.source):
        raise HTTPException(409, 'Фотоотчёт уже привязан к другой дате или категории')
    if row is None:
        row = PhotoReportEvent(**data.model_dump())
        db.add(row)
    else:
        row.active = data.active
    db.commit()
    return {'ok': True}


def photo_report_counts(db: Session, work_date: date):
    counts = {'private': 0, 'gbu': 0, 'gbu_first': 0, 'gbu_second': 0}
    cards = db.scalars(select(NumberCardEvent).where(
        NumberCardEvent.work_date == work_date, _visible_number_card())).all()
    for card in cards:
        if card.source not in {'private', 'gbu'}:
            continue
        photo = effective_order_photo(db, card)
        if photo['first_received']:
            counts[card.source] += 1
        if card.source == 'gbu':
            counts['gbu_first'] += int(bool(photo['first_received']))
            counts['gbu_second'] += int(bool(photo['second_received']))
    return counts


def _visible_number_card():
    return ~select(NumberCardDeletion.event_id).where(NumberCardDeletion.event_id == NumberCardEvent.id).exists()


def number_card_statuses(db: Session, cards):
    if not cards:
        return {}
    closed_ids = set(db.scalars(select(NumberCardClosure.event_id).where(
        NumberCardClosure.event_id.in_([card.id for card in cards]))))
    keys = [f'card:{card.chat_id}:{card.message_id}' for card in cards]
    schedules = {row.report_key: row.contact_time for row in db.scalars(
        select(OrderContactSchedule).where(OrderContactSchedule.report_key.in_(keys)))}
    result = {}
    for card, key in zip(cards, keys):
        clock = schedules.get(key) or None
        # The bot stores this time only after the complete evening report is
        # accepted. Its publication can still be waiting for the other numbers.
        result[card.id] = {'closed': card.id in closed_ids or bool(clock), 'contact_time': clock}
    return result


def order_completion_data(db: Session, work_date: date):
    counts = {source: {'cards': 0, 'closed': 0} for source in ORDER_COUNT_TYPES}
    rows = db.scalars(select(NumberCardEvent).where(
        NumberCardEvent.work_date == work_date, _visible_number_card())).all()
    statuses = number_card_statuses(db, rows)
    for card in rows:
        if card.source in counts:
            counts[card.source]['cards'] += 1
            counts[card.source]['closed'] += int(statuses[card.id]['closed'])
    for source, item in counts.items():
        expected = int(_category_counts(db, work_date, source)['total'])
        item.update(expected=expected,
                    missing_numbers=max(0, expected - item['cards']),
                    extra_numbers=max(0, item['cards'] - expected),
                    missing_reports=max(0, expected - item['closed']),
                    extra_reports=max(0, item['closed'] - expected),
                    complete=expected > 0 and item['cards'] == expected and item['closed'] == expected)
    return {'work_date': work_date.isoformat(), 'sources': counts,
            'expected': sum(x['expected'] for x in counts.values()),
            'cards': sum(x['cards'] for x in counts.values()),
            'closed': sum(x['closed'] for x in counts.values())}


@app.get('/api/bot/order-completion', dependencies=[Depends(_bot_key)])
def bot_order_completion(work_date: date, db: Session = Depends(get_db)):
    result = order_completion_data(db, work_date)
    # The bot publishes queued reports only for the current, non-deleted cards.
    rows = db.scalars(select(NumberCardEvent).where(
        NumberCardEvent.work_date == work_date, _visible_number_card()
    ).order_by(NumberCardEvent.id)).all()
    states = _order_contact_states(db,work_date)
    result['per_order_contacts'] = True
    result['number_cards'] = [{'chat_id':row.chat_id,'message_id':row.message_id,
        'tg_id':row.tg_id,'source':row.source,'surname':row.surname,'full_name':row.full_name,
        'phone':brigadier_phone8(row.phone),'work_date':work_date.isoformat(),
        'sent_at':row.sent_at.isoformat(),
        'on_contact':bool(states.get(row.id))} for row in rows]
    overrides = {o.event_id: o for o in db.scalars(select(OrderPhotoOverride).where(
        OrderPhotoOverride.event_id.in_([r.id for r in rows])))} if rows else {}
    result['photo_overrides'] = {
        f'card:{r.chat_id}:{r.message_id}': {
            'first_received': overrides[r.id].first_received,
            'second_received': overrides[r.id].second_received if r.source == 'gbu' else None}
        for r in rows if r.id in overrides}
    return result


@app.post('/api/bot/number-cards/closed', dependencies=[Depends(_bot_key)])
def record_number_card_closure(data: NumberCardClosureInput, db: Session = Depends(get_db)):
    # The card can arrive with its report if its earlier sync was interrupted.
    result = record_number_card(data.card, db)
    card = db.get(NumberCardEvent, result['id'])
    if card.tg_id != data.card.tg_id or card.source != data.card.source or card.work_date != data.card.work_date:
        raise HTTPException(409, 'Данные карточки не совпадают')
    if db.get(NumberCardDeletion, card.id) is not None:
        return {'saved': False, 'ignored': True, 'id': card.id}
    row = db.get(NumberCardClosure, card.id)
    if row is None:
        row = NumberCardClosure(event_id=card.id, report_chat_id=data.report_chat_id,
                                report_message_id=data.report_message_id)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.get(NumberCardClosure, card.id)
    if row is None:
        raise HTTPException(409, 'Не удалось сохранить отчёт')
    row.report_chat_id = data.report_chat_id
    row.report_message_id = data.report_message_id
    db.commit()
    return {'saved': True, 'id': card.id}


@app.post('/api/bot/number-cards/edit', dependencies=[Depends(_bot_key)])
def edit_number_card(data: NumberCardInput, db: Session = Depends(get_db)):
    row = db.scalar(select(NumberCardEvent).where(
        NumberCardEvent.chat_id == data.chat_id, NumberCardEvent.message_id == data.message_id))
    if (row is None or row.tg_id != data.tg_id or row.work_date != data.work_date
            or db.get(NumberCardDeletion, row.id) is not None):
        return {'saved': False, 'error': 'Карточка удалена или недоступна.'}
    job=db.get(NumberReassignment,row.id)
    if job and job.status=='pending':
        return {'saved':False,'error':'Дождитесь завершения замены бригадира.'}
    photo = db.get(PhotoReportEvent, f'card:{row.chat_id}:{row.message_id}')
    if row.source != data.source and photo and photo.active:
        return {'saved': False, 'error': 'Категорию можно изменить до отправки фото по заказу.'}
    if row.source != data.source and db.scalar(select(CabinetOrderCash.event_id).where(CabinetOrderCash.event_id == row.id).limit(1)) is not None:
        return {'saved': False, 'error': 'После заполнения кассы тип заказа менять нельзя.'}
    surname = data.surname.strip().upper()
    if not surname:
        return {'saved': False, 'error': 'Укажите фамилию умершего.'}
    row.surname, row.source = surname, data.source
    db.commit()
    return {'saved': True, 'id': row.id}


@app.delete('/api/number-cards/{event_id}')
def delete_number_card(event_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    # Authenticated dispatchers may remove number records; financial rights stay unchanged.
    row = db.get(NumberCardEvent, event_id)
    if row is None:
        raise HTTPException(404, 'Запись не найдена')
    job=db.get(NumberReassignment,event_id)
    if job and job.status=='pending':
        raise HTTPException(409,'Дождитесь завершения замены бригадира')
    if db.get(NumberCardDeletion, event_id) is None:
        db.add(NumberCardDeletion(event_id=event_id, deleted_by_tg_id=user.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.get(NumberCardDeletion, event_id) is None:
                raise
    return {'ok': True, 'id': event_id}

@app.post("/api/bot/number-cards", dependencies=[Depends(_bot_key)])
def record_number_card(data: NumberCardInput, db: Session = Depends(get_db)):
    row = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == data.chat_id, NumberCardEvent.message_id == data.message_id))
    if row is None:
        row = NumberCardEvent(**{**data.model_dump(), "phone": brigadier_phone8(data.phone)})
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == data.chat_id, NumberCardEvent.message_id == data.message_id))
            if row is None:
                raise
    return {"saved": True, "id": row.id}

class BrigadierNumberDelete(BaseModel):
    tg_id: int = Field(gt=0)
    chat_id: int = Field(lt=0)
    message_id: int = Field(gt=0)
    full: StrictBool = False


@app.post('/api/bot/number-cards/delete', dependencies=[Depends(_bot_key)])
def delete_brigadier_number(data: BrigadierNumberDelete, db: Session=Depends(get_db)):
    row=db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id==data.chat_id,
        NumberCardEvent.message_id==data.message_id).with_for_update())
    if not row or row.tg_id!=data.tg_id:
        return {'saved':False,'error':'Номер не найден или принадлежит другому бригадиру.'}
    if data.full:
        cabinet_finance_access(db, data.tg_id)
    if db.get(NumberCardDeletion,row.id):return {'saved':True, 'work_date':row.work_date.isoformat()}
    job=db.get(NumberReassignment,row.id)
    if job and job.status=='pending':
        return {'saved':False,'error':'Дождитесь завершения замены бригадира.'}
    deletion = cabinet_order_deletion_info(db, row)
    if not deletion['can_delete']:
        return {'saved':False, 'error':deletion['delete_reason']}
    if db.get(NumberCardDeletion,row.id) is None:
        db.add(NumberCardDeletion(event_id=row.id,deleted_by_tg_id=data.tg_id))
    if data.full:
        key = f'card:{row.chat_id}:{row.message_id}'
        cash = db.get(CabinetOrderCash, (row.id, data.tg_id))
        if cash is not None:
            db.delete(cash)
        for model, identity in ((CabinetOrderState, key), (OrderPhotoControl, key),
                (OrderPhotoOverride, row.id), (PhotoReportEvent, key),
                (OrderContactSchedule, key), (NumberCardClosure, row.id),
                (OrderBrigadeContact, row.id)):
            record = db.get(model, identity)
            if record:
                db.delete(record)
    db.commit()
    return {'saved':True, 'work_date':row.work_date.isoformat()}


def number_card_contact_sort_key(row, statuses):
    value = str(statuses.get(row.id, {}).get('contact_time') or '').strip()
    match = re.fullmatch(r'(\d{1,2})[:.](\d{2})', value)
    minutes = 1440
    if match:
        hour, minute = map(int, match.groups())
        if 0 <= hour < 24 and 0 <= minute < 60:
            minutes = hour * 60 + minute
    return minutes, row.id


@app.get("/api/number-cards")
def list_number_cards(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    rows = db.scalars(select(NumberCardEvent).where(NumberCardEvent.work_date == work_date, _visible_number_card()).order_by(NumberCardEvent.sent_at, NumberCardEvent.id)).all()
    employees = {e.tg_id: e for e in db.scalars(select(Employee).where(Employee.tg_id.in_([r.tg_id for r in rows])))} if rows else {}
    statuses = number_card_statuses(db, rows)
    rows.sort(key=lambda row: number_card_contact_sort_key(row, statuses))
    return {"count": len(rows), "progress": order_completion_data(db, work_date), "items": [{"id": r.id, **statuses[r.id], "employee": _employee_dict(employees[r.tg_id]) if r.tg_id in employees else {"full_name": r.full_name, "display_name": r.full_name},
        "tg_id": r.tg_id, "phone": brigadier_phone8(r.phone), "source": r.source, "surname": r.surname,
        "sent_at": (r.sent_at.replace(tzinfo=timezone.utc) if r.sent_at.tzinfo is None else r.sent_at).isoformat()} for r in rows]}


@app.get("/api/bot/number-cards", dependencies=[Depends(_bot_key)])
def bot_number_card_history(tg_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(select(NumberCardEvent).where(NumberCardEvent.tg_id == tg_id, _visible_number_card()).order_by(NumberCardEvent.id)).all()
    return {"items": [{"chat_id":r.chat_id,"message_id":r.message_id,"work_date":r.work_date.isoformat(),
        "tg_id":r.tg_id,"full_name":r.full_name,"phone":brigadier_phone8(r.phone),"source":r.source,"surname":r.surname,
        "sent_at":r.sent_at.isoformat()} for r in rows]}


# Owner-only deletion inside the employee edit card.
_profile_start = INLINE_INDEX_HTML.index('function openEmployeeProfile(')
_profile_end = INLINE_INDEX_HTML.index('function openManualReadiness(', _profile_start)
_profile_html = INLINE_INDEX_HTML[_profile_start:_profile_end]
_profile_html = _profile_html.replace(
    '<button type="submit" class="primary">Сохранить</button></form>',
    '<div style="display:flex;align-items:center;justify-content:space-between;gap:16px"><button type="submit" class="primary">Сохранить</button>${state.bootstrap?.is_owner && Number(employee.tg_id)!==Number(state.bootstrap.user.id) ? `<button type="button" id="deleteEmployeeProfile" class="secondary danger" aria-label="Удалить сотрудника" title="Удалить сотрудника" style="min-width:44px;min-height:44px;padding:8px;font-size:22px">🗑</button>` : ""}</div></form>',
    1,
)
_profile_html = _profile_html.replace(
    "  qs('#employeeProfileForm').onsubmit=async event=>{",
    """  const deleteButton=qs('#deleteEmployeeProfile');
  if(deleteButton)deleteButton.onclick=async()=>{
    if(deleteButton.disabled)return;
    if(!confirm(`Удалить сотрудника «${employeeListName(employee)}» из рабочих списков? История заказов и кассы сохранится. Автоматическая синхронизация не вернёт его в список.`))return;
    const saveButton=qs('#employeeProfileForm button[type="submit"]');
    deleteButton.disabled=true;saveButton.disabled=true;
    try{
      await api(`/api/employees/${employee.id}`,{method:'DELETE'});
      closeModal();
      await loadReadiness();
      if(onSaved)await onSaved();
      state.bootstrap=await api(`/api/bootstrap?work_date=${state.selectedDate}`);
      renderSummary();restoreEmployeeScroll(position);
      toast('Сотрудник удалён из рабочих списков');
    }catch(error){toast(error.message,4500);deleteButton.disabled=false;saveButton.disabled=false;}
  };
  qs('#employeeProfileForm').onsubmit=async event=>{""",
    1,
)
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_profile_start] + _profile_html + INLINE_INDEX_HTML[_profile_end:]


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<div class="metric"><b>${s.orders_total}</b><span>Заказов</span></div>',
    '<button class="metric" id="orderPeriodBtn"><b>${s.orders_total}</b><span>Заказов · итоги ›</span></button>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "  qs('#numberCardsBtn').onclick=openNumberCards;",
    "  qs('#orderPeriodBtn').onclick=openOrderCountPeriod;\n  qs('#numberCardsBtn').onclick=openNumberCards;", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'async function editDispatchCount(field){',
    "async function editDispatchCount(field){\n if(field==='private_count'||field==='gbu_count'){await openOrderCategoryCounts(field==='private_count'?'private':'gbu');return}",1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</script>\n</body>', r'''
const orderCountTypes={private:['standard:3','standard:4','standard:5','standard:6','vip:4','vip:6','elite:4','elite:6','carryout:2','carryout:3','carryout:4'],gbu:['standard:4','standard:6','elite:4','elite:6']};
const orderCountLabel=key=>{const [cat,size]=key.split(':');return `${{standard:'Стандарт',vip:'Вип',elite:'Элит',carryout:'Вынос'}[cat]} · ${size} чел`};
function periodCountKeys(source,counts){return [...new Set([...orderCountTypes[source],...Object.keys(counts)])].sort((a,b)=>{const rank={standard:0,vip:1,elite:2,carryout:3};return rank[a.split(':')[0]]-rank[b.split(':')[0]]||Number(a.split(':')[1])-Number(b.split(':')[1])})}
async function openOrderCategoryCounts(source){
 const day=state.selectedDate;
 showModal(source==='gbu'?'Заказы ГБУ':'Частные заказы','<div id="categoryCountBody">Загрузка…</div>');
 qs('#modal').scrollTop=0;
 const body=qs('#categoryCountBody');
 try{
  const data=await api(`/api/order-counts/${day}/${source}`);if(qs('#categoryCountBody')!==body)return;
  body.innerHTML=`<p class="muted">На ${esc(day)}. Укажите количество заказов каждой категории — не число сотрудников.</p>
   <form id="categoryCountForm">${orderCountTypes[source].filter(key=>!key.startsWith('carryout:')).map(key=>`<label class="employee-row"><span>${orderCountLabel(key)}</span><span style="display:flex;align-items:center;gap:6px"><button class="secondary" type="button" data-step="-1" data-key="${key}" aria-label="Уменьшить ${orderCountLabel(key)}">−</button><input data-category="${key}" type="number" inputmode="numeric" min="0" max="100000" step="1" value="${data.counts[key]||''}" style="width:64px;text-align:center"><button class="secondary" type="button" data-step="1" data-key="${key}" aria-label="Добавить ${orderCountLabel(key)}">+</button></span></label>`).join('')}
   ${source==='private'?`<details style="border:1px solid var(--line);border-radius:14px;padding:12px;margin-top:10px"><summary style="min-height:44px;font-weight:750">Вынос <span id="carryoutSummary" class="muted" aria-live="polite"></span></summary><label>Состав бригады <select id="carryoutSize" style="min-height:44px;font-size:16px"><option value="2">2 человека</option><option value="3">3 человека</option><option value="4">4 человека</option></select></label><div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-top:12px"><span>Количество заказов</span><span style="display:flex;gap:6px;align-items:center"><button type="button" class="secondary" id="carryoutMinus" aria-label="Уменьшить число выносов">−</button><input id="carryoutQuantity" type="number" inputmode="numeric" min="0" max="100000" step="1" style="width:64px;text-align:center;font-size:16px" aria-label="Количество выносов"><button type="button" class="secondary" id="carryoutPlus" aria-label="Добавить вынос">+</button></span></div></details>`:''}
   <div class="category-save-bar" style="position:sticky;bottom:0;z-index:3;display:flex;align-items:center;justify-content:space-between;gap:12px;background:#111114;padding:12px 0 0;margin-top:8px;border-top:1px solid var(--line)"><h3 style="margin:0;min-width:0;font-size:18px">Всего: <span id="categoryCountTotal">${Object.values(data.counts).reduce((a,b)=>a+b,0)}</span></h3><button class="primary" type="submit" style="flex-shrink:0">Сохранить</button></div>
   </form>`;
  const form=qs('#categoryCountForm');
  const customRows=[];
  if(source==='private'){
   // Keep the common 4/6-person counters; move all uncommon sizes into editable rows.
   qsa('[data-category]',body).filter(el=>!['4','6'].includes(el.dataset.category.split(':')[1])).forEach(el=>el.closest('label').remove());
   const section=document.createElement('section');
   section.innerHTML='<h4 style="color:var(--gold);margin-bottom:6px">Другое количество сотрудников</h4><p class="muted" style="font-size:12px">Укажите число человек и количество заказов. Можно добавить несколько составов одной категории.</p><div id="customCountRows"></div>';
   form.insertBefore(section,form.querySelector('.category-save-bar'));
   const holder=section.querySelector('#customCountRows');
   const addRow=(cat,size='',count=0)=>{
    const row=document.createElement('div');
    row.style.cssText='display:grid;grid-template-columns:minmax(65px,1fr) 68px 76px 30px;gap:6px;align-items:end;margin:10px 0';
    row.innerHTML=`<span style="align-self:center">${{standard:'Стандарт',vip:'Вип',elite:'Элит'}[cat]}</span><label style="font-size:11px">Человек<input data-custom-size type="number" inputmode="numeric" min="1" max="100" step="1" placeholder="—" style="width:100%;font-size:16px" value="${size}"></label><label style="font-size:11px">Заказов<input data-custom-count type="number" inputmode="numeric" min="0" max="100000" step="1" style="width:100%;font-size:16px" value="${count}"></label><button type="button" class="secondary" style="padding:4px;min-width:0" aria-label="Добавить состав ${cat}">+</button>`;
    row.querySelector('button').onclick=()=>{addRow(cat);form.oninput()};
    holder.appendChild(row);customRows.push({cat,size:row.querySelector('[data-custom-size]'),count:row.querySelector('[data-custom-count]')});
   };
   ['standard','vip','elite'].forEach(cat=>{
    const saved=Object.entries(data.counts).filter(([key,count])=>key.startsWith(cat+':')&&!['4','6'].includes(key.split(':')[1])&&count>0).sort((a,b)=>Number(a[0].split(':')[1])-Number(b[0].split(':')[1]));
    if(saved.length)saved.forEach(([key,count])=>addRow(cat,Number(key.split(':')[1]),count));else addRow(cat);
   });
  }
  if(data.unallocated){
   const label=document.createElement('label');label.textContent='Без категории (сохранённые заказы)';
   label.innerHTML+=`<input id="categoryUnallocated" type="number" min="0" max="100000" step="1" value="${data.unallocated}">`;
   form.insertBefore(label,form.querySelector('.category-save-bar'));
  }
  const carryouts=source==='private'?Object.fromEntries([2,3,4].map(size=>['carryout:'+size,Number(data.counts['carryout:'+size]||0)])):{};
  const drawCarryouts=()=>{
   if(source!=='private')return;
   qs('#carryoutSummary').textContent=[2,3,4].filter(size=>carryouts['carryout:'+size]>0).map(size=>`${size} чел. × ${carryouts['carryout:'+size]}`).join(' · ')||'Нет заказов';
  };
  qsa('[data-category]',body).forEach(input=>{
   input.onfocus=()=>{if(input.value==='0')input.value=''};
   input.onblur=()=>{if(input.value!==''&&Number.isInteger(Number(input.value))&&Number(input.value)>=0)input.value=Number(input.value)===0?'':String(Number(input.value))};
  });
  const read=()=>{
   const counts={...Object.fromEntries(orderCountTypes[source].map(k=>[k,0])),...carryouts};
   qsa('[data-category]',body).forEach(el=>counts[el.dataset.category]=Number(el.value));
   customRows.forEach(row=>{const size=Number(row.size.value),count=Number(row.count.value);if(count>0&&Number.isInteger(size)&&size>=1&&size<=100){const key=row.cat+':'+size;counts[key]=(counts[key]||0)+count}});
   return {counts,unallocated:Number(qs('#categoryUnallocated')?.value||0),version:data.version};
  };
  form.oninput=()=>{const v=read();qs('#categoryCountTotal').textContent=Object.values(v.counts).reduce((a,b)=>a+b,0)+v.unallocated};
  if(source==='private'){
   const size=qs('#carryoutSize'),quantity=qs('#carryoutQuantity');
   const update=()=>{carryouts['carryout:'+size.value]=Number(quantity.value);drawCarryouts();form.oninput()};
   size.onchange=()=>{quantity.value=carryouts['carryout:'+size.value]||''};
   quantity.oninput=update;
   qs('#carryoutMinus').onclick=()=>{quantity.value=Math.max(0,(Number(quantity.value)||0)-1);update()};
   qs('#carryoutPlus').onclick=()=>{quantity.value=Math.min(100000,(Number(quantity.value)||0)+1);update()};
   size.onchange();drawCarryouts();
  }
  qsa('[data-step]',body).forEach(btn=>btn.onclick=()=>{
   const input=qsa('[data-category]',body).find(el=>el.dataset.category===btn.dataset.key);
   const value=Number(input.value)||0;
   input.value=String(Math.max(0,Math.min(100000,value+Number(btn.dataset.step))));
   form.oninput();
  });
  form.onsubmit=async event=>{
   event.preventDefault();const button=form.querySelector('button[type="submit"]');if(button.disabled)return;
   if(customRows.some(row=>{const size=Number(row.size.value),count=Number(row.count.value);return !Number.isInteger(count)||count<0||count>100000||(row.size.value!==''&&(!Number.isInteger(size)||size<1||size>100))||(count>0&&row.size.value==='')})){toast('Укажите от 1 до 100 человек и целое количество заказов');return}
   const values=read();if([...Object.values(values.counts),values.unallocated].some(v=>!Number.isInteger(v)||v<0||v>100000)){toast('Введите целые неотрицательные количества');return}
   button.disabled=true;
   try{await api(`/api/order-counts/${day}/${source}`,{method:'PUT',body:JSON.stringify(values)});
    if(qs('#categoryCountBody')===body)closeModal();
    await loadBootstrap(state.selectedDate);toast('Количество заказов сохранено');
   }catch(e){toast(e.message,6000);button.disabled=false}
  };
  form.oninput();
 }catch(e){if(qs('#categoryCountBody')===body)body.textContent=e.message}
}
async function openOrderCountPeriod(){
 let month=state.selectedDate.slice(0,7),half=Number(state.selectedDate.slice(8))<=15?'first':'second',request=0;
 showModal('Итоги заказов',`<div id="orderPeriodPanel"><label>Месяц <input id="orderPeriodMonth" type="month" value="${month}"></label><div style="display:flex;gap:8px;margin:14px 0"><button id="orderHalfFirst" class="secondary">1–15</button><button id="orderHalfSecond" class="secondary">16–конец</button></div><div id="orderPeriodBody"></div></div>`);
 const panel=qs('#orderPeriodPanel'),body=qs('#orderPeriodBody');
 async function draw(){
  const id=++request;if(qs('#orderPeriodPanel')!==panel)return;
  qs('#orderHalfFirst').className=half==='first'?'primary':'secondary';qs('#orderHalfSecond').className=half==='second'?'primary':'secondary';body.textContent='Загрузка…';
  try{const r=await api(`/api/order-count-period?month=${encodeURIComponent(month)}&half=${half}`);if(id!==request||qs('#orderPeriodPanel')!==panel)return;
   body.innerHTML=`<div class="attention"><h3>Всего заказов: ${r.total}</h3><p>${esc(r.start)} — ${esc(r.end)}</p></div>${['private','gbu'].map(src=>`<h3>${src==='gbu'?'ГБУ':'Частные'}: ${r.sources[src].total}</h3>${orderCountTypes[src].map(key=>`<div class="employee-row"><span>${orderCountLabel(key)}</span><b>${r.sources[src].counts[key]}</b></div>`).join('')}${r.sources[src].unallocated?`<p>Без категории: ${r.sources[src].unallocated}</p>`:''}`).join('')}<h3>По дням</h3><table style="width:100%"><thead><tr><th>День</th><th>Частные</th><th>ГБУ</th><th>Всего</th></tr></thead><tbody>${r.daily.map(d=>`<tr><td>${d.date.slice(8)}</td><td>${d.private}</td><td>${d.gbu}</td><td>${d.total}</td></tr>`).join('')}</tbody></table>`;
  }catch(e){if(id===request&&qs('#orderPeriodPanel')===panel)body.textContent=e.message}
 }
 qs('#orderPeriodMonth').onchange=e=>{if(e.target.value){month=e.target.value;draw()}};
 qs('#orderHalfFirst').onclick=()=>{half='first';draw()};qs('#orderHalfSecond').onclick=()=>{half='second';draw()};await draw();
}
</script>
</body>''',1)

_number_list_start = INLINE_INDEX_HTML.index('async function openNumberCards(){')
_number_list_end = INLINE_INDEX_HTML.index('\n}', _number_list_start) + 2
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_number_list_start] + r'''function numberCardDetailsHtml(x,index){
 const sent=new Date(x.sent_at).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'});
 const name=employeeListName(x.employee).replace(/([0-9—])\s*см$/i,'$1');
 const source=x.source==='gbu'?'gbu':'private';
 return `<b class="number-card-name">${index+1}. ${esc(name)}</b>
   <span class="number-card-category number-card-${source}">${source==='gbu'?'ГБУ ⬛️':'Частный'}</span>
   <small class="number-card-line number-card-sent">${esc(sent)}</small>
   <small class="number-card-line number-card-status">${x.closed?'✅ Заказ закрыт':'Ожидается закрытие'}</small>
   <strong class="number-card-surname">${esc(String(x.surname||'').toLocaleUpperCase('ru-RU'))}</strong>
   ${x.closed?`<small class="number-card-line number-card-contact">${x.contact_time?`Связь бригады в <strong class="number-card-time">${esc(x.contact_time)}</strong>`:'Время связи не указано'}</small>`:''}`;
}
async function openNumberCards(){
 const day=state.selectedDate;
 showModal('📱 Номера в группе','<div id="numberCardsBody" class="empty">Загрузка…</div>');
 const body=qs('#numberCardsBody');
 const active=()=>qs('#numberCardsBody')===body&&!qs('#modal').classList.contains('hidden');
 let timer=null,revision=0,mutating=false;
 async function draw(){
  clearTimeout(timer);timer=null;
  if(!active()||mutating)return;
  const request=++revision;
  try{
   const r=await api(`/api/number-cards?work_date=${day}`);if(!active()||request!==revision||mutating)return;
   const scroll=qs('#modal').scrollTop;
   body.className='';body.innerHTML=`<div class="attention"><h4>Номера: ${r.count}/${r.progress.expected}</h4><p>За ${esc(day)}. Кто выставил номер через бота.</p>${["private","gbu"].map(source=>{const p=r.progress.sources[source];return `<p><b class="number-card-${source}">${source==="gbu"?"ГБУ":"Частные"}</b> · Номера ${p.cards}/${p.expected} · Закрыто ${p.closed}/${p.expected}${p.complete?" ✅":""}${p.missing_numbers?` · Не хватает номеров: ${p.missing_numbers}`:""}${p.extra_numbers?` · ⚠️ Лишних номеров: ${p.extra_numbers}`:""}</p>`}).join("")}</div>${r.items.map((x,i)=>`<div class="employee-row number-card-row"><div class="name number-card-details">${numberCardDetailsHtml(x,i)}</div>${state.bootstrap?.is_owner?`<button type="button" class="secondary danger" data-number-delete="${x.id}" aria-label="Удалить запись" style="min-width:44px;min-height:44px">🗑</button>`:''}</div>`).join('')||'<div class="empty">Номера пока не отправляли</div>'}`;
   qs('#modal').scrollTop=scroll;
   if(state.selectedDate===day&&state.bootstrap?.summary){
    state.bootstrap.summary.number_cards_count=r.count;state.bootstrap.summary.order_completion=r.progress;renderSummary();
   }
   qsa('[data-number-delete]',body).forEach(button=>button.onclick=async()=>{
    const item=r.items.find(x=>Number(x.id)===Number(button.dataset.numberDelete));if(!item||button.disabled)return;
    if(!confirm(`Удалить запись «${employeeListName(item.employee)} · ${item.surname}» из списка и подсчёта за ${day}? Сообщение в Telegram останется.`))return;
    button.disabled=true;mutating=true;revision++;clearTimeout(timer);timer=null;
    const modal=qs('#modal'),scroll=modal.scrollTop;
    try{
     await api(`/api/number-cards/${item.id}`,{method:'DELETE'});
     mutating=false;
     await draw();
     if(qs('#numberCardsBody')===body)modal.scrollTop=scroll;
     const selected=state.selectedDate,bootstrap=await api(`/api/bootstrap?work_date=${selected}`);
     if(state.selectedDate===selected){state.bootstrap=bootstrap;renderSummary()}
     toast('Запись удалена');
    }catch(e){button.disabled=false;toast(e.message,5000)}
    finally{mutating=false;if(active()&&!timer)timer=setTimeout(draw,5000)}
   });
  }catch(e){if(active()&&request===revision)body.textContent=e.message}
  if(active()&&!mutating&&request===revision)timer=setTimeout(draw,5000);
 }
 await draw();
}''' + INLINE_INDEX_HTML[_number_list_end:]


# Match the dashboard counters to the same dated plan used for completion notices.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'state.bootstrap=await api(`/api/bootstrap?work_date=${state.selectedDate}`); const cashBtn=',
    'const refreshDay=state.selectedDate;const freshBootstrap=await api(`/api/bootstrap?work_date=${refreshDay}`);if(state.selectedDate!==refreshDay)return;state.bootstrap=freshBootstrap; const cashBtn=', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<div class="metric ${s.unassigned_count?\'warn\':\'good\'}"><b>${s.complete_count}/${s.orders_total}</b><span>Укомплект.</span></div>',
    '<div class="metric"><span>📸 Фотоотчёты</span><span style="color:var(--text)">Частные <strong>${s.photo_reports?.private||0}/${s.private_count}</strong></span><span style="color:var(--text)">ГБУ <strong>${s.photo_reports?.gbu||0}/${s.gbu_count}</strong></span></div>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '${r.totals.brigades_on_contact||0}</b>',
    '${r.totals.brigades_on_contact||0}/${r.totals.brigades_expected||0}</b>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<b>📱 ${s.number_cards_count||0}</b>',
    '<b>📱 ${s.number_cards_count||0}/${s.orders_total}</b>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<span>Частных</span></button>',
    '<span>Частных</span><span>Закрыто ${s.order_completion?.sources.private.closed||0}/${s.private_count}</span></button>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<span>ГБУ</span></button>',
    '<span>ГБУ</span><span>Закрыто ${s.order_completion?.sources.gbu.closed||0}/${s.gbu_count}</span></button>', 1)


def _organize_day_dashboard(html):
    html = html.replace('<section id="summary" class="summary-grid"></section>',
        '<div id="selectedDayHeader" class="section-head"></div><h3>Заказы</h3><section id="summary" class="summary-grid"></section>', 1)
    html = html.replace('<section id="readinessView" class="view active">',
        '<section id="readinessView" class="view active"><div id="employeeReadiness">', 1)
    html = html.replace('<div id="readinessGroups"></div>', '<div id="readinessGroups"></div></div>', 1)
    html = html.replace('<h2>Готовность</h2><p class="muted">Официальный срез — 15:30</p>',
        '<h2 id="readinessTitle">Готовность сотрудников</h2><p id="readinessDayLabel" class="muted"></p>', 1)
    html = re.sub(r'(<button[^>]*id="readinessCountBtn"[^>]*>.*?</button>)(<button[^>]*id="readinessRemindersBtn"[^>]*>.*?</button>)', r'\2\1', html, count=1)
    html = html.replace('grid-template-columns:repeat(3,minmax(0,1fr))',
                        'grid-template-columns:repeat(2,minmax(0,1fr))', 1)
    # A single date owns each request. Late replies cannot overwrite a newer selection.
    html = html.replace('async function loadBootstrap(date=null) {',
        "let dayRequest=0;\nasync function loadBootstrap(date=null) {\n  const request=++dayRequest;\n  if(date)state.selectedDate=date;\n  qs('#summary').innerHTML='<div class=\"muted\">Загрузка выбранного дня…</div>';\n  qs('#readinessView').style.visibility='hidden';", 1)
    html = html.replace('  state.bootstrap = data;',
        '  if(request!==dayRequest)return;\n  state.bootstrap = data;\n  qs(\'#readinessView\').style.visibility=\'visible\';', 1)
    html = html.replace('async function loadReadiness(){\n  const r=await api(`/api/readiness/summary?work_date=${state.selectedDate}`);',
        'async function loadReadiness(){\n  const selectedDay=state.selectedDate, day=nextReadinessDay(selectedDay);\n  const r=await api(`/api/readiness/summary?work_date=${day}`);\n  if(state.selectedDate!==selectedDay||r.work_date!==day)return;\n  qs(\'#readinessDayLabel\').textContent=fmtDate(day)+\' · Время сводки: \'+(r.cutoff_label||\'14:00\');', 1)
    html = html.replace("openReadinessGroupedList('ready');", "openReadinessGroupedList('ready',day);", 1)
    html = html.replace("openReadinessGroupedList('no_response');", "openReadinessGroupedList('no_response',day);", 1)
    html = html.replace('openReadinessPeople(g.label||code,label,people,code,status,state.selectedDate);',
                        'openReadinessPeople(g.label||code,label,people,code,status,day);', 1)
    html = html.replace('/api/readiness/history?work_date=${state.selectedDate}',
                        '/api/readiness/history?work_date=${nextReadinessDay()}', 1)
    html = re.sub(r'    <button id="brigadesContactCount"[^\n]+\n', '', html, count=1)
    html = html.replace("  qs('#brigadesContactCount').onclick=openContactBrigades;", '', 1)
    html = html.replace('count.textContent=String(ids.size);',
        "count.textContent=String(ids.size)+'/'+state.bootstrap.summary.orders_total;", 1)
    start=html.index('function renderSummary() {')
    end=html.index('async function openContactBrigades()', start)
    html=html[:start]+r'''function nextReadinessDay(day=state.selectedDate) {
  return new Date(Date.parse(day+'T12:00:00Z')+86400000).toISOString().slice(0,10);
}
function renderSummary() {
  const s=state.bootstrap.summary, day=state.selectedDate;
  const future=day>state.bootstrap.today;
  const tomorrow=new Date(Date.parse(state.bootstrap.today+'T12:00:00Z')+86400000).toISOString().slice(0,10);
  const closingTitle=day===tomorrow?'Закрытие заказов на завтра':day===state.bootstrap.today?'Закрытие заказов на сегодня':'Закрытие заказов';
  qs('#readinessTitle').textContent=day===state.bootstrap.today?'Готовность сотрудников на завтра':'Готовность сотрудников на следующий день';
  qs('#readinessDayLabel').textContent=fmtDate(nextReadinessDay(day))+' · Время сводки: '+(s.readiness_cutoff_label||'14:00');
  qs('#selectedDayHeader').innerHTML=`<h2>${esc(fmtDate(day))}</h2><label class="muted">Выбрать дату <input type="date" id="dashboardDate" value="${day}" style="max-width:160px"></label>`;
  qs('#dashboardDate').onchange=e=>{if(e.target.value)loadBootstrap(e.target.value).catch(e=>toast(e.message))};
  qs('#summary').innerHTML=`
    <div class="metric order-total-metric" style="display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center"><b>${s.orders_total}</b><span>Всего заказов</span><button type="button" class="day-comment-btn${state.bootstrap.day_comment?.text?' has-comment':''}" id="dayCommentBtn" aria-label="${state.bootstrap.day_comment?.text?'Открыть комментарий на день':'Добавить комментарий на день'}">Комментарий</button></div>
    <button class="metric" data-count-field="private_count"><b>${s.private_count} ✍️</b><span>Частные</span></button>
    <button class="metric" data-count-field="gbu_count"><b>${s.gbu_count} ✍️</b><span>ГБУ</span></button>
    <div class="metric" aria-label="Нужно сотрудников: ${s.requested_staff||0}. Без выносов: ${s.requested_staff_without_carryouts??s.requested_staff??0}. Частные: ${s.requested_staff_by_source?.private||0}. ГБУ: ${s.requested_staff_by_source?.gbu||0}"><b style="font-size:18px;overflow-wrap:anywhere">${s.requested_staff||0}${s.requested_staff_without_carryouts!=null && s.requested_staff_without_carryouts<s.requested_staff ? `(${s.requested_staff_without_carryouts})` : ""}</b><span style="white-space:normal;overflow:visible">Нужно сотрудников</span><b style="font-size:14px;margin-top:8px">${s.requested_staff_by_source?.private||0} / ${s.requested_staff_by_source?.gbu||0}</b><span style="white-space:normal;overflow:visible">Частные / ГБУ</span></div>`;
  qsa('[data-count-field]').forEach(btn=>btn.onclick=()=>editDispatchCount(btn.dataset.countField));
  qs('#dayCommentBtn').onclick=()=>openDayComment(day);
  const view=qs('#readinessView');
  for(const id of ['orderPreparation','brigadeWork']){
    if(!qs('#'+id)){const section=document.createElement('section');section.id=id;section.style.margin='24px 0';view.appendChild(section)}
  }
  const p=s.order_completion?.sources.private||{},g=s.order_completion?.sources.gbu||{};
  qs('#orderPreparation').innerHTML=`<h2>${closingTitle}</h2><p class="muted">${esc(fmtDate(day))} · Номера и закрытие относятся к дате заказа</p>
    <button class="secondary" id="numberCardsBtn" style="width:100%;margin-bottom:12px">📱 Номера в группе ${s.number_cards_count||0}/${s.orders_total} ›</button>
    <div class="summary-grid" style="grid-template-columns:repeat(2,minmax(0,1fr))">
    <div class="metric"><b>Частные</b><span>Номера ${p.cards||0}/${s.private_count}</span><span>Закрыто ${p.closed||0}/${s.private_count}</span></div>
    <div class="metric"><b>ГБУ</b><span>Номера ${g.cards||0}/${s.gbu_count}</span><span>Закрыто ${g.closed||0}/${s.gbu_count}</span></div></div>`;
  qs('#numberCardsBtn').onclick=openNumberCards;
  qs('#brigadeWork').innerHTML=`<h2>Контроль бригады утром</h2><p class="muted">${esc(fmtDate(day))}${future?' · В день заказа':''}</p>
    <div class="summary-grid" style="grid-template-columns:repeat(3,minmax(0,1fr))">
    <button id="brigadesContactCount" class="metric good"><b>${s.brigades_on_contact||0}/${s.orders_total}</b><span>Бригады на связи ›</span></button>
    <button class="metric" id="privatePhotoControl"><b>${s.photo_reports?.private||0}/${s.private_count}</b><span>Фото · частные ›</span></button>
    <button class="metric" id="gbuPhotoControl" aria-label="Фото ГБУ: первое фото ${s.photo_reports?.gbu_first??s.photo_reports?.gbu??0} из ${s.gbu_count}, второе фото ${s.photo_reports?.gbu_second||0} из ${s.gbu_count}"><span class="gbu-dashboard-title">ГБУ⬛️</span><span class="gbu-dashboard-photo-columns"><span class="gbu-dashboard-photo-stat"><b>${s.photo_reports?.gbu_first??s.photo_reports?.gbu??0}/${s.gbu_count}</b><span>1 фото</span></span><span class="gbu-dashboard-photo-stat"><b>${s.photo_reports?.gbu_second||0}/${s.gbu_count}</b><span>2 фото</span></span></span></button></div>`;
  qs('#brigadesContactCount').onclick=openContactBrigades;
  qs('#privatePhotoControl').onclick=()=>openPhotoControl('private',day);
  qs('#gbuPhotoControl').onclick=()=>openPhotoControl('gbu',day);
  const sections=future?['orderPreparation','brigadeWork','employeeReadiness']:['brigadeWork','orderPreparation','employeeReadiness'];
  for(const id of sections)view.appendChild(qs('#'+id));
}

''' + html[end:]
    return html


INLINE_INDEX_HTML = _organize_day_dashboard(INLINE_INDEX_HTML)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<style>
.metric.order-total-metric{padding-left:4px;padding-right:4px}
.day-comment-btn{position:relative;overflow:visible;margin-top:10px;max-width:100%;min-height:30px;padding:5px 3px;border:1px solid var(--gold2);border-radius:12px;background:var(--panel2);color:var(--gold);font-size:10px;line-height:1.2;white-space:nowrap;cursor:pointer}
.day-comment-btn.has-comment{background:#302719;border-color:var(--gold)}
.day-comment-btn.has-comment::after{content:"";position:absolute;right:-2px;top:-2px;width:6px;height:6px;border-radius:50%;background:var(--gold);box-shadow:0 0 0 2px var(--panel2);pointer-events:none}
#numberCardsBody .number-card-row{padding:16px 0;gap:10px}
#numberCardsBody .number-card-details{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:10px;row-gap:6px;align-items:start}
#numberCardsBody .number-card-name{grid-column:1;grid-row:1;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;line-height:1.4}
#numberCardsBody .number-card-category{grid-column:2;grid-row:1;justify-self:end;padding:3px 6px;border-radius:6px;background:#202026;font-size:10px;line-height:1.4;white-space:nowrap}
#numberCardsBody .number-card-private{color:var(--green);font-weight:800}
#numberCardsBody .number-card-gbu{color:#ffd45c;font-weight:800}
#numberCardsBody .number-card-line{display:block;margin:0;line-height:1.4;overflow-wrap:anywhere}
#numberCardsBody .number-card-sent{grid-column:1;grid-row:2}
#numberCardsBody .number-card-status{grid-column:1;grid-row:3;margin-top:7px;color:var(--text)}
#numberCardsBody .number-card-surname{grid-column:2;grid-row:3;justify-self:end;max-width:104px;margin-top:7px;color:#fff;font-size:12px;font-weight:800;line-height:1.4;text-align:right;overflow-wrap:anywhere}
#numberCardsBody .number-card-contact{grid-column:1 / -1;grid-row:4}
#numberCardsBody .number-card-time{color:#ff5c5c;font-size:12px;font-weight:800;white-space:nowrap}
@media(max-width:380px){#numberCardsBody .number-card-details{column-gap:6px}#numberCardsBody .number-card-surname{max-width:88px;font-size:11px}}
.day-comment-text{display:block;width:100%;min-height:120px;margin-top:8px;padding:12px;resize:vertical;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--text);font-size:16px}
</style>
<script>
async function openDayComment(day){
 showModal('Комментарий',`<p class="muted">${esc(fmtDate(day))} · Общая заметка для вас и помощников</p><form id="dayCommentForm" style="margin-top:16px"><label for="dayCommentText">Комментарий на день</label><textarea id="dayCommentText" class="day-comment-text" maxlength="500" rows="4" placeholder="Напишите короткую заметку…" disabled></textarea><div style="display:flex;justify-content:space-between;gap:12px;margin-top:6px"><small class="muted" id="dayCommentAuthor"></small><small class="muted" id="dayCommentCounter">0/500</small></div><p id="dayCommentError" role="status" aria-live="polite" style="color:var(--amber)"></p><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px"><button type="submit" class="primary" id="dayCommentSave" disabled>Сохранить</button><button type="button" class="secondary" id="dayCommentCancel">Отмена</button><button type="button" class="secondary" id="dayCommentReload">Обновить</button></div></form>`);
 const form=qs('#dayCommentForm'),input=qs('#dayCommentText'),save=qs('#dayCommentSave'),reload=qs('#dayCommentReload'),error=qs('#dayCommentError');
 const active=()=>qs('#dayCommentForm')===form&&!qs('#modal').classList.contains('hidden');
 let version=0,savedText='',ready=false,busy=false;
 function setBusy(value){busy=value;input.disabled=value||!ready;save.disabled=value||!ready;reload.disabled=value}
 function count(){qs('#dayCommentCounter').textContent=input.value.length+'/500'}
 function apply(data){
  version=data.version;savedText=data.text;input.value=savedText;ready=true;count();
  const updated=data.updated_at?new Date(data.updated_at).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'';
  qs('#dayCommentAuthor').textContent=data.updated_by?`${data.updated_by}${updated?' · '+updated:''}`:'';
 }
 async function load(){
  if(busy)return;
  if(ready&&input.value!==savedText&&!confirm('Загрузить сохранённый комментарий? Ваш несохранённый текст будет заменён.'))return;
  setBusy(true);error.textContent='';
  try{const data=await api(`/api/day-comments/${day}`);if(active())apply(data)}
  catch(e){if(active())error.textContent=e.message}
  finally{if(active())setBusy(false)}
 }
 input.oninput=count;
 reload.onclick=load;
 qs('#dayCommentCancel').onclick=closeModal;
 form.onsubmit=async event=>{
  event.preventDefault();if(!ready||busy)return;
  setBusy(true);error.textContent='';
  try{
   const data=await api(`/api/day-comments/${day}`,{method:'PUT',body:JSON.stringify({text:input.value,version})});
   if(state.selectedDate===day&&state.bootstrap){state.bootstrap.day_comment=data;renderSummary()}
   if(active()){closeModal();toast(data.text?'Комментарий сохранён':'Комментарий очищен')}
  }catch(e){if(active())error.textContent=e.message}
  finally{if(active())setBusy(false)}
 };
 await load();
}
</script></body>''', 1)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>',r'''
<style>
html,body,button,input,select,textarea,a,label { touch-action:manipulation; }
.photo-mark { display:flex; flex-direction:column; align-items:center; gap:3px; }
.photo-mark-toggle { min-width:44px; min-height:44px; padding:5px; font-size:23px; }
.photo-mark-label { font-size:11px; color:var(--muted); }
.photo-mark-auto { border:0; background:none; color:var(--blue); padding:7px 4px; font-size:11px; }
</style>
<script>
function openPhotoControl(source,day){
 showModal(source==='gbu'?'Фото · ГБУ':'Фото · частные',`<p class="muted">${esc(fmtDate(day))} · По времени фото</p><p class="muted" id="photoControlHint">${source==='gbu'?'Отдельная отметка за первое и второе фото':'Отметка после отправки фото'}</p><div id="photoControlList">Загрузка…</div>`);
 const body=qs('#photoControlList');
 const active=()=>qs('#photoControlList')===body&&!qs('#modal').classList.contains('hidden');
 let items=[],canEdit=false,saving=false,revision=0,timer=null;
 function mark(x,stage){
  const received=stage===1?x.first_received:x.second_received,manual=stage===1?x.first_manual:x.second_manual;
  const label=`${stage===1?'Первое':'Второе'} фото: ${received?'получено':'ожидается'}`;
  return `<div class="photo-mark">${source==='gbu'?`<small class="photo-mark-label">${stage}-е фото</small>`:''}${canEdit?`<button type="button" class="secondary photo-mark-toggle" data-photo-id="${x.id}" data-photo-stage="${stage}" aria-pressed="${received}" aria-label="${label}. ${received?'Снять':'Поставить'} отметку">${received?'✅':'⬜'}</button>`:`<span style="font-size:23px" aria-label="${label}">${received?'✅':'⬜'}</span>`}${manual?`<small class="photo-mark-label">Вручную</small>${canEdit?`<button type="button" class="photo-mark-auto" data-photo-id="${x.id}" data-photo-stage="${stage}" data-photo-auto="1" aria-label="Вернуть автоматическую отметку ${stage}-го фото">Авто</button>`:''}`:''}</div>`;
 }
 body.onclick=async event=>{
  const button=event.target.closest('button[data-photo-id]');
  if(!button||!body.contains(button)||saving||!canEdit)return;
  const item=items.find(x=>x.id===Number(button.dataset.photoId));if(!item)return;
  const stage=Number(button.dataset.photoStage);
  const received=button.dataset.photoAuto?null:!(stage===1?item.first_received:item.second_received);
  saving=true;revision++;clearTimeout(timer);
  qsa('button',body).forEach(b=>b.disabled=true);
  try{
   const r=await api(`/api/photo-control/${item.id}`,{method:'PATCH',body:JSON.stringify({stage,received})});
   if(state.selectedDate===day&&state.bootstrap?.summary){state.bootstrap.summary.photo_reports=r.counts;renderSummary()}
   if(active())toast(received===null?'Автоматическая отметка восстановлена':received?'Фото отмечено':'Отметка снята');
  }catch(e){if(active())toast(e.message,5000)}
  finally{saving=false;if(active())await refresh()}
 };
 async function refresh(){
  clearTimeout(timer);
  if(!active()||saving)return;
  const request=++revision;
  try{
   const r=await api(`/api/photo-control?work_date=${encodeURIComponent(day)}&source=${source}`);
   if(!active()||request!==revision||saving)return;
   items=r.items;canEdit=!!r.can_edit;
   qs('#photoControlHint').textContent=canEdit?'Нажмите на квадратик, чтобы поставить или снять отметку. «Авто» возвращает отметку бота.':source==='gbu'?'Отдельная отметка за первое и второе фото':'Отметка после отправки фото';
   body.innerHTML=items.length?`<div class="list photo-control-grid brigade-control-grid${source==='gbu'?' is-gbu':''}">${items.map((x,i)=>`<div class="employee-row">${brigadeControlCardHeading(x,i,source)}<div style="display:flex;gap:4px;align-items:center;flex-shrink:0"><b>${esc(x.photo_time||'—')}</b>${mark(x,1)}${source==='gbu'?mark(x,2):''}</div></div>`).join('')}</div>`:'Номера по заказам на эту дату ещё не выставлены.';
  }catch(e){if(active()&&request===revision&&!saving)body.textContent=e.message}
  if(active()&&!saving&&request===revision)timer=setTimeout(refresh,5000);
 }
 refresh();
}
</script></body>''',1)


def _contact_bulk_delete_ui(html):
    start = html.index('async function openContactBrigades(')
    end = html.index('function currentViewId()', start)
    return html[:start] + r'''
async function openContactBrigades(workDay){
  const day=typeof workDay==='string'?workDay:state.selectedDate;
  showModal('Связь бригад','<div id="contactOrdersBody" class="empty">Загрузка…</div>');
  const body=qs('#contactOrdersBody');
  const active=()=>qs('#contactOrdersBody')===body&&!qs('#modal').classList.contains('hidden');
  let deleteButton=null;
  async function refresh(){
   if(!active())return;
   try{
    const [r,orders]=await Promise.all([api(`/api/readiness/summary?work_date=${day}`),api(`/api/brigade-contact-orders?work_date=${day}`)]);
    if(!active())return;
    const confirmed=orders.items.filter(x=>x.on_contact);
    body.className='';
    body.innerHTML=`<div class="attention"><div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><h4>Связь по заказам: ${r.totals.brigades_on_contact}/${r.totals.brigades_expected}</h4><button class="secondary small" id="addBrigadeContact">＋ Добавить</button></div><p>За ${esc(day)}. Заказы по времени связи.</p></div><div class="list contact-orders-grid brigade-control-grid">${orders.items.map((x,i)=>`<div class="employee-row">${brigadeControlCardHeading(x,i)}<div style="display:flex;gap:8px;align-items:center;flex-shrink:0"><b>${esc(x.contact_time||'—')}</b><span aria-label="${x.on_contact?'На связи':'Ожидается связь'}">${x.on_contact?'✅':'⬜'}</span></div></div>`).join('')||'<div class="empty">Заказы пока не добавлены</div>'}</div>`;
    qs('#addBrigadeContact').onclick=()=>openAddBrigadeContact(day,orders.items);
    if(state.bootstrap?.is_owner){
      const button=deleteButton||document.createElement('button');
      button.className='secondary danger small';button.textContent='Удалить';button.disabled=!confirmed.length;
      button.style.cssText='margin-left:auto;flex-shrink:0';
      if(!deleteButton){qs('#closeModal').before(button);deleteButton=button}
      button.onclick=()=>openDeleteBrigadeContacts(day,confirmed);
    }
   }catch(e){if(active())body.textContent=e.message}
   if(active())setTimeout(refresh,5000);
  }
  await refresh();
}
function contactOrderLabel(x){
  return `${brigadeControlEmployeeName(x.employee)} · ${x.source==='gbu'?'ГБУ':'Частный'} · ${x.surname||'—'} · ${x.contact_time||'—'}`;
}
async function openAddBrigadeContact(day,items){
  const pending=items.filter(x=>!x.on_contact);
  showModal('Добавить связь по заказу',`<p class="muted">За ${esc(day)}. Каждый заказ отмечается отдельно.</p><div class="list">${pending.map(x=>`<div class="employee-row"><div class="name"><b>${esc(brigadeControlEmployeeName(x.employee))}</b><small>${esc(x.source==='gbu'?'ГБУ':'Частный')} · ${esc(x.surname||'—')} · ${esc(x.contact_time||'—')}</small></div><button class="secondary" data-add-contact="${x.id}" aria-label="Отметить связь по заказу">＋</button></div>`).join('')||'<div class="empty">Все заказы на связи</div>'}</div><button class="secondary" id="contactBack" style="margin-top:12px">Назад</button>`);
  qs('#contactBack').onclick=()=>openContactBrigades(day);
  qsa('[data-add-contact]',qs('#modal')).forEach(button=>button.onclick=async()=>{
    if(button.disabled)return;
    button.disabled=true;
    try{
      await api(`/api/readiness/order-contact/${button.dataset.addContact}?work_date=${day}`,{method:'POST'});
      items.find(x=>x.id===Number(button.dataset.addContact)).on_contact=true;
      button.textContent='✅';button.setAttribute('aria-label','Связь по заказу подтверждена');
      if(state.selectedDate===day){const counter=qs('#brigadesContactCount b');if(counter)counter.textContent=items.filter(x=>x.on_contact).length+'/'+String(state.bootstrap.summary.orders_total||0)}
      toast('Связь по выбранному заказу отмечена');
    }catch(e){button.disabled=false;toast(e.message,4000)}
  });
}
function openDeleteBrigadeContacts(day,people){
  const selected=new Set();
  showModal('Удалить отметки связи',`<p class="muted">За ${esc(day)}. Выберите заказы.</p><label style="display:flex;gap:12px;padding:12px"><input type="checkbox" id="contactSelectAll"> Выбрать все</label><div class="list">${people.map((p,i)=>`<label class="employee-row" style="cursor:pointer;justify-content:flex-start;gap:12px"><input type="checkbox" data-contact-select="${p.id}" style="width:22px;height:22px;flex-shrink:0"><b>${i+1}. ${esc(contactOrderLabel(p))}</b></label>`).join('')}</div><div style="position:sticky;bottom:0;background:var(--card,#17171c);padding:12px 0;display:flex;gap:8px"><button class="secondary" id="cancelContactDelete">Отмена</button><button class="primary" id="submitContactDelete" disabled>Удалить выбранные (0)</button></div>`);
  const submit=qs('#submitContactDelete'),all=qs('#contactSelectAll');
  const checks=qsa('[data-contact-select]',qs('#modal'));
  const update=()=>{submit.disabled=!selected.size;submit.textContent=`Удалить выбранные (${selected.size})`;all.checked=selected.size===people.length;all.indeterminate=selected.size>0&&selected.size<people.length};
  checks.forEach(c=>c.onchange=()=>{const id=Number(c.dataset.contactSelect);if(c.checked)selected.add(id);else selected.delete(id);update()});
  all.onchange=()=>{checks.forEach(c=>{c.checked=all.checked;const id=Number(c.dataset.contactSelect);if(c.checked)selected.add(id);else selected.delete(id)});update()};
  qs('#cancelContactDelete').onclick=()=>openContactBrigades(day);
  submit.onclick=async()=>{
    if(submit.disabled)return;
    submit.disabled=true;all.disabled=true;checks.forEach(c=>c.disabled=true);
    try{
      const result=await api(`/api/readiness/brigade-contacts/delete?work_date=${day}`,{method:'POST',body:JSON.stringify({event_ids:[...selected]})});
      if(state.selectedDate===day){const counter=qs('#brigadesContactCount b');if(counter)counter.textContent=String(result.remaining)+'/'+String(state.bootstrap.summary.orders_total||0)}
      toast(`Удалено отметок: ${result.deleted}`);
      await openContactBrigades(day);
    }catch(e){toast(e.message,4000);all.disabled=false;checks.forEach(c=>c.disabled=false);update()}
  };
}
''' + html[end:]


INLINE_INDEX_HTML = _contact_bulk_delete_ui(INLINE_INDEX_HTML)

# Hide the daily/period grand-total column only in the kickbacks table.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "  grid.scrollLeft=left;grid.scrollTop=top;\n  qsa('[data-marina-day]',grid)",
    "  if(kind==='kickbacks') grid.querySelectorAll('tr').forEach(row=>row.lastElementChild?.remove());\n"
    "  grid.scrollLeft=left;grid.scrollTop=top;\n  qsa('[data-marina-day]',grid)",
    1,
)

# Only contact-list records carry contact_order_count; other employee lists stay unchanged.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<div class="car">${x.has_car?\'🚗\':\'\'}</div>',
    '<div class="car" style="display:flex;align-items:center;gap:10px;flex-shrink:0">'
    '${x.has_car?\'🚗\':\'\'}'
    '${x.contact_order_count!==undefined?`<span aria-label="Связь по заказам: ${Number(x.contact_order_count)||0}" '
    'title="Количество заказов на связи" style="display:inline-flex;align-items:center;justify-content:center;'
    'min-width:32px;height:32px;padding:0 8px;border-radius:10px;background:#20382b;color:#55c98b;'
    'font-weight:700;font-size:18px">${Number(x.contact_order_count)||0}</span>`:\'\'}</div>',
    1,
)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'id="readinessHistoryBtn">🧾 История</button>',
    'id="readinessHistoryBtn" title="Выбрать сотрудников без заказа"><b>Выходной</b><br><small style="font-size:11px;font-weight:400">(Кто не получил заказ)</small></button>',
).replace(
    "qs('#readinessHistoryBtn').onclick=openReadinessHistory;",
    """qs('#readinessHistoryBtn').onclick=async event=>{
      const button=event.currentTarget;
      if(button.disabled)return;
      button.disabled=true;
      try{
        const r=await api('/api/readiness/chat/roshcha',{method:'POST'});
        toast(r.failed_chats ? `Отправлено в ${r.sent_chats} групп. Ошибок: ${r.failed_chats}` : `Сообщение отправлено в ${r.sent_chats} группы`,5000);
      }catch(e){toast(e.message,5000)}
      finally{setTimeout(()=>{button.disabled=false},30000)}
    };""",
)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<button class="primary small" id="readinessCountBtn">',
    '<button class="secondary danger small" id="readinessAvralBtn" title="Напомнить о готовности на завтра во всех четырёх группах">❗️АВРАЛ❗️</button><button class="primary small" id="readinessCountBtn">',
    1,
).replace(
    "qs('#readinessHistoryBtn').onclick=async event=>{",
    """qs('#readinessAvralBtn').onclick=async event=>{
      const button=event.currentTarget;
      if(button.disabled)return;
      button.disabled=true;
      try{
        const r=await api('/api/readiness/chat/avral',{method:'POST'});
        toast(r.failed_chats ? `Отправлено в ${r.sent_chats} групп. Ошибок: ${r.failed_chats}` : `АВРАЛ: сообщение отправлено в ${r.sent_chats} группы`,5000);
      }catch(e){toast(e.message,5000)}
      finally{setTimeout(()=>{button.disabled=false},30000)}
    };
    qs('#readinessHistoryBtn').onclick=async event=>{""",
    1,
)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'function renderDates() {',
    """function shortCalendarDay(value) {
      const parts=value.split('-').map(Number);
      const day=new Date(Date.UTC(parts[0],parts[1]-1,parts[2])).getUTCDay();
      return ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'][day]+', '+value.slice(8,10)+'.'+value.slice(5,7);
    }
    function renderDates() {""",
).replace(
    '<strong>${esc(d.label)}</strong><small>${d.count} заказов</small>',
    '<strong style="display:block;white-space:nowrap">${esc(shortCalendarDay(d.date))}</strong>${["Сегодня","Завтра"].includes(d.label)?`<small style="display:block;line-height:1.35;margin-top:4px">${esc(d.label)}</small>`:""}<small style="display:block;white-space:nowrap;line-height:1.35;margin-top:4px">${d.count} заказов</small>',
)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '''${state.bootstrap?.is_owner?`<button type="button" class="secondary danger" data-number-delete="${x.id}" aria-label="Удалить запись" style="min-width:44px;min-height:44px">🗑</button>`:''}''',
    '''<div style="display:flex;flex-direction:column;align-items:center;gap:10px;flex-shrink:0"><button type="button" class="secondary" data-number-reassign="${x.id}" aria-label="Заменить бригадира" title="Заменить бригадира" style="width:52px;height:52px;padding:6px;font-size:28px">🔄</button>${state.bootstrap?.is_owner?`<button type="button" class="secondary danger" data-number-delete="${x.id}" aria-label="Удалить запись" style="width:36px;height:36px;padding:4px;font-size:18px;border-radius:10px">🗑</button>`:''}</div>''',1,
).replace(
    "   qsa('[data-number-delete]',body).forEach",
    "   qsa('[data-number-reassign]',body).forEach(b=>b.onclick=()=>openNumberReassignment(r.items,Number(b.dataset.numberReassign),day));\n   qsa('[data-number-delete]',body).forEach",1,
).replace('</script>\n</body>',r'''
async function openNumberReassignment(items,id,day){
 const selected=items.find(x=>x.id===id);if(!selected)return;
 try{
  const r=await api('/api/number-reassign/brigadiers');
  const own=items.filter(x=>x.tg_id===selected.tg_id);
  const candidates=r.items.filter(x=>x.tg_id!==selected.tg_id);
  showModal('🔄 Заменить бригадира',`<p>${esc(employeeListName(selected.employee))} · ${esc(day)}</p><p>Выберите заказы для передачи:</p>${own.map(x=>`<label style="display:flex;gap:12px;padding:12px"><input type="checkbox" data-transfer-id="${x.id}" ${x.id===id?'checked':''}>${x.source==='gbu'?'ГБУ':'Частный'} · ${esc(x.surname)}</label>`).join('')}<label>Новый бригадир<select id="transferTarget" style="width:100%;margin:12px 0"><option value="">Выберите бригадира</option>${candidates.map(x=>`<option value="${x.tg_id}">${esc(employeeListName(x))}</option>`).join('')}</select></label><label style="display:flex;gap:12px;padding:12px 0"><input type="checkbox" id="transferContact">Бригада та же — сохранить подтверждённую связь</label><p class="muted">Фото и отчёты сохранятся. Касса не переносится. Отметка связи нового бригадира действует на все его заказы за этот день.</p><button class="primary" id="transferSubmit" style="margin-top:16px">Передать выбранные заказы</button><p id="transferStatus"></p>`);
  const submit=qs('#transferSubmit'),status=qs('#transferStatus');
  submit.onclick=async()=>{
   const ids=qsa('[data-transfer-id]:checked').map(x=>Number(x.dataset.transferId));
   const target=Number(qs('#transferTarget').value);
   if(!ids.length||!target){toast('Выберите заказы и нового бригадира');return}
   submit.disabled=true;
   try{
    const job=await api('/api/number-reassign',{method:'POST',body:JSON.stringify({event_ids:ids,old_tg_id:selected.tg_id,new_tg_id:target,keep_contact:qs('#transferContact').checked})});
    status.textContent='Замена сохранена. Бот обрабатывает её…';
    for(let i=0;i<30;i++){
     await new Promise(resolve=>setTimeout(resolve,2000));
     if(qs('#transferStatus')!==status)return;
     const result=await api('/api/number-reassign/'+job.token);
     if(result.status==='failed')throw new Error(result.error);
     if(result.status==='done'){
      toast('Бригадир заменён');
      state.bootstrap=await api(`/api/bootstrap?work_date=${state.selectedDate}`);renderSummary();
      await openNumberCards();return;
     }
    }
    status.textContent='Замена в очереди. Повторно нажимать не нужно. Обновите список позже.';
   }catch(e){status.textContent=e.message;submit.disabled=false}
  };
 }catch(e){toast(e.message,5000)}
}
</script>
</body>''',1)

# Reuse the cash-day checklist from the owner's personal earnings table.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<h4>Касса выставлена ${r.submitted}/${r.expected}</h4>',
    '<h4>Итого касса: <span style="color:var(--green,#53c68c)">${new Intl.NumberFormat(\'ru-RU\').format(r.total_rub||0)} ₽</span></h4><h4>Касса выставлена ${r.submitted}/${r.expected}</h4>', 1)
_personal_start = INLINE_INDEX_HTML.index('async function openPersonalTables(){')
_personal_end = INLINE_INDEX_HTML.index('</script>', _personal_start)
_personal_html = INLINE_INDEX_HTML[_personal_start:_personal_end]
_personal_html = _personal_html.replace('async function openPersonalTables(){', 'async function openPersonalTables(resume=null){', 1)
_personal_html = _personal_html.replace("let month=new Date()", "let month=resume?.month||new Date()", 1)
_personal_html = _personal_html.replace("kind='earnings',data=null", "kind=resume?.kind||'earnings',data=null", 1)
_personal_html = _personal_html.replace("const left=grid.scrollLeft,top=grid.scrollTop;", "const left=resume?.left??grid.scrollLeft,top=resume?.top??grid.scrollTop;const modalTop=resume?.modalTop??qs('#modal').scrollTop;resume=null;", 1)
_personal_html = _personal_html.replace("'disabled title=\"Из общей кассы\"'", "(kind==='earnings'&&col===0?'title=\"Открыть кассу за день\"':'disabled title=\"Из общей кассы\"')", 1)
_personal_html = _personal_html.replace("const value=prompt(`${data.columns[column]} ·", "if(kind==='earnings'&&column===0){openCashDayCheck(`${month}-${String(day).padStart(2,'0')}`);return}\n   const value=prompt(`${data.columns[column]} ·", 1)
_personal_html = _personal_html.replace('grid.scrollLeft=left;grid.scrollTop=top;', "grid.scrollLeft=left;grid.scrollTop=top;qs('#modal').scrollTop=modalTop;", 1)
_personal_html = _personal_html.replace(' await load();', " qsa('[data-personal-kind]').forEach(btn=>btn.className=btn.dataset.personalKind===kind?'primary':'secondary');\n await load();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_personal_start]+_personal_html+INLINE_INDEX_HTML[_personal_end:]

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "if(kind==='earnings'&&column===0){openCashDayCheck",
    "if(kind==='earnings'&&column===2){openPersonalCashDay(`${month}-${String(day).padStart(2,'0')}`);return}\n   if(kind==='earnings'&&column===0){openCashDayCheck", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<style>
#privateCashBody .private-cash-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(70px,100px);gap:8px;align-items:center}
#privateCashBody .private-cash-row.has-cash-mark{grid-template-columns:minmax(0,1fr) 44px minmax(70px,100px)}
#privateCashBody .private-cash-row .name{min-width:0;overflow-wrap:anywhere}
#privateCashBody .private-cash-mark{width:44px;height:44px;min-height:44px;min-width:44px;padding:4px;font-size:24px;display:flex;align-items:center;justify-content:center}
#privateCashBody .private-cash-row input{width:100%;min-width:0;font-size:16px;padding:10px;border-radius:10px;background:var(--panel2);color:var(--text);border:1px solid var(--line)}
</style>
<script>
async function openPersonalCashDay(day){
 const grid=qs('#personalGrid'),modal=qs('#modal');
 const resume={month:qs('#personalMonth').value,kind:'earnings',left:grid.scrollLeft,top:grid.scrollTop,modalTop:modal.scrollTop};
 showModal('Наличка · '+fmtDate(day),'<button class="secondary" id="privateCashBack">← К моим таблицам</button><div id="privateCashBody">Загрузка…</div>');
 const body=qs('#privateCashBody'),active=()=>qs('#privateCashBody')===body&&!qs('#modal').classList.contains('hidden');
 let busy=false,dirty=false;
 qs('#privateCashBack').onclick=()=>{if(!busy&&(!dirty||confirm('Выйти без сохранения изменений?')))openPersonalTables(resume)};
 try{
  const data=await api('/api/personal-cash-day/'+day);if(!active())return;
  const money=c=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(c/100);
  const field=(key,title,value,subtitle='')=>{const mark=key!=='own'&&key!=='legacy',checked=data.checked?.[key]===true;return `<div class="employee-row private-cash-row${mark?' has-cash-mark':''}"><div class="name"><b>${esc(title)}</b><small>${esc(subtitle)}</small></div>${mark?`<button type="button" class="secondary private-cash-mark" data-private-cash-mark="${esc(key)}" aria-pressed="${checked}" aria-label="${esc(title)} — ручная отметка налички">${checked?'✅':'⬜️'}</button>`:''}<input data-private-cash="${esc(key)}" inputmode="decimal" aria-label="${esc(title)} — сумма в рублях" value="${value===undefined?'':esc(String(value/100))}"></div>`};
  body.innerHTML=`<div class="attention" style="margin-top:12px"><h4 style="text-align:right">Итого: <span id="privateCashTotal" style="color:var(--green,#53c68c)">${money(data.total)} ₽</span></h4><p>Частные заказы бригадиров без кассы. Укажите комиссию за каждый заказ.</p></div><form id="privateCashForm">${data.items.map((x,i)=>field(x.key,`${i+1}. ${x.name}`,data.amounts[x.key],x.surname||'Частный заказ')).join('')||'<p class="muted">Таких заказов за этот день нет.</p>'}${field('own','Мой выход',data.own,'Мой заработок за день')}${data.legacy?field('legacy','Ранее внесённая сумма',data.legacy,'Прежняя наличка сохранена. Уменьшите её, если распределяете эту сумму по заказам.'):''}<p id="privateCashStatus" class="muted"></p><button class="primary" type="submit">Сохранить</button></form>`;
  function values(){
   const out={amounts:{},checked:{},own:0,legacy:0,version:data.version};
   qsa('[data-private-cash-mark]',body).forEach(button=>{out.checked[button.dataset.privateCashMark]=button.getAttribute('aria-pressed')==='true'});
   qsa('[data-private-cash]',body).forEach(input=>{
    const raw=input.value.trim().replace(/\s/g,'').replace(',','.');
    if(raw&&!/^-?\d+(\.\d{1,2})?$/.test(raw))throw Error('Введите сумму с точностью до копеек');
    const cents=Math.round(Number(raw||0)*100),key=input.dataset.privateCash;
    if(!Number.isSafeInteger(cents)||Math.abs(cents)>100000000000||(key!=='legacy'&&cents<0))throw Error('Проверьте сумму');
    if(key==='own'||key==='legacy')out[key]=cents;else out.amounts[key]=cents;
   });return out;
  }
  qsa('[data-private-cash]',body).forEach(input=>input.oninput=()=>{dirty=true;try{const v=values();qs('#privateCashTotal').textContent=money(Object.values(v.amounts).reduce((a,b)=>a+b,0)+v.own+v.legacy)+' ₽';qs('#privateCashStatus').textContent='Не сохранено'}catch(e){qs('#privateCashStatus').textContent=e.message}});
  qsa('[data-private-cash-mark]',body).forEach(button=>button.onclick=()=>{if(busy)return;const checked=button.getAttribute('aria-pressed')!=='true';button.setAttribute('aria-pressed',String(checked));button.textContent=checked?'✅':'⬜️';dirty=true;qs('#privateCashStatus').textContent='Не сохранено'});
  qs('#privateCashForm').onsubmit=async e=>{
   e.preventDefault();if(busy)return;
   try{const payload=values();busy=true;qsa('input,button',body).forEach(x=>x.disabled=true);
    await api('/api/personal-cash-day/'+day,{method:'PUT',body:JSON.stringify(payload)});
    if(active()){dirty=false;toast('Наличка сохранена');await openPersonalTables(resume)}
   }catch(err){if(active())qs('#privateCashStatus').textContent=err.message}
   finally{busy=false;if(active())qsa('input,button',body).forEach(x=>x.disabled=false)}
  };
 }catch(e){if(active())body.textContent=e.message}
}
</script></body>''',1)

# Earnings is the money-button landing screen; the detailed cash matrix lives
# under My Tables. Keep day-check routes and the existing calculation models.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'id="cashBtn" aria-label="Касса"', 'id="cashBtn" aria-label="Заработок"', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "qs('#cashBtn').onclick=openCashTable;",
    "qs('#cashBtn').onclick=()=>openPersonalTables();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "showModal('📒 Мои таблицы',`<p",
    "showModal('💰 Заработок',`<button class=\"secondary\" id=\"earningsTables\" style=\"margin-bottom:12px\">📒 Мои таблицы</button><p", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "qsa('[data-personal-kind],#personalMonth,#personalAdd')",
    "qsa('[data-personal-kind],#personalMonth,#personalAdd,#earningsTables')", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    " qs('#personalAdd').onclick=",
    """ qs('#earningsTables').onclick=()=>{
  if(busy)return;
  earningsReturnState={month,kind,left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop};
  openCashTable();
 };
 qs('#personalAdd').onclick=""", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    'let cashTableReturnState=null;', 'let cashTableReturnState=null,earningsReturnState=null;', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '''showModal('💰 Касса', `<button class="secondary" style="margin-bottom:12px" onclick="openPersonalTables()">📒 Мои таблицы</button>''',
    '''showModal('📒 Мои таблицы', `<button class="secondary" style="margin-bottom:12px" onclick="rememberCashTablePosition();openPersonalTables(earningsReturnState)">← Назад к заработку</button><h3>💰 Касса · расширенная</h3>''', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('← К моим таблицам', '← К заработку')

# Open the daily automatic/manual breakdown instead of the +/- controls.
_kickback_start = INLINE_INDEX_HTML.index("kind==='kickbacks'&&col===0?`<td><div")
_kickback_end = INLINE_INDEX_HTML.index(':`<td><button class="contact-btn"', _kickback_start)
INLINE_INDEX_HTML = INLINE_INDEX_HTML[:_kickback_start] + """kind==='kickbacks'&&col===0?`<td><button class="contact-btn" style="min-width:80px" data-kickback-day="${day}" aria-label="Откат за ${day} число">${money(v)}</button></td>`""" + INLINE_INDEX_HTML[_kickback_end:]
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "  qsa('[data-marina-day]',grid).forEach", """  qsa('[data-kickback-day]',grid).forEach(btn=>btn.onclick=()=>{
   if(busy)return;
   openKickbackDay(month,Number(btn.dataset.kickbackDay),{month,kind,left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop});
  });
  qsa('[data-marina-day]',grid).forEach""", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('«Марине»: − и + меняют сумму на 250 ₽.', '«Марине»: нажмите сумму за день, чтобы увидеть откат из кассы и добавить ручную сумму.')
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<script>
async function openKickbackDay(month,day,resume){
 showModal('Откат · '+fmtDate(`${month}-${String(day).padStart(2,'0')}`),'<button class="secondary" id="kickbackBack">← К откатам</button><div id="kickbackDayBody">Загрузка…</div>');
 const body=qs('#kickbackDayBody'),active=()=>qs('#kickbackDayBody')===body&&!qs('#modal').classList.contains('hidden');
 let busy=false,dirty=false;
 qs('#kickbackBack').onclick=()=>{if(!busy&&(!dirty||confirm('Выйти без сохранения изменений?')))openPersonalTables(resume)};
 const money=c=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(c/100);
 try{
  const data=await api(`/api/personal-tables/kickbacks/${month}`);if(!active())return;
  const manual=Number(data.marina_adjustments[String(day)]||0),auto=Number(data.cells[`${day}:0`]||0)-manual;
  body.innerHTML=`<form id="kickbackForm"><h3 style="text-align:right">Итого: <span id="kickbackTotal">${money(auto+manual)} ₽</span></h3><p>Авто (из кассы): <b>${money(auto)} ₽</b></p><label>Ручное, ₽<input id="kickbackManual" type="text" inputmode="decimal" value="${manual/100}" style="width:100%;box-sizing:border-box;font-size:16px"></label><p class="muted">Итог = авто + ручное. Ручная сумма сохраняется отдельно от кассы.</p><button class="primary" type="submit">Сохранить</button><p id="kickbackStatus" role="status"></p></form>`;
  const input=qs('#kickbackManual'),status=qs('#kickbackStatus');
  function cents(){const raw=input.value.trim().replace(/\s/g,'').replace(',','.')||'0';if(!/^-?\d+(\.\d{1,2})?$/.test(raw)||Math.abs(Number(raw))>1000000000)throw Error('Введите сумму с точностью до копеек');return Math.round(Number(raw)*100)}
  input.oninput=()=>{dirty=true;try{qs('#kickbackTotal').textContent=money(auto+cents())+' ₽';status.textContent='Не сохранено'}catch(e){status.textContent=e.message}};
  qs('#kickbackForm').onsubmit=async e=>{e.preventDefault();if(busy)return;
   try{const value=String(cents()/100);busy=true;qsa('input,button',body).forEach(x=>x.disabled=true);
    await api(`/api/personal-tables/kickbacks/${month}`,{method:'PATCH',body:JSON.stringify({version:data.version,action:'manual_kickback',day,column:0,value})});
    if(active()){dirty=false;toast('Откат сохранён');await openPersonalTables(resume)}
   }catch(err){if(active())status.textContent=err.message}
   finally{busy=false;if(active())qsa('input,button',body).forEach(x=>x.disabled=false)}
  };
 }catch(e){if(active())body.textContent=e.message}
}
</script></body>''',1)

GBU_EARNINGS_RATES = {'standard:4': 200000, 'standard:6': 280000, 'elite:4': 340000, 'elite:6': 480000}
GBU_VLAD_RATES = {'standard:4': 150000, 'standard:6': 230000, 'elite:4': 300000, 'elite:6': 440000}

def gbu_earnings_rate(name, metro, category, team_size):
    label = re.sub(r'\s+', ' ', f'{name or ""} {metro or ""}'.casefold())
    special = bool(re.search(r'\b(влад|владислав)\b', label) and 'рязанск' in label)
    return (GBU_VLAD_RATES if special else GBU_EARNINGS_RATES).get(f'{category}:{team_size}'), special


def gbu_earnings_day(db, work_date, saved=None):
    saved = saved or {}
    orders = list(db.scalars(select(Order).where(Order.work_date == work_date, Order.source == 'gbu', Order.status != 'cancelled')))
    employees = {e.id: e for e in db.scalars(select(Employee).where(_visible_employee()))}
    by_tg = {e.tg_id: e for e in employees.values() if e.tg_id}
    assigned = {a.order_id: employees.get(a.employee_id) for a in db.scalars(select(Assignment).where(
        Assignment.order_id.in_([o.id for o in orders]), Assignment.role == 'brigadier'))}
    cards = list(db.scalars(select(NumberCardEvent).where(NumberCardEvent.work_date == work_date,
        NumberCardEvent.source == 'gbu', _visible_number_card()).order_by(NumberCardEvent.id)))
    def surname(value):
        return re.sub(r'[^а-яa-z]', '', (value or '').casefold().replace('ё','е').split()[0]) if (value or '').strip() else ''
    matched = set(); rows = []
    for card in cards:
        candidates = [o for o in orders if surname(card.surname) and surname(o.deceased_name) == surname(card.surname)]
        order = candidates[0] if len(candidates) == 1 else None
        if order and order.id in matched: order = None
        if order: matched.add(order.id)
        key = f'card:{card.id}'
        if order and key not in saved: key = f'order:{order.id}'
        rows.append((key, by_tg.get(card.tg_id), card.full_name, order))
    for order in orders:
        if order.id not in matched and assigned.get(order.id):
            emp = assigned[order.id]
            # An unmatched card for this foreman may be this order; avoid double-counting.
            if any(row_emp and row_emp.tg_id == emp.tg_id and row_order is None
                   for _, row_emp, _, row_order in rows): continue
            rows.append((f'order:{order.id}', emp, emp.full_name, order))
    items = []
    for key, emp, name, order in rows:
        detail = saved.get(key, {})
        category = detail.get('category') or (order.category if order else None)
        team = detail.get('team_size') or (order.team_size if order else None)
        metro = getattr(emp, 'metro', '') or ''
        rate, special = gbu_earnings_rate(name, metro, category, team)
        manual = detail.get('amount')
        items.append({'key': key, 'name': _employee_dict(emp)['display_name'] if emp else name,
            'category': category, 'team_size': team, 'auto': rate, 'special': special,
            'manual': manual, 'amount': manual if manual is not None else rate})
    return {'items': items, 'total': sum(x['amount'] or 0 for x in items),
        'missing': sum(x['amount'] is None for x in items)}


class GbuEarningsItemInput(BaseModel):
    category: Literal['standard','elite']
    team_size: Literal[4,6]
    amount: int | None = Field(default=None, ge=0, le=100000000000, strict=True)

class GbuEarningsInput(BaseModel):
    version: int = Field(ge=0, strict=True)
    items: dict[str, GbuEarningsItemInput]

@app.get('/api/gbu-earnings/{work_date}')
def get_gbu_earnings(work_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    row = db.scalar(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == user.id,
        PersonalMonthlyTable.kind == 'earnings', PersonalMonthlyTable.month == work_date.strftime('%Y-%m')))
    payload = json.loads(row.payload) if row else {}
    result = gbu_earnings_day(db, work_date, payload.get('gbu_details', {}).get(str(work_date.day)))
    return {**result, 'version': row.version if row else 0,
        'previous': payload.get('cells', {}).get(f'{work_date.day}:1') if str(work_date.day) not in payload.get('gbu_details', {}) else None}

@app.put('/api/gbu-earnings/{work_date}')
def save_gbu_earnings(work_date: date, change: GbuEarningsInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    month = work_date.strftime('%Y-%m')
    row = db.scalar(select(PersonalMonthlyTable).where(PersonalMonthlyTable.owner_id == user.id,
        PersonalMonthlyTable.kind == 'earnings', PersonalMonthlyTable.month == month).with_for_update())
    if change.version != (row.version if row else 0):
        raise HTTPException(409, 'Таблица изменена. Откройте день заново.')
    payload = json.loads(row.payload) if row else {'columns': list(PERSONAL_TABLE_COLUMNS['earnings']), 'cells': {}}
    current = gbu_earnings_day(db, work_date, payload.get('gbu_details', {}).get(str(work_date.day)))
    if set(change.items) != {x['key'] for x in current['items']}:
        raise HTTPException(409, 'Список заказов изменился. Откройте день заново.')
    payload.setdefault('gbu_details', {})[str(work_date.day)] = {k:v.model_dump() for k,v in change.items.items()}
    if row is None:
        row = PersonalMonthlyTable(owner_id=user.id, month=month, kind='earnings', version=0)
        db.add(row)
    row.payload = json.dumps(payload, ensure_ascii=False); row.version += 1
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise HTTPException(409, 'Таблица изменена. Откройте день заново.')
    return get_gbu_earnings(work_date, db, user)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "if(kind==='earnings'&&column===2){openPersonalCashDay",
    "if(kind==='earnings'&&column===1){openGbuEarningsDay(`${month}-${String(day).padStart(2,'0')}`,{month,kind,left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop});return}\n   if(kind==='earnings'&&column===2){openPersonalCashDay", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<script>
async function openGbuEarningsDay(day,resume){
 showModal('ГБУ · '+fmtDate(day),'<button class="secondary" id="gbuEarnBack">← К заработку</button><div id="gbuEarnBody">Загрузка…</div>');
 const body=qs('#gbuEarnBody'),active=()=>qs('#gbuEarnBody')===body&&!qs('#modal').classList.contains('hidden');
 let busy=false,dirty=false;
 qs('#gbuEarnBack').onclick=()=>{if(!busy&&(!dirty||confirm('Выйти без сохранения изменений?')))openPersonalTables(resume)};
 const money=c=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(c/100);
 try{
  const data=await api('/api/gbu-earnings/'+day);if(!active())return;
  const rates={'standard:4':200000,'standard:6':280000,'elite:4':340000,'elite:6':480000};
  const vlad={'standard:4':150000,'standard:6':230000,'elite:4':300000,'elite:6':440000};
  const drafts=data.items.map(x=>({...x,type:rates[x.category+':'+x.team_size]!==undefined?x.category+':'+x.team_size:'',manual:x.manual===null?null:String(x.manual/100)}));
  body.innerHTML=`<form id="gbuEarnForm"><h3 style="text-align:right">Итого: <span id="gbuEarnTotal"></span></h3>${data.previous!==null?`<p class="muted">Ранее в таблице: ${money(data.previous)} ₽. После сохранения эту сумму заменит итог по заказам.</p>`:''}<p class="muted">Каждый заказ — отдельная карточка. Выберите категорию и состав.</p><div class="gbu-earning-grid">${drafts.map((x,i)=>`<article class="gbu-earning-card"><b>${i+1}. ${esc(x.name)}</b><div class="gbu-card-controls"><label class="gbu-card-type-label">Категория и состав<select data-gbu-type="${i}" style="font-size:16px"><option value="">Выбрать</option>${Object.keys(rates).map(key=>`<option value="${key}" ${x.type===key?'selected':''}>${key.startsWith('elite')?'Элит':'Стандарт'} · ${key.endsWith('6')?'6':'4'} чел.</option>`).join('')}</select></label><label class="gbu-card-money-label">Сумма, ₽ <input data-gbu-money="${i}" inputmode="decimal" style="width:100%;font-size:16px" value="${x.manual===null?'':esc(x.manual)}" placeholder="Авто"></label><button type="button" class="secondary" data-gbu-auto="${i}">Авто</button></div><small class="muted" data-gbu-hint="${i}"></small></article>`).join('')||'<p class="gbu-earning-empty">Заказы ГБУ за этот день не найдены.</p>'}</div><p id="gbuEarnStatus" role="status"></p><button class="primary" type="submit" ${drafts.length?'':'disabled'}>Сохранить</button></form>`;
  function amount(x){if(x.manual===null)return (x.special?vlad:rates)[x.type]??null;const raw=x.manual.trim().replace(/\s/g,'').replace(',','.');if(!/^\d+(\.\d{1,2})?$/.test(raw)||Number(raw)>1000000000)throw Error('Введите сумму с точностью до копеек');return Math.round(Number(raw)*100)}
  function renderTotal(){let sum=0,missing=0;drafts.forEach((x,i)=>{const n=amount(x);if(n===null)missing++;else sum+=n;const auto=(x.special?vlad:rates)[x.type];qs(`[data-gbu-hint="${i}"]`,body).textContent=auto===undefined?'Для расчёта выберите категорию и состав':`${x.type.startsWith('elite')?'Элит':'Стандарт'} ${x.type.slice(-1)} чел. — ${money(auto)} ₽${x.special?' · ставка Влада Рязанского':''}${x.manual!==null?' · сумма изменена вручную':''}`;const input=qs(`[data-gbu-money="${i}"]`,body);input.placeholder=auto===undefined?'Не рассчитано':money(auto)});qs('#gbuEarnTotal').textContent=money(sum)+' ₽'+(missing?' · не рассчитано: '+missing:'')}
  qsa('[data-gbu-type]',body).forEach(el=>el.onchange=()=>{dirty=true;drafts[Number(el.dataset.gbuType)].type=el.value;renderTotal()});
  qsa('[data-gbu-money]',body).forEach(el=>el.oninput=()=>{dirty=true;drafts[Number(el.dataset.gbuMoney)].manual=el.value.trim()===''?null:el.value;try{renderTotal();qs('#gbuEarnStatus').textContent='Не сохранено'}catch(e){qs('#gbuEarnStatus').textContent=e.message}});
  qsa('[data-gbu-auto]',body).forEach(el=>el.onclick=()=>{dirty=true;const i=Number(el.dataset.gbuAuto);drafts[i].manual=null;qs(`[data-gbu-money="${i}"]`,body).value='';renderTotal()});
  renderTotal();
  qs('#gbuEarnForm').onsubmit=async e=>{e.preventDefault();if(busy)return;
   try{const items={};drafts.forEach(x=>{if(!x.type)throw Error('Укажите категорию и состав каждого заказа');items[x.key]={category:x.type.split(':')[0],team_size:Number(x.type.split(':')[1]),amount:x.manual===null?null:amount(x)}});
    busy=true;qsa('input,select,button',body).forEach(x=>x.disabled=true);
    await api('/api/gbu-earnings/'+day,{method:'PUT',body:JSON.stringify({version:data.version,items})});
    if(active()){dirty=false;toast('ГБУ сохранено');await openPersonalTables(resume)}
   }catch(err){if(active())qs('#gbuEarnStatus').textContent=err.message}
   finally{busy=false;if(active())qsa('input,select,button',body).forEach(x=>x.disabled=false)}
  };
 }catch(e){if(active())body.textContent=e.message}
}
</script></body>''',1)


# The money icon opens Vlad's own allowance; the owner's earnings remain separate.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "const cashBtn=qs('#cashBtn'); if(cashBtn) cashBtn.classList.toggle('hidden', !data.is_owner);",
    "updateMoneyButton(data);", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "const cashBtn=qs('#cashBtn'); if(cashBtn) cashBtn.classList.toggle('hidden', !state.bootstrap.is_owner);",
    "updateMoneyButton(state.bootstrap);", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "qs('#cashBtn').onclick=()=>openPersonalTables();",
    "qs('#cashBtn').onclick=()=>state.bootstrap?.is_owner?openPersonalTables():openVladAllowance();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<button class="secondary" id="earningsTables" style="margin-bottom:12px">📒 Мои таблицы</button>',
    '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px"><button class="secondary" id="earningsTables">📒 Мои таблицы</button><button class="secondary" id="earningsVladAllowance">💰 Надбавка Владу</button></div>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "qsa('[data-personal-kind],#personalMonth,#personalAdd,#earningsTables')",
    "qsa('[data-personal-kind],#personalMonth,#personalAdd,#earningsTables,#earningsVladAllowance')", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    " qs('#earningsTables').onclick=()=>{",
    """ qs('#earningsVladAllowance').onclick=()=>{
  if(busy)return;
  openVladAllowance({month,kind,left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop});
 };
 qs('#earningsTables').onclick=()=>{""", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</script></body>', r"""
function updateMoneyButton(data){
 const btn=qs('#cashBtn');if(!btn)return;
 btn.classList.toggle('hidden',!(data.is_owner||data.can_view_vlad_allowance));
 const label=data.is_owner?'Заработок':'Моя надбавка';
 btn.setAttribute('aria-label',label);btn.title=label;
}
async function openVladAllowance(resume=null){
 if(!(state.bootstrap?.is_owner||state.bootstrap?.can_view_vlad_allowance)){toast('Нет доступа к надбавке');return}
 let month=resume?.month||state.selectedDate?.slice(0,7)||new Date().toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'}).slice(0,7);
 let data=null,revision=0,signature='';
 const title=state.bootstrap?.is_owner?'💵 Владу':'💵 Моя надбавка';
 showModal(title,`<button type="button" class="secondary" id="vladAllowanceBack" aria-label="Назад">←</button>
 <label class="vlad-month-picker"><input type="month" id="vladAllowanceMonth" value="${esc(month)}" aria-label="Месяц надбавки"></label>
 <p class="muted vlad-month-note">1 сотрудник = 100 ₽ · без выносов</p>
 <div id="vladAllowanceBody">Загрузка…</div><small class="muted" id="vladAllowanceStatus" role="status"></small>`);
 const body=qs('#vladAllowanceBody'),status=qs('#vladAllowanceStatus'),input=qs('#vladAllowanceMonth'),session=cashModalGeneration;
 qs('#modal .modal-head').prepend(qs('#vladAllowanceBack'));qs('#modal').scrollTop=0;
 const active=()=>session===cashModalGeneration && qs('#vladAllowanceBody')===body && !qs('#modal').classList.contains('hidden');
 const rub=value=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0}).format(Number(value||0)/100)+' ₽';
 qs('#vladAllowanceBack').onclick=()=>resume?openPersonalTables(resume):closeModal();
 function draw(){
  if(!active()||!data)return;
  const rows=data.daily;
  const staff=days=>days.reduce((sum,x)=>sum+x.staff_count,0);
  const totalRow=(label,count,amount)=>`<tr><th scope="row">${label}</th><td>${count}</td><td>${rub(amount)}</td></tr>`;
  body.innerHTML=`<table class="vlad-month-table" aria-label="Надбавка за весь месяц"><colgroup><col style="width:22%"><col style="width:34%"><col style="width:44%"></colgroup><thead><tr><th scope="col">День</th><th scope="col">Сотрудников</th><th scope="col">Надбавка</th></tr></thead><tbody>${rows.map(x=>`<tr data-vlad-day="${x.day}"${x.work_date===state.selectedDate?' class="is-selected-day"':''}><td>${x.day}</td><td>${x.staff_count}</td><td>${rub(x.amount)}</td></tr>`).join('')}</tbody><tfoot>${totalRow('1–15',staff(rows.filter(x=>x.day<=15)),data.first)}${totalRow('16–'+data.days,staff(rows.filter(x=>x.day>=16)),data.second)}${totalRow('За месяц',staff(rows),data.total)}</tfoot></table>`;
  fitVladAllowanceMonth();
 }
 async function load(){
  const request=++revision,wanted=month;
  try{
   const fresh=await api(`/api/vlad-allowance/${wanted}`);
   if(!active()||request!==revision||wanted!==month)return;
   const next=JSON.stringify(fresh);data=fresh;
   if(next!==signature){signature=next;draw()}
   status.textContent='Обновляется автоматически';
   fitVladAllowanceMonth();
   requestAnimationFrame(()=>{if(active())fitVladAllowanceMonth()});
  }catch(e){if(active()&&request===revision)status.textContent=e.message}
 }
 input.onchange=()=>{if(!/^\d{4}-\d{2}$/.test(input.value))return;month=input.value;data=null;signature='';body.textContent='Загрузка…';status.textContent='';load()};
 await load();
 if(active())cashRefreshTimer=setInterval(()=>{if(active()&&!document.hidden)load()},10000);
}
</script></body>""", 1)

# Both number-card actions are available to authenticated dispatchers.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '''${state.bootstrap?.is_owner?`<button type="button" class="secondary danger" data-number-delete="${x.id}" aria-label="Удалить запись" style="width:36px;height:36px;padding:4px;font-size:18px;border-radius:10px">🗑</button>`:''}''',
    '''<button type="button" class="secondary danger" data-number-delete="${x.id}" aria-label="Удалить запись" style="width:36px;height:36px;padding:4px;font-size:18px;border-radius:10px">🗑</button>''', 1)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</script></body>', r'''
async function openFreeStaff(){
 showModal('Без заказа на завтра','<div id="freeStaffBody">Загрузка…</div>');
 const body=qs('#freeStaffBody');
 try{
  const data=await api('/api/readiness/free-tomorrow');if(qs('#freeStaffBody')!==body)return;
  const selected=new Set();let locked=false;
  const requestId=Array.from(crypto.getRandomValues(new Uint8Array(20)),b=>b.toString(16).padStart(2,'0')).join('');
  const group=code=>{
   const g=data.groups.find(x=>x.code===code);
   return `<section class="free-staff-group"><b>${esc(g.label)} · ${g.people.length}</b>${g.people.map(p=>`<label class="free-staff-row"><span>${esc(p.name)} · ${esc(p.metro)} · ${esc(p.height)}</span><input type="checkbox" value="${p.id}" aria-label="Без заказа: ${esc(p.name)} ${esc(p.metro)} ${esc(p.height)}"></label>`).join('')||'<small>Нет готовых</small>'}</section>`;
  };
  body.innerHTML=`<style>
   #freeStaffBody .free-staff-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}
   #freeStaffBody .free-staff-group{border:1px solid var(--line);border-radius:10px;padding:5px;margin-bottom:8px}
   #freeStaffBody .free-staff-group>b{display:block;color:var(--gold);font-size:12px;padding:5px 0}
   #freeStaffBody .free-staff-row{display:grid;grid-template-columns:minmax(0,1fr) 17px;align-items:center;gap:5px;min-height:32px;border-top:1px solid var(--line);font-size:11px;line-height:1.2;cursor:pointer}
   #freeStaffBody .free-staff-row span{min-width:0;overflow-wrap:anywhere}
   #freeStaffBody input{width:17px;height:17px;min-width:17px;margin:0;padding:0;accent-color:var(--green)}
   #freeStaffBody .free-staff-footer{position:sticky;bottom:0;background:var(--panel);padding:10px 0;display:flex;align-items:center;gap:10px;border-top:1px solid var(--line)}
   #freeStaffBody .free-staff-footer button{flex:1}
  </style><p style="font-size:13px;color:var(--muted)">${esc(data.work_date)} · Отметьте свободных сотрудников</p>
  <div class="free-staff-grid"><div>${group('brigadier')}${group('cashless')}</div><div>${group('main')}${group('reserve')}</div></div>
  <p style="font-size:12px;color:var(--muted)">Каждая группа получит своих отмеченных сотрудников. В бригадирскую группу отправится общий список всех отмеченных с телефонами для замены утром. Повторная отправка обновит общий список.</p>
  <div id="freeStaffStatus" role="status" style="font-size:13px"></div>
  <div class="free-staff-footer"><span style="font-size:13px">Без заказа: <b id="freeStaffCount">0</b></span><button class="primary" id="freeStaffSend" disabled>Отправить</button></div>`;
  const button=qs('#freeStaffSend'),status=qs('#freeStaffStatus'),checks=qsa('input[type="checkbox"]',body);
  checks.forEach(c=>c.onchange=()=>{if(locked)return;c.checked?selected.add(Number(c.value)):selected.delete(Number(c.value));qs('#freeStaffCount').textContent=selected.size;button.disabled=!selected.size});
  button.onclick=async()=>{
   if(button.disabled)return;
   locked=true;button.disabled=true;checks.forEach(c=>c.disabled=true);status.textContent='Отправка…';
   try{
    const r=await api('/api/readiness/free-tomorrow',{method:'POST',body:JSON.stringify({work_date:data.work_date,employee_ids:[...selected],snapshot:data.snapshot,request_id:requestId})});
    if(qs('#freeStaffBody')!==body)return;
    if(r.failed_groups.length){status.textContent=`Отправлено в ${r.sent_chats} групп. Не доставлено: ${r.failed_groups.join(', ')}. Повтор отправит только недоставленное.`;button.textContent='Повторить';button.disabled=false}
    else{status.textContent=`✓ Отправлено в ${r.sent_chats} групп`;button.textContent='Отправлено'}
   }catch(e){if(qs('#freeStaffBody')!==body)return;status.textContent=e.message;button.textContent='Повторить';button.disabled=false}
  };
 }catch(e){if(qs('#freeStaffBody')===body)body.textContent=e.message}
}
qs('#readinessHistoryBtn').onclick=openFreeStaff;
qs('#readinessHistoryBtn').title='Выбрать готовых сотрудников без заказа на завтра';
</script></body>''', 1)

# Include uncommon team sizes in the period breakdown as well as daily totals.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    "orderCountTypes[src].map(key=>", "periodCountKeys(src,r.sources[src].counts).map(key=>", 1)


def _compact_personal_table_html(html):
    """Keep the earnings columns compact without changing table data or actions."""
    start = html.index('async function openPersonalTables(')
    end = html.index('\nconst orderCountTypes=', start)
    personal = html[start:end]
    personal = re.sub(r'<style>#personalGrid\b.*?</style>', '', personal, count=1, flags=re.S)
    personal = personal.replace(
        '<table style="border-collapse:separate;border-spacing:8px;min-width:100%;text-align:right">',
        '<table class="personal-table">',
        1,
    ).replace(
        'class="contact-btn" style="min-width:80px"',
        'class="contact-btn personal-cell"',
    ).replace(
        '<button class="secondary" style="white-space:nowrap" ${data.automatic_columns.includes(col)?\'disabled\':\'\'} data-personal-col="${col}">${esc(name)} ${data.automatic_columns.includes(col)?\'· авто\':\'✍️\'}</button>',
        '<button class="secondary personal-column" ${data.automatic_columns.includes(col)?\'disabled\':\'\'} data-personal-col="${col}"><span>${esc(name)}</span><small>${data.automatic_columns.includes(col)?\'авто\':\'✍️\'}</small></button>',
        1,
    ).replace(
        '<tr style="color:var(--gold);background:#211d14"><th>${title}</th>',
        '<tr class="personal-total" style="color:var(--gold);background:#211d14"><th class="personal-period">${esc(title).replace(" ","<br>")}</th>',
        1,
    )
    html = html[:start] + personal + html[end:]
    return html.replace('</head>', r'''
<style id="personal-table-compact-style">
#personalGrid{overscroll-behavior:contain;-webkit-overflow-scrolling:touch}
#personalGrid table.personal-table{width:auto;min-width:0;border-collapse:separate;border-spacing:0;text-align:center;font-size:14px;font-variant-numeric:tabular-nums}
#personalGrid .personal-table th,#personalGrid .personal-table td{box-sizing:border-box;padding:2px 3px;vertical-align:middle}
#personalGrid .personal-table th:not(:first-child),#personalGrid .personal-table td:not(:first-child){min-width:76px}
#personalGrid .personal-table thead th{position:sticky;top:0;z-index:3;height:52px;background:var(--panel2);box-shadow:0 1px 0 var(--line);font-size:12px;font-weight:700;line-height:1.25;white-space:normal}
#personalGrid .personal-table th:first-child{position:sticky;left:0;z-index:2;width:44px;min-width:44px;max-width:44px;background:#111114;box-shadow:1px 0 0 var(--line);text-align:center;white-space:normal;overflow-wrap:anywhere}
#personalGrid .personal-table thead th:first-child{z-index:4;background:var(--panel2);box-shadow:1px 1px 0 var(--line)}
#personalGrid .personal-table tbody th{height:44px;font-size:14px;font-weight:600}
#personalGrid .personal-table tbody td{height:44px;white-space:nowrap}
#personalGrid .personal-column{display:block;box-sizing:border-box;width:76px;max-width:76px;min-height:44px;margin:0;padding:4px 2px;border:0;border-radius:6px;background:transparent;font-size:12px;font-weight:700;line-height:1.25;white-space:normal;overflow-wrap:anywhere}
#personalGrid .personal-column:disabled{opacity:1}
#personalGrid .personal-column small{display:block;margin-top:2px;color:var(--muted);font-size:11px;font-weight:400;line-height:1.2}
#personalGrid .personal-cell{box-sizing:border-box;width:100%;min-width:0;min-height:40px;padding:4px 2px;border-radius:8px;font-size:14px;line-height:1.25;white-space:nowrap}
#personalGrid td.vlad-deduction{color:#ff5555;font-weight:700}
#personalGrid .personal-total th:first-child{background:#211d14;font-size:11px;line-height:1.2;overflow-wrap:normal}
#personalGrid .personal-total td{font-size:13px;font-weight:700}
@media(pointer:coarse){#personalGrid .personal-cell{min-height:44px}}
@media(max-width:600px){
 #modal:has(#personalGrid){left:6px;right:6px;width:auto;padding:12px 8px}
 #personalGrid{max-width:100%;min-width:0}
 #personalGrid table.personal-table{table-layout:fixed;width:100%;max-width:100%;min-width:0;font-size:clamp(8px,2.5vw,11px)}
 #personalGrid .personal-table th,#personalGrid .personal-table td{padding:2px 1px;overflow-wrap:anywhere}
 #personalGrid .personal-table th:not(:first-child),#personalGrid .personal-table td:not(:first-child){min-width:0}
 #personalGrid .personal-table th:first-child{width:24px;min-width:24px;max-width:24px}
 #personalGrid .personal-table thead th{height:42px;font-size:clamp(8.5px,2.55vw,11px);line-height:1.15}
 #personalGrid .personal-table tbody th{height:30px;font-size:10px}
 #personalGrid .personal-table tbody td{height:30px;white-space:normal}
 #personalGrid .personal-column{width:100%;max-width:100%;min-width:0;min-height:38px;padding:2px 0;border-radius:4px;font-size:inherit;line-height:1.15}
 #personalGrid .personal-column small{margin-top:1px;font-size:9px;line-height:1.1}
 #personalGrid .personal-cell{min-height:28px;padding:2px 0;border-radius:5px;font-size:inherit;line-height:1.15;white-space:normal;overflow-wrap:anywhere}
 #personalGrid .personal-total th:first-child{font-size:8px;line-height:1.2;overflow-wrap:anywhere}
 #personalGrid .personal-total td{font-size:clamp(8px,2.3vw,10px)}
 #personalGrid .gbu-comment-panel{max-width:100%}
}
</style>
</head>''', 1)


INLINE_INDEX_HTML = _compact_personal_table_html(INLINE_INDEX_HTML)


def _personal_comments_html(html):
    """Open the GBU header's monthly note without changing daily earnings."""
    start = html.index('async function openPersonalTables(')
    end = html.index('\nconst orderCountTypes=', start)
    personal = html[start:end]

    def replace_once(old, new):
        nonlocal personal
        if old not in personal:
            raise ValueError('Personal comment UI anchor missing: ' + old[:70])
        personal = personal.replace(old, new, 1)

    replace_once("data=null,busy=false,request=0;",
                 "data=null,busy=false,request=0,commentOpen=false,commentDraft='',commentVersion=0,commentTop=0;")
    replace_once(".forEach(e=>e.disabled=value)}", """.forEach(e=>e.disabled=value||commentOpen);
  qsa('#personalGbuCommentForm input,#personalGbuCommentForm button,[data-personal-col]',grid).forEach(e=>e.disabled=value||(e.dataset.personalCol!==undefined&&data?.automatic_columns.includes(Number(e.dataset.personalCol))))
 }""")
    replace_once('class="secondary personal-column"',
                 'class="secondary personal-column${kind===\'earnings\'&&col===1&&commentOpen?\' is-comment-open\':\'\'}"')
    replace_once('data-personal-col="${col}"><span>${esc(name)}</span>',
                 '''data-personal-col="${col}" ${kind==='earnings'&&col===1?`aria-expanded="${commentOpen}" aria-label="${data.gbu_comment?.text?.trim()?'Открыть сохранённый комментарий к ГБУ':'Добавить комментарий к ГБУ'}"`:''}><span class="personal-column-label">${esc(name)}${kind==='earnings'&&col===1&&data.gbu_comment?.text?.trim()?'<i class="gbu-comment-dot" aria-hidden="true"></i>':''}</span>''')
    replace_once('<tbody>${rows}</tbody>', '<tbody>${gbuCommentRow()}${rows}</tbody>')
    replace_once("  qsa('[data-kickback-day]',grid)", "  bindGbuComment();\n  qsa('[data-kickback-day]',grid)")
    replace_once("if(busy)return;const column=Number(btn.dataset.personalCol),value=prompt('Название столбца',data.columns[column]);", """if(busy)return;const column=Number(btn.dataset.personalCol);
   if(kind==='earnings'&&column===1){awaitToggleGbuComment();return}
   if(commentOpen)return;
   const value=prompt('Название столбца',data.columns[column]);""")
    replace_once("if(busy)return;const [day,column]", "if(busy||commentOpen)return;const [day,column]")
    # A background response must never replace a draft or overwrite a just-saved note.
    replace_once("  if(busy||!data)return;\n  const id=request,loadedKind=kind,loadedMonth=month;",
                 "  if(busy||!data||commentOpen)return;\n  const id=request,loadedKind=kind,loadedMonth=month;")
    replace_once("if(active()&&!busy&&id===request&&kind===loadedKind",
                 "if(active()&&!busy&&!commentOpen&&id===request&&kind===loadedKind")
    replace_once(' async function load(){', r'''
 function gbuCommentRow(){
  if(!commentOpen||kind!=='earnings')return '';
  const width=Math.max(220,grid.clientWidth-12);
  return `<tr class="gbu-comment-row"><td colspan="${data.columns.length+4}"><form id="personalGbuCommentForm" class="gbu-comment-panel" style="width:${width}px">
   <label for="personalGbuCommentInput">Комментарий к ГБУ</label>
   <p class="muted" id="personalGbuCommentHelp">Заработок за предыдущую половину месяца</p>
   <input id="personalGbuCommentInput" type="text" maxlength="500" aria-describedby="personalGbuCommentHelp" placeholder="Укажите период и сумму заработка">
   <small class="muted">Чтобы удалить запись, очистите поле и сохраните.</small>
   <p id="personalGbuCommentStatus" role="status" aria-live="polite"></p>
   <div class="gbu-comment-actions"><button type="submit" class="primary">Сохранить</button><button type="button" class="secondary" id="personalGbuCommentCancel">Отмена</button></div>
  </form></td></tr>`;
 }
 function closeGbuComment(){
  if(busy)return;
  ++request;commentOpen=false;grid.scrollTop=commentTop;render();lock(false);
 }
 async function awaitToggleGbuComment(){
  if(commentOpen){closeGbuComment();return}
  ++request;lock(true);status.textContent='Загрузка комментария…';
  try{
   const note=await api(`/api/personal-gbu-comment/${month}`);if(!active())return;
   data.gbu_comment=note;commentDraft=note.text;commentVersion=note.version;
   commentTop=grid.scrollTop;commentOpen=true;grid.scrollTop=0;render();status.textContent='';
   qs('#personalGbuCommentInput',grid).focus({preventScroll:true});
  }catch(e){if(active())status.textContent=e.message}
  finally{if(active())lock(false)}
 }
 function bindGbuComment(){
  if(!commentOpen)return;
  const form=qs('#personalGbuCommentForm',grid),input=qs('#personalGbuCommentInput',grid),message=qs('#personalGbuCommentStatus',grid);
  input.value=commentDraft;
  input.oninput=()=>{commentDraft=input.value;message.textContent=''};
  qs('#personalGbuCommentCancel',grid).onclick=closeGbuComment;
  form.onsubmit=async e=>{
   e.preventDefault();if(busy||!active())return;
   ++request;lock(true);message.textContent='Сохранение…';
   try{
    const note=await api(`/api/personal-gbu-comment/${month}`,{method:'PUT',body:JSON.stringify({text:input.value,version:commentVersion})});
    if(!active())return;
    data.gbu_comment=note;commentDraft=note.text;commentVersion=note.version;commentOpen=false;
    grid.scrollTop=commentTop;render();status.textContent=note.text?'✓ Комментарий сохранён':'Комментарий очищен';
   }catch(err){if(active())message.textContent='Не сохранено: '+err.message}
   finally{if(active())lock(false)}
  };
 }
 async function load(){''')
    html = html[:start] + personal + html[end:]
    return html.replace('</head>', r'''
<style id="personal-gbu-comment-style">
#personalGrid .personal-column-label{position:relative;display:inline-block}
#personalGrid .gbu-comment-dot{position:absolute;top:-1px;right:-8px;width:6px;height:6px;border-radius:50%;background:var(--gold);pointer-events:none}
#personalGrid .personal-column.is-comment-open{background:#302719;box-shadow:inset 0 0 0 1px var(--gold)}
#personalGrid .personal-table .gbu-comment-row>td{height:auto;padding:8px 0;text-align:left;white-space:normal}
#personalGrid .gbu-comment-panel{position:sticky;left:0;box-sizing:border-box;margin:0;padding:12px;background:var(--panel2);border:1px solid var(--line);border-radius:12px;color:var(--text);text-align:left;white-space:normal}
#personalGrid .gbu-comment-panel label{display:block;font-size:16px;font-weight:700}
#personalGrid .gbu-comment-panel p{margin:5px 0 8px;font-size:12px;line-height:1.4}
#personalGrid .gbu-comment-panel input{display:block;box-sizing:border-box;width:100%;min-width:0;margin:8px 0;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--panel);color:var(--text);font-size:16px}
#personalGrid .gbu-comment-panel small{font-size:11px;line-height:1.4}
#personalGrid #personalGbuCommentStatus{color:var(--amber);overflow-wrap:anywhere}
#personalGrid .gbu-comment-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
#personalGrid .gbu-comment-actions button{min-height:40px;padding:8px 14px;font-size:14px}
</style>
</head>''', 1)


INLINE_INDEX_HTML = _personal_comments_html(INLINE_INDEX_HTML)


class OwnerCabinetConfig(Base):
    __tablename__ = "owner_brigadier_cabinet_config"
    id = Column(Integer, primary_key=True)
    owner_tg_id = Column(BigInteger, nullable=False)
    bot_username = Column(String(32), nullable=True)


class BrigadierCabinetAccess(Base):
    __tablename__ = "brigadier_cabinet_access"
    tg_id = Column(BigInteger, primary_key=True)
    employee_id = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    granted_by = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CabinetAccessInput(BaseModel):
    active: StrictBool
    version: StrictInt = Field(ge=0)
    kind: Literal['assistant', 'cashier', 'brigadier'] | None = None


class CabinetRole(Base):
    __tablename__ = 'brigadier_cabinet_roles'
    tg_id = Column(BigInteger, primary_key=True)
    kind = Column(String(20), nullable=False)


def cabinet_kind(db, uid):
    if _is_cabinet_owner(db, uid):
        return 'owner'
    role = db.get(CabinetRole, uid)
    grant = db.get(BrigadierCabinetAccess, uid)
    if role and grant:
        return role.kind
    if uid in allowed_ids():
        return 'assistant'
    employee = db.scalar(select(Employee).where(Employee.tg_id == uid,
        Employee.active.is_(True), _visible_employee()).limit(1))
    return 'cashier' if employee and getattr(employee, 'is_cashier', False) else 'brigadier'


def _dispatcher_access(db, uid):
    if _is_cabinet_owner(db, uid):
        return True
    grant = db.get(BrigadierCabinetAccess, uid)
    if grant:
        return _can_use_owner_cabinet(db, uid) and cabinet_kind(db, uid) == 'assistant'
    return uid in allowed_ids() and uid not in HIDDEN_TG_IDS


def cabinet_can_cash(db, uid):
    return _can_use_owner_cabinet(db, uid) and cabinet_kind(db, uid) in {'owner', 'assistant', 'cashier'}


def _is_cabinet_owner(db: Session, tg_id: int) -> bool:
    config = db.get(OwnerCabinetConfig, 1)
    return bool(config and config.owner_tg_id == int(tg_id)
                and int(tg_id) not in HIDDEN_TG_IDS and _is_owner(db, tg_id))


def cabinet_access_info(db: Session, employee):
    if employee.group_code != "brigadier" or not employee.tg_id or not employee.active:
        return None
    if _is_cabinet_owner(db, employee.tg_id):
        return {"owner": True, "active": True, "version": 0, "kind": 'owner'}
    grant = db.get(BrigadierCabinetAccess, employee.tg_id)
    return {"owner": False, "active": _can_use_owner_cabinet(db, employee.tg_id),
            "version": grant.version if grant else 0, "kind": cabinet_kind(db, employee.tg_id)}


@app.put("/api/employees/{employee_id}/cabinet-access")
def set_cabinet_access(employee_id: int, data: CabinetAccessInput,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _is_cabinet_owner(db, user.id):
        raise HTTPException(403, "Подключать кабинеты может только владелец «ЛЕГИОН»")
    employee = db.scalar(select(Employee).where(Employee.id == employee_id,
        Employee.active.is_(True), Employee.group_code == "brigadier", _visible_employee()))
    if not employee or not employee.tg_id:
        raise HTTPException(409, "Сначала нужна регистрация бригадира в боте")
    if employee.tg_id == user.id:
        raise HTTPException(409, "Ваш кабинет уже подключён")
    # Serialize owner-issued role changes, including the two-helper limit.
    db.scalar(select(OwnerCabinetConfig).where(OwnerCabinetConfig.id == 1).with_for_update())
    grant = db.get(BrigadierCabinetAccess, employee.tg_id)
    if (grant.version if grant else 0) != data.version:
        raise HTTPException(409, "Доступ уже изменён. Обновите список бригадиров")
    values = dict(employee_id=employee.id, active=data.active, version=data.version + 1,
                  granted_by=user.id, updated_at=datetime.utcnow())
    try:
        kind = data.kind or cabinet_kind(db, employee.tg_id)
        if kind == 'assistant' and data.active:
            others = db.scalar(select(func.count()).select_from(BrigadierCabinetAccess).join(
                CabinetRole, CabinetRole.tg_id == BrigadierCabinetAccess.tg_id).where(
                BrigadierCabinetAccess.active.is_(True), BrigadierCabinetAccess.granted_by == user.id,
                CabinetRole.kind == 'assistant', BrigadierCabinetAccess.tg_id != employee.tg_id))
            if others >= 2:
                raise HTTPException(409, 'Уже подключены два помощника. Сначала смените роль или отключите одного из них')
        if grant:
            result = db.execute(update(BrigadierCabinetAccess).where(
                BrigadierCabinetAccess.tg_id == employee.tg_id,
                BrigadierCabinetAccess.version == data.version).values(**values))
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "Доступ уже изменён. Обновите список бригадиров")
        else:
            db.add(BrigadierCabinetAccess(tg_id=employee.tg_id, **values))
        role = db.get(CabinetRole, employee.tg_id)
        if role is None:
            db.add(CabinetRole(tg_id=employee.tg_id, kind=kind))
        else:
            role.kind = kind
        employee.is_cashier = kind in {'assistant', 'cashier'}
        if not data.active:
            db.execute(update(CabinetCommand).where(CabinetCommand.tg_id == employee.tg_id,
                CabinetCommand.status == "queued").values(status="error", payload="{}",
                result=json.dumps({"error": "Доступ к кабинету отключён"}, ensure_ascii=False)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Доступ уже изменён. Обновите список бригадиров")
    return {"employee_id": employee.id, "cabinet_access": cabinet_access_info(db, employee)}


def bot_cabinet_access_data(db, tg_id):
    grant = db.get(BrigadierCabinetAccess, tg_id)
    return {"tg_id": tg_id, "allowed": _can_use_owner_cabinet(db, tg_id),
            "version": grant.version if grant else 0, "kind": cabinet_kind(db, tg_id),
            "can_record_cash": cabinet_can_cash(db, tg_id)}


@app.get("/api/bot/cabinet/access", dependencies=[Depends(_bot_key)])
def bot_cabinet_access_list(db: Session = Depends(get_db)):
    return {"items": [bot_cabinet_access_data(db, row.tg_id)
                      for row in db.scalars(select(BrigadierCabinetAccess))]}


@app.get("/api/bot/cabinet/access/{tg_id}", dependencies=[Depends(_bot_key)])
def bot_cabinet_access_check(tg_id: int, db: Session = Depends(get_db)):
    return bot_cabinet_access_data(db, tg_id)


class CabinetProfilePhoto(Base):
    __tablename__ = "owner_cabinet_profile_photos"
    tg_id = Column(BigInteger, primary_key=True)
    image = Column(String, nullable=False)


class CabinetProfilePhotoInput(BaseModel):
    image: str = Field(min_length=30, max_length=350000)


@app.put("/api/owner/brigadier-cabinet/avatar")
def cabinet_avatar_save(data: CabinetProfilePhotoInput, db: Session = Depends(get_db),
                        user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет недоступен")
    import base64
    import binascii
    prefix = "data:image/jpeg;base64,"
    if not data.image.startswith(prefix):
        raise HTTPException(422, "Выберите фото из галереи")
    try:
        raw = base64.b64decode(data.image[len(prefix):], validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "Не удалось прочитать фото. Выберите его заново")
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "Фото слишком большое. Выберите другое фото")
    if len(raw) < 32 or not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
        raise HTTPException(422, "Не удалось прочитать фото. Выберите его заново")
    # The browser crops/re-encodes the avatar; keep it separate from bot profile sync.
    image = prefix + base64.b64encode(raw).decode("ascii")
    db.merge(CabinetProfilePhoto(tg_id=user.id, image=image))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        db.execute(update(CabinetProfilePhoto).where(
            CabinetProfilePhoto.tg_id == user.id).values(image=image))
        db.commit()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "private, no-store"})


@app.delete("/api/owner/brigadier-cabinet/avatar")
def cabinet_avatar_delete(db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет недоступен")
    photo = db.get(CabinetProfilePhoto, user.id)
    if photo:
        db.delete(photo)
        db.commit()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "private, no-store"})


def _can_use_owner_cabinet(db: Session, tg_id: int) -> bool:
    if _is_cabinet_owner(db, tg_id):
        return True
    if int(tg_id) in HIDDEN_TG_IDS:
        return False
    if db.get(OwnerManagedBlock, int(tg_id)):
        return False
    config = db.get(OwnerCabinetConfig, 1)
    grant = db.get(BrigadierCabinetAccess, int(tg_id))
    if not config or not grant or not grant.active or grant.granted_by != config.owner_tg_id:
        return False
    return db.scalar(select(Employee.id).where(Employee.id == grant.employee_id,
        Employee.tg_id == int(tg_id), Employee.active.is_(True),
        Employee.group_code == "brigadier", _visible_employee()).limit(1)) is not None


def _owner_cabinet_readiness(db: Session, employee, work_date: date):
    row = db.scalar(select(Readiness).where(
        Readiness.employee_id == employee.id, Readiness.work_date == work_date)) if employee else None
    status = row.status if row else "no_response"
    raw_text = row.raw_text if row else ""
    if employee:
        manual = db.scalar(select(ManualReadiness).where(
            ManualReadiness.work_date == work_date,
            ManualReadiness.identity_key == f"employee:{employee.id}",
        ).order_by(ManualReadiness.reported_at.desc()).limit(1))
        if manual and (not row or not row.reported_at or manual.reported_at >= row.reported_at):
            status, raw_text = manual.status, ""
    labels = {"ready": "Готов", "not_ready": "На основной", "day_off": "Выходной",
              "responded": "Отписался", "no_response": "Ещё не отмечена"}
    return {"work_date": work_date.isoformat(), "status": status,
            "label": labels.get(status, "Ещё не отмечена"), "text": raw_text or ""}


@app.get("/api/owner/brigadier-cabinet/employees")
def owner_cabinet_employees(db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет не подключён или доступ отключён")
    employees = db.scalars(select(Employee).where(
        Employee.active.is_(True), _visible_employee(),
        Employee.group_code.in_(("brigadier", "main", "reserve", "cashless"))
    ).order_by(Employee.full_name, Employee.id)).all()
    fields = ("id", "full_name", "phone", "telegram_username", "telegram_url",
              "group_code", "height_cm", "has_car", "metro")
    items = []
    manage = _is_cabinet_owner(db, user.id)
    for employee in employees:
        profile = _employee_dict(employee)
        item = {key: profile[key] for key in fields}
        if manage:
            item["cabinet_access"] = cabinet_access_info(db, employee)
        items.append(item)
    return JSONResponse({"items": items}, headers={"Cache-Control": "private, no-store, max-age=0"})


# One-time restart of the cabinet list, by the original "Number to group"
# publication timestamp. This date does not roll forward each midnight.
CABINET_ACTIVE_START_UTC = datetime(2026, 10, 13, tzinfo=ZoneInfo("Europe/Moscow")).astimezone(timezone.utc)


@app.get("/api/owner/brigadier-cabinet")
def owner_brigadier_cabinet(work_date: date | None = None, db: Session = Depends(get_db),
                            user: TelegramUser = Depends(require_app_user), active_only: bool = False):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет не подключён или доступ отключён")
    today = datetime.now(MOSCOW).date()
    selected = work_date or today
    config = db.get(OwnerCabinetConfig, 1)
    # Identity is derived only from the authenticated Telegram session.
    employee = db.scalar(select(Employee).where(
        Employee.tg_id == user.id, Employee.active.is_(True), _visible_employee(),
        Employee.group_code == "brigadier").order_by(Employee.id).limit(1))
    profile = _employee_dict(employee) if employee else None
    avatar = db.get(CabinetProfilePhoto, user.id)
    orders = [] if active_only else db.scalars(select(Order).join(Assignment, Assignment.order_id == Order.id).join(
        Employee, Employee.id == Assignment.employee_id).where(
        Employee.tg_id == user.id, Employee.active.is_(True), _visible_employee(),
        Assignment.role == "brigadier", Order.work_date == selected,
        Order.status.notin_(["closed", "cancelled"]) if active_only else Order.status != "cancelled").order_by(Order.work_date, Order.issue_time, Order.id)).unique().all()
    cards = db.scalars(select(NumberCardEvent).where(
        NumberCardEvent.tg_id == user.id, True if active_only else NumberCardEvent.work_date == selected,
        NumberCardEvent.sent_at >= CABINET_ACTIVE_START_UTC if active_only else True,
        _visible_number_card()).order_by(NumberCardEvent.sent_at, NumberCardEvent.id)).all()
    statuses = number_card_statuses(db, cards)
    contacts = {row.event_id: bool(row.confirmed) for row in db.scalars(
        select(OrderBrigadeContact).where(OrderBrigadeContact.tg_id == user.id,
            OrderBrigadeContact.event_id.in_([card.id for card in cards])))} if cards else {}
    items = [{"id": card.id, "source": card.source, "surname": card.surname,
              "work_date": card.work_date.isoformat(),
              "_action_ref": f"{card.chat_id}_{card.message_id}", "report_key": f"card:{card.chat_id}:{card.message_id}",
              "composition_closed": statuses[card.id]["closed"],
              "contact_time": statuses[card.id]["contact_time"],
              "on_contact": bool(contacts.get(card.id)), **effective_order_photo(db, card), **cabinet_extra_state(db, card),
              **cabinet_order_deletion_info(db, card)} for card in cards]
    if active_only:
        items = [item for item in items if not item.get('tea_received')]
    def contact_sort_key(item):
        clock = item["contact_time"]
        minutes = sum(int(value) * factor for value, factor in zip(clock.split(":"), (60, 1))) if clock else 1440
        return item["work_date"], minutes, item["id"]
    items.sort(key=contact_sort_key)
    username = config.bot_username or ""
    valid_username = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username))
    actions = {key: f"https://t.me/{username}?start=ownercab_{key}" for key in
               ("profile", "register", "readiness", "number", "contact", "photo", "tea")} if valid_username else {}
    for item in items:
        reference = item.pop("_action_ref")
        item["actions"] = {key: f"{actions[key]}_{reference}"
                           for key in ("contact", "photo", "tea")} if actions else {}
    return JSONResponse({
        "user": {"id": user.id, "name": user.full_name}, "profile": profile,
        "avatar": avatar.image if avatar else "",
        "today": today.isoformat(), "work_date": selected.isoformat(), "active_only": active_only,
        "tomorrow": (today + timedelta(days=1)).isoformat(),
        "readiness": _owner_cabinet_readiness(db, employee, selected),
        "tomorrow_readiness": _owner_cabinet_readiness(db, employee, today + timedelta(days=1)),
        "orders": [_cabinet_order_dict(order, db) for order in orders], "number_cards": items,
        "actions": actions,
        "cabinet_kind": cabinet_kind(db, user.id), "can_record_cash": cabinet_can_cash(db, user.id),
    }, headers={"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache"})


def _cabinet_order_dict(order, db):
    # Assigned order details are read-only; never expose accounting or admin actions.
    original = _order_dict(order, db)
    keys = ("id", "source", "source_label", "work_date", "issue_time", "arrival_time",
            "deceased_name", "route", "category_label", "team_size", "status_label",
            "agent_name", "agent_phone", "other_agent_phone", "contact_time")
    result = {key: original.get(key) for key in keys}
    result["members"] = [{k: p.get(k) for k in ("display_name", "phone", "telegram_url")}
                         for p in original.get("members", [])]
    return result


@app.get("/api/owner/brigadier-cabinet/orders/{order_id}")
def cabinet_assigned_order(order_id: int, db: Session = Depends(get_db),
                           user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, "Кабинет недоступен")
    order = db.scalar(select(Order).join(Assignment, Assignment.order_id == Order.id).join(
        Employee, Employee.id == Assignment.employee_id).where(Order.id == order_id,
        Order.status != "cancelled", Assignment.role == "brigadier", Employee.tg_id == user.id,
        Employee.active.is_(True), _visible_employee()))
    if not order:
        raise HTTPException(404, "Заказ удалён или передан другому бригадиру")
    return JSONResponse(_cabinet_order_dict(order, db), headers={"Cache-Control": "private, no-store"})


def _owner_brigadier_cabinet_html(html):
    def replace_once(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError("Owner cabinet UI anchor missing or ambiguous: " + old[:80])
        html = html.replace(old, new, 1)

    replace_once('<button data-view="controlView">', '<button id="ownerCabinetNav" data-view="controlView">')
    replace_once('<section id="controlView" class="view">',
                 '<section id="ownerCabinetView" class="view" aria-label="Личный кабинет бригадира"><div id="ownerCabinetContent"></div></section>\n<section id="controlView" class="view">')
    replace_once("async function renderCurrentView() {\n  const v = currentViewId();", """async function renderCurrentView() {
  updateOwnerCabinetNav();
  const v = currentViewId();
  const personal=v==='ownerCabinetView'&&state.bootstrap?.can_use_owner_cabinet;
  document.body.classList.toggle('owner-cabinet-open',!!personal);
  qs('.topbar h1').textContent=personal?'Кабинет бригадира':'Диспетчерская';
  if(personal)return loadOwnerCabinet();
  if(qs('#cabinetAvatarForm')){closeModal();qs('#cabinetAvatarForm').remove()}
  ownerCabinetRequest++;
  ownerCabinetData=null;
  ownerCabinetSelectedCard=null;
  qs('#ownerCabinetContent').innerHTML='';""")
    replace_once('// Events', r'''
let ownerCabinetRequest=0,ownerCabinetData=null,ownerCabinetSignature='',ownerCabinetSelectedCard=null;
function cabinetIcon(name){
  const paths={trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',user:'<circle cx="12" cy="7" r="3.5"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',edit:'<path d="m4 15 11-11 5 5L9 20H4zM13 6l5 5"/>',lock:'<rect x="5" y="10" width="14" height="11" rx="3"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',camera:'<path d="M4 6h4l2-3h4l2 3h4v14H4z"/><circle cx="12" cy="13" r="4"/>',check:'<path d="m5 12 4 4L19 6"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',phone:'<path d="M5 3H3c0 10 8 18 18 18v-4l-5-2-2 2-7-7 2-2-2-5z"/>',chevron:'<path d="m9 5 7 7-7 7"/>',back:'<path d="m14 5-7 7 7 7"/>',plus:'<path d="M12 5v14M5 12h14"/>',calendar:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/>',wallet:'<path d="M20 7H5a2 2 0 0 1 0-4h13v4M4 7v14h16V7"/><path d="M15 12h5v5h-5z"/>',control:'<circle cx="12" cy="12" r="9"/><path d="M12 7v6m0 3v1"/>',team:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M17 5a3 3 0 0 1 0 6M18 15a4 4 0 0 1 3 4v2"/>',tea:'<path d="M4 7h12v7a6 6 0 0 1-12 0zM16 8h2a3 3 0 0 1 0 6h-2M4 22h12M7 2v2M12 2v2"/>'};
  return `<svg class="cabinet-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths[name]||paths.chevron}</svg>`;
}
function updateOwnerCabinetNav(){
  const nav=qs('#ownerCabinetNav'),allowed=!!state.bootstrap?.can_use_owner_cabinet;
  const next=allowed?'ownerCabinetView':'controlView';
  if(ownerCabinetData&&ownerCabinetData.user.id!==state.bootstrap?.user?.id){if(qs('#cabinetNativeForm,#cabinetAvatarForm')){closeModal();qs('#cabinetNativeForm')?.remove();qs('#cabinetAvatarForm')?.remove()}ownerCabinetData=null;ownerCabinetSignature=''}
  if(nav.dataset.view===next)return;
  const wasActive=nav.classList.contains('active');
  nav.dataset.view=next;
  nav.innerHTML=allowed?`<span aria-hidden="true">${cabinetIcon('user')}</span><small>Кабинет</small>`:'<span aria-hidden="true">!</span><small>Контроль</small>';
  nav.setAttribute('aria-label',allowed?'Мой кабинет бригадира':'Контроль');
  if(wasActive){qsa('.view').forEach(x=>x.classList.remove('active'));qs('#'+next).classList.add('active')}
  if(!allowed&&(qs('#cabinetDirectory')||qs('#cabinetNativeForm')||qs('#cabinetAvatarForm'))){closeModal();qs('#cabinetDirectory')?.remove();qs('#cabinetNativeForm')?.remove();qs('#cabinetAvatarForm')?.remove()}
  if(!allowed){ownerCabinetRequest++;ownerCabinetData=null;ownerCabinetSignature='';ownerCabinetSelectedCard=null;qs('#ownerCabinetContent').innerHTML=''}
}
async function openCabinetEmployees(){
  if(!state.bootstrap?.can_use_owner_cabinet||currentViewId()!=='ownerCabinetView')return;
  const uid=state.bootstrap.user.id;
  showModal('Сотрудники','<div id="cabinetDirectory"><p role="status">Загрузка сотрудников…</p></div>');
  const root=qs('#cabinetDirectory');
  const active=()=>state.bootstrap?.can_use_owner_cabinet&&state.bootstrap?.user?.id===uid&&currentViewId()==='ownerCabinetView'&&qs('#cabinetDirectory')===root&&!qs('#modal').classList.contains('hidden');
  try{
    const data=await api('/api/owner/brigadier-cabinet/employees',{cache:'no-store'});
    if(!active())return;
    const groups=[['brigadier','Бригадиры'],['main','Основа'],['cashless','Безнал'],['reserve','Резерв']];
    const norm=s=>String(s||'').toLocaleLowerCase('ru').replace(/ё/g,'е').trim();
    const personLabel=p=>[String(p.full_name||'').trim().split(/\s+/)[0]||'Без имени',String(p.metro||'').trim(),p.height_cm?String(p.height_cm):''].filter(Boolean).join(' ');
    const people=(data.items||[]).slice().sort((a,b)=>personLabel(a).localeCompare(personLabel(b),'ru')||Number(a.id)-Number(b.id));
    root.innerHTML=`<label class="cabinet-directory-search">Поиск по имени и метро<input id="cabinetStaffSearch" type="search" placeholder="Имя или метро" autocomplete="off"></label><label class="cabinet-directory-filter">Раздел<select id="cabinetStaffGroup"><option value="all">Все сотрудники</option>${groups.map(([code,label])=>`<option value="${code}">${label}</option>`).join('')}</select></label><p id="cabinetStaffCount" class="muted" role="status"></p><div id="cabinetStaffList"></div>`;
    function personHtml(p,number){
      let phone=String(p.phone||'').replace(/\D/g,'');
      if(phone.length===11&&phone.startsWith('8'))phone='7'+phone.slice(1);
      const phoneLink=/^\d{7,15}$/.test(phone)?`<a href="tel:+${phone}">${cabinetIcon('phone')}${esc(p.phone)}</a>`:'<span>Телефон не указан</span>';
      const username=String(p.telegram_username||'').replace(/^@/,'');
      const tgUrl=/^[A-Za-z0-9_]{5,32}$/.test(username)?`https://t.me/${username}`:(/^tg:\/\/user\?id=[1-9]\d*$/.test(p.telegram_url||'')?p.telegram_url:'');
      return `<details class="cabinet-directory-person"><summary><span><b><span class="cabinet-directory-number">${number}.</span> ${esc(personLabel(p))}${p.has_car?' 🚗':''}</b></span>${cabinetIcon('chevron')}</summary><div class="cabinet-directory-contacts">${phoneLink}${tgUrl?`<a href="${esc(tgUrl)}" data-staff-telegram>Telegram${username?' · @'+esc(username):''}</a>`:'<span>Telegram не указан — связь по телефону</span>'}</div></details>`;
    }
    function render(){
      if(!active())return;
      const words=norm(qs('#cabinetStaffSearch',root).value).split(/\s+/).filter(Boolean),group=qs('#cabinetStaffGroup',root).value;
      const rows=people.filter(p=>(group==='all'||p.group_code===group)&&words.every(word=>norm(`${p.full_name} ${p.metro||''}`).includes(word)));
      qs('#cabinetStaffCount',root).textContent=`Найдено: ${rows.length} из ${people.length}`;
      qs('#cabinetStaffList',root).innerHTML=rows.length?groups.map(([code,label])=>{
        const members=rows.filter(p=>p.group_code===code);
        return members.length?`<section class="cabinet-directory-group"><h4>${label} · ${members.length}</h4><div class="cabinet-directory-grid">${members.map((person,index)=>personHtml(person,Math.floor(index/2)+1+(index%2?Math.ceil(members.length/2):0))).join('')}</div></section>`:'';
      }).join(''):'<p class="cabinet-empty">Сотрудники не найдены. Попробуйте другое имя или метро.</p>';
      qsa('[data-staff-telegram]',root).forEach(link=>link.onclick=e=>{
        if(!active()){e.preventDefault();return}
        if(link.href.startsWith('https://t.me/')&&tg?.openTelegramLink){e.preventDefault();tg.openTelegramLink(link.href)}
      });
    }
    qs('#cabinetStaffSearch',root).oninput=render;
    qs('#cabinetStaffGroup',root).onchange=render;
    render();
  }catch(e){if(active()){root.innerHTML=`<p role="alert">${esc(e.message)}</p><button type="button" class="cabinet-secondary" id="cabinetStaffRetry">Повторить</button>`;qs('#cabinetStaffRetry',root).onclick=openCabinetEmployees}}
}
async function cabinetResumePending(){
  const uid=state.bootstrap?.user?.id,id=sessionStorage.getItem('cabinet-pending:'+uid);
  if(!id||!state.bootstrap?.can_use_owner_cabinet)return;
  try{
    const r=await api('/api/owner/brigadier-cabinet/actions/'+id,{cache:'no-store'});
    if(state.bootstrap?.user?.id!==uid)return;
    if(r.status==='done'||r.status==='error'){
      cabinetForgetPending(uid,id);ownerCabinetSignature='';
      if(r.status==='done'&&r.completed)ownerCabinetSelectedCard=null;
      try{
        if(r.status==='done'&&!r.deleted&&r.work_date&&r.work_date!==state.selectedDate)await loadBootstrap(r.work_date);
        await loadOwnerCabinet();
      }catch(e){}
      cabinetActionNotice(r.status==='done'?(r.message||'✅ Действие выполнено'):'⚠️ '+(r.error||'Не удалось подтвердить результат'),r.status==='done'?'success':'error');
    }else toast('⏳ Запрос принят. Бот обрабатывает действие — повторно отправлять не нужно.',7000);
  }catch(e){toast(e.status===404?'⏳ Запрос пока не найден. Если форма ещё открыта, нажмите «Проверить отправку». Не создавайте новую отправку.':cabinetHumanApiError(e).message,8000)}
}
async function cabinetImage(file){
  if(!file)throw new Error('Выберите фото');
  if(file.size>30*1024*1024)throw new Error('Фото больше 30 МБ. Выберите уменьшенную копию');
  const url=URL.createObjectURL(file),img=new Image();
  try{
    img.src=url;await img.decode();
    const scale=Math.min(1,1600/Math.max(img.width,img.height)),canvas=document.createElement('canvas');
    canvas.width=Math.max(1,Math.round(img.width*scale));canvas.height=Math.max(1,Math.round(img.height*scale));
    canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);
    return canvas.toDataURL('image/jpeg',.86).split(',')[1];
  }catch(e){throw new Error('Не удалось открыть фото. Выберите JPEG или PNG из галереи')}
  finally{URL.revokeObjectURL(url)}
}
function cabinetAvatarSource(data){
  const value=String(data?.avatar||'');
  return value.length<=350000&&/^data:image\/jpeg;base64,[A-Za-z0-9+/]+={0,2}$/.test(value)?value:'';
}
async function cabinetAvatarImage(file){
  if(!file)throw new Error('Выберите фото');
  if(file.size>30*1024*1024)throw new Error('Фото больше 30 МБ. Выберите уменьшенную копию');
  const url=URL.createObjectURL(file),img=new Image();
  try{
    img.src=url;await img.decode();
    const side=Math.min(img.naturalWidth,img.naturalHeight),canvas=document.createElement('canvas');
    if(!side)throw new Error('empty image');
    canvas.width=canvas.height=512;
    const context=canvas.getContext('2d');
    context.fillStyle='#141414';context.fillRect(0,0,512,512);
    context.drawImage(img,(img.naturalWidth-side)/2,(img.naturalHeight-side)/2,side,side,0,0,512,512);
    let result=canvas.toDataURL('image/jpeg',.88);
    if(result.length>340000)result=canvas.toDataURL('image/jpeg',.7);
    if(result.length>340000)throw new Error('large image');
    return result;
  }catch(e){throw new Error('Не удалось открыть фото. Выберите JPEG или PNG из галереи')}
  finally{URL.revokeObjectURL(url)}
}
function openCabinetAvatar(){
  if(!state.bootstrap?.can_use_owner_cabinet||currentViewId()!=='ownerCabinetView'||!ownerCabinetData)return;
  const uid=state.bootstrap.user.id,current=cabinetAvatarSource(ownerCabinetData);
  showModal('Фото профиля',`<form id="cabinetAvatarForm" class="cabinet-native-form"><div class="cabinet-avatar-preview">${current?`<img src="${esc(current)}" alt="Фото профиля">`:cabinetIcon('camera')}</div><p class="cabinet-avatar-note">Выберите личное фото. В профиле оно будет круглым.</p><label class="cabinet-avatar-picker">${cabinetIcon('camera')}<span>${current?'Выбрать другое фото':'Выбрать фото'}</span><input id="cabinetAvatarFile" type="file" accept="image/*" aria-label="Выбрать личное фото"></label><p id="cabinetAvatarStatus" role="status" aria-live="polite"></p><button type="submit" class="cabinet-primary" disabled>Сохранить фото</button>${current?'<button type="button" id="cabinetAvatarRemove" class="cabinet-secondary">Удалить фото</button>':''}</form>`);
  const form=qs('#cabinetAvatarForm'),input=qs('#cabinetAvatarFile',form),save=qs('[type=submit]',form),remove=qs('#cabinetAvatarRemove',form),status=qs('#cabinetAvatarStatus',form);
  const active=()=>state.bootstrap?.can_use_owner_cabinet&&state.bootstrap?.user?.id===uid&&currentViewId()==='ownerCabinetView'&&qs('#cabinetAvatarForm')===form&&!qs('#modal').classList.contains('hidden');
  let selected='',busy=false,selection=0;
  const lock=value=>{busy=value;input.disabled=value;save.disabled=value||!selected;if(remove)remove.disabled=value};
  input.onchange=async()=>{
    if(!active()||busy||!input.files[0])return;
    const turn=++selection;selected='';lock(true);status.textContent='Подготавливаем фото…';
    try{
      const image=await cabinetAvatarImage(input.files[0]);
      if(!active()||turn!==selection)return;
      selected=image;qs('.cabinet-avatar-preview',form).innerHTML=`<img src="${esc(image)}" alt="Предпросмотр личного фото">`;
      status.textContent='Фото готово к сохранению';
    }catch(e){if(active())status.textContent=e.message}
    finally{input.value='';lock(false)}
  };
  const persist=async deleting=>{
    if(!active()||busy||(!deleting&&!selected))return;
    lock(true);status.textContent=deleting?'Удаляем фото…':'Сохраняем фото…';
    try{
      await api('/api/owner/brigadier-cabinet/avatar',deleting?{method:'DELETE'}:{method:'PUT',body:JSON.stringify({image:selected})});
      if(!active())return;
      closeModal();form.remove();ownerCabinetSignature='';await loadOwnerCabinet();
      toast(deleting?'Фото удалено':'Фото сохранено');
    }catch(e){if(active())status.textContent=e.message}
    finally{lock(false)}
  };
  form.onsubmit=e=>{e.preventDefault();return persist(false)};
  if(remove)remove.onclick=()=>persist(true);
}
function cabinetPhotoFrameFields(){
 return `<div class="cabinet-photo-reminder" role="note"><strong>❗ Наденьте повязки и шевроны</strong><span>Проверьте, чтобы они были хорошо видны на фото.</span></div><label data-photo-file-label>Фото<input name="photo" type="file" accept="image/*" required></label><section id="cabinetFrameEdit" hidden><p><b>Поместите всю бригаду в рамку</b><br>Все сотрудники целиком: от головы до обуви.</p><div class="cabinet-photo-frame"><canvas id="cabinetFrameCanvas" width="1200" height="800" tabindex="0" aria-label="Горизонтальная рамка: двигайте фото пальцем, меняйте масштаб двумя пальцами или ползунком"></canvas></div><div class="cabinet-frame-tools"><button type="button" data-frame-less aria-label="Уменьшить фото">−</button><input type="range" data-frame-zoom min="1" max="5" step="0.01" value="1" aria-label="Масштаб фото"><button type="button" data-frame-more aria-label="Увеличить фото">+</button><button type="button" data-frame-reset>Сбросить</button></div><small>Двигайте фото пальцем. Масштаб — двумя пальцами или ползунком.</small><button type="button" class="cabinet-primary cabinet-wide" data-frame-review>Далее — проверить фото</button></section><section id="cabinetFrameReview" hidden aria-label="Проверка фото перед отправкой"><h4 tabindex="-1">Фото готово к отправке?</h4><img id="cabinetFrameResult" alt="Итоговое фото, которое будет отправлено"><p><b>Вся бригада в полный рост, повязки и шевроны видны?</b></p><button type="button" class="cabinet-secondary cabinet-wide" data-frame-edit>✏️ Изменить</button></section>`;
}
function cabinetFramePlacement(iw,ih,zoom,cx,cy){
 const fit=Math.min(1200/iw,800/ih),w=iw*fit*zoom,h=ih*fit*zoom;
 const clamp=(v,low,high)=>Math.max(low,Math.min(high,v));
 const x=w<=1200?600:clamp(cx,1200-w/2,w/2),y=h<=800?400:clamp(cy,800-h/2,h/2);
 return {x,y,w,h,scale:fit*zoom};
}
function setupCabinetPhotoFrame(form,notify,metadataReady){
 const file=form.querySelector('[name=photo]'),edit=form.querySelector('#cabinetFrameEdit'),review=form.querySelector('#cabinetFrameReview'),canvas=form.querySelector('#cabinetFrameCanvas'),result=form.querySelector('#cabinetFrameResult'),slider=form.querySelector('[data-frame-zoom]'),submit=form.querySelector('[type=submit]');
 const caption=form.querySelector('[data-photo-caption-fields]'),fileLabel=form.querySelector('[data-photo-file-label]');
 let image=null,source='',jpeg='',phase='empty',zoom=1,cx=600,cy=400,version=0,locked=false,gate=false,gateTimer=null,gesture=null,lastGesture=0;
 const pointers=new Map(),controls=[...form.querySelectorAll('[data-frame-less],[data-frame-more],[data-frame-reset],[data-frame-review],[data-frame-edit],[data-frame-zoom]')];
 const draw=()=>{
  if(!image)return;
  const p=cabinetFramePlacement(image.naturalWidth,image.naturalHeight,zoom,cx,cy);cx=p.x;cy=p.y;
  const ctx=canvas.getContext('2d');ctx.fillStyle='#171717';ctx.fillRect(0,0,1200,800);ctx.drawImage(image,cx-p.w/2,cy-p.h/2,p.w,p.h);slider.value=String(zoom);
 };
 const refresh=()=>{
  edit.hidden=phase!=='edit';review.hidden=phase!=='review';submit.hidden=phase!=='review';
  if(caption)caption.hidden=phase==='review';fileLabel.hidden=phase==='review';
  controls.forEach(c=>c.disabled=locked);notify();
 };
 const clearGate=()=>{clearTimeout(gateTimer);gate=false};
 const change=()=>{jpeg='';clearGate();phase='edit';draw();refresh()};
 const zoomAt=(value,x=600,y=400)=>{
  if(!image||locked||phase!=='edit')return;
  const next=Math.max(1,Math.min(5,value)),factor=next/zoom;
  cx=x+(cx-x)*factor;cy=y+(cy-y)*factor;zoom=next;change();
 };
 const point=event=>{const r=canvas.getBoundingClientRect();return {x:(event.clientX-r.left)*1200/r.width,y:(event.clientY-r.top)*800/r.height}};
 const startGesture=()=>{
  const pts=[...pointers.values()];
  if(pts.length===1)gesture={x:pts[0].x,y:pts[0].y,cx,cy,zoom};
  else if(pts.length===2)gesture={x:(pts[0].x+pts[1].x)/2,y:(pts[0].y+pts[1].y)/2,cx,cy,zoom,distance:Math.max(1,Math.hypot(pts[1].x-pts[0].x,pts[1].y-pts[0].y))};
  else gesture=null;
 };
 canvas.addEventListener('pointerdown',event=>{if(phase!=='edit'||locked||!image)return;event.preventDefault();canvas.setPointerCapture?.(event.pointerId);pointers.set(event.pointerId,point(event));startGesture()});
 canvas.addEventListener('pointermove',event=>{
  if(!pointers.has(event.pointerId)||!gesture||locked||phase!=='edit')return;
  event.preventDefault();pointers.set(event.pointerId,point(event));const pts=[...pointers.values()];
  if(pts.length===1){cx=gesture.cx+pts[0].x-gesture.x;cy=gesture.cy+pts[0].y-gesture.y}
  else if(pts.length===2&&gesture.distance){const x=(pts[0].x+pts[1].x)/2,y=(pts[0].y+pts[1].y)/2;zoom=Math.max(1,Math.min(5,gesture.zoom*Math.hypot(pts[1].x-pts[0].x,pts[1].y-pts[0].y)/gesture.distance));const f=zoom/gesture.zoom;cx=x+(gesture.cx-gesture.x)*f;cy=y+(gesture.cy-gesture.y)*f}
  lastGesture=Date.now();change();
 });
 const end=event=>{pointers.delete(event.pointerId);startGesture();lastGesture=Date.now()};
 for(const name of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(name,end);
 slider.oninput=()=>zoomAt(Number(slider.value));form.querySelector('[data-frame-less]').onclick=()=>zoomAt(zoom-.1);form.querySelector('[data-frame-more]').onclick=()=>zoomAt(zoom+.1);
 form.querySelector('[data-frame-reset]').onclick=()=>{if(locked)return;zoom=1;cx=600;cy=400;change()};
 canvas.addEventListener('keydown',event=>{if(locked||phase!=='edit')return;const delta={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]}[event.key];if(delta){event.preventDefault();cx+=delta[0];cy+=delta[1];change()}});
 file.onchange=async()=>{
  if(locked)return;const turn=++version;clearGate();phase='empty';source='';jpeg='';image=null;pointers.clear();refresh();
  if(!file.files[0])return;
  try{const encoded=await cabinetImage(file.files[0]);const loaded=new Image();loaded.src='data:image/jpeg;base64,'+encoded;await loaded.decode();if(turn!==version||!form.isConnected)return;image=loaded;source=encoded;zoom=1;cx=600;cy=400;phase='edit';draw();refresh()}
  catch(error){if(turn===version&&form.isConnected){form.querySelector('#cabinetActionStatus').textContent=error.message;refresh()}}
 };
 form.querySelector('[data-frame-review]').onclick=()=>{
  if(locked||!image||phase!=='edit'||pointers.size||Date.now()-lastGesture<350)return;
  if(!metadataReady())return;
  draw();jpeg=canvas.toDataURL('image/jpeg',.92).split(',')[1];
  if(source.length+jpeg.length>5400000){form.querySelector('#cabinetActionStatus').textContent='Фото слишком большое. Выберите уменьшенную копию.';return}
  result.src='data:image/jpeg;base64,'+jpeg;phase='review';gate=false;refresh();review.querySelector('h4').focus({preventScroll:true});review.scrollIntoView?.({behavior:'smooth',block:'nearest'});
  // A double tap on the review button must never become a send confirmation.
  gateTimer=setTimeout(()=>{if(form.isConnected&&phase==='review'&&!locked){gate=true;notify()}},700);
 };
 form.querySelector('[data-frame-edit]').onclick=()=>{if(locked)return;phase='edit';clearGate();refresh();draw()};
 submit.hidden=true;
 return {get ready(){return phase==='review'&&gate&&!!jpeg},get image(){return jpeg},get original(){return source},lock(value){locked=value;controls.forEach(c=>c.disabled=value)},get phase(){return phase}};
}

async function ownerCabinetAction(key,cardId=null){
  if(!state.bootstrap?.can_use_owner_cabinet||currentViewId()!=='ownerCabinetView')return;
  let data=ownerCabinetData;const uid=data?.user.id;
  const card=cardId===null?null:data?.number_cards?.find(c=>Number(c.id)===Number(cardId));
  if(cardId!==null&&!card)return;
  if(card)data={...data,work_date:card.work_date};
  const prepared=card&&(card.prepared??card.composition_closed);
  if(card&&(key==='delete'||key==='number')&&!card.can_delete){toast(card.delete_reason||'Удаление доступно до первого фото по заказу');return}
  if(key==='photo'&&(!prepared||data.work_date!==data.today||(data.can_record_cash!==false&&card.source==='private'&&!card.cash))){toast('Фото доступно в день заказа после выполнения предыдущих этапов.');return}
  if(key==='cash'&&card?.work_date>data.today){toast('Касса доступна начиная со дня заказа');return}
  if(key==='cash'&&!card?.on_contact){toast('Сначала нажмите «Бригада на связи» по этому заказу');return}
  if(key==='cash'&&data.can_record_cash===false){toast('В вашем кабинете ведение кассы отключено');return}
  if(key==='cash'&&(!prepared||card.source!=='private')){toast('Сначала отправьте отчёт о закрытии заказа');return}
  if(key==='tea'&&(!prepared||!cabinetPhotoInfo(card).complete)){toast('Сначала отправьте все фото по заказу');return} 
  const input=(name,label,value='',type='text',extra='')=>{
    const clock=type==='time',shown=clock?(normalizeCabinetClock(value)||String(value??'')):value;
    return `<label${clock?' class="cabinet-clock-label"':''}>${clock?'🕒 ':''}${label}<input name="${name}" type="${clock?'text':type}" value="${esc(shown??'')}" ${clock?'data-cabinet-clock inputmode="numeric" placeholder="Например, 06:30" maxlength="5" autocomplete="off" aria-describedby="cabinetClockHelp"':''} ${extra}>${clock?'<small class="cabinet-clock-help">Нажмите на поле и введите время, например 06:30 или 0630</small>':''}</label>`;
  };
  const source=value=>`<label>Тип заказа<select name="source" ${key==='edit'&&(card?.first_received||card?.cash)?'disabled':''}><option value="private" ${value==='private'?'selected':''}>Частный</option><option value="gbu" ${value==='gbu'?'selected':''}>ГБУ</option></select></label>`;
  let title='',fields='',submit='Сохранить';
  if(key==='number'){
    title=card?'Заменить заказ':'Новый заказ на завтра';submit=card?'Заменить заказ и отправить номер':'Отправить номер в группу';
    fields=input('surname','Фамилия умершего','','text','required maxlength="100" autocomplete="off"')+source('private')+`<p class="cabinet-number-date">Заказ на завтра · ${esc(fmtDate(data.tomorrow))}</p>${card?`<p>Прежний заказ «${esc(card.surname)}» и его номер в группе будут удалены при отправке нового.</p>`:''}`+`<p>В группу будет отправлен ваш номер: ${esc(data.profile?.phone||'укажите телефон в профиле')}.</p>`;
  }else if(key==='delete'){
    title='Удалить заказ';submit='Удалить заказ и номер';
    fields='<p>Удалить эту карточку полностью?</p><p>Удаление и замена доступны до первого фото по заказу. Бот уберёт номер из группы, а кассу по удалённому заказу — из таблицы.</p>';
  }else if(key==='edit'){
    title='Изменить заказ';fields=input('surname','Фамилия умершего',card.surname,'text','required maxlength="100"')+source(card.source)+input('photo_time','Время фото',card.photo_time,'time')+((card.prepared??card.composition_closed)?input('contact_time','Время связи с бригадой',card.contact_time,'time'):'')+'<p>Изменится существующая карточка. После сохранения кассы или первого фото тип заказа менять нельзя.</p>';
  }else if(key==='schedule'){
    title='Время фото';fields=input('photo_time',card.source==='gbu'?'Время первого фото':'Время фото',card.photo_time,'time','required');
  }else if(key==='close'){
    title='Отчёт о закрытии заказа';submit='Подтвердить закрытие';
    fields='<label>Связь с агентом<select name="agent_status"><option value="yes">Связался с агентом</option><option value="sms">Написал СМС — агент не на связи</option><option value="missing">Номер агента не указан</option></select></label>'+input('contact_time','Во сколько бригада выйдет на связь',card.contact_time,'time','required')+`<p>Фото запланировано на ${esc(card.photo_time)}. ${card.source==='private'?'Далее заполните кассу.':'Далее — фото в день заказа.'} Сотрудников назначает диспетчер.</p>`;
  }else if(key==='cash'){
    const cash=card.cash||{};title=cash.version?'Изменить кассу':'Касса по заказу';submit='Сохранить кассу';
    fields=`<div class="cabinet-form-pair"><label>Категория<select name="category"><option value="standard" ${cash.category==='standard'?'selected':''}>Стандарт</option><option value="vip" ${cash.category==='vip'?'selected':''}>Вип</option><option value="elite" ${cash.category==='elite'?'selected':''}>Элит</option></select></label><label>Бригада<select name="team_size"><option value="4" ${cash.team_size===4?'selected':''}>4 человека</option><option value="6" ${cash.team_size===6?'selected':''}>6 человек</option></select></label></div>`+
      input('commission','Комиссия, ₽',cash.commission??'','number','required min="0" max="10000000" step="1" inputmode="numeric"')+
      input('kickback','Откат, ₽',cash.kickback??0,'number','required min="0" max="10000000" step="1" inputmode="numeric"')+
      input('reserve','Сотрудников из резерва',cash.reserve??0,'number','required min="0" max="6" step="1" inputmode="numeric"')+
      `<div class="cabinet-cash-result"><span>В личную кассу</span><strong id="cabinetCashPreview">${cabinetMoney(cash.commission??0)} ₽</strong></div><p>Откат учитывается отдельно. После сохранения кассы откроется фото в день заказа.</p>`;
  }else if(key==='photo'){
    title=card.source==='gbu'?(card.first_received?'Второе фото · конечная точка':'Первое фото'):'Фото по заказу';submit='✅ Подтвердить и отправить';
    const captionFields=card.source==='private'?`<p class="cabinet-photo-surname">${esc(String(card.surname||'').toUpperCase())}</p><label>Комментарий к фото<textarea name="comment" required maxlength="500" rows="2" placeholder="Название морга, во сколько быть в морге"></textarea></label><p>Фамилия будет первой строкой подписи к фото.</p>`:(!card.first_received?`<p class="cabinet-photo-surname">${esc(String(card.surname||'').toUpperCase())}</p><p>Фамилия автоматически добавится в подпись к фото.</p>`:'');
    fields='<div data-photo-caption-fields>'+captionFields+'</div>'+cabinetPhotoFrameFields()+(card.source==='gbu'&&card.first_received?'<p>Второе фото сохранится. Два фото и итоговый отчёт появятся в группе после всех первых фото ГБУ.</p>':'');
  }else if(key==='tea'){
    title='Итоговый отчёт';submit=card.tea_received?'Изменить отчёт':'Завершить заказ';
    fields=input('amount','Чай по заказу, ₽',card.tea_amount??'','number','required min="0" max="10000000" step="1" inputmode="numeric"')+'<p>Если чая не было, укажите 0. После сохранения заказ будет отмечен завершённым ✅.</p>';
  }else if(key==='contact'){
    title='Бригада на связи';submit='Подтвердить связь';fields=`<p>Подтвердить связь по заказу «${esc(card.surname)}»?</p>`;
  }else if(key==='profile'||key==='register'){
    title='Мой профиль';const p=data.profile||{};
    fields=input('name','Имя и фамилия',p.full_name,'text','required maxlength="100"')+input('phone','Телефон',p.phone,'tel','required')+input('metro','Метро',p.metro,'text','required maxlength="100"')+input('height','Рост, см',p.height_cm,'number','required min="100" max="230"');
  }else if(key==='readiness'){
    title='Готовность на завтра';submit='Сохранить готовность';
    fields=`<p>${esc(fmtDate(data.tomorrow))}</p><label>Статус<select name="status"><option value="ready">Готов</option><option value="car">Готов на авто</option><option value="main">На основной</option><option value="off">Выходной согласован</option></select></label><label>Комментарий<textarea name="comment" maxlength="500" rows="3" placeholder="Например: готов после 15:00"></textarea></label>`;
  }else return;
  const formHtml=`<form id="cabinetNativeForm" class="cabinet-native-form">${card?`<p class="cabinet-form-order">ум: ${esc(card.surname)} · ${esc(fmtDate(card.work_date))}</p>`:''}${fields}<p id="cabinetActionStatus" role="status" aria-live="polite"></p><button class="cabinet-primary${key==='delete'?' cabinet-danger':''}" type="submit">${submit}</button></form>`;
  const inline=['number','delete','edit','schedule','close','cash','photo','tea','contact'].includes(key);
  if(inline){
    const root=qs('#ownerCabinetContent');
    if(card&&key!=='number'){ownerCabinetSelectedCard=card.id;renderOwnerCabinet(data)}
    else{ownerCabinetSelectedCard=null;root.dataset.cabinetView='new';root.innerHTML=`<button type="button" class="cabinet-back" id="cabinetBack">${cabinetIcon('back')}Мои заказы</button><article class="cabinet-order"><div class="cabinet-kind">Новый заказ · шаг 1</div><div data-stage-host="number"></div></article>`}
    const host=qs(`[data-stage-host="${key}"]`,root)||qs('[data-stage-host="edit"]',root);
    host.innerHTML=`<h3>${esc(title)}</h3>${formHtml}${key==='delete'?`<button type="button" id="cabinetReplaceOrder" class="cabinet-secondary cabinet-wide">${cabinetIcon('plus')}Заменить заказ</button>`:''}<button type="button" id="cabinetFormCancel" class="cabinet-back">Отмена</button>`;
    host.closest('.cabinet-stage')?.querySelector('[data-cabinet-action]')?.setAttribute('hidden','');
    const cancel=()=>{renderOwnerCabinet(ownerCabinetData||data)};
    qs('#cabinetFormCancel').onclick=cancel;
    if(qs('#cabinetReplaceOrder'))qs('#cabinetReplaceOrder').onclick=()=>ownerCabinetAction('number',card.id);
    if(!card||key==='number')qs('#cabinetBack').onclick=()=>{ownerCabinetSelectedCard=null;cancel()};
    host.scrollIntoView?.({behavior:'smooth',block:'nearest'});
  }else showModal(title,formHtml);
  const form=qs('#cabinetNativeForm'),button=qs('button[type=submit]',form),status=qs('#cabinetActionStatus',form);
  const clockHelp=qs('.cabinet-clock-help',form);if(clockHelp)clockHelp.id='cabinetClockHelp';
  qsa('[data-cabinet-clock]',form).forEach(field=>{
    field.oninput=()=>field.setCustomValidity('');
    field.onblur=()=>{const clock=normalizeCabinetClock(field.value);if(clock)field.value=clock};
  });
  let requestId=crypto.randomUUID(),body=null,posted=false,attempted=false,busy=false,previewUrl=null,confirmedResult=null;
  const notice=(message,kind='pending')=>{status.textContent=message;status.dataset.notice=kind};
  const fileInput=qs('input[type=file]',form);
  const photoCaption=key==='photo'?qs('[name=comment]',form):null;
  let photoEditor=null;
  const photoReady=()=>!fileInput||(!!fileInput.files[0]&&photoEditor?.ready&&(!photoCaption||!!photoCaption.value.trim()));
  if(key==='photo'){
    photoEditor=setupCabinetPhotoFrame(form,()=>{button.disabled=busy||!photoReady()},()=>{if(photoCaption&&!photoCaption.value.trim()){photoCaption.reportValidity();return false}return true});
    button.disabled=!photoReady();
    if(photoCaption)photoCaption.oninput=()=>{
      button.disabled=busy||!photoReady();
    };
  }
  if(key==='cash')qs('[name=commission]',form).oninput=e=>{qs('#cabinetCashPreview').textContent=cabinetMoney(Number(e.target.value)||0)+' ₽'};

  form.onsubmit=async e=>{
    e.preventDefault();if(busy||state.bootstrap?.user?.id!==uid||!state.bootstrap?.can_use_owner_cabinet)return;
    if(!form.isConnected)return;
    if(key==='photo'&&!body&&!photoReady()){notice('Сначала настройте рамку и проверьте итоговое фото.','error');return}
    busy=true;button.disabled=true;
    try{
      if(!body){
        const values=Object.fromEntries(new FormData(form));
        for(const field of qsa('[data-cabinet-clock]',form)){
          if(!field.value.trim()&&!field.required){values[field.name]='';continue}
          const clock=normalizeCabinetClock(field.value);
          if(!clock){field.setCustomValidity('Укажите время от 00:00 до 23:59, например 06:30');field.reportValidity();throw Error('Укажите время в формате ЧЧ:ММ, например 06:30')}
          field.value=clock;values[field.name]=clock;
        }
        if(key==='edit'&&(card.first_received||card.cash))values.source=card.source;
        if(key==='cash'){for(const k of ['team_size','commission','kickback','reserve'])values[k]=Number(values[k]);values.version=card.cash?.version||0;if(values.reserve>values.team_size)throw Error('Резерва не может быть больше состава бригады')}
        if(key==='photo'){
          notice('⏳ Подготавливаем фото…');
          if(photoCaption&&!photoCaption.value.trim())throw Error('Укажите название морга и во сколько быть в морге');
          if(photoCaption)values.comment=photoCaption.value.trim();
          values.image=photoEditor.image;values.original_image=photoEditor.original;values.frame_confirmed=true;delete values.photo;values.ordinal=card.source==='gbu'&&card.first_received?2:1;
        }
        if(key==='tea')values.amount=Number(values.amount);
        if(key==='readiness'||key==='number')values.work_date=data.tomorrow;
        body=JSON.stringify({request_id:requestId,action:key,card_id:cardId,values});
        qsa('input,select,textarea',form).forEach(el=>el.disabled=true);
        photoEditor?.lock(true);
      }
      notice(key==='photo'?'⏳ Отправляем фото… Дождитесь подтверждения.':'⏳ Сохраняем… Дождитесь подтверждения.');
      if(attempted&&!posted){
        // A lost POST response does not mean rejection. Query its identity first.
        try{await api('/api/owner/brigadier-cabinet/actions/'+requestId,{cache:'no-store'});posted=true}
        catch(error){if(error.status!==404)throw error}
      }
      if(!posted){
        cabinetRememberPending(uid,requestId,key,cardId);attempted=true;
        await api('/api/owner/brigadier-cabinet/actions',{method:'POST',body});posted=true;
      }
      for(let i=0;i<60;i++){
        const result=await api('/api/owner/brigadier-cabinet/actions/'+requestId,{cache:'no-store'});
        if(state.bootstrap?.user?.id!==uid||!state.bootstrap?.can_use_owner_cabinet)return;
        if(result.status==='error'){
          cabinetForgetPending(uid,requestId);
          if(!result.delivery_unknown){
            body=null;posted=false;attempted=false;requestId=crypto.randomUUID();
            qsa('input,select,textarea',form).forEach(el=>el.disabled=false);photoEditor?.lock(false);
            if(key==='edit'&&(card.first_received||card.cash))qs('[name=source]',form).disabled=true;
          }
          const failure=new Error(result.error||'Не удалось сохранить');failure.deliveryUnknown=!!result.delivery_unknown;throw failure;
        }
        if(result.status==='done'){
          confirmedResult=result;
          cabinetForgetPending(uid,requestId);
          const values=JSON.parse(body).values;
          const stillHere=form.isConnected&&currentViewId()==='ownerCabinetView';
          if(stillHere){if(inline)form.remove();else closeModal()}
          if(stillHere&&(key==='tea'||result.completed))ownerCabinetSelectedCard=null;
          const resultDate=result.work_date||values.work_date;
          if(resultDate&&key==='number'&&resultDate!==state.selectedDate)await loadBootstrap(resultDate);
          // Delivery has been confirmed; a later refresh failure cannot undo it.
          try{await loadOwnerCabinet()}catch(error){}
          const found=ownerCabinetData?.number_cards?.find(c=>c.report_key===result.report_key);
          if(found&&stillHere&&!found.tea_received&&key!=='tea'&&!result.completed){
            ownerCabinetSelectedCard=found.id;renderOwnerCabinet(ownerCabinetData);
            const next=cabinetNextAction(found,ownerCabinetData);
            if(next&&!result.duplicate&&['number','schedule','close','cash','photo'].includes(key))await ownerCabinetAction(next.key,found.id);
          }
          cabinetActionNotice(result.message||'✅ Действие выполнено','success');return;
        }
        notice('⏳ Запрос принят. Бот обрабатывает действие — повторно отправлять не нужно.');
        await new Promise(resolve=>setTimeout(resolve,1500));
      }
      notice('⏳ Запрос сохранён и ожидает бота. Можно закрыть окно и позже нажать «Проверить последнее действие».');button.textContent=key==='photo'?'Проверить отправку':'Проверить результат';
    }catch(raw){
      if(confirmedResult){cabinetActionNotice(confirmedResult.message||'✅ Действие выполнено','success');return}
      const error=cabinetHumanApiError(raw);
      if(attempted&&!posted&&error.status>=400&&error.status<500&&![408,409,429].includes(error.status)){
        cabinetForgetPending(uid,requestId);body=null;attempted=false;requestId=crypto.randomUUID();
        qsa('input,select,textarea',form).forEach(el=>el.disabled=false);photoEditor?.lock(false);
        if(key==='edit'&&(card.first_received||card.cash))qs('[name=source]',form).disabled=true;
      }
      const uncertain=attempted&&(error.isConnectionError||error.temporary);
      notice(uncertain?(key==='photo'?'⚠️ Связь прервалась. Не удалось проверить отправку фото. Оно могло уже отправиться — не загружайте его повторно. Нажмите «Проверить отправку».':'⚠️ Не удалось проверить результат. Запрос мог быть принят. Нажмите «Проверить результат», не создавая новое действие.'):'⚠️ '+error.message,'error');
      button.textContent=attempted?(key==='photo'?'Проверить отправку':'Проверить результат'):submit;
    }
    finally{busy=false;button.disabled=key==='photo'&&!body&&!photoReady()}
  };
  window.addEventListener('online',()=>{if(form.isConnected&&attempted&&body&&!busy)form.requestSubmit()},{once:true});
}
function cabinetButton(key,label,icon,card=null,primary=false,disabled=false){
  return `<button type="button" class="${primary?'cabinet-primary':'cabinet-secondary'}" data-cabinet-action="${key}"${card?` data-cabinet-card="${Number(card.id)}"`:''}${disabled?' disabled':''}>${cabinetIcon(icon)}<span>${esc(label)}</span></button>`;
}
function cabinetOrderTools(card){
  return `<div class="cabinet-order-tools">${cabinetButton('edit','Изменить','edit',card)}<button type="button" class="cabinet-trash" data-cabinet-action="delete" data-cabinet-card="${Number(card.id)}" title="${esc(card.delete_reason||'Удалить заказ')}" aria-label="Удалить заказ ${esc(card.surname)}"${card.can_delete?'':' disabled'}>${cabinetIcon('trash')}</button></div>`;
}
function ownerCabinetProfileHtml(data){
  const p=data.profile,first=String(p?.full_name||data.user.name||'').trim().split(/\s+/)[0];
  const name=p?[first,p.metro,p.height_cm].filter(Boolean).join(' '):(first||'Ваш профиль');
  const initials=[first,p?.metro].filter(Boolean).map(x=>x.trim()[0]||'').join(''),avatar=cabinetAvatarSource(data);
  return `<article class="cabinet-profile"><button type="button" class="cabinet-avatar" id="cabinetAvatar" aria-label="${avatar?'Изменить личное фото':'Добавить личное фото'}"><span class="cabinet-avatar-face">${avatar?`<img src="${esc(avatar)}" alt="Личное фото бригадира">`:esc(initials||'＋')}</span><span class="cabinet-avatar-camera">${cabinetIcon('camera')}</span></button><div class="cabinet-profile-copy"><h2>${esc(name)}${p?.has_car?' 🚗':''}</h2>${p?'':'<p>Заполните профиль бригадира</p>'}</div><button type="button" class="cabinet-edit-profile" data-cabinet-action="${p?'profile':'register'}" aria-label="${p?'Изменить профиль':'Заполнить профиль'}">${cabinetIcon(p?'edit':'plus')}</button></article>`;
}
function cabinetMoney(value){return Number(value||0).toLocaleString('ru-RU')}
async function openCabinetFinance(onDate=ownerCabinetData?.today,tab='earnings'){
  if(!state.bootstrap?.can_use_owner_cabinet||currentViewId()!=='ownerCabinetView'||!onDate)return;
  const uid=state.bootstrap.user.id,root=qs('#ownerCabinetContent');
  root.dataset.cabinetView='finance';root.innerHTML='<section id="cabinetFinance"><p role="status">Загрузка кассы и заработка…</p></section>';
  const panel=qs('#cabinetFinance'),active=()=>panel.isConnected&&qs('#cabinetFinance')===panel&&state.bootstrap?.user?.id===uid&&state.bootstrap?.can_use_owner_cabinet&&currentViewId()==='ownerCabinetView';
  try{
    const data=await api('/api/owner/brigadier-cabinet/finance?on_date='+encodeURIComponent(onDate),{cache:'no-store'});
    if(!active()||data.user_id!==uid)return;
    const rows=new Map(data.days.map(row=>[row.work_date,{...row,saving:null,error:'',dirty:false}]));
    const draftKey=day=>'cabinet-earnings-draft:'+uid+':'+day;
    for(const row of rows.values()){
      try{const draft=JSON.parse(sessionStorage.getItem(draftKey(row.work_date))||'null');if(draft){row.salary=draft.salary;row.tea=draft.tea;row.version=draft.version;row.dirty=true}}catch{}
    }
    const endDay=Number(data.end.slice(8));
    const halfLabel=half=>half===1?'1–15':`16–${endDay}`;
    panel.innerHTML=`<h2>Касса и заработок</h2><label class="cabinet-finance-month">Месяц<input id="cabinetFinanceMonth" type="month" value="${esc(data.start.slice(0,7))}" aria-label="Месяц кассы и заработка"></label><div id="cabinetFinanceBody"></div>`;
    const amount=value=>value===null||value===''?null:/^\d+$/.test(String(value))&&Number(value)<=10000000?Number(value):NaN;
    const valid=row=>!Number.isNaN(amount(row.salary))&&!Number.isNaN(amount(row.tea));
    function remember(row){sessionStorage.setItem(draftKey(row.work_date),JSON.stringify({salary:row.salary,tea:row.tea,version:row.version}))}
    function inputValues(row){return {salary:amount(row.salary),tea:amount(row.tea),version:row.version}}
    function updateTotals(){
      if(!active())return;
      const discard=qs('#cabinetDiscardEarnings',panel);if(discard)discard.hidden=![...rows.values()].some(row=>row.dirty||row.error);
      let salary=0,tea=0;
      const halves={1:{salary:0,tea:0},2:{salary:0,tea:0}};
      for(const row of rows.values()){
        const a=amount(row.salary),b=amount(row.tea);salary+=Number.isFinite(a)?a:0;tea+=Number.isFinite(b)?b:0;
        const half=halves[Number(row.work_date.slice(8))<=15?1:2];
        half.salary+=Number.isFinite(a)?a:0;half.tea+=Number.isFinite(b)?b:0;
        const total=qs(`[data-day-total="${row.work_date}"]`,panel);if(total)total.textContent=cabinetMoney((a||0)+(b||0));
      }
      for(const half of [1,2])for(const field of ['salary','tea','total']){
        const cell=qs(`[data-half-${field}="${half}"]`,panel);
        if(cell)cell.textContent=cabinetMoney(field==='total'?halves[half].salary+halves[half].tea:halves[half][field]);
      }
      const a=qs('#cabinetSalaryTotal',panel),b=qs('#cabinetTeaTotal',panel),c=qs('#cabinetEarningsTotal',panel);
      if(a)a.textContent=cabinetMoney(salary);if(b)b.textContent=cabinetMoney(tea);if(c)c.textContent=cabinetMoney(salary+tea);
      const status=qs('#cabinetEarningsStatus',panel);if(status){
        const failed=[...rows.values()].find(row=>row.error),pending=[...rows.values()].some(row=>row.dirty||row.saving);
        status.textContent=failed?failed.error:pending?'Есть изменения. Сохраняем при выходе из поля…':'Все изменения сохранены';
        status.classList.toggle('has-error',!!failed);
      }
    }
    async function saveRow(row){
      if(row.saving)return row.saving;
      if(!row.dirty)return true;
      if(!valid(row)){row.error='Введите целые суммы от 0 до 10 000 000 ₽. Черновик сохранён.';updateTotals();return false}
      row.saving=(async()=>{
        try{
          while(row.dirty){
            if(!active())return false;
            const values=inputValues(row);
            if(!valid(row))throw Error('Проверьте суммы: только целые числа от 0 до 10 000 000 ₽');
            const response=await api('/api/owner/brigadier-cabinet/earnings/'+row.work_date,{method:'PUT',body:JSON.stringify(values)});
            if(!active())return false;
            row.version=response.version;row.error='';
            const latest=inputValues(row);
            row.dirty=latest.salary!==values.salary||latest.tea!==values.tea;
            if(row.dirty)remember(row);else sessionStorage.removeItem(draftKey(row.work_date));
          }
          return true;
        }catch(error){row.error=error.message+' Черновик сохранён в этом окне.';remember(row);return false}
        finally{row.saving=null;updateTotals()}
      })();
      updateTotals();return row.saving;
    }
    async function flush(){
      for(const row of rows.values())if(!(await saveRow(row))){toast('Не все суммы сохранены. Проверьте таблицу заработка.');return false}
      return active();
    }
    panel.cabinetFinanceLeave=flush;
    function renderTab(next){
      if(!active())return;tab=next;panel.dataset.financeTab=tab;qs('h2',panel).textContent=tab==='cash'?'Касса':data.has_allowance?'Заработок Влада':'Заработок';
      qsa('[data-finance-tab]',panel).forEach(b=>{b.classList.toggle('active',b.dataset.financeTab===tab);b.setAttribute('aria-selected',b.dataset.financeTab===tab)});
      const body=qs('#cabinetFinanceBody',panel);
      if(tab==='earnings'){
        const subtotal=day=>{
          const n=Number(day.slice(8));if(n!==15&&n!==endDay)return '';
          const half=n===15?1:2;
          return `<tr class="cabinet-period-total" data-earnings-half="${half}"><th scope="row"><span>${halfLabel(half)}</span><small>Итого</small></th><td data-half-salary="${half}"></td><td data-half-tea="${half}"></td><td data-half-total="${half}"></td></tr>`;
        };
        body.innerHTML=`<p class="cabinet-finance-hint">Оклад и чай — вручную. Итоги за 1–15 и 16–${endDay} — автоматически. Суммы в ₽.</p><div class="cabinet-finance-scroll" role="region" aria-label="Заработок по дням" tabindex="0"><table class="cabinet-earnings-table"><colgroup><col style="width:16%"><col style="width:30%"><col style="width:30%"><col style="width:24%"></colgroup><thead><tr><th scope="col">Дата</th><th scope="col">Оклад</th><th scope="col">Чай</th><th scope="col">Итог</th></tr></thead><tbody>${[...rows.values()].map(row=>`<tr data-earnings-row="${row.work_date}"><th scope="row">${row.work_date.slice(8)}.${row.work_date.slice(5,7)}</th>${['salary','tea'].map(k=>`<td><input type="text" inputmode="numeric" pattern="[0-9]*" data-earnings-day="${row.work_date}" data-field="${k}" value="${esc(row[k]??'')}" placeholder="—" autocomplete="off" aria-label="${k==='salary'?'Оклад':'Личный чай'} за ${row.work_date}" maxlength="8"></td>`).join('')}<td data-day-total="${row.work_date}"></td></tr>${subtotal(row.work_date)}`).join('')}</tbody><tfoot><tr><th scope="row" title="Всего за месяц">Месяц</th><td id="cabinetSalaryTotal"></td><td id="cabinetTeaTotal"></td><td id="cabinetEarningsTotal"></td></tr></tfoot></table></div><p id="cabinetEarningsStatus" role="status" aria-live="polite"></p><button type="button" class="cabinet-secondary cabinet-wide" id="cabinetSaveEarnings">Сохранить изменения</button><button type="button" class="cabinet-back" id="cabinetDiscardEarnings" hidden>Отменить несохранённые правки</button>`;
        qsa('[data-earnings-day]',body).forEach(input=>{
          const row=rows.get(input.dataset.earningsDay);
          input.oninput=()=>{row[input.dataset.field]=input.value;row.dirty=true;row.error='';remember(row);updateTotals()};
          input.onchange=()=>saveRow(row);
        });
        qs('#cabinetSaveEarnings').onclick=()=>flush();
        qs('#cabinetDiscardEarnings').onclick=()=>{if([...rows.values()].some(row=>row.saving)){toast('Дождитесь сохранения');return}for(const row of rows.values())sessionStorage.removeItem(draftKey(row.work_date));openCabinetFinance(onDate,'earnings')};
        updateTotals();
      }else{
        const groups=new Map();for(const entry of data.cash_entries){if(!groups.has(entry.work_date))groups.set(entry.work_date,[]);groups.get(entry.work_date).push(entry)}
        const cashPeriods=new Map(data.period_totals.map(p=>[p.end,p]));
        const entryHtml=entry=>`<article><div class="cabinet-cash-line"><strong>${entry.surname?'ум: '+esc(entry.surname):'Касса из бота'}</strong><b>${cabinetMoney(entry.cash)} ₽</b></div><p>${esc(entry.category_label)} · ${entry.team_size} чел. · Резерв ${entry.reserve}</p><p>Откат: ${cabinetMoney(entry.kickback)} ₽</p>${entry.editable?`<button type="button" class="cabinet-back" data-edit-cash="${entry.event_id}">${cabinetIcon('edit')}Изменить кассу</button>`:entry.source==='bot'?'<small>Прежняя запись из бота</small>':'<small>Запись сохранена в вашей истории</small>'}</article>`;
        const cashRows=data.cash_days.map(row=>{
          const day=row.work_date,entries=groups.get(day)||[],period=cashPeriods.get(day);
          return `<tr data-cash-row="${day}"><th scope="row">${entries.length?`<button type="button" class="cabinet-cash-date" data-cash-day="${day}" aria-expanded="false" aria-controls="cashDay-${day}" aria-label="Показать заказы за ${day}">${day.slice(8)}.${day.slice(5,7)}${cabinetIcon('chevron')}</button>`:`${day.slice(8)}.${day.slice(5,7)}`}</th><td>${cabinetMoney(row.cash)}</td><td>${cabinetMoney(row.kickback)}</td></tr>${entries.length?`<tr id="cashDay-${day}" class="cabinet-cash-details" hidden><td colspan="3">${entries.map(entryHtml).join('')}</td></tr>`:''}${period?`<tr class="cabinet-period-total" data-cash-half="${Number(day.slice(8))===15?1:2}"><th scope="row"><span>${halfLabel(Number(day.slice(8))===15?1:2)}</span><small>Итого</small></th><td>${cabinetMoney(period.cash)}</td><td>${cabinetMoney(period.kickback)}</td></tr>`:''}`;
        }).join('');
        body.innerHTML=`<p class="cabinet-finance-hint">Касса — из карточек заказов и прежних записей бота. Касса равна комиссии, откат отдельно. Все суммы в ₽.</p><div class="cabinet-finance-scroll" role="region" aria-label="Касса по дням" tabindex="0"><table class="cabinet-earnings-table cabinet-cash-table"><colgroup><col style="width:24%"><col style="width:38%"><col style="width:38%"></colgroup><thead><tr><th>День</th><th>Касса</th><th>Откат</th></tr></thead><tbody>${cashRows}</tbody><tfoot><tr><th scope="row" title="Всего за месяц">Месяц</th><td id="cabinetCashTotal">${cabinetMoney(data.totals.cash)}</td><td id="cabinetKickbackTotal">${cabinetMoney(data.totals.kickback)}</td></tr></tfoot></table></div>`;
        qsa('[data-cash-day]',body).forEach(button=>button.onclick=()=>{
          const detail=qs('#cashDay-'+button.dataset.cashDay,body);detail.hidden=!detail.hidden;button.setAttribute('aria-expanded',String(!detail.hidden));
        });
        qsa('[data-edit-cash]',body).forEach(button=>button.onclick=async()=>{
          const entry=data.cash_entries.find(r=>r.event_id===Number(button.dataset.editCash));
          if(!await flush())return;
          try{await loadBootstrap(entry.work_date);await loadOwnerCabinet();if(state.bootstrap?.user?.id!==uid||!state.bootstrap?.can_use_owner_cabinet||currentViewId()!=='ownerCabinetView')return;await ownerCabinetAction('cash',entry.event_id)}catch(error){toast(error.message)}
        });
      }
    }
    qs('#cabinetFinanceMonth').onchange=async event=>{const month=event.target.value;if(/^\d{4}-\d{2}$/.test(month)&&await flush())await openCabinetFinance(month+'-01',tab);else event.target.value=data.start.slice(0,7)};
    qsa('[data-finance-tab]',panel).forEach(button=>button.onclick=async()=>{if(button.dataset.financeTab===tab)return;if(await flush())renderTab(button.dataset.financeTab)});
    renderTab(tab);
  }catch(error){if(active()){panel.innerHTML=`<p role="alert">${esc(error.message)}</p><button type="button" id="cabinetFinanceRetry" class="cabinet-secondary">Повторить</button>`;qs('#cabinetFinanceRetry').onclick=()=>openCabinetFinance(onDate,tab)}}
}

function cabinetPhotoInfo(c){
  const total=c.source==='gbu'?2:1,received=Number(!!c.first_received)+(c.source==='gbu'?Number(!!c.second_received):0);
  return {total,received,complete:received===total,label:received===total?'Фото получены':received?`Фото ${received}/${total}`:(c.photo_time?`Фото в ${c.photo_time}`:'Фото ожидается')};
}
function cabinetNextAction(c,data){
  data={...data,work_date:c.work_date};
  if(!c.photo_time)return {key:'schedule',title:'Указать время фото',icon:'clock'};
  if(!(c.prepared??c.composition_closed))return {key:'close',title:'Отчёт о закрытии заказа',icon:'check'};
  if(c.work_date===data.today&&!c.on_contact)return {key:'contact',title:'Бригада на связи',icon:'phone'};
  if(c.on_contact&&c.work_date<=data.today&&data.can_record_cash!==false&&c.source==='private'&&!c.cash&&!c.first_received)return {key:'cash',title:'Заполнить кассу',icon:'wallet'};
  if(data.work_date>data.today)return null;
  const photo=cabinetPhotoInfo(c);
  if(!photo.complete)return data.work_date===data.today?{key:'photo',title:c.source==='gbu'?(c.first_received?'Отправить второе фото':'Отправить первое фото'):'Отправить фото',icon:'camera'}:null;
  if(!c.tea_received)return {key:'tea',title:'Итоговый отчёт по заказу',icon:'tea'};
  return null;
}
function ownerCabinetCardsHtml(data){
  const cards=(data.number_cards||[]).filter(c=>!c.tea_received);
  const cardRows=cards.map(c=>{
    const photo=cabinetPhotoInfo(c),next=cabinetNextAction(c,data),done=photo.complete&&c.tea_received;
    return `<article class="cabinet-order"><div class="cabinet-order-top"><span class="cabinet-kind ${c.source==='gbu'?'gbu':''}">${c.source==='gbu'?'ГБУ':'Частный'}</span><small class="cabinet-card-date">${esc(fmtDate(c.work_date))}</small>${cabinetOrderTools(c)}</div><button type="button" class="cabinet-order-open" data-cabinet-detail="${Number(c.id)}"><h3>ум: ${esc(c.surname)}</h3></button><div class="cabinet-order-statuses"><span class="${(c.prepared??c.composition_closed)?'cabinet-done':''}">${cabinetIcon((c.prepared??c.composition_closed)?'check':'clock')}${(c.prepared??c.composition_closed)?'Закрытие подтверждено':'Оформление заказа'}</span><span>${esc(photo.label)} · ${photo.received}/${photo.total}</span></div>${done?'<p class="cabinet-done">✅ Заказ завершён</p>':next?cabinetButton(next.key,next.title,next.icon,c,true):'<p>Фото доступно в день заказа</p>'}<button type="button" class="cabinet-back" data-cabinet-detail="${Number(c.id)}">Все этапы заказа ${cabinetIcon('chevron')}</button></article>`;
  }).join('');
  return `${cardRows}${!cards.length?'<div class="cabinet-empty">Активных заказов нет.</div>':''}`;
}
function ownerCabinetDetailHtml(c,data){
  data={...data,work_date:c.work_date};
  const photo=cabinetPhotoInfo(c),today=data.work_date===data.today,next=cabinetNextAction(c,data),prepared=(c.prepared??c.composition_closed),privateOrder=c.source==='private';
  const stage=(key,icon,title,subtitle,done,action)=>`<section class="cabinet-stage ${done?'is-done':action?.key===next?.key?'is-next':''}"><div class="cabinet-stage-head">${cabinetIcon(done?'check':icon)}<div><h3>${title}</h3><p>${esc(subtitle)}</p></div></div>${action?cabinetButton(action.key,action.title,icon,c,action.key===next?.key,!!action.disabled):''}<div data-stage-host="${key}"></div></section>`;
  return `<button type="button" class="cabinet-back" id="cabinetBack">${cabinetIcon('back')}Мои заказы</button><article class="cabinet-order cabinet-detail"><div class="cabinet-detail-head"><div class="cabinet-order-top"><span class="cabinet-kind">${privateOrder?'Частный':'ГБУ'}</span>${cabinetOrderTools(c)}</div><h2>ум: ${esc(c.surname)}</h2><p>${esc(fmtDate(data.work_date))} · Фото ${esc(c.photo_time||'⏳')}</p>${photo.complete&&c.tea_received?'<p class="cabinet-done">✅ Заказ завершён</p>':''}</div><div data-stage-host="delete"></div><div data-stage-host="edit"></div><div class="cabinet-stages">${stage('number','check','1. Номер в группу','Карточка отправлена',true,null)}${stage('schedule','clock','2. Время фото',c.photo_time||'Укажите плановое время',!!c.photo_time,c.photo_time?null:{key:'schedule',title:'Указать время фото'})}${stage('close','check','3. Отчёт о закрытии',prepared?'Сохранён. Сотрудников назначает диспетчер':'Связь с агентом и время связи с бригадой',prepared,prepared?null:{key:'close',title:'Отчёт о закрытии',disabled:!c.photo_time})}${stage('contact','phone','Связь с бригадой',c.on_contact?'Бригада на связи':`План: ${c.contact_time||'не указан'}`,c.on_contact,c.on_contact?null:{key:'contact',title:'Бригада на связи',disabled:!today})}${privateOrder?stage('cash','wallet','4. Касса',c.cash?`${cabinetMoney(c.cash.commission)} ₽ · Откат ${cabinetMoney(c.cash.kickback)} ₽`:'Заполните перед отправкой фото',!!c.cash,{key:'cash',title:c.work_date>data.today?'Доступно в день заказа':!c.on_contact?'Сначала бригада на связи':c.cash?'Изменить кассу':'Заполнить кассу',disabled:!prepared||!c.on_contact||c.work_date>data.today}):''}${stage('photo','camera',`${privateOrder?5:4}. Фотоотчёт`,`${photo.received} из ${photo.total} · ${c.source==='gbu'?(c.first_received?'✅':'⬜️')+(c.second_received?'✅':'⬜️'):(c.first_received?'✅':'⬜️')}`,photo.complete,photo.complete?null:{key:'photo',title:privateOrder&&!c.cash?'Фото — после кассы':today?(c.source==='gbu'&&c.first_received?'Отправить второе фото':'Отправить фото'):'Доступно в день заказа',disabled:!prepared||!today||(privateOrder&&!c.cash)})}${stage('tea','tea',`${privateOrder?6:5}. Итоговый отчёт`,c.tea_received?`Заказ завершён ✅ · Чай ${cabinetMoney(c.tea_amount)} ₽`:'Откроется после всех фото',photo.complete&&c.tea_received,{key:'tea',title:c.tea_received?'Изменить отчёт':'Завершить заказ',disabled:!prepared||!photo.complete||data.work_date>data.today})}</div></article>`;
}
function renderOwnerCabinet(data){
  const root=qs('#ownerCabinetContent'),day=data.work_date,uid=state.bootstrap?.user?.id;
  const selected=(data.number_cards||[]).find(c=>Number(c.id)===Number(ownerCabinetSelectedCard));
  if(ownerCabinetSelectedCard&&!selected)ownerCabinetSelectedCard=null;
  if(selected){
    root.dataset.cabinetView='order';root.innerHTML=ownerCabinetDetailHtml(selected,data);
    qs('#cabinetBack',root).onclick=()=>{ownerCabinetSelectedCard=null;renderOwnerCabinet(data)};
  }else{
    root.dataset.cabinetView='home';
    const r=data.tomorrow_readiness;
    root.innerHTML=`${sessionStorage.getItem('cabinet-pending:'+uid)?'<button type="button" id="cabinetPending" class="cabinet-secondary cabinet-wide">Проверить последнее действие</button>':''}${ownerCabinetProfileHtml(data)}
      <article class="cabinet-readiness"><div><small>Готовность на завтра · ${esc(fmtDate(data.tomorrow))}</small><strong class="${r.status==='ready'?'cabinet-done':''}">${esc(r.label)}</strong></div><button type="button" class="cabinet-secondary" data-cabinet-action="readiness">${r.status==='no_response'?'Выбрать':'Изменить'}</button>${r.text?`<p>${esc(r.text)}</p>`:''}</article>
      <section class="cabinet-my-orders"><div class="cabinet-section-heading"><h2>Активные заказы</h2><button type="button" class="cabinet-number-link" data-cabinet-action="number">${cabinetIcon('plus')}Номер в группу</button></div>${ownerCabinetCardsHtml(data)}</section>
      <div class="cabinet-personal-links"><button type="button" id="cabinetFinanceLink">${cabinetIcon('wallet')}<span>Касса и заработок</span>${cabinetIcon('chevron')}</button><button type="button" id="cabinetEmployees">${cabinetIcon('team')}<span>Сотрудники</span>${cabinetIcon('chevron')}</button></div>`;
    if(qs('#cabinetPending',root))qs('#cabinetPending',root).onclick=cabinetResumePending;
    qs('#cabinetAvatar',root).onclick=openCabinetAvatar;
    qs('#cabinetEmployees',root).onclick=openCabinetEmployees;
    qs('#cabinetFinanceLink',root).onclick=()=>openCabinetFinance();
    qsa('[data-cabinet-day]',root).forEach(btn=>btn.onclick=()=>{ownerCabinetSelectedCard=null;loadBootstrap(btn.dataset.cabinetDay).catch(e=>toast(e.message))});
    qsa('[data-cabinet-detail]',root).forEach(btn=>btn.onclick=()=>{ownerCabinetSelectedCard=Number(btn.dataset.cabinetDetail);renderOwnerCabinet(data)});
    qsa('[data-cabinet-order]',root).forEach(btn=>btn.onclick=()=>openOrder(Number(btn.dataset.cabinetOrder)).catch(e=>toast(e.message)));
  }
  qsa('[data-cabinet-action]',root).forEach(btn=>{
    const cardId=btn.dataset.cabinetCard,card=cardId?(data.number_cards||[]).find(c=>Number(c.id)===Number(cardId)):null;
    btn.disabled=btn.disabled||!state.bootstrap?.can_use_owner_cabinet;
    btn.onclick=()=>ownerCabinetAction(btn.dataset.cabinetAction,cardId===undefined?null:Number(cardId));
  });
}
async function loadOwnerCabinet(){
  const root=qs('#ownerCabinetContent'),day=state.selectedDate,uid=state.bootstrap?.user?.id;
  if(!state.bootstrap?.can_use_owner_cabinet)return;
  const request=++ownerCabinetRequest;
  const active=()=>request===ownerCabinetRequest&&currentViewId()==='ownerCabinetView'&&state.bootstrap?.can_use_owner_cabinet&&state.selectedDate===day&&state.bootstrap?.user?.id===uid;
  if(ownerCabinetData?.work_date!==day){ownerCabinetData=null;ownerCabinetSignature='';ownerCabinetSelectedCard=null;root.innerHTML='<p class="cabinet-bot-hint" role="status">Загрузка вашего кабинета…</p>'}
  try{
    const data=await api(`/api/owner/brigadier-cabinet?work_date=${encodeURIComponent(day)}&active_only=true`,{cache:'no-store'});
    if(!active()||data.work_date!==day||data.user.id!==uid)return;
    ownerCabinetData=data;
    const signature=JSON.stringify(data);
    if(signature===ownerCabinetSignature&&root.children.length)return;
    // Keep open order forms, unsaved earnings and their focus during background refresh.
    if(qs('#cabinetNativeForm,#cabinetFinance',root))return;
    if(root.dataset.cabinetView==='order'&&data.number_cards.some(c=>Number(c.id)===Number(ownerCabinetSelectedCard)&&c.tea_received))ownerCabinetSelectedCard=null;
    if(root.contains(document.activeElement)&&document.activeElement?.tagName==='INPUT')return;
    ownerCabinetSignature=signature;
    const y=window.scrollY;
    const otherOpen=qs('.cabinet-other-actions',root)?.open,assignmentsOpen=qs('.cabinet-assignments',root)?.open;
    renderOwnerCabinet(data);
    if(otherOpen&&qs('.cabinet-other-actions',root))qs('.cabinet-other-actions',root).open=true;
    if(assignmentsOpen&&qs('.cabinet-assignments',root))qs('.cabinet-assignments',root).open=true;
    window.scrollTo({top:y,behavior:'instant'});
  }catch(e){if(active()){if(qs('#cabinetNativeForm,#cabinetFinance',root)){toast(e.message);return}ownerCabinetData=null;ownerCabinetSignature='';ownerCabinetSelectedCard=null;root.innerHTML=`<div class="cabinet-empty" role="alert">${esc(e.message)}<br><button type="button" class="cabinet-secondary" id="cabinetRetry">Повторить</button></div>`;qs('#cabinetRetry').onclick=()=>loadBootstrap(day).catch(e=>toast(e.message))}}
}
// Events''')
    return html.replace('</head>', r'''
<style id="owner-cabinet-style">
.cabinet-detail .cabinet-stage{border:0;border-top:1px solid var(--line);border-radius:0;padding:16px 0;background:transparent}
.cabinet-detail .cabinet-stages{gap:0}.cabinet-detail .cabinet-stage.is-next{background:transparent}
.cabinet-detail [data-stage-host] h3{font-size:17px;margin:14px 0 4px}
.cabinet-stage button[hidden]{display:none!important}
.cabinet-form-pair{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.cabinet-cash-result{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:14px;border-radius:12px;background:rgba(212,177,97,.12);font-size:14px}
.cabinet-cash-result strong{font-size:20px;color:var(--gold)}
.cabinet-finance-month{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font-size:14px}
.cabinet-finance-month input{min-height:44px;max-width:70%;border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:12px;padding:8px;font-size:16px;color-scheme:dark}
.cabinet-finance-tabs{display:flex;gap:6px;margin:14px 0;padding:4px;background:var(--panel);border-radius:14px;border:1px solid var(--line)}
.cabinet-finance-tabs button{flex:1;border:0;border-radius:10px;min-height:42px;padding:8px;color:var(--muted);background:transparent;font-size:14px}
.cabinet-finance-tabs button.active{color:var(--text);background:var(--panel2);font-weight:700}
.cabinet-finance-summary{display:flex;gap:12px;align-items:center;padding:20px 16px;background:var(--panel);border:1px solid var(--line);border-radius:16px}
.cabinet-finance-summary>div{flex:1}.cabinet-finance-summary small{display:block;color:var(--muted);font-size:12px;margin-bottom:8px}
.cabinet-finance-summary strong{font-size:28px;color:var(--gold)}.cabinet-finance-summary b{font-size:21px}
.cabinet-finance-hint{font-size:13px;color:var(--muted);line-height:1.5;margin:14px 0}
.cabinet-earnings-table{table-layout:fixed;width:100%;border-collapse:collapse;min-width:0;font-variant-numeric:tabular-nums}
.cabinet-earnings-table th,.cabinet-earnings-table td{border-bottom:1px solid var(--line);padding:8px 3px;text-align:right;white-space:nowrap;font-size:14px}
.cabinet-earnings-table th:first-child{text-align:left;font-size:12px}.cabinet-earnings-table tbody th{font-weight:400;color:var(--muted)}
.cabinet-earnings-table td:last-child{color:var(--gold);font-weight:700}
.cabinet-earnings-table input{box-sizing:border-box;width:100%;min-width:0;min-height:42px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text);font-size:16px;text-align:right;padding:7px 5px}
.cabinet-earnings-table tfoot td{font-weight:700;font-size:13px;padding-top:16px}
.cabinet-earnings-table input:focus{outline:1px solid var(--gold);border-color:var(--gold)}
#cabinetEarningsStatus{font-size:12px;color:var(--muted);line-height:1.5}#cabinetEarningsStatus.has-error{color:#ff7b7b}
.cabinet-earnings-table .cabinet-period-total th,.cabinet-earnings-table .cabinet-period-total td{padding:12px 3px;font-weight:700;background:rgba(212,177,97,.1);border-top:1px solid rgba(212,177,97,.35);border-bottom:1px solid rgba(212,177,97,.35);color:var(--gold);font-size:13px}
.cabinet-earnings-table .cabinet-period-total small{display:block;font-size:10px;font-weight:400;margin-top:4px;color:var(--muted)}
.cabinet-cash-table td:nth-child(2){color:var(--gold);font-weight:700}.cabinet-cash-table td:last-child{color:var(--text);font-weight:400}
.cabinet-cash-table .cabinet-period-total td,.cabinet-cash-table tfoot td{font-weight:700}
.cabinet-cash-table [data-cash-row] th,.cabinet-cash-table [data-cash-row] td{height:44px;box-sizing:border-box}
.cabinet-cash-date{display:flex;align-items:center;justify-content:flex-start;gap:2px;border:0;padding:0;min-height:44px;background:transparent;color:var(--gold);font:inherit}
.cabinet-cash-date .cabinet-icon{width:13px;height:13px}.cabinet-cash-date[aria-expanded=true] .cabinet-icon{transform:rotate(90deg)}
.cabinet-cash-details[hidden]{display:none}.cabinet-cash-details>td{white-space:normal;text-align:left;padding:0 10px;background:var(--panel)}
.cabinet-cash-details article{padding:12px 0;border-bottom:1px solid var(--line)}.cabinet-cash-details article:last-child{border-bottom:0}
.cabinet-cash-details p,.cabinet-cash-details small{font-size:13px;color:var(--muted);margin:8px 0}
.cabinet-cash-line{display:flex;justify-content:space-between;gap:10px;font-size:14px}.cabinet-cash-line strong{overflow-wrap:anywhere}.cabinet-cash-line b{white-space:nowrap}

.cabinet-native-form{display:grid;gap:14px;padding:8px 0}
.cabinet-native-form label{display:grid;gap:7px;font-size:14px;color:var(--muted)}
.cabinet-native-form input,.cabinet-native-form select,.cabinet-native-form textarea{box-sizing:border-box;width:100%;min-height:46px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--text);padding:10px;font-size:16px}
.cabinet-native-form p{font-size:13px;line-height:1.5;color:var(--muted);margin:0}
.cabinet-native-form #cabinetActionStatus{color:var(--gold)}
.cabinet-order-top>.cabinet-secondary,.cabinet-order-tools>.cabinet-secondary{min-height:44px;font-size:12px;padding:6px 9px;background:transparent;border:0;color:var(--muted)}

#cabinetDirectory{padding-top:8px}
.cabinet-directory-search,.cabinet-directory-filter{display:block;font-size:13px;color:var(--muted);margin-bottom:12px}
.cabinet-directory-search input,.cabinet-directory-filter select{display:block;width:100%;box-sizing:border-box;min-height:46px;margin-top:6px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--text);font-size:16px}
.cabinet-directory-group h4{font-size:15px;color:var(--gold);margin:20px 0 10px}
.cabinet-directory-person{border:1px solid var(--line);border-radius:13px;background:var(--panel);margin-bottom:8px;overflow:hidden}
.cabinet-directory-person summary{display:flex;align-items:center;justify-content:space-between;gap:10px;cursor:pointer;padding:13px;min-height:48px;list-style:none}
.cabinet-directory-person summary::-webkit-details-marker{display:none}
.cabinet-directory-person summary>span{min-width:0;overflow-wrap:anywhere}
.cabinet-directory-person b{font-size:15px}.cabinet-directory-person small{display:block;margin-top:5px;color:var(--muted);font-size:13px}
.cabinet-directory-person[open] summary>.cabinet-icon{transform:rotate(90deg)}
.cabinet-directory-contacts{display:flex;flex-wrap:wrap;gap:8px;padding:0 13px 13px}
.cabinet-directory-contacts a{display:flex;align-items:center;gap:7px;min-height:44px;padding:8px 10px;box-sizing:border-box;border:1px solid var(--line);border-radius:10px;color:var(--gold);text-decoration:none;overflow-wrap:anywhere;font-size:14px}
.cabinet-directory-contacts span{font-size:12px;color:var(--muted);padding:10px 0}
.owner-cabinet-open #dateStrip,.owner-cabinet-open #selectedDayHeader,.owner-cabinet-open #selectedDayHeader+h3,.owner-cabinet-open #summary,.owner-cabinet-open #cashBtn{display:none!important}
.owner-cabinet-open main{padding-top:0}
.owner-cabinet-open .topbar{padding-bottom:8px}
#ownerCabinetView{padding-bottom:18px;max-width:680px;margin:auto}
#ownerCabinetView button{touch-action:manipulation;cursor:pointer}
#ownerCabinetView button:disabled{opacity:.45;cursor:default}
#ownerCabinetView button:focus-visible,#cabinetDate:focus-visible{outline:2px solid var(--gold);outline-offset:3px}
.cabinet-icon{width:20px;height:20px;flex:none;vertical-align:middle}
.cabinet-owner-badge{display:flex;align-items:center;justify-content:flex-end;gap:5px;color:var(--muted);font-size:11px;margin:0 1px 12px}
.cabinet-owner-badge .cabinet-icon{width:13px;height:13px}
.cabinet-profile{display:flex;gap:14px;align-items:center;margin:14px 2px 22px}
.cabinet-avatar{position:relative;flex:none;width:64px;height:64px;display:grid;place-items:center;padding:0;border:1px solid #433923;border-radius:50%;color:var(--gold);font-size:21px;font-weight:700;background:#272216}
.cabinet-avatar-face{display:grid;place-items:center;width:100%;height:100%;border-radius:50%;overflow:hidden}
.cabinet-avatar-face img{width:100%;height:100%;object-fit:cover;display:block}
.cabinet-avatar-camera{position:absolute;right:-3px;bottom:-2px;width:24px;height:24px;display:grid;place-items:center;background:var(--gold);color:#17130b;border:2px solid var(--panel);border-radius:50%}
.cabinet-avatar-camera .cabinet-icon{width:14px;height:14px}
.cabinet-avatar-preview{width:144px;height:144px;display:grid;place-items:center;justify-self:center;border-radius:50%;overflow:hidden;background:#272216;color:var(--gold);border:1px solid #433923}
.cabinet-avatar-preview img{width:100%;height:100%;object-fit:cover;display:block}
.cabinet-avatar-preview>.cabinet-icon{width:38px;height:38px}
.cabinet-native-form .cabinet-avatar-note{text-align:center}
.cabinet-native-form .cabinet-avatar-picker{position:relative;display:flex;align-items:center;justify-content:center;gap:8px;min-height:48px;border:1px solid var(--line);border-radius:12px;background:var(--panel2);color:var(--text);overflow:hidden}
.cabinet-avatar-picker input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer}
.cabinet-avatar-picker:focus-within{outline:2px solid var(--gold);outline-offset:2px}
#cabinetAvatarStatus{color:var(--gold)}
#cabinetAvatarForm button:disabled{opacity:.45;cursor:default}
.cabinet-profile-copy{min-width:0;flex:1}
.cabinet-profile h2{font-size:17px;line-height:1.35;margin:0;overflow-wrap:anywhere}
.cabinet-profile p{color:var(--muted);font-size:13px;margin:4px 0 0;line-height:1.45;overflow-wrap:anywhere}
.cabinet-edit-profile{display:grid;place-items:center;width:44px;height:44px;flex:none;border:0;border-radius:12px;background:none;color:var(--muted)}
.cabinet-readiness{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px;margin-bottom:24px}
.cabinet-readiness>div{min-width:130px;flex:1}
.cabinet-readiness small{display:block;font-size:12px;color:var(--muted);line-height:1.5}
.cabinet-readiness strong{display:block;font-size:16px;margin-top:5px;line-height:1.4}
.cabinet-readiness p{width:100%;margin:0;color:var(--muted);font-size:12px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}
.cabinet-primary,.cabinet-secondary{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:46px;padding:11px 14px;border-radius:12px;font-size:14px;line-height:1.35;font-weight:650;text-align:center;max-width:100%;overflow-wrap:anywhere}
.cabinet-primary{width:100%;border:0;background:var(--gold);color:#17130b}
.cabinet-secondary{border:1px solid var(--line);background:var(--panel2);color:var(--text)}
.cabinet-wide{width:100%}
.cabinet-section-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.cabinet-section-heading h2{font-size:21px;margin:0;line-height:1.3}
.cabinet-number-link{display:flex;gap:5px;align-items:center;min-height:44px;padding:0 2px;border:0;background:none;color:var(--gold);font-size:13px}
.cabinet-number-link .cabinet-icon{width:17px;height:17px}
.cabinet-day-shortcuts{display:grid;grid-template-columns:1fr 1fr 44px;gap:6px;margin-bottom:14px}
.cabinet-day-shortcuts>button{min-height:44px;border:0;border-radius:11px;background:none;color:var(--muted);font-size:13px}
.cabinet-day-shortcuts>button.active{background:#232328;color:var(--text);font-weight:650}
.cabinet-date-picker{position:relative;display:grid;place-items:center;min-width:0;min-height:44px;border:1px solid var(--line);border-radius:11px;background:var(--panel);color:var(--muted);overflow:hidden}
.cabinet-date-picker:focus-within{outline:2px solid var(--gold);outline-offset:2px}
#cabinetDate{position:absolute;inset:0;opacity:0;width:100%;height:100%;margin:0;padding:0;cursor:pointer;font-size:16px;color-scheme:dark}
.cabinet-selected-date{font-size:13px;color:var(--muted);margin:0 2px 12px}
.cabinet-order{display:block;width:100%;padding:16px;background:var(--panel);border:1px solid var(--line);border-radius:19px;margin-bottom:12px;color:var(--text);text-align:left;overflow-wrap:anywhere}
.cabinet-order-open{display:block;width:100%;text-align:left;border:0;padding:0;background:none;color:var(--text)}
.cabinet-order-top{display:flex;justify-content:space-between;align-items:center;gap:8px;color:var(--muted);font-size:12px}
.cabinet-kind{display:inline-block;padding:5px 8px;background:#282315;color:var(--gold);font-size:11px;border-radius:6px;line-height:1.3}
.cabinet-kind.gbu{background:#23212a;color:#d3cadf}
.cabinet-order-clock{display:flex;align-items:center;gap:6px;font-size:12px;font-variant-numeric:tabular-nums;text-align:right}
.cabinet-order-clock .cabinet-icon{width:16px;height:16px}
.cabinet-order h3{font-size:20px;line-height:1.35;margin:13px 0 6px;color:var(--text)}
.cabinet-order p{font-size:13px;line-height:1.5;margin:8px 0;color:var(--muted)}
.cabinet-order small{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--gold);line-height:1.5;margin-top:12px}
.cabinet-order small .cabinet-icon{width:14px;height:14px}
.cabinet-order-statuses{display:flex;flex-wrap:wrap;justify-content:space-between;gap:9px;margin:15px 0 17px;font-size:12px;color:var(--muted);line-height:1.4}
.cabinet-order-statuses>span{display:flex;gap:5px;align-items:center}
.cabinet-order-statuses .cabinet-icon{width:16px;height:16px}
#ownerCabinetView .cabinet-done{color:var(--green)}
.cabinet-order .cabinet-next-note{display:flex;align-items:center;justify-content:center;gap:6px;font-size:11px;margin:12px 0 0;color:var(--muted);line-height:1.4}
.cabinet-next-note .cabinet-icon{width:13px;height:13px}
.cabinet-assignments{margin:14px 0 0}
.cabinet-assignments summary,.cabinet-other-actions summary{color:var(--muted);font-size:13px;line-height:1.4;padding:12px 2px;min-height:44px;cursor:pointer}
.cabinet-assignments summary span{color:var(--gold);margin-left:5px}
.cabinet-assignments>div{margin-top:8px}
.cabinet-empty{padding:28px 16px;border:1px solid var(--line);border-radius:16px;background:var(--panel);color:var(--muted);font-size:14px;line-height:1.5;text-align:center}
.cabinet-empty button{margin-top:14px}
.cabinet-personal-links{margin-top:22px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:0 14px}
.cabinet-personal-links>button{width:100%;display:flex;gap:12px;align-items:center;border:0;background:none;color:var(--text);min-height:56px;padding:12px 0;text-align:left;font-size:14px;line-height:1.4}
.cabinet-personal-links>button+button{border-top:1px solid var(--line)}
.cabinet-personal-links>button>.cabinet-icon:first-child{color:var(--gold)}
.cabinet-personal-links>button>span{flex:1;min-width:0}
.cabinet-personal-links>button>.cabinet-icon:last-child{width:16px;height:16px;color:var(--muted)}
.cabinet-other-actions{margin-top:10px}
.cabinet-other-actions>div{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:5px 0}
.cabinet-other-actions .cabinet-secondary{font-size:12px;font-weight:500;justify-content:flex-start;gap:7px;padding:10px}
.cabinet-bot-hint{font-size:12px;color:var(--muted);line-height:1.5;margin:14px 2px}
.cabinet-back{display:flex;align-items:center;gap:5px;min-height:44px;padding:0;background:none;border:0;color:var(--gold);font-size:14px;margin:0 0 14px}
.cabinet-back .cabinet-icon{width:18px;height:18px}
.cabinet-order-tools{display:flex;align-items:center;gap:4px;flex-shrink:0}
.cabinet-trash{display:flex;align-items:center;justify-content:center;width:40px;min-width:40px;height:44px;padding:0;border:0;border-radius:10px;background:none;color:var(--muted)}
.cabinet-trash:hover,.cabinet-trash:focus-visible{color:#ff7979;background:#332021}
.cabinet-trash .cabinet-icon{width:20px;height:20px}
.cabinet-trash:disabled{opacity:.3;cursor:default;background:none;color:var(--muted)}
.cabinet-primary.cabinet-danger{background:#492326;color:#ffb5b5;border-color:#7b3a40}
.cabinet-number-date{color:var(--gold)}
.cabinet-detail-head{margin-bottom:22px}
.cabinet-detail-head h2{font-size:26px;line-height:1.3;overflow-wrap:anywhere;margin:14px 0 0}
.cabinet-stages{display:grid;gap:12px}
.cabinet-stage{padding:16px;background:var(--panel);border:1px solid var(--line);border-radius:18px}
.cabinet-stage.is-done{background:#14231b;border-color:#274132}
.cabinet-stage.is-next{border-color:var(--gold)}
.cabinet-stage-head{display:flex;gap:12px;align-items:center}
.cabinet-stage-head>div{min-width:0;flex:1}
.cabinet-stage.is-done .cabinet-stage-head>.cabinet-icon{color:var(--green)}
.cabinet-stage.is-next .cabinet-stage-head>.cabinet-icon{color:var(--gold)}
.cabinet-stage-head h3{font-size:16px;line-height:1.4;margin:0}
.cabinet-stage-head p{font-size:12px;color:var(--muted);line-height:1.5;margin:4px 0 0;overflow-wrap:anywhere}
.cabinet-stage.is-done p{color:var(--green)}
.cabinet-stage>button{width:100%;margin-top:14px}
@media(max-width:350px){.cabinet-section-heading h2{font-size:20px}.cabinet-number-link{font-size:12px}.cabinet-order{padding:14px}.cabinet-order-statuses{font-size:11px;gap:7px}.cabinet-other-actions>div{grid-template-columns:1fr}}
</style></head>''', 1)






INLINE_INDEX_HTML = _owner_brigadier_cabinet_html(INLINE_INDEX_HTML)

# Native cabinet commands are durable; only the bot executes Telegram effects.
class CabinetCommand(Base):
    __tablename__ = 'owner_cabinet_commands'
    id = Column(String(64), primary_key=True)
    tg_id = Column(BigInteger, nullable=False, index=True)
    payload = Column(String, nullable=False)
    fingerprint = Column(String(64), nullable=False)
    status = Column(String(20), nullable=False, default='queued')
    result = Column(String, nullable=False, default='{}')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    lease_until = Column(DateTime, nullable=True)

class CabinetOrderState(Base):
    __tablename__ = 'owner_cabinet_order_state'
    report_key = Column(String(100), primary_key=True)
    tg_id = Column(BigInteger, nullable=False)
    payload = Column(String, nullable=False)

class CabinetCashSummaryPeriod(BaseModel):
    model_config = {'extra': 'forbid'}
    month: str = Field(pattern=r'^[1-9]\d{3}-(0[1-9]|1[0-2])$')
    half: StrictInt = Field(ge=1, le=2)


class CabinetCommandInput(BaseModel):
    request_id: str = Field(min_length=32, max_length=64, pattern=r'^[a-zA-Z0-9-]+$')
    action: Literal['number','delete','edit','schedule','close','cash','cash_summary','contact','photo','tea','profile','register','readiness']
    card_id: int | None = None
    values: dict = Field(default_factory=dict)


def cabinet_extra_state(db, card):
    row = db.get(CabinetOrderState, f'card:{card.chat_id}:{card.message_id}')
    meta = json.loads(row.payload) if row and row.tg_id == card.tg_id else {}
    return {**meta, 'cash': cabinet_cash_for_card(db, card)}


def cabinet_number_date():
    return (datetime.now(MOSCOW).date() + timedelta(days=1)).isoformat()


def cabinet_order_deletion_info(db, card):
    key = f'card:{card.chat_id}:{card.message_id}'
    photo = db.get(PhotoReportEvent, key)
    control = db.get(OrderPhotoControl, key)
    override = db.get(OrderPhotoOverride, card.id)
    if photo is not None or (control and (control.first_received or control.second_received)) or (override and (override.first_received or override.second_received)):
        reason = 'По заказу уже отмечено фото — удаление и замена недоступны'
    else:
        reason = ''
    return {'can_delete': not bool(reason), 'delete_reason': reason}


@app.post('/api/owner/brigadier-cabinet/actions')
def cabinet_command_create(data: CabinetCommandInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, 'Кабинет недоступен')
    if data.action in {'cash', 'cash_summary'} and not cabinet_can_cash(db, user.id):
        raise HTTPException(403, 'В вашем кабинете ведение кассы отключено')
    # Hash the submitted intent before resolving a live card/date. A retry of
    # a successful deletion remains valid after that card has disappeared.
    intent = json.dumps({'tg_id': user.id, **data.model_dump(exclude={'request_id'})},
        ensure_ascii=False, sort_keys=True)
    if len(intent.encode()) > 5600000:
        raise HTTPException(413, 'Фото слишком большое. Выберите другое фото')
    digest = hashlib.sha256(intent.encode()).hexdigest()
    existing = db.get(CabinetCommand, data.request_id)
    if existing:
        if existing.tg_id != user.id or existing.fingerprint != digest:
            raise HTTPException(409, 'Этот запрос уже использован. Откройте форму заново')
        return {'id': existing.id, 'status': existing.status, **json.loads(existing.result)}
    payload = {'action': data.action, 'values': dict(data.values), 'tg_id': user.id}
    if data.action == 'cash_summary':
        cabinet_finance_access(db, user.id)
        try:
            period = CabinetCashSummaryPeriod(**data.values)
        except ValueError:
            raise HTTPException(422, 'Выберите месяц и период подсчёта кассы')
        if data.card_id is not None:
            raise HTTPException(422, 'Подсчёт относится к периоду, а не к заказу')
        payload['values'] = period.model_dump()
        # Serialize submissions per account; double taps/reopened forms reuse a job.
        db.scalar(select(Employee).where(Employee.tg_id == user.id).with_for_update().limit(1))
        recent = db.scalars(select(CabinetCommand).where(CabinetCommand.tg_id == user.id,
            CabinetCommand.created_at >= datetime.utcnow()-timedelta(minutes=10)))
        for command in recent:
            previous = json.loads(command.payload)
            if previous.get('action') == 'cash_summary' and previous.get('values') == payload['values'] and (
                    command.status in {'queued','running'} or (command.status == 'done' and
                    command.created_at >= datetime.utcnow()-timedelta(seconds=60))):
                return {'id': command.id, 'status': command.status, **json.loads(command.result)}
    clock_fields = {'schedule': ('photo_time',), 'close': ('contact_time',),
                    'edit': ('photo_time', 'contact_time')}.get(data.action, ())
    for field in clock_fields:
        value = payload['values'].get(field)
        if data.action == 'edit' and not value:
            continue
        clock = _canonical_cabinet_clock(value)
        if clock is None:
            raise HTTPException(422, 'Укажите время от 00:00 до 23:59 в формате ЧЧ:ММ')
        payload['values'][field] = clock
    if not _is_cabinet_owner(db, user.id):
        payload['access_version'] = db.get(BrigadierCabinetAccess, user.id).version
    if data.action == 'number':
        # The server freezes tomorrow in Moscow when accepting the command.
        # A delayed worker must not move it to another day after midnight.
        payload['values']['work_date'] = cabinet_number_date()
    if data.action in {'delete','edit','schedule','close','cash','contact','photo','tea'} or (data.action == 'number' and data.card_id is not None):
        card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.id == data.card_id,
            NumberCardEvent.tg_id == user.id, _visible_number_card()))
        if not card:
            raise HTTPException(404, 'Заказ удалён или передан другому бригадиру')
        if data.action == 'cash':
            cabinet_cash_stage_guard(db, card)
        if data.action == 'photo':
            values = payload['values']
            if 'original_image' in values or 'frame_confirmed' in values:
                if values.get('frame_confirmed') is not True or not isinstance(values.get('original_image'), str) or not isinstance(values.get('image'), str):
                    raise HTTPException(422, 'Сначала проверьте и подтвердите фото')
                _report_media_bytes('data:image/jpeg;base64,' + values['original_image'])
                _report_media_bytes('data:image/jpeg;base64,' + values['image'])
            ordinal = values.get('ordinal')
            if type(ordinal) is not int or ordinal not in {1, 2} or (card.source == 'private' and ordinal != 1):
                raise HTTPException(400, 'Откройте карточку фото заново')
            if card.source == 'gbu' and ordinal == 1:
                raw_surname = card.surname
                surname = ' '.join(raw_surname.split()).upper() if isinstance(raw_surname, str) else ''
                if not surname or len(surname) > 100 or not re.fullmatch(r"[А-ЯЁA-Z\s.'’\-]+", surname):
                    raise HTTPException(400, 'Укажите фамилию умершего')
                values['surname'] = surname
            elif card.source == 'private':
                raw_comment = values.get('comment')
                comment = raw_comment.strip() if isinstance(raw_comment, str) else ''
                if not comment or len(comment) > 500:
                    raise HTTPException(400, 'Укажите комментарий к фото: название морга, во сколько быть в морге (до 500 символов)')
                values['comment'] = comment
        if data.action == 'delete' or data.action == 'number':
            deletion = cabinet_order_deletion_info(db, card)
            if not deletion['can_delete']:
                raise HTTPException(409, deletion['delete_reason'])
        payload['replace_report_key' if data.action == 'number' else 'report_key'] = f'card:{card.chat_id}:{card.message_id}'
    if data.action in {'number', 'edit', 'photo'} and 'uniform_category' in payload['values']:
        category = payload['values']['uniform_category']
        source = payload['values'].get('source') if data.action in {'number', 'edit'} else card.source
        if source is None and data.action == 'edit':
            source = card.source
        if category not in ({'standard', 'elite'} if source == 'gbu' else {'standard', 'vip', 'elite'}):
            raise HTTPException(422, 'Выберите категорию заказа')
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode()) > 5600000:
        raise HTTPException(413, 'Фото слишком большое. Выберите другое фото')
    pending = db.scalar(select(func.count()).select_from(CabinetCommand).where(
        CabinetCommand.tg_id == user.id, CabinetCommand.status.in_(['queued','running'])))
    if pending >= 5:
        raise HTTPException(409, 'Дождитесь выполнения предыдущих действий')
    db.add(CabinetCommand(id=data.request_id, tg_id=user.id, payload=encoded, fingerprint=digest))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(CabinetCommand, data.request_id)
        if not existing or existing.tg_id != user.id or existing.fingerprint != digest:
            raise HTTPException(409, 'Повторный запрос с другими данными')
    return {'id': data.request_id, 'status':'queued'}


@app.get('/api/owner/brigadier-cabinet/actions/{request_id}')
def cabinet_command_status(request_id: str, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    if not _can_use_owner_cabinet(db, user.id):
        raise HTTPException(403, 'Кабинет недоступен')
    row = db.get(CabinetCommand, request_id)
    if not row or row.tg_id != user.id:
        raise HTTPException(404, 'Действие не найдено')
    return JSONResponse({'id': row.id, 'status': row.status, **json.loads(row.result)},
                        headers={'Cache-Control':'no-store'})


@app.post('/api/bot/cabinet/claim', dependencies=[Depends(_bot_key)])
def cabinet_command_claim(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    row = db.scalar(select(CabinetCommand).where(or_(CabinetCommand.status == 'queued',
        and_(CabinetCommand.status == 'running', CabinetCommand.lease_until < now)))
        .order_by(CabinetCommand.created_at).with_for_update(skip_locked=True).limit(1))
    if not row:
        return {'job': None}
    if not _can_use_owner_cabinet(db, row.tg_id):
        row.status='error'; row.payload='{}'; row.result=json.dumps({'error':'Доступ к кабинету отозван'})
        db.commit()
        return {'job':None}
    row.status='running'; row.lease_until=now+timedelta(minutes=10)
    payload=json.loads(row.payload)
    if not _is_cabinet_owner(db, row.tg_id):
        grant = db.get(BrigadierCabinetAccess, row.tg_id)
        if not grant or payload.get('access_version') != grant.version:
            row.status='error';row.payload='{}';row.result=json.dumps({'error':'Доступ изменён. Откройте действие заново'})
            db.commit()
            return {'job':None}
    db.commit()
    return {'job':{'id':row.id, **payload}}


class CabinetCommandResult(BaseModel):
    id: str
    status: Literal['done','error']
    result: dict = Field(default_factory=dict)

@app.post('/api/bot/cabinet/result', dependencies=[Depends(_bot_key)])
def cabinet_command_finish(data: CabinetCommandResult, db: Session = Depends(get_db)):
    row=db.scalar(select(CabinetCommand).where(CabinetCommand.id == data.id).with_for_update())
    if not row:
        raise HTTPException(404,'Действие не найдено')
    if row.status not in {'done','error'}:
        result = dict(data.result)
        intent = json.loads(row.payload)
        if data.status == 'done' and intent.get('action') in {'number', 'edit', 'photo'} and intent.get('values', {}).get('uniform_category') in {'standard', 'vip', 'elite'}:
            key = str(result.get('report_key') or intent.get('report_key') or '')
            if re.fullmatch(r'card:-?\d+:\d+', key):
                saved = db.get(CabinetOrderState, key)
                meta = json.loads(saved.payload) if saved and saved.tg_id == row.tg_id else {}
                if not (result.get('duplicate') and meta.get('uniform_category')):
                    if saved is None:
                        saved = CabinetOrderState(report_key=key, tg_id=row.tg_id)
                        db.add(saved)
                    saved.tg_id = row.tg_id
                    saved.payload = json.dumps(dict(meta, uniform_category=intent['values']['uniform_category']))
        if data.status == 'done' and intent.get('action') == 'cash_summary':
            record_cash_period_reconciliation(db, row, intent)
        if data.status == 'done' and intent.get('action') in {'schedule', 'edit'}:
            key = str(intent.get('report_key') or '')
            clock = _canonical_cabinet_clock(intent.get('values', {}).get('photo_time'))
            if clock and re.fullmatch(r'card:-?\d+:\d+', key):
                _, chat, message = key.split(':')
                card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == int(chat),
                    NumberCardEvent.message_id == int(message), NumberCardEvent.tg_id == row.tg_id,
                    _visible_number_card()).with_for_update())
                if card:
                    control = db.get(OrderPhotoControl, key)
                    if control is None:
                        control = OrderPhotoControl(report_key=key, tg_id=row.tg_id)
                        db.add(control)
                    control.tg_id = row.tg_id
                    control.photo_time = clock
                    result['photo_time'] = clock
        if data.status == 'done' and intent.get('action') == 'tea':
            key = str(intent.get('report_key') or '')
            if re.fullmatch(r'card:-?\d+:\d+', key):
                _, chat, message = key.split(':')
                card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == int(chat),
                    NumberCardEvent.message_id == int(message), NumberCardEvent.tg_id == row.tg_id,
                    _visible_number_card()).with_for_update())
                if card:
                    state_row = db.get(CabinetOrderState, key)
                    meta = json.loads(state_row.payload) if state_row and state_row.tg_id == row.tg_id else {}
                    if state_row is None:
                        state_row = CabinetOrderState(report_key=key, tg_id=row.tg_id)
                        db.add(state_row)
                    state_row.tg_id = row.tg_id
                    state_row.payload = json.dumps(dict(meta, prepared=True, tea_received=True,
                        tea_amount=int(intent['values']['amount'])))
                    result['completed'] = True
        if data.status == 'done' and intent.get('action') == 'photo' and intent.get('values', {}).get('frame_confirmed') is True:
            values = intent['values']
            key = str(intent.get('report_key') or '')
            ordinal = values.get('ordinal')
            if re.fullmatch(r'card:-?\d+:\d+', key) and ordinal in (1, 2):
                frame = db.get(OrderReportFraming, (key, ordinal))
                if frame is None:
                    frame = OrderReportFraming(report_key=key, stage=ordinal)
                    db.add(frame)
                frame.original_image = 'data:image/jpeg;base64,' + values['original_image']
        row.status=data.status;row.result=json.dumps(result,ensure_ascii=False)
        row.payload=json.dumps(intent,ensure_ascii=False) if intent.get('action') == 'cash_summary' else '{}'
        db.commit()
    return {'ok':True}

@app.post('/api/bot/cabinet/state', dependencies=[Depends(_bot_key)])
def cabinet_state_sync(data: list[dict], db: Session = Depends(get_db)):
    if len(data)>100:
        raise HTTPException(422,'Слишком много записей')
    for item in data:
        key=str(item.get('report_key',''))
        if not re.fullmatch(r'card:-?\d+:\d+',key):
            continue
        _,chat,message=key.split(':')
        card=db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id==int(chat),
            NumberCardEvent.message_id==int(message),NumberCardEvent.tg_id==int(item['tg_id']),_visible_number_card()))
        if not card:
            continue
        row=db.get(CabinetOrderState,key)
        if not row:
            row=CabinetOrderState(report_key=key);db.add(row)
        previous = json.loads(row.payload) if row.payload and row.tg_id == card.tg_id else {}
        values = {k:item.get(k) for k in ('prepared','tea_received','tea_amount')}
        if previous.get('uniform_category') in {'standard', 'vip', 'elite'}:
            values['uniform_category'] = previous['uniform_category']
        if previous.get('tea_received') and not values['tea_received']:
            values.update(prepared=previous.get('prepared'), tea_received=True,
                tea_amount=previous.get('tea_amount'))
        row.tg_id=card.tg_id
        row.payload=json.dumps(values)
    db.commit()
    return {'ok':True}

# Personal brigadier finance is separate from the dispatcher's/company cash ledger.
# In particular, the legacy demo/owner cash purge must never touch these records.
class CabinetOrderCash(Base):
    __tablename__ = 'cabinet_order_cash'
    event_id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, primary_key=True)
    work_date = Column(Date, nullable=False, index=True)
    surname = Column(String(100), nullable=False)
    category = Column(String(20), nullable=False)
    team_size = Column(Integer, nullable=False)
    commission = Column(Integer, nullable=False)
    kickback = Column(Integer, nullable=False)
    reserve = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CabinetEarningsDay(Base):
    __tablename__ = 'cabinet_earnings_days'
    tg_id = Column(BigInteger, primary_key=True)
    work_date = Column(Date, primary_key=True)
    salary = Column(Integer, nullable=True)
    tea = Column(Integer, nullable=True)
    category = Column(String(20), nullable=True)
    second_category = Column(String(20), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CabinetCashReceipt(Base):
    __tablename__ = 'cabinet_cash_receipts'
    request_id = Column(String(64), primary_key=True)
    fingerprint = Column(String(64), nullable=False)
    response = Column(String, nullable=False)


class CabinetCashInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    tg_id: StrictInt
    report_key: str = Field(pattern=r'^card:-?\d+:\d+$')
    category: Literal['standard','vip','elite']
    team_size: StrictInt = Field(ge=4, le=6)
    commission: StrictInt = Field(ge=0, le=10000000)
    kickback: StrictInt = Field(ge=0, le=10000000)
    reserve: StrictInt = Field(ge=0, le=6)
    version: StrictInt = Field(ge=0)


class CabinetEarningsInput(BaseModel):
    salary: StrictInt | None = Field(default=None, ge=0, le=10000000)
    tea: StrictInt | None = Field(default=None, ge=0, le=10000000)
    category: Literal['standard','vip','elite'] | None = None
    second_category: Literal['standard','vip','elite'] | None = None
    version: StrictInt = Field(ge=0)


def cabinet_finance_access(db, uid):
    if not _can_use_owner_cabinet(db, uid):
        raise HTTPException(403, 'Кабинет недоступен')
    employee = db.scalar(select(Employee.id).where(Employee.tg_id == uid,
        Employee.active.is_(True), Employee.group_code == 'brigadier', _visible_employee()).limit(1))
    if employee is None:
        raise HTTPException(403, 'Сначала заполните профиль бригадира')


def cabinet_cash_dict(row):
    return {'entry_key': f'cabinet:{row.event_id}:{row.tg_id}', 'source': 'cabinet',
        'event_id': row.event_id, 'work_date': row.work_date.isoformat(), 'surname': row.surname,
        'category': row.category, 'category_label': {'standard':'Стандарт','vip':'Вип','elite':'Элит'}[row.category],
        'team_size': row.team_size, 'commission': row.commission, 'kickback': row.kickback,
        'reserve': row.reserve, 'cash': row.commission, 'version': row.version}


def cabinet_legacy_cash_dict(row):
    # Read the original ledger: old edits/deletions stay in sync and reopening
    # the cabinet cannot create copies. Amounts in both ledgers are whole RUB.
    commission = int(row.commission_rub or 0)
    return {'entry_key': f'bot:{row.id}', 'source': 'bot', 'cash_entry_id': row.id,
        'event_id': None, 'work_date': row.work_date.isoformat(), 'surname': '',
        'category': row.category,
        'category_label': {'standard':'Стандарт','vip':'Вип','elite':'Элит'}.get(row.category, 'Заказ'),
        'team_size': row.team_size, 'commission': commission,
        'kickback': int(row.kickback_rub or 0), 'reserve': int(row.reserve_count or 0),
        'cash': commission, 'version': None, 'editable': False}


def cabinet_cash_for_card(db, card):
    row = db.get(CabinetOrderCash, (card.id, card.tg_id))
    return cabinet_cash_dict(row) if row else None


def cabinet_finance_card(db, uid, key):
    cabinet_finance_access(db, uid)
    if not re.fullmatch(r'card:-?\d+:\d+', key):
        raise HTTPException(404, 'Заказ недоступен')
    _, chat, message = key.split(':')
    card = db.scalar(select(NumberCardEvent).where(NumberCardEvent.chat_id == int(chat),
        NumberCardEvent.message_id == int(message), NumberCardEvent.tg_id == uid,
        _visible_number_card()).with_for_update())
    if not card:
        raise HTTPException(404, 'Заказ удалён или передан другому бригадиру')
    return card


@app.get('/api/bot/cabinet/cash', dependencies=[Depends(_bot_key)])
def cabinet_cash_status(tg_id: int, report_key: str, db: Session = Depends(get_db)):
    card = cabinet_finance_card(db, tg_id, report_key)
    return {'cash': cabinet_cash_for_card(db, card)}


def cabinet_cash_stage_guard(db, card):
    if card.work_date > datetime.now(MOSCOW).date():
        raise HTTPException(409, 'Касса доступна начиная со дня заказа по московскому времени')
    contact = db.get(OrderBrigadeContact, card.id)
    if not contact or not contact.confirmed or contact.tg_id != card.tg_id:
        raise HTTPException(409, 'Сначала нажмите «Бригада на связи» по этому заказу')


@app.post('/api/bot/cabinet/cash', dependencies=[Depends(_bot_key)])
def cabinet_cash_save(data: CabinetCashInput, db: Session = Depends(get_db)):
    if not cabinet_can_cash(db, data.tg_id):
        raise HTTPException(403, 'В вашем кабинете ведение кассы отключено')
    card = cabinet_finance_card(db, data.tg_id, data.report_key)
    cabinet_cash_stage_guard(db, card)
    if card.source != 'private':
        raise HTTPException(422, 'Касса на этом этапе заполняется только для частного заказа')
    if data.team_size not in (4, 6) or data.reserve > data.team_size:
        raise HTTPException(422, 'Проверьте состав бригады и количество резерва')
    meta = cabinet_extra_state(db, card)
    if not meta.get('prepared') and not number_card_statuses(db, [card])[card.id]['closed']:
        raise HTTPException(409, 'Сначала отправьте отчёт о закрытии заказа')
    fingerprint = hashlib.sha256(json.dumps(data.model_dump(), sort_keys=True).encode()).hexdigest()
    receipt = db.get(CabinetCashReceipt, data.request_id)
    if receipt:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, 'Этот запрос уже использован')
        return json.loads(receipt.response)
    row = db.get(CabinetOrderCash, (card.id, data.tg_id))
    values = {k: getattr(data, k) for k in ('category','team_size','commission','kickback','reserve')}
    if (row.version if row else 0) != data.version:
        raise HTTPException(409, 'Касса уже изменена. Обновите карточку и проверьте суммы')
    try:
        if row:
            changed = db.execute(update(CabinetOrderCash).where(CabinetOrderCash.event_id == card.id,
                CabinetOrderCash.tg_id == data.tg_id, CabinetOrderCash.version == data.version).values(
                **values, surname=card.surname, version=data.version + 1, updated_at=datetime.utcnow()))
            if changed.rowcount != 1:
                db.rollback()
                raise HTTPException(409, 'Касса уже изменена. Обновите карточку')
            db.refresh(row)
        else:
            row = CabinetOrderCash(event_id=card.id, tg_id=data.tg_id, work_date=card.work_date,
                surname=card.surname, version=1, **values)
            db.add(row)
            db.flush()
        response = {'saved': True, 'cash': cabinet_cash_dict(row)}
        db.add(CabinetCashReceipt(request_id=data.request_id, fingerprint=fingerprint,
            response=json.dumps(response, ensure_ascii=False)))
        db.commit()
        return response
    except IntegrityError:
        db.rollback()
        receipt = db.get(CabinetCashReceipt, data.request_id)
        if receipt and receipt.fingerprint == fingerprint:
            return json.loads(receipt.response)
        raise HTTPException(409, 'Касса уже сохранена. Обновите карточку')


def cabinet_earnings_dict(day, row):
    salary = row.salary if row else None
    tea = row.tea if row else None
    return {'work_date': day.isoformat(), 'salary': salary, 'tea': tea,
        'category': row.category if row else None,
        'second_category': row.second_category if row else None,
        'total': (salary or 0) + (tea or 0), 'version': row.version if row else 0}


@app.put('/api/owner/brigadier-cabinet/earnings/{work_date}')
def cabinet_earnings_save(work_date: date, data: CabinetEarningsInput,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    cabinet_finance_access(db, user.id)
    row = db.get(CabinetEarningsDay, (user.id, work_date))
    # Older clients omit category fields; preserve the choices made for this day.
    values = {'salary': data.salary, 'tea': data.tea,
        'category': data.category if 'category' in data.model_fields_set else (row.category if row else None),
        'second_category': data.second_category if 'second_category' in data.model_fields_set else (row.second_category if row else None)}
    if values['second_category'] is not None and values['category'] is None:
        raise HTTPException(422, 'Выберите категорию первого заказа')
    # An identical retry is safe, even if the first response was lost.
    if row and all(getattr(row, field) == value for field, value in values.items()):
        return cabinet_earnings_dict(work_date, row)
    if (row.version if row else 0) != data.version:
        raise HTTPException(409, 'Суммы за день уже изменены. Обновите таблицу и проверьте запись')
    try:
        if row:
            changed = db.execute(update(CabinetEarningsDay).where(CabinetEarningsDay.tg_id == user.id,
                CabinetEarningsDay.work_date == work_date, CabinetEarningsDay.version == data.version).values(
                **values, version=data.version + 1, updated_at=datetime.utcnow()))
            if changed.rowcount != 1:
                db.rollback()
                raise HTTPException(409, 'Суммы за день уже изменены. Обновите таблицу')
            db.refresh(row)
        else:
            row = CabinetEarningsDay(tg_id=user.id, work_date=work_date, **values, version=1)
            db.add(row)
        db.commit()
        return cabinet_earnings_dict(work_date, row)
    except IntegrityError:
        db.rollback()
        row = db.get(CabinetEarningsDay, (user.id, work_date))
        if row and all(getattr(row, field) == value for field, value in values.items()):
            return cabinet_earnings_dict(work_date, row)
        raise HTTPException(409, 'Суммы за день уже сохранены. Обновите таблицу')


@app.get('/api/owner/brigadier-cabinet/finance')
def cabinet_finance(on_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    cabinet_finance_access(db, user.id)
    import calendar
    start = on_date.replace(day=1)
    end = on_date.replace(day=calendar.monthrange(on_date.year, on_date.month)[1])
    earnings = {r.work_date: r for r in db.scalars(select(CabinetEarningsDay).where(
        CabinetEarningsDay.tg_id == user.id, CabinetEarningsDay.work_date.between(start, end)))}
    days = [cabinet_earnings_dict(start + timedelta(days=i), earnings.get(start + timedelta(days=i)))
        for i in range((end-start).days + 1)]
    # Read the same historical calculation as the owner's allowance table.
    # Do not copy it into manual earnings: reopening must never double-count it.
    has_allowance = not _is_owner(db, user.id) and _can_view_vlad_allowance(db, user)
    for row in days:
        row['allowance'] = _vlad_allowance_cents(db, date.fromisoformat(row['work_date'])) // 100 if has_allowance else 0
        row['total'] += row['allowance']
    cash = db.scalars(select(CabinetOrderCash).where(CabinetOrderCash.tg_id == user.id,
        CabinetOrderCash.work_date.between(start, end)).order_by(CabinetOrderCash.work_date, CabinetOrderCash.event_id)).all()
    cards = {r.id: r for r in db.scalars(select(NumberCardEvent).where(NumberCardEvent.tg_id == user.id,
        NumberCardEvent.id.in_([r.event_id for r in cash]), _visible_number_card()))} if cash else {}
    entries = []
    for row in cash:
        item = cabinet_cash_dict(row)
        card = cards.get(row.event_id)
        item['editable'] = bool(card and card.source == 'private')
        if card:
            item['surname'] = card.surname
        entries.append(item)
    # Cash entered through the bot before cabinet access was granted belongs
    # to the same Telegram account. Never infer ownership from a display name.
    legacy_cash = db.scalars(select(CashEntry).where(CashEntry.brigadier_tg_id == user.id,
        CashEntry.work_date.between(start, end)).order_by(CashEntry.work_date, CashEntry.id)).all()
    entries.extend(cabinet_legacy_cash_dict(row) for row in legacy_cash)
    if not cabinet_can_cash(db, user.id):
        entries = []
    # Source-qualified keys keep independent rows with equal IDs/amounts distinct.
    entries.sort(key=lambda item: (item['work_date'], 0 if item['source'] == 'cabinet' else 1,
        item['event_id'] if item['source'] == 'cabinet' else item['cash_entry_id']))
    cash_days = {r['work_date']: {'work_date': r['work_date'], 'cash': 0, 'kickback': 0, 'orders': 0} for r in days}
    for entry in entries:
        day = cash_days[entry['work_date']]
        day['cash'] += entry['cash']
        day['kickback'] += entry['kickback']
        day['orders'] += 1
    period_totals = []
    for first, last in ((1, 15), (16, end.day)):
        period_days = days[first-1:last]
        period_cash = list(cash_days.values())[first-1:last]
        period_totals.append({'start': start.replace(day=first).isoformat(),
            'end': start.replace(day=last).isoformat(),
            'salary': sum(r['salary'] or 0 for r in period_days), 'tea': sum(r['tea'] or 0 for r in period_days),
            'allowance': sum(r['allowance'] for r in period_days), 'total': sum(r['total'] for r in period_days), 'cash': sum(r['cash'] for r in period_cash),
            'kickback': sum(r['kickback'] for r in period_cash), 'orders': sum(r['orders'] for r in period_cash)})
    return JSONResponse({'user_id': user.id, 'start': start.isoformat(), 'end': end.isoformat(),
        'has_allowance': has_allowance, 'days': days, 'cash_entries': entries, 'cash_days': list(cash_days.values()), 'period_totals': period_totals,
        'totals': {'salary': sum(r['salary'] or 0 for r in days), 'tea': sum(r['tea'] or 0 for r in days),
            'allowance': sum(r['allowance'] for r in days), 'total': sum(r['total'] for r in days), 'cash': sum(r['cash'] for r in entries),
            'kickback': sum(r['kickback'] for r in entries), 'orders': len(entries)}},
        headers={'Cache-Control':'private, no-store'})



@app.get('/api/bot/cabinet/cash-summary/{request_id}', dependencies=[Depends(_bot_key)])
def cabinet_cash_summary_data(request_id: str, db: Session = Depends(get_db)):
    command = db.get(CabinetCommand, request_id)
    if not command or command.status != 'running':
        raise HTTPException(409, 'Подсчёт не ожидает выполнения')
    payload = json.loads(command.payload)
    if payload.get('action') != 'cash_summary':
        raise HTTPException(409, 'Неверное действие')
    uid = command.tg_id
    if not cabinet_can_cash(db, uid):
        raise HTTPException(403, 'Ведение кассы отключено')
    if not _is_cabinet_owner(db, uid):
        grant = db.get(BrigadierCabinetAccess, uid)
        if not grant or payload.get('access_version') != grant.version:
            raise HTTPException(403, 'Доступ изменён')
    period = CabinetCashSummaryPeriod(**payload['values'])
    # Reuse the cabinet calculation, including source-qualified historical rows.
    finance = json.loads(cabinet_finance(date.fromisoformat(period.month+'-01'), db,
        TelegramUser(id=uid, first_name='')).body)
    total = finance['period_totals'][period.half-1]
    employee = db.scalar(select(Employee).where(Employee.tg_id == uid,
        Employee.active.is_(True), _visible_employee()).limit(1))
    parts = str(employee.full_name or '').split()
    label = ' '.join(str(x) for x in (parts[0] if parts else 'Бригадир', employee.metro, employee.height_cm) if x)
    money = lambda value: f'{int(value or 0):,}'.replace(',', ' ') + ' ₽'
    first, last = (date.fromisoformat(total[k]).strftime('%d.%m') for k in ('start','end'))
    text = (f'📊 <b>Подсчёт кассы</b>\n<b>{first} — {last}</b>\n'
        f'{escape(label)}\n\nЗаказов: {total["orders"]}\n'
        f'Касса (комиссия): <b>{money(total["cash"])}</b>\n'
        f'Откат отдельно: <b>{money(total["kickback"])}</b>\n\n'
        f'<b>Итого: {money(total["cash"]+total["kickback"])}</b>')
    return JSONResponse({'tg_id': uid, 'start': total['start'], 'end': total['end'],
        'cash': total['cash'], 'kickback': total['kickback'], 'total': total['cash']+total['kickback'], 'orders': total['orders'], 'text': text},
        headers={'Cache-Control': 'private, no-store'})


def _brigadier_access_html(html):
    def replace(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError('Cabinet access UI anchor missing: ' + old[:90])
        html = html.replace(old, new, 1)

    replace("throw new Error(msg || `Ошибка ${response.status}`);", """const error=new Error(msg || `Ошибка ${response.status}`);error.status=response.status;
    if((response.status===401||response.status===403)&&(state.bootstrap?.cabinet_only||state.bootstrap?.cabinet_kind==='assistant')&&(path.startsWith('/api/bootstrap')||path.startsWith('/api/owner/brigadier-cabinet')))cabinetAccessLost(error.message);
    throw error;""")
    replace('<h2>Доступ к диспетчерской</h2>', '<h2>Доступ к приложению</h2>')
    replace('Mini App доступна только владельцу и помощникам, чьи Telegram ID указаны в настройках сервера.', 'Если вы бригадир, попросите владельца подключить ваш кабинет в списке бригадиров.')
    replace('function updateOwnerCabinetNav(){', 'function updateOwnerCabinetNav(){\n  applyCabinetMode();')
    replace('function renderDates() {', "function renderDates() {\n  if(state.bootstrap?.cabinet_only)return;")
    replace('function renderSummary() {', "function renderSummary() {\n  if(state.bootstrap?.cabinet_only)return;")
    replace("if(!qs('#modal').classList.contains('hidden')) return;", "if(!qs('#modal').classList.contains('hidden')&&!state.bootstrap?.cabinet_only) return;")
    replace('await renderCurrentView();\n    if(state.currentOrder', "if(!qs('#modal').classList.contains('hidden'))return;\n    await renderCurrentView();\n    if(state.currentOrder")
    replace('          ${contactButtons(x)}\n        </div>', '          ${contactButtons(x)}${cabinetAccessButton(x)}\n        </div>')
    replace("wireContactButtons(qs('#employeesList'));", "wireContactButtons(qs('#employeesList'));wireCabinetAccessButtons(qs('#employeesList'));")
    replace("}</div></details>`;\n    }\n    function render(){", "}</div>${cabinetAccessButton(p)}</details>`;\n    }\n    function render(){")
    # Bind owner-only controls every time a directory search is rendered.
    replace("qs('#cabinetStaffSearch',root).oninput=render;", "root.addEventListener('click',event=>{const button=event.target.closest('[data-cabinet-grant]');if(button&&root.contains(button))changeCabinetAccess(button)});\n    qs('#cabinetStaffSearch',root).oninput=render;")
    replace("openOrder(Number(btn.dataset.cabinetOrder)).catch(e=>toast(e.message))", "openCabinetAssignedOrder(Number(btn.dataset.cabinetOrder)).catch(e=>toast(e.message))")
    replace('// Events\nqs(\'#cashBtn\')', r"""
function cabinetAccessLost(message){
  const uid=state.bootstrap?.user?.id;
  state.bootstrap=null;state.currentOrder=null;ownerCabinetData=null;ownerCabinetSignature='';ownerCabinetSelectedCard=null;ownerCabinetRequest++;dayRequest++;
  closeModal();closeDrawer();qs('#modal').innerHTML='';qs('#orderDrawer').innerHTML='';qs('#ownerCabinetContent').innerHTML='';
  if(uid)sessionStorage.removeItem('cabinet-pending:'+uid);
  fatal(message);
}
function applyCabinetMode(){
  const only=!!state.bootstrap?.cabinet_only;
  document.body.classList.toggle('cabinet-only',only);
  if(!only)return;
  const nav=qs('#ownerCabinetNav');nav.dataset.view='ownerCabinetView';
  qsa('.bottom-nav button').forEach(x=>x.classList.toggle('active',x===nav));
  qsa('.view').forEach(x=>x.classList.toggle('active',x.id==='ownerCabinetView'));
  qs('#dateStrip').innerHTML='';qs('#summary').innerHTML='';state.currentOrder=null;
}
function cabinetAccessButton(person){
  if(!state.bootstrap?.can_manage_cabinets||person.group_code!=='brigadier')return '';
  const access=person.cabinet_access;
  if(!access)return '<small class="cabinet-access-note">Для подключения нужна регистрация в боте</small>';
  if(access.owner)return '<small class="cabinet-access-note">Ваш кабинет подключён</small>';
  const kinds={assistant:'Помощник',cashier:'Кассовый бригадир',brigadier:'Обычный бригадир'};
  const attrs=`data-cabinet-grant="${Number(person.id)}" data-access-version="${Number(access.version)}"`;
  return `<div class="cabinet-access-control"><span class="${access.active?'is-connected':''}">${access.active?'● '+esc(kinds[access.kind]||kinds.brigadier):'Кабинет не подключён'}</span><select class="cabinet-role-select" aria-label="Тип личного кабинета">${Object.entries(kinds).map(([key,title])=>`<option value="${key}" ${key===(access.kind||'brigadier')?'selected':''}>${title}</option>`).join('')}</select><button type="button" class="cabinet-access-button" ${attrs} data-access-action="save">${access.active?'Сохранить тип':'Подключить кабинет'}</button>${access.active?`<button type="button" class="cabinet-access-button" ${attrs} data-access-action="disable">Отключить</button>`:''}</div>`;
}
function wireCabinetAccessButtons(root){qsa('[data-cabinet-grant]',root).forEach(button=>button.onclick=()=>changeCabinetAccess(button))}
async function changeCabinetAccess(button){
  if(!state.bootstrap?.can_manage_cabinets||button.disabled)return;
  const uid=state.bootstrap.user.id,id=Number(button.dataset.cabinetGrant),active=button.dataset.accessAction!=='disable',version=Number(button.dataset.accessVersion),label=button.textContent;
  const control=button.closest('.cabinet-access-control'),kind=qs('.cabinet-role-select',control).value;
  qsa('button,select',control).forEach(x=>x.disabled=true);button.textContent='Сохранение…';
  try{
    const result=await api(`/api/employees/${id}/cabinet-access`,{method:'PUT',body:JSON.stringify({active,version,kind})});
    if(state.bootstrap?.user?.id!==uid||!button.isConnected)return;
    const access=result.cabinet_access;
    // Keep search/filter rerenders in this modal consistent with the saved grant.
    button.dispatchEvent(new CustomEvent('cabinet-access-changed',{bubbles:true,detail:{id,access}}));
    control.outerHTML=cabinetAccessButton({id,group_code:'brigadier',cabinet_access:access});
    wireCabinetAccessButtons(document);
    toast(!access.active?'Доступ к кабинету отключён':access.kind==='brigadier'?'Кабинет обычного бригадира подключён.': 'Кабинет подключён. Прежняя касса доступна в таблице; новые записи также отправляются в кассовый чат.',5000);
  }catch(e){button.textContent=label;toast(e.message,5000)}finally{qsa('button,select',control).forEach(x=>x.disabled=false)}
}
async function openCabinetAssignedOrder(id){
  const uid=state.bootstrap?.user?.id;
  const order=await api(`/api/owner/brigadier-cabinet/orders/${id}`,{cache:'no-store'});
  if(uid!==state.bootstrap?.user?.id||!state.bootstrap?.can_use_owner_cabinet)return;
  const phone=p=>/^\+?[\d ()-]{7,22}$/.test(String(p||''))?`<a class="contact-btn" href="tel:${esc(p.replace(/[^+\d]/g,''))}">${esc(p)}</a>`:'';
  showModal('Назначенный заказ',`<div class="cabinet-assigned-info"><p>${esc(order.source_label)} · ${esc(order.work_date)} · ${esc(order.issue_time)}</p><h3>ум: ${esc(order.deceased_name||'Не указано')}</h3><p>${esc(order.route||'Маршрут не указан')}</p><p>${esc(order.category_label||'')} · ${Number(order.team_size)||0} чел.</p><p>Агент: ${esc(order.agent_name||'—')} ${phone(order.agent_phone)} ${phone(order.other_agent_phone)}</p><h4>Состав от диспетчера</h4>${(order.members||[]).map(p=>`<p>${esc(p.display_name)} ${phone(p.phone)}</p>`).join('')||'<p>Сотрудники пока не назначены</p>'}<p class="muted">Изменения состава вносит диспетчер.</p></div>`);
}
// Events
qs('#cashBtn')""")
    replace("const people=(data.items||[]).slice().sort", "root.addEventListener('cabinet-access-changed',event=>{const person=(data.items||[]).find(p=>p.id===event.detail.id);if(person)person.cabinet_access=event.detail.access});\n    const people=(data.items||[]).slice().sort")
    return html.replace('</head>', r'''
<style id="cabinet-access-compact-style">
#modal>.modal-head{position:sticky;top:16px;z-index:20;background:#111114;isolation:isolate}
#modal>.modal-head::before{content:"";position:absolute;top:-16px;left:-16px;right:-16px;bottom:0;z-index:-1;background:#111114;pointer-events:none}
#modal>.modal-head .close-btn{flex-shrink:0}
@media(max-width:600px){
 #modal:has(#personalGrid)>.modal-head{top:12px}
 #modal:has(#personalGrid)>.modal-head::before{top:-12px;left:-8px;right:-8px}
}
.owner-cabinet-open .topbar{background:var(--bg);padding:10px 14px 8px;border-bottom:1px solid var(--line)}
.owner-cabinet-open .topbar h1{font-size:20px}.owner-cabinet-open .eyebrow{font-size:9px}
.owner-cabinet-open .topbar .icon-btn{width:36px;height:36px;border-radius:11px;font-size:19px}
#cabinetFinance .cabinet-back{font-size:12px;min-height:32px;margin:0 0 4px}
#cabinetFinance h2{font-size:19px;margin:8px 0 10px}
.cabinet-finance-month{font-size:12px;gap:8px}
.cabinet-finance-month input{min-height:34px;padding:4px 8px;font-size:14px;border-radius:10px;max-width:190px}
.cabinet-finance-tabs{gap:4px;margin:10px 0;padding:3px;border-radius:11px}
.cabinet-finance-tabs button{min-height:34px;padding:6px;font-size:13px;border-radius:8px}
.cabinet-finance-hint{font-size:11px;line-height:1.4;margin:8px 0 10px}
.cabinet-earnings-table th,.cabinet-earnings-table td{padding:1px 2px;font-size:12px;height:28px}
.cabinet-earnings-table th:first-child{font-size:11px}
.cabinet-earnings-table input{height:24px;min-height:24px;padding:1px 3px;border-radius:5px;font-size:13px;line-height:20px}
@supports(-webkit-touch-callout:none){.cabinet-earnings-table input{font-size:16px;letter-spacing:-.8px}}
.cabinet-earnings-table .cabinet-period-total th,.cabinet-earnings-table .cabinet-period-total td{padding:5px 2px;font-size:11px}
.cabinet-earnings-table .cabinet-period-total small{font-size:9px;margin-top:2px}
.cabinet-earnings-table tfoot th,.cabinet-earnings-table tfoot td{font-size:11px;padding:6px 2px}
.cabinet-cash-table [data-cash-row] th,.cabinet-cash-table [data-cash-row] td{height:28px}
.cabinet-cash-table thead th:nth-child(n+2),.cabinet-cash-table tbody tr:not(.cabinet-cash-details) td,.cabinet-cash-table tfoot td{text-align:center}
.cabinet-cash-table tr>:nth-child(3){border-left:1px solid var(--line)}
.cabinet-finance-scroll{max-height:60vh;max-height:60dvh;overflow:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;isolation:isolate;border-radius:6px}
.cabinet-finance-scroll:focus-visible{outline:1px solid var(--gold);outline-offset:2px}
.cabinet-finance-scroll .cabinet-earnings-table{border-collapse:separate;border-spacing:0}
.cabinet-finance-scroll thead th{position:sticky;top:0;z-index:3;background:var(--panel2);box-shadow:0 1px 0 var(--line);height:30px}
.cabinet-finance-scroll .cabinet-earnings-table th:first-child{position:sticky;left:0;z-index:2;background:var(--bg);box-shadow:1px 0 0 var(--line)}
.cabinet-finance-scroll .cabinet-earnings-table thead th:first-child{z-index:4;background:var(--panel2)}
.cabinet-finance-scroll .cabinet-period-total th:first-child{background:#211d14}
.cabinet-finance-scroll .cabinet-earnings-table thead th{font-size:11px}
.cabinet-cash-date{min-height:26px}.cabinet-my-orders{scroll-margin-top:80px}
.cabinet-access-control{margin-top:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cabinet-access-control span,.cabinet-access-note{font-size:11px;color:var(--muted)}
.cabinet-access-control .is-connected{color:var(--green)}
.cabinet-access-button{min-height:34px;padding:6px 10px;font-size:12px;border-radius:8px;border:1px solid var(--gold2);color:var(--gold);background:var(--panel2)}
.cabinet-access-button:disabled{opacity:.5}.cabinet-directory-person>.cabinet-access-control{margin:0;padding:0 13px 13px}
.cabinet-only .bottom-nav{display:none}
body.cabinet-only{padding-bottom:var(--safe-bottom)}
</style></head>''', 1)


INLINE_INDEX_HTML = _brigadier_access_html(INLINE_INDEX_HTML)

# Native cash is edited through its order so its version and Telegram mirror
# remain consistent. Never route its source-qualified ID to legacy cash APIs.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(
    '<button class="contact-btn" data-cash-edit="${x.id}">✏️ Изменить</button>\n            <button class="contact-btn danger" data-cash-del="${x.id}">Удалить</button>',
    '${x.source===\'cabinet\'?\'<small class="muted">Из кабинета бригадира</small>\':`<button class="contact-btn" data-cash-edit="${x.id}">✏️ Изменить</button><button class="contact-btn danger" data-cash-del="${x.id}">Удалить</button>`}')


def _four_cabinets_html(html):
    def replace(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError('Four cabinet UI anchor missing: ' + old[:90])
        html = html.replace(old, new, 1)
    replace('function applyCabinetMode(){', 'function applyCabinetMode(){\n  updateRoleNavigation();')
    replace("if(!state.bootstrap?.is_owner){toast('Касса доступна только владельцу');return}",
        "if(!state.bootstrap?.can_manage_cabinets){toast('Общая касса доступна только владельцу');return}")
    replace("prepared=(c.prepared??c.composition_closed),privateOrder=c.source==='private';",
        "prepared=(c.prepared??c.composition_closed),privateOrder=c.source==='private',cashRequired=privateOrder&&data.can_record_cash!==false;")
    replace("${privateOrder?stage('cash'", "${cashRequired?stage('cash'")
    replace("`${privateOrder?5:4}. Фотоотчёт`", "`${cashRequired?5:4}. Фотоотчёт`")
    replace("`${privateOrder?6:5}. Итоговый отчёт`", "`${cashRequired?6:5}. Итоговый отчёт`")
    html = html.replace("privateOrder&&!c.cash", "cashRequired&&!c.cash")
    replace("${card.source==='private'?'Далее заполните кассу.':'Далее — фото в день заказа.'}",
        "${card.source==='private'&&data.can_record_cash!==false?'Далее заполните кассу.':'Далее — фото в день заказа.'}")
    replace("    const amount=value=>value===null", "    if(state.bootstrap?.can_record_cash===false){qs('[data-finance-tab=\"cash\"]',panel)?.remove();qs('h2',panel).textContent='Мой заработок';tab='earnings'}\n    const amount=value=>value===null")
    replace('<span>Касса и заработок</span>', "<span>${data.can_record_cash===false?'Мой заработок':'Касса и заработок'}</span>")
    replace('// Events\nqs(\'#cashBtn\')', r'''
let roleNavigationIdentity='',roleNavigationActive='';
function roleNavigationItems(kind){
  const staff={key:'staff',title:'Сотрудники',icon:()=>cabinetIcon('team')};
  const readiness={key:'ready',title:'Готовность',icon:()=>cabinetIcon('check')};
  const earnings={key:'earnings',title:'Заработок',icon:()=>'<b class="role-ruble">₽</b>'};
  const cash={key:'cash',title:'Касса',icon:()=>cabinetIcon('wallet')};
  const orders={key:'orders',title:'Заказы',icon:()=>'<span>▤</span>'};
  if(kind==='owner')return [orders,staff,{key:'tables',title:'Мои таблицы',icon:()=>'<span>💰</span>'},readiness];
  if(kind==='assistant')return [orders,staff,cash,earnings,readiness];
  if(kind==='cashier')return [{...readiness,title:'Кабинет'},cash,earnings,staff];
  return [{...readiness,title:'Кабинет'},earnings,staff];
}
function updateRoleNavigation(){
  if(!state.bootstrap)return;
  const kind=state.bootstrap.cabinet_kind||(state.bootstrap.is_owner?'owner':state.bootstrap.cabinet_only?'brigadier':'assistant');
  const identity=state.bootstrap.user.id+':'+kind;
  document.body.classList.add('role-navigation-enabled');document.body.dataset.cabinetKind=kind;
  let nav=qs('#roleNavigation');
  if(!nav){nav=document.createElement('nav');nav.id='roleNavigation';nav.className='role-navigation';nav.setAttribute('aria-label','Разделы личного кабинета');document.body.append(nav)}
  if(identity!==roleNavigationIdentity){
    roleNavigationIdentity=identity;roleNavigationActive='ready';
    const items=roleNavigationItems(kind);nav.style.setProperty('--role-columns',items.length);
    nav.innerHTML=items.map(item=>`<button type="button" data-role-nav="${item.key}" aria-label="${item.title}"><span class="role-nav-icon">${item.icon()}</span><small>${item.title}</small></button>`).join('');
    qsa('button',nav).forEach(button=>button.onclick=()=>roleNavigate(button.dataset.roleNav).catch(e=>toast(e.message)));
  }
  qsa('button',nav).forEach(button=>{const selected=button.dataset.roleNav===roleNavigationActive;button.classList.toggle('active',selected);button.setAttribute('aria-current',selected?'page':'false')});
  let own=qs('#roleOwnOrders');
  if(!own){own=document.createElement('button');own.id='roleOwnOrders';own.type='button';own.className='cabinet-secondary role-own-orders';own.textContent='Мои заказы и готовность';qs('main').prepend(own);own.onclick=()=>roleOpenOwn().catch(e=>toast(e.message))}
  own.hidden=!state.bootstrap.can_use_owner_cabinet||kind!=='assistant'||currentViewId()==='ownerCabinetView';
}
async function roleActivateView(id){
  closeModal();closeDrawer();
  qsa('.bottom-nav button').forEach(button=>button.classList.toggle('active',button.dataset.view===id));
  qsa('.view').forEach(view=>view.classList.toggle('active',view.id===id));
  await renderCurrentView();
}
async function roleOpenOwn(){
  if(!state.bootstrap?.can_use_owner_cabinet){toast('Сначала подключите ваш профиль бригадира');return}
  ownerCabinetSelectedCard=null;ownerCabinetSignature='';qs('#ownerCabinetContent').innerHTML='';await roleActivateView('ownerCabinetView');
}
async function roleNavigate(target){
  const kind=state.bootstrap?.cabinet_kind;
  if(!roleNavigationItems(kind).some(item=>item.key===target))return;
  const finance=qs('#cabinetFinance');if(finance?.cabinetFinanceLeave&&!await finance.cabinetFinanceLeave())return;
  roleNavigationActive=target;updateRoleNavigation();
  const dispatcher=['owner','assistant'].includes(kind);
  if(target==='orders'){await roleActivateView('ordersView');return}
  if(target==='staff'){
    if(dispatcher){await roleActivateView('employeesView');return}
    await roleOpenOwn();await openCabinetEmployees();return;
  }
  if(target==='ready'){if(dispatcher)await roleActivateView('readinessView');else await roleOpenOwn();return}
  if(target==='tables'){closeModal();await openPersonalTables();return}
  await roleOpenOwn();
  await openCabinetFinance(state.selectedDate,target==='cash'?'cash':'earnings');
}
// Events
qs('#cashBtn')''')
    return html.replace('</head>', '''<style id="four-cabinet-style">
body[data-cabinet-kind="cashier"] .cabinet-personal-links,body[data-cabinet-kind="owner"] .cabinet-personal-links{display:none!important}
body.role-navigation-enabled{padding-bottom:calc(70px + var(--safe-bottom))}
.role-navigation-enabled .bottom-nav,.role-navigation-enabled #cashBtn{display:none!important}
.role-navigation{display:grid;grid-template-columns:repeat(var(--role-columns),minmax(0,1fr));position:fixed;z-index:30;left:0;right:0;bottom:0;height:calc(64px + var(--safe-bottom));padding-bottom:var(--safe-bottom);border-top:1px solid var(--line);background:var(--bg)}
.role-navigation button{min-width:0;border:0;background:transparent;color:var(--muted);padding:7px 1px;cursor:pointer}
.role-navigation button.active{color:var(--gold)}.role-nav-icon{height:27px;display:flex;align-items:center;justify-content:center;font-size:23px}.role-nav-icon svg{width:23px;height:23px}.role-ruble{font-size:26px;font-weight:500}
.role-navigation small{display:block;font-size:10px;font-weight:600;white-space:nowrap}
.role-own-orders{margin:4px 0 12px;font-size:13px}.role-own-orders[hidden]{display:none}
.cabinet-role-select{max-width:210px;min-height:34px;padding:4px 6px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--text);font-size:12px}
</style></head>''',1)


INLINE_INDEX_HTML = _four_cabinets_html(INLINE_INDEX_HTML)


class OwnerManagedBlock(Base):
    __tablename__ = 'owner_managed_blocks'
    tg_id = Column(BigInteger, primary_key=True)


class OwnerEmployeeGroup(Base):
    __tablename__ = 'owner_employee_groups'
    employee_id = Column(Integer, primary_key=True)
    group_code = Column(String(20), nullable=False)


class OwnerBotSnapshot(Base):
    __tablename__ = 'owner_bot_snapshot'
    id = Column(Integer, primary_key=True)
    payload = Column(String, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class OwnerManagementCommand(Base):
    __tablename__ = 'owner_management_commands'
    id = Column(String(32), primary_key=True)
    owner_id = Column(BigInteger, nullable=False)
    payload = Column(String, nullable=False)
    status = Column(String(20), nullable=False, default='queued')
    result = Column(String, nullable=False, default='{}')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    lease_until = Column(DateTime, nullable=True)


class OwnerBotOptions(BaseModel):
    first_summary: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    second_summary: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    automatic_summaries: StrictBool
    reminder_minutes: StrictInt = Field(ge=1, le=240)
    cash_summary_hour: StrictInt = Field(ge=0, le=23)


class OwnerManagementInput(BaseModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    action: Literal['cash_connect','cash_disconnect','cash_check','block','unblock','settings','staff_sync','profile_sync']
    employee_id: StrictInt | None = None
    chat_id: StrictInt | None = None
    options: OwnerBotOptions | None = None


class OwnerGroupInput(BaseModel):
    group_code: Literal['brigadier','main','cashless','reserve']


def require_management_owner(db, user):
    if not _is_cabinet_owner(db, user.id):
        raise HTTPException(403, 'Управление доступно только владельцу')


@app.get('/api/owner/management')
def owner_management(db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    require_management_owner(db, user)
    snapshot = db.get(OwnerBotSnapshot, 1)
    data = json.loads(snapshot.payload) if snapshot else {}
    employees = list_employees(None, db, user)
    active_ids = {e['id'] for e in employees}
    for employee in db.scalars(select(Employee).where(_visible_employee(), Employee.tg_id.in_(select(OwnerManagedBlock.tg_id)))):
        if employee.id not in active_ids:
            employees.append({**_employee_dict(employee), 'cabinet_access':cabinet_access_info(db,employee)})
    return JSONResponse({'employees': employees, 'bot': data,
        'bot_online': bool(snapshot and datetime.utcnow()-snapshot.updated_at < timedelta(seconds=120)),
        'updated_at': snapshot.updated_at.isoformat() if snapshot else None},
        headers={'Cache-Control':'private, no-store'})


@app.post('/api/owner/management/actions')
def owner_management_submit(data: OwnerManagementInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    require_management_owner(db, user)
    previous = db.get(OwnerManagementCommand, data.request_id)
    payload = data.model_dump(exclude={'request_id'})
    if previous:
        original = json.loads(previous.payload)
        original.pop('tg_id', None)
        if previous.owner_id != user.id or original != payload:
            raise HTTPException(409, 'Запрос уже использован')
        return {'id':previous.id,'status':previous.status,**json.loads(previous.result)}
    if data.employee_id is not None:
        employee = db.scalar(select(Employee).where(Employee.id == data.employee_id, _visible_employee()))
        if not employee or not employee.tg_id or (not employee.active and not (data.action=='unblock' and db.get(OwnerManagedBlock,employee.tg_id))):
            raise HTTPException(409, 'Нужен зарегистрированный активный сотрудник')
        payload['tg_id'] = int(employee.tg_id)
        if data.action in {'block','unblock'} and employee.tg_id == user.id:
            raise HTTPException(409, 'Свой аккаунт заблокировать нельзя')
        if data.action.startswith('cash_') and (employee.group_code != 'brigadier' or not cabinet_can_cash(db, employee.tg_id)):
            raise HTTPException(409, 'Сначала назначьте кассовый кабинет бригадиру')
    elif data.action in {'cash_connect','cash_disconnect','cash_check','block','unblock','profile_sync'}:
        raise HTTPException(422, 'Выберите сотрудника')
    if data.action == 'cash_connect' and (data.chat_id is None or data.chat_id >= 0):
        raise HTTPException(422, 'Выберите групповой чат кассы')
    if data.action == 'settings':
        if data.options is None or data.options.first_summary >= data.options.second_summary:
            raise HTTPException(422, 'Вторая сводка должна быть позже первой')
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    snapshot = db.get(OwnerBotSnapshot, 1)
    if not snapshot or datetime.utcnow()-snapshot.updated_at >= timedelta(seconds=120):
        raise HTTPException(409, 'Бот не на связи. Проверьте запуск bot.py и повторите позже')
    db.add(OwnerManagementCommand(id=data.request_id, owner_id=user.id, payload=encoded))
    try:db.commit()
    except IntegrityError:
        db.rollback()
        previous = db.get(OwnerManagementCommand, data.request_id)
        if not previous or previous.owner_id != user.id or previous.payload != encoded:
            raise HTTPException(409, 'Конфликт повторного запроса')
        return {'id':previous.id,'status':previous.status,**json.loads(previous.result)}
    return {'id':data.request_id,'status':'queued'}


@app.get('/api/owner/management/actions/{request_id}')
def owner_management_status(request_id: str, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    require_management_owner(db, user)
    command = db.get(OwnerManagementCommand, request_id)
    if not command or command.owner_id != user.id:
        raise HTTPException(404, 'Действие не найдено')
    return JSONResponse({'id':command.id,'status':command.status,**json.loads(command.result)},headers={'Cache-Control':'no-store'})


@app.put('/api/owner/management/employees/{employee_id}/group')
def owner_employee_group(employee_id: int, data: OwnerGroupInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    require_management_owner(db, user)
    employee = db.scalar(select(Employee).where(Employee.id == employee_id, Employee.active.is_(True), _visible_employee()))
    if not employee or employee.tg_id == user.id:
        raise HTTPException(409, 'Этот состав изменить нельзя')
    override = db.get(OwnerEmployeeGroup, employee_id)
    if override is None:db.add(OwnerEmployeeGroup(employee_id=employee_id,group_code=data.group_code))
    else:override.group_code=data.group_code
    employee.group_code=data.group_code
    db.commit()
    return {'ok':True}


@app.post('/api/owner/management/employees')
def owner_employee_add(data: EmployeeSync, db: Session = Depends(get_db), user: TelegramUser = Depends(require_app_user)):
    require_management_owner(db, user)
    if not data.full_name.strip() or not data.metro.strip() or not data.phone.strip():
        raise HTTPException(422, 'Укажите имя, метро и телефон')
    if data.tg_id is not None and (data.tg_id <= 0 or db.scalar(select(Employee.id).where(Employee.tg_id == data.tg_id)) is not None):
        raise HTTPException(409, 'Этот Telegram-аккаунт уже есть в списке или ID неверный')
    if data.tg_id in HIDDEN_TG_IDS or (data.tg_id and db.get(OwnerManagedBlock, data.tg_id)):
        raise HTTPException(409, 'Этот аккаунт исключён или заблокирован')
    if not 7 <= len(_reference_phone(data.phone)) <= 15:
        raise HTTPException(422, 'Укажите корректный телефон')
    if any(_reference_phone(e.phone) == _reference_phone(data.phone) for e in db.scalars(select(Employee))):
        raise HTTPException(409, 'Этот телефон уже есть в списке')
    employee=Employee(tg_id=data.tg_id,full_name=data.full_name.strip(),metro=data.metro.strip(),
        phone=brigadier_phone8(data.phone),telegram_username=data.telegram_username.strip().lstrip('@'),
        group_code=data.group_code,height_cm=data.height_cm,has_car=data.has_car,is_cashier=False,active=True)
    db.add(employee);db.commit()
    return _employee_dict(employee)


@app.post('/api/bot/owner-management/snapshot', dependencies=[Depends(_bot_key)])
def owner_bot_snapshot(data: dict, db: Session = Depends(get_db)):
    config=db.get(OwnerCabinetConfig,1)
    if not config or data.get('owner_id') != config.owner_tg_id:
        raise HTTPException(403,'Владелец изменён')
    encoded=json.dumps(data,ensure_ascii=False)
    if len(encoded)>500000:raise HTTPException(413,'Слишком много настроек')
    snapshot=db.get(OwnerBotSnapshot,1)
    if snapshot is None:db.add(OwnerBotSnapshot(id=1,payload=encoded))
    else:snapshot.payload=encoded;snapshot.updated_at=datetime.utcnow()
    blocked={int(x['tg_id']) for x in data.get('blocked',[]) if int(x['tg_id']) != config.owner_tg_id}
    for row in db.scalars(select(OwnerManagedBlock)):
        if row.tg_id not in blocked:db.delete(row)
    for uid in blocked:
        if db.get(OwnerManagedBlock,uid) is None:db.add(OwnerManagedBlock(tg_id=uid))
    db.commit();return {'ok':True}


@app.post('/api/bot/owner-management/claim', dependencies=[Depends(_bot_key)])
def owner_management_claim(db: Session = Depends(get_db)):
    now=datetime.utcnow()
    command=db.scalar(select(OwnerManagementCommand).where(or_(OwnerManagementCommand.status=='queued',
        and_(OwnerManagementCommand.status=='running',OwnerManagementCommand.lease_until<now)))
        .order_by(OwnerManagementCommand.created_at).with_for_update(skip_locked=True).limit(1))
    if not command:return {'job':None}
    if not _is_cabinet_owner(db,command.owner_id):
        command.status='error';command.result=json.dumps({'error':'Владелец изменён'});db.commit();return {'job':None}
    command.status='running';command.lease_until=now+timedelta(minutes=5);db.commit()
    return {'job':{'id':command.id,'owner_id':command.owner_id,**json.loads(command.payload)}}


@app.post('/api/bot/owner-management/result', dependencies=[Depends(_bot_key)])
def owner_management_finish(data: dict, db: Session = Depends(get_db)):
    command=db.get(OwnerManagementCommand,str(data.get('id','')))
    if not command or data.get('status') not in {'done','error'}:raise HTTPException(422,'Неизвестное действие')
    if command.status in {'done','error'}:return {'ok':True}
    payload=json.loads(command.payload)
    if data['status']=='done' and payload.get('action')=='unblock' and _is_cabinet_owner(db,command.owner_id):
        employee=db.get(Employee,payload.get('employee_id'))
        if employee and employee.tg_id==payload.get('tg_id') and employee.tg_id not in HIDDEN_TG_IDS:
            employee.active=True
    command.status=data['status'];command.result=json.dumps(data.get('result') or {},ensure_ascii=False)
    command.lease_until=None;db.commit();return {'ok':True}


@app.get('/api/bot/owner-management/employees/{employee_id}', dependencies=[Depends(_bot_key)])
def owner_bot_employee(employee_id: int, db: Session = Depends(get_db)):
    employee=db.scalar(select(Employee).where(Employee.id==employee_id,_visible_employee()))
    if not employee:raise HTTPException(404,'Сотрудник не найден')
    return _employee_dict(employee)


def _owner_management_html(html):
    def replace(old,new):
        nonlocal html
        if html.count(old)!=1:raise ValueError('Owner settings UI anchor missing: '+old[:90])
        html=html.replace(old,new,1)
    replace("if(kind==='owner')return [orders,staff,{key:'tables',title:'Мои таблицы',icon:()=>'<span>💰</span>'},readiness];",
        "if(kind==='owner')return [{...readiness,title:'Заказы'},staff,{key:'tables',title:'Мои таблицы',icon:()=>'<span>💰</span>'},{key:'own',title:'Мой кабинет',icon:()=>cabinetIcon('user')},{key:'management',title:'Управление',icon:()=>'<span>⚙</span>'}];")
    replace("if(target==='orders'){await roleActivateView('ordersView');return}",
        "if(target==='own'){await roleOpenOwn();return}\n  if(target==='management'){await roleActivateView('ownerManagementView');return}\n  if(target==='orders'){await roleActivateView('ordersView');return}")
    replace('  const v = currentViewId();', """  const v = currentViewId();
  document.body.classList.toggle('management-open',v==='ownerManagementView');
  if(v==='ownerManagementView'){document.body.classList.remove('owner-cabinet-open');qs('.topbar h1').textContent='Управление';return renderOwnerManagement()}""")
    replace('<section id="controlView" class="view">','<section id="ownerManagementView" class="view"><div id="ownerManagementContent"></div></section>\n<section id="controlView" class="view">')
    replace('    </nav>','      <button data-view="ownerManagementView" hidden aria-hidden="true"></button>\n    </nav>')
    replace('async function autoRefresh(){\n  if(document.hidden) return;', "async function autoRefresh(){\n  if(document.hidden||currentViewId()==='ownerManagementView') return;")
    replace('// Events\nqs(\'#cashBtn\')', r'''
let ownerManagementSection='home',ownerManagementRequest=0;
const ownerGroupLabels={brigadier:'Бригадиры',main:'Основной состав',cashless:'Безнал состав',reserve:'Резерв'};
async function ownerManagementAction(action,employeeId=null,extras={}){
  if(!state.bootstrap?.can_manage_cabinets)throw Error('Только владелец');
  const uid=state.bootstrap.user.id,storage='owner-management-pending:'+uid;
  if(sessionStorage.getItem(storage))throw Error('Сначала нажмите «Проверить последнее действие»');
  const request_id=crypto.randomUUID().replace(/-/g,'');
  const intent={request_id,action,...(employeeId===null?{}:{employee_id:Number(employeeId)}),...extras};
  // Keep intent across a lost HTTP response; a retry never repeats a accepted action.
  sessionStorage.setItem(storage,JSON.stringify(intent));
  let result;
  try{result=await api('/api/owner/management/actions',{method:'POST',body:JSON.stringify(intent)})}
  catch(e){if(e.status>=400&&e.status<500)sessionStorage.removeItem(storage);throw e}
  for(let attempt=0;attempt<90;attempt++){
    if(state.bootstrap?.user?.id!==uid||!state.bootstrap.can_manage_cabinets)throw Error('Доступ изменён');
    const status=attempt===0?result:await api('/api/owner/management/actions/'+request_id,{cache:'no-store'});
    if(['done','error'].includes(status.status)){
      sessionStorage.removeItem(storage);if(status.status==='error')throw Error(status.error||'Действие не выполнено');return status;
    }
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw Error('Действие ещё выполняется. Нажмите «Проверить последнее действие»; повторно отправлять не нужно.');
}
async function ownerManagementPending(){
  const uid=state.bootstrap?.user?.id,storage='owner-management-pending:'+uid,raw=sessionStorage.getItem(storage);
  if(!raw)return;
  const intent=JSON.parse(raw);
  let status;
  try{status=await api('/api/owner/management/actions',{method:'POST',body:JSON.stringify(intent)})}
  catch(e){if(e.status>=400&&e.status<500)sessionStorage.removeItem(storage);await renderOwnerManagement();throw e}
  if(['done','error'].includes(status.status)){sessionStorage.removeItem(storage);toast(status.error||status.message||'Сохранено',6000)}
  else toast('Действие ещё выполняется. Обновите через несколько секунд.',5000);
  await renderOwnerManagement();
}
async function renderOwnerManagement(){
  const root=qs('#ownerManagementContent'),uid=state.bootstrap?.user?.id,request=++ownerManagementRequest;
  if(!state.bootstrap?.can_manage_cabinets||state.bootstrap.cabinet_kind!=='owner'){root.textContent='Раздел доступен только владельцу';return}
  root.innerHTML='<p class="muted">Загрузка настроек…</p>';
  const data=await api('/api/owner/management',{cache:'no-store'});
  if(request!==ownerManagementRequest||currentViewId()!=='ownerManagementView'||state.bootstrap?.user?.id!==uid)return;
  const groups=ownerGroupLabels,people=data.employees||[],brigadiers=people.filter(p=>p.group_code==='brigadier'),cash=(data.bot.cash_chats||[]),blocked=new Set((data.bot.blocked||[]).map(p=>Number(p.tg_id)));
  const label=p=>[String(p.full_name||'').split(' ')[0],p.metro,p.height_cm].filter(Boolean).join(' ');
  const sections=[['access','Кабинеты и доступы','Подключить · сменить тип · отключить','lock'],['brigadiers','Бригадиры','Добавить · редактировать · назначить кассовым','team'],['cash','Бригадиры и чаты кассы','Подключение чатов и проверка связи','wallet'],['staff','Сотрудники и группы','Добавить · перенести · заблокировать','team'],['bot','Настройки бота','Сверки · уведомления · служебные действия','control']];
  const heading=sections.find(x=>x[0]===ownerManagementSection)?.[1]||'Управление';
  root.innerHTML=`${ownerManagementSection==='home'?'':`<button class="cabinet-back" id="managementBack">${cabinetIcon('back')}Управление</button>`}<div class="management-title"><h2>${esc(heading)}</h2><small>${cabinetIcon('lock')} Только вы</small></div><p class="muted management-status">${data.bot_online?'Бот на связи':'Нет связи с ботом — проверьте запуск bot.py'}</p>${sessionStorage.getItem('owner-management-pending:'+uid)?'<button class="cabinet-secondary" id="managementPending">Проверить последнее действие</button>':''}<div id="managementBody"></div>`;
  if(qs('#managementBack'))qs('#managementBack').onclick=()=>{ownerManagementSection='home';renderOwnerManagement().catch(e=>toast(e.message))};
  if(qs('#managementPending'))qs('#managementPending').onclick=()=>ownerManagementPending().catch(e=>toast(e.message,6000));
  const body=qs('#managementBody');
  const perform=async(button,action,eid=null,extras={})=>{if(button.disabled)return;button.disabled=true;const text=button.textContent;button.textContent='Выполняется…';try{const result=await ownerManagementAction(action,eid,extras);toast(result.message||'Сохранено',6000);if(currentViewId()==='ownerManagementView')await renderOwnerManagement();return true}catch(e){toast(e.message,6000);if(currentViewId()==='ownerManagementView'&&sessionStorage.getItem('owner-management-pending:'+uid)&&!qs('#managementPending')){const pending=document.createElement('button');pending.id='managementPending';pending.className='cabinet-secondary';pending.textContent='Проверить последнее действие';pending.onclick=()=>ownerManagementPending().catch(e=>toast(e.message,6000));body.before(pending)}return false}finally{if(button.isConnected){button.disabled=false;button.textContent=text}}};
  if(ownerManagementSection==='home'){
    body.innerHTML=`<p class="muted">Все настройки и действия в одном месте</p><div class="management-menu">${sections.map(([key,title,subtitle,icon])=>`<button type="button" data-management-section="${key}">${cabinetIcon(icon)}<span><b>${title}</b><small>${subtitle}</small></span>${cabinetIcon('chevron')}</button>`).join('')}</div>`;
    qsa('[data-management-section]',body).forEach(button=>button.onclick=()=>{ownerManagementSection=button.dataset.managementSection;renderOwnerManagement().catch(e=>toast(e.message))});return;
  }
  if(['access','brigadiers','staff'].includes(ownerManagementSection)){
    const directory=ownerManagementSection==='staff'?people:brigadiers;
    body.innerHTML=`${ownerManagementSection==='access'?'':'<button class="cabinet-primary" id="managementAdd">＋ Добавить сотрудника</button>'}<input id="managementSearch" class="search" placeholder="Поиск по имени и метро"><div id="managementPeople"></div>`;
    function draw(){
      const search=qs('#managementSearch').value.toLocaleLowerCase('ru');
      qs('#managementPeople').innerHTML=directory.filter(p=>label(p).toLocaleLowerCase('ru').includes(search)).map(p=>`<article class="management-person"><h3>${esc(label(p))}</h3><p>${esc(groups[p.group_code])}${blocked.has(Number(p.tg_id))?' · 🚫 Заблокирован':''}</p>${ownerManagementSection==='access'?cabinetAccessButton(p):`<div class="management-actions"><button class="cabinet-secondary" data-management-edit="${p.id}">Изменить</button>${p.tg_id&&p.group_code==='brigadier'?`<button class="cabinet-secondary" data-management-sync="${p.id}">Синхронизировать профиль</button>`:''}${ownerManagementSection==='staff'&&p.tg_id!==uid?`<button class="cabinet-secondary" data-management-move="${p.id}">Изменить состав</button>${p.tg_id?`<button class="cabinet-secondary" data-management-block="${p.id}" data-is-blocked="${blocked.has(Number(p.tg_id))?'1':'0'}">${blocked.has(Number(p.tg_id))?'Снять блокировку':'Заблокировать'}</button>`:''}`:''}</div>${cabinetAccessButton(p)}`}</article>`).join('')||'<p class="muted">Сотрудники не найдены</p>';
      wireCabinetAccessButtons(qs('#managementPeople'));
      qsa('[data-management-edit]',body).forEach(button=>button.onclick=()=>{const p=directory.find(x=>x.id===Number(button.dataset.managementEdit));openEmployeeProfile(p,()=>renderOwnerManagement())});
      qsa('[data-management-sync]',body).forEach(button=>button.onclick=()=>perform(button,'profile_sync',Number(button.dataset.managementSync)));
      qsa('[data-management-block]',body).forEach(button=>button.onclick=()=>{const block=button.dataset.isBlocked!=='1';if(confirm(block?'Заблокировать доступ сотрудника к боту и кабинету? Из Telegram-групп он не удаляется.':'Снять блокировку сотрудника?'))perform(button,block?'block':'unblock',Number(button.dataset.managementBlock))});
      qsa('[data-management-move]',body).forEach(button=>button.onclick=()=>{
        const p=directory.find(x=>x.id===Number(button.dataset.managementMove));
        showModal('Изменить состав',`<form id="managementMoveForm" class="cabinet-native-form"><p>${esc(label(p))}</p><label>Состав в списке<select name="group_code">${Object.entries(groups).map(([key,title])=>`<option value="${key}" ${p.group_code===key?'selected':''}>${title}</option>`).join('')}</select></label><p class="muted">Меняет состав в учёте приложения. В Telegram-группу сотрудник вступает отдельно.</p><button class="cabinet-primary">Сохранить</button></form>`);
        qs('#managementMoveForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget;qs('button',form).disabled=true;try{await api('/api/owner/management/employees/'+p.id+'/group',{method:'PUT',body:JSON.stringify({group_code:new FormData(form).get('group_code')})});closeModal();await renderOwnerManagement()}catch(e){toast(e.message);qs('button',form).disabled=false}};
      });
    }
    qs('#managementSearch').oninput=draw;draw();
    body.addEventListener('cabinet-access-changed',event=>{const p=directory.find(x=>x.id===event.detail.id);if(p)p.cabinet_access=event.detail.access});
    if(qs('#managementAdd'))qs('#managementAdd').onclick=()=>ownerManagementAdd(ownerManagementSection==='brigadiers'?'brigadier':'main');
    return;
  }
  if(ownerManagementSection==='cash'){
    const cashiers=brigadiers.filter(p=>p.cabinet_access?.active&&['assistant','cashier','owner'].includes(p.cabinet_access.kind));
    body.innerHTML=`<p class="muted">Кассовых бригадиров: ${cashiers.length} · Подключённых чатов: ${cash.length}</p><p class="muted">Добавьте бота в отдельную группу кассы — она появится в выборе чата. При замене новые записи пойдут в новый чат; прежние сообщения останутся в старом.</p>${cashiers.map(p=>{const chat=cash.find(c=>Number(c.brigadier_tg_id)===Number(p.tg_id));return `<article class="management-person"><h3>${esc(label(p))}</h3><p>${esc(chat?.title||'Чат не подключён')}${chat?' · '+esc(chat.chat_id):''}</p><div class="management-actions"><button class="cabinet-secondary" data-management-connect="${p.id}">${chat?'Заменить чат':'Подключить чат'}</button>${chat?`<button class="cabinet-secondary" data-management-check="${p.id}">Проверить связь</button><button class="cabinet-secondary" data-management-disconnect="${p.id}">Отключить чат</button>`:''}</div></article>`}).join('')||'<p>Сначала назначьте кассовый кабинет в разделе «Кабинеты и доступы».</p>'}`;
    qsa('[data-management-connect]',body).forEach(button=>button.onclick=()=>{
      showModal('Подключить чат кассы',`<form id="managementCashForm" class="cabinet-native-form"><label>Группа<select name="known_chat"><option value="">Выберите чат</option>${(data.bot.known_chats||[]).map(c=>`<option value="${c.chat_id}">${esc(c.title)} · ${c.chat_id}</option>`).join('')}</select></label><label>Или ID группы<input name="chat_id" inputmode="numeric" placeholder="−100…"></label><p class="muted">Бот должен состоять в группе, а вы — быть её администратором.</p><button class="cabinet-primary">Подключить</button></form>`);
      qs('#managementCashForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,values=new FormData(form),id=Number(values.get('chat_id')||values.get('known_chat'));if(!Number.isSafeInteger(id)||id>=0){toast('Выберите групповой чат');return}const ok=await perform(qs('button',form),'cash_connect',Number(button.dataset.managementConnect),{chat_id:id});if(ok&&currentViewId()==='ownerManagementView')closeModal()};
    });
    qsa('[data-management-check]',body).forEach(button=>button.onclick=()=>perform(button,'cash_check',Number(button.dataset.managementCheck)));
    qsa('[data-management-disconnect]',body).forEach(button=>button.onclick=()=>{if(confirm('Отключить чат? История кассы останется в таблице.'))perform(button,'cash_disconnect',Number(button.dataset.managementDisconnect))});return;
  }
  const options=data.bot.options||{first_summary:'14:00',second_summary:'18:00',automatic_summaries:true,reminder_minutes:10,cash_summary_hour:23};
  body.innerHTML=`<form id="managementBotForm" class="cabinet-native-form"><label>Первая автоматическая сводка<input type="time" name="first_summary" value="${esc(options.first_summary)}" required></label><label>Вторая автоматическая сводка<input type="time" name="second_summary" value="${esc(options.second_summary)}" required></label><label><input type="checkbox" name="automatic_summaries" ${options.automatic_summaries?'checked':''}> Автоматические сводки включены</label><label>Напоминание о новом заказе, минут<input type="number" name="reminder_minutes" min="1" max="240" value="${options.reminder_minutes}" required></label><label>Час итогов кассы за половину месяца<input type="number" name="cash_summary_hour" min="0" max="23" value="${options.cash_summary_hour}" required></label><p class="muted">Время московское. Настройки сохраняются после перезапуска бота.</p><button class="cabinet-primary" ${data.bot_online?'':'disabled'}>Сохранить настройки</button></form><button class="cabinet-secondary management-sync" id="managementSync">Синхронизировать сотрудников, заказы и кассу</button>`;
  qs('#managementBotForm').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,values=new FormData(form),settings={first_summary:values.get('first_summary'),second_summary:values.get('second_summary'),automatic_summaries:values.has('automatic_summaries'),reminder_minutes:Number(values.get('reminder_minutes')),cash_summary_hour:Number(values.get('cash_summary_hour'))};await perform(qs('button',form),'settings',null,{options:settings})};
  qs('#managementSync').onclick=()=>perform(qs('#managementSync'),'staff_sync');
}
function ownerManagementAdd(group){
  showModal('Добавить сотрудника',`<form id="managementAddForm" class="cabinet-native-form"><label>Имя<input name="full_name" required maxlength="100"></label><label>Метро<input name="metro" required maxlength="100"></label><label>Рост, см<input name="height_cm" type="number" min="150" max="220" required></label><label>Телефон<input name="phone" type="tel" required></label><label>Telegram username<input name="telegram_username" placeholder="Без @"></label><label>Telegram ID<input name="tg_id" inputmode="numeric" placeholder="Если известен"></label><label>Состав<select name="group_code">${Object.entries(ownerGroupLabels).map(([key,title])=>`<option value="${key}" ${key===group?'selected':''}>${title}</option>`).join('')}</select></label><p class="muted">Без Telegram ID сотрудник появится в списке. Для подключения кабинета нужен его зарегистрированный Telegram-аккаунт.</p><button class="cabinet-primary">Добавить</button></form>`);
  qs('#managementAddForm').onsubmit=async event=>{
    event.preventDefault();const form=event.currentTarget,values=new FormData(form),submit=qs('button',form);submit.disabled=true;
    const employee=Object.fromEntries(values);employee.height_cm=Number(employee.height_cm);employee.tg_id=employee.tg_id?Number(employee.tg_id):null;
    try{await api('/api/owner/management/employees',{method:'POST',body:JSON.stringify(employee)});closeModal();await renderOwnerManagement();toast('Сотрудник добавлен. Для бригадира с Telegram ID нажмите «Синхронизировать профиль».',6000)}catch(e){toast(e.message,6000);submit.disabled=false}
  };
}
// Events
qs('#cashBtn')''')
    return html.replace('</head>', '''<style id="owner-management-style">
.management-open #dateStrip,.management-open #summary,.management-open #roleOwnOrders{display:none!important}
#ownerManagementContent{max-width:680px;margin:auto;padding-bottom:16px}.management-title{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.management-title h2{font-size:24px;margin:12px 0}.management-title small{display:flex;gap:5px;color:var(--gold);align-items:center}.management-title small svg{width:16px;height:16px}
.management-status{font-size:12px}.management-menu{display:grid;gap:12px;margin-top:18px}.management-menu>button{display:flex;align-items:center;gap:14px;width:100%;padding:18px 14px;border:1px solid var(--line);border-radius:18px;background:var(--panel);color:var(--text);text-align:left}.management-menu>button>svg:first-child{width:30px;height:30px;color:var(--gold);flex-shrink:0}.management-menu>button>svg:last-child{margin-left:auto;flex-shrink:0}.management-menu b{display:block;font-size:16px}.management-menu small{display:block;color:var(--muted);font-size:12px;margin-top:6px}
.management-person{padding:14px;margin:12px 0;border:1px solid var(--line);border-radius:14px;background:var(--panel)}.management-person h3{font-size:16px;margin:0 0 6px}.management-person p{font-size:12px;color:var(--muted)}.management-actions{display:flex;gap:8px;flex-wrap:wrap}.management-actions button{font-size:12px}.management-sync{margin-top:16px;width:100%}#managementSearch{width:100%;margin:14px 0}
</style></head>''',1)


INLINE_INDEX_HTML = _owner_management_html(INLINE_INDEX_HTML)


def _assistant_month_earnings_html(html):
    def replace(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError('Assistant earnings UI anchor missing: ' + old[:90])
        html = html.replace(old, new, 1)
    replace("if(kind==='assistant')return [orders,staff,cash,earnings,readiness];",
            "if(kind==='assistant')return [{...readiness,title:'Заказы'},staff,cash,earnings,{key:'own',title:'Личный кабинет',icon:()=>cabinetIcon('user')}];")
    replace("own.hidden=!state.bootstrap.can_use_owner_cabinet||kind!=='assistant'||currentViewId()==='ownerCabinetView';", "own.hidden=true;")
    replace("const endDay=Number(data.end.slice(8));", "const endDay=Number(data.end.slice(8)),hasAllowance=data.has_allowance===true;panel.classList.add('compact-month');document.body.classList.add('compact-month-open');")
    replace("    const amount=value=>value===null", "    if(hasAllowance)qs('h2',panel).textContent='Заработок Влада';\n    const amount=value=>value===null")
    replace("let salary=0,tea=0;", "let salary=0,tea=0,allowance=0;")
    replace("const halves={1:{salary:0,tea:0},2:{salary:0,tea:0}};", "const halves={1:{salary:0,tea:0,allowance:0},2:{salary:0,tea:0,allowance:0}};")
    replace("const a=amount(row.salary),b=amount(row.tea);salary+=", "const a=amount(row.salary),b=amount(row.tea),extra=Number(row.allowance||0);allowance+=extra;salary+=")
    replace("half.salary+=Number.isFinite(a)?a:0;half.tea+=Number.isFinite(b)?b:0;", "half.salary+=Number.isFinite(a)?a:0;half.tea+=Number.isFinite(b)?b:0;half.allowance+=extra;")
    replace("cabinetMoney((a||0)+(b||0));", "cabinetMoney((a||0)+(b||0)+extra);")
    replace("for(const field of ['salary','tea','total'])", "for(const field of ['salary','tea','allowance','total'])")
    replace("cabinetMoney(field==='total'?halves[half].salary+halves[half].tea:halves[half][field]);", "(field==='allowance'?'+':'')+cabinetMoney(field==='total'?halves[half].salary+halves[half].tea+halves[half].allowance:halves[half][field]);")
    replace("if(c)c.textContent=cabinetMoney(salary+tea);", "if(c)c.textContent=cabinetMoney(salary+tea+allowance);const extraTotal=qs('#cabinetAllowanceTotal',panel);if(extraTotal)extraTotal.textContent='+'+cabinetMoney(allowance);")
    replace('<td data-half-total="${half}"></td></tr>', """${hasAllowance?`<td class="cabinet-allowance" data-half-allowance="${half}"></td>`:''}<td data-half-total="${half}"></td></tr>""")
    replace('Оклад и чай — вручную. Итоги за 1–15 и 16–${endDay} — автоматически. Суммы в ₽.', """${hasAllowance?'Надбавка: сотрудники × 100 ₽ · автоматически':`Оклад и чай — вручную. Итоги за 1–15 и 16–${endDay} — автоматически. Суммы в ₽.`}""")
    replace('<colgroup><col style="width:16%"><col style="width:30%"><col style="width:30%"><col style="width:24%"></colgroup>', """${hasAllowance?'<colgroup><col style="width:12%"><col style="width:23%"><col style="width:23%"><col style="width:23%"><col style="width:19%"></colgroup>':'<colgroup><col style="width:16%"><col style="width:30%"><col style="width:30%"><col style="width:24%"></colgroup>'}""")
    replace('<th scope="col">Чай</th><th scope="col">Итог</th>', """<th scope="col">Чай</th>${hasAllowance?'<th scope="col">Надбавка</th>':''}<th scope="col">Итог</th>""")
    replace('<td data-day-total="${row.work_date}"></td>', """${hasAllowance?`<td class="cabinet-allowance">+${cabinetMoney(row.allowance||0)}</td>`:''}<td data-day-total="${row.work_date}"></td>""")
    replace('<td id="cabinetTeaTotal"></td><td id="cabinetEarningsTotal"></td>', """<td id="cabinetTeaTotal"></td>${hasAllowance?'<td class="cabinet-allowance" id="cabinetAllowanceTotal"></td>':''}<td id="cabinetEarningsTotal"></td>""")
    replace("const v = currentViewId();", "const v = currentViewId();if(v!=='ownerCabinetView'||!qs('#cabinetFinance.compact-month'))document.body.classList.remove('compact-month-open');")
    return html.replace('</head>', """<style id="vlad-compact-month-style">
.compact-month-open:has(#cabinetFinance.compact-month) .topbar{padding-top:4px;padding-bottom:4px;min-height:32px}
.compact-month-open:has(#cabinetFinance.compact-month) .topbar h1{font-size:16px}.compact-month-open:has(#cabinetFinance.compact-month) .eyebrow{display:none}
#cabinetFinance.compact-month{display:grid;grid-template-columns:minmax(0,1fr) auto;column-gap:8px;align-items:center;--month-row:clamp(14px,calc((100dvh - 190px - var(--safe-bottom))/35),20px)}
#cabinetFinance.compact-month #cabinetFinanceBody{grid-column:1/-1}
#cabinetFinance.compact-month .cabinet-finance-month{font-size:0;gap:0}
#cabinetFinance.compact-month .cabinet-finance-month input{width:140px;max-width:100%}
#cabinetFinance.compact-month .cabinet-cash-date{height:calc(var(--month-row) - 1px);min-height:0;padding:0;font-size:10px;line-height:1}
#cabinetFinance.compact-month .cabinet-cash-date .cabinet-icon{width:10px;height:10px}
#cabinetFinance.compact-month .cabinet-cash-details>td{height:auto;white-space:normal;text-align:left;padding:8px;font-size:12px}
#cabinetFinance.compact-month>.cabinet-back{min-height:20px;margin:0;font-size:10px}
#cabinetFinance.compact-month h2{font-size:15px;margin:3px 0}
#cabinetFinance.compact-month .cabinet-finance-month{font-size:0}
#cabinetFinance.compact-month .cabinet-finance-month input{min-height:24px;padding:2px 4px;font-size:12px}
#cabinetFinance.compact-month .cabinet-finance-tabs{margin:4px 0;padding:2px}
#cabinetFinance.compact-month .cabinet-finance-tabs button{min-height:24px;padding:2px;font-size:11px}
#cabinetFinance.compact-month .cabinet-finance-hint{font-size:9px;margin:3px 0;line-height:1.2}
#cabinetFinance.compact-month .cabinet-finance-scroll{max-height:none;overflow-x:auto;border-radius:0}
#cabinetFinance.compact-month .cabinet-earnings-table{width:100%;min-width:0;table-layout:fixed}
#cabinetFinance.compact-month .cabinet-earnings-table th,#cabinetFinance.compact-month .cabinet-earnings-table td{height:var(--month-row);padding:0 2px;font-size:10px;line-height:1;box-sizing:border-box;white-space:nowrap;text-align:center}
#cabinetFinance.compact-month .cabinet-earnings-table th:first-child{text-align:left;font-size:9px}
#cabinetFinance.compact-month .cabinet-earnings-table input{width:100%;height:calc(var(--month-row) - 2px);min-height:0;padding:0 2px;font-size:11px;line-height:1;border-radius:3px;box-sizing:border-box;text-align:center}
#cabinetFinance.compact-month .cabinet-period-total small{display:none}
#cabinetFinance.compact-month .cabinet-allowance{color:var(--gold);font-weight:700}
#cabinetFinance.compact-month #cabinetEarningsStatus{font-size:9px;margin:3px 0}
#cabinetFinance.compact-month #cabinetSaveEarnings{min-height:24px;padding:2px;font-size:10px}
@supports(-webkit-touch-callout:none){#cabinetFinance.compact-month input[data-earnings-day]:focus{font-size:16px;min-height:26px;height:26px}}
</style></head>""", 1)


INLINE_INDEX_HTML = _assistant_month_earnings_html(INLINE_INDEX_HTML)


def _cabinet_earnings_categories_html(html):
    def replace(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError('Cabinet earnings category UI anchor missing: ' + old[:90])
        html = html.replace(old, new, 1)
    replace('row.tea=draft.tea;row.version=draft.version;',
        "row.tea=draft.tea;for(const field of ['category','second_category'])if(Object.prototype.hasOwnProperty.call(draft,field))row[field]=draft[field];row.version=draft.version;")
    replace('    const amount=value=>value===null',
        """    const categoryLabels={standard:'Стандарт',vip:'ВИП',elite:'ЭЛИТ'},categoryShort={standard:'СТ',vip:'ВИП',elite:'ЭЛ'};
    const categoryText=row=>row.second_category?categoryShort[row.category]+' + '+categoryShort[row.second_category]:categoryLabels[row.category]||'—';
    const categoryDescription=row=>row.second_category?'Первый заказ: '+categoryLabels[row.category]+'; второй заказ: '+categoryLabels[row.second_category]:categoryLabels[row.category]||'не выбрана';
    const amount=value=>value===null""")
    replace('const valid=row=>!Number.isNaN(amount(row.salary))&&!Number.isNaN(amount(row.tea));',
        "const valid=row=>(row.category==null||Object.prototype.hasOwnProperty.call(categoryLabels,row.category))&&(row.second_category==null||(row.category!=null&&Object.prototype.hasOwnProperty.call(categoryLabels,row.second_category)))&&!Number.isNaN(amount(row.salary))&&!Number.isNaN(amount(row.tea));")
    replace('JSON.stringify({salary:row.salary,tea:row.tea,version:row.version})',
        'JSON.stringify({salary:row.salary,tea:row.tea,category:row.category??null,second_category:row.second_category??null,version:row.version})')
    replace('return {salary:amount(row.salary),tea:amount(row.tea),version:row.version}',
        'return {salary:amount(row.salary),tea:amount(row.tea),category:row.category??null,second_category:row.second_category??null,version:row.version}')
    replace('row.dirty=latest.salary!==values.salary||latest.tea!==values.tea;',
        'row.dirty=latest.salary!==values.salary||latest.tea!==values.tea||latest.category!==values.category||latest.second_category!==values.second_category;')
    replace('Есть изменения. Сохраняем при выходе из поля…', 'Есть несохранённые изменения…')
    replace("${hasAllowance?'<colgroup><col style=\"width:12%\"><col style=\"width:23%\"><col style=\"width:23%\"><col style=\"width:23%\"><col style=\"width:19%\"></colgroup>':'<colgroup><col style=\"width:16%\"><col style=\"width:30%\"><col style=\"width:30%\"><col style=\"width:24%\"></colgroup>'}",
        "${hasAllowance?'<colgroup><col style=\"width:10%\"><col style=\"width:20%\"><col style=\"width:18%\"><col style=\"width:16%\"><col style=\"width:19%\"><col style=\"width:17%\"></colgroup>':'<colgroup><col style=\"width:12%\"><col style=\"width:22%\"><col style=\"width:22%\"><col style=\"width:22%\"><col style=\"width:22%\"></colgroup>'}")
    replace('<th scope="col">Дата</th><th scope="col">Оклад</th>',
        '<th scope="col">Дата</th><th scope="col">Категория</th><th scope="col">Оклад</th>')
    replace("</th>${['salary','tea'].map(k=>", """</th><td><button type="button" class="cabinet-earnings-category" data-earnings-category="${row.work_date}" aria-haspopup="dialog" title="${esc(categoryDescription(row))}" aria-label="Категория за ${row.work_date}: ${esc(categoryDescription(row))}">${esc(categoryText(row))}</button></td>${['salary','tea'].map(k=>""")
    replace('</span><small>Итого</small></th><td data-half-salary="${half}">',
        '</span><small>Итого</small></th><td></td><td data-half-salary="${half}">')
    replace('<th scope="row" title="Всего за месяц">Месяц</th><td id="cabinetSalaryTotal">',
        '<th scope="row" title="Всего за месяц">Месяц</th><td></td><td id="cabinetSalaryTotal">')
    replace("        qsa('[data-earnings-day]',body).forEach(input=>{", """        qsa('[data-earnings-category]',body).forEach(button=>button.onclick=()=>{
          if(!active())return;
          const row=rows.get(button.dataset.earningsCategory);
          const applySelection=(first,second=null)=>{
            if(!active()){closeModal();return}
            row.category=first;row.second_category=second;row.dirty=true;row.error='';remember(row);
            button.textContent=categoryText(row);button.title=categoryDescription(row);
            button.setAttribute('aria-label','Категория за '+row.work_date+': '+categoryDescription(row));
            closeModal();button.focus({preventScroll:true});updateTotals();saveRow(row);
          };
          showModal('Категория · '+fmtDate(row.work_date),`<div class="cabinet-category-options" role="group" aria-label="Выберите категорию">${Object.entries({...categoryLabels,two:'Два заказа'}).map(([value,label])=>{const selected=value==='two'?!!row.second_category:!row.second_category&&row.category===value;return `<button type="button" class="cabinet-secondary" data-category-choice="${value}" aria-pressed="${selected}">${label}${selected?' ✓':''}</button>`}).join('')}</div>`);
          qsa('[data-category-choice]',qs('#modal')).forEach(choice=>choice.onclick=()=>{
            if(!active()){closeModal();return}
            if(choice.dataset.categoryChoice!=='two'){applySelection(choice.dataset.categoryChoice);return}
            const options=()=>'<option value="" disabled>Выберите категорию</option>'+Object.entries(categoryLabels).map(([value,label])=>`<option value="${value}">${label}</option>`).join('');
            showModal('Два заказа · '+fmtDate(row.work_date),`<form id="cabinetTwoOrdersForm" class="cabinet-native-form"><label>Первый заказ<select name="first_category" required aria-label="Категория первого заказа">${options()}</select></label><label>Второй заказ<select name="second_category" required aria-label="Категория второго заказа">${options()}</select></label><p id="cabinetTwoOrdersStatus" role="status" aria-live="polite"></p><button type="submit" class="cabinet-primary cabinet-wide" id="cabinetTwoOrdersSave" disabled>Сохранить категории</button></form>`);
            const form=qs('#cabinetTwoOrdersForm'),first=qs('[name=first_category]',form),second=qs('[name=second_category]',form),submit=qs('#cabinetTwoOrdersSave',form);
            first.value=row.category||'';second.value=row.second_category||'';
            const ready=()=>Object.prototype.hasOwnProperty.call(categoryLabels,first.value)&&Object.prototype.hasOwnProperty.call(categoryLabels,second.value);
            const update=()=>{submit.disabled=!ready()};first.onchange=update;second.onchange=update;update();
            form.onsubmit=event=>{event.preventDefault();if(!ready()){qs('#cabinetTwoOrdersStatus',form).textContent='Выберите категорию для обоих заказов';return}applySelection(first.value,second.value)};
          });
        });
        qsa('[data-earnings-day]',body).forEach(input=>{""")
    return html.replace('</head>', """<style id="cabinet-earnings-category-style">
#cabinetFinance.compact-month .cabinet-earnings-category{width:100%;height:calc(var(--month-row) - 2px);min-height:0;margin:0;padding:0 2px;border:1px solid var(--line);border-radius:3px;background:var(--panel2);color:var(--text);font-size:9px;line-height:1;white-space:nowrap;box-sizing:border-box;text-align:center}
#cabinetFinance.compact-month .cabinet-earnings-category::after{content:'⌄';margin-left:2px;color:var(--muted);font-size:8px}
#cabinetFinance.compact-month .cabinet-earnings-category:focus-visible{outline:1px solid var(--gold);outline-offset:-1px}
.cabinet-category-options{display:grid;gap:8px;margin:12px 0}
.cabinet-category-options button{min-height:44px;font-size:16px;text-align:left}
.cabinet-category-options button[aria-pressed="true"]{border-color:var(--gold);color:var(--gold)}
</style></head>""", 1)


INLINE_INDEX_HTML = _cabinet_earnings_categories_html(INLINE_INDEX_HTML)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('Не отписались к 15:30 · ${no.length}',
    "Не отписались к ${esc(c.cutoff_label||'14:00')} · ${no.length}", 1)


def gbu_control_earning_details(db, work_date):
    """Expose only category and crew size saved for an exact GBU card."""
    rows = db.scalars(select(PersonalMonthlyTable).where(
        PersonalMonthlyTable.kind == 'earnings',
        PersonalMonthlyTable.month == work_date.strftime('%Y-%m')))
    candidates = {}
    for row in rows:
        if not _is_owner(db, row.owner_id):
            continue
        try:
            details = json.loads(row.payload).get('gbu_details', {}).get(str(work_date.day), {})
        except (ValueError, TypeError, AttributeError):
            continue
        if not isinstance(details, dict):
            continue
        for key, detail in details.items():
            if not isinstance(key, str) or not key.startswith('card:') or not isinstance(detail, dict):
                continue
            category, team = detail.get('category'), detail.get('team_size')
            if category in ('standard', 'elite') and type(team) is int and team in (4, 6):
                candidates.setdefault(key, set()).add((category, team))
    # Conflicting owner tables must not silently assign the wrong category.
    return {key: {'category': next(iter(values))[0], 'team_size': next(iter(values))[1]}
            for key, values in candidates.items() if len(values) == 1}


@app.get('/api/owner/brigade-control')
def owner_brigade_control(work_date: date, db: Session = Depends(get_db),
        user: TelegramUser = Depends(require_app_user)):
    if not _dispatcher_access(db, user.id) or cabinet_kind(db, user.id) not in {'owner', 'assistant'}:
        raise HTTPException(403, 'Контроль доступен владельцу и помощникам с диспетчерской')
    cards = db.scalars(select(NumberCardEvent).where(NumberCardEvent.work_date == work_date,
        NumberCardEvent.source.in_(('private', 'gbu')), NumberCardEvent.tg_id.notin_(HIDDEN_TG_IDS),
        _visible_number_card())).all()
    statuses = number_card_statuses(db, cards)
    contacts = _order_contact_states(db, work_date)
    cards.sort(key=lambda card: number_card_contact_sort_key(card, statuses))
    employees = {row.tg_id: row for row in db.scalars(select(Employee).where(
        Employee.tg_id.in_([card.tg_id for card in cards]), _visible_employee()))} if cards else {}
    keys = [f'card:{card.chat_id}:{card.message_id}' for card in cards]
    states = {row.report_key: row for row in db.scalars(select(CabinetOrderState).where(
        CabinetOrderState.report_key.in_(keys)))} if keys else {}
    media_rows = db.scalars(select(OrderReportMedia).where(OrderReportMedia.report_key.in_(keys))).all() if keys else []
    manual_frames = set(db.execute(select(OrderReportFraming.report_key, OrderReportFraming.stage).where(OrderReportFraming.report_key.in_(keys))).all()) if keys else set()
    media_by_key = {}
    for media_row in media_rows:
        media_by_key.setdefault(media_row.report_key, []).append(media_row)
    earning_details = gbu_control_earning_details(db, work_date) if any(c.source == 'gbu' for c in cards) else {}
    items = []
    for card, key in zip(cards, keys):
        row = states.get(key)
        try:
            meta = json.loads(row.payload) if row and row.tg_id == card.tg_id else {}
        except (ValueError, TypeError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        photo = effective_order_photo(db, card)
        employee = employees.get(card.tg_id)
        required = 2 if card.source == 'gbu' else 1
        received = int(bool(photo['first_received'])) + (int(bool(photo['second_received'])) if required == 2 else 0)
        items.append({'id': card.id, 'source': card.source, 'surname': card.surname,
            'gbu_earning_detail': earning_details.get(f'card:{card.id}') if card.source == 'gbu' else None,
            'employee': _employee_dict(employee) if employee else {'full_name': card.full_name, 'phone': card.phone},
            'contact_time': statuses[card.id]['contact_time'], 'on_contact': bool(contacts.get(card.id)),
            'prepared': bool(meta.get('prepared') or statuses[card.id]['closed']),
            'report_photos': [{'stage': m.stage, 'revision': m.fingerprint, 'manual': (key, m.stage) in manual_frames} for m in sorted(media_by_key.get(key, []), key=lambda m: m.stage) if m.stage <= required],
            **photo, 'photos_required': required, 'photos_received': received,
            'photos_complete': received == required, 'completed': meta.get('tea_received') is True,
            'tea_amount': meta.get('tea_amount') if meta.get('tea_received') is True else None})
    totals = {}
    for source in ('private', 'gbu'):
        entries = [item for item in items if item['source'] == source]
        expected = int(_category_counts(db, work_date, source)['total'])
        totals[source] = {'orders': max(expected, len(entries)), 'cards': len(entries),
            'on_contact': sum(item['on_contact'] for item in entries),
            'photos_complete': sum(item['photos_complete'] for item in entries),
            'first_photos': sum(bool(item['first_received']) for item in entries),
            'second_photos': sum(bool(item['second_received']) for item in entries),
            'completed': sum(item['completed'] for item in entries)}
    totals['all'] = {field: totals['private'][field] + totals['gbu'][field] for field in totals['private']}
    return JSONResponse({'work_date': work_date.isoformat(), 'items': items, 'totals': totals,
        'order_completion': order_completion_data(db, work_date),
        'updated_at': datetime.now(MOSCOW).isoformat()},
        headers={'Cache-Control': 'private, no-store'})


# Keep the readiness heading full-width and its actions in two compact rows.
# Scoped styles preserve the same buttons, handlers and layouts in other views.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', """<style id="readiness-compact-layout-style">
#employeeReadiness{width:100%;min-width:0}
#employeeReadiness>.section-head{display:block;margin:10px 0 8px}
#employeeReadiness>.section-head>div:first-child{width:100%;min-width:0}
#readinessTitle{width:100%;font-size:clamp(18px,4.7vw,20px);line-height:1.25;white-space:normal;margin:0;overflow-wrap:normal}
#readinessDayLabel{font-size:11px;line-height:1.3;margin:3px 0 0}
#employeeReadiness .section-actions.readiness-actions{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));width:100%;max-width:none;gap:6px;margin-top:8px;align-items:stretch;justify-content:stretch}
#employeeReadiness .readiness-actions>button{box-sizing:border-box;min-width:0;min-height:44px;width:100%;margin:0;padding:6px 8px;border-radius:10px;font-size:12px;line-height:1.2;white-space:normal}
#employeeReadiness #readinessHistoryBtn{grid-column:1/4;grid-row:1;padding:5px 6px}
#employeeReadiness #readinessHistoryBtn b{display:block;font-size:12px;line-height:1.15}
#employeeReadiness #readinessHistoryBtn br{display:none}
#employeeReadiness #readinessHistoryBtn small{display:block;margin-top:1px;font-size:10px!important;line-height:1.1}
#employeeReadiness .readiness-actions #readinessRemindersBtn{grid-column:4/5;grid-row:1;width:100%;min-width:0;padding:4px;font-size:22px}
#employeeReadiness #readinessAvralBtn{grid-column:5/7;grid-row:1;white-space:nowrap;padding-left:4px;padding-right:4px}
#employeeReadiness #readinessCountBtn{grid-column:1/4;grid-row:2}
#employeeReadiness #readinessSendBtn{grid-column:4/7;grid-row:2}
#employeeReadiness #readinessSummary{gap:6px;margin:8px 0}
#employeeReadiness #readinessSummary .metric{padding:8px 10px;border-radius:12px}
#employeeReadiness #readinessSummary .metric b{font-size:18px;line-height:1.15}
#employeeReadiness #readinessSummary .metric span{margin-top:2px;font-size:11px;line-height:1.2}
#employeeReadiness .group-block{margin-bottom:8px;border-radius:14px}
#employeeReadiness .group-title{padding:9px 10px;font-size:14px}
#employeeReadiness .group-stats{gap:4px;padding:0 8px 8px}
#employeeReadiness .group-stats button{min-width:0;min-height:44px;padding:5px 2px;font-size:9px;line-height:1.15}
#employeeReadiness .group-stats b{font-size:15px;line-height:1.3}
@media(max-width:350px){#employeeReadiness .readiness-actions>button{font-size:11px;padding-left:4px;padding-right:4px}#employeeReadiness #readinessHistoryBtn small{font-size:9px!important}}
</style></head>""", 1)


def _owner_brigade_control_html(html):
    def replace(old, new):
        nonlocal html
        if html.count(old) != 1:
            raise ValueError('Owner brigade control UI anchor missing: ' + old[:90])
        html = html.replace(old, new, 1)
    replace('<button class="icon-btn" id="refreshBtn"', '<button type="button" class="icon-btn owner-management-top" id="ownerManagementTop" aria-label="Управление" title="Управление" hidden>⚙</button><button class="icon-btn" id="refreshBtn"')
    replace('<section id="ownerManagementView" class="view">', '<section id="ownerBrigadeControlView" class="view"><div id="ownerBrigadeControlContent"></div></section>\n<section id="ownerManagementView" class="view">')
    replace('<button data-view="ownerManagementView" hidden aria-hidden="true"></button>', '<button data-view="ownerManagementView" hidden aria-hidden="true"></button><button data-view="ownerBrigadeControlView" hidden aria-hidden="true"></button>')
    replace("{key:'management',title:'Управление',icon:()=>'<span>⚙</span>'}", "{key:'brigadeControl',title:'Контроль',icon:()=>cabinetIcon('control')}")
    replace("  if(kind==='assistant')return [{...readiness,title:'Заказы'},staff,cash,earnings,{key:'own',title:'Личный кабинет',icon:()=>cabinetIcon('user')}];",
        "  if(kind==='assistant')return [{...readiness,title:'Заказы'},staff,cash,earnings,{key:'own',title:'Личный кабинет',icon:()=>cabinetIcon('user')},...(canUseBrigadeControl()?[{key:'brigadeControl',title:'Контроль',icon:()=>cabinetIcon('control')}]:[])];")
    replace("  const identity=state.bootstrap.user.id+':'+kind;", "  const identity=state.bootstrap.user.id+':'+kind+':'+canUseBrigadeControl();")
    replace("  own.hidden=true;", """  own.hidden=true;
  const management=qs('#ownerManagementTop'),owner=kind==='owner'&&state.bootstrap.can_manage_cabinets===true;
  management.hidden=!owner;management.classList.toggle('active',owner&&roleNavigationActive==='management');management.setAttribute('aria-current',owner&&roleNavigationActive==='management'?'page':'false');
  management.onclick=()=>roleNavigate('management').catch(e=>toast(e.message));
  if(!canUseBrigadeControl()){ownerBrigadeControlData=null;ownerBrigadeControlIdentity='';qs('#ownerBrigadeControlContent').replaceChildren()}""")
    replace("  if(!roleNavigationItems(kind).some(item=>item.key===target))return;", "  if(!(target==='management'&&kind==='owner'&&state.bootstrap.can_manage_cabinets===true)&&!roleNavigationItems(kind).some(item=>item.key===target))return;")
    replace("  if(target==='management'){await roleActivateView('ownerManagementView');return}", "  if(target==='management'){await roleActivateView('ownerManagementView');return}\n  if(target==='brigadeControl'){await roleActivateView('ownerBrigadeControlView');return}")
    replace("  document.body.classList.toggle('management-open',v==='ownerManagementView');", "  document.body.classList.toggle('management-open',v==='ownerManagementView');\n  document.body.classList.toggle('owner-brigade-control-open',v==='ownerBrigadeControlView');")
    replace("  if (v==='ordersView') return loadOrders();", "  if(v==='ownerBrigadeControlView')return loadOwnerBrigadeControl();\n  if (v==='ordersView') return loadOrders();")
    replace("  if(document.hidden||currentViewId()==='ownerManagementView') return;", "  if(document.hidden||currentViewId()==='ownerManagementView') return;\n  if(currentViewId()==='ownerBrigadeControlView'){try{await loadOwnerBrigadeControl()}catch{}return}")
    replace("function currentViewId() {", r"""let ownerBrigadeControlData=null,ownerBrigadeControlIdentity='',ownerBrigadeControlRequest=0;
let ownerBrigadeControlSource='all',ownerBrigadeControlStage=null;
function canUseBrigadeControl(){
  const b=state.bootstrap;
  return !!b&&((b.cabinet_kind==='owner'&&b.can_manage_cabinets===true)||(b.cabinet_kind==='assistant'&&b.cabinet_only===false));
}
function renderOwnerBrigadeControl(){
  const root=qs('#ownerBrigadeControlContent'),data=ownerBrigadeControlData;
  if(!data||currentViewId()!=='ownerBrigadeControlView')return;
  const open=new Set(qsa('details[open]',root).map(row=>Number(row.dataset.controlCard)));
  const source=ownerBrigadeControlSource,stage=ownerBrigadeControlStage,totals=data.totals[source];
  const captions={all:'Все заказы',private:'Частные заказы',gbu:'Заказы ГБУ'};
  const stages={contact:'Связь бригад',photo:'Фото по заказам',done:'Отчёты по чаю'};
  const items=data.items.filter(item=>source==='all'||item.source===source);
  const waiting='<svg class="cabinet-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12M6 21h12M7 3v4l10 10v4M17 3v4L7 17v4"/></svg>';
  const block=(key,icon,value,label,ready,count=null)=>`<div class="bc-stage bc-stage-${key} ${ready?'is-done':''} ${stage===key?'is-focused':''}" aria-label="${esc(label)}: ${esc(value)}" title="${esc(label)}: ${esc(value)}">${key==='done'?'':`<span class="bc-stage-icon">${cabinetIcon(icon)}</span>`}<b>${esc(value)}</b><span class="bc-stage-state" aria-hidden="true">${count!==null?esc(count):ready?cabinetIcon('check'):waiting}</span></div>`;
  root.innerHTML=`<div class="bc-heading"><div><h2>Контроль заказов</h2><p>${esc(fmtDate(data.work_date))}</p></div><label class="bc-date">Дата<input type="date" id="brigadeControlDate" value="${esc(data.work_date)}" aria-label="Дата контроля заказов"></label></div>
    <div class="bc-counters" role="group" aria-label="Этап контроля">${[['contact','На связи','on_contact'],['photo','Фото получены','photos_complete'],['done','Завершено','completed']].map(([key,label,field])=>`<button type="button" data-brigade-stage="${key}" aria-pressed="${stage===key}" class="${key==='photo'&&source==='gbu'?'bc-gbu-photo-counter':''}">${key==='photo'&&source==='gbu'?`<span class="bc-photo-count-line"><span>1 фото</span><b>${totals.first_photos||0}/${totals.orders}</b></span><span class="bc-photo-count-line"><span>2 фото</span><b>${totals.second_photos||0}/${totals.orders}</b></span>`:`<b>${totals[field]}/${totals.orders}</b><span>${label}</span>`}</button>`).join('')}</div>
    <div class="bc-sources" role="group" aria-label="Тип заказа">${[['all','Все'],['private','Частные'],['gbu','ГБУ ⬛️']].map(([key,label])=>`<button type="button" data-brigade-source="${key}" aria-pressed="${source===key}">${label} · ${data.totals[key].orders}</button>`).join('')}</div>
    <div class="bc-list-caption"><span>${stage?stages[stage]:captions[source]} · ${items.length}</span><span>По времени связи</span></div>
    ${totals.cards<totals.orders?`<p class="bc-missing">Номера в группе: ${totals.cards}/${totals.orders}. Остальные карточки появятся после отправки номера.</p>`:''}
    <div class="bc-list">${items.map(card=>{
      const p=card.employee||{},person=String(p.full_name||'').trim().split(/\s+/)[0]||'Бригадир',name=[person,p.metro].filter(Boolean).join(' · ');
      const phone=String(p.phone||'').replace(/[^+\d]/g,'');
      return `<details class="bc-order" data-control-card="${card.id}"><summary><div class="bc-order-title"><span class="bc-kind bc-kind-${card.source==='gbu'?'gbu':'private'}">${card.source==='gbu'?'ГБУ ⬛️':'Частный'}</span>${cabinetIcon('chevron')}<b>${esc(card.surname)}</b></div><p class="bc-person" aria-label="${esc(name)}"><span class="bc-person-name">${esc(person)}</span> <span class="bc-person-metro">${esc(p.metro||'')}</span></p><div class="bc-stages">${block('contact','phone',card.contact_time||'—',card.on_contact?'Бригада на связи':'Ожидается связь',card.on_contact)}${block('photo','camera',card.photo_time||'—',`Фото ${card.photos_received}/${card.photos_required} — ${card.photos_complete?'получены':'ожидаются'}`,card.photos_complete,`${card.photos_received}/${card.photos_required}`)}${block('done','tea',card.completed?`Чай: ${cabinetMoney(card.tea_amount)} ₽`:'Отчёт',card.completed?'Отчёт по чаю получен':'Ожидается отчёт по чаю',card.completed)}</div></summary></details>`;
    }).join('')||'<div class="cabinet-empty">На эту дату карточек заказов нет.</div>'}</div>
    <p class="bc-legend">${waiting} Ожидается · ${cabinetIcon('check')} Выполнено</p><p class="bc-legend">Частные — 1 фото · ГБУ — 2 фото. Нажмите карточку, чтобы открыть заказ.</p><p class="bc-updated">Обновлено ${esc(new Date(data.updated_at).toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'}))}</p>`;
  qs('#brigadeControlDate',root).onchange=event=>{if(event.target.value)loadBootstrap(event.target.value).catch(error=>toast(error.message))};
  qsa('[data-brigade-source]',root).forEach(button=>button.onclick=()=>{ownerBrigadeControlSource=button.dataset.brigadeSource;renderOwnerBrigadeControl()});
  qsa('[data-brigade-stage]',root).forEach(button=>button.onclick=()=>{ownerBrigadeControlStage=ownerBrigadeControlStage===button.dataset.brigadeStage?null:button.dataset.brigadeStage;renderOwnerBrigadeControl()});
  bindControlReportPhotos(root);
}
async function loadOwnerBrigadeControl(){
  const root=qs('#ownerBrigadeControlContent'),uid=state.bootstrap?.user?.id,day=state.selectedDate;
  if(!canUseBrigadeControl()){ownerBrigadeControlData=null;root.textContent='Раздел доступен владельцу и помощникам с диспетчерской';return}
  const identity=uid+':'+day,request=++ownerBrigadeControlRequest;
  if(ownerBrigadeControlIdentity!==identity){ownerBrigadeControlIdentity=identity;ownerBrigadeControlData=null;ownerBrigadeControlSource='all';ownerBrigadeControlStage=null;root.innerHTML='<p role="status">Загрузка контроля заказов…</p>'}
  const active=()=>request===ownerBrigadeControlRequest&&currentViewId()==='ownerBrigadeControlView'&&state.selectedDate===day&&state.bootstrap?.user?.id===uid&&canUseBrigadeControl();
  try{
    const data=await api('/api/owner/brigade-control?work_date='+encodeURIComponent(day),{cache:'no-store'});
    if(!active()||data.work_date!==day)return;
    ownerBrigadeControlData=data;renderOwnerBrigadeControl();
  }catch(error){if(active()){ownerBrigadeControlData=null;root.innerHTML=`<p role="alert">${esc(error.message)}</p><button type="button" class="cabinet-secondary" id="brigadeControlRetry">Повторить</button>`;qs('#brigadeControlRetry',root).onclick=loadOwnerBrigadeControl}}
}
function currentViewId() {""")
    return html.replace('</head>', """<style id="owner-brigade-control-style">
.owner-management-top[hidden]{display:none!important}.owner-management-top.active{color:var(--gold);border-color:var(--gold)}
.topbar>div:first-child{min-width:0}.topbar>div:last-child{flex-shrink:0}.owner-management-top{font-size:23px}
body[data-cabinet-kind="assistant"] .role-navigation small{font-size:9px;line-height:1.15;white-space:normal;overflow-wrap:anywhere}
.owner-brigade-control-open #summary,.owner-brigade-control-open #selectedDayHeader,.owner-brigade-control-open main>h3,.owner-brigade-control-open #roleOwnOrders{display:none!important}
#ownerBrigadeControlContent{padding:8px 0 16px}.bc-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px}.bc-heading h2{font-size:21px;margin:0}.bc-heading p{font-size:12px;color:var(--muted);margin:4px 0 0}.bc-date{font-size:11px;color:var(--muted);text-align:right}.bc-date input{display:block;max-width:128px;min-height:32px;padding:4px 6px;border:1px solid var(--line);border-radius:10px;background:var(--panel2);color:var(--text);font-size:12px}
.bc-counters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}.bc-counters button{min-width:0;min-height:70px;padding:8px 4px;border:1px solid var(--line);border-radius:13px;background:var(--panel);color:var(--text)}.bc-counters b{display:block;font-size:23px}.bc-counters span{display:block;font-size:11px;color:var(--muted);margin-top:3px}.bc-counters button[aria-pressed=true]{border-color:var(--gold);background:#211d15}
.bc-sources{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;padding:4px;margin-top:10px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}.bc-sources button{min-width:0;min-height:40px;padding:6px 2px;border:0;border-radius:8px;background:transparent;color:var(--muted);font-size:12px}.bc-sources button[aria-pressed=true]{background:#2a251b;color:#f6dfac}
.bc-list-caption{display:flex;justify-content:space-between;gap:8px;font-size:11px;color:var(--muted);margin:12px 0 6px}.bc-missing{font-size:12px;color:var(--gold)}.bc-order{padding:10px 9px;margin-bottom:7px;border:1px solid var(--line);border-radius:13px;background:var(--panel)}.bc-order summary{list-style:none;cursor:pointer}.bc-order summary::-webkit-details-marker{display:none}.bc-order-title{display:flex;gap:6px;align-items:center}.bc-order-title b{font-size:14px;flex:1;min-width:0;overflow-wrap:anywhere}.bc-order-title>span{font-size:11px;color:var(--gold);background:#211d15;border-radius:5px;padding:2px 5px}.bc-order-title>.cabinet-icon{width:14px;height:14px;transform:rotate(90deg)}.bc-order[open] .bc-order-title>.cabinet-icon{transform:rotate(-90deg)}.bc-person{font-size:12px;color:var(--muted);margin:3px 0 7px}
.bc-stages{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}.bc-stage{min-width:0;padding:5px 3px;border-radius:6px;background:var(--panel2);text-align:center}.bc-stage b{display:flex;align-items:center;justify-content:center;gap:4px;font-size:12px;white-space:nowrap}.bc-stage .cabinet-icon{width:13px;height:13px;color:var(--muted)}.bc-stage>span{display:block;font-size:11px;color:var(--muted);margin-top:2px}.bc-stage.is-done b{color:var(--green)}.bc-stage.is-focused{background:#302719}
.bc-details{border-top:1px solid var(--line);margin-top:9px;padding-top:9px;font-size:12px}.bc-details p{color:var(--muted);margin:0 0 8px}.bc-details a{color:var(--gold)}.bc-details dl{margin:0}.bc-details dl>div{display:flex;justify-content:space-between;gap:8px;margin:6px 0}.bc-details dt{color:var(--muted)}.bc-details dd{margin:0;text-align:right}.bc-legend,.bc-updated{font-size:11px;color:var(--muted);margin:8px 0 0}
#ownerBrigadeControlContent .bc-list{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;align-items:stretch}
#ownerBrigadeControlContent .bc-order{min-width:0;padding:4px;margin:0;border-radius:9px}
#ownerBrigadeControlContent .bc-order-title{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:3px;align-items:center}
#ownerBrigadeControlContent .bc-order-title b{grid-column:1/-1;font-size:10px;line-height:1.3}
#ownerBrigadeControlContent .bc-order-title>span{justify-self:start;max-width:100%;font-size:9px;line-height:1.2;padding:2px;overflow-wrap:anywhere}
#ownerBrigadeControlContent .bc-order-title>.cabinet-icon{width:10px;height:10px}
#ownerBrigadeControlContent .bc-kind-gbu{color:var(--gold);background:#211d15}
#ownerBrigadeControlContent .bc-kind-private{color:var(--green);background:#14241a}
#ownerBrigadeControlContent .bc-sources button[data-brigade-source=private]{color:var(--green)}
#ownerBrigadeControlContent .bc-sources button[data-brigade-source=private][aria-pressed=true]{background:#1b3325}
#ownerBrigadeControlContent .bc-sources button[data-brigade-source=gbu]{color:var(--gold)}
#ownerBrigadeControlContent .bc-person{font-size:10px;line-height:1.3;margin:3px 0 5px;overflow-wrap:anywhere}
#ownerBrigadeControlContent .bc-person-name,#ownerBrigadeControlContent .bc-person-metro{display:block}
#ownerBrigadeControlContent .bc-person-metro:empty{display:none}
#ownerBrigadeControlContent .bc-stages{grid-template-columns:minmax(0,1fr);gap:3px}
#ownerBrigadeControlContent .bc-stage{display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:2px;align-items:center;min-height:23px;padding:3px 2px;text-align:left}
#ownerBrigadeControlContent .bc-stage b{display:block;min-width:0;font-size:10px;line-height:1.25;white-space:normal;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
#ownerBrigadeControlContent .bc-stage>span{margin:0;font-size:10px;line-height:1.25}
#ownerBrigadeControlContent .bc-stage .cabinet-icon{display:block;width:10px;height:10px}
#ownerBrigadeControlContent .bc-stage-state{justify-self:end;white-space:nowrap}
#ownerBrigadeControlContent .bc-stage.is-done .bc-stage-icon,#ownerBrigadeControlContent .bc-stage.is-done .bc-stage-state,#ownerBrigadeControlContent .bc-stage.is-done .cabinet-icon{color:var(--green)}
#ownerBrigadeControlContent .bc-stage-done{grid-template-columns:minmax(0,1fr) auto}
#ownerBrigadeControlContent .bc-stage-done b{font-size:10px}
#ownerBrigadeControlContent .bc-order[open],#ownerBrigadeControlContent .bc-list>.cabinet-empty{grid-column:1/-1}
#ownerBrigadeControlContent .bc-order[open]{padding:9px}
#ownerBrigadeControlContent .bc-order[open] .bc-order-title b{font-size:14px}
#ownerBrigadeControlContent .bc-order[open] .bc-person{font-size:12px}
#ownerBrigadeControlContent .bc-order[open] .bc-person-name,#ownerBrigadeControlContent .bc-order[open] .bc-person-metro{display:inline}
#ownerBrigadeControlContent .bc-order[open] .bc-stages{grid-template-columns:repeat(3,minmax(0,1fr))}
#ownerBrigadeControlContent .bc-order[open] .bc-stage b{font-size:12px}
#ownerBrigadeControlContent .bc-legend .cabinet-icon{width:12px;height:12px}
@media(max-width:350px){.bc-heading h2{font-size:18px}.bc-date input{max-width:116px}.bc-list-caption{font-size:10px}}
@media(pointer:coarse){.bc-sources button{min-height:44px}}
</style></head>""", 1)


INLINE_INDEX_HTML = _owner_brigade_control_html(INLINE_INDEX_HTML)


def _compact_dispatcher_dashboard_html(html):
    anchor = "  document.body.classList.toggle('owner-brigade-control-open',v==='ownerBrigadeControlView');"
    if html.count(anchor) != 1:
        raise ValueError('Compact dispatcher dashboard anchor missing')
    html = html.replace(anchor, anchor + "\n  document.body.classList.toggle('dashboard-compact',v==='readinessView'&&!state.bootstrap?.cabinet_only);", 1)
    date_label = '<label class="muted">Выбрать дату <input type="date" id="dashboardDate"'
    if html.count(date_label) != 1:
        raise ValueError('Dispatcher date label anchor missing')
    html = html.replace(date_label, '<label class="muted"><span class="dashboard-date-prompt">Выбрать дату</span><span class="dashboard-date-picker"><span class="dashboard-date-value" aria-hidden="true">${esc(dashboardOrderDateText(day))}${cabinetIcon("chevron")}</span><input type="date" id="dashboardDate" aria-label="Дата заказов"', 1)
    date_end = 'value="${day}" style="max-width:160px"></label>'
    heading = '<h2>${esc(fmtDate(day))}</h2><label class="muted">'
    if html.count(date_end) != 1 or html.count(heading) != 1 or html.count('function renderSummary() {') != 1:
        raise ValueError('Dispatcher weekday/date heading anchor missing')
    html = html.replace(date_end, 'value="${day}" style="max-width:160px"></span></label>', 1)
    html = html.replace(heading, '<h2><span class="dashboard-orders-heading">ЗАКАЗЫ</span><span class="dashboard-selected-day">${esc(fmtDate(day))}</span></h2><label class="muted">', 1)
    html = html.replace('function renderSummary() {', """function dashboardOrderDateText(day){
  const label=new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{weekday:'short',day:'numeric',month:'short',year:'numeric',timeZone:'Europe/Moscow'});
  return label.charAt(0).toLocaleUpperCase('ru-RU')+label.slice(1);
}
function renderSummary() {""", 1)
    date_change = "  qs('#dashboardDate').onchange=e=>{if(e.target.value)loadBootstrap(e.target.value).catch(e=>toast(e.message))};"
    if html.count(date_change) != 1:
        raise ValueError('Dispatcher date change handler anchor missing')
    html = html.replace(date_change, date_change + "\n  qs('#dashboardDate').onclick=event=>{if(!document.body.classList.contains('dashboard-compact'))return;try{event.currentTarget.showPicker?.()}catch{}};", 1)
    return html.replace('</head>', """<style id="compact-dispatcher-dashboard-style">
.dashboard-orders-heading,.dashboard-date-value{display:none}
.dashboard-date-picker{display:inline-block}
body.dashboard-compact .topbar{padding:6px 12px 4px;gap:6px}
body.dashboard-compact .topbar .eyebrow{font-size:9px;line-height:1.2}
body.dashboard-compact .topbar h1{font-size:20px;line-height:1.2;margin:2px 0 0}
body.dashboard-compact .topbar .icon-btn{width:42px;height:42px;border-radius:12px;font-size:21px}
body.dashboard-compact #dateStrip{gap:5px;padding:4px 12px 5px}
body.dashboard-compact #dateStrip .date-pill{min-width:66px;padding:6px;border-radius:10px}
body.dashboard-compact #dateStrip .date-pill strong{font-size:11px;line-height:1.2}
body.dashboard-compact #dateStrip .date-pill small{font-size:10px;line-height:1.2}
body.dashboard-compact main{padding:0 12px}
body.dashboard-compact #selectedDayHeader{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;align-items:center;margin:2px 0 6px}
body.dashboard-compact #selectedDayHeader h2{font-size:17px;line-height:1.2;margin:0;min-width:0}
body.dashboard-compact #selectedDayHeader label{display:flex;align-items:center;gap:5px;font-size:13px;line-height:1.2;margin:0;text-align:right}
body.dashboard-compact .dashboard-date-prompt{display:none}
body.dashboard-compact .dashboard-orders-heading{display:inline;font-weight:800;color:var(--text);letter-spacing:.02em}
body.dashboard-compact .dashboard-selected-day{display:none}
body.dashboard-compact .dashboard-date-picker{position:relative;display:inline-flex;align-items:center;min-height:32px;padding:5px 8px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text)}
body.dashboard-compact .dashboard-date-picker:focus-within{border-color:var(--gold)}
body.dashboard-compact .dashboard-date-value{display:flex;align-items:center;gap:5px;font-size:11px;line-height:1.2;white-space:nowrap;pointer-events:none}
body.dashboard-compact .dashboard-date-value .cabinet-icon{width:10px;height:10px;transform:rotate(90deg)}
body.dashboard-compact #dashboardDate{position:absolute;inset:0;display:block;width:100%;max-width:none!important;height:100%;min-height:32px;opacity:0;cursor:pointer;margin:0;z-index:1}
body.dashboard-compact #selectedDayHeader+h3{display:none}
body.dashboard-compact #summary{gap:5px;margin:0 0 6px}
body.dashboard-compact #summary .metric{padding:7px 4px;min-height:74px;border-radius:11px;text-align:center}
body.dashboard-compact #summary .metric b{font-size:18px!important;line-height:1.15}
body.dashboard-compact #summary .metric b:nth-of-type(2){font-size:12px!important;margin-top:4px!important}
body.dashboard-compact #summary .metric span{font-size:9px;line-height:1.2;margin-top:3px;white-space:normal;overflow:visible}
body.dashboard-compact #dayCommentBtn{min-height:30px;margin-top:4px;padding:4px 2px;font-size:9px;border-radius:8px}
body.dashboard-compact #brigadeWork,body.dashboard-compact #orderPreparation{margin:6px 0!important;padding-top:5px;border-top:1px solid var(--line)}
body.dashboard-compact #brigadeWork h2,body.dashboard-compact #orderPreparation h2{font-size:16px;line-height:1.2;margin:0 0 3px}
body.dashboard-compact #brigadeWork>p,body.dashboard-compact #orderPreparation>p{font-size:10px;line-height:1.3;margin:0 0 5px;overflow-wrap:anywhere}
body.dashboard-compact #brigadeWork .summary-grid,body.dashboard-compact #orderPreparation .summary-grid{gap:5px;margin:0}
body.dashboard-compact #brigadeWork .metric{min-height:58px;padding:7px 4px;border-radius:11px}
body.dashboard-compact #brigadeWork .metric b{font-size:18px;line-height:1.15}
body.dashboard-compact #brigadeWork .metric span{font-size:9px;line-height:1.2;margin-top:3px;white-space:normal;overflow:visible}
body.dashboard-compact #numberCardsBtn{min-height:44px;margin-bottom:5px!important;padding:7px 8px;border-radius:10px;font-size:12px;line-height:1.2}
body.dashboard-compact #orderPreparation .metric{min-height:60px;padding:7px 8px;border-radius:11px}
body.dashboard-compact #orderPreparation .metric b{font-size:15px;line-height:1.2}
body.dashboard-compact #orderPreparation .metric span{font-size:10px;line-height:1.2;margin-top:3px;white-space:normal;overflow:visible}
body.dashboard-compact #employeeReadiness>.section-head{margin:6px 0 5px}
@media(max-width:350px){body.dashboard-compact .topbar h1{font-size:18px}body.dashboard-compact #selectedDayHeader h2{font-size:15px}body.dashboard-compact .dashboard-date-value{font-size:10px}}
</style></head>""", 1)


INLINE_INDEX_HTML = _compact_dispatcher_dashboard_html(INLINE_INDEX_HTML)


def _single_row_readiness_actions_html(html):
    replacements = (
        ('<button class="primary small" id="readinessCountBtn">📊 Посчитать</button>', ''),
        ("qs('#readinessCountBtn').onclick=refreshReadinessCount;", ''),
        ('<button class="primary small" id="readinessSendBtn">Сверка в чат 📝</button>',
         '<button class="primary small" id="readinessSendBtn" title="Сверка в чат" aria-label="Отправить сверку готовности в чат">📝</button>'),
    )
    for old, new in replacements:
        if html.count(old) != 1:
            raise ValueError('Single-row readiness action anchor missing: ' + old[:80])
        html = html.replace(old, new, 1)
    return html.replace('</head>', """<style id="single-row-readiness-actions-style">
#employeeReadiness .section-actions.readiness-actions{grid-template-columns:minmax(0,1.65fr) 44px minmax(0,1.15fr) 44px;gap:5px;margin-top:6px}
#employeeReadiness .readiness-actions>button{min-width:0;min-height:44px;padding:4px;font-size:11px;border-radius:10px}
#employeeReadiness #readinessHistoryBtn{grid-column:1;grid-row:1;padding:4px}
#employeeReadiness #readinessHistoryBtn b{font-size:11px;line-height:1.15}
#employeeReadiness #readinessHistoryBtn small{font-size:9px!important;line-height:1.1;margin-top:1px}
#employeeReadiness .readiness-actions #readinessRemindersBtn{grid-column:2;grid-row:1;font-size:20px;padding:3px}
#employeeReadiness #readinessAvralBtn{grid-column:3;grid-row:1;font-size:11px;padding:4px 2px}
#employeeReadiness #readinessSendBtn{grid-column:4;grid-row:1;font-size:20px;padding:3px}
@media(max-width:350px){#employeeReadiness #readinessAvralBtn{font-size:10px}}
</style></head>""", 1)


INLINE_INDEX_HTML = _single_row_readiness_actions_html(INLINE_INDEX_HTML)


def _dense_readiness_dashboard_html(html):
    """Inline section dates and compact only employee readiness list dialogs."""
    def replace(old, new):
        if html.count(old) != 1:
            raise ValueError('Dense readiness dashboard anchor missing: ' + old[:100])
        return html.replace(old, new, 1)

    html = replace('<h2>Контроль бригады утром</h2><p class="muted">${esc(fmtDate(day))}${future?\' · В день заказа\':\'\'}</p>',
        '<div class="dashboard-section-heading"><h2>Контроль бригады утром</h2><p class="muted dashboard-section-date">${esc(fmtDate(day))}</p></div>')
    html = replace('<h2>${closingTitle}</h2><p class="muted">${esc(fmtDate(day))} · Номера и закрытие относятся к дате заказа</p>',
        '<h2>${closingTitle}</h2>')
    html = replace('<h2 id="readinessTitle">Готовность сотрудников</h2><p id="readinessDayLabel" class="muted"></p>',
        '<div class="dashboard-section-heading readiness-section-heading"><h2 id="readinessTitle">Готовность сотрудников</h2><p id="readinessDayLabel" class="muted dashboard-section-date"></p></div>')
    html = replace("qs('#readinessDayLabel').textContent=fmtDate(nextReadinessDay(day))+' · Время сводки: '+(s.readiness_cutoff_label||'14:00');",
        "qs('#readinessDayLabel').textContent=fmtDate(nextReadinessDay(day));")
    html = replace("qs('#readinessDayLabel').textContent=fmtDate(day)+' · Время сводки: '+(r.cutoff_label||'14:00');",
        "qs('#readinessDayLabel').textContent=fmtDate(day);")
    html = replace("function showModal(title, body) {rememberCashTablePosition();",
        "function showModal(title, body) {qs('#modal').classList.toggle('readiness-people-modal',body.includes('readiness-people-grid'));rememberCashTablePosition();")
    # Scope grids to readiness rows; order, finance and profile forms are unchanged.
    start = html.index('function readinessPersonRows(')
    end = html.index('function captureEmployeeScroll()', start)
    block = html[start:end]
    if block.count('<div class="list">') != 1 or block.count('<div style="display:grid;gap:8px;margin-top:12px">') != 1:
        raise ValueError('Readiness people/selector grid anchors missing')
    block = block.replace('<div class="list">', '<div class="list readiness-people-grid">', 1)
    block = block.replace('<div style="display:grid;gap:8px;margin-top:12px">', '<div class="readiness-people-grid readiness-choice-grid">', 1)
    block = block.replace('employeeListName(x)', 'readinessEmployeeName(x)')
    html = html[:start] + r'''function readinessEmployeeName(employee){
  const name=String(employee.first_name||employee.full_name||employee.display_name||'').trim().split(/\s+/)[0]||'—';
  const metro=String(employee.metro||'').trim()||'—';
  const height=Number(employee.height_cm)||'—';
  return `${name} ${metro} ${height} см`;
}
''' + block + html[end:]
    start = html.index('function chooseUnansweredEmployee(')
    end = html.index('async function loadReadiness()', start)
    block = html[start:end]
    if block.count('<div class="list">') != 1 or block.count('<div style="display:grid;gap:8px">') != 1:
        raise ValueError('Grouped readiness grid anchors missing')
    block = block.replace('<div class="list">', '<div class="list readiness-people-grid">', 1)
    block = block.replace('<div style="display:grid;gap:8px">', '<div class="readiness-people-grid readiness-choice-grid">', 1)
    block = block.replace('employeeListName(x)', 'readinessEmployeeName(x)')
    block = block.replace('employeeListName(employee)', 'readinessEmployeeName(employee)')
    html = html[:start] + block + html[end:]
    return html.replace('</head>', """<style id="dense-readiness-dashboard-style">
.dashboard-section-heading{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 5px}
.dashboard-section-heading h2{min-width:0;flex:1;margin:0!important}
.dashboard-section-heading .dashboard-section-date{flex:none;margin:0;font-size:11px;line-height:1.25;text-align:right;white-space:nowrap;color:var(--muted)}
body.dashboard-compact #brigadeWork .dashboard-section-heading h2{font-size:14px}
body.dashboard-compact #employeeReadiness>.section-head{display:block;margin:5px 0}
body.dashboard-compact #employeeReadiness .readiness-section-heading h2{font-size:14px;line-height:1.2;white-space:nowrap;width:auto;flex:0 0 auto;letter-spacing:normal}
body.dashboard-compact #employeeReadiness .readiness-section-heading .dashboard-section-date{font-size:9px;margin:0 0 0 auto}
body.dashboard-compact #employeeReadiness .readiness-section-heading{margin:0 0 4px;flex-wrap:wrap;column-gap:6px;row-gap:2px}
body.dashboard-compact #employeeReadiness .readiness-actions{margin-top:0}
body.dashboard-compact #brigadeWork,body.dashboard-compact #orderPreparation{margin:5px 0!important;padding-top:4px}
body.dashboard-compact #readinessSummary{gap:5px;margin:5px 0}
body.dashboard-compact #readinessSummary .metric{min-height:48px;padding:6px 8px;border-radius:10px}
body.dashboard-compact #readinessSummary .metric b{font-size:18px;line-height:1.15}
body.dashboard-compact #readinessSummary .metric span{font-size:10px;margin-top:2px}
body.dashboard-compact #readinessGroups .group-block{margin-bottom:6px;border-radius:12px}
body.dashboard-compact #readinessGroups .group-title{padding:8px 10px;font-size:14px}
body.dashboard-compact #readinessGroups .group-stats{gap:4px;padding:0 6px 6px}
body.dashboard-compact #readinessGroups .group-stats button{min-height:44px;padding:5px 2px;font-size:9px;border-radius:8px}
body.dashboard-compact #readinessGroups .group-stats b{font-size:16px;line-height:1.1}
#modal.readiness-people-modal{left:6px;right:6px;top:max(8px,env(safe-area-inset-top,0px));max-height:calc(100dvh - 16px - var(--safe-bottom));padding:10px 8px calc(10px + var(--safe-bottom));border-radius:15px}
#modal.readiness-people-modal .modal-head{position:sticky;top:-10px;z-index:1;background:#111114;padding:2px 0 6px;gap:6px}
#modal.readiness-people-modal .modal-head h3{font-size:17px;line-height:1.2;margin:0}
#modal.readiness-people-modal .close-btn{min-width:44px;min-height:44px}
#modal.readiness-people-modal .attention{padding:6px 8px;margin:0 0 6px;border-radius:9px}
#modal.readiness-people-modal .attention h4{font-size:12px;margin:0}
#modal.readiness-people-modal .attention p{font-size:10px;margin:2px 0}
#modal.readiness-people-modal .drawer-section{margin-top:8px}
#modal.readiness-people-modal .drawer-section h4{margin:0 0 4px;font-size:11px}
#modal .readiness-people-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;align-items:start}
#modal .readiness-people-grid .employee-row{display:grid;grid-template-columns:minmax(0,1fr) 44px;gap:0 3px;align-items:center;min-width:0;min-height:48px;padding:3px 4px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
#modal .readiness-people-grid .name{min-width:0;grid-column:1;grid-row:1}
#modal .readiness-people-grid .name b{font-size:11px;line-height:1.25;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
#modal .readiness-people-grid .name small{font-size:9px;line-height:1.15;margin-top:1px}
#modal .readiness-people-grid .car{grid-column:1;grid-row:2;font-size:11px;line-height:1;gap:3px!important;min-width:0}
#modal .readiness-people-grid .car:empty{display:none!important}
#modal .readiness-people-grid .car span{min-width:20px!important;height:20px!important;padding:0 3px!important;font-size:11px!important}
#modal .readiness-people-grid .employee-row>button{grid-column:2;grid-row:1 / span 2;width:44px;min-width:44px!important;min-height:44px!important;padding:2px!important;font-size:16px!important;border-radius:7px}
#modal .readiness-choice-grid>button{min-width:0;min-height:44px!important;padding:5px!important;font-size:11px;line-height:1.25;overflow-wrap:anywhere}
@supports not(height:100dvh){#modal.readiness-people-modal{max-height:calc(100vh - 16px - var(--safe-bottom))}}
</style></head>""", 1)


INLINE_INDEX_HTML = _dense_readiness_dashboard_html(INLINE_INDEX_HTML)


def _square_photo_indicators_html(html):
    replacements = (
        ('function renderOwnerBrigadeControl(){', r'''function controlPhotoMarkers(card){
  const received=card.source==='gbu'?[card.first_received,card.second_received]:[card.first_received];
  return '<span class="bc-photo-marks">'+received.map((ready,index)=>
    `<span class="bc-photo-mark ${ready?'is-received':'is-waiting'}" data-photo-slot="${index+1}" title="Фото ${index+1}: ${ready?'получено':'ожидается'}" aria-label="Фото ${index+1}: ${ready?'получено':'ожидается'}">${ready?cabinetIcon('check'):'⏳'}</span>`
  ).join('')+'</span>';
}
function renderOwnerBrigadeControl(){'''),
        ('${count!==null?esc(count):ready?cabinetIcon(\'check\'):waiting}',
         '${count!==null?(key===\'photo\'?count:esc(count)):ready?cabinetIcon(\'check\'):waiting}'),
        ('card.photos_complete,`${card.photos_received}/${card.photos_required}`)',
         'card.photos_complete,controlPhotoMarkers(card))'),
    )
    for old, new in replacements:
        if html.count(old) != 1:
            raise ValueError('Square photo indicator anchor missing: ' + old[:100])
        html = html.replace(old, new, 1)
    return html.replace('</head>', """<style id="square-photo-indicators-style">
#ownerBrigadeControlContent .bc-photo-marks{display:inline-flex;align-items:center;gap:1px;white-space:nowrap;vertical-align:middle}
#ownerBrigadeControlContent .bc-photo-mark{display:inline-block;flex:none;font-size:10px;line-height:1}
#ownerBrigadeControlContent .bc-stage-photo b{white-space:nowrap;overflow-wrap:normal}
@media(max-width:350px){#ownerBrigadeControlContent .bc-stage-photo{grid-template-columns:8px minmax(0,1fr) auto;gap:1px}#ownerBrigadeControlContent .bc-stage-photo .cabinet-icon{width:8px;height:8px}#ownerBrigadeControlContent .bc-stage-photo b{font-size:9px}#ownerBrigadeControlContent .bc-photo-mark{font-size:9px}}
</style></head>""", 1)


INLINE_INDEX_HTML = _square_photo_indicators_html(INLINE_INDEX_HTML)


def _compact_ready_employee_rows_html(html):
    replacements = (
        ('async function openReadinessGroupedList(mode,day=state.selectedDate){', r'''function readyEmployeeGroup(code,label,people,options={}){
  const statuses={ready:'Готов',not_ready:'На основной',day_off:'Выходной',responded:'Свободен',no_response:'Не отписался'};
  return `<section class="drawer-section ready-employee-group" data-ready-group="${esc(code)}"><h4>${esc(label)} · ${people.length}</h4>${people.length?
    `<div class="list readiness-people-grid ready-employee-grid">${people.map((employee,index)=>
      `<div class="employee-row ready-employee-row"><div class="name"><b>${esc(readinessEmployeeName(employee).replace(/\s+см$/,''))}${employee.has_car?' <span class="ready-employee-car" role="img" aria-label="На автомобиле">🚗</span>':''}</b>${options.showStatuses?`<small>${esc(statuses[employee.readiness_status]||'Отписался')}</small>`:''}</div><button type="button" class="secondary" ${options.indexOnly?`data-subgroup-edit="${index}"`:`data-person-status="${esc(code)}:${index}"`} aria-label="Изменить готовность ${esc(readinessEmployeeName(employee))}" title="Изменить готовность">✍🏻</button></div>`
    ).join('')}</div>`:`<p class="muted ready-group-empty">${esc(options.emptyLabel||'Готовых сотрудников нет')}</p>`}</section>`;
}
async function openReadinessGroupedList(mode,day=state.selectedDate){'''),
        ('      editGroups.push({code,label:g.label||label,people});',
         "      editGroups.push({code,label:g.label||label,people});\n      if(mode==='ready'||mode==='no_response')return readyEmployeeGroup(code,g.label||label,people,{emptyLabel:mode==='ready'?'Готовых сотрудников нет':'Все сотрудники отписались'});"),
        ('${readinessPersonRows(rows,true)}',
         "${readyEmployeeGroup(code,groupLabel,rows,{indexOnly:true,showStatuses:status==='wrote',emptyLabel:'В этой категории сотрудников нет'})}"),
        ("body.includes('readiness-people-grid')", "(body.includes('readiness-people-grid')||body.includes('ready-employee-group'))"),
    )
    for old, new in replacements:
        if html.count(old) != 1:
            raise ValueError('Compact ready employee rows anchor missing: ' + old[:100])
        html = html.replace(old, new, 1)
    return html.replace('</head>', """<style id="compact-ready-employee-rows-style">
#modal.readiness-people-modal .ready-employee-group{width:100%;min-width:0;margin:8px 0 0;border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
#modal.readiness-people-modal .ready-employee-group h4{margin:0;padding:8px 7px;font-size:13px;line-height:1.25;letter-spacing:normal;text-transform:none;color:var(--gold);font-weight:800;background:#211d15;border-bottom:1px solid var(--line)}
#modal .ready-employee-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:0;align-items:stretch;padding:0 5px}
#modal .ready-employee-grid .ready-employee-row{grid-template-columns:minmax(0,1fr) 32px;gap:3px;min-height:36px;padding:2px 3px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent}
#modal .ready-employee-grid .ready-employee-row:nth-child(odd){border-right:1px solid var(--line)}
#modal .ready-employee-grid .name{grid-row:1}
#modal .ready-employee-grid .name b{font-size:11px;font-weight:400;line-height:1.25;white-space:normal;overflow:visible;overflow-wrap:anywhere}
#modal .ready-employee-grid .ready-employee-row>button{grid-row:1;width:32px;min-width:32px!important;min-height:32px!important;padding:2px!important;font-size:15px!important;border-radius:7px;background:var(--panel2)}
#modal .ready-employee-car{font-size:11px;white-space:nowrap}
#modal .ready-group-empty{margin:0;padding:8px 7px;font-size:11px}
</style></head>""", 1)


INLINE_INDEX_HTML = _compact_ready_employee_rows_html(INLINE_INDEX_HTML)


def _compact_owner_expanded_cash_html(html):
    start = html.index('async function openCashTable(){')
    end = html.index('let ownerCabinetRequest=', start)
    section = html[start:end]
    def replace(old, new):
        nonlocal section
        if old not in section:
            raise RuntimeError('Expanded cash UI anchor missing: ' + old[:70])
        section = section.replace(old, new, 1)
    replace('  const resume=cashTableReturnState;',
            '  const compactCash=!!state.bootstrap?.is_owner;\n  const resume=cashTableReturnState;')
    replace("let cashRange=resume?.range||'month'", "let cashRange=compactCash?'month':resume?.range||'month'")
    replace("showModal('📒 Мои таблицы', `", "showModal(compactCash?'Касса':'📒 Мои таблицы', compactCash?ownerCashControls(cashMonth):`")
    replace('  const cashSession=cashModalGeneration;',
            "  qs('#modal').classList.toggle('owner-cash-modal',compactCash);\n  const cashSession=cashModalGeneration;")
    replace('const matrix=brigadiers.length ? `', 'const matrix=compactCash?ownerCashMatrix(ledger):brigadiers.length ? `')
    replace('      body.innerHTML=`\n        <div class="summary-grid compact">',
            '''      body.innerHTML=compactCash?`<div class="owner-cash-period"><span>Суммы в ₽ · точка — откат</span><button type="button" class="secondary" id="cashReminder" ${cashReminderSending?'disabled':''}>🔔 Сверка</button></div>${matrix}<div id="ownerCashDetail" hidden></div>`:`
        <div class="summary-grid compact">''')
    replace("      qsa('[data-cash-edit]',body)", '''      if(compactCash)bindOwnerCashDetails(body,ledger,cashActive);
      qsa('[data-cash-edit]',body)''')
    replace('if(currentScroller){currentScroller.scrollLeft=savedTableScroll;',
            'if(compactCash&&currentScroller)fitOwnerCashMatrix(currentScroller,brigadiers.length,days.length);\n        if(currentScroller){currentScroller.scrollLeft=savedTableScroll;')
    html = html[:start] + section + html[end:]
    html = html.replace('function showModal(title, body) {',
        "function showModal(title, body) {qs('#modal').classList.remove('owner-cash-modal');", 1)
    return html.replace('</body>', r'''
<style>
#modal.owner-cash-modal{left:0;right:0;top:4px;bottom:calc(72px + var(--safe-bottom));width:auto;max-width:1100px;margin:0 auto;transform:none;padding:6px;border-radius:12px;overflow:auto;max-height:none}
#modal.owner-cash-modal .modal-head{margin:0 0 4px;min-height:30px;gap:6px}
#modal.owner-cash-modal .modal-head h3{font-size:18px;margin:0}
#modal.owner-cash-modal .close-btn{width:30px;height:30px;min-height:30px;padding:0;border-radius:8px;font-size:20px}
#modal.owner-cash-modal .owner-cash-controls{display:flex;align-items:center;gap:4px;margin-bottom:4px}
#modal.owner-cash-modal .owner-cash-controls button,#modal.owner-cash-modal button[data-cash-range],#modal.owner-cash-modal #cashMonthTabs button{padding:4px 6px;min-height:28px;font-size:11px;border-radius:7px;white-space:nowrap}
#modal.owner-cash-modal .owner-cash-controls label{flex:1;min-width:0}
#modal.owner-cash-modal #cashMonth{width:100%;min-width:0;box-sizing:border-box;padding:3px 5px;font-size:14px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px;min-height:28px}
#modal.owner-cash-modal .owner-cash-ranges{display:flex;gap:4px}
#modal.owner-cash-modal #cashMonthTabs{display:flex;gap:4px;overflow-x:auto;margin:3px 0;scrollbar-width:none}
#modal.owner-cash-modal .owner-cash-period{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:10px;color:var(--muted);margin:3px 0}
#modal.owner-cash-modal #cashReminder{padding:3px 6px;min-height:26px;font-size:11px;border-radius:7px}
#modal.owner-cash-modal #cashMatrixScroller{--cash-col:72px;--cash-day:42px;--cash-total:80px;--cash-row:30px;overflow:auto;margin:0;border:1px solid var(--line);border-radius:8px;max-height:none}
#modal.owner-cash-modal .owner-cash-matrix{border-collapse:separate;border-spacing:0;table-layout:fixed;font-size:13px;font-variant-numeric:tabular-nums}
#modal.owner-cash-modal .owner-cash-matrix th,#modal.owner-cash-modal .owner-cash-matrix td{padding:0 3px;height:var(--cash-row);text-align:center;border-bottom:1px solid var(--line);border-right:1px solid var(--line);white-space:nowrap;overflow:hidden}
#modal.owner-cash-modal .owner-cash-matrix th{height:44px;background:var(--panel2);font-size:12px;font-weight:650;line-height:1.35;position:sticky;top:0;z-index:3}
#modal.owner-cash-modal .owner-cash-matrix th button{padding:0 1px;font-size:inherit;font-weight:inherit;line-height:inherit}
#modal.owner-cash-modal .owner-cash-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modal.owner-cash-modal .owner-cash-matrix button{display:block;width:100%;height:100%;min-width:0;min-height:0;padding:0;border:0;border-radius:0;color:inherit;background:none;font-size:inherit;font-weight:inherit;line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#modal.owner-cash-modal .owner-cash-matrix .cash-day{position:sticky;left:0;z-index:2;background:var(--panel)}
#modal.owner-cash-modal .owner-cash-matrix .cash-total{position:sticky;right:0;z-index:2;color:var(--gold);background:#211d15;font-weight:650}
#modal.owner-cash-modal .owner-cash-matrix thead .cash-day,#modal.owner-cash-modal .owner-cash-matrix thead .cash-total{z-index:5}
#modal.owner-cash-modal .owner-cash-matrix .cash-subtotal td{background:#211d15;color:var(--gold);height:calc(var(--cash-row) + 3px);font-weight:650}
#modal.owner-cash-modal .owner-cash-value{position:relative;display:inline-block;max-width:calc(100% - 5px);vertical-align:middle}
#modal.owner-cash-modal .cash-kickback-dot{position:absolute;right:-4px;top:0;width:3px;height:3px;border-radius:50%;background:var(--gold);pointer-events:none}
#modal.owner-cash-modal .owner-cash-hint{font-size:10px;color:var(--muted);text-align:center;margin:4px 0 0}
#modal.owner-cash-modal #ownerCashDetail{padding:10px;margin-top:6px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
#modal.owner-cash-modal #ownerCashDetail h4{margin:0 0 6px;font-size:14px}
#modal.owner-cash-modal #ownerCashDetail p{font-size:12px;margin:4px 0;overflow-wrap:anywhere}
#modal.owner-cash-modal #ownerCashDetail button{font-size:12px;padding:5px 8px;margin-top:6px}
</style>
<script>
function ownerCashControls(month){
 return `<div class="owner-cash-controls"><button type="button" class="secondary" onclick="rememberCashTablePosition();openPersonalTables(earningsReturnState)" aria-label="Назад к заработку">←</button><button type="button" class="secondary" id="cashPrevMonth" aria-label="Предыдущий месяц">‹</button><label><span class="hidden">Месяц кассы</span><input id="cashMonth" type="month" value="${esc(month)}" aria-label="Месяц кассы"></label><button type="button" class="secondary" id="cashNextMonth" aria-label="Следующий месяц">›</button></div><div class="owner-cash-ranges"><button type="button" class="primary" data-cash-range="month">Месяц</button></div><div id="cashMonthTools"><div id="cashMonthTabs"></div></div><div id="cashTableBody">Загрузка…</div>`;
}
function ownerCashShortName(label){
 const parts=String(label||'').trim().split(/\s+/),first=parts.shift()||'—';
 const metro=parts.join(' ').replace(/\s+\d{3}(?:\s*см)?$/,'');
 return `<span class="owner-cash-name">${esc(first)}</span><span class="owner-cash-name">${esc(metro)}</span>`;
}
function ownerCashMatrix(ledger){
 const brigs=ledger.brigadiers||[],days=ledger.days||[];
 if(!brigs.length)return '<div class="empty">Кассовые бригадиры пока не добавлены</div>';
 const n=v=>String(Number(v)||0),value=(commission,kickback,empty=false)=>`<span class="owner-cash-value">${empty&&!Number(commission)?'—':esc(n(commission))}${Number(kickback)?'<i class="cash-kickback-dot" aria-hidden="true"></i>':''}</span>`;
 const btn=(commission,kickback,attrs,label,empty=false)=>`<button type="button" ${attrs} aria-label="${esc(label)}. Касса ${n(commission)} ₽, откат ${n(kickback)} ₽">${value(commission,kickback,empty)}</button>`;
 const cell=(day,id)=>(day.cells||[]).find(c=>String(c.brigadier_tg_id)===String(id))||{};
 function summary(label,subset){
  const sum=(id,key)=>subset.reduce((a,d)=>a+Number(id===null?d.day?.[key]||0:cell(d,id)[key]||0),0);
  return `<tr class="cash-subtotal" data-cash-subtotal="${esc(label)}"><td class="cash-day">${esc(label)}</td>${brigs.map(b=>`<td>${btn(sum(b.tg_id,'commission_rub'),sum(b.tg_id,'kickback_rub'),`data-owner-cash-summary="${esc(label)}" data-owner-cash-brig="${b.tg_id}"`,b.name+' · '+label)}</td>`).join('')}<td class="cash-total">${btn(sum(null,'commission_rub'),sum(null,'kickback_rub'),`data-owner-cash-summary="${esc(label)}"`,label+' · все бригадиры')}</td></tr>`;
 }
 let rows='';
 for(const day of days){
  rows+=`<tr data-owner-cash-day="${esc(day.work_date)}"><td class="cash-day"><button type="button" data-owner-cash-check="${esc(day.work_date)}" aria-label="Проверить кассу за ${esc(day.work_date)}">${dayNumber(day.work_date)}</button></td>${brigs.map(b=>{const c=cell(day,b.tg_id);return `<td>${btn(c.commission_rub,c.kickback_rub,`data-owner-cash-date="${esc(day.work_date)}" data-owner-cash-brig="${b.tg_id}"`,b.name+' · '+fmtDate(day.work_date),true)}</td>`}).join('')}<td class="cash-total">${btn(day.day?.commission_rub,day.day?.kickback_rub,`data-owner-cash-date="${esc(day.work_date)}"`,'Итого · '+fmtDate(day.work_date))}</td></tr>`;
  if(Number(dayNumber(day.work_date))===15)rows+=summary('1–15',days.filter(d=>dayNumber(d.work_date)<=15));
 }
 if(days.some(d=>dayNumber(d.work_date)>15))rows+=summary('16–'+dayNumber(days.at(-1).work_date),days.filter(d=>dayNumber(d.work_date)>15));
 rows+=summary(days[0]?.work_date.slice(-2)==='01'&&dayNumber(days.at(-1)?.work_date)>15?'Месяц':'Период',days);
 return `<div id="cashMatrixScroller" role="region" aria-label="Расширенная касса по дням и бригадирам"><table class="owner-cash-matrix"><colgroup><col style="width:var(--cash-day)">${brigs.map(()=>'<col style="width:var(--cash-col)">').join('')}<col style="width:var(--cash-total)"></colgroup><thead><tr><th class="cash-day">День</th>${brigs.map(b=>`<th><button type="button" data-owner-cash-name="${b.tg_id}" aria-label="${esc(b.name)}" title="${esc(b.name)}">${ownerCashShortName(b.name)}</button></th>`).join('')}<th class="cash-total">Итого</th></tr></thead><tbody>${rows}</tbody></table></div><p class="owner-cash-hint">Нажмите сумму — подробности · ↔ остальные бригадиры</p>`;
}
function fitOwnerCashMatrix(scroller,count,days){
 const width=scroller.clientWidth||Math.max(280,window.innerWidth-14),day=42,total=80;
 const col=Math.max(64,(width-day-total)/Math.min(4,count)),table=scroller.querySelector('table');
 scroller.style.setProperty('--cash-col',col+'px');scroller.style.setProperty('--cash-day',day+'px');scroller.style.setProperty('--cash-total',total+'px');
 table.style.width=(day+total+col*count)+'px';
 const modal=qs('#modal'),viewport=window.visualViewport?.height||window.innerHeight;
 const bottom=modal?.getBoundingClientRect().bottom||viewport-82;
 const available=Math.max(160,bottom-scroller.getBoundingClientRect().top-8);
 const header=table.querySelector('thead').offsetHeight||table.querySelector('thead').getBoundingClientRect().height||44;
 if(modal?.classList.contains('owner-cash-modal')){
  fitTwoPeriodPages(scroller,available,header,false);
 }
}
function bindOwnerCashDetails(body,ledger,active){
 const host=qs('#ownerCashDetail',body);let request=0;
 const money=n=>new Intl.NumberFormat('ru-RU').format(Number(n)||0)+' ₽';
 const show=(title,content)=>{host.hidden=false;host.innerHTML=`<h4>${esc(title)}</h4>${content}<button type="button" class="secondary" data-owner-cash-hide>Свернуть</button>`;host.querySelector('[data-owner-cash-hide]').onclick=()=>{request++;host.hidden=true};host.scrollIntoView?.({block:'nearest'})};
 body.onclick=async event=>{
  const button=event.target.closest('button');if(!button||!body.contains(button)||!active())return;
  if(button.dataset.ownerCashCheck){rememberCashTablePosition();openCashDayCheck(button.dataset.ownerCashCheck);return}
  if(button.dataset.ownerCashName){const b=ledger.brigadiers.find(b=>String(b.tg_id)===button.dataset.ownerCashName);if(b)show(b.name,'');return}
  const date=button.dataset.ownerCashDate,label=button.dataset.ownerCashSummary,id=button.dataset.ownerCashBrig;
  if(!date&&!label)return;
  const seq=++request,b=id?ledger.brigadiers.find(b=>String(b.tg_id)===id):null;
  const days=date?ledger.days.filter(d=>d.work_date===date):ledger.days.filter(d=>label==='1–15'?dayNumber(d.work_date)<=15:label.startsWith('16–')?dayNumber(d.work_date)>15:true);
  const totals=days.reduce((out,d)=>{const c=id?(d.cells||[]).find(c=>String(c.brigadier_tg_id)===id)||{}:d.day||{};out.commission+=Number(c.commission_rub)||0;out.kickback+=Number(c.kickback_rub)||0;return out},{commission:0,kickback:0});
  show((b?.name||'Все бригадиры')+' · '+(date?fmtDate(date):label),`<p>Касса: <b>${money(totals.commission)}</b></p><p>Откат отдельно: <b>${money(totals.kickback)}</b></p>${date?'<div data-owner-cash-entries>Загрузка записей…</div>':''}`);
  if(!date)return;
  try{const result=await api('/api/cash?work_date='+encodeURIComponent(date));if(!active()||seq!==request)return;
   const rows=(result.rows||[]).filter(x=>!id||String(x.brigadier_tg_id)===id);
   host.querySelector('[data-owner-cash-entries]').innerHTML=rows.map(x=>`<p>${esc(x.brigadier_name)} · ${esc(x.category_label||x.category||'')} · ${Number(x.team_size)||0} чел${x.reserve_count?' · резерв '+Number(x.reserve_count):''}<br>Касса ${money(x.commission_rub)} · откат ${money(x.kickback_rub)}</p>`).join('')||'<p>Записей за этот день нет.</p>';
  }catch(e){if(active()&&seq===request)host.querySelector('[data-owner-cash-entries]').textContent=e.message}
 };
}
</script></body>''', 1)


INLINE_INDEX_HTML = _compact_owner_expanded_cash_html(INLINE_INDEX_HTML)


def _exclude_reserve_from_unanswered_html(html):
    old = 'const sections=Object.entries(groupNames).map(([code,label])=>{'
    new = "const sections=Object.entries(groupNames).filter(([code])=>mode!=='no_response'||['brigadier','main','cashless'].includes(code)).map(([code,label])=>{"
    count = 'const currentNoResponse=groupEntries.reduce((sum,[,g])=>sum+Number(g?.counts?.no_response||0),0);'
    filtered = "const currentNoResponse=groupEntries.filter(([code])=>['brigadier','main','cashless'].includes(code)).reduce((sum,[,g])=>sum+Number(g?.counts?.no_response||0),0);"
    for before, after in ((old, new), (count, filtered)):
        if html.count(before) != 1:
            raise RuntimeError('Unanswered readiness UI anchor missing: ' + before)
        html = html.replace(before, after, 1)
    return html


INLINE_INDEX_HTML = _exclude_reserve_from_unanswered_html(INLINE_INDEX_HTML)


def _personal_first_half_view_html(html):
    start = html.index('async function openPersonalTables(resume=null){')
    end = html.index('</script>', start)
    section = html[start:end]
    cash_button = '<button class="secondary" id="earningsTables">📒 Мои таблицы</button>'
    if cash_button not in section:
        raise RuntimeError('Expanded cash navigation button missing')
    section = section.replace(cash_button, '', 1)
    section = section.replace('<button class="secondary" data-personal-kind="kickbacks">Откаты</button></div>',
        '<button class="secondary" data-personal-kind="kickbacks">Откаты</button><button type="button" class="secondary" id="earningsTables">Расширенная касса</button></div>', 1)
    section = section.replace('<button class="secondary" id="personalAdd">＋ Столбец</button>', '', 1)
    add_handler = " qs('#personalAdd').onclick=()=>{if(busy||!data)return;const value=prompt('Название нового столбца');if(value!==null)save({action:'add',value})};"
    if add_handler not in section:
        raise RuntimeError('Personal add-column handler missing')
    section = section.replace(add_handler, '', 1)
    old = '<p class="muted">Касса и «Марине» — из общей кассы.'
    if old not in section:
        raise RuntimeError('Personal earnings introduction missing')
    section = section.replace(old, '<details class="personal-help"><summary>Как считается заработок</summary><p class="muted">Касса и «Марине» — из общей кассы.', 1)
    section = section.replace('Сохранение после каждой правки.</p><label>Месяц',
        'Сохранение после каждой правки.</p></details><label class="personal-month-label">Месяц', 1)
    old = " const grid=qs('#personalGrid'),status=qs('#personalStatus'),session=cashModalGeneration;"
    if old not in section:
        raise RuntimeError('Personal earnings grid missing')
    section = section.replace(old, old + "\n qs('#modal').classList.add('personal-first-half-modal');\n if(!resume){grid.scrollTop=0;qs('#modal').scrollTop=0}", 1)
    old = "  grid.scrollLeft=left;grid.scrollTop=top;qs('#modal').scrollTop=modalTop;"
    if old not in section:
        raise RuntimeError('Personal earnings scroll restoration missing')
    section = section.replace(old, "  fitPersonalFirstHalf(grid);\n" + old, 1)
    html = html[:start] + section + html[end:]
    html = html.replace('function showModal(title, body) {',
        "function showModal(title, body) {qs('#modal').classList.remove('personal-first-half-modal');", 1)
    return html.replace('</body>', r'''
<style>
#modal.personal-first-half-modal{left:4px;right:4px;top:4px;bottom:calc(76px + var(--safe-bottom));width:auto;max-width:1100px;margin:0 auto;transform:none;max-height:none;padding:8px 6px;overflow:auto}
#modal.personal-first-half-modal>.modal-head{position:relative;top:auto;margin:0 0 6px;min-height:32px}
#modal.personal-first-half-modal>.modal-head::before{display:none}
#modal.personal-first-half-modal>.modal-head h3{font-size:18px;margin:0}
#modal.personal-first-half-modal .close-btn{width:32px;height:32px;min-height:32px;padding:0;font-size:20px}
#modal.personal-first-half-modal>div:not(#personalGrid):not(.modal-head){margin:4px 0!important;gap:4px!important}
#modal.personal-first-half-modal .secondary,#modal.personal-first-half-modal .primary{font-size:12px;padding:6px 9px;min-height:30px;border-radius:9px}
#modal.personal-first-half-modal .personal-help{font-size:11px;color:var(--muted);margin:4px 0}
#modal.personal-first-half-modal .personal-help summary{cursor:pointer}
#modal.personal-first-half-modal .personal-month-label{display:flex;align-items:center;gap:7px;font-size:11px;margin:4px 0}
#modal.personal-first-half-modal #personalMonth{flex:1;min-width:0;width:auto!important;min-height:30px;padding:3px 5px;font-size:14px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:7px}
#modal.personal-first-half-modal #personalStatus:empty{display:none}
#modal.personal-first-half-modal #personalGrid{--personal-half-row:26px;max-height:none!important;margin-top:5px!important;overflow:auto}
#modal.personal-first-half-modal #personalGrid .personal-table tbody tr:not(.personal-total):not(.gbu-comment-row)>th,#modal.personal-first-half-modal #personalGrid .personal-table tbody tr:not(.personal-total):not(.gbu-comment-row)>td{height:var(--personal-half-row);padding:2px 1px;box-sizing:border-box}
#modal.personal-first-half-modal #personalGrid .personal-cell{height:calc(var(--personal-half-row) - 5px);min-height:0!important;padding:1px 0;font-size:inherit;line-height:1.15;box-sizing:border-box}
#modal.personal-first-half-modal #personalGrid .personal-column{min-height:38px;height:auto;padding:2px 0;font-size:inherit;border-radius:4px}
#modal.personal-first-half-modal #personalGrid .personal-table .personal-total th,#modal.personal-first-half-modal #personalGrid .personal-table .personal-total td{height:28px;padding:2px 1px;box-sizing:border-box}
</style>
<script>
function fitPersonalFirstHalf(grid){
 const modal=qs('#modal');if(!modal?.classList.contains('personal-first-half-modal')||!grid.querySelector('table'))return;
 const viewport=window.visualViewport?.height||window.innerHeight;
 const bottom=modal.getBoundingClientRect().bottom||viewport-76;
 const available=Math.max(160,bottom-grid.getBoundingClientRect().top-8);
 const header=grid.querySelector('thead').offsetHeight||grid.querySelector('thead').getBoundingClientRect().height||42;
 fitTwoPeriodPages(grid,available,header,true);
}
window.addEventListener('resize',()=>{const grid=qs('#personalGrid');if(grid)fitPersonalFirstHalf(grid)});
window.visualViewport?.addEventListener('resize',()=>{const grid=qs('#personalGrid');if(grid)fitPersonalFirstHalf(grid)});
function resizeOwnerCashFirstHalf(){const scroller=qs('#cashMatrixScroller');if(!qs('#modal')?.classList.contains('owner-cash-modal')||!scroller)return;fitOwnerCashMatrix(scroller,scroller.querySelector('thead').querySelectorAll('th').length-2,scroller.querySelectorAll('[data-owner-cash-day]').length)}
window.addEventListener('resize',resizeOwnerCashFirstHalf);
window.visualViewport?.addEventListener('resize',resizeOwnerCashFirstHalf);
</script></body>''', 1)


INLINE_INDEX_HTML = _personal_first_half_view_html(INLINE_INDEX_HTML)

# Give each half its own screen of rows, including its subtotal and the month
# total on screen two. Daily values and drill-down handlers remain unchanged.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<style>
#modal.personal-first-half-modal #personalGrid,#modal.owner-cash-modal #cashMatrixScroller{scroll-snap-type:y mandatory;scroll-padding-top:0}
#modal .period-table-page{position:relative;overflow:clip;scroll-snap-align:start;scroll-snap-stop:always;box-sizing:border-box}
#modal .period-table-page>table{transform-origin:top left}
#modal .period-table-page>table thead th{position:static!important}
#modal .period-table-page>table thead .cash-day{position:sticky!important;top:auto}
#modal .period-table-page>table thead .cash-total{position:sticky!important;top:auto}
#modal.personal-first-half-modal #personalGrid .personal-table th:first-child{width:42px;min-width:42px;max-width:42px;overflow-wrap:normal}
#modal.personal-first-half-modal #personalGrid .personal-total th:first-child{white-space:nowrap;font-size:10px;line-height:1.2}
#modal.personal-first-half-modal #personalGrid .personal-total td{white-space:nowrap;overflow-wrap:normal}
#modal.personal-first-half-modal #personalGrid tr[data-period-day]>th,
#modal.personal-first-half-modal #personalGrid tr[data-period-day]>td,
#modal.owner-cash-modal .owner-cash-matrix tr[data-period-day]>td{height:var(--period-row)!important;box-sizing:border-box}
#modal.personal-first-half-modal #personalGrid tr[data-period-day] .personal-cell{height:calc(var(--period-row) - 5px);font-size:inherit}
#modal.owner-cash-modal .owner-cash-matrix tr[data-period-day] button{height:calc(var(--period-row) - 1px)}
#modal.personal-first-half-modal #personalGrid tr[data-period-page="1"],
#modal.owner-cash-modal .owner-cash-matrix tr[data-period-page="1"]{--period-row:var(--period-first-row)}
#modal.personal-first-half-modal #personalGrid tr[data-period-page="2"],
#modal.owner-cash-modal .owner-cash-matrix tr[data-period-page="2"]{--period-row:var(--period-second-row)}
#modal.personal-first-half-modal #personalGrid tr[data-period-start],
#modal.owner-cash-modal .owner-cash-matrix tr[data-period-start]{scroll-snap-align:start}
#modal.personal-first-half-modal #personalGrid .period-page-gap>td,
#modal.owner-cash-modal .owner-cash-matrix .period-page-gap>td{padding:0;border:0;height:var(--period-gap);min-height:0;background:var(--panel);line-height:0;font-size:0;box-sizing:border-box}
#modal.owner-cash-modal .owner-cash-matrix .cash-subtotal>td{height:28px;box-sizing:border-box;font-size:12px}
</style>
<script>
function fitTwoPeriodPages(scroller,available,header,personal){
 const table=scroller.querySelector('table');if(!table)return;
 const previousHeight=Number(scroller.dataset.periodPageHeight)||available;
 const position=scroller.scrollTop/previousHeight;
 scroller.style.height=available+'px';scroller.style.maxHeight=available+'px';
 if(scroller.querySelector('.gbu-comment-row'))return;
 if(!scroller.querySelector('.period-table-page')){
  const body=table.querySelector('tbody');if(!body)return;
  const groups=[[],[]];let page=0;
  for(const row of Array.from(body.rows)){
   if(row.classList.contains('period-page-gap')){row.remove();continue}
   const total=row.classList.contains(personal?'personal-total':'cash-subtotal');
   const day=total?0:personal?Number(row.firstElementChild?.textContent):Number((row.dataset.ownerCashDay||'').slice(-2));
   if(day){page=day>15?1:0;row.dataset.periodDay=String(day)}
   else if(!total)continue;
   row.dataset.periodPage=String(page+1);groups[page].push(row);
   if(personal&&total){const label=row.firstElementChild;label.title=label.title||label.textContent;label.textContent=page===0?'1–15':groups[page].filter(r=>r.classList.contains('personal-total')).length===1?'16–'+groups[page].filter(r=>r.dataset.periodDay).at(-1)?.dataset.periodDay:'Месяц'}
  }
  if(groups.some(rows=>!rows.some(row=>row.dataset.periodDay)))return;
  // Separate tables include separate headers. Their fixed-height containers
  // make the last scroll position start with day 16, regardless of row minima.
  const second=table.cloneNode(true);second.querySelector('tbody').replaceChildren();
  groups[1].forEach(row=>second.querySelector('tbody').append(row));
  const fragment=document.createDocumentFragment();
  [table,second].forEach((item,index)=>{const panel=document.createElement('section');panel.className='period-table-page';panel.dataset.periodPage=String(index+1);panel.setAttribute('aria-label',index?'16–конец месяца и итоги':'1–15 и итог');panel.append(item);fragment.append(panel)});
  scroller.replaceChildren(fragment);
 }
 const pageHeight=Math.max(1,available-(scroller.offsetHeight-scroller.clientHeight||0));
 scroller.dataset.periodPageHeight=String(pageHeight);
 Array.from(scroller.querySelectorAll('.period-table-page')).forEach((panel,index)=>{
  const item=panel.querySelector('table'),days=Array.from(item.querySelectorAll('tbody tr[data-period-day]')),totals=Array.from(item.querySelectorAll(personal?'.personal-total':'.cash-subtotal'));
  item.style.transform='none';item.style.width=table.style.width;
  panel.style.height=pageHeight+'px';panel.style.width=personal?'100%':table.style.width;
  const ownHeader=item.querySelector('thead').offsetHeight||header;
  const totalHeight=totals.reduce((sum,row)=>sum+(row.offsetHeight||28),0);
  const rowHeight=Math.min(48,Math.max(1,(pageHeight-ownHeader-totalHeight-2)/days.length));
  scroller.style.setProperty(index?'--period-second-row':'--period-first-row',rowHeight+'px');
  days[0].dataset.periodStart=String(index+1);
  // CSS table row heights are minima. Measure actual layout and fit it too.
  const natural=item.offsetHeight||ownHeader+days.length*rowHeight+totalHeight;
  const scale=Math.min(1,(pageHeight-1)/natural);
  if(scale<1)item.style.transform='scaleY('+scale+')';
  panel.dataset.periodFitScale=String(scale);
 });
 scroller.scrollTop=Math.min(1,position)*pageHeight;
}
document.addEventListener('toggle',event=>{if(event.target.matches?.('.personal-help')){const grid=qs('#personalGrid');if(grid)fitPersonalFirstHalf(grid)}},true);
</script></body>''', 1)

# Group the brigadier's read-only contact directory into two compact columns.
# Scope every style to this directory so dispatch editing stays unchanged.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', r'''
<style id="brigadier-directory-compact-style">
#modal:has(#cabinetDirectory){left:6px;right:6px;top:6px;bottom:calc(76px + var(--safe-bottom));width:auto;max-width:1100px;margin:0 auto;transform:none;max-height:none;padding:8px;overflow:auto}
#modal:has(#cabinetDirectory)>.modal-head{position:relative;top:auto;margin:0 0 8px;min-height:34px}
#modal:has(#cabinetDirectory)>.modal-head::before{display:none}
#modal:has(#cabinetDirectory)>.modal-head h3{margin:0;font-size:18px}
#cabinetDirectory .cabinet-directory-search,#cabinetDirectory .cabinet-directory-filter{font-size:11px;margin-bottom:7px}
#cabinetDirectory .cabinet-directory-search input,#cabinetDirectory .cabinet-directory-filter select{font-size:13px;min-height:36px;margin-top:3px;padding:6px 8px;border-radius:8px}
#cabinetDirectory #cabinetStaffCount{font-size:11px;margin:5px 0 8px}
#cabinetDirectory .cabinet-directory-group{margin:8px 0 0;border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
#cabinetDirectory .cabinet-directory-group h4{margin:0;padding:8px 7px;font-size:13px;line-height:1.25;color:var(--gold);font-weight:800;background:#211d15;border-bottom:1px solid var(--line)}
#cabinetDirectory .cabinet-directory-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;align-items:stretch;padding:0 5px}
#cabinetDirectory .cabinet-directory-person{min-width:0;margin:0;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent}
#cabinetDirectory .cabinet-directory-person:nth-child(odd){border-right:1px solid var(--line)}
#cabinetDirectory .cabinet-directory-person summary{min-height:36px;padding:3px 4px;gap:4px}
#cabinetDirectory .cabinet-directory-person summary>span{flex:1;min-width:0;overflow-wrap:anywhere}
#cabinetDirectory .cabinet-directory-person b{font-size:11px;font-weight:650;line-height:1.25;white-space:normal}
#cabinetDirectory .cabinet-directory-number{color:var(--muted);font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
#cabinetDirectory .cabinet-directory-person summary>.cabinet-icon{width:12px;height:12px;flex-shrink:0;color:var(--muted)}
#cabinetDirectory .cabinet-directory-contacts{display:flex;flex-direction:column;gap:4px;padding:0 4px 5px;min-width:0}
#cabinetDirectory .cabinet-directory-contacts a{font-size:11px;min-height:36px;padding:5px 4px;gap:4px;border-radius:7px;overflow-wrap:anywhere}
#cabinetDirectory .cabinet-directory-contacts a>.cabinet-icon{width:13px;height:13px;flex-shrink:0}
#cabinetDirectory .cabinet-directory-contacts span{font-size:10px;padding:3px 0;overflow-wrap:anywhere}
</style></head>''', 1)


def _compact_earnings_toolbar_html(html):
    start = html.index('async function openPersonalTables(resume=null){')
    end = html.index('</script>', start)
    section = html[start:end]
    allowance_bar = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px"><button class="secondary" id="earningsVladAllowance">💰 Надбавка Владу</button></div>'
    if section.count(allowance_bar) != 1:
        raise RuntimeError('Earnings allowance toolbar anchor missing')
    section = section.replace(allowance_bar, '', 1)
    section, removed = re.subn(r'<details class="personal-help">.*?</details>', '', section, count=1, flags=re.S)
    if removed != 1:
        raise RuntimeError('Earnings help anchor missing')
    toolbar = '<div style="display:flex;gap:6px;overflow:auto;margin:12px 0"><button class="primary" data-personal-kind="earnings">'
    if section.count(toolbar) != 1:
        raise RuntimeError('Earnings tabs anchor missing')
    section = section.replace(toolbar, '<div class="personal-table-tabs"><button class="primary" data-personal-kind="earnings">', 1)
    cash_button = '<button type="button" class="secondary" id="earningsTables">Расширенная касса</button>'
    if section.count(cash_button) != 1:
        raise RuntimeError('Expanded cash tab anchor missing')
    section = section.replace(cash_button, cash_button + '<button type="button" class="secondary" id="earningsVladAllowance" aria-label="Надбавка Владу">💵 Владу</button>', 1)
    html = html[:start] + section + html[end:]
    return html.replace('</head>', r'''
<style id="compact-earnings-toolbar-style">
#modal.personal-first-half-modal>.personal-table-tabs{display:grid!important;grid-template-columns:minmax(0,1fr) minmax(0,.75fr) minmax(0,1.55fr) minmax(0,.8fr);gap:4px!important;overflow:visible;margin:4px 0!important}
#modal.personal-first-half-modal>.personal-table-tabs>button{min-width:0;min-height:30px;padding:6px 4px;font-size:clamp(9px,2.8vw,12px);line-height:1.2;white-space:nowrap;border-radius:8px}
</style></head>''', 1)


INLINE_INDEX_HTML = _compact_earnings_toolbar_html(INLINE_INDEX_HTML)


# Compact employee directories in every cabinet; dispatch users keep editing.
def _all_cabinet_employee_directory_html(html):
    start = html.index('async function loadEmployees(){')
    end = html.index('async function loadControl(){', start)
    section = html[start:end]
    render_start = section.index("  qs('#employeesList').innerHTML=filtered.length")
    render_end = section.index("  wireContactButtons(qs('#employeesList'));", render_start)
    section = section[:render_start] + r"""  const canEdit=state.bootstrap?.can_manage_cash===true&&!state.bootstrap?.cabinet_only;
  const entries=filtered.map((person,index)=>({person,index}));
  const directoryGroups=[['brigadier','Бригадиры'],['main','Основной состав'],['cashless','Безнал состав'],['reserve','Резерв']];
  qs('#employeesList').innerHTML=filtered.length?directoryGroups.map(([code,label])=>{
    const members=entries.filter(entry=>entry.person.group_code===code);
    if(!members.length)return '';
    return `<section class="staff-directory-group"><h4>${esc(label)} · ${members.length}</h4><div class="staff-directory-grid">${members.map(({person:x,index},groupIndex)=>
      `<article class="staff-directory-person${canEdit?' is-editable':''}"><details><summary><span><span class="staff-directory-number">${Math.floor(groupIndex/2)+1+(groupIndex%2?Math.ceil(members.length/2):0)}.</span> ${esc(readinessEmployeeName(x).replace(/\s+см$/,''))}${x.has_car?' 🚗':''}</span><span class="staff-contact-chevron" aria-hidden="true">›</span></summary><div class="staff-directory-contacts">${contactButtons(x)||'<small>Контакты не указаны</small>'}${cabinetAccessButton(x)}</div></details>${canEdit?`<button type="button" class="secondary" data-profile-edit="${index}" aria-label="Изменить данные сотрудника">✍🏻</button>`:''}</article>`
    ).join('')}</div></section>`;
  }).join(''):'<div class="empty">Ничего не найдено</div>';

""" + section[render_end:]
    html=html[:start]+section+html[end:]
    return html.replace('</head>', r"""
<style id="all-cabinet-employee-directory-style">
#cabinetNativeForm .cabinet-clock-label{font-weight:700;color:var(--text)}
#cabinetNativeForm [data-cabinet-clock]{min-height:56px;padding:12px 14px;font-size:22px;font-weight:700;color:var(--text);background:#25221b;border:2px solid var(--gold);border-radius:12px;letter-spacing:.04em;cursor:text}
#cabinetNativeForm [data-cabinet-clock]::placeholder{font-size:16px;color:#cdbf9f;font-weight:400;letter-spacing:normal;opacity:1}
#cabinetNativeForm [data-cabinet-clock]:focus{outline:2px solid var(--gold);outline-offset:2px;background:#302a1e}
#cabinetNativeForm .cabinet-clock-help{display:block;margin-top:6px;font-size:11px;font-weight:400;line-height:1.35;color:var(--muted)}
#employeesList{display:block}
#employeesList .staff-directory-group{margin:8px 0;border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}
#employeesList .staff-directory-group h4{margin:0;padding:8px 7px;color:var(--gold);font-size:13px;line-height:1.25;background:#211d15;border-bottom:1px solid var(--line)}
#employeesList .staff-directory-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;padding:0 5px;align-items:stretch}
#employeesList .staff-directory-person{min-width:0;display:grid;grid-template-columns:minmax(0,1fr);align-items:start;padding:2px 3px;border-bottom:1px solid var(--line);gap:3px}
#employeesList .staff-directory-person.is-editable{grid-template-columns:minmax(0,1fr) 32px}
#employeesList .staff-directory-person:nth-child(odd){border-right:1px solid var(--line)}
#employeesList .staff-directory-person details{min-width:0}
#employeesList .staff-directory-person summary{display:flex;align-items:center;justify-content:space-between;min-height:32px;gap:3px;font-size:11px;font-weight:650;line-height:1.25;cursor:pointer;list-style:none}
#employeesList .staff-directory-person summary::-webkit-details-marker{display:none}
#employeesList .staff-directory-person summary>span:first-child{min-width:0;overflow-wrap:anywhere}
#employeesList .staff-directory-number{color:var(--muted);font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}
#employeesList .staff-contact-chevron{color:var(--muted);flex-shrink:0}
#employeesList .staff-directory-person details[open] .staff-contact-chevron{transform:rotate(90deg)}
#employeesList .staff-directory-person>button{width:32px;min-width:32px;min-height:32px;padding:2px;font-size:15px;border-radius:7px}
#employeesList .staff-directory-contacts{min-width:0;font-size:10px;padding-bottom:4px}
#employeesList .contact-btns{display:flex;flex-direction:column;gap:4px;margin:3px 0}
#employeesList .contact-btn{min-width:0;min-height:32px;font-size:10px;padding:5px 4px;white-space:normal;overflow-wrap:anywhere}
#employeesList .cabinet-access-control{padding:4px 0;min-width:0}
#employeesList .cabinet-role-select{min-width:0;max-width:100%;font-size:10px}
#employeesList .cabinet-access-button{min-width:0;max-width:100%;font-size:10px;white-space:normal}
</style></head>""",1).replace('</body>', r"""
<script>
function normalizeCabinetClock(value){
 const raw=String(value??'').trim();
 const compact=/^\d{3,4}$/.test(raw)?raw.slice(0,-2)+':'+raw.slice(-2):raw;
 const match=/^(\d{1,2})[:.](\d{2})$/.exec(compact);
 return match&&Number(match[1])<24&&Number(match[2])<60?match[1].padStart(2,'0')+':'+match[2]:'';
}
</script></body>""",1)


INLINE_INDEX_HTML = _all_cabinet_employee_directory_html(INLINE_INDEX_HTML)


# Keep the agent directory separate from employee phone links, which make calls.
def _compact_gbu_agent_directory_html(html):
    start = html.index('async function openGbuAgents(){')
    end = html.index('\nlet cashReminderSending=', start)
    section = r'''
async function copyGbuAgentPhone(value){
 const phone=String(value??'').trim();
 if(!phone)return false;
 let copied=false;
 try{if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(phone);copied=true}}catch(e){}
 if(!copied){
  const focused=document.activeElement,field=document.createElement('textarea');
  field.value=phone;field.setAttribute('readonly','');field.style.cssText='position:fixed;top:0;left:0;opacity:0;font-size:16px';
  document.body.append(field);
  try{field.select();field.setSelectionRange(0,phone.length);copied=!!document.execCommand('copy')}catch(e){}
  finally{field.remove();if(focused?.isConnected)focused.focus({preventScroll:true})}
 }
 toast(copied?'Номер скопирован':'Не удалось скопировать номер. Попробуйте ещё раз');
 return copied;
}
async function openGbuAgents(){
 showModal('⬛ Агенты ГБУ',`<div id="gbuAgentDirectory"><form id="gbuAgentAddForm" class="hidden"><div class="field"><label for="gbuAgentName">ФИО агента</label><input class="search" id="gbuAgentName" name="full_name" placeholder="Фамилия Имя Отчество" autocomplete="name" maxlength="200" required></div><div class="field"><label for="gbuAgentPhone">Телефон агента</label><input class="search" id="gbuAgentPhone" name="phone" type="tel" inputmode="tel" placeholder="+7 999 123-45-67" autocomplete="tel" maxlength="40" required></div><p id="gbuAgentAddError" role="alert" class="hidden"></p><div class="action-row"><button type="button" class="secondary" id="gbuAgentAddCancel">Отмена</button><button type="submit" class="primary" id="gbuAgentSave">Сохранить</button></div></form><p class="muted gbu-agent-help">Нажмите номер, чтобы скопировать.</p><input class="search" id="gbuAgentSearch" placeholder="ФИО или номер" aria-label="Поиск агента по ФИО или номеру"><div id="gbuAgentsList" class="gbu-agent-grid"><div class="empty">Загрузка…</div></div></div>`);
 const session=cashModalGeneration,search=qs('#gbuAgentSearch'),root=qs('#gbuAgentsList');
 const active=()=>session===cashModalGeneration&&qs('#gbuAgentsList')===root&&!qs('#modal').classList.contains('hidden');
 const form=qs('#gbuAgentAddForm'),name=qs('#gbuAgentName'),phone=qs('#gbuAgentPhone'),error=qs('#gbuAgentAddError'),save=qs('#gbuAgentSave');
 let saving=false;
 const canAdd=state.bootstrap?.cabinet_only===false&&['owner','assistant'].includes(state.bootstrap?.cabinet_kind);
 if(canAdd){
  const add=document.createElement('button');
  add.id='gbuAgentAddBtn';add.type='button';add.className='primary small';add.textContent='＋ Добавить';add.setAttribute('aria-expanded','false');add.setAttribute('aria-controls','gbuAgentAddForm');
  qs('#modal .modal-head').insertBefore(add,qs('#closeModal'));
  function toggleForm(open){
   if(saving||!active())return;
   form.classList.toggle('hidden',!open);add.setAttribute('aria-expanded',String(open));
   error.classList.add('hidden');
   if(open){qs('#modal').scrollTop=0;name.focus()}else add.focus({preventScroll:true});
  }
  add.onclick=()=>toggleForm(form.classList.contains('hidden'));
  qs('#gbuAgentAddCancel').onclick=()=>toggleForm(false);
  form.onsubmit=async event=>{
   event.preventDefault();if(saving||!active())return;
   const full_name=name.value.trim(),number=phone.value.trim();
   if(!full_name||!number){error.textContent='Заполните ФИО и телефон агента';error.classList.remove('hidden');return}
   saving=true;save.disabled=true;add.disabled=true;qs('#gbuAgentAddCancel').disabled=true;name.disabled=true;phone.disabled=true;save.textContent='Сохранение…';error.classList.add('hidden');
   try{
    await api('/api/gbu/agents',{method:'POST',body:JSON.stringify({full_name,phone:number})});
    if(!active())return;
    form.reset();form.classList.add('hidden');add.setAttribute('aria-expanded','false');search.value='';
    toast('Агент добавлен');await draw();
   }catch(e){if(active()){error.textContent=e.message||'Не удалось сохранить агента';error.classList.remove('hidden')}}
   finally{saving=false;if(active()){save.disabled=false;add.disabled=false;qs('#gbuAgentAddCancel').disabled=false;name.disabled=false;phone.disabled=false;save.textContent='Сохранить'}}
  };
 }
 let request=0;
 async function draw(){
  const id=++request,q=encodeURIComponent(search.value.trim());
  try{
   const items=await api(`/api/gbu/agents?search=${q}`);
   if(!active()||id!==request)return;
   const list=items.slice().sort((a,b)=>String(a.full_name||'').localeCompare(String(b.full_name||''),'ru'));
   root.innerHTML=list.length?list.map(x=>{
    const phone=String(x.phone||'').trim();
    return `<article class="gbu-agent-card"><b>${esc(x.full_name||'—')}</b>${phone?`<button type="button" class="contact-btn" data-copy-agent-phone="${esc(phone)}" aria-label="Скопировать номер ${esc(x.full_name||'агента')}">📋 ${esc(phone)}</button>`:'<small>Номер не указан</small>'}</article>`;
   }).join(''):'<div class="empty">Не найдено</div>';
   qsa('[data-copy-agent-phone]',root).forEach(button=>button.onclick=event=>{event.stopPropagation();return copyGbuAgentPhone(button.dataset.copyAgentPhone)});
  }catch(e){if(active()&&id===request)root.innerHTML=`<div class="empty">${esc(e.message||'Не удалось загрузить агентов')}</div>`}
 }
 search.oninput=draw;
 await draw();
}
'''
    html = html[:start] + section + html[end:]
    return html.replace('</head>', r'''
<style id="compact-gbu-agent-directory-style">
#modal:has(#gbuAgentDirectory){padding:10px;left:6px;right:6px;width:auto;max-width:1100px;margin:0 auto}
#modal:has(#gbuAgentDirectory) .modal-head{gap:6px;margin-bottom:8px}
#modal:has(#gbuAgentDirectory) .modal-head h3{flex:1;min-width:0;margin:0;font-size:18px;overflow-wrap:anywhere}
#gbuAgentAddBtn{flex:none;white-space:nowrap;min-height:36px;padding:8px 10px}
#modal:has(#gbuAgentDirectory) .close-btn{flex:none}
#gbuAgentAddForm{margin-bottom:10px;padding:10px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
#gbuAgentAddForm input.search{font-size:16px;min-height:42px}
#gbuAgentAddError{color:var(--red);font-size:13px;margin:6px 0 10px}
#gbuAgentAddForm .action-row button{min-height:42px}
#gbuAgentDirectory .gbu-agent-help{margin:0 0 4px;font-size:11px}
#gbuAgentDirectory .search{margin:0 0 5px;min-height:36px;padding:6px 8px;font-size:13px}
#gbuAgentsList.gbu-agent-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}
#gbuAgentsList .gbu-agent-card{display:flex;flex-direction:column;align-items:stretch;justify-content:space-between;min-width:0;gap:4px;padding:5px 4px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
#gbuAgentsList .gbu-agent-card b{font-size:11px;line-height:1.2;overflow-wrap:anywhere}
#gbuAgentsList .gbu-agent-card .contact-btn{display:block;width:100%;min-width:0;min-height:34px;margin:0;padding:4px 1px;font-size:clamp(8px,2.5vw,11px);line-height:1.2;white-space:nowrap;text-align:center;border-radius:6px}
#gbuAgentsList .gbu-agent-card small{font-size:11px;color:var(--muted)}
#gbuAgentsList>.empty{grid-column:1/-1}
</style></head>''', 1)


INLINE_INDEX_HTML = _compact_gbu_agent_directory_html(INLINE_INDEX_HTML)


# Evening preparation and morning reports share the selected Control date.
def _control_morning_evening_layout_html(html):
    start = html.index('function renderSummary() {')
    end = html.index('\nasync function openContactBrigades(', start)
    section = html[start:end]
    section = section.replace("for(const id of ['orderPreparation','brigadeWork'])", "for(const id of ['brigadeWork'])", 1)
    block_start = section.index("  const p=s.order_completion?.sources.private")
    block_end = section.index("  qs('#brigadeWork').innerHTML=", block_start)
    section = section[:block_start] + section[block_end:]
    section = section.replace("const sections=future?['orderPreparation','brigadeWork','employeeReadiness']:['brigadeWork','orderPreparation','employeeReadiness'];", "const sections=['brigadeWork','employeeReadiness'];", 1)
    html = html[:start] + section + html[end:]
    heading = '<div class="bc-heading"><div><h2>Контроль заказов</h2><p>${esc(fmtDate(data.work_date))}</p></div>'
    if html.count(heading) != 1:
        raise RuntimeError('Control morning heading anchor missing')
    html = html.replace(heading, '${controlEveningPreparationHtml(data)}<div class="bc-heading"><div><h2>Контроль заказов (утром)</h2></div>', 1)
    binding = "  qs('#brigadeControlDate',root).onchange="
    if html.count(binding) != 1:
        raise RuntimeError('Control date handler anchor missing')
    html = html.replace(binding, "  qs('#numberCardsBtn',root).onclick=()=>openNumberCards(data.work_date);\n" + binding, 1)
    html = html.replace("async function openNumberCards(){\n const day=state.selectedDate;", "async function openNumberCards(workDay){\n const day=typeof workDay==='string'?workDay:state.selectedDate;", 1)
    progress = 'state.bootstrap.summary.number_cards_count=r.count;state.bootstrap.summary.order_completion=r.progress;renderSummary();'
    if html.count(progress) != 1:
        raise RuntimeError('Number card progress anchor missing')
    html = html.replace(progress, progress + "\n    if(currentViewId()==='ownerBrigadeControlView'&&ownerBrigadeControlData?.work_date===day){ownerBrigadeControlData.order_completion=r.progress;renderOwnerBrigadeControl()}", 1)
    return html.replace('</head>', r'''
<style id="control-morning-evening-style">
#ownerBrigadeControlContent #orderPreparation{margin:6px 0 14px;padding:0 0 10px;border-bottom:1px solid var(--line)}
#ownerBrigadeControlContent #orderPreparation h2{margin:0 0 8px;font-size:clamp(17px,4.4vw,21px);line-height:1.25}
#ownerBrigadeControlContent #numberCardsBtn{width:100%;min-height:44px;margin-bottom:6px;padding:8px 6px;border-radius:11px;font-size:13px}
#ownerBrigadeControlContent #orderPreparation .summary-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:0}
#ownerBrigadeControlContent #orderPreparation .metric{padding:9px 8px;min-height:68px;border-radius:12px}
#ownerBrigadeControlContent #orderPreparation .metric b{font-size:16px;line-height:1.2}
#ownerBrigadeControlContent #orderPreparation .metric span{font-size:11px;line-height:1.3;white-space:normal;overflow:visible;margin-top:3px}
#ownerBrigadeControlContent .bc-heading{display:grid;grid-template-columns:minmax(0,1fr) minmax(104px,128px);align-items:center;gap:6px;margin-bottom:10px}
#ownerBrigadeControlContent .bc-heading>div{min-width:0}
#ownerBrigadeControlContent .bc-heading h2{font-size:clamp(12px,3.45vw,17px);line-height:1.25;white-space:nowrap}
#ownerBrigadeControlContent .bc-date input{width:100%;max-width:128px;min-width:0}
</style></head>''', 1).replace('</body>', r'''
<script>
function controlEveningPreparationHtml(data){
 const progress=data.order_completion;
 const p=progress?.sources?.private||{cards:data.totals.private.cards,expected:data.totals.private.orders,closed:0};
 const g=progress?.sources?.gbu||{cards:data.totals.gbu.cards,expected:data.totals.gbu.orders,closed:0};
 const cards=progress?.cards??(p.cards+g.cards),expected=progress?.expected??(p.expected+g.expected);
 return `<section id="orderPreparation" aria-label="Закрытие заказов вечером"><h2>Закрытие заказов (вечером)</h2><button type="button" class="secondary" id="numberCardsBtn">📱 Номера в группе ${cards}/${expected} ›</button><div class="summary-grid"><div class="metric"><b>Частные</b><span>Номера ${p.cards}/${p.expected}</span><span>Закрыто ${p.closed}/${p.expected}</span></div><div class="metric"><b>ГБУ</b><span>Номера ${g.cards}/${g.expected}</span><span>Закрыто ${g.closed}/${g.expected}</span></div></div></section>`;
}
</script></body>''', 1)


INLINE_INDEX_HTML = _control_morning_evening_layout_html(INLINE_INDEX_HTML)


# Compact the Control chrome while preserving every existing action and counter.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', r'''
<style id="compact-control-menu-style">
body.owner-brigade-control-open .topbar{padding:9px 12px 7px}
body.owner-brigade-control-open .topbar .eyebrow{font-size:10px}
body.owner-brigade-control-open .topbar h1{font-size:20px;margin:2px 0 0}
body.owner-brigade-control-open .topbar .icon-btn{width:36px;height:36px;border-radius:11px;font-size:20px}
body.owner-brigade-control-open #dateStrip{gap:5px;padding:2px 12px 7px}
body.owner-brigade-control-open #dateStrip .date-pill{min-width:66px;min-height:62px;padding:7px 5px;border-radius:11px}
body.owner-brigade-control-open #dateStrip .date-pill strong{font-size:12px;line-height:1.2}
body.owner-brigade-control-open #dateStrip .date-pill small{font-size:10px;line-height:1.2!important;margin-top:3px!important}
body.owner-brigade-control-open #ownerBrigadeControlContent{padding:2px 0 12px}
#ownerBrigadeControlContent #orderPreparation{margin:0 0 7px;padding:8px;border:1px solid var(--line);border-radius:13px;background:var(--panel)}
#ownerBrigadeControlContent #orderPreparation h2{margin:0 0 5px;font-size:clamp(15px,4.1vw,18px);line-height:1.2}
#ownerBrigadeControlContent #numberCardsBtn{min-height:36px;margin-bottom:4px;padding:6px 5px;font-size:12px;border-radius:9px}
#ownerBrigadeControlContent #orderPreparation .summary-grid{gap:4px}
#ownerBrigadeControlContent #orderPreparation .metric{min-height:54px;padding:6px 7px;border-radius:9px;background:var(--panel2)}
#ownerBrigadeControlContent #orderPreparation .metric b{font-size:14px;line-height:1.2}
#ownerBrigadeControlContent #orderPreparation .metric span{font-size:10px;line-height:1.2;margin-top:2px}
#ownerBrigadeControlContent .bc-heading{padding:7px 7px 6px;margin:0;border:1px solid var(--line);border-bottom:0;border-radius:13px 13px 0 0;background:var(--panel);grid-template-columns:minmax(0,1fr) minmax(100px,116px);gap:5px}
#ownerBrigadeControlContent .bc-heading h2{font-size:clamp(12px,3.35vw,16px);line-height:1.2}
#ownerBrigadeControlContent .bc-date{font-size:10px;line-height:1.2}
#ownerBrigadeControlContent .bc-date input{min-height:30px;padding:3px 5px;font-size:11px;border-radius:8px}
#ownerBrigadeControlContent .bc-counters{gap:4px;padding:0 7px 5px;border-left:1px solid var(--line);border-right:1px solid var(--line);background:var(--panel)}
#ownerBrigadeControlContent .bc-counters button{min-height:54px;padding:6px 3px;border-radius:9px;background:var(--panel2)}
#ownerBrigadeControlContent .bc-counters button[aria-pressed=true]{background:#211d15;border-color:var(--gold)}
#ownerBrigadeControlContent .bc-counters b{font-size:20px;line-height:1.1}
#ownerBrigadeControlContent .bc-counters span{font-size:10px;line-height:1.2;margin-top:3px}
#ownerBrigadeControlContent .bc-sources{gap:3px;margin-top:0;padding:3px 7px 6px;border-top:0;border-radius:0 0 13px 13px;background:var(--panel)}
#ownerBrigadeControlContent .bc-sources button{min-height:34px;padding:5px 2px;font-size:11px;border-radius:8px}
#ownerBrigadeControlContent .bc-list-caption{font-size:10px;margin:8px 0 5px}
#ownerBrigadeControlContent .bc-missing{font-size:11px;margin:5px 0}
</style></head>''', 1)


# A full month of Vlad's allowance fits in one view, independently of other ledgers.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', r'''
<style id="vlad-allowance-full-month-style">
#modal:has(#vladAllowanceBody){display:flex;flex-direction:column;gap:5px;left:6px;right:6px;top:6px;bottom:calc(76px + var(--safe-bottom));width:auto;max-width:1100px;max-height:none;margin:0 auto;padding:8px;transform:none;overflow:hidden}
#modal:has(#vladAllowanceBody)>.modal-head{position:relative;top:auto;display:grid;grid-template-columns:34px minmax(0,1fr) 34px;align-items:center;gap:5px;min-height:34px;margin:0;padding:0}
#modal:has(#vladAllowanceBody)>.modal-head::before{display:none}
#modal:has(#vladAllowanceBody)>.modal-head h3{text-align:center;font-size:18px;margin:0}
#modal:has(#vladAllowanceBody)>.modal-head button{width:34px;min-width:34px;height:34px;min-height:34px;padding:4px;border-radius:9px;font-size:20px}
#modal:has(#vladAllowanceBody) .vlad-month-picker{display:block;margin:0}
#vladAllowanceMonth{display:block;width:100%;box-sizing:border-box;min-height:34px;margin:0;padding:5px 8px;text-align:center;font-size:14px;border-radius:9px}
#modal:has(#vladAllowanceBody) .vlad-month-note{margin:0;text-align:center;font-size:11px;line-height:1.3}
#vladAllowanceStatus{display:block;min-height:12px;line-height:12px;text-align:center;font-size:9px;margin:0}
#vladAllowanceBody{flex:1;min-height:0;overflow:hidden;--vlad-row:16px;--vlad-font:12px}
#vladAllowanceBody .vlad-month-table{width:100%;max-width:300px;margin:0 auto;table-layout:fixed;border-collapse:collapse;transform-origin:top center;font-size:var(--vlad-font);line-height:1.15;font-variant-numeric:tabular-nums}
#vladAllowanceBody th,#vladAllowanceBody td{padding:0 3px;border-bottom:1px solid var(--line);white-space:nowrap}
#vladAllowanceBody th:first-child,#vladAllowanceBody td:first-child{text-align:left}
#vladAllowanceBody th:nth-child(2),#vladAllowanceBody td:nth-child(2){text-align:center}
#vladAllowanceBody th:last-child,#vladAllowanceBody td:last-child{text-align:right}
#vladAllowanceBody thead{background:var(--panel2)}
#vladAllowanceBody thead tr{height:24px;font-size:11px}
#vladAllowanceBody tbody tr{height:var(--vlad-row)}
#vladAllowanceBody tbody td:last-child{font-weight:700;color:var(--gold)}
#vladAllowanceBody .is-selected-day{background:rgba(215,178,96,.10)}
#vladAllowanceBody tfoot tr{height:18px;color:var(--gold);font-weight:700;background:#211d15}
#vladAllowanceBody tfoot tr:last-child{font-size:13px}
</style></head>''', 1).replace('</body>', r'''
<script>
function fitVladAllowanceMonth(){
 const body=qs('#vladAllowanceBody'),table=body?.querySelector('table');
 if(!table||qs('#modal').classList.contains('hidden'))return;
 const available=body.clientHeight||Math.max(1,(window.visualViewport?.height||window.innerHeight||700)-180);
 table.style.transform='none';table.style.height=available+'px';
 const header=table.tHead.offsetHeight||24,footer=Array.from(table.tFoot.rows).reduce((sum,row)=>sum+(row.offsetHeight||18),0);
 const count=table.tBodies[0].rows.length;
 const row=Math.max(1,(available-header-footer)/Math.max(1,count));
 body.style.setProperty('--vlad-row',row+'px');body.style.setProperty('--vlad-font',Math.min(13,Math.max(10,row-3))+'px');
 const natural=table.offsetHeight||available;
 const scale=Math.min(1,available/natural);
 if(scale<1)table.style.transform='scaleY('+scale+')';
 body.dataset.monthFitScale=String(scale);
}
window.addEventListener('resize',fitVladAllowanceMonth);
window.visualViewport?.addEventListener('resize',fitVladAllowanceMonth);
</script></body>''', 1)




def _cabinet_cash_summary_html(html):
    anchor = "qsa('[data-finance-tab]',panel).forEach(b=>"
    if html.count(anchor) != 1:
        raise ValueError('Cash summary toolbar anchor missing')
    html = html.replace(anchor, "bindCabinetCashSummary(panel,data,active,tab);\n      "+anchor, 1)
    return html.replace('</head>', r'''<style id="cabinet-cash-summary-style">
#cabinetFinance .cabinet-finance-summary-heading{display:flex;align-items:center;gap:6px;min-width:0;position:relative}
#cabinetFinance .cabinet-finance-summary-heading h2{flex-shrink:0}
#cabinetFinance .cabinet-summary-trigger{border:0;border-radius:9px;background:var(--gold);color:#171208;min-height:36px;padding:5px 8px;font-size:11px;font-weight:750;cursor:pointer;line-height:1.2}
#cabinetFinance .cabinet-summary-trigger[hidden],#cabinetFinance .cabinet-summary-picker[hidden]{display:none}
#cabinetFinance .cabinet-summary-picker{position:absolute;top:100%;left:0;width:min(300px,calc(100vw - 38px));padding:12px;background:var(--panel);border:1px solid var(--gold2);border-radius:12px;box-shadow:0 10px 35px #000b;z-index:45}
#cabinetFinance .cabinet-summary-picker strong{display:block;font-size:14px}
#cabinetFinance .cabinet-summary-picker p{font-size:12px;color:var(--muted);margin:6px 0}
#cabinetFinance .cabinet-summary-picker button{display:block;width:100%;min-height:44px;padding:8px;margin-top:7px;border:1px solid var(--gold2);border-radius:9px;background:var(--panel2);color:var(--gold);font-weight:650;font-size:14px;cursor:pointer}
#cabinetFinance .cabinet-summary-picker button:disabled{opacity:.55}
#cabinetFinance .cabinet-summary-picker [role=status]{white-space:normal;line-height:1.4}
#cabinetFinance:has(.cabinet-finance-summary-heading) .cabinet-finance-month input{width:115px}
</style></head>''',1).replace('</body>', r'''<script>
function bindCabinetCashSummary(panel,data,active,tab){
 let heading=qs('.cabinet-finance-summary-heading',panel);
 if(!heading){
  const title=qs('h2',panel);heading=document.createElement('div');heading.className='cabinet-finance-summary-heading';title.before(heading);heading.append(title);
  heading.insertAdjacentHTML('beforeend','<button type="button" class="cabinet-summary-trigger" aria-expanded="false" aria-controls="cabinetCashSummaryPicker">📊 Подсчёт кассы</button><div id="cabinetCashSummaryPicker" class="cabinet-summary-picker" hidden><strong>Отправить итог в чат кассы</strong><p class="cabinet-summary-month"></p><button type="button" data-summary-half="1">1–15 число</button><button type="button" data-summary-half="2">16–конец месяца</button><p role="status" aria-live="polite">Выберите период для отправки</p><button type="button" data-summary-close>Закрыть</button></div>');
 }
 const trigger=qs('.cabinet-summary-trigger',heading),picker=qs('.cabinet-summary-picker',heading),status=qs('[role=status]',picker);
 trigger.hidden=tab!=='cash';if(trigger.hidden){picker.hidden=true;trigger.setAttribute('aria-expanded','false')}
 qs('.cabinet-summary-month',picker).textContent=new Date(data.start+'T12:00:00').toLocaleDateString('ru-RU',{month:'long',year:'numeric'});
 const toggle=open=>{picker.hidden=!open;trigger.setAttribute('aria-expanded',String(open))};
 trigger.onclick=()=>toggle(picker.hidden);qs('[data-summary-close]',picker).onclick=()=>{toggle(false);trigger.focus()};
 picker.onkeydown=e=>{if(e.key==='Escape'){toggle(false);trigger.focus()}};
 let busy=false;
 qsa('[data-summary-half]',picker).forEach(button=>button.onclick=async()=>{
  if(busy||!active())return;busy=true;
  qsa('[data-summary-half]',picker).forEach(b=>b.disabled=true);
  const half=Number(button.dataset.summaryHalf),month=data.start.slice(0,7),key='cash-summary:'+data.user_id+':'+month+':'+half;
  let id=sessionStorage.getItem(key);
  if(!id){id=crypto.randomUUID();sessionStorage.setItem(key,id)}
  status.textContent='Отправляем итог '+(half===1?'1–15':'16–'+data.end.slice(8))+'…';
  try{
   let result=await api('/api/owner/brigadier-cabinet/actions',{method:'POST',body:JSON.stringify({request_id:id,action:'cash_summary',values:{month,half}})});
   if(!active())return;
   id=result.id;sessionStorage.setItem(key,id);
   for(let i=0;i<30;i++){
    if(!active())return;
    if(result.status==='done'){sessionStorage.removeItem(key);status.textContent=result.message||'Итог отправлен в чат кассы';toast(status.textContent);return}
    if(result.status==='error'){sessionStorage.removeItem(key);throw Error(result.error||'Итог не отправлен')}
    await new Promise(resolve=>setTimeout(resolve,1500));if(!active())return;
    result=await api('/api/owner/brigadier-cabinet/actions/'+encodeURIComponent(id),{cache:'no-store'});
   }
   status.textContent='Запрос сохранён. Нажмите тот же период, чтобы проверить результат.';
  }catch(error){if(active())status.textContent=error.message+' Нажмите период, чтобы повторить проверку.'}
  finally{busy=false;if(active())qsa('[data-summary-half]',picker).forEach(b=>b.disabled=false)}
 });
}
</script></body>''',1)


INLINE_INDEX_HTML = _cabinet_cash_summary_html(INLINE_INDEX_HTML)


def _cabinet_action_notices_html(html):
    start = html.index('async function api(path, opts={}) {')
    end = html.index('function toast(', start)
    original = html[start:end]
    opening = 'async function api(path, opts={}) {'
    wrapped = original.replace(opening, opening+'\n  try{', 1)
    last = wrapped.rfind('}')
    wrapped = wrapped[:last]+'  }catch(error){throw cabinetHumanApiError(error)}\n'+wrapped[last:]
    html = html[:start]+wrapped+html[end:]
    return html.replace('</head>', r'''<style id="cabinet-action-notices-style">
#cabinetActionStatus:not(:empty),.cabinet-action-notice{padding:11px 12px;border-radius:11px;border:1px solid var(--gold2);background:#211d15;color:#ead39d;font-size:13px;line-height:1.5;white-space:pre-line;overflow-wrap:anywhere}
#cabinetActionStatus[data-notice=error],.cabinet-action-notice[data-notice=error]{border-color:#764447;background:#291b1d;color:#ffb7b7}
.cabinet-action-notice[data-notice=success]{border-color:#326c4b;background:#14281d;color:#83dfaa}
.cabinet-action-notice{grid-column:1/-1;margin:8px 0}
</style></head>''',1).replace('</body>', r'''<script>
function cabinetHumanApiError(raw){
 const error=raw instanceof Error?raw:new Error(String(raw||'Не удалось выполнить действие'));
 if(error.humanNotice)return error;
 const network=!error.status&&(/load failed|failed to fetch|networkerror|network request failed|network connection|fetch failed|aborted/i.test(error.message)||error.name==='AbortError');
 if(network){error.message='🌐 Соединение с сервером прервалось. Проверьте интернет или переключитесь между Wi-Fi и мобильной сетью.';error.isConnectionError=true}
 else if(error.status>=500||error.status===408||error.status===429){error.message=error.status===429?'⏳ Слишком много запросов. Подождите немного и проверьте результат.':'⚠️ Сервер временно не отвечает. Попробуйте проверить результат чуть позже.';error.temporary=true}
 else if(error.name==='SyntaxError'){error.message='⚠️ Получен неполный ответ сервера. Проверьте результат ещё раз.';error.temporary=true}
 error.humanNotice=true;return error;
}
function cabinetRememberPending(uid,id,action,cardId){
 try{sessionStorage.setItem('cabinet-pending:'+uid,id);sessionStorage.setItem('cabinet-pending-meta:'+uid,JSON.stringify({id,action,cardId}))}catch(e){}
}
function cabinetForgetPending(uid,id){
 try{if(sessionStorage.getItem('cabinet-pending:'+uid)===id){sessionStorage.removeItem('cabinet-pending:'+uid);sessionStorage.removeItem('cabinet-pending-meta:'+uid)}}catch(e){}
}
function cabinetActionNotice(message,kind='success'){
 const root=qs('#ownerCabinetContent');
 if(root&&currentViewId()==='ownerCabinetView'){
  let banner=qs('#cabinetActionNotice',root);
  if(!banner){banner=document.createElement('p');banner.id='cabinetActionNotice';banner.className='cabinet-action-notice';banner.setAttribute('role','status');banner.setAttribute('aria-live','polite');root.prepend(banner)}
  banner.dataset.notice=kind;banner.textContent=message;
 }
 toast(message,8000);
}
</script></body>''',1)


INLINE_INDEX_HTML = _cabinet_action_notices_html(INLINE_INDEX_HTML)


class CashPeriodReconciliation(Base):
    __tablename__ = 'cash_period_reconciliation'
    period_start = Column(Date, primary_key=True)
    tg_id = Column(BigInteger, primary_key=True)
    checked = Column(Boolean, nullable=False)
    source = Column(String(20), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CashReconciliationMark(BaseModel):
    checked: StrictBool
    version: StrictInt = Field(ge=0)


class CashPeriodReceipt(Base):
    __tablename__ = 'cash_period_receipt'
    period_start = Column(Date, primary_key=True)
    tg_id = Column(BigInteger, primary_key=True)
    received = Column(Boolean, nullable=False, default=False)
    comment = Column(String(240), nullable=False, default='')
    version = Column(Integer, nullable=False, default=1)
    updated_by = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CashReceiptChange(BaseModel):
    field: Literal['received', 'comment']
    received: StrictBool = False
    comment: StrictStr = Field(default='', max_length=240)
    version: StrictInt = Field(ge=0)


class CashNoteDelivery(Base):
    __tablename__ = 'cash_note_delivery'
    period_start = Column(Date, primary_key=True)
    tg_id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, primary_key=True)
    note_hash = Column(String(64), primary_key=True)
    status = Column(String(20), nullable=False, default='pending')
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CashNotesSendInput(BaseModel):
    snapshot: StrictStr = Field(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$')


def _cash_notes_snapshot(month, half, items):
    notes = sorted((int(item['tg_id']), str(item.get('comment') or '').strip())
                   for item in items if str(item.get('comment') or '').strip())
    return hashlib.sha256(json.dumps([month, half, notes], ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def cash_reconciliation_period(month, half):
    import calendar
    try:
        period = CabinetCashSummaryPeriod(month=month, half=half)
    except ValueError:
        raise HTTPException(422, 'Выберите месяц и половину месяца')
    first = date.fromisoformat(period.month+'-01')
    return first.replace(day=1 if half == 1 else 16), first.replace(
        day=15 if half == 1 else calendar.monthrange(first.year, first.month)[1])


def cash_reconciliation_cashiers(db):
    return [employee for employee in db.scalars(select(Employee).where(
        Employee.active.is_(True), Employee.group_code == 'brigadier',
        Employee.is_cashier.is_(True), _visible_employee()))
        if employee.tg_id and not _cashier_excluded(db, employee)]


def record_cash_period_reconciliation(db, command, intent):
    values = intent.get('values') or {}
    start, _ = cash_reconciliation_period(values.get('month'), values.get('half'))
    # Use the same account lock as manual edits, including first-row creation.
    db.scalar(select(Employee).where(Employee.tg_id == command.tg_id).with_for_update().limit(1))
    mark = db.get(CashPeriodReconciliation, (start, command.tg_id))
    if mark is None:
        mark = CashPeriodReconciliation(period_start=start,tg_id=command.tg_id,version=0)
        db.add(mark)
    mark.checked = True
    mark.source = 'auto'
    mark.version += 1
    mark.updated_at = datetime.utcnow()


@app.get('/api/cash/reconciliation')
def cash_reconciliation(month: str, half: int, db: Session = Depends(get_db),
        user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    start, end = cash_reconciliation_period(month, half)
    employees = cash_reconciliation_cashiers(db)
    people = {int(e.tg_id): e for e in employees}
    sums = defaultdict(lambda: {'cash':0, 'kickback':0, 'orders':0})
    for entry in _all_cash_rows(db, start, end):
        uid = int(entry.brigadier_tg_id)
        if uid in people:
            sums[uid]['cash'] += int(entry.commission_rub or 0)
            sums[uid]['kickback'] += int(entry.kickback_rub or 0)
            sums[uid]['orders'] += 1
    # Earlier successful summaries predate these marks; their retained intents
    # are trustworthy receipts, not guessed from text in Telegram messages.
    automatic = set()
    if people:
        commands = db.scalars(select(CabinetCommand).where(CabinetCommand.tg_id.in_(people),
            CabinetCommand.status == 'done', CabinetCommand.payload.contains('cash_summary'),
            CabinetCommand.payload.contains(month)))
        for command in commands:
            intent = json.loads(command.payload)
            if intent.get('action') == 'cash_summary' and intent.get('values') == {'month':month,'half':half}:
                automatic.add(command.tg_id)
    marks = {m.tg_id:m for m in db.scalars(select(CashPeriodReconciliation).where(
        CashPeriodReconciliation.period_start == start))}
    items = []
    receipts = {r.tg_id:r for r in db.scalars(select(CashPeriodReceipt).where(
        CashPeriodReceipt.period_start == start))}
    for uid, employee in people.items():
        parts = str(employee.full_name or '').split()
        name = ' '.join(str(x) for x in (parts[0] if parts else 'Бригадир', employee.metro) if x)
        values = sums[uid]
        mark = marks.get(uid)
        receipt = receipts.get(uid)
        items.append({'tg_id':uid,'name':name,**values,'total':values['cash']+values['kickback'],
            'received':bool(receipt.received) if receipt else False,
            'comment':receipt.comment if receipt else '',
            'receipt_version':receipt.version if receipt else 0,
            'checked':bool(mark.checked if mark else uid in automatic),
            'source':mark.source if mark else 'auto' if uid in automatic else None,
            'version':mark.version if mark else 0})
    items.sort(key=lambda item:(item['name'].casefold(),item['tg_id']))
    return JSONResponse({'month':month,'half':half,'start':start.isoformat(),'end':end.isoformat(),
        'can_mark':_is_cabinet_owner(db,user.id),'items':items,
        'notes_snapshot':_cash_notes_snapshot(month,half,items),
        'checked_count':sum(item['checked'] for item in items),
        'total':sum(item['total'] for item in items)},headers={'Cache-Control':'private, no-store'})


@app.put('/api/cash/reconciliation/{tg_id}')
def cash_reconciliation_mark(tg_id: int, month: str, half: int, change: CashReconciliationMark,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _is_cabinet_owner(db,user.id):
        raise HTTPException(403, 'Ручная отметка доступна только владельцу')
    start, _ = cash_reconciliation_period(month, half)
    db.scalar(select(Employee).where(Employee.tg_id == tg_id).with_for_update().limit(1))
    if tg_id not in {int(e.tg_id) for e in cash_reconciliation_cashiers(db)}:
        raise HTTPException(404, 'Кассовый бригадир не найден')
    mark = db.get(CashPeriodReconciliation,(start,tg_id))
    if (mark.version if mark else 0) != change.version:
        raise HTTPException(409, 'Отметка уже изменилась. Обновите карточки')
    if mark is None:
        mark = CashPeriodReconciliation(period_start=start,tg_id=tg_id,version=0)
        db.add(mark)
    mark.checked = change.checked
    mark.source = 'manual'
    mark.version += 1
    mark.updated_at = datetime.utcnow()
    db.commit()
    return {'checked':mark.checked,'source':mark.source,'version':mark.version}


@app.put('/api/cash/receipt/{tg_id}')
def cash_receipt_update(tg_id: int, month: str, half: int, change: CashReceiptChange,
        db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    if not _is_cabinet_owner(db, user.id):
        raise HTTPException(403, 'Отметка сдачи и комментарий доступны только владельцу')
    start, _ = cash_reconciliation_period(month, half)
    # Serialize first creation as well as edits across workers on PostgreSQL.
    db.scalar(select(Employee).where(Employee.tg_id == tg_id).with_for_update().limit(1))
    if tg_id not in {int(e.tg_id) for e in cash_reconciliation_cashiers(db)}:
        raise HTTPException(404, 'Кассовый бригадир не найден')
    receipt = db.get(CashPeriodReceipt, (start, tg_id))
    if (receipt.version if receipt else 0) != change.version:
        raise HTTPException(409, 'Данные уже изменились. Закройте редактор и обновите карточки')
    if receipt is None:
        receipt = CashPeriodReceipt(period_start=start, tg_id=tg_id, received=False,
            comment='', version=0)
        db.add(receipt)
    if change.field == 'received':
        receipt.received = change.received
    else:
        receipt.comment = change.comment.strip()
    receipt.version += 1
    receipt.updated_by = user.id
    receipt.updated_at = datetime.utcnow()
    db.commit()
    return {'received':receipt.received, 'comment':receipt.comment, 'receipt_version':receipt.version}


def _cash_note_destinations(payload):
    """Use only exact account-to-group bindings, rejecting ambiguous routes."""
    routes = defaultdict(set)
    owners = defaultdict(set)
    for row in payload.get('cash_chats', []):
        try:
            uid, chat_id = int(row['brigadier_tg_id']), int(row['chat_id'])
        except (KeyError, TypeError, ValueError):
            continue
        if uid > 0 and chat_id < 0:
            routes[uid].add(chat_id)
            owners[chat_id].add(uid)
    return {uid: next(iter(chats)) for uid, chats in routes.items()
            if len(chats) == 1 and len(owners[next(iter(chats))]) == 1}


@app.post('/api/cash/notes/send')
async def send_cash_notes(month: str, half: int, change: CashNotesSendInput,
                          db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_cash_manager(db, user)
    start, end = cash_reconciliation_period(month, half)
    # The existing bot already syncs these bindings every ten seconds.
    # This shared row also serializes claims across service replicas.
    snapshot = db.scalar(select(OwnerBotSnapshot).where(OwnerBotSnapshot.id == 1).with_for_update())
    if not snapshot or datetime.utcnow() - snapshot.updated_at > timedelta(minutes=5):
        raise HTTPException(409, 'Привязки групп кассы не обновлены. Проверьте работу бота и повторите через минуту.')
    try:
        payload = json.loads(snapshot.payload)
        if not isinstance(payload, dict) or payload.get('owner_id') != user.id:
            raise ValueError()
        routes = _cash_note_destinations(payload)
        blocked = {int(row['tg_id']) for row in payload.get('blocked', [])}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(409, 'Не удалось проверить привязки групп кассы. Обновите настройки бота.')
    receipts = {row.tg_id: row for row in db.scalars(select(CashPeriodReceipt).where(
        CashPeriodReceipt.period_start == start))}
    items = []
    for employee in cash_reconciliation_cashiers(db):
        uid = int(employee.tg_id)
        receipt = receipts.get(uid)
        parts = str(employee.full_name or '').split()
        name = ' '.join(x for x in (parts[0] if parts else 'Бригадир', employee.metro) if x)
        items.append({'tg_id': uid, 'name': name,
                      'comment': str(receipt.comment or '').strip() if receipt else ''})
    if not secrets.compare_digest(change.snapshot, _cash_notes_snapshot(month, half, items)):
        raise HTTPException(409, 'Заметки или список кассиров изменились. Обновите карточки перед отправкой.')
    results, claimed = [], []
    empty_count = sum(not item['comment'] for item in items)
    for item in items:
        if not item['comment']:
            continue
        uid, comment = item['tg_id'], item['comment']
        if uid in blocked or db.get(OwnerManagedBlock, uid):
            results.append({'name': item['name'], 'status': 'skipped', 'reason': 'Аккаунт заблокирован'})
            continue
        chat_id = routes.get(uid)
        if chat_id is None:
            results.append({'name': item['name'], 'status': 'skipped', 'reason': 'Не найдена однозначная привязка группы кассы'})
            continue
        note_hash = hashlib.sha256(comment.encode()).hexdigest()
        key = (start, uid, chat_id, note_hash)
        delivery = db.get(CashNoteDelivery, key)
        if delivery and delivery.status == 'sent':
            results.append({'name': item['name'], 'status': 'already_sent'})
            continue
        if delivery and delivery.status != 'failed':
            results.append({'name': item['name'], 'status': 'unknown', 'reason': 'Отправка не подтверждена; проверьте группу'})
            continue
        if delivery is None:
            delivery = CashNoteDelivery(period_start=start, tg_id=uid, chat_id=chat_id, note_hash=note_hash)
            db.add(delivery)
        delivery.status, delivery.updated_at = 'pending', datetime.utcnow()
        claimed.append((key, item))
    # Commit the claim before sending; an interrupted response never triggers a duplicate.
    db.commit()
    for key, item in claimed:
        outcome, reason = 'unknown', 'Отправка не подтверждена; проверьте группу'
        message = (f'📝 <b>Комментарий к кассе</b>\n'
                   f'<b>{start:%d.%m.%Y} — {end:%d.%m.%Y}</b>\n'
                   f'{escape(item["name"])}\n\n{escape(item["comment"])}')
        try:
            delivered = await send_telegram_message(key[2], message)
            outcome = 'sent' if delivered else 'failed'
            reason = '' if delivered else 'Telegram отклонил отправку'
        except Exception as exc:
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if isinstance(code, int) and 400 <= code < 500 and code != 408:
                outcome, reason = 'failed', 'Telegram отклонил отправку; проверьте доступ бота к группе'
        delivery = db.get(CashNoteDelivery, key)
        delivery.status, delivery.updated_at = outcome, datetime.utcnow()
        db.commit()
        results.append({'name': item['name'], 'status': outcome, 'reason': reason})
    return {'sent_count': sum(x['status'] == 'sent' for x in results),
            'already_sent_count': sum(x['status'] == 'already_sent' for x in results),
            'failed_count': sum(x['status'] == 'failed' for x in results),
            'unconfirmed_count': sum(x['status'] == 'unknown' for x in results),
            'skipped_count': sum(x['status'] == 'skipped' for x in results),
            'empty_count': empty_count, 'items': results}


def _cash_reconciliation_cards_html(html):
    html = html.replace("if(!body||!table||!modal||modal.classList.contains('hidden'))return;",
        "if(!body||(!table&&!qs('.cash-reconciliation-grid',body))||!modal||modal.classList.contains('hidden'))return;",1)
    html = html.replace('tableScroll:table.scrollLeft,tableTop:table.scrollTop',
        'tableScroll:table?.scrollLeft||0,tableTop:table?.scrollTop||0',1)
    controls = '<button type="button" class="primary" data-cash-range="month">Месяц</button></div>'
    if html.count(controls) != 1:
        raise RuntimeError('Cash reconciliation controls missing')
    html = html.replace(controls, '<button type="button" class="primary" data-cash-range="month">Месяц</button><button type="button" class="secondary" data-cash-range="first">1–15</button><button type="button" class="secondary" data-cash-range="second">16–конец месяца</button></div>',1)
    start = html.index('async function openCashTable(){')
    end = html.index('let ownerCabinetRequest=',start)
    section = html[start:end]
    section = section.replace("let cashRange=compactCash?'month':resume?.range||'month', cashRequest=0;",
        "let cashRange=resume?.range||'month', cashRequest=0, cashMarkSaving=false,cashNotesSending=false,cashNotesStatus=null;",1)
    section = section.replace('if(!body || !cashActive()) return;',
        'if(!body || !cashActive() || cashMarkSaving) return;',1)
    section = section.replace("qsa('[data-cash-range]')", "qsa('button[data-cash-range]')")
    reminder_start = section.index("      const reminder=qs('#cashReminder');")
    reminder_end = section.index("      const refresh=qs('#cashRefreshNow');",reminder_start)
    reminder = section[reminder_start:reminder_end]
    anchor = '      const ledger=await api(`/api/cash/ledger?'
    at = section.index(anchor)
    branch = r'''
      if(qs('[data-cash-note-editor]')&&(body.dataset.noteMonth!==cashMonth||body.dataset.noteRange!==cashRange)&&typeof cashNoteDismiss==='function')cashNoteDismiss();
      qs('#modal').classList.toggle('cash-reconciliation-open',compactCash&&cashRange!=='month');
      if(cashRange==='month'){
        qs('.cash-reconciliation-toolbar',qs('#modal .modal-head'))?.remove();
        qs('.cash-notes-send-row',qs('#modal'))?.remove();
      }
      if(compactCash&&cashRange!=='month'){
        const half=cashRange==='first'?1:2;
        if(qs('[data-cash-note-editor]')&&body.dataset.noteMonth===cashMonth&&body.dataset.noteRange===cashRange)return;
        const report=await api(`/api/cash/reconciliation?month=${encodeURIComponent(cashMonth)}&half=${half}`,{cache:'no-store'});
        if(!cashActive()||request!==cashRequest||requestedMonth!==cashMonth||requestedRange!==cashRange)return;
        const notesKey=report.month+':'+report.half;
        body.innerHTML=ownerCashReconciliationHtml({...report,notes_sending:cashNotesSending,notes_status:cashNotesStatus?.key===notesKey?cashNotesStatus.text:''});
        // Put the existing controls in the title row; keep their original IDs
        // so the reminder handler and refresh behavior remain unchanged.
        const cashHead=qs('#modal .modal-head');
        qs('.cash-reconciliation-toolbar',cashHead)?.remove();
        cashHead.insertBefore(qs('.cash-reconciliation-toolbar',body),qs('#closeModal',cashHead));
        const monthTools=qs('#cashMonthTools');
        if(monthTools){
          qs('.cash-notes-send-row',monthTools)?.remove();
          monthTools.append(qs('.cash-notes-send-row',body));
        }
        const notesButton=qs('#cashNotesSend',qs('#modal'));
        if(notesButton)notesButton.onclick=async()=>{
          if(!cashActive()||cashMarkSaving||cashNotesSending||!report.can_mark)return;
          cashNotesSending=true;cashMarkSaving=true;notesButton.disabled=true;notesButton.textContent='Отправка…';
          const message=qs('#cashNotesSendStatus',body);
          if(message){message.textContent='Отправляем сохранённые заметки…';message.hidden=false}
          try{
            const result=await api(`/api/cash/notes/send?month=${encodeURIComponent(report.month)}&half=${report.half}`,{method:'POST',body:JSON.stringify({snapshot:report.notes_snapshot})});
            const problems=(result.items||[]).filter(item=>['failed','unknown','skipped'].includes(item.status)).map(item=>`${item.name}: ${item.reason||'Не отправлено'}`);
            const text=`Отправлено: ${result.sent_count}. Уже отправлены: ${result.already_sent_count}. Без заметки: ${result.empty_count}.`+(problems.length?'\n'+problems.join('\n'):'');
            cashNotesStatus={key:notesKey,text};if(cashActive())toast(text,8000);
          }catch(error){cashNotesStatus={key:notesKey,text:error.message||'Не удалось отправить заметки'};if(cashActive())toast(cashNotesStatus.text,6000)}
          finally{cashNotesSending=false;cashMarkSaving=false;if(cashActive())await drawCash()}
        };
        qsa('[data-cash-received]',body).forEach(button=>button.onclick=async()=>{
          if(cashMarkSaving||!cashActive()||!report.can_mark)return;
          const item=report.items.find(x=>String(x.tg_id)===button.dataset.cashReceived);if(!item)return;
          cashMarkSaving=true;button.disabled=true;
          try{
            await api(`/api/cash/receipt/${item.tg_id}?month=${encodeURIComponent(report.month)}&half=${report.half}`,{method:'PUT',body:JSON.stringify({field:'received',received:!item.received,version:item.receipt_version||0})});
            toast(item.received?'Отметка сдачи снята':'Касса отмечена как сданная ✅');
          }catch(error){toast(error.message)}
          finally{cashMarkSaving=false;if(cashActive())await drawCash()}
        });
        qsa('[data-cash-note]',body).forEach(button=>button.onclick=()=>{
          if(cashMarkSaving||!cashActive()||!report.can_mark)return;
          const item=report.items.find(x=>String(x.tg_id)===button.dataset.cashNote);if(!item)return;
          if(qs('[data-cash-note-editor]'))return;
          cashRequest++;body.dataset.noteMonth=report.month;body.dataset.noteRange=cashRange;
          const overlay=document.createElement('div');overlay.className='cash-note-overlay';
          const editor=document.createElement('form');editor.className='cash-note-dialog';editor.setAttribute('data-cash-note-editor','');editor.setAttribute('role','dialog');editor.setAttribute('aria-modal','true');editor.setAttribute('aria-labelledby','cashNoteTitle');
          editor.innerHTML=`<div class="cash-note-heading"><h3 id="cashNoteTitle">Комментарий к кассе</h3><button type="button" class="close-btn cash-note-close" data-note-close aria-label="Закрыть комментарий">×</button></div><p class="cash-note-context">${esc(item.name)} · ${report.half===1?'1–15':'16–конец'} · ${esc(new Date(report.month+'-01T12:00:00Z').toLocaleDateString('ru-RU',{month:'long',timeZone:'Europe/Moscow'}))}</p><div class="cash-note-field-head"><label for="cashNoteText">Ваш комментарий</label><strong class="cash-note-total" aria-label="Итого касса вместе с откатом">${Number(item.total||0).toLocaleString('ru-RU')} ₽</strong></div><textarea id="cashNoteText" maxlength="240" rows="4" placeholder="Например: сдал часть, остаток 5 000 ₽">${esc(item.comment||'')}</textarea><small data-note-count></small><div class="cash-note-buttons"><button type="submit" class="primary">Сохранить</button><button type="button" class="secondary" data-note-cancel>Отмена</button></div><p role="status" aria-live="polite"></p>`;
          overlay.append(editor);document.body.append(overlay);
          const input=qs('textarea',editor),pageX=window.scrollX,pageY=window.scrollY,tableTop=body.scrollTop,modalTop=qs('#modal').scrollTop;
          const viewport=window.visualViewport;
          const placeEditor=()=>{
            overlay.style.top=(viewport?.offsetTop||0)+'px';overlay.style.left=(viewport?.offsetLeft||0)+'px';
            overlay.style.width=(viewport?.width||window.innerWidth)+'px';overlay.style.height=(viewport?.height||window.innerHeight)+'px';
          };
          placeEditor();viewport?.addEventListener('resize',placeEditor);viewport?.addEventListener('scroll',placeEditor);window.addEventListener('resize',placeEditor);
          const dismiss=()=>{
            viewport?.removeEventListener('resize',placeEditor);viewport?.removeEventListener('scroll',placeEditor);window.removeEventListener('resize',placeEditor);document.removeEventListener('keydown',keyHandler);
            input.blur();overlay.remove();if(cashNoteDismiss===dismiss)cashNoteDismiss=null;
            if(cashActive()){body.scrollTop=tableTop;qs('#modal').scrollTop=modalTop;button.focus({preventScroll:true});window.scrollTo(pageX,pageY)}
          };
          const cancel=()=>{if(cashMarkSaving)return;dismiss();if(cashActive())drawCash()};
          const keyHandler=event=>{
            if(event.key==='Escape'){event.preventDefault();cancel()}
            if(event.key==='Tab'){const fields=qsa('textarea,button:not(:disabled)',editor),first=fields[0],last=fields.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus()}}
          };
          cashNoteDismiss=dismiss;document.addEventListener('keydown',keyHandler);
          input.oninput=()=>qs('[data-note-count]',editor).textContent=input.value.length+' / 240';input.oninput();
          input.focus({preventScroll:true});
          qsa('[data-note-cancel],[data-note-close]',editor).forEach(control=>control.onclick=cancel);
          editor.onsubmit=async event=>{
            event.preventDefault();if(cashMarkSaving||!cashActive())return;
            cashMarkSaving=true;qsa('button,textarea',editor).forEach(x=>x.disabled=true);
            try{
              await api(`/api/cash/receipt/${item.tg_id}?month=${encodeURIComponent(report.month)}&half=${report.half}`,{method:'PUT',body:JSON.stringify({field:'comment',comment:input.value,version:item.receipt_version||0})});
              dismiss();if(cashActive())toast('Комментарий сохранён');
            }catch(error){qs('[role=status]',editor).textContent=error.message}
            finally{cashMarkSaving=false;qsa('button,textarea',editor).forEach(x=>x.disabled=false);if(cashActive()&&!editor.isConnected)await drawCash()}
          };
        });
        qsa('[data-reconcile-mark]',body).forEach(button=>button.onclick=async()=>{
          if(cashMarkSaving||!cashActive())return;
          const item=report.items.find(item=>String(item.tg_id)===button.dataset.reconcileMark);
          if(!report.can_mark||!item)return;
          cashMarkSaving=true;button.disabled=true;
          try{
            await api(`/api/cash/reconciliation/${item.tg_id}?month=${encodeURIComponent(report.month)}&half=${report.half}`,{method:'PUT',body:JSON.stringify({checked:!item.checked,version:item.version})});
            toast(item.checked?'Ручная отметка снята':'Отмечено вручную ✅');
          }catch(error){toast(error.message)}
          finally{cashMarkSaving=false;if(cashActive())await drawCash()}
        });
''' + reminder + r'''
        requestAnimationFrame(()=>{if(cashActive()&&request===cashRequest){qs('#modal').scrollTop=0;fitCashReconciliation();rememberCashTablePosition()}});
        return;
      }
'''
    section = section[:at]+branch+section[at:]
    html = html[:start]+section+html[end:]
    html = html.replace('function showModal(title, body) {',
        "function showModal(title, body) {qs('#modal').classList.remove('cash-reconciliation-open');",1)
    return html.replace('</head>', r'''<style id="cash-reconciliation-style">
#modal.owner-cash-modal.cash-reconciliation-open{overflow:hidden;display:flex;flex-direction:column}
#modal.owner-cash-modal.cash-reconciliation-open{padding:6px!important}
#modal.cash-reconciliation-open .modal-head{display:grid;grid-template-columns:auto auto minmax(0,1fr) auto 30px;align-items:center;gap:3px;margin:0 0 3px!important;min-height:30px;flex-shrink:0}
#modal.cash-reconciliation-open .modal-head h3{font-size:17px;line-height:1.1;white-space:nowrap}
#modal.cash-reconciliation-open .modal-head .cash-reconciliation-toolbar{display:contents}
#modal.cash-reconciliation-open .modal-head .cash-reconciliation-toolbar>span{font-size:9px;white-space:nowrap;color:var(--muted)}
#modal.cash-reconciliation-open .modal-head .cash-reconciliation-toolbar>strong{font-size:10px;white-space:nowrap;text-align:center;color:var(--text)}
#modal.cash-reconciliation-open .modal-head #cashReminder{font-size:10px;padding:4px;min-height:28px;border-radius:7px;white-space:nowrap}
#modal.cash-reconciliation-open .owner-cash-controls{margin:0 0 3px!important;flex-shrink:0}
#modal.cash-reconciliation-open .owner-cash-ranges{margin:0 0 3px!important;gap:4px;flex-shrink:0}
#modal.cash-reconciliation-open #cashMonthTabs{margin:0 0 4px!important;gap:4px;flex-shrink:0}
#modal.cash-reconciliation-open #cashTableBody{min-height:0;min-width:0;flex:0 1 auto;overflow:auto;display:flex;flex-direction:column;white-space:normal}
.cash-reconciliation-toolbar{display:flex;align-items:center;justify-content:space-between;gap:5px;font-size:11px;color:var(--muted);margin:5px 0}
.cash-reconciliation-toolbar strong{color:var(--text);font-size:12px}
.cash-reconciliation-grid{display:grid;grid-template-columns:repeat(var(--reconcile-cols,4),minmax(0,1fr));grid-auto-rows:auto;align-items:start;align-content:start;gap:5px;min-height:0;flex-shrink:0}
.cash-reconciliation-card{container-type:inline-size;min-width:0;min-height:0;padding:4px;border:1px solid var(--line);border-radius:10px;background:var(--panel);font-size:var(--reconcile-font,10px);line-height:1.15;display:flex;flex-direction:column;font-variant-numeric:tabular-nums;white-space:normal;overflow:hidden}
.cash-reconciliation-card.is-checked{border-color:#377b50}
.cash-reconciliation-name{display:grid;grid-template-columns:minmax(0,1fr) 20px;align-items:center;column-gap:2px;row-gap:1px;min-width:0;white-space:normal;overflow-wrap:anywhere;margin-bottom:3px}
.cash-reconciliation-name strong{grid-column:1;grid-row:1;min-width:0;display:block;white-space:nowrap;overflow-wrap:normal;font-size:9px;font-size:clamp(8px,10cqi,12px);letter-spacing:-.2px;font-weight:650}
.cash-reconciliation-place{grid-column:1/-1;grid-row:2;min-width:0;display:block;white-space:normal;overflow-wrap:anywhere}
.cash-reconciliation-mark{position:static;grid-column:2;grid-row:1;justify-self:end;border:0!important;background:transparent!important;padding:0!important;width:20px;min-width:20px;min-height:23px!important;height:23px;font-size:16px!important;line-height:23px;display:grid;place-items:center;cursor:pointer;overflow:hidden}
.cash-reconciliation-mark:disabled{opacity:.6}.cash-reconciliation-mark[aria-readonly=true]{cursor:default}
.cash-reconciliation-line{display:flex;align-items:baseline;justify-content:space-between;gap:2px;margin:0;white-space:nowrap}
.cash-reconciliation-line span{color:var(--muted)}.cash-reconciliation-line b{font-weight:500}
.cash-reconciliation-total{margin-top:4px;padding-top:2px;border-top:1px solid var(--line);color:var(--gold);font-weight:700}
.cash-reconciliation-total span{color:var(--gold)}.cash-reconciliation-total b{font-weight:750}
.cash-reconciliation-legend{margin:5px 0 0;font-size:10px;color:var(--muted);line-height:1.3}
.cash-receipt-actions{display:flex;align-items:center;justify-content:space-between;gap:2px;margin-top:3px;border-top:1px solid var(--line);padding-top:3px}
.cash-receipt-actions button{border:1px solid var(--line);border-radius:5px;background:var(--panel2);color:var(--text);padding:2px;min-height:26px;cursor:pointer;font-size:10px;white-space:nowrap}
.cash-receipt-actions .cash-received{flex:1;border-color:transparent;background:transparent;text-align:left}
.cash-receipt-actions .cash-note{width:25px;flex-shrink:0;font-size:14px}
.cash-receipt-actions .cash-note.has-note{background:var(--gold);color:var(--panel)}
.cash-note-editor{margin-top:5px;font-size:11px;min-width:0}
.cash-note-editor textarea{display:block;box-sizing:border-box;width:100%;min-width:0;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:4px;padding:3px;font-size:12px;resize:vertical}
.cash-note-editor>div{display:flex;flex-wrap:wrap;gap:3px;margin-top:3px}
.cash-note-editor button{font-size:10px;padding:5px 3px;border-radius:5px}
.cash-note-editor p{color:var(--red);white-space:normal;overflow-wrap:anywhere}
.cash-note-overlay{position:fixed;z-index:1000;box-sizing:border-box;display:flex;align-items:center;justify-content:center;padding:8px;background:rgba(0,0,0,.35);overscroll-behavior:contain}
.cash-note-dialog{box-sizing:border-box;width:100%;max-width:440px;max-height:100%;overflow:auto;overscroll-behavior:contain;padding:16px;border:1px solid var(--line);border-radius:14px;background:var(--panel);color:var(--text);box-shadow:0 8px 40px rgba(0,0,0,.2)}
.cash-note-dialog h3{font-size:18px;margin:0 0 6px}
.cash-note-heading{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 6px}
.cash-note-dialog .cash-note-heading h3{margin:0;min-width:0;overflow-wrap:anywhere}
.cash-note-dialog .cash-note-close{flex:none;display:grid;place-items:center;width:32px;height:32px;min-height:32px;padding:0;font-size:22px;line-height:1;border-radius:9px}
.cash-note-field-head{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:4px 10px;margin:0 0 5px}
.cash-note-dialog .cash-note-field-head label{margin:0}
.cash-note-total{color:var(--gold);font-size:14px;font-weight:750;white-space:nowrap;font-variant-numeric:tabular-nums}
.cash-note-dialog .cash-note-context{font-size:14px;font-weight:650;margin:0 0 12px;overflow-wrap:anywhere}
.cash-note-dialog label{display:block;font-size:14px;margin-bottom:5px}
.cash-note-dialog textarea{box-sizing:border-box;display:block;width:100%;min-height:96px;max-height:140px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--text);font-size:16px!important;line-height:1.4;resize:none}
.cash-note-dialog small{display:block;color:var(--muted);font-size:12px;text-align:right;margin:4px 0 8px}
.cash-note-buttons{display:flex;gap:8px}.cash-note-buttons button{flex:1;min-height:40px;padding:8px;font-size:14px}
.cash-note-dialog [role=status]{margin:8px 0 0;color:var(--red);font-size:13px;overflow-wrap:anywhere}
</style></head>''',1).replace('</body>',r'''<script>
function ownerCashReconciliationHtml(report){
 const number=value=>Number(value||0).toLocaleString('ru-RU');
 const cards=(report.items||[]).map((item,index)=>{const parts=String(item.name||'').trim().split(/\s+/),first=parts.shift()||'Бригадир',place=parts.join(' ');return `<article class="cash-reconciliation-card${item.checked?' is-checked':''}"><div class="cash-reconciliation-name"><strong>${index+1}. ${esc(first)}</strong><span class="cash-reconciliation-place">${esc(place)}</span>${report.can_mark?`<button type="button" class="cash-reconciliation-mark" data-reconcile-mark="${item.tg_id}" aria-pressed="${item.checked}" aria-label="${item.checked?'Снять отметку':'Отметить вручную'}: ${esc(item.name)}" title="${item.source==='manual'?'Отмечено вручную':item.checked?'Подсчёт отправлен из личного кабинета':'Подсчёт не отправлен'}">${item.checked?'✅':'⬜️'}</button>`:`<span class="cash-reconciliation-mark" aria-readonly="true">${item.checked?'✅':'⬜️'}</span>`}</div><div class="cash-reconciliation-line"><span>Заказов:</span><b>${number(item.orders)}</b></div><div class="cash-reconciliation-line"><span>Касса:</span><b>${number(item.cash)}</b></div><div class="cash-reconciliation-line"><span>Откат:</span><b>${number(item.kickback)}</b></div><div class="cash-reconciliation-line cash-reconciliation-total"><span>Итого:</span><b>${number(item.total)}</b></div><div class="cash-receipt-actions">${report.can_mark?`<button type="button" class="cash-received" data-cash-received="${item.tg_id}" aria-pressed="${!!item.received}" aria-label="${item.received?'Снять отметку сдачи':'Касса сдана'}: ${esc(item.name)}">Сдана ${item.received?'✅':'⬜️'}</button><button type="button" class="cash-note${item.comment?' has-note':''}" data-cash-note="${item.tg_id}" aria-label="Комментарий: ${esc(item.name)}" title="${esc(item.comment||'Добавить комментарий')}">📝</button>`:`<span>Сдана ${item.received?'✅':'⬜️'}</span>`}</div></article>`}).join('');
 return `<div class="cash-reconciliation-toolbar"><span>Суммы в ₽</span><strong>Сверились: ${report.checked_count} из ${report.items.length}</strong><button type="button" class="secondary" id="cashReminder" ${cashReminderSending?'disabled':''}>🔔 Сверка</button></div><div class="cash-notes-send-row"><span>Заметок: ${(report.items||[]).filter(item=>String(item.comment||'').trim()).length}</span><button type="button" class="secondary" id="cashNotesSend" aria-label="Отправить каждому кассовому бригадиру его сохранённую заметку в группу кассы" ${!report.can_mark||report.notes_sending||!(report.items||[]).some(item=>String(item.comment||'').trim())?'disabled':''}>📨 Заметки</button></div><p id="cashNotesSendStatus" role="status" aria-live="polite" ${report.notes_status?'':'hidden'}>${esc(report.notes_status||'')}</p>${cards?`<div class="cash-reconciliation-grid">${cards}</div>`:'<div class="empty">Кассовых бригадиров пока нет</div>'}<p class="cash-reconciliation-legend">✅ сверху — сверился · Сдана — деньги получены · 📝 — комментарий</p>`;
}
function fitCashReconciliation(){
 const modal=qs('#modal'),grid=qs('.cash-reconciliation-grid',modal);
 if(!modal?.classList.contains('cash-reconciliation-open')||!grid)return;
 const count=grid.children.length;if(!count)return;
 // Keep the normal type size whenever the cards fit. On shorter screens,
 // reduce it only as far as 10px; leave scrolling available for larger lists.
 grid.style.setProperty('--reconcile-cols',Math.min(4,count));
 grid.style.gridTemplateRows='none';grid.style.height='auto';
 const body=qs('#cashTableBody',modal);
 for(let font=12;font>=10;font--){
   grid.style.setProperty('--reconcile-font',font+'px');
   if(!body||body.scrollHeight<=body.clientHeight+1)break;
 }
}
window.addEventListener('resize',fitCashReconciliation);
window.visualViewport?.addEventListener('resize',fitCashReconciliation);
</script></body>''',1)


INLINE_INDEX_HTML = _cash_reconciliation_cards_html(INLINE_INDEX_HTML)


def _employees_focus_html(html):
    panel = '<div id="selectedDayHeader" class="section-head"></div><h3>Заказы</h3><section id="summary" class="summary-grid"></section>'
    button = '<button class="primary small" id="employeeImportBtn">Импорт</button>'
    handler = "qs('#employeeImportBtn').onclick=openEmployeeImport;"
    view = "  const v = currentViewId();"
    for anchor in (panel, button, handler, view):
        if html.count(anchor) != 1:
            raise RuntimeError('Employees focus anchor missing: ' + anchor[:70])
    html = html.replace(panel, '<div id="dispatcherOrderOverview">' + panel + '</div>', 1)
    html = html.replace(button, '', 1).replace(handler, '', 1)
    html = html.replace(view, view + "\n  qs('#dispatcherOrderOverview').hidden=['employeesView','ownerBrigadeControlView'].includes(v);", 1)
    return html.replace('</head>', '<style>#dispatcherOrderOverview[hidden]{display:none!important}</style></head>', 1)


INLINE_INDEX_HTML = _employees_focus_html(INLINE_INDEX_HTML)


def _one_screen_orders_html(html):
    return html.replace('</head>', r'''<style id="one-screen-orders-style">
body.dashboard-compact{height:100dvh;min-height:0;overflow:hidden;padding-bottom:0}
body.dashboard-compact .topbar{position:relative}
body.dashboard-compact main{width:100%;max-width:none;margin:0;padding-left:8px;padding-right:8px;transform-origin:top left}
body.dashboard-compact #readinessGroups .group-block{min-height:calc(80px + var(--orders-group-extra,0px))}
</style></head>''', 1).replace('</body>', r'''<script>
let ordersScreenFitFrame=0;
function fitOrdersScreen(){
 const main=qs('main');if(!main)return;
 if(!document.body.classList.contains('dashboard-compact')){main.style.removeProperty('transform');main.style.removeProperty('--orders-group-extra');return}
 const viewport=window.visualViewport,viewportBottom=viewport?viewport.offsetTop+viewport.height:window.innerHeight;
 const nav=qs('#roleNavigation')||qs('.bottom-nav');
 const navTop=nav?.getBoundingClientRect().top;
 const bottom=navTop>0?Math.min(navTop,viewportBottom):viewportBottom-72;
 const top=main.getBoundingClientRect().top;
 const groups=qsa('#readinessGroups .group-block');
 main.style.setProperty('--orders-group-extra','0px');
 const baseHeight=Math.max(main.scrollHeight,main.offsetHeight);
 if(!baseHeight)return;
 const available=Math.max(1,bottom-top-5);
 // Give spare space back to the group cards instead of leaving a blank footer.
 if(groups.length&&baseHeight<available){
  main.style.setProperty('--orders-group-extra',Math.max(0,Math.floor((available-baseHeight)/groups.length))+'px');
 }
 const height=Math.max(main.scrollHeight,main.offsetHeight);
 const scale=Math.min(1,Math.max(.1,available/height));
 main.style.transform=scale<1?`scaleY(${scale})`:'';
}
function scheduleOrdersScreenFit(){
 if(ordersScreenFitFrame)return;
 ordersScreenFitFrame=requestAnimationFrame(()=>{ordersScreenFitFrame=0;fitOrdersScreen()});
}
new MutationObserver(scheduleOrdersScreenFit).observe(document.body,{attributes:true,attributeFilter:['class'],childList:true,subtree:true});
if(window.ResizeObserver){const observer=new ResizeObserver(scheduleOrdersScreenFit);for(const el of [qs('main'),qs('.topbar'),qs('#dateStrip')])if(el)observer.observe(el)}
window.addEventListener('resize',scheduleOrdersScreenFit);
window.visualViewport?.addEventListener('resize',scheduleOrdersScreenFit);
document.fonts?.ready.then(scheduleOrdersScreenFit);
scheduleOrdersScreenFit();
</script></body>''',1)


INLINE_INDEX_HTML = _one_screen_orders_html(INLINE_INDEX_HTML)


# Shared warm light palette for dispatcher, personal cabinets and dialogs.
def _warm_light_interface_html(html):
    palette = {}
    def colors(old, new):
        for color in old.split():
            palette[color] = new
    colors('#09090b #0a0a0b', '#e8e7e3')
    colors('#101013 #111114 #121215', '#f5f3ee')
    colors('#17171c #18181c #1b1b1f #202026 #232328 #24242a #26262c', '#e9e7e1')
    colors('#2a2a30 #303038 #34343b #3a3a40 #4a4a52', '#d4d1c8')
    colors('#f5f5f5 #fff #ddd #ccc #cfcfd4 #333', '#303330')
    colors('#9a9aa3 #777', '#686d68')
    colors('#171208 #17130b #1b160c', '#40351d')
    colors('#211d15 #211d14 #2a251b #302719 #272216 #282315 #19170f #25221b #302a1e #2b2315 #25251f #211a14', '#efe4c9')
    colors('#433923 #5b5036 #4c3a28 #8f7442', '#b4a078')
    colors('#d8b46a #d9c59a #f6dfac #f0d397 #cdbf9f #ead39d #ffd45c', '#967539')
    colors('#1b3325 #284330 #17221c #14231b #274132 #14241a #14281d #20382b', '#e0ece3')
    colors('#58c785 #53c68c #55c98b #7ee0a2 #83dfaa #a9dfbd #305c40 #326c4b #377b50', '#388167')
    colors('#07120b', '#f5f3ee')
    colors('#201d27 #23212a #241c22', '#eee5ef')
    colors('#d7c7f1 #d3cadf #edc7df', '#775780')
    colors('#241616 #221616 #332021 #492326 #291b1d', '#f1dedb')
    colors('#5a2d2d #512c2c #764447 #7b3a40', '#d5aaa4')
    colors('#e06969 #ff5555 #ff5c5c #ff7979 #ff7b7b #ffaaaa #ffb5b5 #ffb7b7', '#a44740')
    colors('#e3b454', '#997526')
    colors('#6ca6e8 #9fc8ff', '#416e94')
    colors('#000b', '#302c2433')
    colors('#000', '#302c2426')
    html = re.sub(r'(?<![\w-])#[0-9a-fA-F]{3,8}\b',
                  lambda match: ('var(--legion-color-' + match.group()[1:].lower() + ')' if match.group().lower() in palette else match.group()), html)
    light_colors = ';'.join('--legion-color-' + key[1:] + ':' + value for key, value in palette.items())
    dark_colors = ';'.join('--legion-color-' + key[1:] + ':' + key for key in palette)
    html = html.replace('</head>', '<style id="legion-legacy-palette">:root{' + light_colors + '}:root[data-legion-theme="dark"]{' + dark_colors + '}</style></head>', 1)
    html = re.sub(r'(<meta name="theme-color" content=")[^"]*(")', r'\g<1>#e8e7e3\2', html, count=1)
    html = html.replace('rgba(13,13,15,.95)', 'rgba(245,243,238,.97)')
    html = html.replace('rgba(0,0,0,.62)', 'rgba(48,51,48,.24)')
    html = html.replace('rgba(0,0,0,.6)', 'rgba(48,51,48,.12)')
    html = html.replace('color-scheme:dark', 'color-scheme:light')
    html = html.replace('</head>', r'''<style id="warm-light-interface-style">
:root{color-scheme:light;--bg:#e8e7e3;--panel:#f5f3ee;--panel2:#e9e7e1;--line:#d4d1c8;--text:#303330;--muted:#686d68;--gold:#967539;--gold2:#b4a078;--green:#388167;--red:#a44740;--amber:#997526;--blue:#416e94}
html,body{background:var(--bg);color:var(--text)}
.topbar{background:linear-gradient(180deg,#e8e7e3 85%,rgba(232,231,227,0))}
.primary,.primary.small{background:#e5cd93;color:#40351d}
.primary:disabled{opacity:.5}
.secondary,.icon-btn,.search,input,select,textarea{color:var(--text)}
input,select,textarea{color-scheme:light}
.date-pill.active{background:#efe4c9;border-color:#a88b50}
.segmented button.active{background:#efe4c9;color:#775c27}
.group-stats button{background:#e9e7e1;color:var(--muted)}
.group-stats button.has-people{box-shadow:inset 0 0 0 1px #d4d1c8}
.group-stats button:active{background:#e0ddd5}
.danger{background:#f1dedb;border-color:#d5aaa4;color:#a44740}
#readinessAvralBtn{background:#f1dedb!important;color:#a44740!important;border-color:#d5aaa4!important}
#readinessSendBtn{background:#e5cd93;color:#40351d}
.bottom-nav,.role-navigation{background:rgba(245,243,238,.97);border-top-color:var(--line)}
.bottom-nav button,.role-navigation button{color:#737975}
.bottom-nav button.active,.role-navigation button.active{color:#967539}
.modal,.drawer{background:#f5f3ee;box-shadow:0 12px 45px rgba(48,51,48,.12)}
.backdrop{background:rgba(48,51,48,.24)}
#toast{background:#303330;color:#f5f3ee;border:1px solid #5b605a}
#fatal{background:#f5f3ee;color:#a44740}
#dayCommentBtn{background:#efe4c9;color:#775c27;border-color:#b4a078}
.cabinet-status-note,.attention{color:var(--text)}
</style></head>''', 1)
    return html.replace('</body>', r'''<script>
function applyLegionLightTelegramChrome(){
 const webApp=window.Telegram?.WebApp;
 const dark=document.documentElement.dataset.legionTheme==='dark',bg=dark?'#09090b':'#e8e7e3',panel=dark?'#121215':'#f5f3ee';
 try{webApp?.setHeaderColor?.(bg)}catch{}
 try{webApp?.setBackgroundColor?.(bg)}catch{}
 try{webApp?.setBottomBarColor?.(panel)}catch{}
}
applyLegionLightTelegramChrome();
try{window.Telegram?.WebApp?.onEvent?.('themeChanged',applyLegionLightTelegramChrome)}catch{}
</script></body>''',1)


INLINE_INDEX_HTML = _warm_light_interface_html(INLINE_INDEX_HTML)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', '<style id="control-spacing-fix">.owner-brigade-control-open #dispatcherOrderOverview{display:none!important}.owner-brigade-control-open #ownerBrigadeControlContent{padding:0 0 16px!important}</style></head>', 1)


def _cabinet_theme_picker_html(html):
    old = '</div><button type="button" class="cabinet-edit-profile" data-cabinet-action="${p?\'profile\':\'register\'}" aria-label="${p?\'Изменить профиль\':\'Заполнить профиль\'}">${cabinetIcon(p?\'edit\':\'plus\')}</button></article>`;'
    new = '<button type="button" class="cabinet-edit-profile" data-cabinet-action="${p?\'profile\':\'register\'}" aria-label="${p?\'Изменить профиль\':\'Заполнить профиль\'}">${cabinetIcon(p?\'edit\':\'plus\')}</button></div>${legionThemePickerHtml()}</article>`;'
    if html.count(old) != 1:
        raise RuntimeError('Profile theme picker anchor missing')
    html = html.replace(old, new, 1)
    html = html.replace('function updateRoleNavigation(){', 'function updateRoleNavigation(){\n  applyLegionAccountTheme();', 1)
    return html.replace('</head>', r'''<style id="cabinet-theme-picker-style">
:root[data-legion-theme="dark"]{color-scheme:dark;--bg:#09090b;--panel:#121215;--panel2:#18181c;--line:#2a2a30;--text:#f5f5f5;--muted:#9a9aa3;--gold:#d8b46a;--gold2:#8f7442;--green:#58c785;--red:#e06969;--amber:#e3b454;--blue:#6ca6e8}
html[data-legion-theme="dark"] .topbar{background:linear-gradient(180deg,#09090b 85%,transparent)}
html[data-legion-theme="dark"] .primary{background:var(--gold);color:#171208}
html[data-legion-theme="dark"] input,html[data-legion-theme="dark"] select,html[data-legion-theme="dark"] textarea{color-scheme:dark}
html[data-legion-theme="dark"] #cabinetDate{color-scheme:dark}
html[data-legion-theme="dark"] .date-pill.active,html[data-legion-theme="dark"] .segmented button.active{background:#211d15;border-color:var(--gold);color:#f6dfac}
html[data-legion-theme="dark"] .group-stats button{background:#1b1b1f;color:var(--muted)}
html[data-legion-theme="dark"] .group-stats button.has-people{box-shadow:inset 0 0 0 1px #34343b}
html[data-legion-theme="dark"] .group-stats button:active{background:#26262c}
html[data-legion-theme="dark"] .danger,html[data-legion-theme="dark"] #readinessAvralBtn{background:#241616!important;border-color:#5a2d2d!important;color:#ffaaaa!important}
html[data-legion-theme="dark"] #readinessSendBtn{background:var(--gold);color:#171208}
html[data-legion-theme="dark"] .bottom-nav,html[data-legion-theme="dark"] .role-navigation{background:rgba(13,13,15,.97)}
html[data-legion-theme="dark"] .bottom-nav button,html[data-legion-theme="dark"] .role-navigation button{color:var(--muted)}
html[data-legion-theme="dark"] .bottom-nav button.active,html[data-legion-theme="dark"] .role-navigation button.active{color:var(--gold)}
html[data-legion-theme="dark"] .modal,html[data-legion-theme="dark"] .drawer{background:#111114;box-shadow:0 12px 45px #0006}
html[data-legion-theme="dark"] .backdrop{background:rgba(0,0,0,.62)}
html[data-legion-theme="dark"] #toast{background:#24242a;color:var(--text)}
html[data-legion-theme="dark"] #fatal{background:var(--panel);color:var(--red)}
html[data-legion-theme="dark"] #dayCommentBtn{background:#211d15;color:var(--gold);border-color:var(--gold2)}
.cabinet-profile{gap:10px;overflow:visible}
.cabinet-profile-copy{display:grid;grid-template-columns:minmax(0,1fr) 40px;align-items:center;gap:5px}
.cabinet-profile-copy h2{grid-column:1;grid-row:1;font-size:16px}
.cabinet-profile-copy p{grid-column:1;grid-row:2}
.cabinet-profile-copy .cabinet-edit-profile{grid-column:2;grid-row:1;min-width:40px;min-height:40px;width:40px;padding:8px;border:1px solid var(--line);border-radius:11px;background:var(--panel);color:var(--muted)}
.legion-theme-menu{position:relative;flex:none;align-self:center;z-index:22}
.legion-theme-menu>summary{display:grid;place-items:center;list-style:none;width:40px;height:40px;cursor:pointer;border:1px solid var(--line);border-radius:11px;background:var(--panel);color:var(--text)}
.legion-theme-menu>summary::-webkit-details-marker{display:none}
.legion-theme-menu svg{width:22px;height:22px}
.legion-theme-popup{position:absolute;right:0;top:calc(100% + 7px);width:190px;border:1px solid var(--line);border-radius:13px;background:var(--panel);box-shadow:0 10px 30px #0002;overflow:hidden}
.legion-theme-popup>p{font-size:12px;color:var(--muted);margin:0;padding:10px 12px;border-bottom:1px solid var(--line)}
.legion-theme-popup button{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:10px 12px;background:transparent;border:0;text-align:left;color:var(--text);font-size:14px;cursor:pointer}
.legion-theme-popup button+button{border-top:1px solid var(--line)}
.legion-theme-popup button[aria-pressed="true"]{background:var(--panel2)}
.legion-theme-popup .theme-choice-check{margin-left:auto;color:var(--gold)}
</style></head>''', 1).replace('</body>', r'''<script>
var legionThemeAccount=null;
function legionThemePickerHtml(){
 const selected=document.documentElement.dataset.legionTheme||'light';
 const icon='<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M12 3a9 9 0 0 1 0 18Z" fill="currentColor"/></svg>';
 return `<details class="legion-theme-menu"><summary aria-label="Выбрать цвет темы" title="Тема интерфейса">${icon}</summary><div class="legion-theme-popup"><p>Тема интерфейса</p>${[['light','☀','Белая'],['dark','☾','Чёрная']].map(([key,symbol,label])=>`<button type="button" data-legion-theme-choice="${key}" aria-pressed="${selected===key}"><span aria-hidden="true">${symbol}</span>${label}<span class="theme-choice-check" aria-hidden="true">${selected===key?'✓':''}</span></button>`).join('')}</div></details>`;
}
function setLegionTheme(theme){
 theme=theme==='dark'?'dark':'light';
 document.documentElement.dataset.legionTheme=theme;
 const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=theme==='dark'?'#09090b':'#e8e7e3';
 qsa('[data-legion-theme-choice]').forEach(button=>{const selected=button.dataset.legionThemeChoice===theme;button.setAttribute('aria-pressed',String(selected));qs('.theme-choice-check',button).textContent=selected?'✓':''});
 applyLegionLightTelegramChrome();
}
function applyLegionAccountTheme(){
 const uid=state.bootstrap?.user?.id;if(!uid)return;
 if(String(uid)===legionThemeAccount)return;
 legionThemeAccount=String(uid);
 let theme='light';try{theme=localStorage.getItem('legion-theme:'+uid)||'light'}catch{}
 setLegionTheme(theme);
}
document.addEventListener('click',event=>{
 const choice=event.target.closest('[data-legion-theme-choice]');
 if(choice){
  const uid=state.bootstrap?.user?.id;if(!uid)return;
  const theme=choice.dataset.legionThemeChoice;if(!['light','dark'].includes(theme))return;
  setLegionTheme(theme);
  try{localStorage.setItem('legion-theme:'+uid,theme)}catch{toast('Тема изменена. Не удалось сохранить выбор на устройстве')}
  choice.closest('details').open=false;return;
 }
 if(!event.target.closest('.legion-theme-menu'))qsa('.legion-theme-menu[open]').forEach(menu=>menu.open=false);
});
document.addEventListener('keydown',event=>{if(event.key==='Escape')qsa('.legion-theme-menu[open]').forEach(menu=>menu.open=false)});
applyLegionAccountTheme();
</script></body>''', 1)


INLINE_INDEX_HTML = _cabinet_theme_picker_html(INLINE_INDEX_HTML)


def _current_half_tables_html(html):
    def replace_once(source, before, after):
        if source.count(before) != 1:
            raise RuntimeError('Current half navigation anchor missing: ' + before[:80])
        return source.replace(before, after, 1)

    start = html.index('async function openPersonalTables(resume=null){')
    end = html.index('</script>', start)
    section = html[start:end]
    section = replace_once(section, " const grid=qs('#personalGrid'),status=qs('#personalStatus'),session=cashModalGeneration;",
        " let openCurrentHalf=!resume;\n const grid=qs('#personalGrid'),status=qs('#personalStatus'),session=cashModalGeneration;")
    section = replace_once(section,
        "grid.scrollLeft=left;grid.scrollTop=top;qs('#modal').scrollTop=modalTop;",
        "grid.scrollLeft=left;grid.scrollTop=openCurrentHalf?legionTableOpeningTop(month,grid):top;openCurrentHalf=false;qs('#modal').scrollTop=modalTop;")
    section = replace_once(section, "month=e.target.value;load()", "month=e.target.value;openCurrentHalf=true;load()")
    section = replace_once(section, "kind=btn.dataset.personalKind;", "kind=btn.dataset.personalKind;openCurrentHalf=true;")
    html = html[:start]+section+html[end:]

    start = html.index('async function openCashTable(){')
    end = html.index('let ownerCabinetRequest=', start)
    section = html[start:end]
    section = replace_once(section, 'let pendingRestore=resume;', 'let pendingRestore=resume,openCurrentCashHalf=!resume;')
    section = replace_once(section, 'pendingRestore=null;cashMonth=value;', 'pendingRestore=null;openCurrentCashHalf=true;cashMonth=value;')
    section = replace_once(section, 'pendingRestore=null;cashRange=btn.dataset.cashRange;', 'pendingRestore=null;openCurrentCashHalf=true;cashRange=btn.dataset.cashRange;')
    section = replace_once(section, 'currentScroller.scrollTop=savedTableTop',
        "currentScroller.scrollTop=openCurrentCashHalf&&cashRange==='month'?legionTableOpeningTop(cashMonth,currentScroller):savedTableTop;openCurrentCashHalf=false")
    html = html[:start]+section+html[end:]
    return html.replace('</body>', r'''<script>
function legionTableOpeningTop(month,scroller){
 const today=new Date().toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'});
 // Archives start at day one. Current-month tables open at the Moscow half.
 if(month!==today.slice(0,7)||Number(today.slice(8,10))<=15)return 0;
 if(!scroller.querySelector('.period-table-page[data-period-page="2"]'))return 0;
 return Number(scroller.dataset.periodPageHeight)||0;
}
</script></body>''', 1)


INLINE_INDEX_HTML = _current_half_tables_html(INLINE_INDEX_HTML)

# A nested cash note must not survive navigation to another modal/account.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('function showModal(title, body) {',
    "function showModal(title, body) {if(typeof cashNoteDismiss==='function')cashNoteDismiss();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('function closeModal(){',
    "function closeModal(){if(typeof cashNoteDismiss==='function')cashNoteDismiss();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<script>var cashNoteDismiss=null;</script></body>', 1)


# The order ledger projects the existing counters, including legacy overrides.
# No historical rows are copied, reset or counted a second time.
@app.get('/api/order-count-table/{month}')
def order_count_table(month: str, db: Session = Depends(get_db),
                      user: TelegramUser = Depends(require_admin)):
    days = _personal_month_days(month)
    first_day = date.fromisoformat(month + '-01')
    daily = []
    for number in range(1, days + 1):
        work_date = first_day.replace(day=number)
        sources = {source: _category_counts(db, work_date, source) for source in ORDER_COUNT_TYPES}
        staff = sum(int(key.rsplit(':', 1)[1]) * count
                    for values in sources.values() for key, count in values['counts'].items())
        daily.append({'date': work_date.isoformat(), 'day': number,
                      'private': sources['private']['total'], 'gbu': sources['gbu']['total'],
                      'total': sum(values['total'] for values in sources.values()),
                      'staff_count': staff,
                      'unallocated': sum(values['unallocated'] for values in sources.values()),
                      'sources': sources})

    def totals(rows):
        result = {key: sum(row[key] for row in rows)
                  for key in ('private', 'gbu', 'total', 'staff_count', 'unallocated')}
        result['sources'] = {}
        for source in ORDER_COUNT_TYPES:
            counts = dict.fromkeys(ORDER_COUNT_TYPES[source], 0)
            for row in rows:
                for key, count in row['sources'][source]['counts'].items():
                    counts[key] = counts.get(key, 0) + count
            result['sources'][source] = {
                'counts': counts,
                'unallocated': sum(row['sources'][source]['unallocated'] for row in rows),
                'total': sum(row['sources'][source]['total'] for row in rows),
            }
        return result

    return {'month': month, 'days': days, 'daily': daily,
            'first': totals(daily[:15]), 'second': totals(daily[15:]), 'total': totals(daily)}


def _orders_personal_table_html(html):
    def replace_once(source, old, new):
        if source.count(old) != 1:
            raise RuntimeError('Orders table anchor missing: ' + old[:90])
        return source.replace(old, new, 1)

    start = html.index('async function openPersonalTables(resume=null){')
    end = html.index('\nconst orderCountTypes=', start)
    section = html[start:end]
    section = replace_once(section,
        '<button type="button" class="secondary" id="earningsVladAllowance" aria-label="Надбавка Владу">💵 Владу</button>',
        '<button type="button" class="secondary" data-personal-kind="orders">Заказы</button>')
    section = replace_once(section, " showModal('💰 Заработок',", " showModal(kind==='orders'?'📋 Заказы':'💰 Заработок',")
    section = replace_once(section, ' function render(){', ''' function render(){
  qs('#modal .modal-head h3').textContent=kind==='orders'?'📋 Заказы':'💰 Заработок';
  if(kind==='orders'){renderOrders();return}''')
    section = section.replace('/api/personal-tables/${kind}/${month}', '${personalTableEndpoint(kind,month)}')
    section = section.replace('/api/personal-tables/${loadedKind}/${loadedMonth}', '${personalTableEndpoint(loadedKind,loadedMonth)}')
    # Orders use their existing versioned category editor rather than financial PATCHes.
    section = replace_once(section, '  if(busy||!data||!active())return;++request;',
        "  if(busy||!data||!active()||kind==='orders')return;++request;")
    old = ''' qs('#earningsVladAllowance').onclick=()=>{
  if(busy)return;
  openVladAllowance({month,kind,left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop});
 };
'''
    section = replace_once(section, old, '')
    section = replace_once(section, ' async function load(){', r'''
 function personalTableEndpoint(tableKind,tableMonth){
  return tableKind==='orders'?`/api/order-count-table/${tableMonth}`:`/api/personal-tables/${tableKind}/${tableMonth}`;
 }
 function renderOrders(){
  const left=resume?.left??grid.scrollLeft,top=resume?.top??grid.scrollTop,modalTop=resume?.modalTop??qs('#modal').scrollTop;resume=null;
  const columns=['private','gbu','total','staff_count'];
  const countButton=(value,key,day='',period='')=>`<button type="button" class="contact-btn personal-cell${period?' order-period-count':''}" data-order-breakdown-key="${key}" data-order-breakdown-day="${day}" data-order-breakdown-period="${period}" aria-label="${esc(day||period)} · ${{private:'Частные',gbu:'ГБУ',total:'Всего заказов',staff_count:'Сотрудников'}[key]}: ${value}. Открыть расшифровку">${value||(!period&&['private','gbu'].includes(key)?'—':0)}</button>`;
  const totalRow=(label,values,period)=>`<tr class="personal-total"><th>${esc(label)}</th>${columns.map(key=>`<td>${countButton(values[key],key,'',period)}</td>`).join('')}</tr>`;
  let rows='';
  for(const row of data.daily){
   rows+=`<tr><th>${row.day}</th>${columns.map(key=>`<td>${countButton(row[key],key,row.date)}</td>`).join('')}</tr>`;
   if(row.day===15)rows+=totalRow('1–15',data.first,'first');
  }
  rows+=totalRow('16–'+data.days,data.second,'second')+totalRow('Месяц',data.total,'total');
  grid.innerHTML=`<table class="personal-table orders-month-table"><thead><tr><th>День</th><th>Частные<br><small>✍️</small></th><th>ГБУ<br><small>✍️</small></th><th>Всего<br>заказов</th><th>Сотрудников<br><small>по составам</small></th></tr></thead><tbody>${rows}</tbody></table>`;
  status.textContent=data.total.unallocated?`Без категории: ${data.total.unallocated}. Нажмите число за день или в итогах, чтобы открыть расшифровку.`:'Нажмите любое число за день или в итогах — откроется разбор по категориям и составам.';
  fitPersonalFirstHalf(grid);
  grid.scrollLeft=left;grid.scrollTop=openCurrentHalf?legionTableOpeningTop(month,grid):top;openCurrentHalf=false;qs('#modal').scrollTop=modalTop;
  qsa('[data-order-breakdown-key]',grid).forEach(button=>button.onclick=()=>{
   if(busy||!active())return;
   const saved={month,kind:'orders',left:grid.scrollLeft,top:grid.scrollTop,modalTop:qs('#modal').scrollTop};
   const day=button.dataset.orderBreakdownDay,period=button.dataset.orderBreakdownPeriod,key=button.dataset.orderBreakdownKey;
   const values=day?data.daily.find(row=>row.date===day):data[period];
   if(values)openOrderCountBreakdown(values,{month,day,period,source:['private','gbu'].includes(key)?key:'all',days:data.days,returnState:saved});
  });
 }
 async function load(){''')
    html = html[:start] + section + html[end:]

    start = html.index('async function openOrderCategoryCounts(source){')
    end = html.index('\nasync function openOrderCountPeriod()', start)
    section = html[start:end]
    section = replace_once(section,
        'async function openOrderCategoryCounts(source){\n const day=state.selectedDate;',
        'async function openOrderCategoryCounts(source,workDate=state.selectedDate,onReturn=null){\n const day=workDate;')
    section = replace_once(section, "  const form=qs('#categoryCountForm');", r'''
  const form=qs('#categoryCountForm');
  if(onReturn){
   const back=document.createElement('button');back.type='button';back.className='secondary';back.textContent='‹ К таблице заказов';back.style.marginBottom='10px';
   back.onclick=()=>{if(!form.querySelector('button[type="submit"]').disabled)onReturn()};body.prepend(back);
  }''')
    section = replace_once(section,
        "    if(qs('#categoryCountBody')===body)closeModal();\n    await loadBootstrap(state.selectedDate);toast('Количество заказов сохранено');",
        """    if(qs('#categoryCountBody')!==body)return;
    if(onReturn){
     await loadBootstrap(state.selectedDate);
     if(qs('#categoryCountBody')===body){toast('Количество заказов сохранено');await onReturn()}
    }else{closeModal();await loadBootstrap(state.selectedDate);toast('Количество заказов сохранено')}""")
    html = html[:start] + section + html[end:]
    return html.replace('</head>', '''<style id="orders-month-table-style">
#personalGrid .orders-month-table{width:100%;table-layout:fixed}
#personalGrid .orders-month-table thead th{font-size:11px;overflow-wrap:normal}
#personalGrid .orders-month-table thead small{font-size:9px;font-weight:400;color:var(--muted)}
#personalGrid .orders-month-table .personal-total{color:var(--gold);background:var(--legion-color-211d14,var(--panel2))}
#personalGrid .orders-month-table .personal-total th:first-child{background:var(--legion-color-211d14,var(--panel2))}
#personalGrid .orders-month-table tbody td{font-variant-numeric:tabular-nums}
#personalGrid .orders-month-table td:nth-last-child(2){color:var(--gold)}
#personalGrid .orders-month-table td:nth-last-child(2) .personal-cell{color:var(--gold);font-weight:750}
#personalGrid .orders-month-table .order-period-count{background:transparent;color:var(--gold);border-color:transparent;font-weight:750}
#personalStatus{font-size:10px;line-height:1.25}
@media(max-width:350px){#personalGrid .orders-month-table thead th{font-size:9px}}
</style></head>''', 1)


INLINE_INDEX_HTML = _orders_personal_table_html(INLINE_INDEX_HTML)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''
<script>
function orderBreakdownStaff(values){
 return Object.entries(values.counts||{}).reduce((sum,[key,count])=>sum+Number(key.split(':')[1])*Number(count),0);
}
function orderBreakdownRows(values){
 const labels={standard:'Стандарт',vip:'ВИП',elite:'Элит',carryout:'Вынос'},rank={standard:0,vip:1,elite:2,carryout:3};
 const entries=Object.entries(values.counts||{}).filter(([,count])=>Number(count)>0).sort(([a],[b])=>{
  const [ac,as]=a.split(':'),[bc,bs]=b.split(':');return (rank[ac]??9)-(rank[bc]??9)||Number(as)-Number(bs);
 });
 return entries.map(([key,count])=>{
  const [category,size]=key.split(':');
  return `<tr><th scope="row">${esc(labels[category]||category)}<small>${Number(size)} чел. в бригаде</small></th><td>${Number(count)}</td><td>${Number(size)*Number(count)}</td></tr>`;
 }).join('')+(values.unallocated?`<tr><th scope="row">Без категории<small>Состав не указан</small></th><td>${Number(values.unallocated)}</td><td>—</td></tr>`:'');
}
function openOrderCountBreakdown(values,options){
 const {month,day,period,source,days,returnState}=options;
 const selected=source==='all'?['private','gbu']:[source];
 const title=source==='private'?'Частные заказы':source==='gbu'?'Заказы ГБУ':'Все заказы';
 const monthLabel=new Date(month+'-01T12:00:00Z').toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'Europe/Moscow'});
 const range=day?new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'Europe/Moscow'}):`${period==='first'?'1–15':period==='second'?'16–'+days:'Весь месяц'} · ${monthLabel}`;
 const orders=selected.reduce((sum,key)=>sum+Number(values.sources[key].total),0);
 const staff=selected.reduce((sum,key)=>sum+orderBreakdownStaff(values.sources[key]),0);
 const unknown=selected.reduce((sum,key)=>sum+Number(values.sources[key].unallocated||0),0);
 showModal('📋 '+title,`<div id="orderCountBreakdown"><button type="button" class="secondary" id="orderBreakdownBack">‹ К таблице заказов</button><p class="order-breakdown-range">${esc(range)}</p><div class="order-breakdown-summary"><b>Заказов: ${orders}</b><b>Мест в бригадах: ${staff}</b></div>${selected.map(key=>{
  const details=values.sources[key],rows=orderBreakdownRows(details);
  return `<section class="order-breakdown-source"><div class="order-breakdown-heading"><h4>${key==='private'?'Частные':'ГБУ'}</h4>${day?`<button type="button" class="secondary" data-edit-order-breakdown="${key}">✍️ Изменить</button>`:''}</div>${rows?`<table><thead><tr><th>Категория и состав</th><th>Заказов</th><th>Мест</th></tr></thead><tbody>${rows}</tbody><tfoot><tr><th>Итого</th><td>${Number(details.total)}</td><td>${orderBreakdownStaff(details)}</td></tr></tfoot></table>`:'<p class="muted">Заказов нет</p>'}</section>`;
 }).join('')}<p class="order-breakdown-help">Места в бригадах = число заказов × состав. Один сотрудник может участвовать в нескольких заказах.${unknown?' Заказы без категории включены в количество заказов; состав для них пока неизвестен.':''}</p></div>`);
 const root=qs('#orderCountBreakdown'),session=cashModalGeneration;
 const active=()=>session===cashModalGeneration&&qs('#orderCountBreakdown')===root&&!qs('#modal').classList.contains('hidden');
 qs('#modal').scrollTop=0;
 const back=()=>{if(active())return openPersonalTables(returnState)};
 qs('#orderBreakdownBack').onclick=back;
 qsa('[data-edit-order-breakdown]',root).forEach(button=>button.onclick=()=>{
  if(!active())return;
  openOrderCategoryCounts(button.dataset.editOrderBreakdown,day,()=>openPersonalTables(returnState));
 });
}
</script></body>''', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', '''<style id="order-count-breakdown-style">
#modal:has(#orderCountBreakdown)>.modal-head,
#modal:has(#cashDayCheckBody)>.modal-head,
#modal:has(#cashDayEditBody)>.modal-head,
#modal:has(#privateCashBack)>.modal-head,
#modal:has(#gbuEarnBack)>.modal-head,
#modal:has(#categoryCountBody)>.modal-head{position:relative;top:auto;margin:0 0 12px;min-height:44px;gap:10px}
#modal:has(#orderCountBreakdown)>.modal-head::before,
#modal:has(#cashDayCheckBody)>.modal-head::before,
#modal:has(#cashDayEditBody)>.modal-head::before,
#modal:has(#privateCashBack)>.modal-head::before,
#modal:has(#gbuEarnBack)>.modal-head::before,
#modal:has(#categoryCountBody)>.modal-head::before{display:none}
#modal:has(#orderCountBreakdown)>.modal-head h3,
#modal:has(#cashDayCheckBody)>.modal-head h3,
#modal:has(#cashDayEditBody)>.modal-head h3,
#modal:has(#privateCashBack)>.modal-head h3,
#modal:has(#gbuEarnBack)>.modal-head h3,
#modal:has(#categoryCountBody)>.modal-head h3{margin:0;min-width:0;overflow-wrap:anywhere}
#orderCountBreakdown #orderBreakdownBack,
#modal>#privateCashBack,
#modal>#gbuEarnBack,
#categoryCountBody>button.secondary:first-child{display:inline-flex;align-items:center;justify-content:center;position:relative;min-height:44px;max-width:100%;height:auto;margin:0 0 4px;padding:10px 14px;box-sizing:border-box;line-height:1.25;white-space:normal;font-size:12px}
#orderCountBreakdown .order-breakdown-range{font-size:14px;font-weight:650;margin:12px 0}
#orderCountBreakdown .order-breakdown-summary{display:flex;flex-wrap:wrap;gap:8px 16px;color:var(--gold);font-size:14px;padding:10px;background:var(--panel2);border-radius:10px}
#orderCountBreakdown .order-breakdown-source{margin-top:16px;border:1px solid var(--line);border-radius:12px;padding:10px}
#orderCountBreakdown .order-breakdown-heading{display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:8px}
#orderCountBreakdown h4{margin:0;font-size:15px}
#orderCountBreakdown [data-edit-order-breakdown]{min-height:36px;padding:6px 10px;font-size:12px}
#orderCountBreakdown table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
#orderCountBreakdown th,#orderCountBreakdown td{padding:9px 4px;border-bottom:1px solid var(--line);text-align:center}
#orderCountBreakdown th:first-child{text-align:left;width:56%}
#orderCountBreakdown tbody th{font-weight:650}
#orderCountBreakdown th small{display:block;color:var(--muted);font-size:11px;font-weight:400;margin-top:3px}
#orderCountBreakdown thead th{font-size:11px;color:var(--muted)}
#orderCountBreakdown tfoot{color:var(--gold);font-weight:750}
#orderCountBreakdown tfoot th,#orderCountBreakdown tfoot td{border-bottom:0}
#orderCountBreakdown .order-breakdown-help{font-size:11px;line-height:1.4;color:var(--muted);margin:12px 0 0}
</style></head>''', 1)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', '''<style id="gbu-earning-cards-style">
#modal:has(#gbuEarnBody){padding:10px}
#modal:has(#gbuEarnBody)>.modal-head{min-height:36px;margin-bottom:6px;gap:6px}
#modal:has(#gbuEarnBody)>.modal-head h3{font-size:18px;line-height:1.2}
#modal:has(#gbuEarnBody)>.modal-head .close-btn{width:32px;height:32px;min-height:32px}
#modal>#gbuEarnBack{min-height:36px;padding:6px 10px;margin-bottom:2px}
#gbuEarnBody .gbu-earning-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;margin-top:6px;align-items:stretch}
#gbuEarnBody .gbu-earning-card{min-width:0;display:flex;flex-direction:column;gap:4px;padding:6px;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
#gbuEarnBody .gbu-earning-card>b{display:block;min-height:2.4em;font-size:12px;line-height:1.2;overflow-wrap:anywhere}
#gbuEarnBody .gbu-card-controls{display:grid;grid-template-columns:minmax(0,1fr) 42px;gap:4px;align-items:end}
#gbuEarnBody .gbu-card-controls label{display:flex;flex-direction:column;min-width:0;gap:2px;font-size:10px;color:var(--muted);line-height:1.15}
#gbuEarnBody .gbu-card-type-label{grid-column:1/-1}
#gbuEarnBody [data-gbu-type],#gbuEarnBody [data-gbu-money]{display:block;box-sizing:border-box;width:100%;max-width:100%;min-width:0;min-height:36px;margin:0;padding:5px 4px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--text);font-size:16px}
#gbuEarnBody [data-gbu-auto]{min-width:0;min-height:36px;padding:4px 2px;font-size:11px;border-radius:6px;box-sizing:border-box}
#gbuEarnBody [data-gbu-hint]{display:block;font-size:10px;line-height:1.2;overflow-wrap:anywhere}
#gbuEarnBody .gbu-earning-empty{grid-column:1/-1}
#gbuEarnForm>h3{font-size:14px;line-height:1.2;overflow-wrap:anywhere;margin:6px 0}
#gbuEarnForm>p.muted{font-size:11px;line-height:1.25;margin:4px 0}
#gbuEarnForm>button[type="submit"]{min-height:38px;padding:8px 12px;font-size:13px}
</style></head>''', 1)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', '''<style id="daily-cash-cards-style">
#modal .cash-day-check-toolbar{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:0 0 12px}
#modal .cash-day-check-toolbar>button{display:flex;align-items:center;justify-content:center;min-width:0;min-height:44px;padding:8px 5px;font-size:12px;line-height:1.25;white-space:normal}
#modal>#cashDayEditBack{display:inline-flex;align-items:center;min-height:44px;max-width:100%;padding:10px 14px;line-height:1.25;white-space:normal}
#cashDayCheckBody{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;align-items:stretch}
#cashDayCheckBody.empty{display:block}
#cashDayCheckBody>.attention,#cashDayCheckBody>.empty{grid-column:1/-1;margin-bottom:0}
#cashDayCheckBody>.employee-row{display:grid;grid-template-columns:minmax(0,1fr) 24px;align-items:center;gap:6px;min-width:0;padding:10px 8px;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
#cashDayCheckBody .employee-row .name{min-width:0}
#cashDayCheckBody .employee-row .name b{font-size:12px;line-height:1.3;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
#cashDayCheckBody .employee-row .name small{display:block;font-size:10px;line-height:1.35;margin-top:5px;white-space:normal;overflow-wrap:anywhere}
#cashDayCheckBody .employee-row>span{justify-self:end}
#cashDayCheckBody>.attention h4{font-size:14px;line-height:1.3}
#cashDayCheckBody>.attention p{font-size:11px;line-height:1.35}
</style></head>''', 1)

INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</head>', '''<style id="cash-notes-toolbar-style">
#modal.owner-cash-modal.cash-reconciliation-open>.modal-head{position:relative;top:auto;grid-template-columns:auto minmax(0,1fr) auto 30px;min-height:32px;margin:0 0 6px!important;padding:0;gap:5px}
#modal.owner-cash-modal.cash-reconciliation-open>.modal-head::before{display:none}
#modal.cash-reconciliation-open>.modal-head h3{margin:0;font-size:16px}
#modal.cash-reconciliation-open>.modal-head .cash-reconciliation-toolbar>span{display:none}
#modal.cash-reconciliation-open>.modal-head .cash-reconciliation-toolbar>strong{min-width:0;font-size:10px;line-height:1.2;white-space:normal;text-align:center}
#modal.owner-cash-modal .owner-cash-controls{display:grid;grid-template-columns:28px 28px minmax(0,1fr) 28px;gap:4px;align-items:center;min-height:32px;margin-bottom:5px!important}
#modal.owner-cash-modal .owner-cash-controls>button{box-sizing:border-box;min-width:0;width:100%;min-height:32px;padding:4px;text-align:center}
#modal.owner-cash-modal .owner-cash-controls>label{display:block;min-width:0;width:100%;overflow:hidden;margin:0}
#modal.owner-cash-modal #cashMonth{box-sizing:border-box;display:block;width:100%;max-width:100%;min-width:0;min-height:32px;height:32px;margin:0;padding:3px 5px;font-size:14px}
#modal .cash-notes-send-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:2px 0 5px;flex-shrink:0}
#modal .cash-notes-send-row>span{font-size:10px;color:var(--muted)}
#modal #cashNotesSend{min-height:32px;max-width:100%;padding:5px 9px;font-size:11px;line-height:1.2;white-space:nowrap;border-radius:8px}
#modal #cashNotesSend:disabled{opacity:.5}
#modal #cashNotesSendStatus{flex-shrink:0;margin:0 0 6px;font-size:10px;line-height:1.3;white-space:pre-line;overflow-wrap:anywhere}
#modal #cashNotesSendStatus[hidden]{display:none}
</style></head>''', 1)


# Keep these scoped overrides after the base owner-cash styles in the document.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="cash-compact-controls-style">
#modal.owner-cash-modal.cash-reconciliation-open>.modal-head{margin:0 0 3px!important;min-height:30px}
#modal.owner-cash-modal.cash-reconciliation-open .owner-cash-controls{display:grid;grid-template-columns:28px 28px minmax(0,1fr) 28px;gap:4px;min-height:28px;margin:0 0 3px!important}
#modal.owner-cash-modal.cash-reconciliation-open .owner-cash-controls>button{width:28px;min-width:28px;height:28px;min-height:28px;padding:2px}
#modal.owner-cash-modal.cash-reconciliation-open .owner-cash-controls>label{width:100%;min-width:0;margin:0}
#modal.owner-cash-modal.cash-reconciliation-open #cashMonth{height:28px;min-height:28px;padding:2px 5px;font-size:13px}
#modal.owner-cash-modal.cash-reconciliation-open .owner-cash-ranges{gap:4px;margin:0 0 3px!important}
#modal.owner-cash-modal.cash-reconciliation-open button[data-cash-range]{min-height:26px;padding:3px 6px}
#modal.owner-cash-modal #cashMonthTools{min-width:0}
#modal.owner-cash-modal.cash-reconciliation-open #cashMonthTools{display:flex;align-items:center;gap:5px;margin:0 0 4px;min-width:0;flex-shrink:0}
#modal.owner-cash-modal.cash-reconciliation-open #cashMonthTabs{flex:1;min-width:0;margin:0!important;gap:4px}
#modal.owner-cash-modal.cash-reconciliation-open #cashMonthTabs button{min-height:26px;padding:3px 5px;font-size:10px}
#modal.owner-cash-modal.cash-reconciliation-open .cash-notes-send-row{margin:0;gap:3px;flex-shrink:0}
#modal.owner-cash-modal.cash-reconciliation-open .cash-notes-send-row>span{font-size:9px;white-space:nowrap}
#modal.owner-cash-modal.cash-reconciliation-open #cashNotesSend{min-height:28px;padding:4px 6px;font-size:10px}
#modal.owner-cash-modal.cash-reconciliation-open .cash-reconciliation-grid{gap:4px}
#modal.owner-cash-modal.cash-reconciliation-open .cash-reconciliation-card{padding:3px}
#modal.owner-cash-modal.cash-reconciliation-open .cash-reconciliation-total{margin-top:2px}
#modal.owner-cash-modal.cash-reconciliation-open .cash-reconciliation-legend{margin:3px 0 0;font-size:9px}
@media(max-width:359px){
 #modal.owner-cash-modal.cash-reconciliation-open .cash-notes-send-row>span{display:none}
}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="contact-orders-compact-style">
#contactOrdersBody .attention{padding:10px;margin:0 0 8px;border-radius:12px}
#contactOrdersBody .attention>div{flex-wrap:wrap;gap:5px!important}
#contactOrdersBody .attention h4{margin:0;font-size:14px;line-height:1.25}
#contactOrdersBody .attention p{margin:5px 0 0;font-size:11px;line-height:1.3}
#contactOrdersBody #addBrigadeContact{min-height:32px;padding:5px 8px;font-size:12px}
#modal .brigade-control-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;border:0;border-radius:0;background:transparent;overflow:visible}
#modal .brigade-control-grid>.employee-row{display:flex;flex-direction:column;align-items:stretch;justify-content:space-between;gap:6px;min-width:0;padding:7px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
#modal .brigade-control-grid .name{min-width:0;width:100%}
#modal .brigade-control-grid .name b{display:block;font-size:12px;line-height:1.25;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere}
#modal .brigade-control-grid .name small{display:block;margin-top:3px;font-size:10px;line-height:1.25;white-space:normal;overflow-wrap:anywhere}
#modal .brigade-control-grid>.employee-row>div:last-child{justify-content:flex-end;gap:5px!important}
#modal .brigade-control-grid>.employee-row>div:last-child b{font-size:14px;line-height:1.2}
#modal .brigade-control-grid>.employee-row>div:last-child>span{font-size:18px;line-height:1.2}
#modal .brigade-control-grid>.empty{grid-column:1/-1;padding:12px;font-size:12px}

#photoControlHint{font-size:11px;line-height:1.3;margin:4px 0 8px}
#modal:has(#photoControlList)>.modal-body>p:first-child{font-size:11px;margin:0 0 4px}
#modal .photo-control-grid>.employee-row>div:last-child{flex-wrap:wrap;align-items:flex-end!important}
#modal .photo-control-grid .photo-mark{gap:2px}
#modal .photo-control-grid .photo-mark-toggle{width:32px;min-width:32px;height:32px;min-height:32px;padding:2px;font-size:19px}
#modal .photo-control-grid .photo-mark-label{font-size:9px;line-height:1.2}
#modal .photo-control-grid .photo-mark-auto{min-height:28px;padding:3px;font-size:10px}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="brigade-control-card-lines-style">
#modal .brigade-control-grid .name{display:grid;gap:2px}
#modal .brigade-control-grid .brigade-control-metro{display:block;font-size:11px;line-height:1.25;white-space:normal;overflow-wrap:anywhere}
#modal .brigade-control-grid .name small{margin-top:0}
</style><script>
function brigadeControlEmployeeName(employee){
 return employeeListName(employee||{}).replace(/([0-9—])\s*см\s*$/i,'$1');
}
function brigadeControlCardHeading(item,index,source=item.source){
 const parts=String(brigadeControlEmployeeName(item.employee||{})).trim().split(/\s+/);
 const name=parts.shift()||'—',details=parts.join(' ')||'—';
 return `<div class="name"><b>${index+1}. ${esc(name)}</b><span class="brigade-control-metro">${esc(details)}</span><small>${source==='gbu'?'ГБУ':'Частный'} · ${esc(item.surname||'—')}</small></div>`;
}
</script></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="brigade-control-top-marks-style">
#modal .brigade-control-grid>.employee-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:2px 5px;padding:6px}
#modal .brigade-control-grid>.employee-row>.name{display:contents}
#modal .brigade-control-grid>.employee-row>.name>b{grid-column:1;grid-row:1}
#modal .brigade-control-grid>.employee-row>.name>.brigade-control-metro{grid-column:1;grid-row:2}
#modal .brigade-control-grid>.employee-row>.name>small{grid-column:1/-1;grid-row:3;margin:0}
#modal .brigade-control-grid>.employee-row>div:last-child{grid-column:2;grid-row:1/3;display:flex!important;flex-direction:column-reverse;align-items:center!important;justify-content:flex-end;gap:2px!important;min-width:0}
#modal .brigade-control-grid>.employee-row>div:last-child>b{font-size:12px;line-height:1.2;white-space:nowrap}
#modal .brigade-control-grid>.employee-row>div:last-child>span{font-size:20px;line-height:1.1}
#modal .photo-control-grid>.employee-row>div:last-child{display:grid!important;grid-auto-flow:column;grid-template-rows:auto auto;align-items:start!important;gap:2px 3px!important}
#modal .photo-control-grid>.employee-row>div:last-child>b{grid-row:2;grid-column:1/-1;text-align:center}
#modal .photo-control-grid .photo-mark{grid-row:1;align-self:start}
#modal .photo-control-grid .photo-mark-toggle{width:28px;min-width:28px;height:28px;min-height:28px;padding:1px;font-size:19px}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="gbu-photo-card-top-time-style">
#modal .photo-control-grid.is-gbu>.employee-row{position:relative;display:flex;flex-direction:column;align-items:stretch;gap:4px}
#modal .photo-control-grid.is-gbu>.employee-row>.name{display:grid;gap:2px}
#modal .photo-control-grid.is-gbu>.employee-row>.name>b{padding-right:42px}
#modal .photo-control-grid.is-gbu>.employee-row>div:last-child{display:flex!important;flex-direction:row;flex-wrap:wrap;align-items:flex-start!important;justify-content:center;gap:4px 8px!important;grid-column:auto;grid-row:auto;width:100%}
#modal .photo-control-grid.is-gbu>.employee-row>div:last-child>b{position:absolute;right:6px;top:6px;margin:0;font-size:12px;line-height:1.25;white-space:nowrap}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="gbu-photo-marks-under-time-style">
#modal .photo-control-grid.is-gbu>.employee-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;gap:2px 5px}
#modal .photo-control-grid.is-gbu>.employee-row>.name{display:contents}
#modal .photo-control-grid.is-gbu>.employee-row>.name>b{padding-right:0}
#modal .photo-control-grid.is-gbu>.employee-row>div:last-child{grid-column:2;grid-row:1/3;display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr));grid-template-rows:auto auto;grid-auto-flow:row;align-items:start!important;justify-content:normal;width:auto;gap:2px 3px!important}
#modal .photo-control-grid.is-gbu>.employee-row>div:last-child>b{position:static;grid-column:1/-1;grid-row:1;text-align:center;margin:0}
#modal .photo-control-grid.is-gbu .photo-mark{grid-row:2}
#modal .photo-control-grid.is-gbu .photo-mark-label{white-space:nowrap;font-size:8px}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="gbu-stage-photo-counts-style">
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:5px;padding:8px}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line{display:flex;align-items:baseline;justify-content:center;gap:6px;margin:0;white-space:nowrap;font-size:12px;line-height:1.25}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line>span{margin:0;font-size:11px;line-height:1.25}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line>b{font-size:18px;line-height:1.2}
</style></body>''', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', r'''<style id="gbu-dashboard-photo-counts-style">
#gbuPhotoControl{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:2px;padding:4px 6px}
#gbuPhotoControl .gbu-dashboard-title{display:block;margin:0;font-size:10px;font-weight:700;line-height:1.15;color:var(--text);white-space:nowrap}
#gbuPhotoControl .gbu-dashboard-photo-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin:0;overflow:visible}
#gbuPhotoControl .gbu-dashboard-photo-stat{display:flex;flex-direction:column;align-items:center;gap:1px;margin:0;min-width:0;overflow:visible}
#gbuPhotoControl .gbu-dashboard-photo-stat>b{font-size:16px;line-height:1.15;color:var(--blue);font-variant-numeric:tabular-nums;white-space:nowrap}
#gbuPhotoControl .gbu-dashboard-photo-stat>span{margin:0;font-size:9px;line-height:1.15;white-space:nowrap}
</style></body>''', 1)


def _balanced_single_screen_orders_html(html):
    """Compact the existing dashboard and fit uniformly, preserving every action."""
    start = html.index('function fitOrdersScreen(){')
    end = html.index('function scheduleOrdersScreenFit(){', start)
    html = html[:start] + r'''function fitOrdersScreen(){
 const main=qs('main');if(!main)return;
 main.style.removeProperty('transform');
 main.style.removeProperty('width');
 main.style.removeProperty('--orders-group-extra');
 if(!document.body.classList.contains('dashboard-compact'))return;
 const viewport=window.visualViewport;
 const viewportBottom=viewport?viewport.offsetTop+viewport.height:window.innerHeight;
 const nav=qs('#roleNavigation')||qs('.bottom-nav');
 const navTop=nav?.getBoundingClientRect().top;
 const bottom=navTop>0?Math.min(navTop,viewportBottom):viewportBottom-72;
 const available=Math.max(1,bottom-main.getBoundingClientRect().top-6);
 let scale=1;
 // Compact CSS normally fits at full size. Small windows get uniform scaling:
 // expand layout width by the inverse factor so its visible width still fills
 // the screen. Re-measure wrapping after each step; never distort text in Y.
 for(let attempt=0;attempt<8;attempt++){
  const height=Math.max(main.scrollHeight,main.offsetHeight)*scale;
  if(height<=available||!height)break;
  scale*=available/height*.995;
  main.style.width=`${100/scale}%`;
  main.style.transform=`scale(${scale})`;
 }
}
''' + html[end:]
    return html.replace('</body>', r'''<style id="balanced-single-screen-orders-style">
body.dashboard-compact main{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;letter-spacing:normal;line-height:1.25;transform-origin:top left}
body.dashboard-compact button{font-family:inherit;letter-spacing:normal;font-stretch:normal}
body.dashboard-compact .topbar{padding:6px 10px 5px}
body.dashboard-compact .topbar h1{font-size:21px;font-weight:700;letter-spacing:normal}
body.dashboard-compact .topbar .icon-btn{width:36px;height:36px;border-radius:11px;font-size:20px}
body.dashboard-compact #dateStrip{padding:3px 10px 6px;gap:5px}
body.dashboard-compact #dateStrip .date-pill{min-width:64px;min-height:54px;padding:6px 5px;border-radius:10px}
body.dashboard-compact #dateStrip .date-pill strong{font-size:12px;line-height:1.25;font-weight:650}
body.dashboard-compact #dateStrip .date-pill small{font-size:10px;line-height:1.2}
body.dashboard-compact #selectedDayHeader{margin:0 0 5px;gap:6px}
body.dashboard-compact #selectedDayHeader h2{font-size:17px}
body.dashboard-compact .dashboard-orders-heading{letter-spacing:normal;font-weight:700}
body.dashboard-compact .dashboard-date-picker{min-height:29px;padding:5px 7px;background:var(--panel);border-radius:9px}
body.dashboard-compact .dashboard-date-value{font-size:10px}
body.dashboard-compact #summary{gap:5px;margin:0 0 5px}
body.dashboard-compact #summary .metric{min-height:66px;padding:6px 3px;border-radius:11px}
body.dashboard-compact #summary .metric b{font-size:19px!important;line-height:1.15;font-weight:700}
body.dashboard-compact #summary .metric b:nth-of-type(2){font-size:12px!important;margin-top:4px!important}
body.dashboard-compact #summary .metric span{font-size:9px;line-height:1.2;margin-top:3px}
body.dashboard-compact #dayCommentBtn{min-height:24px;padding:3px 4px;margin-top:4px;font-size:9px;border-radius:7px}
body.dashboard-compact #brigadeWork{margin:5px 0!important;padding-top:5px}
body.dashboard-compact #brigadeWork .dashboard-section-heading{margin-bottom:5px;gap:5px;flex-wrap:wrap}
body.dashboard-compact #brigadeWork .dashboard-section-heading h2{font-size:14px;font-weight:700;flex:1 1 auto}
body.dashboard-compact #brigadeWork .dashboard-section-date{font-size:9px;margin-left:auto}
body.dashboard-compact #brigadeWork .summary-grid{gap:5px}
body.dashboard-compact #brigadeWork .metric{min-height:53px;padding:5px 4px;border-radius:10px}
body.dashboard-compact #brigadeWork .metric b{font-size:19px;line-height:1.15;font-weight:700}
body.dashboard-compact #brigadeWork .metric span{font-size:9px;line-height:1.2}
body.dashboard-compact #gbuPhotoControl{gap:2px;padding:4px 5px}
body.dashboard-compact #gbuPhotoControl .gbu-dashboard-title{font-size:9px;margin:0}
body.dashboard-compact #gbuPhotoControl .gbu-dashboard-photo-stat>b{font-size:16px}
body.dashboard-compact #gbuPhotoControl .gbu-dashboard-photo-stat>span{font-size:9px;margin:1px 0 0}
body.dashboard-compact #employeeReadiness>.section-head{margin:5px 0}
body.dashboard-compact #employeeReadiness .readiness-section-heading{margin:0 0 4px;gap:4px}
body.dashboard-compact #employeeReadiness .readiness-section-heading h2{font-size:14px;line-height:1.2;font-weight:700;white-space:normal;flex:1 1 auto}
body.dashboard-compact #employeeReadiness .readiness-section-heading .dashboard-section-date{font-size:9px}
body.dashboard-compact #employeeReadiness .readiness-actions{gap:5px;grid-template-columns:minmax(0,1.65fr) 38px minmax(0,1.15fr) 40px}
body.dashboard-compact #employeeReadiness .readiness-actions>button{min-height:36px;padding:4px 3px;border-radius:10px}
body.dashboard-compact #employeeReadiness #readinessHistoryBtn b{font-size:11px;line-height:1.15;font-weight:700}
body.dashboard-compact #employeeReadiness #readinessHistoryBtn small{font-size:9px!important;line-height:1.15}
body.dashboard-compact #employeeReadiness #readinessAvralBtn{font-size:11px}
body.dashboard-compact #readinessSummary{gap:5px;margin:5px 0}
body.dashboard-compact #readinessSummary .metric{min-height:41px;padding:5px 9px;border-radius:10px}
body.dashboard-compact #readinessSummary .metric b{font-size:18px;line-height:1.15;font-weight:700}
body.dashboard-compact #readinessSummary .metric span{font-size:10px;line-height:1.2;margin-top:2px}
body.dashboard-compact #readinessGroups .group-block{min-height:0;margin-bottom:5px;border-radius:12px}
body.dashboard-compact #readinessGroups .group-title{padding:6px 9px 4px;font-size:14px;line-height:1.2;font-weight:700}
body.dashboard-compact #readinessGroups .group-stats{gap:4px;padding:0 6px 6px}
body.dashboard-compact #readinessGroups .group-stats button{min-width:0;min-height:33px;padding:3px 2px;border-radius:7px;font-size:9px;line-height:1.2}
body.dashboard-compact #readinessGroups .group-stats b{display:block;font-size:16px;line-height:1.15;font-weight:650;margin-bottom:1px}
body.dashboard-compact button:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
@media(max-width:350px){body.dashboard-compact #dayCommentBtn{font-size:8px;padding:3px 2px}body.dashboard-compact #brigadeWork .metric b{font-size:18px}}
</style></body>''', 1)


INLINE_INDEX_HTML = _balanced_single_screen_orders_html(INLINE_INDEX_HTML)


# Isolated UI preview: the same rendered interface, fictional fixtures, no API calls.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="control-report-gallery-style">\n.bc-photo-viewer{position:fixed;inset:0;box-sizing:border-box;width:min(94vw,480px);height:min(680px,calc(100dvh - 24px));max-width:94vw;max-height:calc(100dvh - 24px);margin:auto;border:1px solid var(--gold2);border-radius:24px;padding:0;background:var(--panel);color:var(--text);z-index:1000;overflow:hidden;overscroll-behavior:contain}\n.bc-photo-viewer::backdrop{background:#0009;backdrop-filter:blur(7px);-webkit-backdrop-filter:blur(7px)}\n.bc-expanded-card{box-sizing:border-box;height:100%;padding:13px;display:grid;grid-template-rows:auto minmax(100px,1fr) auto;gap:10px}\n.bc-photo-viewer header{display:flex;align-items:center;justify-content:space-between;gap:8px}\n.bc-viewer-kind{font-size:13px;font-weight:700;color:var(--gold);background:var(--panel2);border-radius:8px;padding:5px 9px}\n.bc-photo-viewer button{min-width:44px;min-height:44px;border:1px solid var(--line);border-radius:50%;background:var(--panel2);color:var(--text);font-size:26px;line-height:1}\n.bc-photo-viewer button:disabled{opacity:.35}\n.bc-viewer-photo{position:relative;min-height:0;background:#171717;border-radius:14px;overflow:hidden}\n.bc-viewer-stage{position:absolute;inset:0;display:grid;place-items:center;min-width:0;min-height:0;overflow:hidden;color:#eee;touch-action:pan-y pinch-zoom;text-align:center}\n.bc-viewer-stage img{position:absolute;inset:0;display:block;width:100%;height:100%;min-width:0;min-height:0;max-width:none;max-height:none;object-fit:contain;object-position:center;user-select:none}\n.bc-viewer-photo>button{position:absolute;top:50%;transform:translateY(-50%);background:#0009;color:white;border-color:#ffffff55;z-index:1}\n.bc-viewer-photo>button[data-photo-prev]{left:7px}.bc-viewer-photo>button[data-photo-next]{right:7px}\n.bc-viewer-status{position:absolute;bottom:7px;left:50%;transform:translateX(-50%);padding:4px 10px;border-radius:20px;background:#000a;color:white;font-size:12px;pointer-events:none}\n.bc-viewer-info{min-width:0}.bc-viewer-info>small{display:block;color:var(--muted);font-size:11px;margin:0 0 3px}\n.bc-viewer-info h2{margin:0 0 8px;font-size:23px;line-height:1.1;overflow-wrap:anywhere}\n.bc-viewer-brigadier{margin:0 0 9px;font-size:16px;font-weight:650;line-height:1.2;overflow-wrap:anywhere}\n.bc-viewer-rows{margin:0}.bc-viewer-rows>div{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 1px;border-top:1px solid var(--line);font-size:14px;min-height:37px;box-sizing:border-box}\n.bc-viewer-rows dt,.bc-viewer-rows dd{display:flex;align-items:center;gap:6px;margin:0}\n.bc-viewer-rows dd{text-align:right;font-variant-numeric:tabular-nums;font-weight:650}.bc-viewer-rows dd.is-done{color:var(--green)}\n.bc-viewer-rows .cabinet-icon{width:17px;height:17px;color:var(--gold)}\n.bc-viewer-rows .bc-photo-marks{display:inline-flex;gap:3px}.bc-viewer-rows .bc-photo-mark{font-size:16px}\nbody.bc-viewer-open{overflow:hidden!important}\n@media(max-height:590px){.bc-photo-viewer{height:calc(100dvh - 16px);max-height:calc(100dvh - 16px);border-radius:18px}.bc-expanded-card{padding:10px;gap:6px}.bc-viewer-info h2{font-size:19px;margin-bottom:5px}.bc-viewer-brigadier{font-size:14px;margin-bottom:5px}.bc-viewer-rows>div{padding:6px 1px;min-height:32px;font-size:13px}}\n#ownerBrigadeControlContent .bc-order>summary:focus:not(:focus-visible){outline:none;box-shadow:none}\n#ownerBrigadeControlContent .bc-order>summary:focus-visible{outline:2px solid var(--gold);outline-offset:2px;border-radius:7px}\n#ownerBrigadeControlContent .bc-photo-mark.is-received,.bc-viewer-rows .bc-photo-mark.is-received{color:var(--green)}\n#ownerBrigadeControlContent .bc-photo-mark.is-waiting,.bc-viewer-rows .bc-photo-mark.is-waiting{color:var(--muted)}\n#ownerBrigadeControlContent .bc-photo-marks,.bc-viewer-rows .bc-photo-marks{display:inline-flex;align-items:center;gap:3px}\n.bc-viewer-rows dd .cabinet-icon{color:inherit}\n\n.bc-photo-viewer[open]{animation:bc-card-in 180ms cubic-bezier(.2,.7,.2,1) both;will-change:transform,opacity}\n.bc-photo-viewer[open]::backdrop{animation:bc-backdrop-in 180ms ease-out both}\n.bc-photo-viewer.is-closing[open]{animation:bc-card-out 150ms ease-in both;pointer-events:none}\n.bc-photo-viewer.is-closing[open]::backdrop{animation:bc-backdrop-out 150ms ease-in both}\n@keyframes bc-card-in{from{opacity:0;transform:translateY(8px) scale(.975)}to{opacity:1;transform:translateY(0) scale(1)}}\n@keyframes bc-card-out{from{opacity:1;transform:translateY(0) scale(1)}to{opacity:0;transform:translateY(6px) scale(.98)}}\n@keyframes bc-backdrop-in{from{opacity:0}to{opacity:1}}\n@keyframes bc-backdrop-out{from{opacity:1}to{opacity:0}}\n@media(prefers-reduced-motion:reduce){.bc-photo-viewer[open],.bc-photo-viewer[open]::backdrop{animation:none!important;will-change:auto}}\n.bc-viewer-photo>button[data-photo-original]{top:7px;right:7px;transform:none;border-radius:8px;padding:6px 9px;min-height:32px;font-size:12px}\n.bc-viewer-photo>button[data-photo-original][hidden]{display:none}\n</style>\n<script>\nconst controlReportPhotoCache=new Map(),controlReportPhotoPending=new Map();\nasync function controlReportImage(card,ref){\n const key=[state.bootstrap?.user?.id,card.id,ref.stage,ref.revision].join(\':\');\n if(controlReportPhotoCache.has(key))return controlReportPhotoCache.get(key);\n if(!controlReportPhotoPending.has(key)){\n  const request=api(`/api/owner/order-report-media/${card.id}/${ref.stage}`,{cache:\'no-store\'}).then(result=>{\n   if(!/^data:image\\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(result.image||\'\'))throw new Error(\'Не удалось прочитать фото\');\n   controlReportPhotoCache.set(key,result.image);\n   while(controlReportPhotoCache.size>8)controlReportPhotoCache.delete(controlReportPhotoCache.keys().next().value);\n   return result.image;\n  }).finally(()=>controlReportPhotoPending.delete(key));\n  controlReportPhotoPending.set(key,request);\n }\n return controlReportPhotoPending.get(key);\n}\n// Conservative, vertical-only framing: original files are never modified.\nfunction controlSafePeopleCrop(poses,detections,width,height){\n if(!Array.isArray(poses)||!Array.isArray(detections)||!width||!height)return null;\n const people=detections.filter(d=>(d.categories||[]).some(c=>c.categoryName===\'person\'&&c.score>=.35));\n if(people.length<2||people.length>10||poses.length!==people.length)return null;\n const critical=[0,7,8,11,12,23,24,25,26,27,28,29,30,31,32];\n let top=1,bottom=0;const matched=new Set(),heights=[];\n for(const pose of poses){\n  if(!Array.isArray(pose)||pose.length<33)return null;\n  if(critical.some(i=>{const p=pose[i];return !p||!Number.isFinite(p.x)||!Number.isFinite(p.y)||p.x<.005||p.x>.995||p.y<.005||p.y>.995||!(p.visibility>=.65)||!(p.presence>=.65)}))return null;\n  const head=Math.min(...[0,7,8].map(i=>pose[i].y));\n  const feet=Math.max(...[27,28,29,30,31,32].map(i=>pose[i].y));\n  const bodyHeight=feet-head;\n  if(bodyHeight<.18||bodyHeight>.95||pose[23].y<=head||pose[23].y>=feet)return null;\n  heights.push(bodyHeight);\n  // Match every pose to exactly one independently detected person.\n  const hipX=(pose[23].x+pose[24].x)*width/2,hipY=(pose[23].y+pose[24].y)*height/2;\n  const matches=people.map((d,i)=>({b:d.boundingBox,i})).filter(({b,i})=>b&&!matched.has(i)&&hipX>=b.originX&&hipX<=b.originX+b.width&&hipY>=b.originY&&hipY<=b.originY+b.height);\n  if(matches.length!==1)return null;\n  const {b,i}=matches[0];matched.add(i);\n  if(![b.originX,b.originY,b.width,b.height].every(Number.isFinite)||b.width<=0||b.height<=0)return null;\n  top=Math.min(top,head-bodyHeight*.16,b.originY/height-bodyHeight*.08);\n  bottom=Math.max(bottom,feet+bodyHeight*.10,(b.originY+b.height)/height+bodyHeight*.06);\n }\n // Different depths, occlusion, clipped feet or inconsistent counts => full frame.\n if(Math.max(...heights)/Math.min(...heights)>1.65||matched.size!==people.length)return null;\n top=Math.max(0,top);bottom=Math.min(1,bottom);\n if(bottom-top<.3||bottom-top>.92)return null;\n // Keep the entire horizontal frame to protect peripheral people and hands.\n return {top,bottom};\n}\nfunction controlPeopleCropWorker(){\n let models=null,queue=Promise.resolve();\n async function getModels(){\n  if(!models)models=(async()=>{\n   const vision=await import(\'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21/vision_bundle.mjs\');\n   const files=await vision.FilesetResolver.forVisionTasks(\'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.21/wasm\');\n   const pose=await vision.PoseLandmarker.createFromOptions(files,{baseOptions:{modelAssetPath:\'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task\',delegate:\'CPU\'},runningMode:\'IMAGE\',numPoses:12,minPoseDetectionConfidence:.65,minPosePresenceConfidence:.65,outputSegmentationMasks:false});\n   const objects=await vision.ObjectDetector.createFromOptions(files,{baseOptions:{modelAssetPath:\'https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite\',delegate:\'CPU\'},runningMode:\'IMAGE\',categoryAllowlist:[\'person\'],scoreThreshold:.35,maxResults:16});\n   return {pose,objects};\n  })();\n  return models;\n }\n self.onmessage=event=>{\n  const {id,image}=event.data;\n  queue=queue.then(async()=>{\n   try{const {pose,objects}=await getModels();const a=pose.detect(image),b=objects.detect(image);\n    self.postMessage({id,crop:controlSafePeopleCrop(a.landmarks,b.detections,image.width,image.height)});\n   }catch{self.postMessage({id,crop:null})}finally{image?.close()}\n  }).catch(()=>{});\n };\n}\nlet controlCropWorker=null,controlCropId=0,controlCropDisabled=false,controlCropIdle=null;\nconst controlCropJobs=new Map(),controlCropCache=new Map(),controlCropPending=new Map();\nfunction controlStopCropWorker(){\n clearTimeout(controlCropIdle);controlCropWorker?.terminate();controlCropWorker=null;\n for(const job of controlCropJobs.values()){clearTimeout(job.timer);job.resolve(null)}controlCropJobs.clear();\n}\nfunction controlGetCropWorker(){\n if(controlCropDisabled||typeof Worker===\'undefined\'||typeof createImageBitmap===\'undefined\')return null;\n if(!controlCropWorker){\n  const code=controlSafePeopleCrop.toString()+\'\\n(\'+controlPeopleCropWorker.toString()+\')();\';\n  const url=URL.createObjectURL(new Blob([code],{type:\'text/javascript\'}));\n  try{controlCropWorker=new Worker(url)}catch{controlCropDisabled=true;return null}finally{URL.revokeObjectURL(url)}\n  controlCropWorker.onmessage=event=>{\n   const job=controlCropJobs.get(event.data.id);if(!job)return;\n   clearTimeout(job.timer);controlCropJobs.delete(event.data.id);job.resolve(event.data.crop||null);\n   if(!controlCropJobs.size)controlCropIdle=setTimeout(controlStopCropWorker,90000);\n  };\n  controlCropWorker.onerror=()=>{controlCropDisabled=true;controlStopCropWorker()};\n }\n clearTimeout(controlCropIdle);return controlCropWorker;\n}\nasync function controlAutoCropImage(card,ref,img,src){\n if(ref.manual)return null;\n const key=[state.bootstrap?.user?.id,card.id,ref.stage,ref.revision].join(\':\');\n if(controlCropCache.has(key))return controlCropCache.get(key);\n if(controlCropPending.has(key))return controlCropPending.get(key);\n const work=(async()=>{\n  try{\n   const worker=controlGetCropWorker();if(!worker)return null;\n   const w=img.naturalWidth,h=img.naturalHeight;if(!w||!h)return null;\n   const scale=Math.min(1,1280/Math.max(w,h));\n   const bitmap=await createImageBitmap(img,{resizeWidth:Math.max(1,Math.round(w*scale)),resizeHeight:Math.max(1,Math.round(h*scale)),resizeQuality:\'high\'});\n   const crop=await new Promise(resolve=>{\n    const id=++controlCropId;\n    const timer=setTimeout(()=>{controlCropDisabled=true;controlStopCropWorker()},30000);\n    controlCropJobs.set(id,{resolve,timer});\n    try{worker.postMessage({id,image:bitmap},[bitmap])}catch{bitmap.close();clearTimeout(timer);controlCropJobs.delete(id);resolve(null)}\n   });\n   if(!crop||!Number.isFinite(crop.top)||!Number.isFinite(crop.bottom)||crop.top<0||crop.bottom>1||crop.bottom<=crop.top)return null;\n   const y=Math.floor(crop.top*h),bottom=Math.ceil(crop.bottom*h),ch=bottom-y;\n   const canvas=document.createElement(\'canvas\'),outScale=Math.min(1,1600/Math.max(w,ch));\n   canvas.width=Math.max(1,Math.round(w*outScale));canvas.height=Math.max(1,Math.round(ch*outScale));\n   const ctx=canvas.getContext(\'2d\');if(!ctx)return null;\n   ctx.drawImage(img,0,y,w,ch,0,0,canvas.width,canvas.height);\n   return canvas.toDataURL(\'image/jpeg\',.94);\n  }catch{return null}\n })().then(result=>{controlCropCache.set(key,result);while(controlCropCache.size>8)controlCropCache.delete(controlCropCache.keys().next().value);return result}).finally(()=>controlCropPending.delete(key));\n controlCropPending.set(key,work);return work;\n}\nif(typeof window!==\'undefined\')window.addEventListener(\'pagehide\',controlStopCropWorker);\n\nfunction bindControlReportPhotos(root){\n root.querySelectorAll(\'details.bc-order\').forEach(details=>{\n  const summary=details.querySelector(\'summary\');\n  summary.setAttribute(\'aria-haspopup\',\'dialog\');\n  summary.onclick=event=>{\n   event.preventDefault();\n   const card=ownerBrigadeControlData?.items.find(c=>c.id===Number(details.dataset.controlCard));\n   if(card)openControlReportViewer(card,1,summary);\n  };\n });\n}\nfunction openControlReportViewer(card,stage=1,trigger){\n document.querySelector(\'.bc-photo-viewer\')?.close();\n const refs=Array.from({length:card.source===\'gbu\'?2:1},(_,i)=>(card.report_photos||[]).find(p=>p.stage===i+1)||{stage:i+1});\n let index=Math.max(0,refs.findIndex(ref=>ref.stage===stage)),request=0,start=null,useOriginal=false,croppedSrc=null,rawSrc=null,displayedImage=null;\n const p=card.employee||{},person=String(p.full_name||\'\').trim().split(/\\s+/)[0]||\'Бригадир\';\n const name=[person,p.metro,p.height_cm].filter(Boolean).join(\' · \');\n const kind=card.source===\'gbu\'?\'ГБУ ⬛️\':\'Частный\';\n const dialog=document.createElement(\'dialog\');dialog.className=\'bc-photo-viewer\';dialog.setAttribute(\'aria-label\',\'Заказ · \'+card.surname);\n const mark=ready=>ready?cabinetIcon(\'check\'):\'⏳\';\n const keyboardFocus=trigger?.matches?.(\':focus-visible\')===true;\n dialog.innerHTML=`<div class="bc-expanded-card"><header><span class="bc-viewer-kind">${kind}</span><button type="button" aria-label="Закрыть заказ">×</button></header><div class="bc-viewer-photo"><div class="bc-viewer-stage"></div>${refs.length>1?\'<button type="button" data-photo-prev aria-label="Предыдущее фото">‹</button><button type="button" data-photo-next aria-label="Следующее фото">›</button>\':\'\'}<button type="button" data-photo-original hidden aria-pressed="false">Оригинал</button><span role="status" class="bc-viewer-status"></span></div><section class="bc-viewer-info"><small>Фамилия умершего</small><h2>${esc(card.surname||\'—\')}</h2><small>Бригадир</small><p class="bc-viewer-brigadier">${esc(name)}</p><dl class="bc-viewer-rows"><div><dt>${cabinetIcon(\'phone\')}Связь</dt><dd class="${card.on_contact?\'is-done\':\'\'}">${esc(card.contact_time||\'—\')} <span aria-label="${card.on_contact?\'На связи\':\'Ожидается связь\'}">${mark(card.on_contact)}</span></dd></div><div><dt>${cabinetIcon(\'camera\')}Фото</dt><dd class="${card.photos_complete?\'is-done\':\'\'}">${esc(card.photo_time||\'—\')} ${controlPhotoMarkers(card)}</dd></div><div><dt>₽ Чай</dt><dd class="${card.completed?\'is-done\':\'\'}">${card.completed?esc(cabinetMoney(card.tea_amount))+\' ₽\':\'Ожидается\'} <span>${mark(card.completed)}</span></dd></div></dl></section></div>`;\n const close=dialog.querySelector(\'header button\'),area=dialog.querySelector(\'.bc-viewer-stage\'),status=dialog.querySelector(\'[role=status]\'),prev=dialog.querySelector(\'[data-photo-prev]\'),next=dialog.querySelector(\'[data-photo-next]\');\n const original=dialog.querySelector(\'[data-photo-original]\');\n const updateOriginal=()=>{if(!original)return;original.hidden=!croppedSrc;original.textContent=useOriginal?\'Автокадр\':\'Оригинал\';original.setAttribute(\'aria-pressed\',String(useOriginal));if(displayedImage)displayedImage.src=useOriginal||!croppedSrc?rawSrc:croppedSrc};\n if(original)original.onclick=()=>{useOriginal=!useOriginal;updateOriginal()};\n const render=async()=>{\n  const ref=refs[index],ticket=++request;croppedSrc=null;rawSrc=null;displayedImage=null;if(original)original.hidden=true;\n  status.textContent=(index+1)+\' / \'+refs.length;\n  if(prev)prev.disabled=index===0;if(next)next.disabled=index===refs.length-1;\n  if(!ref.revision){area.textContent=card[ref.stage===1?\'first_received\':\'second_received\']?\'Фото \'+ref.stage+\' ещё не синхронизировано\':\'Ожидается фото \'+ref.stage;return}\n  area.textContent=\'Загрузка фото…\';\n  try{const src=await controlReportImage(card,ref);if(!dialog.open||ticket!==request)return;\n   const img=new Image();img.alt=\'Фото \'+ref.stage+\' · \'+card.surname;img.draggable=false;rawSrc=src;displayedImage=img;\n   img.onload=()=>{img.onload=null;if(!dialog.open||ticket!==request||closing)return;controlAutoCropImage(card,ref,img,src).then(cropped=>{if(!dialog.open||ticket!==request||closing)return;croppedSrc=cropped;updateOriginal()})};\n   img.src=src;area.replaceChildren(img);\n  }catch(error){if(dialog.open&&ticket===request){area.textContent=error.message;const retry=document.createElement(\'button\');retry.textContent=\'Повторить\';retry.onclick=render;area.append(retry)}}\n };\n let closing=false,closeTimer=null;\n const finishClose=()=>{if(closeTimer!==null)clearTimeout(closeTimer);if(dialog.open)dialog.close()};\n const requestClose=()=>{\n  if(closing)return;closing=true;request++;\n  if(globalThis.matchMedia?.(\'(prefers-reduced-motion: reduce)\').matches){finishClose();return}\n  dialog.classList.add(\'is-closing\');\n  dialog.addEventListener(\'animationend\',event=>{if(event.target===dialog&&event.animationName===\'bc-card-out\')finishClose()});\n  closeTimer=setTimeout(finishClose,230);\n };\n dialog.addEventListener(\'cancel\',event=>{event.preventDefault();requestClose()});\n const move=delta=>{if(closing)return;const target=index+delta;if(target>=0&&target<refs.length){index=target;render()}};\n close.onclick=requestClose;if(prev)prev.onclick=()=>move(-1);if(next)next.onclick=()=>move(1);\n dialog.addEventListener(\'keydown\',event=>{if(event.key===\'ArrowLeft\'){event.preventDefault();move(-1)}if(event.key===\'ArrowRight\'){event.preventDefault();move(1)}});\n area.addEventListener(\'touchstart\',event=>{start=event.touches.length===1?{x:event.touches[0].clientX,y:event.touches[0].clientY}:null},{passive:true});\n area.addEventListener(\'touchend\',event=>{if(!start||!event.changedTouches.length)return;const dx=event.changedTouches[0].clientX-start.x,dy=event.changedTouches[0].clientY-start.y;start=null;if(Math.abs(dx)>45&&Math.abs(dx)>Math.abs(dy)*1.3)move(dx<0?1:-1)},{passive:true});\n area.addEventListener(\'touchcancel\',()=>{start=null},{passive:true});\n dialog.addEventListener(\'click\',event=>{if(event.target!==dialog)return;const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)requestClose()});\n dialog.addEventListener(\'close\',()=>{if(closeTimer!==null)clearTimeout(closeTimer);request++;dialog.remove();document.body.classList.remove(\'bc-viewer-open\');const target=trigger?.isConnected?trigger:document.querySelector(`[data-control-card="${card.id}"]>summary`);target?.focus({preventScroll:true});if(!keyboardFocus)target?.blur()},{once:true});\n document.body.append(dialog);document.body.classList.add(\'bc-viewer-open\');dialog.showModal();close.focus();render();\n}\n</script>\n' + '</body>', 1)


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="cabinet-photo-frame-style">\n#cabinetNativeForm [hidden]{display:none!important}\n.cabinet-photo-reminder{padding:12px;border:1px solid var(--gold2);border-radius:12px;background:var(--panel2);margin:10px 0}\n.cabinet-photo-reminder strong{display:block;color:var(--gold);font-size:16px;line-height:1.3}\n.cabinet-photo-reminder span{display:block;margin-top:4px;font-size:13px;line-height:1.35}\n.cabinet-photo-frame{position:relative;aspect-ratio:3/2;width:100%;overflow:hidden;border:2px solid var(--gold);border-radius:12px;background:#171717}\n.cabinet-photo-frame canvas{display:block;width:100%;height:100%;touch-action:none;user-select:none;-webkit-user-select:none}\n.cabinet-photo-frame::after{content:\'\';position:absolute;inset:0;pointer-events:none;background:linear-gradient(to right,transparent 33%,#ffffff30 33%,#ffffff30 33.3%,transparent 33.3%,transparent 66.6%,#ffffff30 66.6%,#ffffff30 67%,transparent 67%),linear-gradient(to bottom,transparent 33%,#ffffff30 33%,#ffffff30 33.3%,transparent 33.3%,transparent 66.6%,#ffffff30 66.6%,#ffffff30 67%,transparent 67%)}\n.cabinet-frame-tools{display:flex;align-items:center;gap:6px;margin:8px 0}\n.cabinet-frame-tools button{min-height:44px;min-width:44px;padding:6px;border:1px solid var(--line);border-radius:9px;background:var(--panel2);color:var(--text)}\n.cabinet-frame-tools input[type=range]{min-width:0;flex:1;padding:0;accent-color:var(--gold)}\n#cabinetFrameEdit>small{display:block;font-size:12px;color:var(--muted);margin:6px 0 12px}\n#cabinetFrameEdit>p{font-size:14px;line-height:1.4}\n#cabinetFrameReview h4{font-size:18px;margin:12px 0}\n#cabinetFrameReview img{display:block;width:100%;height:auto;aspect-ratio:3/2;object-fit:contain;border-radius:12px;background:#171717}\n#cabinetFrameReview p{font-size:15px;line-height:1.4}\n</style>\n' + '</body>', 1)


_PREVIEW_FIXTURE_SCRIPT = r'''
const previewRole='owner';
const previewStore={comments:{},agents:[]};
const previewGroups={brigadier:'Бригадиры',main:'Основной состав',cashless:'Безнал состав',reserve:'Резерв'};
const previewSizes={brigadier:22,main:34,cashless:24,reserve:13};
const previewNames=['Алексей','Андрей','Арсений','Владислав','Даниил','Дмитрий','Евгений','Иван','Игорь','Кирилл','Лев','Максим','Никита','Роман'];
const previewMetros=['Аннино','Балашиха','ВДНХ','Давыдково','Котельники','Лухмановская','Марьино','Некрасовка','Одинцово','Перово','Рязанский','Свиблово','Таганская','Щелковская'];
const previewPeople=Object.entries(previewSizes).flatMap(([code,size],g)=>Array.from({length:size},(_,i)=>({id:g*100+i+1,tg_id:-(g*100+i+1),full_name:previewNames[i%14]+' Тестовый',display_name:previewNames[i%14],first_name:previewNames[i%14],metro:previewMetros[(i+g)%14],height_cm:180+i%12,group_code:code,group_label:previewGroups[code],active:true,has_car:i%7===0,phone:'',telegram_username:''})));
let previewPrivateCount=11,previewGbuCount=8;
const previewCards=Array.from({length:19},(_,i)=>({id:i+1,source:i<11?'private':'gbu',surname:'ТЕСТОВЫЙ '+(i+1),employee:previewPeople[i],contact_time:['06:00','07:10','07:30','08:00','08:30','09:00'][Math.min(5,Math.floor(i/3))],photo_time:['08:15','09:20','09:30','10:00','10:30','11:00'][Math.min(5,Math.floor(i/3))],on_contact:i<14,first_received:i<10||i>=11&&i<16,second_received:i===12,first_manual:false,second_manual:false,completed:i===12,tea_amount:i===12?5000:0}));
const previewToday='2026-10-17';
function previewPhotoCounts(){return {private:previewCards.filter(c=>c.source==='private'&&c.first_received).length,gbu:previewCards.filter(c=>c.source==='gbu'&&c.first_received).length,gbu_first:previewCards.filter(c=>c.source==='gbu'&&c.first_received).length,gbu_second:previewCards.filter(c=>c.source==='gbu'&&c.second_received).length}}
function previewTotals(source){const rows=previewCards.filter(c=>source==='all'||c.source===source);return {orders:source==='private'?previewPrivateCount:source==='gbu'?previewGbuCount:previewPrivateCount+previewGbuCount,cards:rows.length,on_contact:rows.filter(c=>c.on_contact).length,first_photos:rows.filter(c=>c.first_received).length,second_photos:rows.filter(c=>c.second_received).length,photos_complete:rows.filter(c=>c.first_received&&(c.source!=='gbu'||c.second_received)).length,completed:rows.filter(c=>c.completed).length}}
function previewProgress(){return {cards:19,expected:previewPrivateCount+previewGbuCount,sources:{private:{cards:11,expected:previewPrivateCount,closed:11},gbu:{cards:8,expected:previewGbuCount,closed:8}}}}
function previewReadiness(day){
 const specs={brigadier:[9,0,7,0,6],main:[8,3,0,0,23],cashless:[8,0,3,0,13],reserve:[2,0,0,0,11]},groups={};
 for(const [code,spec] of Object.entries(specs)){const people=previewPeople.filter(p=>p.group_code===code),keys=['ready','not_ready','day_off','responded','no_response'];let offset=0;const g={label:previewGroups[code],counts:{}};keys.forEach((key,i)=>{g[key]=people.slice(offset,offset+spec[i]);g.counts[key]=spec[i];offset+=spec[i]});groups[code]=g}
 return {work_date:day,groups,cutoff_label:'14:00',totals:{ready_now:27,brigades_on_contact:14,brigades_expected:19}};
}
function previewDateFolders(anchor){const ym=anchor.slice(0,7),count=new Date(Date.parse(ym+'-01T12:00:00Z'));count.setUTCMonth(count.getUTCMonth()+1);count.setUTCDate(0);return Array.from({length:count.getUTCDate()},(_,i)=>{const date=ym+'-'+String(i+1).padStart(2,'0');return {date,label:date===previewToday?'Сегодня':'',count:date===previewToday?previewPrivateCount+previewGbuCount:0}})}
async function previewApi(path,opts={}){
 if(typeof document!=='undefined'&&document.readyState!=='complete')await new Promise(resolve=>window.addEventListener('load',resolve,{once:true}));
 const url=new URL(path,'https://preview.invalid'),p=url.pathname,day=url.searchParams.get('work_date')||previewToday,method=(opts.method||'GET').toUpperCase();
 const body=typeof opts.body==='string'?JSON.parse(opts.body):{};
 if(p==='/api/date-folders')return {dates:previewDateFolders(url.searchParams.get('anchor')||day)};
 if(p==='/api/bootstrap')return {user:{id:-999,name:previewRole==='assistant'?'Тестовый диспетчер':'Тестовый владелец'},is_owner:previewRole!=='assistant',cabinet_only:false,cabinet_kind:previewRole==='assistant'?'assistant':'owner',can_manage_cabinets:previewRole!=='assistant',can_manage_cash:true,can_record_cash:true,can_use_owner_cabinet:false,can_view_vlad_allowance:false,selected_date:day,today:previewToday,groups:previewGroups,dates:previewDateFolders(day),summary:{orders_total:previewPrivateCount+previewGbuCount,private_count:previewPrivateCount,gbu_count:previewGbuCount,requested_staff:previewPrivateCount*4+previewGbuCount*4+2,requested_staff_by_source:{private:previewPrivateCount*4+2,gbu:previewGbuCount*4},brigades_on_contact:previewCards.filter(c=>c.on_contact).length,photo_reports:previewPhotoCounts(),order_completion:previewProgress(),number_cards_count:19},day_comment:previewStore.comments[day]||{text:'Условная заметка для проверки интерфейса',version:1,updated_by:'Тестовый пользователь'}};
 if(p==='/api/readiness/summary')return previewReadiness(day);
 if(p==='/api/employees')return previewPeople;
 if(p==='/api/orders')return [];
 if(p==='/api/brigade-contact-orders')return {work_date:day,items:previewCards,can_edit:true};
 if(p==='/api/photo-control')return {work_date:day,items:previewCards.filter(c=>c.source===url.searchParams.get('source')),can_edit:true};
 if(/^\/api\/photo-control\/\d+$/.test(p)&&method==='PATCH'){
  const card=previewCards.find(c=>c.id===Number(p.split('/').pop()));if(!card)throw Error('Нет тестовой карточки');
  const key=body.stage===2?'second':'first';card[key+'_received']=body.received===null?false:!!body.received;card[key+'_manual']=body.received!==null;return {counts:previewPhotoCounts()};
 }
 if(/^\/api\/owner\/order-report-media\/\d+\/[12]$/.test(p))return {image:["data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAFUAfQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDNooopnnBRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAU6ON5pUiiRnkdgqooyWJ6ADuabT4pZIJklidkkRgyMpwVI5BFAF//hHdb/6A+of+Az/4Uf8ACO63/wBAfUP/AAGf/CtPQvEWsz+INNil1S7eN7qJWVpmIYFgCDV/xrrurWfi6+t7bUbqGFPL2okpAGY1J4+poNLRtc5aDStRuZZorewupZIW2ypHCzFDzwwA4PB6+lT/APCO63/0B9Q/8Bn/AMK6rwTdT/2L4su/Of7R9nEnm7vm37ZTuz655rmf+En13/oL3v8A3+b/ABoFaKSbM+5tLizm8m6t5YJQM7JUKtj6GpoNK1G6h863sLqaL+/HCzD8wK1PD2de8X2KarM9wHf5jK27dtUkKc9iQB+NaPibxHr9j4jurdLua0ihkKwRRjauwfdOO+RQCirXZyKwyvOIFidpmbYIwpLFs4xj1q//AMI7rf8A0B9Q/wDAZ/8ACtbxFrtlq0Om6jblodZjAFwyLtBI6MD6gj9famaF4i1mfxBpsUuqXbxvdRKytMxDAsAQaAtG9jM/4R3W/wDoD6h/4DP/AIVnyRvDK8UqMkiMVZGGCpHUEdjXZeNdd1az8XX1vbajdQwp5e1ElIAzGpPH1NcdLLJPM8srs8jsWdmOSxPJJoFJJOyLFrpWo30RltLC6uIw20vDCzgH0yB15FOuNH1O0jMlzp15DGOrSQMoH4kV1+hXU9n8LNXuLaZ4ZkvBtdGwRkxA8/Q1Q8MeKNZbxDZW817PcwTyrFJHM28FWOO/p1oK5Vpfqcvb2893OsFtDJNK2dscalmOBngD2pkkbwyvFKjJIhKsjDBUjqCOxrvNOsoNP+L4trZQsKu7Ko6LuhLED6E1yfiL/kZtV/6/Jv8A0M0CcbK5UtbK6vpTFaW01xIBuKQxlyB64HbkVBXb/C7/AJGW5/682/8AQ0rM8F6Lb6tq0kt6M2VnGZpgejY6A+3U/hQChdK3UxbbTNQvE32tjczr/eiiZh+gqGaCa2kMc8UkUg6rIpU/ka6HVPG2rXl032O5eytFOIYIPkCr2zjrWxoGonxlbzaFrRWW48svaXRUB0YdiR19fwPtQCjFuyZxtrpWo30RltLC6uIw20vDCzgH0yB15FT/APCO63/0B9Q/8Bn/AMKbDqeq6QZbS3vbi2CyHekchUbuh6fT9K7TT9Y1KT4Y6revf3DXUd0qpMZDuUZi4B/E/nQEYxZw11pWo2MQlu7C6t4y20PNCyAn0yR14NRW1pc3jlLW3lncfwxIWP6Vp21zqviXULTS7nULiZJZhgSyFgvq2D6DNb3iXxDJolydC0A/Y7a1wskkYG+R8ckmgOVb9DkLqwvLLH2u0ngz082Mrn8xVeuu0HxfePepYazL9u065YRyLONxXPG4HrxWT4o0YaFr9xZISYRh4ieu09Py6fhQJxVrox6sWtheXxYWlpPcFeohjL4/IUll9n+3W/2vd9m81fO29dmfmx74zXqHg3xJFqWtyabp9jHZ6ZDbM6RgfMzblG5j9Cf8TQOEVJ6s8ooo70UEBRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFAGl4d/wCRm0r/AK/If/QxWl4+/wCR21D/ALZ/+i1rJ0a4itNd0+5nbZDFcxyO2CcKGBJwParvi6/tdU8UXl5Zy+bbybNj7SucIoPBAPUGgu/uW8zofh/JHFoniaSaHzoktlZ4t23eoWTK57Z6ZrL/ALf8N/8AQop/4HP/AIVN4N1TS7LTdbs9SvPsovYViRvKZ+zgnAHbcKh/sfwp/wBDU/8A4ASUF3fKrfoYxMl5qrzaTZSw4bzI4YS0jRAY79eveuht/H1xLElvrWnWmpRLwTIgD/1H6Vn2epW/hfxJHdaVd/b7dVw7GMx7weq4PI+taF5D4N1O6e9XVLqw8073tjblsE9cEcD9aCVdbMZ4q0TTY9Kste0cPHZ3bbGhY52Nz0/75YfhWJ4d/wCRm0r/AK/If/QxWl4j16zvNPs9H0qKRNOsySrS/ekbnk+nU/n2rJ0a4itNd0+5nbZDFcxyO2CcKGBJwPagUrc2hrePv+R21D/tn/6LWubrb8XX9rqnii8vLOXzbeTZsfaVzhFB4IB6g1iUCn8TPQPDc1pb/DLVZb61N1bLeDfCJCm7/VY5HI5wfwrMt/FWjaXJ9o0nw3HBdgEJLNctIE9wD/iKjsNXsYfh3qelST4vZrkSRxbG+Zcx85xj+E9+1ctQW52SsdV4LuZr34gWlzcOXmlaV3Y9yY2q3rOt+H4tcv45vC6TSpcyK8v2113sGOWxjjPXFYvhG/tdL8UWd5eS+Vbx7977S2MowHABPUiqWs3EV3ruoXMDb4ZbmSRGwRlSxIOD7UCUrR+Z6D4E1PSL3W547DQlsJRbMxlFy0mV3L8uCPcHPtWP4BHn6d4jso+Z57P92B1PDj+bCqPgLV7HRdcnudQn8mJrZow2xmy25TjAB7A1jaLq9zoeqRX1tgunDKejqeoNBSns35mfXVfDyF5fGNs6A7Ykkd/YbSv8yKlux4P1mdrz7bc6VLId0kJgMi7u+3b/AJ9hSvr2k6DpdxZeHRNLc3I2y30w2kL6KO3+etBMUou7ZzmsypPrmoTRkFJLmRlx6FiRXVaZ/wAkj1n/AK/F/nFXEV1Njq9jD8OtT0qSfF7NciSOLY3K5j5zjH8J79qBReruVvBEqQ+MdNeQgAuy8+rKQP1Iqt4qheDxVqiSAhjcu4z6Mdw/QisqOR4pFkjYq6EMrDqCOhrsbnWNA8UQxSa001hqUahGuYY96SAeoHP+etALWNjkbWF7i7hgiBMkjqigepOBXW/EyVJPFSqpGY7ZFb65Y/yIosrzwv4ak+22c0+rX6g+TviMcaH1IPP8/wCtcre3k+oXs13cvvmmYsx9/wDCgHpGxXrtvhd/yM1z/wBebf8AoaVxNdT4C1ex0XXJ7nUJ/Jia2aMNsZssWU4wAewNAU3aSOW70UUUEBRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFdVq+m+HNJ1ubTJRqn7tlUzCWMgZAOduwZ6+tVbvwdq0F7cwwQieKCcwmbeqjO0Nzk8fKRyeOcZoKcGc/RWlY6DqeowefbWu6IttDs6oGPoNxGT9Khk0q+htZrmS3ZIoZvIl3EAo+M4K9fxxigVmU6K2NF0OfUZoJGtppbWSR4h5MiK7OELYG49gMk/1qrYaPf6mrvaQb44yA0jOqKD6bmIGfagLMo0VoSaJqUX2zzLVkNmqtOGIBUE4Bxnke4zUZ0q9XSl1QwH7E0nlCXcPvemM57dcYoCzKdFWLmxubOO3knj2LcR+bF8wJZc4zgdOh61ZstC1LULf7Rb2+Yd20SSSLGpPoCxGT9KAszOorSTQNVkvZ7NbKT7TBGZZIzgEKMcjPXqOmc0l7oWpafbC5ubbbCW2b1dXAb0O0nB9jQFmZ1FdF4l0L7LrmrLp1ttsrLyy435KBlHqcnk++KxY7G5lsZr1I828LKjuWAwW6ADqenagHFp2K9FFFAgooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigDtvF2q21l4uvf+JPZzTxshE0rSHJ2KQSu7b+lVLu/ur3wDNcTys0txrBMp6bv3QP5dOPauYnuJrqZpriaSaVvvPIxZj25Jo+0TfZvs3nSeRv8zytx27sY3Y6Zxxmgtzu2dlqUVjc6VoLGw1G6j+xrGptJQEEmTvGNh+bP9KluLg3/AIi1TR5ovKkvLRY9plDnz41DISwAGeMH3Ncbbahe2aMlreXECv8AeWKVlB+uDUMc0sUyzRyOkqtuV1YhgfUH1oHzneaM32TxToGjjj7JbyvMP+mskTM2foNo/CqKLZT+B9LD2l7cpHPMJhaSBdshI2lgVb+HGPxrlVvbtLs3a3Uy3JJJmEhD5PB+brzmi1vbuyZmtLqa3ZhgmKQoT+VAc53C3CjW9O0qa3e2S5042TCaUPIFfJj3YAwQccelRrBJNHL4UAxKmmowT/p4DeaR9cMR+FcQZ5jP55lfzt27zNx3Z9c9c1IL+8W8+2Ldzi6znzxId+cY+916UBzmj4puEm16aGE5gtFW1i/3UG3+YJ/Gr/iCCe70jQZrKN5bJLNYiI1JCzAnfnHQk4+tcwzFmLMSSTkk96sWuoXtju+yXlxb7vveVKyZ+uDQTzb3PQbWSWy09YpWK6nb6BcGTP3kUupjB9CB27Vy+jnPg3xKD0AtiB7+ZWGt5co8rrcTB5lKSsHOXU9Q3qD701LiaOGWFJpFilx5iKxCvg5GR3waCnO53WpTLD8S7+1uMrbaggtXJHGHjUA/g2OawtcjfSdGsNFcbZyzXVyv+0flQfgoz/wKs+1vYri9jl1m4vZ44kAQI+5uDwuWPyjr0/KotW1KXV9UuL+fh5n3YH8I6AfgMCgHK6ZTooooMwooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooA//9k=", "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAFUAfQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCjRRRXnnSFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABTo43mlSKJGeRyFVVGSxPQAdzTafFLJBMksTskiMGVlOCpHIIoAv/wDCPa1/0B9Q/wDAZ/8ACj/hHta/6A+of+Az/wCFaWh+IdZn8QabFLql28b3USsrSkhgWGQav+M9c1Wz8WX1vbajdQwp5e1ElIAzGpPH1NXaNrk3d7HLwaXqFzLLFBYXUskJ2yrHCzFDzwwA4PB6+lT/APCPa1/0B9Q/8Bn/AMK6jwVcz/2N4ru/Of7R9nEnm7vm3bZTuz655rmv+Em1z/oL3v8A3+ai0Uk2F3coXNrcWc3k3UEsEoGdkqFTj6GpYNK1G5i823sLqWP+/HCzD8wK1PD+dd8XWKarM9wHf5jK2d21SQpz2yAPxrQ8S+ItesfEVzAl1NaxQyFYYkG1dg+6cd8ihRVrsLvY5NYZXnECxu0zNsEYUli3TGPWr/8Awj2tf9AfUP8AwGf/AArV8Q65Z6rDpuo25aHWIwPtDIu3JHRgfUEfr7UzQ/EOsz+INNil1S7eN7qJWVpSQwLDINFo3sF3Yzf+Ee1r/oD6h/4DP/hWfJG8MrxSoySISrKwwVI6gjsa7Hxnrmq2fiy+t7bUbqGFPL2okpAGY1J4+prj5ZZJ5nlldnkdizMxyWJ5JNKSSdkNNssWul6hfRGW0sLq4jB2looWcA+mQOvIp0+kanaxmS4067hjHVpIGUD8SK63Q7mez+F2rT20zwzJeDa6NgjJiHX6GqPhnxPrLeILK3mvZrmGeVYpI5W3ghjjv6darlWl+ors5e3t57qdYbeGSaVs7UjUsxwM8Ae1NkjeGV4pUZJEJVlYYKkdQR2Nd3p9lBp/xcFtbKFhV3KqOi5hLED6E1yniH/kZdV/6/Jf/QzUuNkNO7KlrZ3V9KYrS2muJANxWJC5A9cDtyKgrtfhh/yMlz/15t/6GlZvgzRrfVtVklvRmytIzNKD0bHQH26n8KajdK3UVzGttMv7xN9rY3M6/wB6KJmH6CoZoJraQxzxPE46q6lT+RroNT8aateXTfZLh7K1U4ihg+UKvbOOtbGg6ifGNvNoeslZbjyy9rdFQHVh2JHX1/A0KKbsmF2tTjrXS9QvojLaWF1cRg7S0ULOAfTIHXkVP/wj2tf9AfUP/AZ/8KbDqeqaQZbS3vbi2CyHekchUbuh6fT9K7LT9Y1KT4Z6revfXDXUd0qpMZDuUZj4B/E/nRGKYNtHEXWl6hYxCW7sLq3jJ2hpYWQE+mSOvBqK3tLm7cpbW8szD+GNCx/StO2udV8SX9pplxf3EySzDAkcsF9WwfQZrd8SeIJNEuToWgn7HbWuFkkjHzyPjkk0cq36Bd7HI3NheWWPtVpPBnp5sZXP51XrrdB8XXb3iWOsSfbtPuWEcizjcVzxuB68VleJ9HGh69cWaEmHh4ieu09Py6fhSaVroaetmY9W4dK1G5h86CwupYv76Qsy/mBVrw3HYSa9bf2nIkdmpLyb+hwMgficVo6l401eXVpZrO9eC3RyIYo+ECA8ZHfj1oSVrsG30OaZSrFWBBBwQe1JXaeP4opBpGpeUsV1eW2+dVGOQFIz/wB9EfhXF0SVnYE7oKKKKkYUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFAGl4e/5GXSv+vyL/ANDFaPjz/kdNQ/7Z/wDotaydGuIrTXLC4nbbFFcxu7YJwoYEnirvi2/ttT8T3l5Zy+bbybNr7SucIoPBAPUGrv7hPU6DwDJHFoviWSaHzokt1Z4t23eoWTIz2z0zWZ/b3hz/AKFJP/A5/wDCpvB2qaZZadrdnqV59mF7CsSN5bP2cE4A7bhUP9j+Ff8AoaX/APAGSqu+VW/QXV3MYmS81R5tKs5YcN5kcUJZ2jAx369e9dDB49uJYlt9Z0601KJeCZEAf+o/Ss+z1K38MeI47rS7s39uq4djGY94PVcHkfWtC7h8HandPerqd1YeYd725ty2CeuCOB+tJXWzGxninRdNj0qy13Rw0dndtsMLHOxuen/fLD8KxfD3/Iy6V/1+Rf8AoYrR8Ra9aXen2ej6VFImnWmSrS/ekbnk+nU/nWTo1xFaa5YXE7bYormN3bBOFDAk8Unbm0BXsa3jz/kdNQ/7Z/8Aota5ytrxbf22p+J7y8s5fNt5Nm19pXOEUHggHqDWLSl8TGtjv/Dk1rb/AA01SW9tTdWy3g3wiQpu/wBVjkcjnB/Csy38U6Ppkn2jSvDkcN2AQksty0mz3wf8RUdjq9jD8PNS0uSfF7Nch44tjcrmPnOMfwnv2rl6pyslYSW9zqfBlzNeePrS5uHLzStKzse5MbVb1jWtAi1u/jm8MJNKlxIry/bHXewY5OMcZ64rF8JX9tpniezvLyXyrePfufaWxlGA4AJ6kVS1m4iu9c1C4gbdFLcyOjYIypYkHmhStELanoHgbU9JvdamjsNDWwlFuzGUXLSZXcvGCPcHPtWP4D/fad4is4+Z5rT92B1PDj+bCqXgTV7HRtbnuNQn8mJrZkDbGbLblOOAfQ1jaNq1xompxX1tgunDKejqeoNNT2bFbcoV1Hw9heTxfbOoO2JJHf2G0j+ZFTXY8I6xO159tudLlkO6SEwGRd3fbt/z7Ch9e0nQtMnsvDwmlubgbZb2YbSF9FHb/PWkkk7tjburHPaxKk+t380ZBSS5kZSPQsSK6jTf+STax/1+L/OKuKrqLHV7GH4ealpck+L2a5DxxbG5XMfOcY/hPftSi9WDRW8FSpD4w05nIALsvPqVIH6kVX8UwvB4p1NJAQTcO4z6Mcj9CKyo5HikWSNiroQysOoI6GuvudX0HxPDFJrLTWGpRqEa5hTekg9wOf8APWhaxsD0dzk7WF7i7hhiBMkjqigepOBXV/EqVJPFKqpGY7ZFb65Y/wAiKWyvPDHhuT7ZZzT6rfKD5O+IxxofUg8/zrlb28n1C9mu7l980rFmPvQ9I2Dd3IK6nw5oNuLY67rR8rTITlEI5uG7ADuP5/nWV4fj0mTVUOs3Bhs0XcQEZvMORhflBI+vtXU69ceHtduEL+JjDbRDbDbx2Mm2Mf40RS3Bvoctr+tz6/qj3ko2JjbFGOiIOgrLrT1ez0q08n+zNUa+3bvMzA0ezGMdeuefyrMqXe+o1sFFFFIYUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFdTq+m+HdK1ubTJBqf7tlBmEsZAyAc7dgz19aq3fhDVYby5hghE8UExhM29VGdobnJ44I5PHOM1TixXRgUVo2WhalqEHnW1tuiLbQ7OqBj6DcRk/SopNLvYbWa4kt2SOGXyZNxAKPjOCOv44xSswuU6K19G0SfUZoJGtppLWSR4h5MiK7OELYG49gMk/1qtYaRfakrvawb40OGdmVFB9NzEDPtRZhco0Vfk0TUYvtfmWrIbRVacMQCoJwDjPI9xmmHS71dKXUzAfsbSeUJdw+96Yznt1xiizC5Toqxc2NxaR28k8exbiPzIvmBLLnGcDp071YstD1HULf7Rb2+Yd20SSSLGpPoCxGT9KLMdzPorRTQdUkvZ7NbKT7TBGZZIzgEKMcjPXqOmc0l7oepafbC4ubbbCW2b1dXAb0O0nB9jRZiujPorofEmh/Zdb1VdOtttlZeWXG/JQMo9Tk8n3xWNHY3MtjNepHm3hZVdywGC3QY6np2oaadgTK9FFFIYUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB2vi3Vbaz8W3n/Eos5p42QiaVpDk7FIyu7b+lVLu/ubzwHNcTylpbjVyZD03fugfy6ce1czPcTXUzTXE0k0rfeeRizH6k0faJvs32bzpPI3+Z5W47d2MbsdM44zVud2yeU7HUY7G50rQmNhqN1H9jWNTaSgIJMneMbD82f6VLPOb/wAQ6no8sXlveWqx7TIHPnxqGQlgAM8YPua4631C9s0ZLa8uIFf7yxSlQfrg1DHNLFMs0cjpKrbldWIYH1B9afOHKd1o7fZPFGg6QOPslvI8w/6ayRMzfkNo/CqSLZz+CdMD2l7crHPMJhaSBdrkjaWBU/w4x+Ncst7dpdm7W6mFySSZhId+Twfm60W17d2TM1rdTQMwwTFIVJ/KjnDlO2W4Ua1p2lTW72yXOnGzYTSh5Ar5Me7AGCDjj0piwSSxy+FQMSrpyME/6bhvNI+uGI/CuJM8xn88yv527d5m47s+ufWpBfXi3n2xbqcXWc+cJDvzjH3uvSjnDlNHxRcJNrssMRzBaKtrF/uoNv8AME/jV7X4J7vSdCms43ls0s1iIjUkLMCd+cdCePrXMklmLMSSTkk96ntdQvbLd9ku57fd97ypCmfrg1PNvcdj0C1kls7BY5WK6lb6BcGTP3kUupjB9CB2rmNHOfB3iQHoBbED38ysRbu5R5XW4lDzKUlYOcup6g+oPvTUuJo4ZYUmkWKXHmIrEK+DkZHfBpuYrHc6lMsPxJv7W4yttqCC2ckcYeNQD+DY5rC1uN9J0ex0VxtmLNdXK/7R+VB+CjP/AAKqFrexXF5HLrFxezxxKAgRtzcHhcsflHXp+VRarqMurapcX03DzPuwP4R0A/AYFDldMEinRRRUFBRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQAUUUUAFFFFABRRRQB//9k="][Number(p.split('/').pop())-1]};
 if(p==='/api/owner/brigade-control')return {work_date:day,updated_at:'2026-10-17T09:00:00Z',totals:Object.fromEntries(['all','private','gbu'].map(s=>[s,previewTotals(s)])),order_completion:previewProgress(),items:previewCards.map(c=>({...c,photos_required:c.source==='gbu'?2:1,photos_received:Number(c.first_received)+(c.source==='gbu'?Number(c.second_received):0),photos_complete:c.first_received&&(c.source!=='gbu'||c.second_received),report_photos:[{stage:1,revision:'preview-1'},...(c.source==='gbu'?[{stage:2,revision:'preview-2'}]:[])]}))};
 if(p.startsWith('/api/day-comments/')){const key=p.split('/').pop();if(method==='PUT')previewStore.comments[key]={text:String(body.text||''),version:Number(body.version||0)+1,updated_by:'Тестовый пользователь'};return previewStore.comments[key]||{text:'Условная заметка для проверки интерфейса',version:1,updated_by:'Тестовый пользователь'}}
 if(p.startsWith('/api/dispatch-counts/')&&method==='PATCH'){if(body.field==='private_count')previewPrivateCount=Number(body.value);else if(body.field==='gbu_count')previewGbuCount=Number(body.value);return {ok:true}}
 if(p==='/api/gbu/agents'&&method==='GET'){const q=(url.searchParams.get('search')||'').toLowerCase();return previewStore.agents.filter(a=>(a.full_name+a.phone).toLowerCase().includes(q))}
 if(p==='/api/gbu/agents'&&method==='POST'){const a={id:previewStore.agents.length+1,full_name:String(body.full_name),phone:String(body.phone)};previewStore.agents.push(a);return a}
 throw Error('Тестовая страница: это действие пока не смоделировано. Рабочие данные и сообщения не затронуты.');
}
// Fail closed even if a future UI action bypasses the shared api() helper.
window.fetch=()=>Promise.reject(new Error('Сеть отключена на тестовой странице'));
window.XMLHttpRequest=function(){throw Error('Сеть отключена на тестовой странице')};
try{navigator.sendBeacon=()=>false}catch{}
'''


_CONTROL_COMPACT_CSS = r'''<style id="control-compact-layout-style">
/* Compact Control summaries shared by production and isolated preview. */
#ownerBrigadeControlContent #orderPreparation{padding:6px 7px;margin:0 0 6px;border-radius:11px}
#ownerBrigadeControlContent #orderPreparation h2{font-size:15px;line-height:1.2;margin:0 0 4px}
#ownerBrigadeControlContent #numberCardsBtn{min-height:28px;padding:4px 5px;margin-bottom:4px;font-size:11px;line-height:1.2;border-radius:8px}
#ownerBrigadeControlContent #orderPreparation .summary-grid{gap:4px}
#ownerBrigadeControlContent #orderPreparation .metric{min-height:43px;padding:5px 7px;border-radius:8px}
#ownerBrigadeControlContent #orderPreparation .metric b{font-size:13px;line-height:1.15}
#ownerBrigadeControlContent #orderPreparation .metric span{font-size:9px;line-height:1.15;margin-top:1px}
#ownerBrigadeControlContent .bc-heading{padding:5px 7px 4px;gap:4px;grid-template-columns:minmax(0,1fr) 110px;border-radius:11px 11px 0 0}
#ownerBrigadeControlContent .bc-heading h2{font-size:14px;line-height:1.2;white-space:normal}
#ownerBrigadeControlContent .bc-date{font-size:9px;line-height:1.1}
#ownerBrigadeControlContent .bc-date input{min-height:26px;padding:3px 4px;font-size:10px;border-radius:7px}
#ownerBrigadeControlContent .bc-counters{gap:4px;padding:0 7px 4px}
#ownerBrigadeControlContent .bc-counters button{min-height:42px;padding:4px 3px;border-radius:8px}
#ownerBrigadeControlContent .bc-counters b{font-size:18px;line-height:1.1}
#ownerBrigadeControlContent .bc-counters span{font-size:9px;line-height:1.15;margin-top:2px}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter{padding:4px;gap:2px}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line{gap:4px;font-size:10px;line-height:1.1}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line>span{font-size:9px;line-height:1.1;margin:0}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line>b{font-size:15px;line-height:1.1}
#ownerBrigadeControlContent .bc-sources{gap:3px;padding:2px 7px 5px;border-radius:0 0 11px 11px}
#ownerBrigadeControlContent .bc-sources button{min-height:28px;padding:4px 2px;font-size:10px;line-height:1.2;border-radius:7px}
#ownerBrigadeControlContent .bc-list-caption{margin:6px 0 4px;font-size:10px}
/* Put heading/action together and remove tall summary tiles. */
#ownerBrigadeControlContent #orderPreparation{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:center;gap:4px 5px;padding:5px 6px;margin-bottom:5px}
#ownerBrigadeControlContent #orderPreparation h2{grid-column:1;grid-row:1;font-size:13px;line-height:1.15;margin:0}
#ownerBrigadeControlContent #numberCardsBtn{grid-column:2;grid-row:1;width:100%;min-width:0;max-width:100%;min-height:25px;padding:3px 5px;margin:0;font-size:10px;line-height:1.15;border-radius:6px}
#ownerBrigadeControlContent #orderPreparation .summary-grid{grid-column:1/-1;gap:5px}
#ownerBrigadeControlContent #orderPreparation .metric{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:1px 6px;min-height:27px;padding:3px 5px;border-radius:6px}
#ownerBrigadeControlContent #orderPreparation .metric b{grid-column:1;grid-row:1/3;font-size:11px;line-height:1.15}
#ownerBrigadeControlContent #orderPreparation .metric span{grid-column:2;font-size:9px;line-height:1.1;margin:0;white-space:nowrap}
#ownerBrigadeControlContent .bc-heading{padding:4px 6px 3px;grid-template-columns:minmax(0,1fr) 104px}
#ownerBrigadeControlContent .bc-heading h2{font-size:13px;line-height:1.15}
#ownerBrigadeControlContent .bc-date{font-size:0;line-height:1}
#ownerBrigadeControlContent .bc-date input{min-height:25px;font-size:10px;margin:0}
#ownerBrigadeControlContent .bc-counters{padding:0 6px 3px;gap:4px}
#ownerBrigadeControlContent .bc-counters button{min-height:32px;padding:3px 2px;border-radius:6px}
#ownerBrigadeControlContent .bc-counters b{font-size:16px;line-height:1.05}
#ownerBrigadeControlContent .bc-counters span{font-size:9px;line-height:1.1;margin-top:1px}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter{gap:1px;padding:2px}
#ownerBrigadeControlContent .bc-counters .bc-gbu-photo-counter .bc-photo-count-line>b{font-size:13px;line-height:1}
#ownerBrigadeControlContent .bc-sources{padding:1px 6px 4px;gap:3px}
#ownerBrigadeControlContent .bc-sources button{min-height:24px;padding:3px 2px;font-size:10px;border-radius:6px}
#ownerBrigadeControlContent .bc-list-caption{margin:5px 0 3px;font-size:9px}
@media(max-width:350px){#ownerBrigadeControlContent .bc-heading{grid-template-columns:minmax(0,1fr) 100px}#ownerBrigadeControlContent .bc-heading h2{font-size:12px}#ownerBrigadeControlContent #orderPreparation h2{font-size:12px}#ownerBrigadeControlContent #numberCardsBtn{font-size:9px}#ownerBrigadeControlContent #orderPreparation .metric{gap:1px 4px;padding:3px}#ownerBrigadeControlContent #orderPreparation .metric b{font-size:10px}}
#ownerBrigadeControlContent .bc-heading>div{min-width:0}
#ownerBrigadeControlContent .bc-date{display:block;width:100%;min-width:0;max-width:100%;overflow:hidden}
#ownerBrigadeControlContent .bc-date input{display:block;box-sizing:border-box;width:100%;min-width:0;max-width:100%;height:26px;text-align:center}
#ownerBrigadeControlContent .bc-date input::-webkit-date-and-time-value{min-width:0;text-align:center}
</style>'''


INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', _CONTROL_COMPACT_CSS + '</body>', 1)




# Compact contact controls and consistent report row icons.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('const paths={trash:', 'const paths={ruble:\'<path d="M8 21V3h6a5 5 0 0 1 0 10H5M5 17h10"/>\',trash:', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<dt>₽ Чай</dt>', "<dt>${cabinetIcon('ruble')}Чай</dt>", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('  let deleteButton=null;\n  async function refresh(){', "  let deleteButton=null;\n  const head=qs('#modal .modal-head');head.classList.add('contact-compact-head');\n  const count=document.createElement('span');count.className='contact-header-count';head.querySelector('h3').after(count);\n  const addButton=document.createElement('button');addButton.className='secondary contact-header-action';addButton.innerHTML=cabinetIcon('plus');addButton.setAttribute('aria-label','Добавить связь');addButton.title='Добавить связь';qs('#closeModal').before(addButton);\n  async function refresh(){", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<div class="attention"><div style="display:flex;align-items:center;justify-content:space-between;gap:8px"><h4>Связь по заказам: ${r.totals.brigades_on_contact}/${r.totals.brigades_expected}</h4><button class="secondary small" id="addBrigadeContact">＋ Добавить</button></div><p>За ${esc(day)}. Заказы по времени связи.</p></div>', '', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("    qs('#addBrigadeContact').onclick=()=>openAddBrigadeContact(day,orders.items);", '    count.textContent=`${r.totals.brigades_on_contact}/${r.totals.brigades_expected}`;\n    addButton.onclick=()=>openAddBrigadeContact(day,orders.items);', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("button.className='secondary danger small';button.textContent='Удалить';button.disabled=!confirmed.length;", "button.className='secondary danger contact-header-action';button.innerHTML=cabinetIcon('trash');button.setAttribute('aria-label','Удалить связь');button.title='Удалить связь';button.disabled=!confirmed.length;", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("button.style.cssText='margin-left:auto;flex-shrink:0';", "button.style.cssText='flex-shrink:0';", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="contact-compact-header-style">\n#modal .contact-compact-head{display:flex;align-items:center;gap:6px;margin-bottom:10px}\n#modal .contact-compact-head h3{font-size:clamp(15px,4.1vw,20px);line-height:1.15;min-width:0;margin:0}\n#modal .contact-header-count{font-size:14px;font-weight:700;white-space:nowrap;margin-right:auto;color:var(--gold)}\n#modal .contact-compact-head .contact-header-action,#modal .contact-compact-head #closeModal{box-sizing:border-box;display:inline-flex;align-items:center;justify-content:center;width:34px;height:36px;min-width:34px;padding:6px;flex-shrink:0;margin:0}\n#modal .contact-header-action .cabinet-icon{width:19px;height:19px}\n</style></body>', 1)



# Read brigade lists down each column; offer camera or photo library.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('class="list contact-orders-grid brigade-control-grid">', 'class="list contact-orders-grid brigade-control-grid" style="grid-template-rows:repeat(${Math.max(1,Math.ceil(orders.items.length/2))},auto)">', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('class="list photo-control-grid brigade-control-grid${source===\'gbu\'?\' is-gbu\':\'\'}">', 'class="list photo-control-grid brigade-control-grid${source===\'gbu\'?\' is-gbu\':\'\'}" style="grid-template-rows:repeat(${Math.max(1,Math.ceil(items.length/2))},auto)">', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<label data-photo-file-label>Фото<input name="photo" type="file" accept="image/*" required></label>', '<div data-photo-file-label class="cabinet-photo-source"><span>Фотоотчёт</span><div class="cabinet-photo-source-buttons"><button type="button" class="cabinet-secondary" data-photo-camera>${cabinetIcon(\'camera\')}Сделать фото</button><button type="button" class="cabinet-secondary" data-photo-library>Выбрать из медиатеки</button></div><input name="photo" type="file" accept="image/*" required hidden aria-label="Фотоотчёт"></div>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(" const caption=form.querySelector('[data-photo-caption-fields]'),fileLabel=form.querySelector('[data-photo-file-label]');", " const caption=form.querySelector('[data-photo-caption-fields]'),fileLabel=form.querySelector('[data-photo-file-label]');\n const camera=form.querySelector('[data-photo-camera]'),library=form.querySelector('[data-photo-library]');\n camera.onclick=()=>{if(locked)return;file.setAttribute('capture','environment');file.click()};\n library.onclick=()=>{if(locked)return;file.removeAttribute('capture');file.click()};", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('controls.forEach(c=>c.disabled=locked);notify();', 'controls.forEach(c=>c.disabled=locked);camera.disabled=locked;library.disabled=locked;notify();', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="brigade-columns-and-photo-source-style">\n#modal .brigade-control-grid{position:relative;grid-auto-flow:column;column-gap:14px;isolation:isolate}\n#modal .brigade-control-grid:before{content:"";position:absolute;top:0;bottom:0;left:50%;width:1px;background:var(--line);pointer-events:none;z-index:-1}\n#cabinetNativeForm .cabinet-photo-source-buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:6px 0 12px}\n#cabinetNativeForm .cabinet-photo-source-buttons button{display:flex;align-items:center;justify-content:center;gap:6px;min-height:44px;font-size:13px;white-space:normal}\n</style></body>', 1)



# Category-specific uniform reminders and approved framing controls.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('async function ownerCabinetAction(key,cardId=null){', 'function cabinetUniformText(source,category){\n if(!category)return \'\';\n if(source===\'gbu\')return category===\'elite\'?\'Наденьте галстуки и перчатки.\':\'Наденьте повязки и значки ГБУ ⬛️.\';\n return {standard:\'Наденьте повязки, шевроны и галстуки.\',vip:\'Наденьте повязки, шевроны, галстуки и перчатки.\',elite:\'Наденьте галстуки и белые перчатки.\'}[category]||\'\';\n}\nfunction cabinetUniformFields(){\n return \'<div data-uniform-fields><label>Категория заказа<select name="uniform_category" required><option value="">Выберите категорию</option></select></label><div class="cabinet-photo-reminder" data-uniform-reminder role="note" hidden><strong>❗ Проверьте форму бригады</strong><span data-uniform-text></span><small>Вся бригада — в полный рост. Атрибутика хорошо видна в кадре.</small></div></div>\';\n}\nfunction setupCabinetUniform(form,card){\n const category=form.querySelector(\'[name=uniform_category]\');if(!category)return;\n const source=form.querySelector(\'[name=source]\'),note=form.querySelector(\'[data-uniform-reminder]\');\n const currentSource=()=>source?.value||card?.source||\'private\';\n const show=()=>{const text=cabinetUniformText(currentSource(),category.value);note.hidden=!text;form.querySelector(\'[data-uniform-text]\').textContent=text};\n const fill=value=>{const types=currentSource()===\'gbu\'?[\'standard\',\'elite\']:[\'standard\',\'vip\',\'elite\'];category.innerHTML=\'<option value="">Выберите категорию</option>\'+types.map(x=>`<option value="${x}">${{standard:\'Стандарт\',vip:\'VIP\',elite:\'Элит\'}[x]}</option>`).join(\'\');category.value=types.includes(value)?value:\'\';show()};\n fill(card?.uniform_category||card?.cash?.category||\'\');\n category.onchange=show;if(source)source.addEventListener(\'change\',()=>fill(category.value));\n}\n\nasync function ownerCabinetAction(key,cardId=null){', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("+source('private')+`<p class=", "+source('private')+cabinetUniformFields()+`<p class=", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("+source(card.source)+input('photo_time'", "+source(card.source)+cabinetUniformFields()+input('photo_time'", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<div class="cabinet-photo-reminder" role="note"><strong>❗ Наденьте повязки и шевроны</strong><span>Проверьте, чтобы они были хорошо видны на фото.</span></div>', '${cabinetUniformFields()}', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("  const clockHelp=qs('.cabinet-clock-help',form);", "  setupCabinetUniform(form,card);\n  const clockHelp=qs('.cabinet-clock-help',form);", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('if(!metadataReady())return;', "if(!metadataReady())return;\n  const category=form.querySelector('[name=uniform_category]');if(category&&!category.value){category.reportValidity();return;}", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("cx=600;cy=400;phase='edit';draw();refresh()}", "cx=600;cy=400;phase='edit';draw();refresh();edit.scrollIntoView?.({block:'nearest',behavior:'smooth'})}", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('const x=w<=1200?600:clamp(cx,1200-w/2,w/2),y=h<=800?400:clamp(cy,800-h/2,h/2);', 'const x=w<=1200?clamp(cx,w/2,1200-w/2):clamp(cx,1200-w/2,w/2),y=h<=800?clamp(cy,h/2,800-h/2):clamp(cy,800-h/2,h/2);', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('Поместите всю бригаду в рамку</b><br>Все сотрудники целиком: от головы до обуви.', 'Поместите всю бригаду в золотую рамку</b><br>Все сотрудники целиком. Запас вокруг рамки тоже войдёт в фото.', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('Вся бригада в полный рост, повязки и шевроны видны?', 'Вся бригада в полный рост, нужная атрибутика видна?', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(' const pointers=new Map(),controls=', " const frame=canvas.parentElement;\n for(const type of ['touchstart','touchmove','touchend','touchcancel'])frame.addEventListener(type,event=>{if(event.cancelable)event.preventDefault();event.stopPropagation()},{passive:false});\n const pointers=new Map(),controls=", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="uniform-and-safe-frame-style">\n#cabinetNativeForm .cabinet-photo-frame{border:0;touch-action:none;overscroll-behavior:none;user-select:none;-webkit-user-select:none}\n#cabinetNativeForm .cabinet-photo-frame::after{inset:6% 4%;border:2px solid var(--gold);border-radius:6px;box-shadow:0 0 0 1000px #00000025}\n#cabinetNativeForm .cabinet-photo-frame canvas{overscroll-behavior:none;-webkit-touch-callout:none}\n#cabinetNativeForm [data-uniform-fields]{margin:8px 0 12px}\n#cabinetNativeForm [data-uniform-reminder] small{display:block;color:var(--muted);margin-top:6px;font-size:12px}\n</style></body>', 1)



# Reserve: ready employees first, neutral placeholders, no redundant response counters.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('    if(code===\'reserve\'){\n      const wrotePeople=[\n        ...(g.ready||[]),\n        ...(g.responded||[]),\n        ...(g.not_ready||[]),\n        ...(g.day_off||[])\n      ];\n      return `<div class="group-block">\n        <div class="group-title"><span>${esc(g.label)}</span><span>${answered}/${total}</span></div>\n        <div class="group-stats">\n          <button class="${wrotePeople.length?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="wrote"><b>${wrotePeople.length}</b>Отписались</button>\n          <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готовы</button>\n          <button class="${free?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="responded"><b>${free}</b>Свободная</button>\n          <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n        </div>\n      </div>`;\n    }\n', '    if(code===\'reserve\'){\n      return `<div class="group-block">\n        <div class="group-title"><span>${esc(g.label)}</span><span>${ready}/${total}</span></div>\n        <div class="group-stats">\n          <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готовы</button>\n          <button type="button" disabled aria-label="Не используется"><b>—</b></button>\n          <button type="button" disabled aria-label="Не используется"><b>—</b></button>\n          <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n        </div>\n      </div>`;\n    }\n', 1)



# Swipeable date history, extended in either direction without changing selection.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('    function renderDates() {\n  if(state.bootstrap?.cabinet_only)return;\n  const el = qs(\'#dateStrip\');\n  el.innerHTML = state.bootstrap.dates.map(d => `\n    <button class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}">\n      <strong style="display:block;white-space:nowrap">${esc(shortCalendarDay(d.date))}</strong>${["Сегодня","Завтра"].includes(d.label)?`<small style="display:block;line-height:1.35;margin-top:4px">${esc(d.label)}</small>`:""}<small style="display:block;white-space:nowrap;line-height:1.35;margin-top:4px">${d.count} заказов</small>\n    </button>`).join(\'\');\n  qsa(\'.date-pill\', el).forEach(btn => btn.onclick = async () => {\n    state.selectedDate = btn.dataset.date;\n    await loadBootstrap(state.selectedDate);\n  });\n}\n', 'const dispatchDateCache=new Map();\nlet dispatchDateAccount=null,dispatchDateLoading=false,dispatchDateTimer=null;\nfunction dispatchShiftDay(day,offset){return new Date(Date.parse(day+\'T12:00:00Z\')+offset*86400000).toISOString().slice(0,10)}\nfunction renderDates(){\n if(state.bootstrap?.cabinet_only)return;\n const el=qs(\'#dateStrip\'),uid=state.bootstrap.user?.id;\n if(dispatchDateAccount!==uid){dispatchDateCache.clear();dispatchDateAccount=uid;el.dataset.datePositioned=\'\';}\n const old=[...el.querySelectorAll(\'[data-date]\')];\n const anchor=old.find(x=>x.offsetLeft+x.offsetWidth>el.scrollLeft);\n const anchorDate=anchor?.dataset.date,anchorOffset=anchor?anchor.offsetLeft-el.scrollLeft:0;\n const selectedWasVisible=dispatchDateCache.has(state.selectedDate);\n for(const d of state.bootstrap.dates||[])dispatchDateCache.set(d.date,d);\n for(const d of dispatchDateCache.values()){\n  const today=state.bootstrap.today;d.label=d.date===today?\'Сегодня\':d.date===dispatchShiftDay(today,-1)?\'Вчера\':d.date===dispatchShiftDay(today,1)?\'Завтра\':\'\';\n }\n el.innerHTML=[...dispatchDateCache.values()].sort((a,b)=>a.date.localeCompare(b.date)).map(d=>`<button type="button" class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}" aria-pressed="${d.date===state.selectedDate}"><strong style="display:block;white-space:nowrap">${esc(shortCalendarDay(d.date))}</strong>${d.label?`<small style="display:block;line-height:1.35;margin-top:4px">${esc(d.label)}</small>`:\'\'}<small style="display:block;white-space:nowrap;line-height:1.35;margin-top:4px">${d.count} заказов</small></button>`).join(\'\');\n el.setAttribute(\'aria-label\',\'Выбор даты. Листайте в обе стороны\');\n qsa(\'.date-pill\',el).forEach(btn=>btn.onclick=async()=>{if(btn.dataset.date===state.selectedDate)return;state.selectedDate=btn.dataset.date;await loadBootstrap(state.selectedDate)});\n const selected=el.querySelector(`[data-date="${state.selectedDate}"]`);\n const restored=anchorDate&&el.querySelector(`[data-date="${anchorDate}"]`);\n if(!el.dataset.datePositioned||!selectedWasVisible){if(selected)el.scrollLeft=Math.max(0,selected.offsetLeft-100);el.dataset.datePositioned=\'1\'}\n else if(restored)el.scrollLeft=Math.max(0,restored.offsetLeft-anchorOffset);\n el.onscroll=()=>{clearTimeout(dispatchDateTimer);dispatchDateTimer=setTimeout(()=>extendDispatchDates(el),180)};\n}\nasync function extendDispatchDates(el){\n if(dispatchDateLoading||state.bootstrap?.cabinet_only||!dispatchDateCache.size)return;\n const days=[...dispatchDateCache.keys()].sort();\n const left=el.scrollLeft<160,right=el.scrollWidth-el.clientWidth-el.scrollLeft<160;\n if(!left&&!right)return;\n const uid=dispatchDateAccount,anchor=dispatchShiftDay(left?days[0]:days.at(-1),left?-16:16);\n dispatchDateLoading=true;\n try{\n  const result=await api(\'/api/date-folders?anchor=\'+anchor);\n  if(uid!==state.bootstrap?.user?.id||state.bootstrap?.cabinet_only)return;\n  for(const d of result.dates||[])dispatchDateCache.set(d.date,d);\n  renderDates();\n }catch(error){toast(\'Не удалось загрузить даты. Листните ещё раз.\')}\n finally{dispatchDateLoading=false}\n}\n', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="swipe-dates-style">#dateStrip{overflow-x:auto;overflow-y:hidden;touch-action:pan-x;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;scrollbar-width:none}#dateStrip .date-pill{flex-shrink:0;touch-action:pan-x;user-select:none;-webkit-user-select:none}</style></body>', 1)



# Select a calendar month; swipe through only its days.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('const dispatchDateCache=new Map();\nlet dispatchDateAccount=null,dispatchDateLoading=false,dispatchDateTimer=null;\nfunction dispatchShiftDay(day,offset){return new Date(Date.parse(day+\'T12:00:00Z\')+offset*86400000).toISOString().slice(0,10)}\nfunction renderDates(){\n if(state.bootstrap?.cabinet_only)return;\n const el=qs(\'#dateStrip\'),uid=state.bootstrap.user?.id;\n if(dispatchDateAccount!==uid){dispatchDateCache.clear();dispatchDateAccount=uid;el.dataset.datePositioned=\'\';}\n const old=[...el.querySelectorAll(\'[data-date]\')];\n const anchor=old.find(x=>x.offsetLeft+x.offsetWidth>el.scrollLeft);\n const anchorDate=anchor?.dataset.date,anchorOffset=anchor?anchor.offsetLeft-el.scrollLeft:0;\n const selectedWasVisible=dispatchDateCache.has(state.selectedDate);\n for(const d of state.bootstrap.dates||[])dispatchDateCache.set(d.date,d);\n for(const d of dispatchDateCache.values()){\n  const today=state.bootstrap.today;d.label=d.date===today?\'Сегодня\':d.date===dispatchShiftDay(today,-1)?\'Вчера\':d.date===dispatchShiftDay(today,1)?\'Завтра\':\'\';\n }\n el.innerHTML=[...dispatchDateCache.values()].sort((a,b)=>a.date.localeCompare(b.date)).map(d=>`<button type="button" class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}" aria-pressed="${d.date===state.selectedDate}"><strong style="display:block;white-space:nowrap">${esc(shortCalendarDay(d.date))}</strong>${d.label?`<small style="display:block;line-height:1.35;margin-top:4px">${esc(d.label)}</small>`:\'\'}<small style="display:block;white-space:nowrap;line-height:1.35;margin-top:4px">${d.count} заказов</small></button>`).join(\'\');\n el.setAttribute(\'aria-label\',\'Выбор даты. Листайте в обе стороны\');\n qsa(\'.date-pill\',el).forEach(btn=>btn.onclick=async()=>{if(btn.dataset.date===state.selectedDate)return;state.selectedDate=btn.dataset.date;await loadBootstrap(state.selectedDate)});\n const selected=el.querySelector(`[data-date="${state.selectedDate}"]`);\n const restored=anchorDate&&el.querySelector(`[data-date="${anchorDate}"]`);\n if(!el.dataset.datePositioned||!selectedWasVisible){if(selected)el.scrollLeft=Math.max(0,selected.offsetLeft-100);el.dataset.datePositioned=\'1\'}\n else if(restored)el.scrollLeft=Math.max(0,restored.offsetLeft-anchorOffset);\n el.onscroll=()=>{clearTimeout(dispatchDateTimer);dispatchDateTimer=setTimeout(()=>extendDispatchDates(el),180)};\n}\nasync function extendDispatchDates(el){\n if(dispatchDateLoading||state.bootstrap?.cabinet_only||!dispatchDateCache.size)return;\n const days=[...dispatchDateCache.keys()].sort();\n const left=el.scrollLeft<160,right=el.scrollWidth-el.clientWidth-el.scrollLeft<160;\n if(!left&&!right)return;\n const uid=dispatchDateAccount,anchor=dispatchShiftDay(left?days[0]:days.at(-1),left?-16:16);\n dispatchDateLoading=true;\n try{\n  const result=await api(\'/api/date-folders?anchor=\'+anchor);\n  if(uid!==state.bootstrap?.user?.id||state.bootstrap?.cabinet_only)return;\n  for(const d of result.dates||[])dispatchDateCache.set(d.date,d);\n  renderDates();\n }catch(error){toast(\'Не удалось загрузить даты. Листните ещё раз.\')}\n finally{dispatchDateLoading=false}\n}\n\n', 'let dispatchRenderedMonth=null,dispatchDateAccount=null;\nfunction dispatchShiftDay(day,offset){return new Date(Date.parse(day+\'T12:00:00Z\')+offset*86400000).toISOString().slice(0,10)}\nfunction renderDates(){\n if(state.bootstrap?.cabinet_only)return;\n const el=qs(\'#dateStrip\'),month=state.selectedDate.slice(0,7),uid=state.bootstrap.user?.id;\n const changed=dispatchRenderedMonth!==month||dispatchDateAccount!==uid;\n const oldScroll=el.scrollLeft;\n dispatchRenderedMonth=month;dispatchDateAccount=uid;\n const picker=qs(\'#dispatchMonth\'),label=qs(\'#dispatchMonthLabel\');\n picker.value=month;\n const name=new Date(month+\'-01T12:00:00Z\').toLocaleDateString(\'ru-RU\',{month:\'long\',year:\'numeric\',timeZone:\'Europe/Moscow\'}).replace(/\\s*г\\.$/,\'\');\n label.textContent=name.charAt(0).toUpperCase()+name.slice(1)+\' ▾\';\n picker.onchange=()=>{\n  const selected=picker.value;if(!/^\\d{4}-(0[1-9]|1[0-2])$/.test(selected)||selected===state.selectedDate.slice(0,7))return;\n  const target=selected===state.bootstrap.today.slice(0,7)?state.bootstrap.today:selected+\'-01\';\n  loadBootstrap(target).catch(error=>toast(error.message));\n };\n const dates=(state.bootstrap.dates||[]).filter(d=>d.date.slice(0,7)===month).sort((a,b)=>a.date.localeCompare(b.date));\n el.innerHTML=dates.map(d=>{\n  const today=state.bootstrap.today,label=d.date===today?\'Сегодня\':d.date===dispatchShiftDay(today,-1)?\'Вчера\':d.date===dispatchShiftDay(today,1)?\'Завтра\':\'\';\n  return `<button type="button" class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}" aria-pressed="${d.date===state.selectedDate}"><strong style="display:block;white-space:nowrap">${esc(shortCalendarDay(d.date))}</strong>${label?`<small style="display:block;line-height:1.35;margin-top:4px">${esc(label)}</small>`:\'\'}<small style="display:block;white-space:nowrap;line-height:1.35;margin-top:4px">${d.count} заказов</small></button>`;\n }).join(\'\');\n el.setAttribute(\'aria-label\',\'Дни выбранного месяца. Листайте в обе стороны\');\n qsa(\'.date-pill\',el).forEach(btn=>btn.onclick=async()=>{if(btn.dataset.date===state.selectedDate)return;await loadBootstrap(btn.dataset.date)});\n const selected=el.querySelector(`[data-date="${state.selectedDate}"]`);\n el.scrollLeft=changed&&selected?Math.max(0,selected.offsetLeft-100):oldScroll;\n el.onscroll=null;\n}\n// A fresh page starts with the server\'s current Moscow date. A restored page\n// (back/forward cache) follows the same rule, without resetting normal navigation.\nwindow.addEventListener(\'pageshow\',event=>{\n if(event.persisted&&state.bootstrap&&!state.bootstrap.cabinet_only){dispatchRenderedMonth=null;loadBootstrap().catch(error=>toast(error.message))}\n});\n\n', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<header class="topbar">\n      <div>', '<header class="topbar">\n      <div class="dispatch-date-title">', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('    </header>\n\n    <section class="date-strip" id="dateStrip">', '      <label class="dispatch-month-picker"><span id="dispatchMonthLabel">Месяц ▾</span><input id="dispatchMonth" type="month" min="1900-01" max="9998-12" aria-label="Месяц и год заказов"></label>\n    </header>\n\n    <section class="date-strip" id="dateStrip">', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="dispatch-month-picker-style">\n.topbar:has(.dispatch-month-picker){display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 8px;align-items:center}\n.topbar .dispatch-date-title{display:contents}\n.topbar .dispatch-date-title .eyebrow{grid-column:1;grid-row:1}\n.topbar .dispatch-date-title h1{grid-column:1;grid-row:2;min-width:0}\n.topbar>.dispatch-date-title+div{grid-column:2;grid-row:2}\n.topbar .dispatch-month-picker{grid-column:1/-1;grid-row:1;justify-self:end;position:relative;display:block;margin:0;padding:4px 9px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--gold);font-size:12px;line-height:1.4;min-height:30px;box-sizing:border-box}\n.dispatch-month-picker input{position:absolute;inset:0;width:100%;height:100%;opacity:0;margin:0;font-size:16px;cursor:pointer}\n.dispatch-month-picker:focus-within{outline:2px solid var(--gold);outline-offset:2px}\n.owner-cabinet-open .dispatch-month-picker{display:none!important}\n</style></body>', 1)



# Six date buttons, reset on bottom navigation, compact employee actions.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('let dispatchRenderedMonth=null,dispatchDateAccount=null;', 'function dispatchAlignSelectedDate(){\n const el=qs(\'#dateStrip\'),selected=el?.querySelector(`[data-date="${state.selectedDate}"]`);if(!el||!selected)return;\n const padding=parseFloat(getComputedStyle(el).paddingLeft)||0;\n const x=Math.max(0,selected.offsetLeft-padding);el.scrollLeft=x;\n const remaining=Math.max(0,el.clientWidth-padding-(parseFloat(getComputedStyle(el).paddingRight)||0)-selected.offsetWidth);\n const tail=el.querySelector(\'.date-strip-tail\');if(tail)tail.style.width=remaining+\'px\';el.scrollLeft=x;\n}\nasync function dispatchResetToday(){\n if(state.bootstrap?.cabinet_only)return;\n dispatchRenderedMonth=null;await loadBootstrap();dispatchAlignSelectedDate();\n}\n\nlet dispatchRenderedMonth=null,dispatchDateAccount=null;', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(' el.scrollLeft=changed&&selected?Math.max(0,selected.offsetLeft-100):oldScroll;', " if(changed&&selected){const tail=document.createElement('span');tail.className='date-strip-tail';tail.setAttribute('aria-hidden','true');el.append(tail);dispatchAlignSelectedDate()}else{const tail=document.createElement('span');tail.className='date-strip-tail';tail.setAttribute('aria-hidden','true');el.append(tail);const padding=parseFloat(getComputedStyle(el).paddingLeft)||0;tail.style.width=Math.max(0,el.clientWidth-padding-(parseFloat(getComputedStyle(el).paddingRight)||0)-(selected?.offsetWidth||0))+'px';el.scrollLeft=oldScroll;}", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('  roleNavigationActive=target;updateRoleNavigation();', "  if(['owner','assistant'].includes(kind)){await dispatchResetToday()}\n  roleNavigationActive=target;updateRoleNavigation();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<button class="secondary small" id="gbuAgentsBtn">⬛ Агенты ГБУ</button>', '<button type="button" class="secondary small" id="gbuPayrollBtn">₽ Зарплата ГБУ</button><button class="secondary small" id="gbuAgentsBtn">⬛ Агенты ГБУ</button>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<summary><span><span class="staff-directory-number">', '<summary ${canEdit?`data-employee-profile="${index}"`:""}><span><span class="staff-directory-number">', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('${canEdit?`<button type="button" class="secondary" data-profile-edit="${index}" aria-label="Изменить данные сотрудника">✍🏻</button>`:\'\'}', '', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("qsa('[data-profile-edit]',qs('#employeesList')).forEach(button=>button.onclick=()=>openEmployeeProfile(filtered[Number(button.dataset.profileEdit)],loadEmployees));", "qsa('[data-employee-profile]',qs('#employeesList')).forEach(button=>button.onclick=event=>{event.preventDefault();openEmployeeProfile(filtered[Number(button.dataset.employeeProfile)],loadEmployees)});\n  const payroll=qs('#gbuPayrollBtn');payroll.hidden=!canEdit;payroll.onclick=()=>openGbuPayroll();", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="six-dates-staff-actions-style">\n#dateStrip{--date-gap:4px;gap:var(--date-gap);padding-left:8px;padding-right:8px;position:relative}\n#dateStrip .date-pill{box-sizing:border-box;flex:0 0 calc((100% - 20px)/6);width:calc((100% - 20px)/6);min-width:0!important;padding:7px 2px!important;letter-spacing:0}\n#dateStrip .date-pill strong{font-size:clamp(9px,2.6vw,13px)!important}\n#dateStrip .date-pill small{font-size:clamp(8px,2.3vw,11px)!important;letter-spacing:0}\n#dateStrip .date-strip-tail{display:block;flex:0 0 auto;min-height:1px;pointer-events:none}\n#employeesView>.section-head{gap:8px}\n#employeesView>.section-head>.section-actions{display:flex;gap:6px}\n#employeesView #gbuAgentsBtn,#employeesView #gbuPayrollBtn{font-size:11px;padding:7px 8px;min-height:36px;white-space:normal;line-height:1.2;max-width:100px}\n#employeesView .staff-directory-person.is-editable{grid-template-columns:minmax(0,1fr)}\n#employeesView .staff-directory-person summary{min-height:36px}\n@media(max-width:360px){#employeesView>.section-head{flex-wrap:wrap}#employeesView>.section-head>.section-actions{margin-left:auto}}\n</style></body>', 1)



INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style id="gbu-staff-payroll-style">.gbu-staff-payroll-table{overflow-x:auto}.gbu-staff-payroll-table table{width:100%;border-collapse:collapse;font-size:13px}.gbu-staff-payroll-table td,.gbu-staff-payroll-table th{padding:8px 5px;text-align:left;border-bottom:1px solid var(--line)}.gbu-staff-payroll-table th:first-child{width:60%}.gbu-staff-payroll-table p{font-size:12px;color:var(--muted)}#gbuPayrollBody small{display:block}#gbuPayrollRoster .gbu-roster-choice{display:flex;gap:10px;align-items:center;padding:8px;min-height:40px}#gbuPayrollRoster .gbu-roster-choice input{width:18px;height:18px}#gbuPayrollRoster [hidden]{display:none!important}</style><script>async function openGbuPayroll(month=state.selectedDate.slice(0,7)){\n showModal(\'Зарплата ГБУ\',`<label>Месяц<input id="gbuPayrollMonth" type="month" value="${esc(month)}"></label><div id="gbuPayrollBody">Загрузка…</div>`);\n const body=qs(\'#gbuPayrollBody\'),active=()=>qs(\'#gbuPayrollBody\')===body&&!qs(\'#modal\').classList.contains(\'hidden\');\n qs(\'#gbuPayrollMonth\').onchange=event=>{if(event.target.value)openGbuPayroll(event.target.value)};\n try{\n  const data=await api(\'/api/gbu-staff-payroll?month=\'+month);if(!active())return;\n  body.innerHTML=`<p class="muted">Один заказ — один выход. Повторное фото не добавляет начисление.</p><h3>Начислено: ${esc(cabinetMoney(data.total))} ₽</h3><div class="gbu-staff-payroll-table"><table><thead><tr><th>Сотрудник</th><th>Выходы</th><th>Начислено</th></tr></thead><tbody>${data.employees.map(x=>`<tr><td><details><summary>${esc(x.name)}</summary>${x.orders.map(o=>`<p>${esc(o.date.slice(8)+\'.\'+o.date.slice(5,7))} · ${esc(o.surname)} · ${o.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${esc(cabinetMoney(o.amount))} ₽</p>`).join(\'\')}</details></td><td>${x.exits}</td><td>${esc(cabinetMoney(x.amount))} ₽</td></tr>`).join(\'\')||\'<tr><td colspan="3">Подтверждённых составов пока нет</td></tr>\'}</tbody></table></div>${data.missing.length?`<h4>Укажите состав · ${data.missing.length}</h4><p class="muted">Первое фото отмечено, но состав сотрудников не сохранён. Эти заказы пока не включены в начисления.</p><div class="list">${data.missing.map((o,i)=>`<button type="button" class="secondary" data-payroll-roster="${i}">${esc(o.date.slice(8)+\'.\'+o.date.slice(5,7))} · ${esc(o.surname)}<small>${esc(o.brigadier)}</small></button>`).join(\'\')}</div>`:\'\'}`;\n  qsa(\'[data-payroll-roster]\',body).forEach(b=>b.onclick=()=>openGbuPayrollRoster(data.missing[Number(b.dataset.payrollRoster)],month));\n }catch(error){if(active())body.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){\n showModal(\'Состав ГБУ · \'+order.surname,\'<div id="gbuPayrollRoster">Загрузка…</div>\');const root=qs(\'#gbuPayrollRoster\');\n try{\n  const employees=await api(\'/api/employees\');if(qs(\'#gbuPayrollRoster\')!==root)return;\n  const leader=employees.find(e=>Number(e.tg_id)===Number(order.tg_id));\n  if(!leader){root.textContent=\'Бригадир не найден в списке сотрудников. Проверьте его профиль.\';return}\n  root.innerHTML=`<form id="gbuPayrollRosterForm"><p>${esc(order.brigadier)} · ${esc(order.date)}</p><label>Категория<select name="category" required><option value="">Выберите категорию</option><option value="standard">Стандарт</option><option value="elite">Элит</option></select></label><input class="search" data-roster-search placeholder="Поиск сотрудника"><div class="list">${employees.map(e=>`<label class="gbu-roster-choice" data-roster-name="${esc(readinessEmployeeName(e).toLowerCase())}"><input type="checkbox" value="${e.id}" ${e.id===leader.id?\'checked disabled\':\'\'}>${esc(readinessEmployeeName(e))}${e.id===leader.id?\' · бригадир\':\'\'}</label>`).join(\'\')}</div><p data-roster-status role="status"></p><button type="submit" class="primary">Подтвердить состав и начислить</button></form><button type="button" class="secondary" data-roster-back>Назад</button>`;\n  qs(\'[data-roster-search]\',root).oninput=e=>qsa(\'[data-roster-name]\',root).forEach(row=>row.hidden=!row.dataset.rosterName.includes(e.target.value.toLowerCase().trim()));\n  qs(\'[data-roster-back]\',root).onclick=()=>openGbuPayroll(month);\n  let busy=false;qs(\'#gbuPayrollRosterForm\').onsubmit=async event=>{\n   event.preventDefault();if(busy)return;const form=event.currentTarget,ids=qsa(\'input[type=checkbox]:checked\',form).map(x=>Number(x.value));\n   if(![4,6].includes(ids.length)){qs(\'[data-roster-status]\',root).textContent=\'Выберите 4 или 6 сотрудников вместе с бригадиром\';return}\n   busy=true;const button=qs(\'[type=submit]\',form);button.disabled=true;\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:order.card_id,category:form.elements.category.value,employee_ids:ids})});await openGbuPayroll(month)}catch(error){qs(\'[data-roster-status]\',root).textContent=error.message;busy=false;button.disabled=false}\n  };\n }catch(error){if(qs(\'#gbuPayrollRoster\')===root)root.textContent=error.message}\n}\n</script></body>', 1)



# Align after section layout; allow retrying newly synchronized historical images.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('  await renderCurrentView();\n}\nasync function roleOpenOwn(){', '  await renderCurrentView();\n  if(state.selectedDate===state.bootstrap?.today)requestAnimationFrame(dispatchAlignSelectedDate);\n}\nasync function roleOpenOwn(){', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("qsa('.bottom-nav button').forEach(btn=>btn.onclick=async()=>{qsa('.bottom-nav button').forEach", "qsa('.bottom-nav button').forEach(btn=>btn.onclick=async()=>{if(state.bootstrap&&!state.bootstrap.cabinet_only)await dispatchResetToday();qsa('.bottom-nav button').forEach", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("if(!ref.revision){area.textContent=card[ref.stage===1?'first_received':'second_received']?'Фото '+ref.stage+' ещё не синхронизировано':'Ожидается фото '+ref.stage;return}", "if(!ref.revision&&!card[ref.stage===1?'first_received':'second_received']){area.textContent='Ожидается фото '+ref.stage;return}", 1)



# Keep today identifiable when a different date is selected.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}" aria-pressed="${d.date===state.selectedDate}"', 'class="date-pill ${d.date===state.selectedDate?\'active\':\'\'} ${d.date===today?\'is-today\':\'\'}" data-date="${d.date}" aria-pressed="${d.date===state.selectedDate}"${d.date===today?\' aria-current="date"\':\'\'}', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<small style="display:block;line-height:1.35;margin-top:4px">${esc(label)}</small>', '<small class="${d.date===today?\'date-today-label\':\'\'}" style="display:block;line-height:1.35;margin-top:4px">${esc(label)}</small>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>\n#dateStrip .date-pill.is-today:not(.active){border-color:var(--green);box-shadow:inset 0 0 0 1px var(--green)}\n#dateStrip .date-pill.is-today .date-today-label{color:var(--green);font-weight:700}\n</style></body>', 1)




# GBU card category and crew size come from the owner earnings entry.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("<h2>${esc(card.surname||'—')}</h2><small>Бригадир</small>", '<h2>${esc(card.surname||\'—\')}</h2>${card.source===\'gbu\'&&card.gbu_earning_detail?`<p class="bc-viewer-category">${esc(card.gbu_earning_detail.category===\'elite\'?\'Элит\':\'Стандарт\')} · ${esc(card.gbu_earning_detail.team_size)} ${card.gbu_earning_detail.team_size===4?\'человека\':\'человек\'}</p>`:\'\'}<small>Бригадир</small>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>.bc-viewer-category{display:table;margin:0 0 10px;padding:4px 9px;border-radius:8px;background:var(--gold-soft,#eee4c8);color:var(--gold,#967334);font-size:14px;font-weight:600;line-height:1.3}</style></body>', 1)



# GBU category button opens the date-scoped, exclusive crew picker.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<h2>${esc(card.surname||\'—\')}</h2>${card.source===\'gbu\'&&card.gbu_earning_detail?`<p class="bc-viewer-category">${esc(card.gbu_earning_detail.category===\'elite\'?\'Элит\':\'Стандарт\')} · ${esc(card.gbu_earning_detail.team_size)} ${card.gbu_earning_detail.team_size===4?\'человека\':\'человек\'}</p>`:\'\'}', '<div class="bc-viewer-title-row"><h2>${esc(card.surname||\'—\')}</h2>${card.source===\'gbu\'&&card.gbu_earning_detail?`<button type="button" class="bc-viewer-category" data-gbu-order-roster aria-label="Выбрать состав заказа">${esc(card.gbu_earning_detail.category===\'elite\'?\'Элит\':\'Стандарт\')} · ${esc(card.gbu_earning_detail.team_size)} ${card.gbu_earning_detail.team_size===4?\'человека\':\'человек\'} ›</button>`:\'\'}</div>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(" const close=dialog.querySelector('header button'),area=dialog.querySelector('.bc-viewer-stage')", " const rosterButton=dialog.querySelector('[data-gbu-order-roster]');if(rosterButton)rosterButton.onclick=()=>openGbuOrderRoster(card.id);\n const close=dialog.querySelector('header button'),area=dialog.querySelector('.bc-viewer-stage')", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<script>async function openGbuOrderRoster(cardId,afterSave){\n document.querySelector(\'.gbu-crew-dialog\')?.close();\n const dialog=document.createElement(\'dialog\');dialog.className=\'gbu-crew-dialog\';\n dialog.innerHTML=\'<header class="gbu-crew-head"><h3>Состав заказа</h3><button type="button" data-crew-close aria-label="Закрыть">×</button></header><div data-crew-body>Загрузка…</div>\';\n dialog.querySelector(\'[data-crew-close]\').onclick=()=>dialog.close();dialog.addEventListener(\'close\',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();\n const root=dialog.querySelector(\'[data-crew-body]\');\n try{\n  const data=await api(\'/api/gbu-staff-payroll/roster/\'+cardId);if(!dialog.open)return;\n  const detail=data.confirmed_detail||data.detail;\n  if(!detail){root.textContent=\'Сначала сохраните категорию и количество человек в заработке ГБУ.\';return}\n  if(!data.leader){root.textContent=\'Бригадир не найден. Проверьте его профиль.\';return}\n  const need=detail.team_size-1;\n  root.innerHTML=`<p><b>${esc(data.surname)}</b> · ${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</p><p class="muted">${esc(data.date)} · Сотрудники, отписавшиеся на эту дату</p><form data-crew-form><p><b>Бригадир: ${esc(readinessEmployeeName(data.leader))}</b></p><p>${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p><input type="search" class="search" data-crew-search placeholder="Поиск по имени и метро"><div data-crew-list>${data.employees.filter(e=>!e.leader).map((e,i)=>`<label class="gbu-crew-choice ${e.busy?\'is-busy\':\'\'}" data-crew-name="${esc((readinessEmployeeName(e)+\' \'+(e.metro||\'\')).toLowerCase())}"><input type="checkbox" value="${e.id}" ${e.selected?\'checked\':\'\'} ${e.busy||data.confirmed?\'disabled\':\'\'}><span><b data-crew-number>${i+1}.</b> ${esc(readinessEmployeeName(e))}<small>${e.busy?\'Занят в другом заказе\':esc(({ready:\'Готов\',not_ready:\'На основной\',day_off:\'Выходной\',responded:\'Отписался\'})[e.readiness_status]||\'\')}</small></span></label>`).join(\'\')}</div><p data-crew-error role="alert" class="gbu-crew-message"></p><footer class="gbu-crew-footer"><p data-crew-count role="status"></p><button type="submit" class="primary" ${data.confirmed?\'hidden\':\'\'}>Подтвердить состав</button></footer></form>`;\n  const form=root.querySelector(\'[data-crew-form]\'),button=form.querySelector(\'[type=submit]\'),counter=form.querySelector(\'[data-crew-count]\'),error=form.querySelector(\'[data-crew-error]\');\n  const layoutCrewRows=()=>{const rows=[...form.querySelectorAll(\'[data-crew-name]\')].filter(r=>!r.hidden),half=Math.ceil(rows.length/2);rows.forEach((r,i)=>{r.style.gridColumn=i<half?\'1\':\'2\';r.style.gridRow=String(i%half+1)})};layoutCrewRows();\n  const boxes=[...form.querySelectorAll(\'input[type=checkbox]\')];let busy=false;\n  const selected=()=>boxes.filter(x=>x.checked).map(x=>Number(x.value));\n  const update=()=>{const n=selected().length;counter.textContent=\'Выбрано \'+n+\' из \'+need+\' сотрудников\';button.disabled=busy||data.confirmed||n!==need||!data.photo_received||data.leader.busy;boxes.forEach(box=>{const emp=data.employees.find(e=>e.id===Number(box.value));box.disabled=data.confirmed||busy||emp.busy||(!box.checked&&n>=need)});};\n  if(!data.photo_received)error.textContent=\'Подтверждение состава доступно после первого фото.\';\n  if(data.leader.busy)error.textContent=\'Бригадир уже подтверждён в другом заказе на эту дату.\';\n  boxes.forEach(box=>box.onchange=update);update();\n  form.querySelector(\'[data-crew-search]\').oninput=event=>{const term=event.target.value.toLowerCase().trim();form.querySelectorAll(\'[data-crew-name]\').forEach(row=>{row.hidden=!row.dataset.crewName.includes(term)});layoutCrewRows()};\n  form.onsubmit=async event=>{event.preventDefault();if(button.disabled||busy)return;const ids=selected();if(ids.length!==need)return;busy=true;update();error.textContent=\'\';\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:cardId,category:detail.category,employee_ids:[data.leader.id,...ids]})});if(!dialog.open)return;dialog.close();if(afterSave)await afterSave();else await openGbuOrderRoster(cardId)}catch(err){if(!dialog.open)return;error.textContent=err.message;busy=false;update()}\n  };\n }catch(error){if(dialog.open)root.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){return openGbuOrderRoster(order.card_id,()=>openGbuPayroll(month))}\n</script><style>\n.bc-viewer-title-row{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:12px}\n.bc-viewer-title-row h2{min-width:0;overflow-wrap:anywhere;margin:0;font-size:clamp(20px,5vw,28px)}\n.bc-viewer-title-row .bc-viewer-category{flex:0 0 auto;max-width:49%;margin:0;border:0;min-height:40px;text-align:center;cursor:pointer;font-size:12px;white-space:normal}\n.gbu-crew-dialog{box-sizing:border-box;width:calc(100% - 24px);max-width:540px;max-height:88dvh;overflow:auto;border:1px solid #b49a60;border-radius:20px;padding:16px;background:var(--card,#f7f5ef);color:var(--text,#30342d)}\n.gbu-crew-dialog::backdrop{background:#0008}.gbu-crew-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.gbu-crew-head h3{margin:0}.gbu-crew-head button{min-width:44px;min-height:44px}\n.gbu-crew-choice{display:flex;gap:10px;align-items:center;padding:11px 4px;border-bottom:1px solid #d8d8d0;cursor:pointer}.gbu-crew-choice input{width:21px;height:21px;flex:0 0 auto;accent-color:#987838}.gbu-crew-choice span{min-width:0}.gbu-crew-choice small{display:block;font-size:12px;color:#777}.gbu-crew-choice.is-busy{opacity:.55}.gbu-crew-footer{position:sticky;bottom:-16px;background:var(--card,#f7f5ef);padding:10px 0 16px}.gbu-crew-footer button{width:100%;min-height:44px}.gbu-crew-message{color:#a7463e}\n</style></body>', 1)



# Crew checklist follows the shared two-column, top-to-bottom directory layout.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', """<style>
.gbu-crew-choice[hidden]{display:none}
[data-crew-list]{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:14px;position:relative}
[data-crew-list]::before{content:'';position:absolute;left:50%;top:0;bottom:0;width:1px;background:#d8d8d0;pointer-events:none}
.gbu-crew-choice{min-width:0;align-items:flex-start;font-size:13px;line-height:1.35;overflow-wrap:anywhere}
.gbu-crew-choice small{font-size:11px}.gbu-crew-choice input{width:19px;height:19px;margin:1px 0 0}
</style></body>""", 1)



# Ready-only grouped crew choices with right-aligned checkboxes.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('async function openGbuOrderRoster(cardId,afterSave){\n document.querySelector(\'.gbu-crew-dialog\')?.close();\n const dialog=document.createElement(\'dialog\');dialog.className=\'gbu-crew-dialog\';\n dialog.innerHTML=\'<header class="gbu-crew-head"><h3>Состав заказа</h3><button type="button" data-crew-close aria-label="Закрыть">×</button></header><div data-crew-body>Загрузка…</div>\';\n dialog.querySelector(\'[data-crew-close]\').onclick=()=>dialog.close();dialog.addEventListener(\'close\',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();\n const root=dialog.querySelector(\'[data-crew-body]\');\n try{\n  const data=await api(\'/api/gbu-staff-payroll/roster/\'+cardId);if(!dialog.open)return;\n  const detail=data.confirmed_detail||data.detail;\n  if(!detail){root.textContent=\'Сначала сохраните категорию и количество человек в заработке ГБУ.\';return}\n  if(!data.leader){root.textContent=\'Бригадир не найден. Проверьте его профиль.\';return}\n  const need=detail.team_size-1;\n  root.innerHTML=`<p><b>${esc(data.surname)}</b> · ${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</p><p class="muted">${esc(data.date)} · Сотрудники, отписавшиеся на эту дату</p><form data-crew-form><p><b>Бригадир: ${esc(readinessEmployeeName(data.leader))}</b></p><p>${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p><input type="search" class="search" data-crew-search placeholder="Поиск по имени и метро"><div data-crew-list>${data.employees.filter(e=>!e.leader).map((e,i)=>`<label class="gbu-crew-choice ${e.busy?\'is-busy\':\'\'}" data-crew-name="${esc((readinessEmployeeName(e)+\' \'+(e.metro||\'\')).toLowerCase())}"><input type="checkbox" value="${e.id}" ${e.selected?\'checked\':\'\'} ${e.busy||data.confirmed?\'disabled\':\'\'}><span><b data-crew-number>${i+1}.</b> ${esc(readinessEmployeeName(e))}<small>${e.busy?\'Занят в другом заказе\':esc(({ready:\'Готов\',not_ready:\'На основной\',day_off:\'Выходной\',responded:\'Отписался\'})[e.readiness_status]||\'\')}</small></span></label>`).join(\'\')}</div><p data-crew-error role="alert" class="gbu-crew-message"></p><footer class="gbu-crew-footer"><p data-crew-count role="status"></p><button type="submit" class="primary" ${data.confirmed?\'hidden\':\'\'}>Подтвердить состав</button></footer></form>`;\n  const form=root.querySelector(\'[data-crew-form]\'),button=form.querySelector(\'[type=submit]\'),counter=form.querySelector(\'[data-crew-count]\'),error=form.querySelector(\'[data-crew-error]\');\n  const layoutCrewRows=()=>{const rows=[...form.querySelectorAll(\'[data-crew-name]\')].filter(r=>!r.hidden),half=Math.ceil(rows.length/2);rows.forEach((r,i)=>{r.style.gridColumn=i<half?\'1\':\'2\';r.style.gridRow=String(i%half+1)})};layoutCrewRows();\n  const boxes=[...form.querySelectorAll(\'input[type=checkbox]\')];let busy=false;\n  const selected=()=>boxes.filter(x=>x.checked).map(x=>Number(x.value));\n  const update=()=>{const n=selected().length;counter.textContent=\'Выбрано \'+n+\' из \'+need+\' сотрудников\';button.disabled=busy||data.confirmed||n!==need||!data.photo_received||data.leader.busy;boxes.forEach(box=>{const emp=data.employees.find(e=>e.id===Number(box.value));box.disabled=data.confirmed||busy||emp.busy||(!box.checked&&n>=need)});};\n  if(!data.photo_received)error.textContent=\'Подтверждение состава доступно после первого фото.\';\n  if(data.leader.busy)error.textContent=\'Бригадир уже подтверждён в другом заказе на эту дату.\';\n  boxes.forEach(box=>box.onchange=update);update();\n  form.querySelector(\'[data-crew-search]\').oninput=event=>{const term=event.target.value.toLowerCase().trim();form.querySelectorAll(\'[data-crew-name]\').forEach(row=>{row.hidden=!row.dataset.crewName.includes(term)});layoutCrewRows()};\n  form.onsubmit=async event=>{event.preventDefault();if(button.disabled||busy)return;const ids=selected();if(ids.length!==need)return;busy=true;update();error.textContent=\'\';\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:cardId,category:detail.category,employee_ids:[data.leader.id,...ids]})});if(!dialog.open)return;dialog.close();if(afterSave)await afterSave();else await openGbuOrderRoster(cardId)}catch(err){if(!dialog.open)return;error.textContent=err.message;busy=false;update()}\n  };\n }catch(error){if(dialog.open)root.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){return openGbuOrderRoster(order.card_id,()=>openGbuPayroll(month))}\n', 'async function openGbuOrderRoster(cardId,afterSave){\n document.querySelector(\'.gbu-crew-dialog\')?.close();\n const dialog=document.createElement(\'dialog\');dialog.className=\'gbu-crew-dialog\';\n dialog.innerHTML=\'<header class="gbu-crew-head"><h3>Состав заказа</h3><button type="button" data-crew-close aria-label="Закрыть">×</button></header><div data-crew-body>Загрузка…</div>\';\n dialog.querySelector(\'[data-crew-close]\').onclick=()=>dialog.close();dialog.addEventListener(\'close\',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();\n const root=dialog.querySelector(\'[data-crew-body]\');\n try{\n  const data=await api(\'/api/gbu-staff-payroll/roster/\'+cardId);if(!dialog.open)return;\n  const detail=data.confirmed_detail||data.detail;\n  if(!detail){root.textContent=\'Сначала сохраните категорию и количество человек в заработке ГБУ.\';return}\n  if(!data.leader){root.textContent=\'Бригадир не найден. Проверьте его профиль.\';return}\n  const need=detail.team_size-1;\n  const crewGroups=[[\'brigadier\',\'Бригадиры\'],[\'main\',\'Основной состав\'],[\'cashless\',\'Безнал состав\'],[\'reserve\',\'Резерв\']];\n  const crewSections=crewGroups.map(([code,label])=>{const people=data.employees.filter(e=>!e.leader&&e.group_code===code&&(e.readiness_status===\'ready\'||(data.confirmed&&e.selected)));return `<section class="gbu-crew-group" data-crew-group><h4>${esc(label)} · ${people.length}</h4><div data-crew-list>${people.map((e,i)=>`<label class="gbu-crew-choice ${e.busy?\'is-busy\':\'\'}" data-crew-name="${esc((readinessEmployeeName(e)+\' \'+(e.metro||\'\')).toLowerCase())}"><span><b data-crew-number>${i+1}.</b> ${esc(readinessEmployeeName(e))}<small>${e.busy?\'Занят в другом заказе\':e.readiness_status===\'ready\'?\'Готов\':\'В подтверждённом составе\'}</small></span><input type="checkbox" value="${e.id}" ${e.selected?\'checked\':\'\'} ${e.busy||data.confirmed?\'disabled\':\'\'}></label>`).join(\'\')}</div>${people.length?\'\':\'<small class="muted">Готовых сотрудников нет</small>\'}</section>`}).join(\'\');\n  root.innerHTML=`<p><b>${esc(data.surname)}</b> · ${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</p><p class="muted">${esc(data.date)} · Готовые сотрудники на эту дату</p><form data-crew-form><p><b>Бригадир: ${esc(readinessEmployeeName(data.leader))}</b></p><p>${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p><input type="search" class="search" data-crew-search placeholder="Поиск по имени и метро">${crewSections}<p data-crew-error role="alert" class="gbu-crew-message"></p><footer class="gbu-crew-footer"><p data-crew-count role="status"></p><button type="submit" class="primary" ${data.confirmed?\'hidden\':\'\'}>Подтвердить состав</button></footer></form>`;\n  const form=root.querySelector(\'[data-crew-form]\'),button=form.querySelector(\'[type=submit]\'),counter=form.querySelector(\'[data-crew-count]\'),error=form.querySelector(\'[data-crew-error]\');\n  const layoutCrewRows=()=>{form.querySelectorAll(\'[data-crew-list]\').forEach(list=>{const rows=[...list.querySelectorAll(\'[data-crew-name]\')].filter(r=>!r.hidden),half=Math.ceil(rows.length/2);rows.forEach((r,i)=>{r.style.gridColumn=i<half?\'1\':\'2\';r.style.gridRow=String(i%half+1)})})};layoutCrewRows();\n  const boxes=[...form.querySelectorAll(\'input[type=checkbox]\')];let busy=false;\n  const selected=()=>boxes.filter(x=>x.checked).map(x=>Number(x.value));\n  const update=()=>{const n=selected().length;counter.textContent=\'Выбрано \'+n+\' из \'+need+\' сотрудников\';button.disabled=busy||data.confirmed||n!==need||!data.photo_received||data.leader.busy;boxes.forEach(box=>{const emp=data.employees.find(e=>e.id===Number(box.value));box.disabled=data.confirmed||busy||emp.busy||(!box.checked&&n>=need)});};\n  if(!data.photo_received)error.textContent=\'Подтверждение состава доступно после первого фото.\';\n  if(data.leader.busy)error.textContent=\'Бригадир уже подтверждён в другом заказе на эту дату.\';\n  boxes.forEach(box=>box.onchange=update);update();\n  form.querySelector(\'[data-crew-search]\').oninput=event=>{const term=event.target.value.toLowerCase().trim();form.querySelectorAll(\'[data-crew-name]\').forEach(row=>{row.hidden=!row.dataset.crewName.includes(term)});layoutCrewRows()};\n  form.onsubmit=async event=>{event.preventDefault();if(button.disabled||busy)return;const ids=selected();if(ids.length!==need)return;busy=true;update();error.textContent=\'\';\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:cardId,category:detail.category,employee_ids:[data.leader.id,...ids]})});if(!dialog.open)return;dialog.close();if(afterSave)await afterSave();else await openGbuOrderRoster(cardId)}catch(err){if(!dialog.open)return;error.textContent=err.message;busy=false;update()}\n  };\n }catch(error){if(dialog.open)root.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){return openGbuOrderRoster(order.card_id,()=>openGbuPayroll(month))}\n', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('return `${name} ${metro} ${height} см`;', 'return `${name} ${metro} ${height}`;', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("return hasHeight?`${name} ${Number(employee.height_cm)||'—'} см`:name;", "return hasHeight?`${name} ${Number(employee.height_cm)||'—'}`:name;", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>.gbu-crew-group{margin:14px 0}.gbu-crew-group h4{margin:0 0 5px;padding:7px 9px;background:var(--gold-soft,#eee4c8);border-radius:8px;font-size:14px}.gbu-crew-choice{justify-content:space-between;align-items:center;gap:6px}.gbu-crew-choice>span{flex:1}.gbu-crew-choice input{margin:0}.gbu-crew-choice[hidden]{display:none}</style></body>', 1)



# Rectangular gold action for the GBU order category and crew.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>\n.bc-photo-viewer .bc-viewer-title-row button.bc-viewer-category{\n appearance:none;-webkit-appearance:none;box-sizing:border-box;\n border:1px solid #b49a60;border-radius:9px;\n background:#eee3c6;color:#8b6b30;padding:8px 10px;\n min-height:40px;line-height:1.3;font-weight:700;box-shadow:none;\n}\n.bc-photo-viewer .bc-viewer-title-row button.bc-viewer-category:active{background:#e5d3a4}\n.bc-photo-viewer .bc-viewer-title-row button.bc-viewer-category:focus-visible{outline:2px solid #a7853f;outline-offset:2px}\n</style></body>', 1)



# Ready status is implicit in the ready-only crew picker.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("<small>${e.busy?'Занят в другом заказе':e.readiness_status==='ready'?'Готов':'В подтверждённом составе'}</small>", "${e.busy?'<small>Занят в другом заказе</small>':e.readiness_status!=='ready'?'<small>В подтверждённом составе</small>':''}", 1)



# Compact crew header: gold order badge, fixed leader, then search.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('<p><b>${esc(data.surname)}</b> · ${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</p><p class="muted">${esc(data.date)} · Готовые сотрудники на эту дату</p><form data-crew-form><p><b>Бригадир: ${esc(readinessEmployeeName(data.leader))}</b></p><p>${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p>', '<div class="gbu-crew-order-badge"><b>${esc(data.surname)}</b><span>${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</span></div><form data-crew-form><div class="gbu-crew-leader"><small>Бригадир</small><b>${esc(readinessEmployeeName(data.leader))}</b></div><p class="gbu-crew-instruction">${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p>', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>\n.gbu-crew-dialog .gbu-crew-head{margin-bottom:8px;gap:8px}\n.gbu-crew-dialog .gbu-crew-head h3{font-size:19px;line-height:1.25}\n.gbu-crew-dialog .gbu-crew-head button{width:38px;min-width:38px;height:38px;min-height:38px;padding:0;border-radius:10px;font-size:22px}\n.gbu-crew-order-badge{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:4px 10px;padding:8px 10px;border:1px solid #b49a60;border-radius:9px;background:#eee3c6;color:#8b6b30;font-size:13px;line-height:1.3;margin-bottom:9px}\n.gbu-crew-order-badge b{min-width:0;overflow-wrap:anywhere;font-size:14px}.gbu-crew-order-badge span{font-weight:600}\n.gbu-crew-leader{display:flex;align-items:baseline;flex-wrap:wrap;gap:3px 7px;font-size:14px;line-height:1.35}\n.gbu-crew-leader small{font-size:12px;color:#777}.gbu-crew-instruction{margin:6px 0 8px;font-size:13px}\n.gbu-crew-dialog [data-crew-search]{box-sizing:border-box;width:100%;min-height:40px;padding:8px 10px;font-size:16px;border-radius:10px;margin:0}\n.gbu-crew-dialog .gbu-crew-group{margin:10px 0}\n</style></body>', 1)




# Prefix suggestions sit directly below search; omit empty group headings.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("  form.querySelector('[data-crew-search]').oninput=event=>{const term=event.target.value.toLowerCase().trim();form.querySelectorAll('[data-crew-name]').forEach(row=>{row.hidden=!row.dataset.crewName.includes(term)});layoutCrewRows()};", "  const normalizeCrewSearch=value=>String(value||'').toLocaleLowerCase('ru-RU').replace(/ё/g,'е').trim();\n  const search=form.querySelector('[data-crew-search]');search.setAttribute('autocomplete','off');search.setAttribute('autocorrect','off');search.setAttribute('spellcheck','false');\n  const noMatches=document.createElement('p');noMatches.className='muted gbu-crew-no-matches';noMatches.setAttribute('role','status');noMatches.hidden=true;noMatches.textContent='Сотрудники не найдены';search.after(noMatches);\n  search.oninput=event=>{\n   const term=normalizeCrewSearch(event.target.value),tokens=term.split(/\\s+/).filter(Boolean);let matches=0;\n   form.classList.toggle('is-searching',!!term);\n   form.querySelectorAll('[data-crew-name]').forEach(row=>{const words=normalizeCrewSearch(row.dataset.crewName).split(/\\s+/);row.hidden=!tokens.every(token=>words.some(word=>word.startsWith(token)));if(!row.hidden)matches++});\n   form.querySelectorAll('[data-crew-group]').forEach(group=>{group.hidden=!!term&&![...group.querySelectorAll('[data-crew-name]')].some(row=>!row.hidden)});\n   noMatches.hidden=!term||matches>0;layoutCrewRows();\n  };", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>\n.gbu-crew-group[hidden],.gbu-crew-no-matches[hidden]{display:none}\n.gbu-crew-dialog .is-searching .gbu-crew-group{margin-top:4px;margin-bottom:6px}\n.gbu-crew-dialog .is-searching .gbu-crew-group h4{padding:5px 8px;margin-bottom:0}\n.gbu-crew-dialog .is-searching .gbu-crew-choice{padding-top:8px;padding-bottom:8px}\n.gbu-crew-no-matches{font-size:13px;margin:8px 0}\n</style></body>', 1)




# Inline confirmed crew disclosure with an explicit edit action.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('async function openGbuOrderRoster(cardId,afterSave){\n document.querySelector(\'.gbu-crew-dialog\')?.close();\n const dialog=document.createElement(\'dialog\');dialog.className=\'gbu-crew-dialog\';\n dialog.innerHTML=\'<header class="gbu-crew-head"><h3>Состав заказа</h3><button type="button" data-crew-close aria-label="Закрыть">×</button></header><div data-crew-body>Загрузка…</div>\';\n dialog.querySelector(\'[data-crew-close]\').onclick=()=>dialog.close();dialog.addEventListener(\'close\',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();\n const root=dialog.querySelector(\'[data-crew-body]\');\n try{\n  const data=await api(\'/api/gbu-staff-payroll/roster/\'+cardId);if(!dialog.open)return;\n  const detail=data.confirmed_detail||data.detail;\n  if(!detail){root.textContent=\'Сначала сохраните категорию и количество человек в заработке ГБУ.\';return}\n  if(!data.leader){root.textContent=\'Бригадир не найден. Проверьте его профиль.\';return}\n  const need=detail.team_size-1;\n  const crewGroups=[[\'brigadier\',\'Бригадиры\'],[\'main\',\'Основной состав\'],[\'cashless\',\'Безнал состав\'],[\'reserve\',\'Резерв\']];\n  const crewSections=crewGroups.map(([code,label])=>{const people=data.employees.filter(e=>!e.leader&&e.group_code===code&&(e.readiness_status===\'ready\'||(data.confirmed&&e.selected)));return `<section class="gbu-crew-group" data-crew-group><h4>${esc(label)} · ${people.length}</h4><div data-crew-list>${people.map((e,i)=>`<label class="gbu-crew-choice ${e.busy?\'is-busy\':\'\'}" data-crew-name="${esc((readinessEmployeeName(e)+\' \'+(e.metro||\'\')).toLowerCase())}"><span><b data-crew-number>${i+1}.</b> ${esc(readinessEmployeeName(e))}${e.busy?\'<small>Занят в другом заказе</small>\':e.readiness_status!==\'ready\'?\'<small>В подтверждённом составе</small>\':\'\'}</span><input type="checkbox" value="${e.id}" ${e.selected?\'checked\':\'\'} ${e.busy||data.confirmed?\'disabled\':\'\'}></label>`).join(\'\')}</div>${people.length?\'\':\'<small class="muted">Готовых сотрудников нет</small>\'}</section>`}).join(\'\');\n  root.innerHTML=`<div class="gbu-crew-order-badge"><b>${esc(data.surname)}</b><span>${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</span></div><form data-crew-form><div class="gbu-crew-leader"><small>Бригадир</small><b>${esc(readinessEmployeeName(data.leader))}</b></div><p class="gbu-crew-instruction">${data.confirmed?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p><input type="search" class="search" data-crew-search placeholder="Поиск по имени и метро">${crewSections}<p data-crew-error role="alert" class="gbu-crew-message"></p><footer class="gbu-crew-footer"><p data-crew-count role="status"></p><button type="submit" class="primary" ${data.confirmed?\'hidden\':\'\'}>Подтвердить состав</button></footer></form>`;\n  const form=root.querySelector(\'[data-crew-form]\'),button=form.querySelector(\'[type=submit]\'),counter=form.querySelector(\'[data-crew-count]\'),error=form.querySelector(\'[data-crew-error]\');\n  const layoutCrewRows=()=>{form.querySelectorAll(\'[data-crew-list]\').forEach(list=>{const rows=[...list.querySelectorAll(\'[data-crew-name]\')].filter(r=>!r.hidden),half=Math.ceil(rows.length/2);rows.forEach((r,i)=>{r.style.gridColumn=i<half?\'1\':\'2\';r.style.gridRow=String(i%half+1)})})};layoutCrewRows();\n  const boxes=[...form.querySelectorAll(\'input[type=checkbox]\')];let busy=false;\n  const selected=()=>boxes.filter(x=>x.checked).map(x=>Number(x.value));\n  const update=()=>{const n=selected().length;counter.textContent=\'Выбрано \'+n+\' из \'+need+\' сотрудников\';button.disabled=busy||data.confirmed||n!==need||!data.photo_received||data.leader.busy;boxes.forEach(box=>{const emp=data.employees.find(e=>e.id===Number(box.value));box.disabled=data.confirmed||busy||emp.busy||(!box.checked&&n>=need)});};\n  if(!data.photo_received)error.textContent=\'Подтверждение состава доступно после первого фото.\';\n  if(data.leader.busy)error.textContent=\'Бригадир уже подтверждён в другом заказе на эту дату.\';\n  boxes.forEach(box=>box.onchange=update);update();\n  const normalizeCrewSearch=value=>String(value||\'\').toLocaleLowerCase(\'ru-RU\').replace(/ё/g,\'е\').trim();\n  const search=form.querySelector(\'[data-crew-search]\');search.setAttribute(\'autocomplete\',\'off\');search.setAttribute(\'autocorrect\',\'off\');search.setAttribute(\'spellcheck\',\'false\');\n  const noMatches=document.createElement(\'p\');noMatches.className=\'muted gbu-crew-no-matches\';noMatches.setAttribute(\'role\',\'status\');noMatches.hidden=true;noMatches.textContent=\'Сотрудники не найдены\';search.after(noMatches);\n  search.oninput=event=>{\n   const term=normalizeCrewSearch(event.target.value),tokens=term.split(/\\s+/).filter(Boolean);let matches=0;\n   form.classList.toggle(\'is-searching\',!!term);\n   form.querySelectorAll(\'[data-crew-name]\').forEach(row=>{const words=normalizeCrewSearch(row.dataset.crewName).split(/\\s+/);row.hidden=!tokens.every(token=>words.some(word=>word.startsWith(token)));if(!row.hidden)matches++});\n   form.querySelectorAll(\'[data-crew-group]\').forEach(group=>{group.hidden=!!term&&![...group.querySelectorAll(\'[data-crew-name]\')].some(row=>!row.hidden)});\n   noMatches.hidden=!term||matches>0;layoutCrewRows();\n  };\n  form.onsubmit=async event=>{event.preventDefault();if(button.disabled||busy)return;const ids=selected();if(ids.length!==need)return;busy=true;update();error.textContent=\'\';\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:cardId,category:detail.category,employee_ids:[data.leader.id,...ids]})});if(!dialog.open)return;dialog.close();if(afterSave)await afterSave();else await openGbuOrderRoster(cardId)}catch(err){if(!dialog.open)return;error.textContent=err.message;busy=false;update()}\n  };\n }catch(error){if(dialog.open)root.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){return openGbuOrderRoster(order.card_id,()=>openGbuPayroll(month))}\n', 'async function openGbuOrderRoster(cardId,afterSave,editing=false){\n document.querySelector(\'.gbu-crew-dialog\')?.close();\n const dialog=document.createElement(\'dialog\');dialog.className=\'gbu-crew-dialog\';\n dialog.innerHTML=\'<header class="gbu-crew-head"><h3>Состав заказа</h3><button type="button" data-crew-close aria-label="Закрыть">×</button></header><div data-crew-body>Загрузка…</div>\';\n dialog.querySelector(\'[data-crew-close]\').onclick=()=>dialog.close();dialog.addEventListener(\'close\',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();\n const root=dialog.querySelector(\'[data-crew-body]\');\n try{\n  const data=await api(\'/api/gbu-staff-payroll/roster/\'+cardId);if(!dialog.open)return;\n  const readonly=data.confirmed&&!editing;\n  const detail=editing?(data.detail||data.confirmed_detail):(data.confirmed_detail||data.detail);\n  if(!detail){root.textContent=\'Сначала сохраните категорию и количество человек в заработке ГБУ.\';return}\n  if(!data.leader){root.textContent=\'Бригадир не найден. Проверьте его профиль.\';return}\n  const need=detail.team_size-1;\n  const crewGroups=[[\'brigadier\',\'Бригадиры\'],[\'main\',\'Основной состав\'],[\'cashless\',\'Безнал состав\'],[\'reserve\',\'Резерв\']];\n  const crewSections=crewGroups.map(([code,label])=>{const people=data.employees.filter(e=>!e.leader&&e.group_code===code&&(e.readiness_status===\'ready\'||(data.confirmed&&e.selected)));return `<section class="gbu-crew-group" data-crew-group><h4>${esc(label)} · ${people.length}</h4><div data-crew-list>${people.map((e,i)=>`<label class="gbu-crew-choice ${e.busy?\'is-busy\':\'\'}" data-crew-name="${esc((readinessEmployeeName(e)+\' \'+(e.metro||\'\')).toLowerCase())}"><span><b data-crew-number>${i+1}.</b> ${esc(readinessEmployeeName(e))}${e.busy?\'<small>Занят в другом заказе</small>\':e.readiness_status!==\'ready\'?\'<small>В подтверждённом составе</small>\':\'\'}</span><input type="checkbox" value="${e.id}" ${e.selected?\'checked\':\'\'} ${e.busy||readonly?\'disabled\':\'\'}></label>`).join(\'\')}</div>${people.length?\'\':\'<small class="muted">Готовых сотрудников нет</small>\'}</section>`}).join(\'\');\n  root.innerHTML=`<div class="gbu-crew-order-badge"><b>${esc(data.surname)}</b><span>${detail.category===\'elite\'?\'Элит\':\'Стандарт\'} · ${detail.team_size} чел.</span></div><form data-crew-form><div class="gbu-crew-leader"><small>Бригадир</small><b>${esc(readinessEmployeeName(data.leader))}</b></div><p class="gbu-crew-instruction">${readonly?\'Состав подтверждён\':\'Добавьте ещё \'+need+\' сотрудников\'}</p><input type="search" class="search" data-crew-search placeholder="Поиск по имени и метро">${crewSections}<p data-crew-error role="alert" class="gbu-crew-message"></p><footer class="gbu-crew-footer"><p data-crew-count role="status"></p><button type="submit" class="primary" ${readonly?\'hidden\':\'\'}>Подтвердить состав</button></footer></form>`;\n  const form=root.querySelector(\'[data-crew-form]\'),button=form.querySelector(\'[type=submit]\'),counter=form.querySelector(\'[data-crew-count]\'),error=form.querySelector(\'[data-crew-error]\');\n  const layoutCrewRows=()=>{form.querySelectorAll(\'[data-crew-list]\').forEach(list=>{const rows=[...list.querySelectorAll(\'[data-crew-name]\')].filter(r=>!r.hidden),half=Math.ceil(rows.length/2);rows.forEach((r,i)=>{r.style.gridColumn=i<half?\'1\':\'2\';r.style.gridRow=String(i%half+1)})})};layoutCrewRows();\n  const boxes=[...form.querySelectorAll(\'input[type=checkbox]\')];let busy=false;\n  const selected=()=>boxes.filter(x=>x.checked).map(x=>Number(x.value));\n  const update=()=>{const n=selected().length;counter.textContent=\'Выбрано \'+n+\' из \'+need+\' сотрудников\';button.disabled=busy||readonly||n!==need||!data.photo_received||data.leader.busy;boxes.forEach(box=>{const emp=data.employees.find(e=>e.id===Number(box.value));box.disabled=readonly||busy||emp.busy||(!box.checked&&n>=need)});};\n  if(!data.photo_received)error.textContent=\'Подтверждение состава доступно после первого фото.\';\n  if(data.leader.busy)error.textContent=\'Бригадир уже подтверждён в другом заказе на эту дату.\';\n  boxes.forEach(box=>box.onchange=update);update();\n  const normalizeCrewSearch=value=>String(value||\'\').toLocaleLowerCase(\'ru-RU\').replace(/ё/g,\'е\').trim();\n  const search=form.querySelector(\'[data-crew-search]\');search.setAttribute(\'autocomplete\',\'off\');search.setAttribute(\'autocorrect\',\'off\');search.setAttribute(\'spellcheck\',\'false\');\n  const noMatches=document.createElement(\'p\');noMatches.className=\'muted gbu-crew-no-matches\';noMatches.setAttribute(\'role\',\'status\');noMatches.hidden=true;noMatches.textContent=\'Сотрудники не найдены\';search.after(noMatches);\n  search.oninput=event=>{\n   const term=normalizeCrewSearch(event.target.value),tokens=term.split(/\\s+/).filter(Boolean);let matches=0;\n   form.classList.toggle(\'is-searching\',!!term);\n   form.querySelectorAll(\'[data-crew-name]\').forEach(row=>{const words=normalizeCrewSearch(row.dataset.crewName).split(/\\s+/);row.hidden=!tokens.every(token=>words.some(word=>word.startsWith(token)));if(!row.hidden)matches++});\n   form.querySelectorAll(\'[data-crew-group]\').forEach(group=>{group.hidden=!!term&&![...group.querySelectorAll(\'[data-crew-name]\')].some(row=>!row.hidden)});\n   noMatches.hidden=!term||matches>0;layoutCrewRows();\n  };\n  form.onsubmit=async event=>{event.preventDefault();if(button.disabled||busy)return;const ids=selected();if(ids.length!==need)return;busy=true;update();error.textContent=\'\';\n   try{await api(\'/api/gbu-staff-payroll/roster\',{method:\'POST\',body:JSON.stringify({card_id:cardId,category:detail.category,employee_ids:[data.leader.id,...ids],expected_revision:data.revision||null})});if(!dialog.open)return;dialog.close();if(afterSave)await afterSave();else await openGbuOrderRoster(cardId)}catch(err){if(!dialog.open)return;error.textContent=err.message;busy=false;update()}\n  };\n }catch(error){if(dialog.open)root.textContent=error.message}\n}\nasync function openGbuPayrollRoster(order,month){return openGbuOrderRoster(order.card_id,()=>openGbuPayroll(month))}\n', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(" const rosterButton=dialog.querySelector('[data-gbu-order-roster]');if(rosterButton)rosterButton.onclick=()=>openGbuOrderRoster(card.id);", ' bindGbuCrewDisclosure(dialog,card);', 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<script>function bindGbuCrewDisclosure(dialog,card){\n const button=dialog.querySelector(\'[data-gbu-order-roster]\');if(!button||card.source!==\'gbu\')return;\n const panel=document.createElement(\'div\');panel.className=\'bc-crew-disclosure\';panel.hidden=true;panel.id=\'gbu-card-crew-\'+card.id;\n button.setAttribute(\'aria-controls\',panel.id);button.setAttribute(\'aria-expanded\',\'false\');dialog.querySelector(\'.bc-viewer-title-row\').after(panel);\n let loading=false;\n const show=on=>{panel.hidden=!on;button.setAttribute(\'aria-expanded\',String(on))};\n const refresh=async()=>{show(true);panel.textContent=\'Загрузка состава…\';loading=true;\n  try{const data=await api(\'/api/gbu-staff-payroll/roster/\'+card.id);if(!dialog.open)return;\n   if(!data.confirmed){show(false);await openGbuOrderRoster(card.id,refresh);return}\n   const crew=(data.confirmed_crew||[]).slice().sort((a,b)=>Number(b.role===\'brigadier\')-Number(a.role===\'brigadier\'));\n   panel.innerHTML=`<ol>${crew.map(m=>`<li>${esc(m.employee?readinessEmployeeName(m.employee):m.name)}${m.role===\'brigadier\'?\' <small>· бригадир</small>\':\'\'}</li>`).join(\'\')}</ol><button type="button" data-edit-crew>Изменить состав</button>`;\n   panel.querySelector(\'[data-edit-crew]\').onclick=()=>openGbuOrderRoster(card.id,refresh,true);\n  }catch(error){if(dialog.open){panel.textContent=error.message;const retry=document.createElement(\'button\');retry.type=\'button\';retry.textContent=\'Повторить\';retry.onclick=refresh;panel.append(retry)}}finally{loading=false}\n };\n button.onclick=()=>{if(!panel.hidden){show(false);return}if(loading){show(true);return}refresh()};\n}\n</script><style>\n.bc-crew-disclosure[hidden]{display:none}.bc-crew-disclosure{margin:0 0 12px;padding:8px 10px;border:1px solid #d6c49b;border-radius:9px;background:#f4eddd;font-size:13px;line-height:1.4}\n.bc-crew-disclosure ol{margin:0 0 6px;padding-left:20px}.bc-crew-disclosure li{padding:2px 0}.bc-crew-disclosure small{color:#8b6b30;font-size:11px}\n.bc-crew-disclosure button{appearance:none;-webkit-appearance:none;display:inline-block;padding:5px 8px;min-height:32px;border:1px solid #b49a60;border-radius:7px;background:#eee3c6;color:#8b6b30;font-size:12px;font-weight:600}\n</style></body>', 1)



INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace(" const show=on=>{panel.hidden=!on;button.setAttribute('aria-expanded',String(on))};", " const show=on=>{panel.hidden=!on;button.setAttribute('aria-expanded',String(on));button.setAttribute('aria-label',on?'Свернуть состав заказа':'Показать состав заказа');button.textContent=button.textContent.replace(/\\s*[›⌃]$/,'')+(on?' ⌃':' ›')};", 1)



# Anchor a small crew dropdown directly below the category button.
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace("dialog.querySelector('.bc-viewer-title-row').after(panel);", "dialog.querySelector('.bc-viewer-title-row').append(panel);", 1)
INLINE_INDEX_HTML = INLINE_INDEX_HTML.replace('</body>', '<style>\n.bc-photo-viewer .bc-viewer-title-row{position:relative}\n.bc-photo-viewer .bc-crew-disclosure{position:absolute;top:calc(100% + 5px);right:0;z-index:8;box-sizing:border-box;width:min(300px,calc(100vw - 64px));max-height:230px;overflow:auto;margin:0;padding:9px 10px;border:1px solid #b49a60;border-radius:10px;background:#f8f4e9;box-shadow:0 5px 18px #302a1b26;font-size:12px;line-height:1.35;text-align:left}\n.bc-photo-viewer .bc-crew-disclosure[hidden]{display:none}\n.bc-photo-viewer .bc-crew-disclosure ol{margin:0 0 6px;padding-left:19px}\n.bc-photo-viewer .bc-crew-disclosure li{padding:4px 0;border-bottom:1px solid #e2d8c4;overflow-wrap:anywhere}\n.bc-photo-viewer .bc-crew-disclosure button{min-height:30px;padding:4px 7px;font-size:11px}\n</style></body>', 1)


def _isolated_preview_screen_html():
    html = INLINE_INDEX_HTML
    # No real Telegram SDK, credentials, database session or notifications.
    html = re.sub(r'<script\b[^>]*\bsrc=["\'][^"\']+["\'][^>]*>\s*</script>', '', html)
    start = html.index('async function api(path, opts={}) {')
    end = html.index('function toast(', start)
    html = html[:start] + 'async function api(path, opts={}) {return previewApi(path,opts)}\n\n' + html[end:]
    csp = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'"
    html = html.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" content="' + csp + '"><script>' + _PREVIEW_FIXTURE_SCRIPT + '</script>', 1)
    return html.replace('<title>ЛЕГИОН — Диспетчерская</title>', '<title>ЛЕГИОН — тестовые данные</title>', 1)


def legion_ui_preview_html():
    # srcdoc makes the page self-contained and fixes the *actual* viewport seen
    # by the production fit routine. Toolbar remains outside that viewport.
    screen = escape(_isolated_preview_screen_html(), quote=True)
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ЛЕГИОН — проверка интерфейса</title><style>
body{margin:0;background:#deddd8;color:#303330;font:14px system-ui,sans-serif}header{padding:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #c9c7bf}h1{font-size:17px;margin:0}p{margin:0;padding:8px 12px;font-size:12px}select,button{font:inherit;padding:7px;border:1px solid #bbb8ae;border-radius:8px;background:#faf8f3;color:inherit}main{padding:12px;display:flex;justify-content:center;overflow:auto}iframe{flex:none;width:393px;height:700px;border:1px solid #bcb9ae;border-radius:14px;background:#e8e7e3}strong{color:#966e20}#result{font-variant-numeric:tabular-nums}
</style></head><body><header><h1>ЛЕГИОН · Проверка интерфейса</h1><strong>ТОЛЬКО ТЕСТОВЫЕ ДАННЫЕ</strong><label>Экран <select id="size"><option value="393,700">393 × 700</option><option value="320,568">320 × 568</option><option value="393,620">393 × 620</option><option value="430,800">430 × 800</option></select></label><label>Роль <select id="role"><option value="owner">Владелец</option><option value="assistant">Диспетчер</option></select></label><button id="reset">Сбросить</button><button id="measure">Проверить размещение</button><span id="result" role="status"></span></header><p>Интерфейс взят из текущего app.py. Комментарии, счётчики и отметки меняются только в памяти этой страницы. Несмоделированные действия показывают предупреждение. Финансовые расчёты и отправка сообщений здесь не проверяются.</p><main><iframe id="screen" name="owner" title="Тестовая диспетчерская" srcdoc="''' + screen + '''"></iframe></main><script>
const frame=document.querySelector('#screen'),initial=frame.srcdoc;
function reset(){const role=document.querySelector('#role').value==='assistant'?'assistant':'owner';frame.name=role;frame.srcdoc=initial.replace("const previewRole='owner';","const previewRole='"+role+"';");document.querySelector('#result').textContent=''}
document.querySelector('#size').onchange=e=>{const [w,h]=e.target.value.split(',');frame.style.width=w+'px';frame.style.height=h+'px';document.querySelector('#result').textContent=''};
document.querySelector('#role').onchange=reset;document.querySelector('#reset').onclick=reset;
document.querySelector('#measure').onclick=()=>{const doc=frame.contentDocument,main=doc.querySelector('main'),nav=doc.querySelector('#roleNavigation'),groups=[...doc.querySelectorAll('#readinessGroups .group-block')];if(!main||!nav||groups.length!==4){document.querySelector('#result').textContent='Сначала откройте «Заказы» и дождитесь загрузки';return}const bottom=Math.max(...[main,...groups].map(e=>e.getBoundingClientRect().bottom)),limit=nav.getBoundingClientRect().top,overflow=[...doc.querySelectorAll('#summary button,#brigadeWork button,#readinessGroups button')].some(e=>e.getBoundingClientRect().right>frame.clientWidth+1);document.querySelector('#result').textContent=bottom<=limit+1&&!overflow?'✓ Четыре группы видны, перекрытий с нижним меню нет':'⚠ Есть выход за границы экрана';};
</script></body></html>'''


@app.get('/preview', response_class=HTMLResponse, include_in_schema=False)
def legion_ui_preview():
    """Public fictional UI sandbox; never calls working APIs or database code."""
    return HTMLResponse(legion_ui_preview_html(), headers={
        'Cache-Control': 'no-store',
        'X-Robots-Tag': 'noindex, nofollow',
        'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-src 'self' about:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'",
    })
