from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

import db
from common import CATEGORY_LABELS, READINESS_STATUSES, SOURCE_LABELS, settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("legion-bot-v28")

router = Router()


class AgentProfile(StatesGroup):
    name = State()
    phone = State()


class OrderWizard(StatesGroup):
    source = State()
    work_date = State()
    deceased = State()
    issue_time = State()
    contact_time = State()
    route = State()
    category = State()
    people_count = State()
    other_agent = State()
    other_agent_name = State()
    other_agent_phone = State()
    requires_cash = State()
    confirm = State()


class AgentEdit(StatesGroup):
    value = State()


class PhotoFlow(StatesGroup):
    waiting_photo = State()


class BrigProfileEdit(StatesGroup):
    value = State()


class FinishFlow(StatesGroup):
    tip_amount = State()
    cash_amount = State()
    cash_people_count = State()


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


def agent_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="➕ Создать заказ"), KeyboardButton(text="📋 Мои заказы")]],
        resize_keyboard=True,
    )


def brig_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📌 Мои заказы"), KeyboardButton(text="📷 Фотоотчёт")],
            [KeyboardButton(text="ℹ️ Мой статус"), KeyboardButton(text="✏️ Изменить профиль")],
        ],
        resize_keyboard=True,
    )


def owner_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📟 Диспетчерская"), KeyboardButton(text="💰 Касса")],
            [KeyboardButton(text="📷 Фотоотчёт"), KeyboardButton(text="👥 Бригадиры")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
    )


def parse_date(s: str) -> date | None:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%d.%m", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt == "%d.%m":
                d = d.replace(year=settings.now().year)
            return d.date()
        except ValueError:
            pass
    return None


def parse_time(s: str) -> time | None:
    try:
        return datetime.strptime(s.strip(), "%H:%M").time()
    except ValueError:
        return None


def order_text(o: dict[str, Any]) -> str:
    submit = (datetime.combine(o["work_date"], o["issue_time"]) - timedelta(minutes=30)).time()
    return (
        f"<b>{SOURCE_LABELS.get(o['source'], o['source'])} №{o['daily_number']}</b>\n"
        f"📅 {o['work_date'].strftime('%d.%m.%Y')}\n"
        f"🏷 {CATEGORY_LABELS.get(o['category'], o['category'])}\n"
        f"👤 Агент: {o.get('agent_name') or '—'}\n"
        f"☎️ {o.get('agent_phone') or '—'}\n"
        f"⚰️ {o.get('deceased_name') or '—'}\n"
        f"🕘 Выдача: {o['issue_time'].strftime('%H:%M')}\n"
        f"🚐 Подача: {submit.strftime('%H:%M')}\n"
        f"📞 Связь: {o['contact_time'].strftime('%H:%M') if o.get('contact_time') else 'не задана'}\n"
        f"📍 {o.get('route') or '—'}\n"
        f"👥 {o['people_count']} человек\n"
        f"Статус: <b>{o['status']}</b>"
    )


async def ensure_agent_profile(message: Message, state: FSMContext) -> bool:
    row = await db.pool().fetchrow("SELECT * FROM agents WHERE telegram_id=$1", message.from_user.id)
    if row and row["full_name"] and row["phone"]:
        return True
    await state.set_state(AgentProfile.name)
    await message.answer("Введите ваши <b>ФИО</b> для профиля агента:")
    return False


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    # Owner/helper access must be checked before the agent wizard. In the old
    # working version the owner never had to create an agent profile.
    if message.from_user.id in settings.owner_ids:
        await state.clear()
        await message.answer("ЛЕГИОН — управление.", reply_markup=owner_menu())
        return
    emp = await db.get_employee_by_tg(message.from_user.id)
    if emp:
        if emp["employee_group"] == "brigadier":
            await message.answer(f"ЛЕГИОН. {emp['full_name']}.\nОткройте ближайшие заказы или отправьте готовность одним сообщением.", reply_markup=brig_menu())
        else:
            await message.answer(
                f"ЛЕГИОН. {emp['full_name']}.\nОтправьте готовность обычным сообщением, например:\n<code>Готов 186 на авто</code>\n<code>На основной 186 без авто</code>\n<code>Выходной</code>"
            )
        return
    if message.from_user.id in settings.helper_ids:
        await state.clear()
        await message.answer("ЛЕГИОН — диспетчерская.", reply_markup=owner_menu())
        return
    if await ensure_agent_profile(message, state):
        await message.answer("ЛЕГИОН — приём заказов.", reply_markup=agent_menu())


@router.message(AgentProfile.name)
async def agent_name(message: Message, state: FSMContext) -> None:
    await state.update_data(agent_name=message.text.strip())
    await state.set_state(AgentProfile.phone)
    await message.answer("Введите номер телефона агента:")


@router.message(AgentProfile.phone)
async def agent_phone(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await db.pool().execute(
        """
        INSERT INTO agents(telegram_id,full_name,phone) VALUES($1,$2,$3)
        ON CONFLICT(telegram_id) DO UPDATE SET full_name=EXCLUDED.full_name,phone=EXCLUDED.phone,updated_at=NOW()
        """, message.from_user.id, data["agent_name"], message.text.strip(),
    )
    await state.clear()
    await message.answer("Профиль сохранён.", reply_markup=agent_menu())


@router.message(Command("dispatcher"))
async def dispatcher_link(message: Message) -> None:
    if message.from_user.id not in settings.allowed_dispatcher_ids:
        return
    if not settings.dispatcher_url:
        await message.answer("DISPATCHER_URL пока не задан.")
        return
    await message.answer(
        "Открыть диспетчерскую:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📟 Диспетчерская", web_app=WebAppInfo(url=settings.dispatcher_url))]])
    )


