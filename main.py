import asyncio
import logging
import os
import secrets
import sqlite3
import string
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не указан")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID не указан")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("license_bot")

bot = Bot(
    token=BOT_TOKEN,
    parse_mode="HTML"
)

dp = Dispatcher(
    bot,
    storage=MemoryStorage()
)

broadcast_task = None
broadcast_running = False


# =========================================================
# DATABASE
# =========================================================

def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    connection = get_db()

    connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS licenses (
            license_key TEXT PRIMARY KEY,
            user_id INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0
        );
    """)

    connection.commit()
    connection.close()


def utc_now():
    return datetime.now(timezone.utc)


def save_user(user: types.User):
    connection = get_db()

    current_time = utc_now().isoformat()

    connection.execute(
        """
        INSERT INTO users
        (
            user_id,
            username,
            first_name,
            created_at,
            last_seen
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
        """,
        (
            user.id,
            user.username,
            user.first_name,
            current_time,
            current_time
        )
    )

    connection.commit()
    connection.close()


# =========================================================
# LICENSE SYSTEM
# =========================================================

def generate_license_key():

    alphabet = string.ascii_uppercase + string.digits

    parts = []

    for _ in range(4):
        part = "".join(
            secrets.choice(alphabet)
            for _ in range(4)
        )

        parts.append(part)

    return "PAZS-" + "-".join(parts)


def create_license(days: int):

    connection = get_db()

    while True:

        key = generate_license_key()

        exists = connection.execute(
            """
            SELECT 1
            FROM licenses
            WHERE license_key=?
            """,
            (key,)
        ).fetchone()

        if not exists:
            break

    expires = utc_now() + timedelta(days=days)

    connection.execute(
        """
        INSERT INTO licenses
        (
            license_key,
            user_id,
            created_at,
            expires_at,
            active
        )
        VALUES (?, NULL, ?, ?, 1)
        """,
        (
            key,
            utc_now().isoformat(),
            expires.isoformat()
        )
    )

    connection.commit()
    connection.close()

    return key, expires


def get_user_license(user_id: int):

    connection = get_db()

    license_row = connection.execute(
        """
        SELECT *
        FROM licenses
        WHERE user_id=?
        AND active=1
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (user_id,)
    ).fetchone()

    connection.close()

    if not license_row:
        return None

    expires = datetime.fromisoformat(
        license_row["expires_at"]
    )

    if expires <= utc_now():

        connection = get_db()

        connection.execute(
            """
            UPDATE licenses
            SET active=0
            WHERE license_key=?
            """,
            (license_row["license_key"],)
        )

        connection.commit()
        connection.close()

        return None

    return license_row


def has_valid_license(user_id: int):

    return get_user_license(user_id) is not None


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard(user_id: int):

    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.add(
        InlineKeyboardButton(
            "📢 Рассылка",
            callback_data="broadcast"
        ),
        InlineKeyboardButton(
            "🔑 Лицензия",
            callback_data="license"
        )
    )

    if user_id == ADMIN_ID:

        keyboard.add(
            InlineKeyboardButton(
                "🛠 Админ-панель",
                callback_data="admin"
            )
        )

    return keyboard


def license_keyboard():

    keyboard = InlineKeyboardMarkup(row_width=1)

    keyboard.add(
        InlineKeyboardButton(
            "🔑 Активировать ключ",
            callback_data="license_activate"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "📅 Моя лицензия",
            callback_data="license_info"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "🔙 Назад",
            callback_data="home"
        )
    )

    return keyboard


def admin_keyboard():

    keyboard = InlineKeyboardMarkup(row_width=2)

    keyboard.add(
        InlineKeyboardButton(
            "🔑 Создать ключ",
            callback_data="admin_create_key"
        ),
        InlineKeyboardButton(
            "📊 Статистика",
            callback_data="admin_stats"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "📢 Рассылки",
            callback_data="admin_broadcasts"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "🔙 Назад",
            callback_data="home"
        )
    )

    return keyboard


