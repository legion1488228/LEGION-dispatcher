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
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from database import Base, engine, get_db
from models import AccessUser, Agent, Assignment, Employee, ImportLog, Order, Readiness
from parsers import parse_agent_ready_form, parse_gbu_ocr_text
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

app = FastAPI(title="ЛЕГИОН — Диспетчерская", version="1.0.1")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


INLINE_INDEX_HTML = '<!doctype html>\n<html lang="ru">\n<head>\n  <meta charset="utf-8" />\n  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />\n  <meta name="theme-color" content="#0a0a0b" />\n  <title>ЛЕГИОН — Диспетчерская</title>\n  <script src="https://telegram.org/js/telegram-web-app.js?63"></script>\n  <style>\n:root{\n  --bg:#09090b; --panel:#121215; --panel2:#18181c; --line:#2a2a30;\n  --text:#f5f5f5; --muted:#9a9aa3; --gold:#d8b46a; --gold2:#8f7442;\n  --green:#58c785; --red:#e06969; --amber:#e3b454; --blue:#6ca6e8;\n  --radius:18px; --safe-bottom:env(safe-area-inset-bottom,0px);\n}\n*{box-sizing:border-box} html,body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif} body{min-height:100vh;padding-bottom:calc(82px + var(--safe-bottom))}\nbutton,input,select,textarea{font:inherit}.hidden{display:none!important}\n.topbar{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;padding:16px 16px 10px;background:linear-gradient(180deg,#09090b 78%,transparent)}\n.eyebrow{font-size:11px;letter-spacing:.22em;color:var(--gold);font-weight:800}.topbar h1{font-size:23px;margin:3px 0 0}.icon-btn{width:42px;height:42px;border-radius:14px;border:1px solid var(--line);background:var(--panel);color:var(--text);font-size:22px}\n.date-strip{display:flex;gap:8px;overflow:auto;padding:6px 16px 13px;scrollbar-width:none}.date-strip::-webkit-scrollbar{display:none}.date-pill{min-width:78px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:14px;padding:9px 10px;text-align:center}.date-pill.active{border-color:var(--gold);background:#211d15}.date-pill strong{display:block;font-size:13px}.date-pill small{color:var(--muted);font-size:11px}.date-pill.active small{color:#d9c59a}\nmain{padding:0 14px}.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:4px 0 18px}.summary-grid.compact{grid-template-columns:repeat(3,1fr)}.metric{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px 10px;min-width:0}.metric b{font-size:20px;display:block}.metric span{font-size:10px;color:var(--muted);display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.metric.warn b{color:var(--amber)}.metric.good b{color:var(--green)}\n.view{display:none}.view.active{display:block}.section-head{display:flex;align-items:center;justify-content:space-between;margin:6px 2px 12px}.section-head h2{font-size:21px;margin:0}.muted{color:var(--muted);margin:4px 0 0;font-size:12px}\n.primary{background:var(--gold);border:0;color:#171208;font-weight:800;border-radius:14px;padding:12px 16px}.primary.small{padding:9px 13px;font-size:13px}.primary:disabled{opacity:.35}.secondary{background:var(--panel2);border:1px solid var(--line);color:var(--text);font-weight:650;border-radius:14px;padding:11px 14px}.danger{border-color:#5a2d2d;color:#ffaaaa}\n.segmented{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;background:var(--panel);padding:4px;border:1px solid var(--line);border-radius:14px;margin-bottom:10px}.segmented.scroll{display:flex;overflow:auto}.segmented button{border:0;background:transparent;color:var(--muted);padding:8px 10px;border-radius:10px;font-weight:700;font-size:12px;white-space:nowrap}.segmented button.active{background:#2a251b;color:#f6dfac}.status-row{display:flex;gap:6px;overflow:auto;margin-bottom:12px;scrollbar-width:none}.chip{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:7px 10px;font-size:11px;white-space:nowrap}.chip.active{color:#1b160c;background:var(--gold);border-color:var(--gold)}\n.cards{display:flex;flex-direction:column;gap:10px}.order-card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:14px;position:relative;overflow:hidden}.order-card.complete{border-color:#305c40}.order-card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--gold2)}.order-card.complete::before{background:var(--green)}.order-top{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.order-time{font-size:20px;font-weight:850}.order-name{font-size:17px;font-weight:800;margin:3px 0 8px}.badges{display:flex;gap:5px;flex-wrap:wrap}.badge{font-size:10px;padding:4px 7px;border-radius:999px;background:#24242a;color:#ccc;border:1px solid #303038}.badge.gbu{background:#201d27;color:#d7c7f1}.badge.private{background:#17221c;color:#a9dfbd}.badge.vip{background:#2b2315;color:#f0d397}.badge.elite{background:#241c22;color:#edc7df}.status-badge{font-size:10px;color:var(--muted);text-align:right}.order-foot{display:flex;justify-content:space-between;align-items:center;margin-top:12px;padding-top:10px;border-top:1px solid var(--line);font-size:12px}.order-foot .brig{max-width:65%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.progress{color:var(--muted)}.progress.done{color:var(--green)}\n.bottom-nav{position:fixed;z-index:30;left:0;right:0;bottom:0;height:calc(68px + var(--safe-bottom));padding-bottom:var(--safe-bottom);display:grid;grid-template-columns:repeat(4,1fr);background:rgba(13,13,15,.95);backdrop-filter:blur(16px);border-top:1px solid var(--line)}.bottom-nav button{border:0;background:transparent;color:#777;padding:7px 2px}.bottom-nav button.active{color:var(--gold)}.bottom-nav span{display:block;font-size:18px;height:23px}.bottom-nav small{font-size:9px;font-weight:700}\n.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:50}.drawer{position:fixed;z-index:60;left:0;right:0;bottom:0;max-height:92vh;overflow:auto;background:#101013;border:1px solid var(--line);border-radius:24px 24px 0 0;padding:18px 16px calc(22px + var(--safe-bottom));box-shadow:0 -20px 80px rgba(0,0,0,.6)}.handle{width:42px;height:4px;background:#3a3a40;border-radius:5px;margin:0 auto 15px}.drawer h3{margin:0 0 2px;font-size:22px}.drawer-section{margin-top:18px}.drawer-section h4{font-size:12px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;margin:0 0 9px}.data-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.data-item{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:10px}.data-item small{display:block;color:var(--muted);font-size:9px;margin-bottom:4px}.data-item b{font-size:13px}.full{grid-column:1/-1}.action-row{display:flex;gap:8px}.action-row>*{flex:1}.person-row{display:flex;align-items:center;gap:10px;padding:11px 3px;border-bottom:1px solid #24242a}.person-row:last-child{border:0}.person-main{min-width:0;flex:1}.person-main b{display:block;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.person-main small{color:var(--muted);font-size:10px}.person-meta{font-size:11px;color:#cfcfd4;white-space:nowrap}.check{width:25px;height:25px;border:1px solid #4a4a52;border-radius:8px;display:grid;place-items:center;color:transparent}.person-row.selected .check{background:var(--green);border-color:var(--green);color:#07120b}.person-row.blocked{opacity:.4}.person-row.match{background:linear-gradient(90deg,rgba(216,180,106,.08),transparent)}\n.modal{position:fixed;z-index:70;left:12px;right:12px;top:7vh;max-height:86vh;overflow:auto;background:#111114;border:1px solid var(--line);border-radius:22px;padding:16px;box-shadow:0 20px 90px #000}.modal h3{margin:2px 0 14px}.modal-head{display:flex;justify-content:space-between;align-items:center}.close-btn{border:0;background:var(--panel2);color:var(--text);border-radius:10px;width:34px;height:34px}.candidate-list{max-height:60vh;overflow:auto}.search{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:14px;padding:12px;margin:0 0 10px;outline:none}.search:focus{border-color:var(--gold2)}\n.group-block{background:var(--panel);border:1px solid var(--line);border-radius:17px;margin:0 0 10px;overflow:hidden}.group-title{display:flex;justify-content:space-between;padding:13px;font-weight:800}.group-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;padding:0 10px 10px}.group-stats button{border:0;border-radius:10px;background:#1b1b1f;color:var(--muted);padding:8px 3px;font-size:10px}.group-stats b{display:block;font-size:15px;color:var(--text)}.group-details{padding:0 12px 10px}.status-section{margin-top:10px}.status-section h5{margin:0 0 6px;font-size:11px;color:var(--muted)}\n.list{background:var(--panel);border:1px solid var(--line);border-radius:17px;overflow:hidden}.employee-row{display:flex;align-items:center;padding:11px 12px;border-bottom:1px solid var(--line);gap:10px}.employee-row:last-child{border:0}.employee-row .name{flex:1;min-width:0}.employee-row .name b{font-size:13px;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.employee-row .name small{font-size:10px;color:var(--muted)}.car{font-size:14px}.height{font-size:12px;font-weight:800;color:#ddd}.attention{background:#211a14;border:1px solid #4c3a28;border-radius:16px;padding:13px;margin-bottom:10px}.attention.red{background:#221616;border-color:#512c2c}.attention h4{margin:0 0 7px}.attention p{margin:5px 0;color:var(--muted);font-size:11px}.attention-list{display:flex;flex-direction:column;gap:5px;font-size:12px}\n.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.field{margin-bottom:9px}.field label{display:block;color:var(--muted);font-size:10px;margin:0 0 5px}.field input,.field select,.field textarea{width:100%;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:11px;padding:10px}.field textarea{min-height:74px;resize:vertical}.gbu-draft{padding:12px;border:1px solid var(--line);border-radius:15px;margin-bottom:10px;background:var(--panel)}\n.toast{position:fixed;z-index:100;left:20px;right:20px;bottom:calc(82px + var(--safe-bottom));padding:12px 14px;border-radius:14px;background:#25251f;border:1px solid #5b5036;color:#fff;text-align:center;font-size:12px}.fatal{position:fixed;z-index:200;inset:0;padding:60px 20px;background:#09090b;color:#fff}.fatal h2{color:var(--red)}\n.empty{padding:32px 12px;text-align:center;color:var(--muted);font-size:13px}\n@media(min-width:760px){body{max-width:960px;margin:auto}.drawer{left:50%;transform:translateX(-50%);max-width:680px}.modal{left:50%;right:auto;width:680px;transform:translateX(-50%)}.summary-grid{grid-template-columns:repeat(6,1fr)}}\n\n</style>\n</head>\n<body>\n  <div id="app">\n    <header class="topbar">\n      <div>\n        <div class="eyebrow">ЛЕГИОН</div>\n        <h1>Диспетчерская</h1>\n      </div>\n      <button class="icon-btn" id="refreshBtn" aria-label="Обновить">↻</button>\n    </header>\n\n    <section class="date-strip" id="dateStrip"></section>\n\n    <main>\n      <section id="summary" class="summary-grid"></section>\n\n      <section id="ordersView" class="view active">\n        <div class="section-head">\n          <div>\n            <h2>Заказы</h2>\n            <p id="ordersDateLabel" class="muted"></p>\n          </div>\n          <button class="primary small" id="gbuBtn">＋ ГБУ</button>\n        </div>\n        <div class="segmented" id="sourceFilters">\n          <button data-source="all" class="active">Все</button>\n          <button data-source="private">Частные</button>\n          <button data-source="gbu">ГБУ</button>\n        </div>\n        <div class="status-row" id="statusFilters">\n          <button data-status="all" class="chip active">Все</button>\n          <button data-status="new" class="chip">Новые</button>\n          <button data-status="assigning" class="chip">Расстановка</button>\n          <button data-status="ready" class="chip">Готовы</button>\n          <button data-status="sent" class="chip">Отправлены</button>\n        </div>\n        <div id="ordersList" class="cards"></div>\n      </section>\n\n      <section id="readinessView" class="view">\n        <div class="section-head">\n          <div><h2>Готовность</h2><p class="muted">Официальный срез — 15:30</p></div>\n        </div>\n        <div id="readinessSummary" class="summary-grid compact"></div>\n        <div id="readinessGroups"></div>\n      </section>\n\n      <section id="employeesView" class="view">\n        <div class="section-head"><div><h2>Сотрудники</h2><p class="muted">Рост, группа, авто</p></div><button class="primary small" id="employeeImportBtn">Импорт</button></div>\n        <input id="employeeSearch" class="search" placeholder="Поиск по ФИО" />\n        <div class="segmented scroll" id="employeeFilters">\n          <button data-group="all" class="active">Все</button>\n          <button data-group="brigadier">Бригадиры</button>\n          <button data-group="main">Основной</button>\n          <button data-group="cashless">Безнал</button>\n          <button data-group="reserve">Резерв</button>\n        </div>\n        <div id="employeesList" class="list"></div>\n      </section>\n\n      <section id="controlView" class="view">\n        <div class="section-head"><div><h2>Контроль</h2><p class="muted">Что требует внимания</p></div></div>\n        <div id="controlContent"></div>\n      </section>\n    </main>\n\n    <nav class="bottom-nav">\n      <button data-view="ordersView" class="active"><span>▤</span><small>Заказы</small></button>\n      <button data-view="readinessView"><span>✓</span><small>Готовность</small></button>\n      <button data-view="employeesView"><span>👥</span><small>Сотрудники</small></button>\n      <button data-view="controlView"><span>!</span><small>Контроль</small></button>\n    </nav>\n  </div>\n\n  <div id="drawerBackdrop" class="backdrop hidden"></div>\n  <aside id="orderDrawer" class="drawer hidden"></aside>\n\n  <div id="modalBackdrop" class="backdrop hidden"></div>\n  <div id="modal" class="modal hidden"></div>\n\n  <div id="toast" class="toast hidden"></div>\n  <div id="fatal" class="fatal hidden"></div>\n\n  <script>\nconst tg = window.Telegram?.WebApp;\nif (tg) { tg.ready(); tg.expand(); }\n\nconst state = {\n  selectedDate: null,\n  source: \'all\',\n  status: \'all\',\n  employeeGroup: \'all\',\n  employeeSearch: \'\',\n  bootstrap: null,\n  currentOrder: null,\n};\n\nconst qs = (s, root=document) => root.querySelector(s);\nconst qsa = (s, root=document) => [...root.querySelectorAll(s)];\nconst fmtDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(\'ru-RU\',{day:\'2-digit\',month:\'long\',weekday:\'short\'});\nconst esc = (s=\'\') => String(s).replace(/[&<>\'"]/g, c => ({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',"\'":\'&#39;\',\'"\':\'&quot;\'}[c]));\n\nfunction headers(json=true) {\n  const h = {};\n  if (json) h[\'Content-Type\'] = \'application/json\';\n  const init = tg?.initData || \'\';\n  if (init) h[\'X-Telegram-Init-Data\'] = init;\n  const p = new URLSearchParams(location.search);\n  if (p.get(\'dev_uid\')) h[\'X-Dev-Telegram-Id\'] = p.get(\'dev_uid\');\n  return h;\n}\n\nasync function api(path, opts={}) {\n  const isForm = opts.body instanceof FormData;\n  const response = await fetch(path, {...opts, headers:{...headers(!isForm), ...(opts.headers||{})}});\n  const contentType = response.headers.get(\'content-type\') || \'\';\n  const data = contentType.includes(\'application/json\') ? await response.json() : await response.text();\n  if (!response.ok) {\n    const msg = data?.detail || data?.message || String(data) || `Ошибка ${response.status}`;\n    throw new Error(msg);\n  }\n  return data;\n}\n\nfunction toast(msg, timeout=2500) {\n  const el = qs(\'#toast\'); el.textContent = msg; el.classList.remove(\'hidden\');\n  clearTimeout(window.__toastTimer); window.__toastTimer = setTimeout(()=>el.classList.add(\'hidden\'), timeout);\n}\n\nfunction fatal(msg) {\n  const el = qs(\'#fatal\');\n  el.innerHTML = `<h2>Доступ к диспетчерской</h2><p>${esc(msg)}</p><p class="muted">Mini App доступна только владельцу и помощникам, чьи Telegram ID указаны в настройках сервера.</p>`;\n  el.classList.remove(\'hidden\');\n}\n\nfunction statusColorClass(o) { return o.composition_complete ? \'complete\' : \'\'; }\n\nasync function loadBootstrap(date=null) {\n  const suffix = date ? `?work_date=${date}` : \'\';\n  const data = await api(`/api/bootstrap${suffix}`);\n  state.bootstrap = data;\n  state.selectedDate = data.selected_date;\n  renderDates(); renderSummary();\n  await renderCurrentView();\n}\n\nfunction renderDates() {\n  const el = qs(\'#dateStrip\');\n  el.innerHTML = state.bootstrap.dates.map(d => `\n    <button class="date-pill ${d.date===state.selectedDate?\'active\':\'\'}" data-date="${d.date}">\n      <strong>${esc(d.label)}</strong><small>${d.count} заказов</small>\n    </button>`).join(\'\');\n  qsa(\'.date-pill\', el).forEach(btn => btn.onclick = async () => {\n    state.selectedDate = btn.dataset.date;\n    await loadBootstrap(state.selectedDate);\n  });\n}\n\nfunction renderSummary() {\n  const s = state.bootstrap.summary;\n  qs(\'#summary\').innerHTML = `\n    <div class="metric"><b>${s.orders_total}</b><span>Заказов</span></div>\n    <div class="metric"><b>${s.private_count}</b><span>Частных</span></div>\n    <div class="metric"><b>${s.gbu_count}</b><span>ГБУ</span></div>\n    <div class="metric ${s.unassigned_count?\'warn\':\'good\'}"><b>${s.complete_count}/${s.orders_total}</b><span>Укомплект.</span></div>\n    <div class="metric good"><b>${s.ready_cutoff}</b><span>Готовы 15:30</span></div>\n    <div class="metric ${s.no_response_cutoff?\'warn\':\'\'}"><b>${s.no_response_cutoff}</b><span>Не отписались</span></div>`;\n}\n\nfunction currentViewId() { return qs(\'.bottom-nav button.active\')?.dataset.view || \'ordersView\'; }\nasync function renderCurrentView() {\n  const v = currentViewId();\n  if (v===\'ordersView\') return loadOrders();\n  if (v===\'readinessView\') return loadReadiness();\n  if (v===\'employeesView\') return loadEmployees();\n  if (v===\'controlView\') return loadControl();\n}\n\nasync function loadOrders() {\n  qs(\'#ordersDateLabel\').textContent = fmtDate(state.selectedDate);\n  const params = new URLSearchParams({work_date:state.selectedDate, source:state.source, status:state.status});\n  const orders = await api(`/api/orders?${params}`);\n  const el = qs(\'#ordersList\');\n  if (!orders.length) { el.innerHTML = `<div class="empty">На эту дату заказов в выбранном фильтре нет.</div>`; return; }\n  el.innerHTML = orders.map(o => `\n    <article class="order-card ${statusColorClass(o)}" data-order-id="${o.id}">\n      <div class="order-top">\n        <div><div class="order-time">${esc(o.issue_time)}</div><div class="order-name">${esc(o.deceased_name)}</div>\n          <div class="badges">\n            <span class="badge ${o.source}">${esc(o.source_label)}</span>\n            <span class="badge ${o.category}">${esc(o.category_label)}</span>\n            <span class="badge">${o.team_size} чел.</span>\n          </div>\n        </div>\n        <div class="status-badge">${esc(o.status_label)}</div>\n      </div>\n      <div class="order-foot">\n        <div class="brig">${o.brigadier ? `🧑\u200d✈️ ${esc(o.brigadier.full_name)} · ${o.brigadier.height_cm||\'—\'}` : \'⚠️ Бригадир не назначен\'}</div>\n        <div class="progress ${o.member_count===o.member_target?\'done\':\'\'}">👥 ${o.member_count}/${o.member_target}</div>\n      </div>\n    </article>`).join(\'\');\n  qsa(\'.order-card\', el).forEach(card => card.onclick = () => openOrder(Number(card.dataset.orderId)));\n}\n\nasync function openOrder(id) {\n  const o = await api(`/api/orders/${id}`); state.currentOrder = o;\n  const d = qs(\'#orderDrawer\'); const b = qs(\'#drawerBackdrop\');\n  d.innerHTML = orderDrawerHtml(o); d.classList.remove(\'hidden\'); b.classList.remove(\'hidden\');\n  wireOrderDrawer(o);\n}\n\nfunction orderDrawerHtml(o) {\n  const members = o.members.length ? o.members.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(x.full_name)}</b><small>${esc(x.group_label)}</small></div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\') : `<div class="empty">Состав пока не выбран</div>`;\n  return `\n    <div class="handle"></div>\n    <div class="order-top"><div><h3>${esc(o.deceased_name)}</h3><p class="muted">${fmtDate(o.work_date)} · ${o.issue_time}</p></div><button class="close-btn" id="closeDrawer">×</button></div>\n    <div class="drawer-section data-grid">\n      <div class="data-item"><small>Источник</small><b>${esc(o.source_label)}</b></div>\n      <div class="data-item"><small>Категория</small><b>${esc(o.category_label)}</b></div>\n      <div class="data-item"><small>Подача</small><b>${esc(o.arrival_time||\'—\')}</b></div>\n      <div class="data-item"><small>Количество</small><b>${o.team_size} чел.</b></div>\n      <div class="data-item full"><small>Маршрут</small><b>${esc(o.route||\'—\')}</b></div>\n      ${o.organization?`<div class="data-item full"><small>Организация</small><b>${esc(o.organization)}</b></div>`:\'\'}\n      ${o.kickback_rub?`<div class="data-item full"><small>Дополнительно для бригадира</small><b>Откат ${o.kickback_rub} ₽</b></div>`:\'\'}\n    </div>\n\n    <div class="drawer-section"><h4>Бригадир</h4>\n      ${o.brigadier ? `<div class="person-row"><div class="person-main"><b>${esc(o.brigadier.full_name)}</b><small>${o.brigadier.phone||\'\'}</small></div><div class="person-meta">${o.brigadier.height_cm||\'—\'} см ${o.brigadier.has_car?\'🚗\':\'\'}</div></div>` : `<div class="empty">Не назначен</div>`}\n      <button class="secondary" id="assignBrigBtn" style="width:100%;margin-top:8px">${o.brigadier?\'Сменить бригадира\':\'Назначить бригадира\'}</button>\n    </div>\n\n    <div class="drawer-section"><h4>Состав · ${o.member_count}/${o.member_target}</h4><div>${members}</div>\n      <button class="secondary" id="selectMembersBtn" style="width:100%;margin-top:8px" ${o.brigadier?\'\':\'disabled\'}>Выбрать состав по галочкам</button>\n    </div>\n\n    <div class="drawer-section action-row">\n      <button class="secondary danger" id="resetCompositionBtn">Снять состав</button>\n      <button class="primary" id="finalizeBtn" ${o.composition_complete?\'\':\'disabled\'}>Состав готов</button>\n    </div>\n    <p class="muted" style="margin-top:10px">Заказ отправится бригадиру только после полного состава.</p>`;\n}\n\nfunction wireOrderDrawer(o) {\n  qs(\'#closeDrawer\').onclick = closeDrawer;\n  qs(\'#assignBrigBtn\').onclick = ()=>openBrigadierModal(o.id);\n  qs(\'#selectMembersBtn\').onclick = ()=>openMembersModal(o.id);\n  qs(\'#resetCompositionBtn\').onclick = async ()=>{\n    if (!confirm(\'Снять бригадира и весь состав с этого заказа?\')) return;\n    await api(`/api/orders/${o.id}/reset-composition`,{method:\'POST\'}); toast(\'Состав снят\'); await refreshOrder(o.id);\n  };\n  qs(\'#finalizeBtn\').onclick = async ()=>{\n    try {\n      const r = await api(`/api/orders/${o.id}/finalize`,{method:\'POST\'});\n      toast(r.telegram_sent?\'Заказ отправлен бригадиру\':\'Состав сохранён. \'+(r.warning||\'\'),3500);\n      await refreshOrder(o.id);\n    } catch(e){toast(e.message,3500)}\n  };\n}\n\nasync function refreshOrder(id) {\n  state.bootstrap = await api(`/api/bootstrap?work_date=${state.selectedDate}`); renderSummary(); renderDates();\n  await loadOrders(); await openOrder(id);\n}\n\nfunction closeDrawer(){qs(\'#orderDrawer\').classList.add(\'hidden\');qs(\'#drawerBackdrop\').classList.add(\'hidden\');state.currentOrder=null}\n\nasync function openBrigadierModal(orderId) {\n  const list = await api(`/api/orders/${orderId}/brigadiers`);\n  showModal(`Назначить бригадира`, `\n    <p class="muted">Показаны только бригадиры, которые отметились «готов» на эту дату.</p>\n    <div class="candidate-list">${list.length?list.map(personRowBrig).join(\'\'):`<div class="empty">Готовых бригадиров нет</div>`}</div>`);\n  qsa(\'[data-brig-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    if (row.classList.contains(\'blocked\')) {toast(\'У бригадира уже 2 заказа\'); return;}\n    try {await api(`/api/orders/${orderId}/brigadier`,{method:\'POST\',body:JSON.stringify({employee_id:Number(row.dataset.brigId)})}); closeModal(); toast(\'Бригадир назначен\'); await refreshOrder(orderId)} catch(e){toast(e.message,3500)}\n  });\n}\n\nfunction personRowBrig(x){return `<div class="person-row ${x.blocked?\'blocked\':\'\'}" data-brig-id="${x.id}"><div class="person-main"><b>${esc(x.full_name)}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small></div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nasync function openMembersModal(orderId) {\n  const data = await api(`/api/orders/${orderId}/members/candidates`);\n  if(!data.brigadier){toast(\'Сначала назначьте бригадира\');return}\n  showModal(`Состав · нужно ${data.target}`, `\n    <p class="muted">Сверху сотрудники ±2 см от бригадира ${data.brigadier.height_cm||\'—\'} см. Можно максимум 2 заказа на человека в день.</p>\n    <input class="search" id="candidateSearch" placeholder="Поиск сотрудника">\n    <div class="candidate-list" id="candidateList">${data.candidates.map(personRowCandidate).join(\'\')}</div>`);\n  const wire = ()=>qsa(\'[data-member-id]\', qs(\'#modal\')).forEach(row => row.onclick = async ()=>{\n    const id=Number(row.dataset.memberId); if(row.dataset.brig===\'1\') return;\n    if(row.classList.contains(\'blocked\') && row.dataset.selected!==\'1\'){toast(\'У сотрудника уже 2 заказа\');return}\n    try{\n      if(row.dataset.selected===\'1\') await api(`/api/orders/${orderId}/members/${id}`,{method:\'DELETE\'});\n      else await api(`/api/orders/${orderId}/members`,{method:\'POST\',body:JSON.stringify({employee_id:id})});\n      const fresh=await api(`/api/orders/${orderId}/members/candidates`); qs(\'#candidateList\').innerHTML=fresh.candidates.map(personRowCandidate).join(\'\'); wire(); await loadOrders();\n    }catch(e){toast(e.message,3500)}\n  });\n  wire();\n  qs(\'#candidateSearch\').oninput = e => {const v=e.target.value.toLowerCase(); qsa(\'[data-member-id]\',qs(\'#candidateList\')).forEach(r=>r.style.display=r.textContent.toLowerCase().includes(v)?\'flex\':\'none\')};\n}\n\nfunction personRowCandidate(x){return `<div class="person-row ${x.selected?\'selected\':\'\'} ${x.blocked&&!x.selected?\'blocked\':\'\'} ${x.height_match?\'match\':\'\'}" data-member-id="${x.id}" data-selected="${x.selected?\'1\':\'0\'}" data-brig="${x.is_brigadier?\'1\':\'0\'}">\n  <div class="check">✓</div><div class="person-main"><b>${esc(x.full_name)} ${x.is_brigadier?\'(бригадир)\':\'\'}</b><small>${esc(x.group_label)} · ${x.workload}/2 заказа</small></div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`}\n\nfunction showModal(title, body) {qs(\'#modal\').innerHTML=`<div class="modal-head"><h3>${esc(title)}</h3><button id="closeModal" class="close-btn">×</button></div>${body}`;qs(\'#modal\').classList.remove(\'hidden\');qs(\'#modalBackdrop\').classList.remove(\'hidden\');qs(\'#closeModal\').onclick=closeModal}\nfunction closeModal(){qs(\'#modal\').classList.add(\'hidden\');qs(\'#modalBackdrop\').classList.add(\'hidden\')}\n\nasync function loadReadiness(){\n  const r=await api(`/api/readiness/summary?work_date=${state.selectedDate}`);\n  qs(\'#readinessSummary\').innerHTML=`\n    <div class="metric good"><b>${r.totals.ready_cutoff}</b><span>Готовы 15:30</span></div>\n    <div class="metric"><b>${r.totals.ready_now}</b><span>Готовы сейчас</span></div>\n    <div class="metric ${r.totals.no_response_cutoff?\'warn\':\'\'}"><b>${r.totals.no_response_cutoff}</b><span>Не отписались</span></div>`;\n  qs(\'#readinessGroups\').innerHTML=Object.entries(r.groups).map(([code,g])=>`\n    <div class="group-block"><div class="group-title"><span>${esc(g.label)}</span><span>${g.counts.ready}/${g.counts.ready+g.counts.not_ready+g.counts.day_off+g.counts.no_response}</span></div>\n      <div class="group-stats"><button><b>${g.counts.ready}</b>Готов</button><button><b>${g.counts.not_ready}</b>Не готов</button><button><b>${g.counts.day_off}</b>Выходной</button><button><b>${g.no_response_cutoff.length}</b>Не отпис.</button></div>\n      ${g.no_response_cutoff.length?`<div class="group-details status-section"><h5>⚠️ Не отписались к 15:30</h5>${g.no_response_cutoff.map(x=>`<div class="person-row"><div class="person-main"><b>${esc(x.full_name)}</b></div><div class="person-meta">${x.height_cm||\'—\'} см ${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\')}</div>`:\'\'}\n    </div>`).join(\'\');\n}\n\nasync function loadEmployees(){\n  const params=state.employeeGroup===\'all\'?\'\':`?group=${state.employeeGroup}`; const list=await api(`/api/employees${params}`); const v=state.employeeSearch.toLowerCase(); const filtered=list.filter(x=>x.full_name.toLowerCase().includes(v));\n  qs(\'#employeesList\').innerHTML=filtered.length?filtered.map(x=>`<div class="employee-row"><div class="name"><b>${esc(x.full_name)}</b><small>${esc(x.group_label)}${x.phone?` · ${esc(x.phone)}`:\'\'}</small></div><div class="height">${x.height_cm||\'—\'} см</div><div class="car">${x.has_car?\'🚗\':\'\'}</div></div>`).join(\'\'):`<div class="empty">Ничего не найдено</div>`;\n}\n\nasync function loadControl(){\n  const c=await api(`/api/control?work_date=${state.selectedDate}`); const no=c.no_response_cutoff; const incomplete=c.orders.not_complete;\n  qs(\'#controlContent\').innerHTML=`\n    <div class="attention ${incomplete.length?\'red\':\'\'}"><h4>Не укомплектованы · ${incomplete.length}</h4><div class="attention-list">${incomplete.length?incomplete.map(o=>`<div>${o.issue_time} · ${esc(o.deceased_name)} · ${o.brigadier?\'состав \'+o.member_count+\'/\'+o.member_target:\'без бригадира\'}</div>`).join(\'\'):\'Все заказы укомплектованы\'}</div></div>\n    <div class="attention ${no.length?\'red\':\'\'}"><h4>Не отписались к 15:30 · ${no.length}</h4><div class="attention-list">${no.length?no.map(x=>`<div>${esc(x.full_name)} · ${esc(x.group_label)}</div>`).join(\'\'):\'Все сотрудники отписались\'}</div></div>\n    <div class="attention"><h4>Нагрузка</h4><div class="attention-list">${c.workload.length?c.workload.map(x=>`<div>${esc(x.full_name)} · ${x.workload}/2</div>`).join(\'\'):\'Назначений пока нет\'}</div></div>`;\n}\n\n\nasync function openEmployeeImport(){\n  showModal(\'Импорт сотрудников\', `\n    <p class="muted">Одна строка — один сотрудник. Формат: ФИО; группа; рост; авто; телефон; Telegram ID</p>\n    <p class="muted">Группа: бригадиры / основной / безнал / резерв. Поля телефон и Telegram ID можно оставить пустыми.</p>\n    <div class="field"><label>Список</label><textarea id="employeeImportText" placeholder="Иван Иванов; основной; 186; авто; +79990000000; 123456789\nПетр Петров; резерв; 184; ; ;"></textarea></div>\n    <button class="primary" id="employeeImportSave" style="width:100%">Импортировать</button>`);\n  qs(\'#employeeImportSave\').onclick=async()=>{\n    const raw=qs(\'#employeeImportText\').value.trim(); if(!raw){toast(\'Вставьте список\');return}\n    const groupMap={\'бригадиры\':\'brigadier\',\'бригадир\':\'brigadier\',\'основной\':\'main\',\'основной состав\':\'main\',\'безнал\':\'cashless\',\'безнал состав\':\'cashless\',\'резерв\':\'reserve\'};\n    const items=[];\n    for(const line of raw.split(/\\n+/)){\n      const p=line.split(\';\').map(x=>x.trim()); if(!p[0]) continue; const group=groupMap[(p[1]||\'\').toLowerCase()]; if(!group){toast(`Не понял группу: ${p[1]||\'—\'}`,3500);return}\n      items.push({full_name:p[0],group_code:group,height_cm:p[2]?Number(p[2]):null,has_car:/авто|да|yes|\\+/.test((p[3]||\'\').toLowerCase()),phone:p[4]||\'\',tg_id:p[5]?Number(p[5]):null,active:true});\n    }\n    try{await api(\'/api/employees/bulk\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Импортировано: ${items.length}`);await loadEmployees()}catch(e){toast(e.message,4000)}\n  };\n}\n\nasync function openGbuImport(){\n  showModal(\'Импорт ГБУ\', `\n    <p class="muted">Загрузите фото таблицы. Бот распознает текст и создаст черновики. Перед сохранением обязательно проверьте строки.</p>\n    <div class="field"><label>Фото таблицы</label><input type="file" id="gbuFile" accept="image/*"></div>\n    <button class="primary" id="ocrBtn" style="width:100%">Распознать фото</button>\n    <div id="gbuResult" style="margin-top:12px"></div>`);\n  qs(\'#ocrBtn\').onclick=async()=>{\n    const f=qs(\'#gbuFile\').files[0]; if(!f){toast(\'Выберите фото\');return}\n    const fd=new FormData();fd.append(\'file\',f); qs(\'#ocrBtn\').disabled=true; qs(\'#ocrBtn\').textContent=\'Распознаю…\';\n    try{const r=await api(\'/api/gbu/ocr\',{method:\'POST\',body:fd}); renderGbuDrafts(r.drafts,r.raw_text)}catch(e){toast(e.message,4000)}finally{qs(\'#ocrBtn\').disabled=false;qs(\'#ocrBtn\').textContent=\'Распознать фото\'}\n  };\n}\n\nfunction renderGbuDrafts(drafts, raw){\n  const el=qs(\'#gbuResult\'); const rows=(drafts||[]).map((d,i)=>gbuDraftHtml(d,i)).join(\'\');\n  el.innerHTML=`<div class="field"><label>Распознанный текст</label><textarea readonly>${esc(raw)}</textarea></div><h4>Черновики заказов</h4><div id="gbuDrafts">${rows||\'<div class="empty">Строки автоматически не найдены. Добавьте заказ вручную.</div>\'}</div><button class="secondary" id="addGbuRow" style="width:100%;margin-bottom:8px">＋ Добавить строку</button><button class="primary" id="saveGbuDrafts" style="width:100%">Сохранить заказы ГБУ</button>`;\n  qs(\'#addGbuRow\').onclick=()=>{const c=qs(\'#gbuDrafts\');const i=qsa(\'.gbu-draft\',c).length;c.insertAdjacentHTML(\'beforeend\',gbuDraftHtml({work_date:state.selectedDate,issue_time:\'\',deceased_name:\'\',route:\'\',team_size:4,category:\'standard\'},i))};\n  qs(\'#saveGbuDrafts\').onclick=saveGbuDrafts;\n}\n\nfunction gbuDraftHtml(d,i){return `<div class="gbu-draft" data-i="${i}"><div class="form-grid"><div class="field"><label>Дата</label><input data-k="work_date" type="date" value="${d.work_date||state.selectedDate}"></div><div class="field"><label>Выдача</label><input data-k="issue_time" type="time" value="${d.issue_time||\'\'}"></div><div class="field full"><label>Умерший</label><input data-k="deceased_name" value="${esc(d.deceased_name||\'\')}"></div><div class="field"><label>Категория</label><select data-k="category"><option value="standard" ${d.category===\'standard\'?\'selected\':\'\'}>Стандарт</option><option value="vip" ${d.category===\'vip\'?\'selected\':\'\'}>Вип</option><option value="elite" ${d.category===\'elite\'?\'selected\':\'\'}>Элит</option></select></div><div class="field"><label>Человек</label><select data-k="team_size"><option value="4" ${Number(d.team_size)!==6?\'selected\':\'\'}>4</option><option value="6" ${Number(d.team_size)===6?\'selected\':\'\'}>6</option></select></div><div class="field full"><label>Маршрут</label><textarea data-k="route">${esc(d.route||\'\')}</textarea></div></div></div>`}\n\nasync function saveGbuDrafts(){\n  const items=qsa(\'.gbu-draft\').map(row=>{const get=k=>qs(`[data-k="${k}"]`,row).value;return {work_date:get(\'work_date\'),issue_time:get(\'issue_time\'),deceased_name:get(\'deceased_name\'),route:get(\'route\'),category:get(\'category\'),team_size:Number(get(\'team_size\')),organization:\'ГБУ\',notes:\'\'}}).filter(x=>x.work_date&&x.issue_time&&x.deceased_name);\n  if(!items.length){toast(\'Нет заполненных строк\');return}\n  try{await api(\'/api/gbu/drafts\',{method:\'POST\',body:JSON.stringify(items)});closeModal();toast(`Добавлено: ${items.length}`);await loadBootstrap(state.selectedDate)}catch(e){toast(e.message,4000)}\n}\n\n// Events\nqs(\'#refreshBtn\').onclick=()=>loadBootstrap(state.selectedDate).catch(e=>toast(e.message));\nqs(\'#drawerBackdrop\').onclick=closeDrawer; qs(\'#modalBackdrop\').onclick=closeModal; qs(\'#gbuBtn\').onclick=openGbuImport;\nqsa(\'.bottom-nav button\').forEach(btn=>btn.onclick=async()=>{qsa(\'.bottom-nav button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');qsa(\'.view\').forEach(x=>x.classList.remove(\'active\'));qs(`#${btn.dataset.view}`).classList.add(\'active\');await renderCurrentView()});\nqsa(\'#sourceFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#sourceFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.source=btn.dataset.source;await loadOrders()});\nqsa(\'#statusFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#statusFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.status=btn.dataset.status;await loadOrders()});\nqsa(\'#employeeFilters button\').forEach(btn=>btn.onclick=async()=>{qsa(\'#employeeFilters button\').forEach(x=>x.classList.remove(\'active\'));btn.classList.add(\'active\');state.employeeGroup=btn.dataset.group;await loadEmployees()});\nqs(\'#employeeSearch\').oninput=e=>{state.employeeSearch=e.target.value;loadEmployees()};\nqs(\'#employeeImportBtn\').onclick=openEmployeeImport;\n\nloadBootstrap().catch(e=>fatal(e.message));\n\n</script>\n</body>\n</html>\n'

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INLINE_INDEX_HTML)


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
