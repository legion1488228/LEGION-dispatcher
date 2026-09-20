# Подключение существующего Telegram-бота «ЛЕГИОН» к Диспетчерской

Mini App — отдельный сервис. Ваш существующий бот продолжает работать с агентами,
бригадирами и четырьмя группами сотрудников. Он только передаёт данные в Mini App через API.

## 1. Переменные в сервисе БОТА

Добавьте в Railway сервис текущего бота:

```text
DISPATCHER_API_URL=https://ВАШ-ДОМЕН-MINI-APP.railway.app
BOT_API_KEY=ТОТ_ЖЕ_СЕКРЕТ_ЧТО_В_MINI_APP
```

Добавьте в requirements бота:

```text
httpx==0.28.1
```

Скопируйте `dispatcher_client.py` рядом с `bot.py`.

## 2. Регистрация агента

Сразу после сохранения ФИО и телефона агента:

```python
from dispatcher_client import sync_agent

await sync_agent(
    tg_id=message.from_user.id,
    full_name=name,
    phone=phone,
    organization="",
)
```

## 3. Регистрация бригадира

После сохранения профиля бригадира:

```python
from dispatcher_client import sync_employee

await sync_employee(
    tg_id=message.from_user.id,
    full_name=name,
    phone=phone,
    group_code="brigadier",
    height_cm=height_cm,
    has_car=has_car,
)
```

## 4. Готовность в 4 группах

Соответствие групп:

- Бригадиры → `brigadier`
- Основной состав → `main`
- Безнал состав → `cashless`
- Резерв → `reserve`

Когда бот увидел `готов 185`, `готов 185 на авто`, `не готов` или `выходной`, передайте:

```python
from dispatcher_client import report_readiness

await report_readiness(
    tg_id=message.from_user.id,
    full_name=message.from_user.full_name,
    group_code="main",
    work_date=tomorrow,
    status="ready",          # ready / not_ready / day_off
    height_cm=185,
    has_car=True,
    raw_text=message.text,
)
```

Mini App сама посчитает официальный срез на 15:30 и отдельно покажет тех,
кто не отписался к этому времени.

## 5. Новый заказ агента

После нажатия агентом «Отправить заказ» бот передаёт заказ в Mini App:

```python
from dispatcher_client import push_order

result = await push_order({
    "source": "private",
    "work_date": "2026-09-21",
    "issue_time": "10:00",
    "arrival_time": "09:30",
    "deceased_name": "Гудковский Александр Владимирович",
    "route": "СМЭ Жуковский → храм Космы → кладбище Загорново",
    "category": "vip",
    "team_size": 4,
    "organization": "Раменское",
    "agent_tg_id": message.from_user.id,
    "agent_name": "Арсений",
    "agent_phone": "89998369351",
})
```

Заказ сразу появится у вас и помощников в папке нужной даты.

## 6. Готовая форма агента

Добавьте агенту кнопку `📎 Отправить готовую форму`.

Текст можно отправить на парсер:

```python
from dispatcher_client import parse_ready_form

draft = await parse_ready_form(message.text, "Раменское")
```

или:

```python
draft = await parse_ready_form(message.text, "Марина контора")
```

Бот должен показать агенту распознанную карточку **до сохранения**:

`✅ Всё верно` / `✏️ Изменить`.

Для профиля «Марина контора» Mini App автоматически считает:

- 4 человека → откат 1000 ₽
- 6 человек → откат 1500 ₽

Гроб, постель, ленты, катафалк и другие ненужные позиции в карточку бригады не переносятся.

## 7. Назначение бригады

Помощник в Mini App:

1. выбирает готового бригадира одной кнопкой;
2. ставит галочки сотрудникам;
3. приложение предлагает сначала рост ±2 см к бригадиру;
4. рядом видны группа, рост, 🚗 и загрузка `0/2`, `1/2`, `2/2`;
5. человек с `2/2` заблокирован;
6. нажимает `Состав готов`.

Только после полного состава Mini App отправит заказ бригадиру в личные сообщения.

## 8. Бригадир закрыл заказ

Mini App отправляет бригадиру inline-кнопку с callback:

```text
mini_brig_close:<PUBLIC_ID>
```

В текущем боте добавьте обработчик этого callback. Он должен спросить время связи утром,
например кнопками `07:00 / 07:30 / 08:00 / 08:30 / Другое`.

После выбора времени вызовите:

```python
from dispatcher_client import brigadier_close

await brigadier_close(public_id, "08:00")
```

После этого Mini App сама отправит агенту:

- ФИО бригадира;
- телефон бригадира;
- время связи утром.

## 9. Важно

Mini App не использует `getUpdates` и не запускает Telegram polling. Поэтому она может
использовать тот же `BOT_TOKEN`, что и существующий бот, для отправки сообщений.
Polling остаётся только в вашем основном `bot.py`.

## 10. Кнопка Mini App только вам и помощникам

Не обязательно включать глобальную кнопку Mini App для всех пользователей бота.
В существующем боте можно показывать WebApp-кнопку только вашим Telegram ID.

Добавьте в Variables бота:

```text
MINI_APP_URL=https://ВАШ-ДОМЕН-MINI-APP.railway.app
MINI_APP_ADMIN_IDS=ВАШ_ID,ID_ПОМОЩНИКА_1,ID_ПОМОЩНИКА_2
```

Пример aiogram-кнопки:

```python
import os
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

ADMIN_IDS = {int(x) for x in os.getenv("MINI_APP_ADMIN_IDS", "").split(",") if x}
MINI_APP_URL = os.getenv("MINI_APP_URL")

def owner_menu(user_id: int):
    rows = []
    if user_id in ADMIN_IDS and MINI_APP_URL:
        rows.append([KeyboardButton(
            text="📋 Диспетчерская",
            web_app=WebAppInfo(url=MINI_APP_URL),
        )])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True) if rows else None
```

Агентам и бригадирам такую кнопку не показывайте. Даже если посторонний узнает URL,
backend Mini App всё равно проверит его Telegram ID через `ALLOWED_TELEGRAM_IDS`.
