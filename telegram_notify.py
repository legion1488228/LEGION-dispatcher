import os
from html import escape

import httpx


async def send_telegram_message(chat_id: int, text: str, reply_markup: dict | None = None) -> bool:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or not chat_id:
        return False
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        r.raise_for_status()
    return True


def _contact_suffix(employee) -> str:
    parts = []
    if getattr(employee, "phone", ""):
        parts.append(escape(employee.phone))
    username = (getattr(employee, "telegram_username", "") or "").lstrip("@")
    tg_id = getattr(employee, "tg_id", None)
    if username:
        parts.append(f'<a href="https://t.me/{escape(username)}">Telegram</a>')
    elif tg_id:
        parts.append(f'<a href="tg://user?id={int(tg_id)}">Telegram</a>')
    return " · " + " · ".join(parts) if parts else ""


def _agent_contact_block(order) -> str:
    name = escape(getattr(order, "agent_name", "") or "—")
    phone = escape(getattr(order, "agent_phone", "") or "—")
    source = str(getattr(order, "source", "") or "").lower()

    lines = [
        f"<b>Агент:</b> {name}",
        f"<b>Телефон агента:</b> {phone}",
    ]

    # Telegram contact is shown to brigadier ONLY for private orders.
    if source == "private":
        tg_id = getattr(order, "agent_tg_id", None)
        telegram = (
            f'<a href="tg://user?id={int(tg_id)}">Открыть Telegram агента</a>'
            if tg_id else "—"
        )
        lines.append(f"<b>Telegram агента:</b> {telegram}")

    return "\n".join(lines)


def brigadier_order_text(order, brigadier, members) -> str:
    member_text = "\n".join(
        f"• {escape(x.full_name)} — {x.height_cm or '—'} см{_contact_suffix(x)}"
        for x in members
    )
    extra = f"\n<b>Дополнительно:</b> откат {order.kickback_rub} ₽" if order.kickback_rub else ""
    return (
        "<b>📋 НАЗНАЧЕН ЗАКАЗ</b>\n\n"
        f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n"
        f"<b>Умерший:</b> {escape(order.deceased_name)}\n"
        f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}\n"
        f"<b>Подача:</b> {order.arrival_time.strftime('%H:%M') if order.arrival_time else '—'}\n"
        f"<b>Категория:</b> {escape(order.category.title())}\n"
        f"<b>Маршрут:</b> {escape(order.route or '—')}\n\n"
        f"{_agent_contact_block(order)}\n\n"
        f"<b>Бригадир:</b> {escape(brigadier.full_name)}\n"
        f"<b>Состав:</b>\n{member_text or '—'}"
        f"{extra}\n\n"
        "После закрытия заказа подтвердите его в боте и укажите время связи утром."
    )


def agent_brigadier_text(order, brigadier, contact_time: str) -> str:
    source = str(getattr(order, "source", "") or "").lower()

    lines = [
        "<b>✅ БРИГАДА НАЗНАЧЕНА</b>",
        "",
        f"<b>Умерший:</b> {escape(order.deceased_name)}",
        f"<b>Бригадир:</b> {escape(brigadier.full_name)}",
    ]

    # Brigadier phone is sent to the agent ONLY for private orders.
    if source == "private":
        lines.append(f"<b>Телефон:</b> {escape(brigadier.phone)}")

    lines.append(
        f"<b>Связь утром:</b> {escape(contact_time or 'по договорённости')}"
    )
    return "\n".join(lines)

