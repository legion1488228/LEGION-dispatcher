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


def brigadier_order_text(order, brigadier, members) -> str:
    member_text = "\n".join(f"• {escape(x.full_name)} — {x.height_cm or '—'} см" for x in members)
    extra = f"\n<b>Дополнительно:</b> откат {order.kickback_rub} ₽" if order.kickback_rub else ""
    return (
        "<b>📋 НАЗНАЧЕН ЗАКАЗ</b>\n\n"
        f"<b>Дата:</b> {order.work_date.strftime('%d.%m.%Y')}\n"
        f"<b>Умерший:</b> {escape(order.deceased_name)}\n"
        f"<b>Выдача:</b> {order.issue_time.strftime('%H:%M')}\n"
        f"<b>Подача:</b> {order.arrival_time.strftime('%H:%M') if order.arrival_time else '—'}\n"
        f"<b>Категория:</b> {escape(order.category.title())}\n"
        f"<b>Маршрут:</b> {escape(order.route or '—')}\n\n"
        f"<b>Бригадир:</b> {escape(brigadier.full_name)}\n"
        f"<b>Состав:</b>\n{member_text or '—'}"
        f"{extra}\n\n"
        "После закрытия заказа подтвердите его в боте и укажите время связи утром."
    )


def agent_brigadier_text(order, brigadier, contact_time: str) -> str:
    return (
        "<b>✅ БРИГАДА НАЗНАЧЕНА</b>\n\n"
        f"<b>Умерший:</b> {escape(order.deceased_name)}\n"
        f"<b>Бригадир:</b> {escape(brigadier.full_name)}\n"
        f"<b>Телефон:</b> {escape(brigadier.phone)}\n"
        f"<b>Связь утром:</b> {escape(contact_time or 'по договорённости')}"
    )