@router.message(Command("setupdispatcher"))
async def setup_dispatcher_menu(message: Message, bot: Bot) -> None:
    if message.from_user.id not in settings.owner_ids:
        return
    if not settings.dispatcher_url:
        await message.answer("Сначала задайте DISPATCHER_URL в Railway.")
        return
    ok = 0
    for uid in settings.allowed_dispatcher_ids:
        try:
            await bot.set_chat_menu_button(
                chat_id=uid,
                menu_button=MenuButtonWebApp(text="Диспетчерская", web_app=WebAppInfo(url=settings.dispatcher_url)),
            )
            ok += 1
        except TelegramBadRequest:
            pass
    await message.answer(f"Закреплённая кнопка «Диспетчерская» настроена для {ok} пользователей.")


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message) -> None:
    uid = message.from_user.id
    if uid in settings.owner_ids:
        await message.answer(
            "⚙️ <b>Настройки владельца</b>\n\n"
            "Доступны диспетчерская, касса, фотоотчёты и список бригадиров.",
            reply_markup=owner_menu(),
        )
        return
    emp = await db.get_employee_by_tg(uid)
    if emp and emp["employee_group"] == "brigadier":
        await show_brig_profile(message, emp)
        return
    if uid in settings.helper_ids:
        await message.answer("⚙️ Диспетчерский доступ активен.", reply_markup=owner_menu())


async def show_brig_profile(message: Message, emp: dict[str, Any]) -> None:
    car = "Да" if emp.get("has_car") else "Нет"
    await message.answer(
        "👤 <b>Профиль бригадира</b>\n"
        f"ФИО: {emp.get('full_name') or '—'}\n"
        f"Метро: {emp.get('metro') or '—'}\n"
        f"Рост: {emp.get('height_cm') or '—'}\n"
        f"Телефон: {emp.get('phone') or '—'}\n"
        f"Авто: {car}",
        reply_markup=ikb([
            [("ФИО", "bpedit:name"), ("Метро", "bpedit:metro")],
            [("Рост", "bpedit:height"), ("Телефон", "bpedit:phone")],
            [("Авто", "bpedit:car")],
        ]),
    )


@router.message(F.text == "✏️ Изменить профиль")
async def brig_profile_button(message: Message) -> None:
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp or emp["employee_group"] != "brigadier":
        return
    await show_brig_profile(message, dict(emp))


@router.callback_query(F.data.startswith("bpedit:"))
async def brig_profile_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not emp or emp["employee_group"] != "brigadier":
        await call.answer("Недоступно", show_alert=True)
        return
    field = call.data.split(":", 1)[1]
    if field == "car":
        await call.message.answer("Есть автомобиль?", reply_markup=ikb([[("🚗 Да", "bpcar:1"), ("Нет", "bpcar:0")]]))
        await call.answer()
        return
    prompts = {"name": "Введите ФИО:", "metro": "Введите метро:", "height": "Введите рост в см:", "phone": "Введите номер телефона:"}
    if field not in prompts:
        await call.answer()
        return
    await state.set_state(BrigProfileEdit.value)
    await state.update_data(brig_profile_field=field)
    await call.message.answer(prompts[field])
    await call.answer()


@router.callback_query(F.data.startswith("bpcar:"))
async def brig_profile_car(call: CallbackQuery) -> None:
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not emp or emp["employee_group"] != "brigadier":
        await call.answer("Недоступно", show_alert=True)
        return
    value = call.data.endswith("1")
    await db.pool().execute("UPDATE employees SET has_car=$2,updated_at=NOW() WHERE id=$1", emp["id"], value)
    await call.message.answer("✅ Профиль обновлён.", reply_markup=brig_menu())
    await call.answer()


@router.message(BrigProfileEdit.value)
async def brig_profile_edit_value(message: Message, state: FSMContext) -> None:
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp or emp["employee_group"] != "brigadier":
        await state.clear()
        return
    data = await state.get_data()
    field = data.get("brig_profile_field")
    value = (message.text or "").strip()
    if field == "height":
        try:
            height = int(value)
        except ValueError:
            await message.answer("Введите рост числом, например 186")
            return
        if not 150 <= height <= 220:
            await message.answer("Проверьте рост и введите число от 150 до 220.")
            return
        await db.pool().execute("UPDATE employees SET height_cm=$2,updated_at=NOW() WHERE id=$1", emp["id"], height)
    elif field == "name":
        await db.pool().execute("UPDATE employees SET full_name=$2,updated_at=NOW() WHERE id=$1", emp["id"], value)
    elif field == "metro":
        await db.pool().execute("UPDATE employees SET metro=$2,updated_at=NOW() WHERE id=$1", emp["id"], value)
    elif field == "phone":
        await db.pool().execute("UPDATE employees SET phone=$2,updated_at=NOW() WHERE id=$1", emp["id"], value)
    await state.clear()
    await message.answer("✅ Профиль обновлён.", reply_markup=brig_menu())