def broadcast_keyboard():

    keyboard = InlineKeyboardMarkup(row_width=1)

    keyboard.add(
        InlineKeyboardButton(
            "✏️ Создать рассылку",
            callback_data="broadcast_create"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "⏹ Остановить",
            callback_data="broadcast_stop"
        )
    )

    keyboard.add(
        InlineKeyboardButton(
            "🔙 Назад",
            callback_data="home"
        )
    )

    return keyboard


# =========================================================
# FSM
# =========================================================

class LicenseStates(StatesGroup):

    waiting_key = State()


class AdminStates(StatesGroup):

    waiting_days = State()


class BroadcastStates(StatesGroup):

    waiting_text = State()


# =========================================================
# START
# =========================================================

@dp.message_handler(commands=["start"])
async def start_handler(message: types.Message):

    save_user(message.from_user)

    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# HOME
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "home"
)
async def home_handler(
    callback: types.CallbackQuery,
    state: FSMContext
):

    await state.finish()

    await callback.answer()

    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>",
        reply_markup=main_keyboard(
            callback.from_user.id
        )
    )


# =========================================================
# LICENSE MENU
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "license"
)
async def license_handler(
    callback: types.CallbackQuery
):

    await callback.answer()

    await callback.message.edit_text(
        "🔑 <b>Лицензия</b>\n\n"
        "Здесь можно активировать ключ "
        "или посмотреть срок действия.",
        reply_markup=license_keyboard()
    )


# =========================================================
# ACTIVATE LICENSE
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "license_activate"
)
async def license_activate_handler(
    callback: types.CallbackQuery
):

    await callback.answer()

    await callback.message.edit_text(
        "🔑 <b>Активация лицензии</b>\n\n"
        "Отправь лицензионный ключ:"
    )

    await LicenseStates.waiting_key.set()


@dp.message_handler(
    state=LicenseStates.waiting_key
)
async def license_key_handler(
    message: types.Message,
    state: FSMContext
):

    key = message.text.strip().upper()

    connection = get_db()

    license_row = connection.execute(
        """
        SELECT *
        FROM licenses
        WHERE license_key=?
        AND active=1
        """,
        (key,)
    ).fetchone()

    if not license_row:

        connection.close()

        await state.finish()

        await message.answer(
            "❌ Такой ключ не существует "
            "или уже отключён."
        )

        return

    expires = datetime.fromisoformat(
        license_row["expires_at"]
    )

    if expires <= utc_now():

        connection.execute(
            """
            UPDATE licenses
            SET active=0
            WHERE license_key=?
            """,
            (key,)
        )

        connection.commit()
        connection.close()

        await state.finish()

        await message.answer(
            "❌ Срок действия ключа уже истёк."
        )

        return

    connection.execute(
        """
        UPDATE licenses
        SET user_id=?
        WHERE license_key=?
        """,
        (
            message.from_user.id,
            key
        )
    )

    connection.commit()
    connection.close()

    await state.finish()

    await message.answer(
        "✅ <b>Лицензия активирована!</b>\n\n"
        f"🔑 Ключ: <code>{key}</code>\n"
        f"📅 До: <code>"
        f"{expires.strftime('%d.%m.%Y %H:%M UTC')}"
        f"</code>",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# LICENSE INFO
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "license_info"
)
async def license_info_handler(
    callback: types.CallbackQuery
):

    await callback.answer()

    license_row = get_user_license(
        callback.from_user.id
    )

    if not license_row:

        text = (
            "🔒 <b>Активной лицензии нет.</b>\n\n"
            "Активируй лицензионный ключ."
        )

    else:

        expires = datetime.fromisoformat(
            license_row["expires_at"]
        )

        text = (
            "🔑 <b>Твоя лицензия</b>\n\n"
            f"Ключ: <code>"
            f"{license_row['license_key']}"
            f"</code>\n"
            f"Действует до: <code>"
            f"{expires.strftime('%d.%m.%Y %H:%M UTC')}"
            f"</code>"
        )

    await callback.message.edit_text(
        text,
        reply_markup=license_keyboard()
    )


