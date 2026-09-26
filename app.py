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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, inspect as sa_inspect, text
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import Column, Integer, BigInteger, String, Date, DateTime, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from database import Base, engine, get_db
from models import AccessUser, Agent, Assignment, CashEntry, Employee, GbuAgentContact, ImportLog, Order, Readiness
from parsers import parse_agent_ready_form, parse_gbu_ocr_text
from gbu_tools import best_agent_match, parse_gbu_table_image
from telegram_auth import TelegramUser, require_admin, require_bot_key
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


def _visible_employee():
    return or_(Employee.tg_id.is_(None), Employee.tg_id.notin_(HIDDEN_TG_IDS))

app = FastAPI(title="ЛЕГИОН — Диспетчерская", version="1.0.1")


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


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    _startup_migrations()
    Base.metadata.create_all(bind=engine)
    _seed_gbu_agents()
    with Session(engine) as db:
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


INLINE_INDEX_HTML = '<!doctype html>\n<html lang="ru">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n  <meta name="theme-color" content="#0a0a0b" />\n  <title>ЛЕГИОН — Диспетчерская</title>\n  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>\n  <style>\n:root{\n  --bg:#09090b; --panel:#121215; --panel2:#18181c; --line:#2a2a30;\n  --text:#f5f5f5; --muted:#9a9aa3; --gold:#d8b46a; --gold2:#8f7442;\n  --green:#58c785; --red:#e06969; --amber:#e3b454; --blue:#6ca6e8;\n  --radius:18px; --safe-bottom:env(safe-area-inset-bottom,0px);\n}\n*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif} body{min-height:100vh;padding-bottom:calc(82px + var(--safe-bottom))}\nbutton,input,select,textarea{font:inherit}.hidden{display:none!important}\n.topbar{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;padding:16px 16px 10px;background:linear-gradient(180deg,#09090b 78%,transparent)}\n.eyebrow{font-size:11px;letter-spacing:.22em;color:var(--gold);font-weight:800}.topbar h1{font-size:23px;margin:3px 0 0}.icon-btn{width:42px;height:42px;border-radius:14px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:22px}\n.date-strip{display:flex;gap:8px;overflow:auto;padding:6px 16px 13px;scrollbar-width:none}.date-strip::-webkit-scrollbar{display:none}.date-pill{min-width:78px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:14px;padding:9px 10px;text-align:center}.date-pill.active{border-color:var(--gold);background:#211d15}.date-pill strong{display:block;font-size:13px}.date-pill small{color:var(--muted);font-size:11px}.date-pill.active small{color:#d9c59a}\nmain{padding:0 14px}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:4px 0 18px}.summary-grid.compact{grid-template-columns:repeat(3,1fr)}.metric{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px 10px;min-width:0}.metric b{font-size:20px;display:block}.metric span{font-size:10px;color:var(--muted);display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.metric.warn b{color:var(--amber)}.metric.good b{color:var(--green)}\n.view{display:none}.view.active{display:block}.section-head{display:flex;align-items:center;justify-content:space-between;margin:6px 2px 12px}.section-head h2{font-size:21px;margin:0}.muted{color:var(--muted);margin:4px 0 0;font-size:12px}\n.primary{background:var(--gold);border:0;color:#171208;font-weight:800;border-radius:14px;padding:12px 16px}.primary.small{padding:9px 13px;font-size:13px}.primary:disabled{opacity:.35}.secondary{background:var(--panel2);border:1px solid var(--line);color:var(--text);font-weight:650;border-radius:14px;padding:11px 14px}.danger{border-color:#5a2d2d;color:#ffaaaa}\n.segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;background:var(--panel);padding:4px;border:1px solid var(--line);border-radius:14px;margin-bottom:10px}.segmented.scroll{display:flex;overflow:auto}.segmented button{border:0;background:transparent;color:var(--muted);padding:8px 10px;border-radius:10px;font-weight:700;font-size:12px;white-space:nowrap}.segmented button.active{background:#2a251b;color:#f6dfac}\n#employeeFilters button{display:inline-flex;align-items:center;gap:7px}\n.employee-ready-count{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:#1b3325;color:var(--green);font-size:11px;font-weight:850}\n#employeeFilters button.active .employee-ready-count{background:#284330;color:#7ee0a2}\n.status-row{display:flex;gap:6px;overflow:auto;margin-bottom:12px;scrollbar-width:none}.chip{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:7px 10px;font-size:11px;white-space:nowrap}.chip.active{color:#1b160c;background:var(--gold);border-color:var(--gold)}\n.cards{display:flex;flex-direction:column;gap:10px}.order-card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px;position:relative;overflow:hidden}.order-card.complete{border-color:#305c40}.order-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--gold2)}.order-card.complete::before{background:var(--green)}.order-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.order-time{font-size:20px;font-weight:850}.order-name{font-size:17px;font-weight:800;margin:3px 0 8px}.badges{display:flex;gap:5px;flex-wrap:wrap}.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#24242a;color:#ccc;border:1px solid #303038}.badge.gbu{background:#201d27;color:#d7c7f1}.badge.private{background:#17221c;color:#a9dfbd}.badge.vip{background:#2b2315;color:#f0d397}.badge.elite{background:#241c22;color:#edc7df}.order-number{font-size:12px;font-weight:850;color:var(--gold);margin-bottom:7px}.order-actions-top{display:flex;align-items:flex-start;gap:8px}.status-badge{font-size:10px;color:var(--muted);text-align:right;padding-top:4px}.delete-card-btn{width:34px;height:34px;border-radius:10px;border:1px solid #5a2d2d;background:#241616;color:#ffaaaa;font-size:15px;display:grid;place-items:center;padding:0}.order-foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:12px}.order-foot .brig{max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.progress{color:var(--muted)}.progress.done{color:var(--green)}\n.bottom-nav{position:fixed;z-index:30;left:0;right:0;bottom:0;height:calc(68px + var(--safe-bottom));padding-bottom:var(--safe-bottom);display:grid;grid-template-columns:repeat(4,1fr);background:rgba(13,13,15,.95);backdrop-filter:blur(16px);border-top:1px solid var(--line)}.bottom-nav button{border:0;background:transparent;color:#777;padding:7px 2px}.bottom-nav button.active{color:var(--gold)}.bottom-nav span{display:block;font-size:18px;height:23px}.bottom-nav small{font-size:9px;font-weight:700}\n.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:50}.drawer{position:fixed;z-index:60;left:0;right:0;bottom:0;max-height:92vh;overflow:auto;background:#101013;border:1px solid var(--line);border-radius:24px 24px 0 0;padding:18px 16px calc(22px + var(--safe-bottom));box-shadow:0 -20px 80px rgba(0,0,0,.6)}.handle{width:42px;height:4px;background:#3a3a40;border-radius:5px;margin:0 auto 15px}.drawer h3{margin:0 0 2px;font-size:22px}.drawer-section{margin-top:18px}.drawer-section h4{font-size:12px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin:0 0 9px}.data-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.data-item{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:10px}.data-item small{display:block;color:var(--muted);font-size:9px;margin-bottom:4px}.data-item b{font-size:13px}.full{grid-column:1/-1}.action-row{display:flex;gap:8px}.action-row>*{flex:1}.person-row{display:flex;align-items:center;gap:10px;padding:11px 3px;border-bottom:1px solid #24242a}.person-row:last-child{border:0}.person-main{min-width:0;flex:1}.person-main b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.person-main small{color:var(--muted);font-size:10px}.person-meta{font-size:11px;color:#cfcfd4;white-space:nowrap}.check{width:25px;height:25px;border:1px solid #4a4a52;border-radius:8px;display:grid;place-items:center;color:transparent}.person-row.selected .check{background:var(--green);border-color:var(--green);color:#07120b}.person-row.blocked{opacity:.4}.person-row.match{background:linear-gradient(90deg,rgba(216,180,106,.08),transparent)}\n.modal{position:fixed;z-index:70;left:12px;right:12px;top:7vh;max-height:86vh;overflow:auto;background:#111114;border:1px solid var(--line);border-radius:22px;padding:16px;box-shadow:0 20px 90px #000}.modal h3{margin:2px 0 14px}.modal-head{display:flex;justify-content:space-between;align-items:center}.close-btn{border:0;background:var(--panel2);color:var(--text);border-radius:10px;width:34px;height:34px}.candidate-list{max-height:60vh;overflow:auto}.search{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:14px;padding:12px;margin:0 0 10px;outline:none}.search:focus{border-color:var(--gold2)}\n.group-block{background:var(--panel);border:1px solid var(--line);border-radius:17px;margin:0 0 10px;overflow:hidden}.group-title{display:flex;justify-content:space-between;padding:13px;font-weight:800}.group-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:0 10px 10px}.group-stats button{border:0;border-radius:10px;background:#1b1b1f;color:var(--muted);padding:8px 3px;font-size:10px;cursor:pointer}.group-stats button:active{transform:scale(.98);background:#26262c}.group-stats button.has-people{box-shadow:inset 0 0 0 1px #34343b}.group-stats b{display:block;font-size:15px;color:var(--text)}.group-details{padding:0 12px 10px}.status-section{margin-top:10px}.status-section h5{margin:0 0 6px;font-size:11px;color:var(--muted)}\n.list{background:var(--panel);border:1px solid var(--line);border-radius:17px;overflow:hidden}.employee-row{display:flex;align-items:center;padding:11px 12px;border-bottom:1px solid var(--line);gap:10px}.employee-row:last-child{border:0}.employee-row .name{flex:1;min-width:0}.employee-row .name b{font-size:13px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.employee-row .name small{font-size:10px;color:var(--muted)}.car{font-size:14px}.height{font-size:12px;font-weight:800;color:#ddd}.contact-btns{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.contact-btn{border:1px solid var(--line);background:#202026;color:#ddd;border-radius:9px;padding:5px 8px;font-size:10px}.contact-btn.telegram{color:#9fc8ff}.section-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.attention{background:#211a14;border:1px solid #4c3a28;border-radius:16px;padding:13px;margin-bottom:10px}.attention.red{background:#221616;border-color:#512c2c}.attention h4{margin:0 0 7px}.attention p{margin:5px 0;color:var(--muted);font-size:11px}.attention-list{display:flex;flex-direction:column;gap:5px;font-size:12px}\n.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.field{margin-bottom:9px}.field label{display:block;color:var(--muted);font-size:10px;margin:0 0 5px}.field input,.field select,.field textarea{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:11px;padding:10px}.field textarea{min-height:74px;resize:vertical}.gbu-draft{padding:12px;border:1px solid var(--line);border-radius:15px;margin-bottom:10px;background:var(--panel)}\n.toast{position:fixed;z-index:100;left:20px;right:20px;bottom:calc(82px + var(--safe-bottom));padding:12px 14px;border-radius:14px;background:#25251f;border:1px solid #5b5036;color:#fff;text-align:center;font-size:12px}.fatal{position:fixed;z-index:200;inset:0;padding:60px 20px;background:#09090b;color:#fff}.fatal h2{color:var(--red)}\n.empty{padding:32px 12px;text-align:center;color:var(--muted);font-size:13px}\n@media(min-width:760px){body{max-width:960px;margin:auto}.drawer{left:50%;transform:translateX(-50%);max-width:680px}.modal{left:50%;right:auto;width:680px;transform:translateX(-50%)}.summary-grid{grid-template-columns:repeat(6,1fr)}}\n\n</style>\n</head>\n<body>\n  <div id="app">\n    <header class="topbar">\n      <div>\n        <div class="eyebrow">ЛЕГИОН</div>\n        <h1>Диспетчерская</h1>\n      </div>\n      <div style="display:flex;gap:8px"><button class="icon-btn hidden" id="cashBtn" aria-label="Касса">💰</button><button class="icon-btn" id="refreshBtn" aria-label="Обновить">↻</button></div>\n    </header>\n\n    <section class="date-strip" id="dateStrip"></section>\n\n    <main>\n      <section id="summary" class="summary-grid"></section>\n\n      <section id="ordersView" class="view active">\n        <div class="section-head">\n          <div>\n            <h2>Заказы</h2>\n            <p id="ordersDateLabel" class="muted"></p>\n          </div>\n          <div class="section-actions">\n            <button class="primary small" id="newOrderBtn">＋ Добавить заказ</button>\n            <button class="secondary small" id="gbuBtn">＋ ГБУ</button>\n          </div>\n        </div>\n        <div class="segmented" id="sourceFilters">\n          <button data-source="all" class="active">Все</button>\n          <button data-source="private">Частные</button>\n          <button data-source="gbu">ГБУ</button>\n        </div>\n        <div class="status-row" id="statusFilters">\n          <button data-status="all" class="chip active">Все</button>\n          <button data-status="new" class="chip">Новые</button>\n          <button data-status="assigning" class="chip">Расстановка</button>\n          <button data-status="ready" class="chip">Готовы</button>\n          <button data-status="sent" class="chip">Отправлены</button>\n        </div>\n        <div id="ordersList" class="cards"></div>\n      </section>\n\n      <section id="readinessView" class="view">\n        <div class="section-head">\n          <div><h2>Готовность</h2><p class="muted">Официальный срез — 15:30</p></div>\n          <div class="section-actions"><button class="secondary small" id="readinessHistoryBtn">🧾 История</button><button class="primary small" id="readinessCountBtn">📊 Посчитать</button></div>\n        </div>\n        <div id="readinessSummary" class="summary-grid"></div>\n        <div id="readinessGroups"></div>\n      </section>\n\n      <section id="employeesView" class="view">\n        <div class="section-head"><div><h2>Сотрудники</h2><p class="muted">Рост, группа, авто, контакты</p></div><div class="section-actions"><button class="secondary small" id="gbuAgentsBtn">⬛ Агенты ГБУ</button><button class="primary small" id="employeeImportBtn">Импорт</button></div></div>\n        <input id="employeeSearch" class="search" placeholder="Поиск по имени, метро и росту" />\n        <div class="segmented scroll" id="employeeFilters">\n          <button data-group="all" class="active"><span>Все</span><b class="employee-ready-count" data-ready-count="all">0</b></button>\n          <button data-group="brigadier"><span>Бригадиры</span><b class="employee-ready-count" data-ready-count="brigadier">0</b></button>\n          <button data-group="main"><span>Основной</span><b class="employee-ready-count" data-ready-count="main">0</b></button>\n          <button data-group="cashless"><span>Безнал</span><b class="employee-ready-count" data-ready-count="cashless">0</b></button>\n          <button data-group="reserve"><span>Резерв</span><b class="employee-ready-count" data-ready-count="reserve">0</b></button>\n        </div>\n        <div id="employeesList" class="list"></div>\n      </section>\n\n      <section id="controlView" class="view">\n        <div class="section-head"><div><h2>Контроль</h2><p class="muted">Что требует внимания</p></div></div>\n        <div id="controlContent"></div>\n      </section>\n    </main>\n\n    <nav class="bottom-nav">\n      <button data-view="ordersView" class="active"><span>▤</span><small>Заказы</small></button>\n      <button data-view="readinessView"><span>✓</span><small>Готовность</small></button>\n      <button data-view="employeesView"><span>👥</span><small>Сотрудники</small></button>\n      <button data-view="controlView"><span>!</span><small>Контроль</small></button>\n    </nav>\n  </div>\n\n  <div id="drawerBackdrop" class="backdrop hidden"></div>\n  <aside id="orderDrawer" class="drawer hidden"></aside>\n\n  <div id="modalBackdrop" class="backdrop hidden"></div>\n  <div id="modal" class="modal hidden"></div>\n\n  <div id="toast" class="toast hidden"></div>\n  <div id="fatal" class="fatal hidden"></div>\n\n  <script>\nconst tg = window.Telegram?.WebApp;\nif (tg) { tg.ready(); tg.expand(); }\n\nconst state = {\n  selectedDate: null,\n  source: \'all\',\n  status: \'all\',\n  employeeGroup: \'all\',\n  employeeSearch: \'\',\n  bootstrap: null,\n  currentOrder: null,\n};\n\nconst qs = (s, root=document) => root.querySelector(s);\nconst qsa = (s, root=document) => [...root.querySelectorAll(s)];\nconst fmtDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(\'ru-RU\',{day:\'2-digit\',month:\'long\',weekday:\'short\'});\nconst fmtMonthYear = (iso) => {\n  const s = new Date(`${iso}T12:00:00`).toLocaleDateString(\'ru-RU\',{month:\'long\',year:\'numeric\'});\n  return s.charAt(0).toUpperCase()+s.slice(1);\n};\nconst dayNumber = (iso) => String(new Date(`${iso}T12:00:00`).getDate());\nconst esc = (s=\'\') => String(s).replace(/[&<>\'"]/g, c => ({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[c]));\n\nasync function copyText(value, showToast=true){\n  if(!value) return false;\n  try{\n    if(navigator.clipboard?.writeText){\n      await navigator.clipboard.writeText(value);\n    }else{\n      const ta=document.createElement(\'textarea\');\n      ta.value=value;\n      ta.setAttribute(\'readonly\',\'\');\n      ta.style.position=\'fixed\';\n      ta.style.opacity=\'0\';\n      document.body.appendChild(ta);\n      ta.select();\n      const ok=document.execCommand(\'copy\');\n      ta.remove();\n      if(!ok) throw new Error(\'copy failed\');\n    }\n    if(showToast) toast(\'Скопировано\');\n    return true;\n  }catch(e){\n    window.prompt(\'Скопируйте:\',value);\n    return false;\n  }\n}\n\nfunction visibleOrderNumber(o){\n  return o?.display_number || o?.daily_number || o?.id || \'—\';\n}\n\nfunction telegramOrderText(o){\n  const dateText=o.work_date ? o.work_date.split(\'-\').reverse().join(\'.\') : \'—\';\n  const lines=[\n    `Заказ №${visibleOrderNumber(o)}`,\n    `Дата: ${dateText}`,\n    `Категория заказа: ${o.category_label||\'—\'}`,\n    `Агент: ${o.agent_name||\'—\'}`,\n    `Номер агента: ${o.agent_phone||\'—\'}`,\n    `Время подачи: ${o.arrival_time||\'—\'}`,\n    `Время выдачи: ${o.issue_time||\'—\'}`,\n    `ФИО умершего: ${o.deceased_name||\'—\'}`,\n    `Маршрут: ${o.route||\'—\'}`,\n    `Количество человек: ${o.team_size||\'—\'} чел.`\n  ];\n\n  if(o.organization) lines.push(`Организация: ${o.organization}`);\n  if(o.other_agent_phone) lines.push(`Номер другого агента: ${o.other_agent_phone}`);\n  if(o.notes) lines.push(`Примечание: ${o.notes}`);\n  if(o.kickback_rub) lines.push(`Откат: ${o.kickback_rub} ₽`);\n\n  lines.push(\'\', \'Состав:\');\n  lines.push(`Бригадир: ${o.brigadier ? (o.brigadier.display_name||o.brigadier.full_name) : \'—\'}`);\n  if(o.members?.length){\n    o.members.forEach((x,i)=>lines.push(`${i+1}. ${x.display_name||x.full_name}`));\n  }else{\n    lines.push(\'Сотрудники: —\');\n  }\n  return lines.join(\'\\n\');\n}\nfunction contactButtons(x){\n  const out=[];\n  if(x.phone) out.push(`<button class="contact-btn" data-copy-phone="${esc(x.phone)}">📋 ${esc(x.phone)}</button>`);\n  if(x.telegram_url) out.push(`<button class="contact-btn telegram" data-open-tg="${esc(x.telegram_url)}">Telegram ↗</button>`);\n  return out.length?`<div class="contact-btns">${out.join(\'\')}</div>`:\'\';\n}\nfunction wireContactButtons(root=document){\n  qsa(\'[data-copy-phone]\',root).forEach(b=>b.onclick=e=>{e.stopPropagation();copyText(b.dataset.copyPhone)});\n  qsa(\'[data-open-tg]\',root).forEach(b=>b.onclick=e=>{e.stopPropagation();const u=b.dataset.openTg;if(u.startsWith(\'https://t.me/\')&&tg?.openTelegramLink)tg.openTelegramLink(u);else window.location.href=u});\n}\n\nfunction headers(json=true) {\n  const h = {};\n  if (json) h[\'Content-Type\'] = \'application/json\';\n  const init = tg?.initData || \'\';\n  if (init) h[\'X-Telegram-Init-Data\'] = init;\n  const p = new URLSearchParams(location.search);\n  if (p.get(\'dev_uid\')) h[\'X-Dev-Telegram-Id\'] = p.get(\'dev_uid\');\n  return h;\n}\n\nasync function api(path, opts={}) {\n  const isForm = opts.body instanceof FormData;\n  const response = await fetch(path, {...opts, headers:{...headers(!isForm), ...(opts.headers||{})}});\n  const contentType = response.headers.get(\'content-type\') || \'\';\n  const data = contentType.includes(\'application/json\') ? await response.json() : await response.text();\n  if (!response.ok) {\n    const msg = data?.detail || data?.message || String(data) || `Ошибка ${response.status}`;\n    throw new Error(msg);\n  }\n  return data;\n}\n\nfunction toast(msg, timeout=2500) {\n  const el = qs(\'#toast\'); el.textContent = msg; el.classList.remove(\'hidden\');\n  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(()=>el.classList.add(\'hidden\'), timeout);\n}\n\nfunction fatal(msg) {\n  const el = qs(\'#fatal\');\n  el.innerHTML = `<h2>Доступ к диспетчерской</h2><p>${esc(msg)}</p><p class="muted">Mini App доступна только владельцу и помощникам, чьи Telegram ID указаны в настройках сервера.</p>`;\n  el.classList.remove(\'hidden\');\n}\n\nfunction statusColorClass(o) { return o.composition_complete ? \'complete\' : \'\'; }\n\nasync function loadBootstrap(date=null) {\n  const suffix = date ? `?work_date=${date}` : \'\';\n  const data = await api(`/api/bootstrap${suffix}`);\n  state.bootstrap = data;\n  state.selectedDate = data.selected_date;\n  const cashBtn=qs(\'#cashBtn\'); if(cashBtn) cashBtn.classList.toggle(\'hidden\', !data.is_owner);\n  renderDates(); renderSummary();\n  await renderCurrentView();\n}\n\nfunction renderDates() {\n  const el = qs(\'#dateStrip\');\n  el.innerHTML = state.bootstrap.dates.map(d => `\n    <button class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}">\n      <strong>${esc(d.label)}</strong><small>${d.count} заказов</small>\n    </button>`).join(\'\');\n  qsa(\'.date-pill\', el).forEach(btn => btn.onclick = async () => {\n    state.selectedDate = btn.dataset.date;\n    await loadBootstrap(state.selectedDate);\n  });\n}\n\nfunction renderSummary() {\n  const s = state.bootstrap.summary;\n  qs(\'#summary\').innerHTML = `\n    <div class="metric"><b>${s.orders_total}</b><span>Заказов</span></div>\n    <div class="metric"><b>${s.private_count}</b><span>Частных</span></div>\n    <div class="metric"><b>${s.gbu_count}</b><span>ГБУ</span></div>\n    <div class="metric ${s.unassigned_count?\'warn\':\'good\'}"><b>${s.complete_count}/${s.orders_total}</b><span>Укомплект.</span></div>\n    <div class="metric good"><b>${s.ready_cutoff}</b><span>Готовы 15:30</span></div>\n    <div class="metric ${s.no_response_cutoff?\'warn\':\'\'}"><b>${s.no_response_cutoff}</b><span>Не отписались</span></div>`;\n}\n\nfunction currentViewId() { return qs(\'.bottom-nav button.active\')?.dataset.view || \'ordersView\'; }\nasync function renderCurrentView() {\n  const v = currentViewId();\n  if (v===\'ordersView\') return loadOrders();\n  if (v===\'readinessView\') return loadReadiness();\n  if (v===\'employeesView\') return loadEmployees();\n  if (v===\'controlView\') return loadControl();\n}\n\nasync function loadOrders() {\n  qs(\'#ordersDateLabel\').textContent = fmtDate(state.selectedDate);\n  const params = new URLSearchParams({work_date:state.selectedDate, source:state.source, status:state.status});\n  const orders = await api(`/api/orders?${params}`);\n  const el = qs(\'#ordersList\');\n  if (!orders.length) { el.innerHTML = `<div class="empty">На эту дату заказов в выбранном фильтре нет.</div>`; return; }\n  el.innerHTML = orders.map(o => `\n    <article class="order-card ${statusColorClass(o)}" data-order-id="${o.id}">\n      <div class="order-top">\n        <div><div class="order-number">Заказ №${visibleOrderNumber(o)}</div><div class="badges"><span class="badge ${o.source}">${esc(o.source_label)}</span></div></div>\n        <div class="order-actions-top"><div class="status-badge">${esc(o.status_label)}</div><button class="delete-card-btn" data-delete-order="${o.id}" data-display-number="${visibleOrderNumber(o)}" aria-label="Удалить заказ">🗑</button></div>\n      </div>\n      <div class="data-grid" style="margin-top:10px">\n        <div class="data-item"><small>Дата</small><b>${fmtDate(o.work_date)}</b></div>\n        <div class="data-item"><small>Категория заказа</small><b>${esc(o.category_label)}</b></div>\n        <div class="data-item"><small>Агент</small><b>${esc(o.agent_name||\'—\')}</b></div>\n        <div class="data-item"><small>Номер агента</small><b>${esc(o.agent_phone||\'—\')}</b></div>\n        <div class="data-item"><small>Время подачи</small><b>${esc(o.arrival_time||\'—\')}</b></div>\n        <div class="data-item"><small>Время выдачи</small><b>${esc(o.issue_time||\'—\')}</b></div>\n        <div class="data-item full"><small>ФИО умершего</small><b>${esc(o.deceased_name||\'—\')}</b></div>\n        <div class="data-item full"><small>Маршрут</small><b>${esc(o.route||\'—\')}</b></div>\n        <div class="data-item full"><small>Количество человек</small><b>${o.team_size} чел.</b></div>\n      </div>\n      <div class="order-foot">\n        <div class="brig">${o.brigadier ? `🧑\u200d✈️ ${esc(o.brigadier.full_name)} · ${o.brigadier.height_cm||\'—\'}` : \'⚠️ Бригадир не назначен\'}</div>\n        <div class="progress ${o.member_count===o.member_target?\'done\':\'\'}">👥 ${o.member_count}/${o.member_target}</div>\n      </div>\n    </article>`).join(\'\');\n  qsa(\'[data-delete-order]\', el).forEach(btn => btn.onclick = async (e) => {\n    e.stopPropagation();\n    await deleteOrderCompletely(Number(btn.dataset.deleteOrder), btn.dataset.displayNumber);\n  });\n  qsa(\'.order-card\', el).forEach(card => card.onclick = () => openOrder(Number(card.dataset.orderId)));\n}\n\nasync function deleteOrderCompletely(id, displayNumber=null) {\n  const shown=displayNumber || id;\n  if (!confirm(`Удалить заказ №${shown} полностью?\\n\\nЗаказ и назначенный состав будут удалены без возможности восстановления.`)) return false;\n  try {\n    await api(`/api/orders/${id}`, {method:\'DELETE\'});\n    if (state.currentOrder && Number(state.currentOrder.id) === Number(id)) closeDrawer();\n    toast(`Заказ №${shown} удалён`);\n    await loadBootstrap(state.selectedDate);\n    return true;\n  } catch(e) {\n    toast(e.message, 4000);\n    return false;\n  }\n}\n\nasync function openOrder(id) {\n  const o = await api(`/api/orders/${id}`); state.currentOrder = o;\n  const d = qs(\'#orderDrawer\'); const b = qs(\'#drawerBackdrop\');\n  d.innerHTML = orderDrawerHtml(o); d.classList.remove(\'hidden\'); b.classList.remove(\'hidden\');\n  wireOrderDrawer(o);\n}\n\nfunction orderDrawerHtml(o) {\n  const members = o.members.length ? o.members.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(x.display_name||x.full_name)}</b><small>${esc(x.group_label)}</small>${contactButtons(x)}</div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\') : `<div class="empty">Состав пока не выбран</div>`;\n  return `\n    <div class="handle"></div>\n    <div class="order-top"><div><h3>Заказ №${visibleOrderNumber(o)}</h3><p class="muted">${esc(o.source_label)} · ${esc(o.status_label)}</p></div><button class="close-btn" id="closeDrawer">×</button></div>\n    <div class="drawer-section data-grid">\n      <div class="data-item"><small>Дата</small><b>${fmtDate(o.work_date)}</b></div>\n      <div class="data-item"><small>Категория заказа</small><b>${esc(o.category_label)}</b></div>\n      <div class="data-item"><small>Агент</small><b>${esc(o.agent_name||\'—\')}</b></div>\n      <div class="data-item"><small>Номер агента</small><b>${esc(o.agent_phone||\'—\')}</b></div>\n      <div class="data-item"><small>Время подачи</small><b>${esc(o.arrival_time||\'—\')}</b></div>\n      <div class="data-item"><small>Время выдачи</small><b>${esc(o.issue_time||\'—\')}</b></div>\n      <div class="data-item full"><small>ФИО умершего</small><b>${esc(o.deceased_name||\'—\')}</b></div>\n      <div class="data-item full"><small>Маршрут</small><b>${esc(o.route||\'—\')}</b></div>\n      <div class="data-item full"><small>Количество человек</small><b>${o.team_size} чел.</b></div>\n      ${o.organization?`<div class="data-item full"><small>Организация</small><b>${esc(o.organization)}</b></div>`:\'\'}\n      ${o.other_agent_phone?`<div class="data-item full"><small>Номер другого агента</small><b>${esc(o.other_agent_phone)}</b></div>`:\'\'}\n      ${o.notes?`<div class="data-item full"><small>Примечание</small><b>${esc(o.notes)}</b></div>`:\'\'}\n      ${o.kickback_rub?`<div class="data-item full"><small>Дополнительно для бригадира</small><b>Откат ${o.kickback_rub} ₽</b></div>`:\'\'}\n      ${o.brigade_on_contact_at?`<div class="data-item"><small>Бригада на связи</small><b>✅ Отмечено</b></div>`:\'\'}\n      ${o.contact_time?`<div class="data-item"><small>Время связи</small><b>${esc(o.contact_time)}</b></div>`:\'\'}\n      ${o.tea_reported_at?`<div class="data-item full"><small>Отчёт по заказу · количество чая</small><b>${Number(o.tea_count||0)}</b></div>`:\'\'}\n    </div>\n\n    <div class="drawer-section"><h4>Бригадир</h4>\n      ${o.brigadier ? `<div class="person-row"><div class="person-main"><b>${esc(o.brigadier.full_name)}</b><small>${esc(o.brigadier.group_label)}</small>${contactButtons(o.brigadier)}</div><div class="person-meta">${o.brigadier.height_cm||\'—\'} см ${o.brigadier.has_car?\'🚗\':\'\'}</div></div>` : `<div class="empty">Не назначен</div>`}\n      <button class="secondary" id="assignBrigBtn" style="width:100%;margin-top:8px">${o.brigadier?\'Сменить бригадира\':\'Назначить бригадира\'}</button>\n    </div>\n\n    <div class="drawer-section"><h4>Состав · ${o.member_count}/${o.member_target}</h4><div>${members}</div>\n      <button class="secondary" id="selectMembersBtn" style="width:100%;margin-top:8px" ${o.brigadier?\'\':\'disabled\'}>Выбрать состав по галочкам</button>\n      <button class="secondary" id="manualCompositionBtn" style="width:100%;margin-top:8px">✍️ Заполнить состав вручную</button>\n    </div>\n\n    <div class="drawer-section action-row">\n      <button class="secondary" id="editOrderBtn">✏️ Изменить заказ</button>\n      <button class="secondary danger" id="cancelOrderBtn" ${[\'cancelled\',\'closed\'].includes(o.status)?\'disabled\':\'\'}>🚫 Отменить</button>\n    </div>\n    <div class="drawer-section action-row">\n      <button class="secondary danger" id="resetCompositionBtn">Снять состав</button>\n      <button class="primary" id="finalizeBtn" ${o.composition_complete?\'\':\'disabled\'}>Состав готов</button>\n    </div>\n    <div class="drawer-section">\n      <button class="secondary" id="copyOrderBtn" style="width:100%">📋 Копировать для Telegram</button>\n      <button class="secondary" id="addOrderBtn" style="width:100%;margin-top:8px">＋ Добавить заказ</button>\n      <p class="muted" style="margin:8px 2px 0">«Копировать» — только текст для отправки бригадиру в Telegram. «Добавить заказ» — открывает пустой шаблон нового заказа без копирования данных и состава.</p>\n      <button class="secondary danger" id="deleteOrderBtn" style="width:100%;margin-top:8px">🗑 Удалить заказ полностью</button>\n    </div>\n    <p class="muted" style="margin-top:10px">Заказ отправится бригадиру только после полного состава.</p>`;\n}\n\nfunction wireOrderDrawer(o) {\n  wireContactButtons(qs(\'#orderDrawer\'));\n  qs(\'#closeDrawer\').onclick = closeDrawer;\n  qs(\'#assignBrigBtn\').onclick = ()=>openBrigadierModal(o.id);\n  qs(\'#selectMembersBtn\').onclick = ()=>openMembersModal(o.id);\n  qs(\'#manualCompositionBtn\').onclick = ()=>openManualCompositionModal(o.id);\n  qs(\'#copyOrderBtn\').onclick = async ()=>{\n    const ok=await copyText(telegramOrderText(o), false);\n    toast(ok ? \'Заказ скопирован для Telegram\' : \'Текст подготовлен для копирования\', 3000);\n  };\n  qs(\'#addOrderBtn\').onclick = ()=>{\n    closeDrawer();\n    openManualOrder();\n  };\n  qs(\'#editOrderBtn\').onclick = ()=>openEditOrderModal(o);\n  qs(\'#cancelOrderBtn\').onclick = async ()=>{\n    if ([\'cancelled\',\'closed\'].includes(o.status)) return;\n    if (!confirm(`Отменить заказ ${o.deceased_name}? Состав будет освобождён.`)) return;\n    try {await api(`/api/orders/${o.id}/cancel`,{method:\'POST\'});toast(\'Заказ отменён\');closeDrawer();await loadBootstrap(state.selectedDate)} catch(e){toast(e.message,3500)}\n  };\n  qs(\'#deleteOrderBtn\').onclick = async ()=>{ await deleteOrderCompletely(o.id); };\n  qs(\'#resetCompositionBtn\').onclick = async ()=>{\n    if (!confirm(\'Снять бригадира и весь состав с этого заказа?\')) return;\n    await api(`/api/orders/${o.id}/reset-composition`,{method:\'POST\'}); toast(\'Состав снят\'); await refreshOrder(o.id);\n  };\n  qs(\'#finalizeBtn\').onclick = async ()=>{\n    try {\n      const r = await api(`/api/orders/${o.id}/finalize`,{method:\'POST\'});\n      toast(r.telegram_sent?\'Заказ отправлен бригадиру\':\'Состав сохранён. \'+(r.warning||\'\'),3500);\n      await refreshOrder(o.id);\n    } catch(e){toast(e.message,3500)}\n  };\n}\n\nasync function refreshOrder(id) {\n  state.bootstrap = await api(`/api/bootstrap?work_date=${state.selectedDate}`); renderSummary(); renderDates();\n  await loadOrders(); await openOrder(id);\n}\n\nfunction closeDrawer(){qs(\'#orderDrawer\').classList.add(\'hidden\');qs(\'#drawerBackdrop\').classList.add(\'hidden\');state.currentOrder=null}\n\nfunction editOrderFormHtml(o){return `\n  <div class="form-grid">\n    <div class="field"><label>Дата</label><input id="editWorkDate" type="date" value="${o.work_date||\'\'}"></div>\n    <div class="field"><label>Категория заказа</label><select id="editCategory"><option value="standard" ${o.category===\'standard\'?\'selected\':\'\'}>Стандарт</option><option value="vip" ${o.category===\'vip\'?\'selected\':\'\'}>Вип</option><option value="elite" ${o.category===\'elite\'?\'selected\':\'\'}>Элит</option></select></div>\n    <div class="field"><label>Агент</label><input id="editAgentName" value="${esc(o.agent_name||\'\')}"></div>\n    <div class="field"><label>Номер агента</label><input id="editAgentPhone" value="${esc(o.agent_phone||\'\')}"></div>\n    <div class="field"><label>Время подачи</label><input id="editArrivalTime" type="time" value="${o.arrival_time||\'\'}"></div>\n    <div class="field"><label>Время выдачи</label><input id="editIssueTime" type="time" value="${o.issue_time||\'\'}"></div>\n    <div class="field full"><label>ФИО умершего</label><input id="editDeceased" value="${esc(o.deceased_name||\'\')}"></div>\n    <div class="field full"><label>Маршрут</label><textarea id="editRoute">${esc(o.route||\'\')}</textarea></div>\n    <div class="field full"><label>Количество человек</label><select id="editTeamSize"><option value="4" ${Number(o.team_size)===4?\'selected\':\'\'}>4</option><option value="6" ${Number(o.team_size)===6?\'selected\':\'\'}>6</option></select></div>\n    <div class="field"><label>Источник</label><select id="editSource"><option value="private" ${o.source===\'private\'?\'selected\':\'\'}>Частный</option><option value="gbu" ${o.source===\'gbu\'?\'selected\':\'\'}>ГБУ</option></select></div>\n    <div class="field"><label>Организация</label><input id="editOrganization" value="${esc(o.organization||\'\')}"></div>\n    <div class="field full"><label>Номер другого агента</label><input id="editOtherAgentPhone" value="${esc(o.other_agent_phone||\'\')}"></div>\n    <div class="field full"><label>Примечание</label><textarea id="editNotes">${esc(o.notes||\'\')}</textarea></div>\n    <div class="field"><label>Откат, ₽</label><input id="editKickback" inputmode="numeric" type="number" min="0" value="${Number(o.kickback_rub||0)}"></div>\n  </div>\n  <p class="muted">Если изменить дату или количество человек, текущий состав будет автоматически снят.</p>\n  <button class="primary" id="saveOrderEdit" style="width:100%">Сохранить изменения</button>`}\n\nfunction openEditOrderModal(o){\n  showModal(\'Изменить заказ\', editOrderFormHtml(o));\n  qs(\'#saveOrderEdit\').onclick=async()=>{\n    const payload={work_date:qs(\'#editWorkDate\').value,issue_time:qs(\'#editIssueTime\').value,arrival_time:qs(\'#editArrivalTime\').value||null,deceased_name:qs(\'#editDeceased\').value.trim(),route:qs(\'#editRoute\').value.trim(),category:qs(\'#editCategory\').value,team_size:Number(qs(\'#editTeamSize\').value),source:qs(\'#editSource\').value,organization:qs(\'#editOrganization\').value.trim(),agent_name:qs(\'#editAgentName\').value.trim(),agent_phone:qs(\'#editAgentPhone\').value.trim(),other_agent_phone:qs(\'#editOtherAgentPhone\').value.trim(),notes:qs(\'#editNotes\').value.trim(),kickback_rub:Number(qs(\'#editKickback\').value||0)};\n    if(!payload.work_date||!payload.issue_time||!payload.deceased_name){toast(\'Заполните дату, время и умершего\');return}\n    try{const updated=await api(`/api/orders/${o.id}`,{method:\'PATCH\',body:JSON.stringify(payload)});closeModal();toast(\'Заказ обновлён\');state.selectedDate=updated.work_date;await loadBootstrap(state.selectedDate);await openOrder(updated.id)}catch(e){toast(e.message,4000)}\n  };\n}\n\nasync function openManualCompositionModal(orderId) {\n  const data = await api(`/api/orders/${orderId}/manual-composition`);\n  if(!data.brigadiers.length){toast(\'В списке нет зарегистрированных бригадиров\');return}\n  const selected = new Set((data.member_ids||[]).map(Number));\n  const brigOptions = data.brigadiers.map(x=>`<option value="${x.id}" ${Number(data.brigadier_id)===Number(x.id)?\'selected\':\'\'}>${esc(x.display_name||x.full_name)} · ${x.workload}/2</option>`).join(\'\');\n  showModal(\'✍️ Ручное заполнение состава\', `\n    <p class="muted">Можно выбрать сотрудников вручную, даже если они не отмечали «Готов». Ограничение — не более 2 заказов на человека в день.</p>\n    <div class="field full"><label>Бригадир</label><select id="manualBrigSelect">${brigOptions}</select></div>\n    <div class="drawer-section"><h4>Сотрудники · нужно <span id="manualNeedCount">${data.target}</span></h4>\n      <input class="search" id="manualMemberSearch" placeholder="Поиск сотрудника">\n      <div class="candidate-list" id="manualMemberList"></div>\n    </div>\n    <div class="attention"><p>Выбрано: <b id="manualSelectedCount">0</b> / ${data.target}</p></div>\n    <button class="primary" id="saveManualComposition" style="width:100%">Сохранить состав</button>`);\n\n  const brigSelect=qs(\'#manualBrigSelect\');\n  const memberList=qs(\'#manualMemberList\');\n  const selectedCount=qs(\'#manualSelectedCount\');\n  const search=qs(\'#manualMemberSearch\');\n\n  function renderManualMembers(){\n    const brigId=Number(brigSelect.value);\n    if(selected.has(brigId)) selected.delete(brigId);\n    const q=(search.value||\'\').trim().toLowerCase();\n    const rows=(data.members||[]).filter(x=>Number(x.id)!==brigId && (!q || `${x.display_name||x.full_name} ${x.group_label}`.toLowerCase().includes(q)));\n    memberList.innerHTML=rows.length?rows.map(x=>`<div class="person-row ${selected.has(Number(x.id))?\'selected\':\'\'} ${x.blocked&&!selected.has(Number(x.id))?\'blocked\':\'\'}" data-manual-member="${x.id}">\n      <div class="check">✓</div><div class="person-main"><b>${esc(x.display_name||x.full_name)}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа${x.readiness_status===\'ready\'?\' · ✅ готов\':\'\'}</small>${contactButtons(x)}</div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\'):\'<div class="empty">Не найдено</div>\';\n    selectedCount.textContent=String(selected.size);\n    wireContactButtons(memberList);\n    qsa(\'[data-manual-member]\',memberList).forEach(row=>row.onclick=e=>{\n      if(e.target.closest(\'.contact-btn\')) return;\n      const id=Number(row.dataset.manualMember);\n      if(selected.has(id)){selected.delete(id);renderManualMembers();return}\n      if(selected.size>=Number(data.target)){toast(`Нужно выбрать ${data.target} сотрудников`);return}\n      if(row.classList.contains(\'blocked\')){toast(\'У сотрудника уже 2 заказа\');return}\n      selected.add(id);renderManualMembers();\n    });\n  }\n  brigSelect.onchange=renderManualMembers;\n  search.oninput=renderManualMembers;\n  renderManualMembers();\n\n  qs(\'#saveManualComposition\').onclick=async()=>{\n    if(selected.size!==Number(data.target)){toast(`Выберите ровно ${data.target} сотрудников`);return}\n    try{\n      await api(`/api/orders/${orderId}/manual-composition`,{method:\'POST\',body:JSON.stringify({brigadier_id:Number(brigSelect.value),member_ids:[...selected]})});\n      closeModal();toast(\'Состав сохранён вручную\');await refreshOrder(orderId);\n    }catch(e){toast(e.message,4500)}\n  };\n}\n\nasync function openBrigadierModal(orderId) {\n  const list = await api(`/api/orders/${orderId}/brigadiers`);\n  showModal(`Назначить бригадира`, `\n    <p class="muted">Показаны только бригадиры, которые отметились «готов» на эту дату.</p>\n    <div class="candidate-list">${list.length?list.map(personRowBrig).join(\'\'):`<div class="empty">Готовых бригадиров нет</div>`}</div>`);\n  wireContactButtons(qs(\'#modal\'));\n  qsa(\'[data-brig-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    if (row.classList.contains(\'blocked\')) {toast(\'У бригадира уже 2 заказа\'); return;}\n    try {await api(`/api/orders/${orderId}/brigadier`,{method:\'POST\',body:JSON.stringify({employee_id:Number(row.dataset.brigId)})}); closeModal(); toast(\'Бригадир назначен\'); await refreshOrder(orderId)} catch(e){toast(e.message,3500)}\n  });\n}\n\nfunction personRowBrig(x){return `<div class="person-row ${x.blocked?\'blocked\':\'\'}" data-brig-id="${x.id}"><div class="person-main"><b>${esc(x.display_name||x.full_name)}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small>${contactButtons(x)}</div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nasync function openMembersModal(orderId) {\n  const data = await api(`/api/orders/${orderId}/members/candidates`);\n  if(!data.brigadier){toast(\'Сначала назначьте бригадира\');return}\n  showModal(`Состав · нужно ${data.target}`, `\n    <p class="muted">Сверху сотрудники ±2 см от бригадира ${data.brigadier.height_cm||\'—\'} см. Можно максимум 2 заказа на человека в день.</p>\n    <input class="search" id="candidateSearch" placeholder="Поиск сотрудника">\n    <div class="candidate-list" id="candidateList">${data.candidates.map(personRowCandidate).join(\'\')}</div>`);\n  const wire = ()=>{wireContactButtons(qs(\'#modal\'));qsa(\'[data-member-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    const id=Number(row.dataset.memberId); if(row.dataset.brig===\'1\') return;\n    if(row.classList.contains(\'blocked\') && row.dataset.selected!==\'1\'){toast(\'У сотрудника уже 2 заказа\');return}\n    try{\n      if(row.dataset.selected===\'1\') await api(`/api/orders/${orderId}/members/${id}`,{method:\'DELETE\'});\n      else await api(`/api/orders/${orderId}/members`,{method:\'POST\',body:JSON.stringify({employee_id:id})});\n      const fresh=await api(`/api/orders/${orderId}/members/candidates`); qs(\'#candidateList\').innerHTML=fresh.candidates.map(personRowCandidate).join(\'\'); wire(); await loadOrders();\n    }catch(e){toast(e.message,3500)}\n  });};\n  wire();\n  qs(\'#candidateSearch\').oninput = e => {const v=e.target.value.toLowerCase(); qsa(\'[data-member-id]\',qs(\'#candidateList\')).forEach(r=>r.style.display=r.textContent.toLowerCase().includes(v)?\'flex\':\'none\')};\n}\n\nfunction personRowCandidate(x){return `<div class="person-row ${x.selected?\'selected\':\'\'} ${x.blocked&&!x.selected?\'blocked\':\'\'} ${x.height_match?\'match\':\'\'}" data-member-id="${x.id}" data-selected="${x.selected?\'1\':\'0\'}" data-brig="${x.is_brigadier?\'1\':\'0\'}">\n  <div class="check">✓</div><div class="person-main"><b>${esc(x.display_name||x.full_name)} ${x.is_brigadier?\'(бригадир)\':\'\'}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small>${contactButtons(x)}</div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nfunction showModal(title, body) {qs(\'#modal\').innerHTML=`<div class="modal-head"><h3>${esc(title)}</h3><button id="closeModal" class="close-btn">×</button></div>${body}`;qs(\'#modal\').classList.remove(\'hidden\');qs(\'#modalBackdrop\').classList.remove(\'hidden\');qs(\'#closeModal\').onclick=closeModal}\nlet cashRefreshTimer=null;\nfunction closeModal(){if(cashRefreshTimer){clearInterval(cashRefreshTimer);cashRefreshTimer=null}qs(\'#modal\').classList.add(\'hidden\');qs(\'#modalBackdrop\').classList.add(\'hidden\')}\n\nfunction readinessPersonRows(people){\n  const rows=(people||[]).slice().sort((a,b)=>\n    String(a.display_name||a.full_name||\'\').localeCompare(\n      String(b.display_name||b.full_name||\'\'),\'ru\',{sensitivity:\'base\'}\n    )\n  );\n  if(!rows.length) return \'<div class="empty">В этой категории сотрудников нет</div>\';\n  return `<div class="list">${rows.map((x,i)=>`\n    <div class="employee-row">\n      <div class="name">\n        <b>${i+1}. ${esc(x.display_name||x.full_name)}</b>\n        <small>${x.metro?esc(x.metro):\'\'}</small>\n      </div>\n      <div class="height">${x.height_cm||\'—\'} см</div>\n      <div class="car">${x.has_car?\'🚗\':\'\'}${x.manual_entry_id?`<button class="icon-btn" data-manual-remove="${x.manual_entry_id}" aria-label="Удалить ручную запись">×</button>`:\'\'}</div>\n    </div>\n  `).join(\'\')}</div>`;\n}\n\nfunction openReadinessPeople(groupLabel,statusLabel,people,code,status,workDate){\n  const rows=people||[];\n  showModal(\n    `${groupLabel} · ${statusLabel}`,\n    `<div class="attention"><h4>Всего: ${rows.length}</h4><p>Список сотрудников на выбранную дату.</p><button id="manualReadyAdd" class="secondary" style="min-height:48px;margin-top:12px" aria-label="Добавить сотрудника вручную">✍️ Добавить сотрудника</button></div>${readinessPersonRows(rows)}`\n  );\n  qs(\'#manualReadyAdd\').onclick=()=>openManualReadiness(code,status,groupLabel,statusLabel,workDate);\n  qsa(\'[data-manual-remove]\',qs(\'#modal\')).forEach(button=>button.onclick=async()=>{\n    if(!confirm(\'Удалить эту ручную запись из подсчёта?\'))return;\n    button.disabled=true;\n    try{await api(`/api/readiness/manual/${button.dataset.manualRemove}`,{method:\'DELETE\'});closeModal();await loadReadiness();toast(\'Ручная запись удалена\');}\n    catch(e){toast(e.message,4000);button.disabled=false;}\n  });\n}\n\nfunction openManualReadiness(code,status,groupLabel,statusLabel,workDate){\n  const options={ready:\'Готов\',not_ready:\'На основной\',day_off:\'Выходной\',responded:\'Отписался / свободен\'};\n  showModal(`✍️ ${groupLabel}`, `\n    <p class="muted">Дата: ${esc(workDate)}. Запись учитывается только на эту дату.</p>\n    <form id="manualReadinessForm">\n      <div class="field full"><label for="manualReadyName">Имя сотрудника</label><input id="manualReadyName" required maxlength="120" autocomplete="off" placeholder="Иван"></div>\n      <div class="field full"><label for="manualReadyMetro">Метро</label><input id="manualReadyMetro" required maxlength="120" placeholder="Марьино"></div>\n      <div class="field full"><label for="manualReadyHeight">Рост, см</label><input id="manualReadyHeight" required type="number" inputmode="numeric" min="100" max="250" step="1" placeholder="185"></div>\n      <div class="field full"><label for="manualReadyStatus">Статус</label><select id="manualReadyStatus">${Object.entries(options).map(([key,label])=>`<option value="${key}" ${key===status?\'selected\':\'\'}>${label}</option>`).join(\'\')}</select></div>\n      <button class="primary" style="width:100%;min-height:48px" type="submit">Добавить в подсчёт</button>\n      <p class="muted">Для сотрудника из списка вводите имя, метро и рост так же, как в его записи.</p>\n    </form>`);\n  qs(\'#manualReadinessForm\').onsubmit=async e=>{\n    e.preventDefault();const button=e.target.querySelector(\'button[type="submit"]\');button.disabled=true;\n    try{\n      await api(\'/api/readiness/manual\',{method:\'POST\',body:JSON.stringify({\n        work_date:workDate,group_code:code,status:qs(\'#manualReadyStatus\').value,\n        full_name:qs(\'#manualReadyName\').value.trim(),metro:qs(\'#manualReadyMetro\').value.trim(),\n        height_cm:Number(qs(\'#manualReadyHeight\').value)\n      })});\n      closeModal();await loadReadiness();toast(\'Сотрудник учтён\');\n    }catch(e){toast(e.message,4500);button.disabled=false;}\n  };\n}\n\nasync function loadReadiness(){\n  const r=await api(`/api/readiness/summary?work_date=${state.selectedDate}`);\n\n  const groupEntries=Object.entries(r.groups||{});\n  const currentNoResponse=groupEntries.reduce((sum,[,g])=>sum+Number(g?.counts?.no_response||0),0);\n  const respondedNow=groupEntries.reduce((sum,[,g])=>\n    sum\n    + Number(g?.counts?.ready||0)\n    + Number(g?.counts?.not_ready||0)\n    + Number(g?.counts?.day_off||0)\n    + Number(g?.counts?.responded||0)\n  ,0);\n\n  qs(\'#readinessSummary\').innerHTML=`\n    <div class="metric"><b>${r.totals.employees}</b><span>Сотрудников</span></div>\n    <div class="metric good"><b>${r.totals.ready_cutoff}</b><span>Готовы 15:30</span></div>\n    <div class="metric"><b>${r.totals.ready_now}</b><span>Готовы сейчас</span></div>\n    <div class="metric ${currentNoResponse?\'warn\':\'\'}"><b>${currentNoResponse}</b><span>Не отписались</span></div>`;\n\n  qs(\'#readinessGroups\').innerHTML=groupEntries.map(([code,g])=>{\n    const ready=Number(g.counts.ready||0);\n    const mainJob=Number(g.counts.not_ready||0);\n    const dayOff=Number(g.counts.day_off||0);\n    const free=Number(g.counts.responded||0);\n    const noResponse=Number(g.counts.no_response||0);\n    const answered=ready+mainJob+dayOff+free;\n    const total=answered+noResponse;\n\n    if(code===\'reserve\'){\n      const wrotePeople=[\n        ...(g.ready||[]),\n        ...(g.responded||[]),\n        ...(g.not_ready||[]),\n        ...(g.day_off||[])\n      ];\n      return `<div class="group-block">\n        <div class="group-title"><span>${esc(g.label)}</span><span>${answered}/${total}</span></div>\n        <div class="group-stats">\n          <button class="${wrotePeople.length?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="wrote"><b>${wrotePeople.length}</b>Отписались</button>\n          <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готовы</button>\n          <button class="${free?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="responded"><b>${free}</b>Свободная</button>\n          <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n        </div>\n      </div>`;\n    }\n\n    return `<div class="group-block">\n      <div class="group-title"><span>${esc(g.label)}</span><span>${answered}/${total}</span></div>\n      <div class="group-stats">\n        <button class="${ready?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="ready"><b>${ready}</b>Готов</button>\n        <button class="${mainJob?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="not_ready"><b>${mainJob}</b>На основной</button>\n        <button class="${dayOff?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="day_off"><b>${dayOff}</b>Выходной</button>\n        <button class="${noResponse?\'has-people\':\'\'}" data-rgroup="${code}" data-rstatus="no_response"><b>${noResponse}</b>Не отпис.</button>\n      </div>\n    </div>`;\n  }).join(\'\');\n\n  qsa(\'[data-rgroup]\',qs(\'#readinessGroups\')).forEach(btn=>{\n    btn.onclick=()=>{\n      const code=btn.dataset.rgroup;\n      const status=btn.dataset.rstatus;\n      const g=r.groups?.[code]||{};\n      let people=[];\n      let label=\'\';\n\n      if(status===\'ready\'){people=g.ready||[];label=\'Готов\';}\n      else if(status===\'not_ready\'){people=g.not_ready||[];label=\'На основной\';}\n      else if(status===\'day_off\'){people=g.day_off||[];label=\'Выходной\';}\n      else if(status===\'responded\'){people=g.responded||[];label=\'Свободная\';}\n      else if(status===\'no_response\'){people=g.no_response||[];label=\'Не отписались\';}\n      else if(status===\'wrote\'){\n        people=[\n          ...(g.ready||[]),\n          ...(g.responded||[]),\n          ...(g.not_ready||[]),\n          ...(g.day_off||[])\n        ];\n        label=\'Отписались\';\n      }\n\n      openReadinessPeople(g.label||code,label,people,code,status,state.selectedDate);\n    };\n  });\n}\n\nasync function loadEmployees(){\n  const params=state.employeeGroup===\'all\'?\'\':`?group=${state.employeeGroup}`;\n\n  const [list, readiness] = await Promise.all([\n    api(`/api/employees${params}`),\n    api(`/api/readiness/summary?work_date=${state.selectedDate}`)\n  ]);\n\n  const counts = {\n    all: Number(readiness?.totals?.ready_now || 0),\n    brigadier: Number(readiness?.groups?.brigadier?.counts?.ready || 0),\n    main: Number(readiness?.groups?.main?.counts?.ready || 0),\n    cashless: Number(readiness?.groups?.cashless?.counts?.ready || 0),\n    reserve: Number(readiness?.groups?.reserve?.counts?.ready || 0),\n  };\n\n  qsa(\'[data-ready-count]\').forEach(el=>{\n    const group=el.dataset.readyCount;\n    el.textContent=String(counts[group] ?? 0);\n  });\n\n  const v=(state.employeeSearch||\'\').trim().toLowerCase();\n  const ownerTgId=Number(state.bootstrap?.user?.id||0);\n\n  const filtered=list\n    .filter(x=>{\n      if(!v) return true;\n      const haystack=[\n        x.full_name,\n        x.display_name,\n        x.metro,\n        x.height_cm,\n      ].filter(Boolean).join(\' \').toLowerCase();\n      return haystack.includes(v);\n    })\n    .sort((a,b)=>{\n      const aOwner=ownerTgId && Number(a.tg_id||0)===ownerTgId;\n      const bOwner=ownerTgId && Number(b.tg_id||0)===ownerTgId;\n      if(aOwner!==bOwner) return aOwner ? -1 : 1;\n      return String(a.display_name||a.full_name||\'\').localeCompare(\n        String(b.display_name||b.full_name||\'\'),\n        \'ru\',\n        {sensitivity:\'base\'}\n      );\n    });\n\n  qs(\'#employeesList\').innerHTML=filtered.length\n    ? filtered.map((x,i)=>`\n      <div class="employee-row">\n        <div class="name">\n          <b>${i+1}. ${esc(x.display_name||x.full_name)}</b>\n          <small>${esc(x.group_label)}${x.telegram_username?` · @${esc(x.telegram_username)}`:\'\'}</small>\n          ${contactButtons(x)}\n        </div>\n        <div class="height">${x.height_cm||\'—\'} см</div>\n        <div class="car">${x.has_car?\'🚗\':\'\'}</div>\n      </div>\n    `).join(\'\')\n    : `<div class="empty">Ничего не найдено</div>`;\n\n  wireContactButtons(qs(\'#employeesList\'));\n}\n\nasync function loadControl(){\n  const c=await api(`/api/control?work_date=${state.selectedDate}`); const no=c.no_response_cutoff; const incomplete=c.orders.not_complete;\n  qs(\'#controlContent\').innerHTML=`\n    <div class="attention ${incomplete.length?\'red\':\'\'}"><h4>Не укомплектованы · ${incomplete.length}</h4><div class="attention-list">${incomplete.length?incomplete.map(o=>`<div>${o.issue_time} · ${esc(o.deceased_name)} · ${o.brigadier?\'состав \'+o.member_count+\'/\'+o.member_target:\'без бригадира\'}</div>`).join(\'\'):\'Все заказы укомплектованы\'}</div></div>\n    <div class="attention ${no.length?\'red\':\'\'}"><h4>Не отписались к 15:30 · ${no.length}</h4><div class="attention-list">${no.length?no.map(x=>`<div>${esc(x.display_name||x.full_name)} · ${esc(x.group_label)}</div>`).join(\'\'):\'Все сотрудники отписались\'}</div></div>\n    <div class="attention"><h4>Нагрузка</h4><div class="attention-list">${c.workload.length?c.workload.map(x=>`<div>${esc(x.display_name||x.full_name)} · ${x.workload}/2</div>`).join(\'\'):\'Назначений пока нет\'}</div></div>`;\n}\n\n\nasync function openReadinessHistory(){\n  try{\n    const rows=await api(`/api/readiness/history?work_date=${state.selectedDate}&limit=60`);\n    showModal(\'🧾 История готовности\', `\n      <p class="muted">Все изменения статусов сохраняются в базе. Удаление старого сообщения в Telegram не удаляет отметку готовности.</p>\n      <div class="list">${rows.length?rows.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(x.display_name||x.full_name)}</b><small>${esc(x.group_label)} · ${esc(x.changed_at_label)}</small></div><div class="person-meta">${esc(x.old_label)} → <b>${esc(x.new_label)}</b></div></div>`).join(\'\'):\'<div class="empty">Изменений пока нет</div>\'}</div>`);\n  }catch(e){toast(e.message,4000)}\n}\n\nasync function refreshReadinessCount(){\n  try{await loadReadiness();toast(\'Количество обновлено\')}catch(e){toast(e.message,4000)}\n}\n\nasync function openEmployeeImport(){\n  showModal(\'Импорт сотрудников\', `\n    <p class="muted">Одна строка — один сотрудник. Формат: ФИО; группа; рост; авто; телефон; @telegram; Telegram ID</p>\n    <p class="muted">Группа: бригадиры / основной / безнал / резерв. Телефон, @telegram и Telegram ID можно оставить пустыми.</p>\n    <div class="field"><label>Список</label><textarea id="employeeImportText" placeholder="Иван Иванов; основной; 186; авто; +79990000000; @ivanov; 123456789\nПетр Петров; резерв; 184; ; ; ;"></textarea></div>\n    <button class="primary" id="employeeImportSave" style="width:100%">Импортировать</button>`);\n  qs(\'#employeeImportSave\').onclick=async()=>{\n    const raw=qs(\'#employeeImportText\').value.trim(); if(!raw){toast(\'Вставьте список\');return}\n    const groupMap={\'бригадиры\':\'brigadier\',\'бригадир\':\'brigadier\',\'основной\':\'main\',\'основной состав\':\'main\',\'безнал\':\'cashless\',\'безнал состав\':\'cashless\',\'резерв\':\'reserve\'};\n    const items=[];\n    for(const line of raw.split(/\\n+/)){\n      const p=line.split(\';\').map(x=>x.trim()); if(!p[0]) continue; const group=groupMap[(p[1]||\'\').toLowerCase()]; if(!group){toast(`Не понял группу: ${p[1]||\'—\'}`,3500);return}\n      items.push({full_name:p[0],group_code:group,height_cm:p[2]?Number(p[2]):null,has_car:/авто|да|yes|\\+/.test((p[3]||\'\').toLowerCase()),phone:p[4]||\'\',telegram_username:(p[5]||\'\').replace(\'@\',\'\'),tg_id:p[6]?Number(p[6]):null,active:true});\n    }\n    try{await api(\'/api/employees/bulk\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Импортировано: ${items.length}`);await loadEmployees()}catch(e){toast(e.message,4000)}\n  };\n}\n\nfunction manualOrderFormHtml(){\n  return `\n    <p class="muted">Номер присвоится автоматически после сохранения и останется постоянным.</p>\n    <div class="form-grid">\n      <div class="field"><label>Тип заказа</label><select id="manualSource"><option value="private">Частный</option><option value="gbu">ГБУ</option></select></div>\n      <div class="field"><label>Дата</label><input id="manualWorkDate" type="date" value="${state.selectedDate}"></div>\n      <div class="field full"><label>Категория заказа</label><select id="manualCategory"><option value="standard">Стандарт</option><option value="vip">Вип</option><option value="elite">Элит</option></select></div>\n      <div class="field"><label>Агент</label><input id="manualAgentName" autocomplete="off" placeholder="ФИО агента"></div>\n      <div class="field"><label>Номер агента</label><input id="manualAgentPhone" inputmode="tel" autocomplete="tel" placeholder="+7 ..."></div>\n      <div class="field"><label>Время подачи</label><input id="manualArrivalTime" type="time"></div>\n      <div class="field"><label>Время выдачи</label><input id="manualIssueTime" type="time"></div>\n      <div class="field full"><label>ФИО умершего</label><input id="manualDeceasedName" autocomplete="off"></div>\n      <div class="field full"><label>Маршрут</label><textarea id="manualRoute" placeholder="Морг → ... → конечная точка"></textarea></div>\n      <div class="field full"><label>Количество человек</label><select id="manualTeamSize"><option value="4">4 человека</option><option value="6">6 человек</option></select></div>\n    </div>\n    <button class="primary" id="saveManualOrderBtn" style="width:100%;margin-top:12px">Сохранить новый заказ</button>`;\n}\n\nasync function openManualOrder(){\n  showModal(\'＋ Добавить заказ\', manualOrderFormHtml());\n  const source = qs(\'#manualSource\');\n  const category = qs(\'#manualCategory\');\n  const syncSourceRules = ()=>{\n    if(source.value===\'gbu\'){\n      category.value=\'standard\';\n      category.disabled=true;\n    }else{\n      category.disabled=false;\n    }\n  };\n  source.onchange=syncSourceRules;\n  syncSourceRules();\n  qs(\'#saveManualOrderBtn\').onclick=saveManualOrder;\n}\n\nasync function saveManualOrder(){\n  const get=id=>qs(id)?.value?.trim()||\'\';\n  const source=get(\'#manualSource\')||\'private\';\n  const payload={\n    source,\n    work_date:get(\'#manualWorkDate\'),\n    category:source===\'gbu\'?\'standard\':get(\'#manualCategory\'),\n    agent_name:get(\'#manualAgentName\'),\n    agent_phone:get(\'#manualAgentPhone\'),\n    arrival_time:get(\'#manualArrivalTime\')||null,\n    issue_time:get(\'#manualIssueTime\'),\n    deceased_name:get(\'#manualDeceasedName\'),\n    route:get(\'#manualRoute\'),\n    team_size:Number(get(\'#manualTeamSize\')||4),\n    organization:source===\'gbu\'?\'ГБУ\':\'\',\n    notes:\'\'\n  };\n  const missing=[];\n  if(!payload.work_date) missing.push(\'дата\');\n  if(!payload.agent_name) missing.push(\'агент\');\n  if(!payload.agent_phone) missing.push(\'номер агента\');\n  if(!payload.arrival_time) missing.push(\'время подачи\');\n  if(!payload.issue_time) missing.push(\'время выдачи\');\n  if(!payload.deceased_name) missing.push(\'ФИО умершего\');\n  if(!payload.route) missing.push(\'маршрут\');\n  if(missing.length){toast(\'Заполните: \'+missing.join(\', \'),4500);return}\n  const btn=qs(\'#saveManualOrderBtn\');\n  btn.disabled=true;btn.textContent=\'Сохраняю…\';\n  try{\n    const created=await api(\'/api/orders\',{method:\'POST\',body:JSON.stringify(payload)});\n    closeModal();\n    state.selectedDate=created.work_date||payload.work_date;\n    toast(`Создан заказ №${created.id}`);\n    await loadBootstrap(state.selectedDate);\n  }catch(e){\n    toast(e.message,4500);\n    btn.disabled=false;btn.textContent=\'Сохранить новый заказ\';\n  }\n}\n\nasync function openGbuImport(){\n  showModal(\'Импорт ГБУ\', `\n    <p class="muted">Первые два столбца игнорируются. Время в таблице — <b>подача</b>; выдача автоматически +30 минут. 4 и 6 человек — категория Стандарт.</p>\n    <div class="field"><label>Дата заказов</label><input type="date" id="gbuWorkDate" value="${state.selectedDate}"></div>\n    <div class="field"><label>Фото таблицы</label><input type="file" id="gbuFile" accept="image/*"></div>\n    <button class="primary" id="ocrBtn" style="width:100%">Распознать фото</button>\n    <div id="gbuResult" style="margin-top:12px"></div>`);\n  qs(\'#ocrBtn\').onclick=async()=>{\n    const f=qs(\'#gbuFile\').files[0]; if(!f){toast(\'Выберите фото\');return}\n    const workDate=qs(\'#gbuWorkDate\').value; if(!workDate){toast(\'Укажите дату\');return}\n    const fd=new FormData();fd.append(\'file\',f);fd.append(\'work_date\',workDate); qs(\'#ocrBtn\').disabled=true; qs(\'#ocrBtn\').textContent=\'Распознаю…\';\n    try{const r=await api(\'/api/gbu/ocr\',{method:\'POST\',body:fd}); renderGbuDrafts(r.drafts,r.raw_text,r.warning)}catch(e){toast(e.message,5000)}finally{qs(\'#ocrBtn\').disabled=false;qs(\'#ocrBtn\').textContent=\'Распознать фото\'}\n  };\n}\n\nfunction renderGbuDrafts(drafts, raw, warning=\'\'){\n  const el=qs(\'#gbuResult\'); const rows=(drafts||[]).map((d,i)=>gbuDraftHtml(d,i)).join(\'\');\n  el.innerHTML=`${warning?`<div class="attention"><p>${esc(warning)}</p></div>`:\'\'}<details><summary class="muted">Показать распознанный текст</summary><div class="field"><textarea readonly>${esc(raw)}</textarea></div></details><h4>Черновики заказов</h4><div id="gbuDrafts">${rows||\'<div class="empty">Строки автоматически не найдены. Добавьте заказ вручную.</div>\'}</div><button class="secondary" id="addGbuRow" style="width:100%;margin-bottom:8px">＋ Добавить строку</button><button class="primary" id="saveGbuDrafts" style="width:100%">Сохранить заказы ГБУ</button>`;\n  qs(\'#addGbuRow\').onclick=()=>{const c=qs(\'#gbuDrafts\');const i=qsa(\'.gbu-draft\',c).length;c.insertAdjacentHTML(\'beforeend\',gbuDraftHtml({work_date:state.selectedDate,arrival_time:\'\',issue_time:\'\',deceased_name:\'\',route:\'\',team_size:4,category:\'standard\',agent_name:\'\',agent_phone:\'\'},i))};\n  qs(\'#saveGbuDrafts\').onclick=saveGbuDrafts;\n}\n\nfunction gbuDraftHtml(d,i){return `<div class="gbu-draft" data-i="${i}"><div class="form-grid"><div class="field"><label>Дата</label><input data-k="work_date" type="date" value="${d.work_date||state.selectedDate}"></div><div class="field"><label>Категория заказа</label><input value="Стандарт" disabled></div><div class="field"><label>Агент</label><input data-k="agent_name" value="${esc(d.agent_name||\'\')}"></div><div class="field"><label>Номер агента</label><input data-k="agent_phone" value="${esc(d.agent_phone||\'\')}"></div><div class="field"><label>Время подачи</label><input data-k="arrival_time" type="time" value="${d.arrival_time||\'\'}"></div><div class="field"><label>Время выдачи</label><input data-k="issue_time" type="time" value="${d.issue_time||\'\'}"></div><div class="field full"><label>ФИО умершего</label><input data-k="deceased_name" value="${esc(d.deceased_name||\'\')}"></div><div class="field full"><label>Маршрут</label><textarea data-k="route">${esc(d.route||\'\')}</textarea></div><div class="field full"><label>Количество человек</label><select data-k="team_size"><option value="4" ${Number(d.team_size)!==6?\'selected\':\'\'}>4</option><option value="6" ${Number(d.team_size)===6?\'selected\':\'\'}>6</option></select></div>${d.agent_match_confidence!==undefined?`<div class="field full"><label>Совпадение со справочником</label><div class="muted">${d.agent_phone?\'✅ номер найден\':\'⚠️ номер не найден — проверьте вручную\'}</div></div>`:\'\'}</div></div>`}\n\nasync function saveGbuDrafts(){\n  const items=qsa(\'.gbu-draft\').map(row=>{const get=k=>qs(`[data-k="${k}"]`,row).value;return {work_date:get(\'work_date\'),arrival_time:get(\'arrival_time\')||null,issue_time:get(\'issue_time\'),deceased_name:get(\'deceased_name\'),route:get(\'route\'),category:\'standard\',team_size:Number(get(\'team_size\')),organization:\'ГБУ\',agent_name:get(\'agent_name\'),agent_phone:get(\'agent_phone\'),notes:\'\'}}).filter(x=>x.work_date&&x.issue_time&&x.deceased_name);\n  if(!items.length){toast(\'Нет заполненных строк\');return}\n  try{await api(\'/api/gbu/drafts\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Добавлено: ${items.length}`);await loadBootstrap(state.selectedDate)}catch(e){toast(e.message,4000)}\n}\n\nasync function openGbuAgents(){\n  showModal(\'⬛ Агенты ГБУ\', `<p class="muted">Справочник номеров из загруженного списка ГБУ. Нажмите номер, чтобы скопировать.</p><input class="search" id="gbuAgentSearch" placeholder="ФИО или номер"><div id="gbuAgentsList" class="list"></div>`);\n  async function draw(){\n    const q=encodeURIComponent(qs(\'#gbuAgentSearch\').value.trim()); const list=await api(`/api/gbu/agents?search=${q}`);\n    qs(\'#gbuAgentsList\').innerHTML=list.length?list.map(x=>`<div class="employee-row"><div class="name"><b>${esc(x.display_name||x.full_name)}</b><small>ГБУ${x.source_page?` · стр. ${x.source_page}`:\'\'}</small><div class="contact-btns"><button class="contact-btn" data-copy-phone="${esc(x.phone)}">📋 ${esc(x.phone)}</button></div></div></div>`).join(\'\'):\'<div class="empty">Не найдено</div>\'; wireContactButtons(qs(\'#gbuAgentsList\'));\n  }\n  qs(\'#gbuAgentSearch\').oninput=draw; await draw();\n}\n\nasync function openCashTable(){\n  if(!state.bootstrap?.is_owner){toast(\'Касса доступна только владельцу\');return}\n\n  showModal(\'💰 Касса\', `\n    <p class="muted">В столбцах указаны имя и метро бригадира. Откат считается отдельно. Суммы можно исправлять вручную.</p>\n    <div id="cashTableBody"><div class="empty">Загрузка…</div></div>\n  `);\n\n  const rub=n=>new Intl.NumberFormat(\'ru-RU\').format(Number(n||0))+\' ₽\';\n\n  async function editCashEntry(entry){\n    const raw=prompt(\n      `Новая сумма кассы\\n${entry.brigadier_name} · ${fmtDate(entry.work_date)}`,\n      String(Number(entry.commission_rub||0))\n    );\n    if(raw===null) return;\n\n    const value=Number(String(raw).replace(/\\s/g,\'\').replace(/[^\\d]/g,\'\'));\n    if(!Number.isFinite(value) || value<0){\n      toast(\'Введите корректную сумму\');\n      return;\n    }\n\n    await api(`/api/cash/${entry.id}`,{\n      method:\'PATCH\',\n      body:{commission_rub:Math.round(value)}\n    });\n    toast(\'Касса изменена\');\n    await drawCash();\n  }\n\n  async function drawCash(){\n    const body=qs(\'#cashTableBody\');\n    if(!body) return;\n\n    // Запоминаем положение окна и горизонтальную прокрутку таблицы.\n    // Автообновление больше не возвращает владельца в начало.\n    const modal=qs(\'#modal\');\n    const savedModalScroll=modal ? modal.scrollTop : 0;\n    const oldScroller=qs(\'#cashMatrixScroller\');\n    const savedTableScroll=oldScroller ? oldScroller.scrollLeft : 0;\n\n    try{\n      const [ledger, selectedDay]=await Promise.all([\n        api(`/api/cash/ledger?on_date=${state.selectedDate}`),\n        api(`/api/cash?work_date=${state.selectedDate}`)\n      ]);\n      if(!qs(\'#cashTableBody\')) return;\n\n      const brigadiers=ledger.brigadiers||[];\n      const days=ledger.days||[];\n      const selectedRows=selectedDay.rows||[];\n      const totalsByBrig=new Map((ledger.brigadier_totals||[]).map(x=>[String(x.brigadier_tg_id),x]));\n\n      const matrix=brigadiers.length ? `\n        <div id="cashMatrixScroller" style="overflow:auto;border:1px solid var(--line);border-radius:16px;margin:10px 0 12px">\n          <table style="border-collapse:separate;border-spacing:0;min-width:max-content;width:100%;font-size:11px">\n            <thead>\n              <tr>\n                <th style="position:sticky;left:0;z-index:3;background:#18181c;padding:10px 6px;border-bottom:1px solid var(--line);text-align:center;min-width:42px">День</th>\n                ${brigadiers.map(b=>`<th style="background:#18181c;padding:10px;border-bottom:1px solid var(--line);min-width:120px">${esc(b.name)}</th>`).join(\'\')}\n                <th style="position:sticky;right:0;z-index:3;background:#211d15;color:var(--gold);padding:10px;border-bottom:1px solid var(--line);min-width:130px">Итого за день</th>\n              </tr>\n            </thead>\n            <tbody>\n              ${days.map(day=>`\n                <tr>\n                  <td style="position:sticky;left:0;z-index:2;background:#121215;padding:9px 6px;border-bottom:1px solid var(--line);font-weight:900;text-align:center;min-width:42px">${dayNumber(day.work_date)}</td>\n                  ${(day.cells||[]).map(cell=>`\n                    <td style="padding:8px 10px;border-bottom:1px solid var(--line);text-align:center">\n                      <b>${cell.commission_rub?rub(cell.commission_rub):\'—\'}</b>\n                      ${cell.kickback_rub?`<small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(cell.kickback_rub)}</small>`:\'\'}\n                    </td>\n                  `).join(\'\')}\n                  <td style="position:sticky;right:0;z-index:2;background:#19170f;padding:8px 10px;border-bottom:1px solid var(--line);text-align:center">\n                    <b style="color:var(--gold)">${rub(day.day?.commission_rub||0)}</b>\n                    <small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(day.day?.kickback_rub||0)}</small>\n                  </td>\n                </tr>\n              `).join(\'\')}\n              <tr>\n                <td style="position:sticky;left:0;z-index:2;background:#1b1b1f;padding:10px;font-weight:900">Итого</td>\n                ${brigadiers.map(b=>{\n                  const t=totalsByBrig.get(String(b.tg_id))||{};\n                  return `<td style="background:#1b1b1f;padding:10px;text-align:center"><b>${rub(t.commission_rub||0)}</b><small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(t.kickback_rub||0)}</small></td>`\n                }).join(\'\')}\n                <td style="position:sticky;right:0;z-index:3;background:#2a251b;padding:10px;text-align:center"><b style="color:var(--gold)">${rub(ledger.period?.commission_rub||0)}</b><small style="display:block;color:var(--muted);margin-top:3px">откат ${rub(ledger.period?.kickback_rub||0)}</small></td>\n              </tr>\n            </tbody>\n          </table>\n        </div>\n      ` : \'<div class="empty">Кассовые бригадиры пока не добавлены</div>\';\n\n      const dayRows=selectedRows.map(x=>`\n        <div class="employee-row">\n          <div class="name">\n            <b>${esc(x.brigadier_name)}</b>\n            <small>${esc(x.category_label)} · ${x.team_size} чел${x.reserve_count?` · резерв ${x.reserve_count}`:\'\'}</small>\n            <div class="muted">Касса ${rub(x.commission_rub)} · откат отдельно ${rub(x.kickback_rub)}</div>\n          </div>\n          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">\n            <button class="contact-btn" data-cash-edit="${x.id}">✏️ Изменить</button>\n            <button class="contact-btn danger" data-cash-del="${x.id}">Удалить</button>\n          </div>\n        </div>\n      `).join(\'\');\n\n      body.innerHTML=`\n        <div class="summary-grid compact">\n          <div class="metric good"><b>${rub(ledger.period?.commission_rub||0)}</b><span>Касса периода</span></div>\n          <div class="metric"><b>${rub(ledger.period?.kickback_rub||0)}</b><span>Откат отдельно</span></div>\n        </div>\n        <div class="attention">\n          <h4>${fmtMonthYear(ledger.start)}</h4>\n          <p>Период: ${dayNumber(ledger.start)}–${dayNumber(ledger.end)}. Каждый столбец — имя и метро бригадира.</p>\n        </div>\n        ${matrix}\n        <div class="section-head" style="margin-top:16px">\n          <div><h2 style="font-size:16px">Записи за ${fmtDate(state.selectedDate)}</h2><p class="muted">Здесь можно вручную изменить кассу сотрудника</p></div>\n          <button class="secondary" id="cashRefreshNow">↻</button>\n        </div>\n        <div class="list">${dayRows||\'<div class="empty">На выбранную дату записей нет</div>\'}</div>\n      `;\n\n      qsa(\'[data-cash-edit]\',body).forEach(btn=>btn.onclick=async()=>{\n        const entry=selectedRows.find(x=>Number(x.id)===Number(btn.dataset.cashEdit));\n        if(entry) await editCashEntry(entry);\n      });\n\n      qsa(\'[data-cash-del]\',body).forEach(btn=>btn.onclick=async()=>{\n        if(!confirm(\'Удалить запись кассы?\')) return;\n        await api(`/api/cash/${btn.dataset.cashDel}`,{method:\'DELETE\'});\n        toast(\'Удалено\');\n        await drawCash();\n      });\n\n      const refresh=qs(\'#cashRefreshNow\');\n      if(refresh) refresh.onclick=drawCash;\n\n      // Возвращаем пользователя ровно туда, где он смотрел таблицу.\n      requestAnimationFrame(()=>{\n        const currentModal=qs(\'#modal\');\n        const currentScroller=qs(\'#cashMatrixScroller\');\n        if(currentModal) currentModal.scrollTop=savedModalScroll;\n        if(currentScroller) currentScroller.scrollLeft=savedTableScroll;\n      });\n    }catch(e){\n      if(qs(\'#cashTableBody\')) qs(\'#cashTableBody\').innerHTML=`<div class="empty">${esc(e.message)}</div>`;\n    }\n  }\n\n  await drawCash();\n  cashRefreshTimer=setInterval(()=>{\n    if(qs(\'#cashTableBody\')) drawCash();\n  },10000);\n}\n\n// Events\nqs(\'#cashBtn\').onclick=openCashTable;\nqs(\'#refreshBtn\').onclick=()=>loadBootstrap(state.selectedDate).catch(e=>toast(e.message));\nqs(\'#drawerBackdrop\').onclick=closeDrawer; qs(\'#modalBackdrop\').onclick=closeModal; qs(\'#newOrderBtn\').onclick=openManualOrder; qs(\'#gbuBtn\').onclick=openGbuImport;\nqsa(\'.bottom-nav button\').forEach(btn=>btn.onclick=async()=>{qsa(\'.bottom-nav button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');qsa(\'.view\').forEach(x=>x.classList.remove(\'active\'));qs(`#${btn.dataset.view}`).classList.add(\'active\');await renderCurrentView()});\nqsa(\'#sourceFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#sourceFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.source=btn.dataset.source;await loadOrders()});\nqsa(\'#statusFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#statusFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.status=btn.dataset.status;await loadOrders()});\nqsa(\'#employeeFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#employeeFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.employeeGroup=btn.dataset.group;await loadEmployees()});\nqs(\'#employeeSearch\').oninput=e=>{state.employeeSearch=e.target.value;loadEmployees()};\nqs(\'#employeeImportBtn\').onclick=openEmployeeImport;\nqs(\'#gbuAgentsBtn\').onclick=openGbuAgents;\nqs(\'#readinessCountBtn\').onclick=refreshReadinessCount;\nqs(\'#readinessHistoryBtn\').onclick=openReadinessHistory;\n\nasync function autoRefresh(){\n  if(document.hidden) return;\n  if(!qs(\'#modal\').classList.contains(\'hidden\')) return;\n  try{\n    state.bootstrap=await api(`/api/bootstrap?work_date=${state.selectedDate}`); const cashBtn=qs(\'#cashBtn\'); if(cashBtn) cashBtn.classList.toggle(\'hidden\', !state.bootstrap.is_owner); renderSummary(); renderDates();\n    await renderCurrentView();\n    if(state.currentOrder && !qs(\'#orderDrawer\').classList.contains(\'hidden\')){\n      const fresh=await api(`/api/orders/${state.currentOrder.id}`); state.currentOrder=fresh; qs(\'#orderDrawer\').innerHTML=orderDrawerHtml(fresh); wireOrderDrawer(fresh);\n    }\n  }catch(e){}\n}\nsetInterval(autoRefresh,10000);\n\nloadBootstrap().catch(e=>fatal(e.message));\n\n</script>\n</body>\n</html>\n'

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INLINE_INDEX_HTML)


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
    tg_id: int
    full_name: str = ""


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