@router.message(Command("cash"))
@router.message(F.text == "💰 Касса")
async def owner_cash(message: Message) -> None:
    if message.from_user.id not in settings.owner_ids:
        return
    if settings.dispatcher_url:
        sep = "&" if "?" in settings.dispatcher_url else "?"
        url = f"{settings.dispatcher_url}{sep}tab=cash"
        await message.answer("💰 Касса:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть кассу", web_app=WebAppInfo(url=url))]]))
        return
    rows = await db.pool().fetch(
        """SELECT c.amount,o.daily_number,o.source,e.full_name FROM cash_entries c JOIN orders o ON o.id=c.order_id LEFT JOIN employees e ON e.id=c.brigadier_employee_id WHERE o.work_date=CURRENT_DATE ORDER BY o.issue_time"""
    )
    total = sum(Decimal(str(r["amount"] or 0)) for r in rows)
    lines = [f"{SOURCE_LABELS.get(r['source'], r['source'])} №{r['daily_number']} · {r['full_name'] or '—'} · {r['amount']} ₽" for r in rows]
    await message.answer("💰 <b>Касса за сегодня</b>\n" + ("\n".join(lines) if lines else "Записей нет.") + f"\n\nИтого: <b>{total} ₽</b>")


def photo_report_targets() -> list[int]:
    # If a dedicated Telegram group is configured, send reports there.
    # Otherwise keep the previous behaviour and send them to owner/helpers.
    configured = list(settings.photo_report_chat_ids)
    return configured if configured else list(settings.allowed_dispatcher_ids)


def gbu_report_text(o: dict[str, Any], emp: dict[str, Any]) -> str:
    tips = o.get("tips_amount")
    if tips is None or Decimal(str(tips)) == 0:
        tea = "Без чая"
    else:
        tea = f"{Decimal(str(tips)):g} ₽"
    return (
        f"📷 <b>Фотоотчёт ГБУ №{o['daily_number']}</b>\n"
        f"⚰️ {o.get('deceased_name') or '—'}\n"
        f"👤 Бригадир: {emp.get('full_name') or '—'}\n"
        f"🏷 {CATEGORY_LABELS.get(o.get('category'), o.get('category') or '—')}\n"
        f"👥 {o.get('people_count') or '—'} человек\n"
        f"🕘 Выдача: {o['issue_time'].strftime('%H:%M') if o.get('issue_time') else '—'}\n"
        f"📍 {o.get('route') or '—'}\n"
        f"☕ Чай: <b>{tea}</b>"
    )


async def send_gbu_photo_report(bot: Bot, order_id: int, emp: dict[str, Any]) -> None:
    o = await db.get_order(order_id)
    if not o:
        return
    photos = await db.pool().fetch(
        """SELECT kind,telegram_file_id FROM photos
           WHERE order_id=$1 AND kind IN ('gbu_first','gbu_second')
           ORDER BY CASE kind WHEN 'gbu_first' THEN 1 ELSE 2 END, id""",
        order_id,
    )
    first = next((x for x in photos if x["kind"] == "gbu_first"), None)
    second = next((x for x in photos if x["kind"] == "gbu_second"), None)
    if not first or not second:
        return
    caption = gbu_report_text(o, emp)
    media = [
        InputMediaPhoto(media=first["telegram_file_id"], caption=caption, parse_mode=ParseMode.HTML),
        InputMediaPhoto(media=second["telegram_file_id"]),
    ]
    for chat_id in photo_report_targets():
        with contextlib.suppress(Exception):
            await bot.send_media_group(chat_id=chat_id, media=media)


async def send_gbu_photo_report_if_pending(bot: Bot, state: FSMContext, order_id: int) -> None:
    data = await state.get_data()
    if int(data.get("gbu_report_pending") or 0) != order_id:
        return
    emp = await db.get_employee_by_tg(int(data.get("gbu_report_brigadier_tg") or 0))
    if emp:
        await send_gbu_photo_report(bot, order_id, emp)
    await state.update_data(gbu_report_pending=None, gbu_report_brigadier_tg=None)


@router.message(Command("photos"))
@router.message(F.text == "📷 Фотоотчёт")
async def photo_reports(message: Message, bot: Bot) -> None:
    uid = message.from_user.id
    emp = await db.get_employee_by_tg(uid)
    if emp and emp["employee_group"] == "brigadier" and uid not in settings.owner_ids:
        await brigadier_orders(message)
        return
    if uid not in settings.owner_ids:
        return
    rows = await db.pool().fetch(
        """SELECT p.telegram_file_id,p.kind,p.created_at,o.daily_number,o.source,e.full_name
           FROM photos p JOIN orders o ON o.id=p.order_id JOIN employees e ON e.id=p.employee_id
           WHERE o.work_date=CURRENT_DATE ORDER BY p.created_at DESC LIMIT 30"""
    )
    if not rows:
        await message.answer("📷 Сегодня фотоотчётов пока нет.", reply_markup=owner_menu())
        return
    await message.answer(f"📷 Фотоотчётов за сегодня: {len(rows)}")
    for r in reversed(rows):
        caption = f"{SOURCE_LABELS.get(r['source'], r['source'])} №{r['daily_number']} · {r['full_name']}"
        with contextlib.suppress(Exception):
            await bot.send_photo(uid, r["telegram_file_id"], caption=caption)