# =========================================================
# BROADCAST MENU
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "broadcast"
)
async def broadcast_handler(
    callback: types.CallbackQuery
):

    if not has_valid_license(
        callback.from_user.id
    ):

        await callback.answer(
            "🔒 Нужна активная лицензия.",
            show_alert=True
        )

        return

    await callback.answer()

    status = (
        "🟢 запущена"
        if broadcast_running
        else
        "🔴 остановлена"
    )

    await callback.message.edit_text(
        "📢 <b>Рассылка</b>\n\n"
        f"Статус: {status}",
        reply_markup=broadcast_keyboard()
    )


# =========================================================
# CREATE BROADCAST
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "broadcast_create"
)
async def broadcast_create_handler(
    callback: types.CallbackQuery
):

    if not has_valid_license(
        callback.from_user.id
    ):

        await callback.answer(
            "🔒 Нужна активная лицензия.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(
        "✏️ <b>Новая рассылка</b>\n\n"
        "Отправь текст сообщения.\n\n"
        "Сообщение будет отправлено "
        "только пользователям, которые "
        "ранее запустили этого бота."
    )

    await BroadcastStates.waiting_text.set()


@dp.message_handler(
    state=BroadcastStates.waiting_text
)
async def broadcast_text_handler(
    message: types.Message,
    state: FSMContext
):

    global broadcast_task

    if not has_valid_license(
        message.from_user.id
    ):

        await state.finish()

        await message.answer(
            "🔒 Лицензия недействительна."
        )

        return

    if not message.text:

        await message.answer(
            "❌ Сообщение не может быть пустым."
        )

        return

    if (
        broadcast_task
        and not broadcast_task.done()
    ):

        await state.finish()

        await message.answer(
            "⚠️ Рассылка уже выполняется."
        )

        return

    connection = get_db()

    cursor = connection.execute(
        """
        INSERT INTO broadcasts
        (
            text,
            created_at
        )
        VALUES (?, ?)
        """,
        (
            message.text,
            utc_now().isoformat()
        )
    )

    broadcast_id = cursor.lastrowid

    connection.commit()
    connection.close()

    await state.finish()

    broadcast_task = asyncio.create_task(
        run_broadcast(
            broadcast_id,
            message.text
        )
    )

    await message.answer(
        "🚀 <b>Рассылка запущена.</b>\n\n"
        f"ID рассылки: <code>{broadcast_id}</code>"
    )


# =========================================================
# RUN BROADCAST
# =========================================================

async def run_broadcast(
    broadcast_id: int,
    text: str
):

    global broadcast_running

    broadcast_running = True

    connection = get_db()

    users = connection.execute(
        """
        SELECT user_id
        FROM users
        """
    ).fetchall()

    connection.close()

    sent = 0
    failed = 0

    for user in users:

        if not broadcast_running:
            break

        try:

            await bot.send_message(
                user["user_id"],
                text
            )

            sent += 1

        except Exception as error:

            failed += 1

            logger.warning(
                "Не удалось отправить %s: %s",
                user["user_id"],
                error
            )

        # Небольшая пауза между сообщениями.
        await asyncio.sleep(0.2)

    connection = get_db()

    connection.execute(
        """
        UPDATE broadcasts
        SET sent=?,
            failed=?
        WHERE id=?
        """,
        (
            sent,
            failed,
            broadcast_id
        )
    )

    connection.commit()
    connection.close()

    broadcast_running = False

    logger.info(
        "Broadcast %s finished. Sent=%s Failed=%s",
        broadcast_id,
        sent,
        failed
    )