def _require_owner(db: Session, user: TelegramUser) -> None:
    if not _is_owner(db, user.id):
        raise HTTPException(status_code=403, detail="Касса доступна только владельцу")


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
    }


def _employee_dict(emp: Employee, workload: int = 0, readiness_status: str | None = None, brig_height: int | None = None):
    diff = abs((emp.height_cm or 0) - brig_height) if brig_height and emp.height_cm else None
    return {
        "id": emp.id,
        "tg_id": emp.tg_id,
        "full_name": emp.full_name,
        "display_name": ((emp.full_name.split()[0] if emp.full_name else "") + (f" {emp.metro.strip()}" if emp.group_code == "brigadier" and (emp.metro or "").strip() else "") + (f" {emp.height_cm}" if emp.group_code == "brigadier" and emp.height_cm else "")).strip() or emp.full_name,
        "phone": emp.phone,
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
    if not data.preserve_identity or not emp.full_name:
        emp.full_name = data.full_name.strip()
    if data.phone.strip():
        emp.phone = data.phone.strip()
    if data.telegram_username.strip():
        emp.telegram_username = data.telegram_username.strip().lstrip("@")
    emp.group_code = data.group_code
    if data.height_cm is not None:
        emp.height_cm = data.height_cm
    emp.has_car = data.has_car
    if data.metro.strip():
        emp.metro = data.metro.strip()
    if data.is_cashier is not None:
        emp.is_cashier = bool(data.is_cashier)
    emp.active = data.active and emp.tg_id not in HIDDEN_TG_IDS
    if emp.tg_id in HIDDEN_TG_IDS:
        emp.is_cashier = False
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
        "is_owner": _is_owner(db, user.id),
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


class ManualReadinessInput(BaseModel):
    work_date: date
    group_code: Literal["brigadier", "main", "cashless", "reserve"]
    status: Literal["ready", "not_ready", "day_off", "responded"]
    full_name: str = Field(min_length=1, max_length=120)
    metro: str = Field(min_length=1, max_length=120)
    height_cm: int = Field(ge=100, le=250)


@app.post("/api/readiness/manual")
def save_manual_readiness(data: ManualReadinessInput, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    name, metro = " ".join(data.full_name.split()), " ".join(data.metro.split())
    if not name or not metro:
        raise HTTPException(422, "Укажите имя и метро сотрудника")
    key = manual_readiness_identity(name, metro, data.height_cm)
    row = db.execute(select(ManualReadiness).where(
        ManualReadiness.work_date == data.work_date, ManualReadiness.identity_key == key
    ).with_for_update()).scalar_one_or_none()
    if row is None:
        row = ManualReadiness(work_date=data.work_date, identity_key=key)
        db.add(row)
    row.full_name, row.metro, row.height_cm = name, metro, data.height_cm
    row.group_code, row.status = data.group_code, data.status
    row.reported_at, row.created_by_tg_id = datetime.utcnow(), user.id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Запись уже добавлена. Обновите список и повторите при необходимости.")
    return {"ok": True, "id": row.id}


@app.delete("/api/readiness/manual/{entry_id}")
def delete_manual_readiness(entry_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    row = db.get(ManualReadiness, entry_id)
    if row is None:
        raise HTTPException(404, "Ручная запись уже удалена")
    db.delete(row)
    db.commit()
    return {"ok": True}


def apply_manual_readiness(db, work_date, groups, cutoff):
    """Merge date-scoped count corrections without changing the staff directory."""
    statuses = ("ready", "not_ready", "day_off", "responded", "no_response")
    manual_rows = db.execute(select(ManualReadiness).where(ManualReadiness.work_date == work_date)).scalars().all()
    for row in manual_rows:
        matches = []
        for group in groups.values():
            for status in statuses:
                for item in group[status]:
                    if manual_readiness_identity(item.get("full_name"), item.get("metro"), item.get("height_cm")) == row.identity_key:
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
        item.update(group_code=row.group_code, group_label=GROUP_LABELS[row.group_code],
                    readiness_status=row.status, manual_entry_id=row.id,
                    reported_at=row.reported_at.isoformat())
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


def readiness_summary_data(db: Session, work_date: date):
    employees = db.execute(select(Employee).where(Employee.active.is_(True), _visible_employee()).order_by(Employee.group_code, Employee.full_name)).scalars().all()
    ready_rows = _ready_map(db, work_date)
    cutoff = _cutoff_utc_naive(work_date)

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

    for g in groups.values():
        g["counts"] = {k: len(g[k]) for k in ["ready", "not_ready", "day_off", "responded", "no_response"]}
        # Official no-response at 15:30 can include a person who reported late.
        g["no_response_cutoff"] = [
            x for bucket in [g["ready"], g["not_ready"], g["day_off"], g["responded"], g["no_response"]]
            for x in bucket if x["cutoff_status"] == "no_response"
        ]

    return {
        "work_date": work_date.isoformat(),
        "cutoff_label": "15:30",
        "totals": manual_totals,
        "groups": groups,
    }


@app.get("/api/employees")
def list_employees(group: str | None = None, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    q = select(Employee).where(Employee.active.is_(True), _visible_employee())
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
    return [_employee_dict(x) for x in result if x.tg_id not in HIDDEN_TG_IDS]


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
            stats["updated"] += 1
        else:
            emp = Employee(
                tg_id=None, full_name=item.full_name.strip(), phone=item.phone,
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
    db.commit(); return {"ok": True}


@app.post("/api/bot/brigadiers/{tg_id}/cashier", dependencies=[Depends(_bot_key)])
def bot_toggle_cashier(tg_id: int, data: CashierToggle, db: Session = Depends(get_db)):
    if tg_id in HIDDEN_TG_IDS:
        raise HTTPException(404, "Бригадир не найден")
    emp = db.execute(select(Employee).where(Employee.tg_id == tg_id, Employee.group_code == "brigadier")).scalar_one_or_none()
    if not emp: raise HTTPException(404, "Бригадир не найден в Диспетчерской")
    emp.is_cashier = bool(data.is_cashier); db.commit()
    return {"ok": True, "tg_id": tg_id, "is_cashier": bool(emp.is_cashier)}


@app.post("/api/bot/cash", dependencies=[Depends(_bot_key)])
def bot_create_cash_entry(data: CashEntryCreate, db: Session = Depends(get_db)):
    emp = db.execute(select(Employee).where(Employee.tg_id == data.tg_id, Employee.group_code == "brigadier")).scalar_one_or_none()
    if not emp or not emp.active: raise HTTPException(404, "Бригадир не найден")
    if not emp.is_cashier: raise HTTPException(403, "Этот бригадир не назначен кассовым")
    row = CashEntry(work_date=data.work_date, brigadier_tg_id=data.tg_id, brigadier_name=emp.full_name, category=data.category, team_size=data.team_size, commission_rub=data.commission_rub, kickback_rub=data.kickback_rub, reserve_count=data.reserve_count, notes=data.notes.strip())
    db.add(row); db.commit(); db.refresh(row)
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
    if not emp or not emp.active or not emp.is_cashier:
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
    if not emp or not emp.active or not emp.is_cashier:
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
    rows = db.execute(select(CashEntry).where(CashEntry.brigadier_tg_id == tg_id, CashEntry.work_date >= start, CashEntry.work_date <= end).order_by(CashEntry.work_date, CashEntry.id)).scalars().all()
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
    if not emp or not emp.active or not emp.is_cashier:
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
    _require_owner(db, user)
    rows = db.execute(select(CashEntry).where(CashEntry.work_date == work_date, CashEntry.brigadier_tg_id.notin_(HIDDEN_TG_IDS)).order_by(CashEntry.brigadier_name, CashEntry.id)).scalars().all()
    start, end = _period_bounds(work_date)
    period_rows = db.execute(select(CashEntry).where(CashEntry.work_date >= start, CashEntry.work_date <= end, CashEntry.brigadier_tg_id.notin_(HIDDEN_TG_IDS))).scalars().all()
    return {"work_date": work_date.isoformat(), "rows": [_cash_row_dict(x, db) for x in rows], "day": {"commission_rub": sum(x.commission_rub or 0 for x in rows), "kickback_rub": sum(x.kickback_rub or 0 for x in rows), "cash_rub": sum(x.commission_rub or 0 for x in rows), "total_rub": sum(x.commission_rub or 0 for x in rows)}, "period": {"start": start.isoformat(), "end": end.isoformat(), "commission_rub": sum(x.commission_rub or 0 for x in period_rows), "kickback_rub": sum(x.kickback_rub or 0 for x in period_rows), "cash_rub": sum(x.commission_rub or 0 for x in period_rows), "total_rub": sum(x.commission_rub or 0 for x in period_rows), "entries": len(period_rows)}}


@app.get("/api/cash/ledger")
def owner_cash_ledger(on_date: date, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user)
    start, end = _period_bounds(on_date)

    entries = db.execute(
        select(CashEntry)
        .where(CashEntry.work_date >= start, CashEntry.work_date <= end, CashEntry.brigadier_tg_id.notin_(HIDDEN_TG_IDS))
        .order_by(CashEntry.work_date, CashEntry.brigadier_name, CashEntry.id)
    ).scalars().all()

    current_cashiers = db.execute(
        select(Employee)
        .where(Employee.group_code == "brigadier", Employee.is_cashier.is_(True), _visible_employee())
        .order_by(Employee.full_name)
    ).scalars().all()

    brig_by_tg = {
        int(emp.tg_id): _cash_brigadier_label(db, int(emp.tg_id), emp.full_name)
        for emp in current_cashiers
        if emp.tg_id
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
    _require_owner(db, user); row = db.get(CashEntry, entry_id)
    if not row: raise HTTPException(404, "Запись кассы не найдена")
    for key, value in data.model_dump(exclude_unset=True).items(): setattr(row, key, value)
    db.commit(); db.refresh(row); return _cash_row_dict(row, db)


@app.delete("/api/cash/{entry_id}")
def owner_delete_cash(entry_id: int, db: Session = Depends(get_db), user: TelegramUser = Depends(require_admin)):
    _require_owner(db, user); row = db.get(CashEntry, entry_id)
    if not row: raise HTTPException(404, "Запись кассы не найдена")
    db.delete(row); db.commit(); return {"ok": True}


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
    rows = db.execute(
        select(Order)
        .join(Assignment, Assignment.order_id == Order.id)
        .join(Employee, Employee.id == Assignment.employee_id)
        .where(
            Employee.tg_id == data.tg_id,
            Assignment.role == "brigadier",
            Order.work_date == data.work_date,
            ~Order.status.in_(["closed", "cancelled"]),
        )
        .order_by(Order.issue_time, Order.id)
    ).scalars().all()
    now = datetime.utcnow()
    for o in rows:
        o.brigade_on_contact_at = now
    db.add(ImportLog(
        kind="brigade_contact",
        created_by_tg_id=data.tg_id,
        raw_text=data.raw_text or "",
        result_json=json.dumps({
            "tg_id": data.tg_id,
            "work_date": data.work_date.isoformat(),
            "orders": [o.public_id for o in rows],
        }, ensure_ascii=False),
    ))
    db.commit()
    return {
        "ok": True,
        "count": len(rows),
        "orders": [
            {
                "public_id": o.public_id,
                "issue_time": o.issue_time.strftime("%H:%M"),
                "deceased_name": o.deceased_name,
                "source": o.source,
            }
            for o in rows
        ],
    }


@app.post("/api/bot/readiness", dependencies=[Depends(_bot_key)])
def bot_readiness(data: ReadinessReport, db: Session = Depends(get_db)):
    if data.tg_id in HIDDEN_TG_IDS:
        raise HTTPException(403, "Участник недоступен")
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
                "source": "button" if str(data.raw_text or "").startswith("button:") else "message",
            }, ensure_ascii=False),
        ))
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
        "workload": overloaded,
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "LEGION Dispatcher"}