@router.message(F.text == "👥 Бригадиры")
async def owner_brigadiers(message: Message) -> None:
    if message.from_user.id not in settings.owner_ids:
        return
    rows = await db.pool().fetch("SELECT * FROM employees WHERE active=TRUE AND employee_group='brigadier' ORDER BY full_name")
    if not rows:
        await message.answer("Бригадиры пока не найдены. После запуска диспетчерская автоматически восстанавливает зарегистрированные профили из старой базы.")
        return
    text = [f"👥 <b>Бригадиры — {len(rows)}</b>"]
    for i, r in enumerate(rows, 1):
        text.append(f"{i}. {r['full_name']} · {r['metro'] or '—'} · {r['height_cm'] or '—'}" + (" 🚗" if r['has_car'] else ""))
    await message.answer("\n".join(text), reply_markup=owner_menu())


@router.message(F.text == "📟 Диспетчерская")
async def dispatcher_button(message: Message) -> None:
    await dispatcher_link(message)


@router.message(F.text == "➕ Создать заказ")
@router.message(Command("order"))
async def new_order(message: Message, state: FSMContext) -> None:
    if not await ensure_agent_profile(message, state):
        return
    await state.clear()
    await state.set_state(OrderWizard.source)
    await message.answer("Тип заказа:", reply_markup=ikb([[('Частный','ow:source:private'),('ГБУ','ow:source:gbu')]]))