# =========================================================
# STOP BROADCAST
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "broadcast_stop"
)
async def broadcast_stop_handler(
    callback: types.CallbackQuery
):

    global broadcast_running

    if not has_valid_license(
        callback.from_user.id
    ):

        await callback.answer(
            "🔒 Нужна активная лицензия.",
            show_alert=True
        )

        return

    broadcast_running = False

    await callback.answer(
        "Рассылка остановлена."
    )

    await callback.message.edit_text(
        "⏹ <b>Рассылка остановлена.</b>",
        reply_markup=main_keyboard(
            callback.from_user.id
        )
    )


# =========================================================
# ADMIN
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "admin"
)
async def admin_handler(
    callback: types.CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(
        "🛠 <b>Админ-панель</b>",
        reply_markup=admin_keyboard()
    )


# =========================================================
# CREATE LICENSE
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "admin_create_key"
)
async def admin_create_key_handler(
    callback: types.CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    await callback.answer()

    await callback.message.edit_text(
        "🔑 <b>Создание лицензии</b>\n\n"
        "Введи срок действия в днях.\n\n"
        "Например:\n"
        "<code>30</code>"
    )

    await AdminStates.waiting_days.set()


@dp.message_handler(
    state=AdminStates.waiting_days
)
async def admin_create_key_days(
    message: types.Message,
    state: FSMContext
):

    if message.from_user.id != ADMIN_ID:

        await state.finish()

        return

    try:

        days = int(
            message.text.strip()
        )

        if days <= 0 or days > 3650:
            raise ValueError

    except ValueError:

        await message.answer(
            "❌ Введи число от 1 до 3650."
        )

        return

    key, expires = create_license(days)

    await state.finish()

    await message.answer(
        "✅ <b>Лицензия создана!</b>\n\n"
        f"🔑 <code>{key}</code>\n"
        f"⏳ Дней: <b>{days}</b>\n"
        f"📅 До: <code>"
        f"{expires.strftime('%d.%m.%Y %H:%M UTC')}"
        f"</code>",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN STATISTICS
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "admin_stats"
)
async def admin_stats_handler(
    callback: types.CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    connection = get_db()

    users = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    licenses = connection.execute(
        """
        SELECT COUNT(*)
        FROM licenses
        WHERE active=1
        """
    ).fetchone()[0]

    broadcasts = connection.execute(
        "SELECT COUNT(*) FROM broadcasts"
    ).fetchone()[0]

    connection.close()

    await callback.answer()

    await callback.message.edit_text(
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"🔑 Активных лицензий: <b>{licenses}</b>\n"
        f"📢 Всего рассылок: <b>{broadcasts}</b>",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN BROADCAST HISTORY
# =========================================================

@dp.callback_query_handler(
    lambda callback: callback.data == "admin_broadcasts"
)
async def admin_broadcasts_handler(
    callback: types.CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:

        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )

        return

    connection = get_db()

    broadcasts = connection.execute(
        """
        SELECT *
        FROM broadcasts
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()

    connection.close()

    await callback.answer()

    if not broadcasts:

        text = "📢 <b>Рассылок пока нет.</b>"

    else:

        lines = [
            "📢 <b>Последние рассылки</b>\n"
        ]

        for item in broadcasts:

            lines.append(
                f"#{item['id']} — "
                f"отправлено: {item['sent']}, "
                f"ошибок: {item['failed']}"
            )

        text = "\n".join(lines)

    await callback.message.edit_text(
        text,
        reply_markup=admin_keyboard()
    )


# =========================================================
# UNKNOWN MESSAGE
# =========================================================

@dp.message_handler()
async def unknown_handler(
    message: types.Message
):

    save_user(message.from_user)

    await message.answer(
        "Используй /start, чтобы открыть меню."
    )


# =========================================================
# STARTUP / SHUTDOWN
# =========================================================

async def on_startup(_):

    init_database()

    me = await bot.get_me()

    logger.info(
        "🚀 Bot started: @%s | ID=%s",
        me.username,
        me.id
    )


async def on_shutdown(_):

    global broadcast_running

    broadcast_running = False

    logger.info(
        "🛑 Bot stopped"
    )


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