@router.callback_query(OrderWizard.source, F.data.startswith("ow:source:"))
async def ow_source(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(source=call.data.split(":")[-1])
    await state.set_state(OrderWizard.work_date)
    await call.message.answer("Дата заказа, например <b>25.09.2026</b>:")
    await call.answer()


@router.message(OrderWizard.work_date)
async def ow_date(message: Message, state: FSMContext) -> None:
    d = parse_date(message.text)
    if not d:
        await message.answer("Не понял дату. Формат: 25.09.2026")
        return
    await state.update_data(work_date=d)
    await state.set_state(OrderWizard.deceased)
    await message.answer("Фамилия/ФИО умершего:")


@router.message(OrderWizard.deceased)
async def ow_deceased(message: Message, state: FSMContext) -> None:
    await state.update_data(deceased_name=message.text.strip())
    await state.set_state(OrderWizard.issue_time)
    await message.answer("Время выдачи, например <b>10:30</b>:")


@router.message(OrderWizard.issue_time)
async def ow_issue_time(message: Message, state: FSMContext) -> None:
    t = parse_time(message.text)
    if not t:
        await message.answer("Формат времени: 10:30")
        return
    await state.update_data(issue_time=t)
    await state.set_state(OrderWizard.contact_time)
    await message.answer("Время утренней связи с агентом, например <b>08:30</b>:")


@router.message(OrderWizard.contact_time)
async def ow_contact_time(message: Message, state: FSMContext) -> None:
    t = parse_time(message.text)
    if not t:
        await message.answer("Формат времени: 08:30")
        return
    await state.update_data(contact_time=t)
    await state.set_state(OrderWizard.route)
    await message.answer("Маршрут заказа:")


@router.message(OrderWizard.route)
async def ow_route(message: Message, state: FSMContext) -> None:
    await state.update_data(route=message.text.strip())
    await state.set_state(OrderWizard.category)
    await message.answer("Категория:", reply_markup=ikb([[('Стандарт','ow:cat:standard'),('Вип','ow:cat:vip'),('Элит','ow:cat:elite')]]))


@router.callback_query(OrderWizard.category, F.data.startswith("ow:cat:"))
async def ow_category(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(category=call.data.split(":")[-1])
    await state.set_state(OrderWizard.people_count)
    await call.message.answer("Количество людей <b>вместе с бригадиром</b>:", reply_markup=ikb([[('4','ow:people:4'),('6','ow:people:6')]]))
    await call.answer()


@router.callback_query(OrderWizard.people_count, F.data.startswith("ow:people:"))
async def ow_people(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(people_count=int(call.data.split(":")[-1]))
    await state.set_state(OrderWizard.other_agent)
    await call.message.answer("Контакт на заказе:", reply_markup=ikb([[('Я','ow:other:no'),('Другой агент','ow:other:yes')]]))
    await call.answer()


@router.callback_query(OrderWizard.other_agent, F.data.startswith("ow:other:"))
async def ow_other(call: CallbackQuery, state: FSMContext) -> None:
    other = call.data.endswith("yes")
    await state.update_data(has_other_agent=other)
    if other:
        await state.set_state(OrderWizard.other_agent_name)
        await call.message.answer("ФИО другого агента:")
    else:
        await state.update_data(other_agent_name="", other_agent_phone="")
        await state.set_state(OrderWizard.requires_cash)
        await call.message.answer("По заказу требуется касса?", reply_markup=ikb([[('Да','ow:cash:1'),('Нет','ow:cash:0')]]))
    await call.answer()


@router.message(OrderWizard.other_agent_name)
async def ow_other_name(message: Message, state: FSMContext) -> None:
    await state.update_data(other_agent_name=message.text.strip())
    await state.set_state(OrderWizard.other_agent_phone)
    await message.answer("Телефон другого агента:")


@router.message(OrderWizard.other_agent_phone)
async def ow_other_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(other_agent_phone=message.text.strip())
    await state.set_state(OrderWizard.requires_cash)
    await message.answer("По заказу требуется касса?", reply_markup=ikb([[('Да','ow:cash:1'),('Нет','ow:cash:0')]]))


@router.callback_query(OrderWizard.requires_cash, F.data.startswith("ow:cash:"))
async def ow_cash(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(requires_cash=call.data.endswith("1"))
    data = await state.get_data()
    agent = await db.pool().fetchrow("SELECT * FROM agents WHERE telegram_id=$1", call.from_user.id)
    preview = dict(data)
    preview.update(agent_name=agent["full_name"], agent_phone=agent["phone"], status="new", daily_number="?", agent_telegram_id=call.from_user.id)
    await state.set_state(OrderWizard.confirm)
    await call.message.answer(order_text(preview), reply_markup=ikb([[('✅ Создать','ow:confirm'),('❌ Отмена','ow:cancel')]]))
    await call.answer()


@router.callback_query(OrderWizard.confirm, F.data == "ow:confirm")
async def ow_confirm(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    agent = await db.pool().fetchrow("SELECT * FROM agents WHERE telegram_id=$1", call.from_user.id)
    data.update(agent_telegram_id=call.from_user.id, agent_name=agent["full_name"], agent_phone=agent["phone"])
    order = await db.create_order(data, call.from_user.id)
    await state.clear()
    await call.message.answer("✅ Заказ создан.\n\n" + order_text(order), reply_markup=agent_menu())
    await notify_dispatchers(call.bot, f"🆕 Новый заказ\n\n{order_text(order)}")
    await call.answer()


@router.callback_query(OrderWizard.confirm, F.data == "ow:cancel")
async def ow_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.answer("Создание заказа отменено.", reply_markup=agent_menu())
    await call.answer()


@router.message(F.text == "📋 Мои заказы")
async def my_agent_orders(message: Message) -> None:
    rows = await db.pool().fetch(
        "SELECT * FROM orders WHERE agent_telegram_id=$1 AND work_date >= CURRENT_DATE-2 ORDER BY work_date,issue_time LIMIT 20",
        message.from_user.id,
    )
    if not rows:
        await message.answer("Заказов пока нет.")
        return
    for r in rows:
        if r["status"] in ("cancelled", "completed"):
            kb = None
        else:
            kb = ikb([
                [('🕘 Время',f'aedit:{r["id"]}:issue_time'),('📍 Маршрут',f'aedit:{r["id"]}:route')],
                [('👥 Кол-во',f'aedit:{r["id"]}:people_count'),('❌ Отменить',f'acancel:{r["id"]}')],
            ])
        await message.answer(order_text(dict(r)), reply_markup=kb)


@router.callback_query(F.data.startswith("aedit:"))
async def agent_edit_start(call: CallbackQuery, state: FSMContext) -> None:
    _, oid, field = call.data.split(":")
    o = await db.get_order(int(oid))
    if not o or o["agent_telegram_id"] != call.from_user.id or o["status"] in ("completed", "cancelled"):
        await call.answer("Заказ недоступен", show_alert=True)
        return
    await state.set_state(AgentEdit.value)
    await state.update_data(edit_order_id=int(oid), edit_field=field)
    hint = {"issue_time":"Новое время HH:MM", "route":"Новый маршрут", "people_count":"Введите 4 или 6"}[field]
    await call.message.answer(hint)
    await call.answer()


@router.message(AgentEdit.value)
async def agent_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    field = data["edit_field"]
    value: Any = message.text.strip()
    if field == "issue_time":
        value = parse_time(value)
        if not value:
            await message.answer("Формат HH:MM")
            return
    if field == "people_count":
        if value not in ("4", "6"):
            await message.answer("Можно только 4 или 6.")
            return
        value = int(value)
    row, invalidated = await db.update_order(data["edit_order_id"], {field: value}, message.from_user.id)
    await state.clear()
    await message.answer("✅ Изменение сохранено.\n\n" + order_text(row))
    note = "\n⚠️ Состав снят и требует повторной расстановки." if invalidated else ""
    await notify_dispatchers(bot, f"✏️ Агент изменил заказ\n\n{order_text(row)}{note}")


@router.callback_query(F.data.startswith("acancel:"))
async def agent_cancel_order(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[-1])
    o = await db.get_order(oid)
    if not o or o["agent_telegram_id"] != call.from_user.id:
        await call.answer("Недоступно", show_alert=True)
        return
    await db.cancel_order(oid, call.from_user.id)
    await call.message.answer(f"❌ Заказ {SOURCE_LABELS[o['source']]} №{o['daily_number']} отменён.")
    await notify_dispatchers(call.bot, f"❌ Агент отменил заказ\n\n{order_text(o)}")
    await call.answer()


@router.message(Command("brigadier"))
@router.message(F.text == "📌 Мои заказы")
async def brigadier_orders(message: Message) -> None:
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp or emp["employee_group"] != "brigadier":
        return
    rows = await db.pool().fetch(
        """SELECT o.*, a.accepted_at,a.contact_confirmed_at FROM assignments a JOIN orders o ON o.id=a.order_id
           WHERE a.employee_id=$1 AND a.role='brigadier' AND o.work_date >= CURRENT_DATE-1 AND o.status<>'cancelled'
           ORDER BY o.work_date,o.issue_time LIMIT 20""", emp["id"],
    )
    if not rows:
        await message.answer("Назначенных заказов нет.")
        return
    for r in rows:
        rows_kb = []
        if not r["accepted_at"]:
            rows_kb.append([('✅ Заказ принял',f'bacc:{r["id"]}')])
        if not r["contact_confirmed_at"]:
            rows_kb.append([('📞 Связь подтверждена',f'bcontact:{r["id"]}')])
        rows_kb.append([('📷 Фотоотчёт',f'bphoto:{r["id"]}'),('🏁 Заказ закончил',f'bfinish:{r["id"]}')])
        await message.answer(order_text(dict(r)), reply_markup=ikb(rows_kb))


@router.callback_query(F.data.startswith("bacc:"))
async def brig_accept(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[-1])
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not emp:
        return
    await db.pool().execute("UPDATE assignments SET accepted_at=NOW() WHERE order_id=$1 AND employee_id=$2 AND role='brigadier'", oid, emp["id"])
    await db.pool().execute("UPDATE orders SET status='in_progress' WHERE id=$1 AND status IN ('ready','sent')", oid)
    await call.message.answer("✅ Получение заказа подтверждено.")
    await call.answer()


@router.callback_query(F.data.startswith("bcontact:"))
async def brig_contact(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[-1])
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not emp:
        return
    await db.pool().execute("UPDATE assignments SET contact_confirmed_at=NOW() WHERE order_id=$1 AND employee_id=$2 AND role='brigadier'", oid, emp["id"])
    await call.message.answer("📞 Связь отмечена.")
    await call.answer()


@router.callback_query(F.data.startswith("bphoto:"))
async def brig_photo(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":")[-1])
    o = await db.get_order(oid)
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not o or not emp:
        return
    count = int(await db.pool().fetchval("SELECT COUNT(*) FROM photos WHERE order_id=$1", oid) or 0)
    if o["source"] == "private" and count >= 1:
        await call.answer("Фото уже получено", show_alert=True)
        return
    if o["source"] == "gbu" and count >= 2:
        await call.answer("Оба фото уже получены", show_alert=True)
        return
    kind = "private" if o["source"] == "private" else ("gbu_first" if count == 0 else "gbu_second")
    await state.set_state(PhotoFlow.waiting_photo)
    await state.update_data(photo_order_id=oid, photo_kind=kind)
    text = "Отправьте одно фото по заказу." if kind == "private" else (
        "Отправьте первое фото с фамилией." if kind == "gbu_first" else "Отправьте второе фото ответом на первое фото."
    )
    await call.message.answer(text)
    await call.answer()


@router.message(PhotoFlow.waiting_photo, F.photo)
async def receive_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    oid = int(data["photo_order_id"])
    kind = data["photo_kind"]
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp:
        await state.clear()
        return

    if kind == "gbu_second":
        first = await db.pool().fetchrow(
            "SELECT telegram_message_id FROM photos WHERE order_id=$1 AND kind='gbu_first' ORDER BY id DESC LIMIT 1",
            oid,
        )
        if first and (not message.reply_to_message or message.reply_to_message.message_id != first["telegram_message_id"]):
            await message.answer(
                "Второе фото нужно отправить <b>ответом на первое</b>. "
                "Нажмите Reply/Ответить на первое фото и отправьте ещё раз."
            )
            return

    await db.pool().execute(
        """INSERT INTO photos(order_id,employee_id,kind,telegram_file_id,telegram_message_id,reply_to_message_id)
           VALUES($1,$2,$3,$4,$5,$6)""",
        oid, emp["id"], kind, message.photo[-1].file_id, message.message_id,
        message.reply_to_message.message_id if message.reply_to_message else None,
    )
    o = await db.get_order(oid)

    if kind == "gbu_first":
        # The first GBU photo is kept inside the bot and is NOT forwarded to
        # the photo-report group. Keep the state open for the second photo.
        await state.update_data(photo_kind="gbu_second")
        await message.answer(
            "✅ Первое фото сохранено. В фотоотчёт оно пока не отправлено.\n"
            "Теперь отправьте <b>второе фото ответом на первое</b>."
        )
        return

    if kind == "gbu_second":
        # Two photos mean the GBU job is finished. Ask only for tea/tips, then
        # publish both photos together with one order report.
        await db.pool().execute(
            "UPDATE orders SET completed_at=COALESCE(completed_at,NOW()),status='completed',updated_at=NOW() WHERE id=$1",
            oid,
        )
        await state.clear()
        await state.update_data(
            finish_order_id=oid,
            gbu_report_pending=oid,
            gbu_report_brigadier_tg=message.from_user.id,
        )
        await message.answer(
            "📷 Второе фото получено.\n\n<b>Отчёт по заказу — чай:</b>",
            reply_markup=ikb([[('Без чая',f'tip0:{oid}'),('Указать сумму',f'tipx:{oid}')]]),
        )
        return

    # Private order behaviour stays unchanged: one photo is sent immediately.
    await state.clear()
    await message.answer("📷 Фото получено.")
    caption = f"📷 Фотоотчёт: {SOURCE_LABELS[o['source']]} №{o['daily_number']} — {emp['full_name']}"
    for chat_id in photo_report_targets():
        with contextlib.suppress(Exception):
            await bot.send_photo(chat_id, message.photo[-1].file_id, caption=caption)


@router.message(PhotoFlow.waiting_photo)
async def photo_only(message: Message) -> None:
    await message.answer("Нужно отправить именно фотографию.")


@router.callback_query(F.data.startswith("bfinish:"))
async def brig_finish(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":")[-1])
    emp = await db.get_employee_by_tg(call.from_user.id)
    if not emp:
        return
    current_order = await db.get_order(oid)
    if current_order and current_order.get("status") == "completed":
        await call.answer("Заказ уже закрыт после фотоотчёта", show_alert=True)
        return
    assigned = await db.pool().fetchval("SELECT 1 FROM assignments WHERE order_id=$1 AND employee_id=$2 AND role='brigadier'", oid, emp["id"])
    if not assigned:
        await call.answer("Это не ваш заказ", show_alert=True)
        return
    await db.pool().execute("UPDATE orders SET completed_at=NOW(),status='completed',updated_at=NOW() WHERE id=$1", oid)
    await state.update_data(finish_order_id=oid)
    await call.message.answer("Чаевые:", reply_markup=ikb([[('Без чаевых',f'tip0:{oid}'),('Указать сумму',f'tipx:{oid}')]]))
    await call.answer()


@router.callback_query(F.data.startswith("tip0:"))
async def tip_zero(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":")[-1])
    await db.pool().execute("UPDATE orders SET tips_amount=0 WHERE id=$1", oid)
    await send_gbu_photo_report_if_pending(call.bot, state, oid)
    await maybe_ask_cash(call.message, state, oid)
    await call.answer()


@router.callback_query(F.data.startswith("tipx:"))
async def tip_custom(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":")[-1])
    await state.set_state(FinishFlow.tip_amount)
    await state.update_data(finish_order_id=oid)
    await call.message.answer("Введите сумму чаевых в ₽:")
    await call.answer()


@router.message(FinishFlow.tip_amount)
async def tip_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Введите число, например 1500")
        return
    data = await state.get_data()
    oid = int(data["finish_order_id"])
    await db.pool().execute("UPDATE orders SET tips_amount=$2 WHERE id=$1", oid, amount)
    await send_gbu_photo_report_if_pending(message.bot, state, oid)
    await maybe_ask_cash(message, state, oid)


async def maybe_ask_cash(message: Message, state: FSMContext, oid: int) -> None:
    o = await db.get_order(oid)
    if o["requires_cash"]:
        await state.set_state(FinishFlow.cash_amount)
        await state.update_data(finish_order_id=oid)
        await message.answer("Введите сумму кассы в ₽:")
    else:
        await state.clear()
        await message.answer("🏁 Заказ закрыт.")


@router.message(FinishFlow.cash_amount)
async def cash_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal(message.text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        await message.answer("Введите число, например 12500")
        return
    await state.update_data(cash_amount=str(amount))
    await state.set_state(FinishFlow.cash_people_count)
    await message.answer(
        "👥 Выберите количество сотрудников на заказе:",
        reply_markup=ikb([
            [('2','cashpeople:2'),('3','cashpeople:3'),('4','cashpeople:4'),('5','cashpeople:5')],
            [('6','cashpeople:6'),('7','cashpeople:7'),('8','cashpeople:8')],
        ]),
    )


@router.callback_query(FinishFlow.cash_people_count, F.data.startswith("cashpeople:"))
async def cash_people_count(call: CallbackQuery, state: FSMContext) -> None:
    count = int(call.data.split(":")[-1])
    if count < 2 or count > 8:
        await call.answer("Допустимо от 2 до 8 сотрудников", show_alert=True)
        return
    data = await state.get_data()
    oid = int(data["finish_order_id"])
    amount = Decimal(str(data["cash_amount"]))
    emp = await db.get_employee_by_tg(call.from_user.id)
    try:
        await db.pool().execute(
            """INSERT INTO cash_entries(order_id,brigadier_employee_id,amount,employee_count,updated_by)
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT(order_id) DO UPDATE SET
                 amount=EXCLUDED.amount,employee_count=EXCLUDED.employee_count,
                 brigadier_employee_id=EXCLUDED.brigadier_employee_id,updated_by=EXCLUDED.updated_by,updated_at=NOW()
            """,
            oid, emp["id"] if emp else None, amount, count, call.from_user.id,
        )
    except Exception:
        # Safe fallback while the dispatcher is still restarting on the schema migration.
        await db.pool().execute(
            """INSERT INTO cash_entries(order_id,brigadier_employee_id,amount,updated_by) VALUES($1,$2,$3,$4)
               ON CONFLICT(order_id) DO UPDATE SET amount=EXCLUDED.amount,brigadier_employee_id=EXCLUDED.brigadier_employee_id,updated_by=EXCLUDED.updated_by,updated_at=NOW()
            """,
            oid, emp["id"] if emp else None, amount, call.from_user.id,
        )
    await state.clear()
    await call.message.answer(f"🏁 Заказ закрыт. Касса сохранена. Сотрудников: {count}.")
    await call.answer()


@router.message(F.text == "ℹ️ Мой статус")
async def my_status(message: Message) -> None:
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp:
        return
    rc = await db.pool().fetchrow("SELECT * FROM readiness_current WHERE employee_id=$1 AND work_date=$2", emp["id"], settings.now().date())
    if not rc:
        await message.answer("Сегодня готовность ещё не отправлена.")
    else:
        await message.answer(f"Сегодня: <b>{READINESS_STATUSES[rc['status']]}</b>\nПоследнее обновление: {rc['updated_at'].astimezone(settings.tz).strftime('%H:%M')}")


async def notify_dispatchers(bot: Bot, text: str) -> None:
    for uid in settings.allowed_dispatcher_ids:
        with contextlib.suppress(Exception):
            await bot.send_message(uid, text)


async def send_once(bot: Bot, order_id: int, employee_id: int | None, key: str, tg_id: int, text: str) -> bool:
    inserted = await db.pool().fetchrow(
        """INSERT INTO notification_state(order_id,employee_id,notification_key) VALUES($1,$2,$3)
           ON CONFLICT(order_id,employee_id,notification_key) DO NOTHING RETURNING id""",
        order_id, employee_id, key,
    )
    if not inserted:
        return False
    try:
        await bot.send_message(tg_id, text)
        return True
    except Exception:
        await db.pool().execute("DELETE FROM notification_state WHERE id=$1", inserted["id"])
        return False


async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            now = settings.now()
            today = now.date()
            rows = await db.pool().fetch(
                """SELECT o.*, a.employee_id,a.accepted_at,a.contact_confirmed_at,e.telegram_id,e.full_name,e.phone
                   FROM orders o JOIN assignments a ON a.order_id=o.id AND a.role='brigadier'
                   JOIN employees e ON e.id=a.employee_id
                   WHERE o.work_date=$1 AND o.status NOT IN ('cancelled','completed')""", today,
            )
            for r in rows:
                if r["contact_time"]:
                    contact_dt = datetime.combine(today, r["contact_time"], tzinfo=settings.tz)
                    delta = (contact_dt - now).total_seconds() / 60
                    if 0 <= delta <= settings.reminder_minutes + 0.9 and not r["contact_confirmed_at"]:
                        await send_once(bot, r["id"], r["employee_id"], "contact_minus", r["telegram_id"], f"⏰ Через {settings.reminder_minutes} мин связь по заказу {SOURCE_LABELS[r['source']]} №{r['daily_number']}.")
                    if -1 <= delta <= 1 and not r["contact_confirmed_at"]:
                        await send_once(bot, r["id"], r["employee_id"], "contact_now", r["telegram_id"], f"📞 Время связи по заказу {SOURCE_LABELS[r['source']]} №{r['daily_number']}. Подтвердите связь в боте.")
                    if delta <= 0 and r["accepted_at"] and not r["agent_contact_sent_at"] and r["agent_telegram_id"]:
                        text = f"Бригадир по заказу {SOURCE_LABELS[r['source']]} №{r['daily_number']}: {r['full_name']}, {r['phone']}"
                        if await send_once(bot, r["id"], r["employee_id"], "agent_brig_contact", r["agent_telegram_id"], text):
                            await db.pool().execute("UPDATE orders SET agent_contact_sent_at=NOW() WHERE id=$1", r["id"])

            # 3-hour second-photo reminder for GBU
            gbu = await db.pool().fetch(
                """SELECT o.id,o.daily_number,a.employee_id,e.telegram_id,MIN(p.created_at) first_at,
                          COUNT(*) FILTER(WHERE p.kind='gbu_second') second_count
                   FROM orders o JOIN assignments a ON a.order_id=o.id AND a.role='brigadier'
                   JOIN employees e ON e.id=a.employee_id JOIN photos p ON p.order_id=o.id
                   WHERE o.work_date=$1 AND o.source='gbu' GROUP BY o.id,o.daily_number,a.employee_id,e.telegram_id""", today,
            )
            for r in gbu:
                if r["second_count"] == 0 and now - r["first_at"].astimezone(settings.tz) >= timedelta(hours=3):
                    await send_once(bot, r["id"], r["employee_id"], "gbu_second_photo", r["telegram_id"], f"📷 По ГБУ №{r['daily_number']} нет второго фото. Отправьте его ответом на первое.")

            # cash reminder at/after 20:00
            if now.time() >= time(20, 0):
                cash = await db.pool().fetch(
                    """SELECT o.id,o.daily_number,o.source,a.employee_id,e.telegram_id FROM orders o
                       JOIN assignments a ON a.order_id=o.id AND a.role='brigadier' JOIN employees e ON e.id=a.employee_id
                       LEFT JOIN cash_entries c ON c.order_id=o.id
                       WHERE o.work_date=$1 AND o.status='completed' AND o.requires_cash=TRUE AND c.id IS NULL""", today,
                )
                for r in cash:
                    await send_once(bot, r["id"], r["employee_id"], "cash_20", r["telegram_id"], f"💰 По заказу {SOURCE_LABELS[r['source']]} №{r['daily_number']} не внесена касса.")
        except Exception:
            log.exception("reminder loop")
        await asyncio.sleep(60)


@router.message()
async def readiness_free_text(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None or not message.text:
        return
    emp = await db.get_employee_by_tg(message.from_user.id)
    if not emp:
        return
    result = await db.set_readiness(message.from_user.id, message.text)
    if result["ok"]:
        await message.answer(f"✅ На сегодня: <b>{READINESS_STATUSES[result['status']]}</b>")
    elif result["reason"] == "status_not_recognized":
        await message.answer("Не распознал статус. Напишите одним сообщением: «Готов 186 на авто», «На основной 186 без авто» или «Выходной».")


async def on_startup(bot: Bot) -> None:
    await db.init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="order", description="Создать заказ"),
        BotCommand(command="brigadier", description="Заказы бригадира"),
        BotCommand(command="dispatcher", description="Открыть диспетчерскую"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="cash", description="Касса"),
        BotCommand(command="photos", description="Фотоотчёт"),
    ])
    if settings.dispatcher_url:
        for uid in settings.allowed_dispatcher_ids:
            with contextlib.suppress(Exception):
                await bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonWebApp(text="Диспетчерская", web_app=WebAppInfo(url=settings.dispatcher_url)))
    asyncio.create_task(reminder_loop(bot))


async def on_shutdown() -> None:
    await db.close_db()


async def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
