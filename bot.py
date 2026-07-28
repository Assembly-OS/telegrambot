import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

# Подхватываем .env, если установлен python-dotenv.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- НАСТРОЙКИ ---
DB_PATH = "assambleya_ai_os.db"

# Токен бота из BotFather. ХРАНИТЕ его в .env, НЕ в коде!
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Публичный HTTPS-адрес мини-приложения (туннель или домен). Пусто => кнопок приложения нет.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
MENU_BUTTON_TEXT = os.getenv("MENU_BUTTON_TEXT", "Platforma")  # Надпись на кнопке-меню

# Локальный адрес встроенного веб-сервера Mini App (его наружу выставляет туннель).
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

# --- КОНТРОЛЬ ДОСТУПА ---
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# --- СТАТУСЫ ЗАДАЧ (в БД храним коды, текст — в MESSAGES) ---
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"

# --- СПРАВОЧНИКИ (используются и в чате, и в мини-приложении) ---
DIRECTIONS = [
    {"code": "GR", "name": "Government Relations"},
    {"code": "FR", "name": "Foreign Relations"},
    {"code": "BR", "name": "Business Relations"},
    {"code": "PR", "name": "Public Relations"},
]
PROJECTS = [
    {"id": 1, "name": "Central Asian Smart City"},
    {"id": 2, "name": "Konglomerat"},
    {"id": 3, "name": "Sokin Osmon"},
]
DIRECTION_CODES = {d["code"] for d in DIRECTIONS}
PROJECT_IDS = {p["id"] for p in PROJECTS}

logging.basicConfig(level=logging.INFO)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан. Укажите его в .env или в переменных окружения.")

if WEBAPP_URL and not WEBAPP_URL.startswith("https://"):
    logging.warning("WEBAPP_URL должен начинаться с https:// — кнопка приложения отключена")
    WEBAPP_URL = ""

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


def esc(value) -> str:
    """Экранирует пользовательский текст для HTML-разметки сообщений."""
    return html.escape(str(value), quote=False)


# --- МНОГОЯЗЫЧНЫЙ ТЕКСТ (разметка — HTML) ---
MESSAGES = {
    'ru': {
        'welcome': "🏛 <b>ASSAMBLEYA AI OS — Система управления</b>\n\nПриветствуем, <b>{name}</b>!\nВаша роль: <b>{role}</b>",
        'role_admin': "👑 Главный (Администратор)",
        'role_worker': "👷 Работник / Исполнитель",
        'btn_add_task': "➕ Дать задание",
        'btn_my_tasks': "📋 Мои задания",
        'btn_all_tasks': "📊 Все поручения",
        'btn_profile': "💼 Мой профиль / Должность",
        'btn_admin_panel': "⚙️ Админ-панель",
        'btn_change_lang': "🌐 Сменить язык",
        'btn_open_app': "📱 Открыть приложение",
        'choose_lang': "🌐 <b>Выберите язык / Tilni tanlang / Choose language:</b>",
        'enter_title': "📝 Введите <b>суть / описание задания</b>:",
        'choose_dir': "🏢 Выберите <b>направление (GR/FR/BR/PR)</b>:",
        'choose_proj': "🎯 Выберите <b>проект</b>:",
        'no_proj': "🚫 Без проекта",
        'enter_assignee': "👤 Напишите <b>ФИО или должность работника</b>:",
        'choose_assignee': "👤 Выберите <b>ответственного</b> (из тех, кто запускал бота):",
        'no_users': "⚠️ Пока никто не запускал бота, кроме вас. Попросите сотрудников открыть бота и нажать /start.",
        'enter_deadline': "📅 Укажите <b>срок исполнения (Дедлайн)</b>:",
        'task_created': "✅ <b>Задание #{id} успешно создано!</b>\n\n📌 <b>Суть:</b> {title}\n🏛 <b>Направление:</b> {dir}\n👤 <b>Ответственный:</b> {assignee}\n⏰ <b>Срок:</b> {deadline}",
        'task_assigned': "🔔 <b>Вам назначено новое задание #{id}!</b>\n\n📌 <b>Суть:</b> {title}\n🏛 <b>Направление:</b> {dir}\n⏰ <b>Срок:</b> {deadline}",
        'btn_done': "✅ Отметить как выполненное",
        'no_tasks': "🎉 Нет активных заданий!",
        'task_done_msg': "✅ Задание выполнено!",
        'admin_profile': "👑 <b>АДМИН-ПАНЕЛЬ</b>\n\n👤 <b>Пользователь:</b> {name}\n🏛 <b>Должность:</b> Главный руководитель / Раис\n🔑 <b>Права:</b> Полный доступ ко всем функциям.",
        'worker_profile': "💼 <b>ПРОФИЛЬ СОТРУДНИКА</b>\n\n👤 <b>Пользователь:</b> {name}\n🏢 <b>Должность:</b> Исполнитель / Работник\n🔑 <b>Права:</b> Просмотр и выполнение заданий.",
        'no_access': "⛔ У вас нет прав для выполнения этой команды.",
        'webapp_error': "⚠️ Не удалось обработать данные из приложения. Попробуйте ещё раз.",
        'status_in_progress': "В процессе",
        'status_done': "Выполнено",
        'admin_granted': "👑 <b>Права Главного (Администратора) получены!</b>",
        'admin_login_disabled': "⛔ Вход администратора отключён. Задайте ADMIN_SECRET в .env.",
        'admin_login_wrong': "⛔ Неверный пароль администратора."
    },
    'uz': {
        'welcome': "🏛 <b>ASSAMBLEYA AI OS — Boshqaruv tizimi</b>\n\nXush kelibsiz, <b>{name}</b>!\nSizning rolingiz: <b>{role}</b>",
        'role_admin': "👑 Boshliq (Admin)",
        'role_worker': "👷 Xodim / Bajaruvchi",
        'btn_add_task': "➕ Topshiriq berish",
        'btn_my_tasks': "📋 Mening topshiriqlarim",
        'btn_all_tasks': "📊 Barcha topshiriqlar",
        'btn_profile': "💼 Mening lavozimim",
        'btn_admin_panel': "⚙️ Admin panel",
        'btn_change_lang': "🌐 Tilni o'zgartirish",
        'btn_open_app': "📱 Ilovani ochish",
        'choose_lang': "🌐 <b>Tilni tanlang / Выберите язык / Choose language:</b>",
        'enter_title': "📝 Topshiriq <b>mazmunini</b> kiriting:",
        'choose_dir': "🏢 <b>Yo'nalishni tanlang (GR/FR/BR/PR):</b>",
        'choose_proj': "🎯 <b>Loyiha tanlang:</b>",
        'no_proj': "🚫 Loyihasiz",
        'enter_assignee': "👤 Xodimning <b>F.I.SH yoki lavozimini</b> kiriting:",
        'choose_assignee': "👤 <b>Mas'ulni</b> tanlang (botni ishga tushirganlardan):",
        'no_users': "⚠️ Hozircha sizdan boshqa hech kim botni ishga tushirmagan. Xodimlardan botni ochib /start bosishlarini so'rang.",
        'enter_deadline': "📅 Topshiriq <b>muddatini (dedlayn)</b> kiriting:",
        'task_created': "✅ <b>Topshiriq #{id} muvaffaqiyatli yaratildi!</b>\n\n📌 <b>Mazmuni:</b> {title}\n🏛 <b>Yo'nalish:</b> {dir}\n👤 <b>Mas'ul:</b> {assignee}\n⏰ <b>Muddat:</b> {deadline}",
        'task_assigned': "🔔 <b>Sizga yangi topshiriq #{id} berildi!</b>\n\n📌 <b>Mazmuni:</b> {title}\n🏛 <b>Yo'nalish:</b> {dir}\n⏰ <b>Muddat:</b> {deadline}",
        'btn_done': "✅ Bajarildi deb belgilash",
        'no_tasks': "🎉 Faol topshiriqlar yo'q!",
        'task_done_msg': "✅ Topshiriq bajarildi!",
        'admin_profile': "👑 <b>ADMIN PANEL</b>\n\n👤 <b>Foydalanuvchi:</b> {name}\n🏛 <b>Lavozim:</b> Bosh rahbar / Rais\n🔑 <b>Huquqlar:</b> To'liq kirish huquqi.",
        'worker_profile': "💼 <b>XODIM PROFILI</b>\n\n👤 <b>Foydalanuvchi:</b> {name}\n🏢 <b>Lavozim:</b> Ijrochi / Xodim\n🔑 <b>Huquqlar:</b> Topshiriqlarni ko'rish va bajarish.",
        'no_access': "⛔ Ushbu buyruqni bajarish uchun sizda huquq yo'q.",
        'webapp_error': "⚠️ Ilovadan kelgan ma'lumotni qayta ishlab bo'lmadi. Qayta urinib ko'ring.",
        'status_in_progress': "Jarayonda",
        'status_done': "Bajarildi",
        'admin_granted': "👑 <b>Boshliq (Admin) huquqlari berildi!</b>",
        'admin_login_disabled': "⛔ Administrator kirishi o'chirilgan. .env faylida ADMIN_SECRET ni belgilang.",
        'admin_login_wrong': "⛔ Administrator paroli noto'g'ri."
    },
    'en': {
        'welcome': "🏛 <b>ASSAMBLEYA AI OS — Management System</b>\n\nWelcome, <b>{name}</b>!\nYour role: <b>{role}</b>",
        'role_admin': "👑 Chief (Admin)",
        'role_worker': "👷 Employee / Worker",
        'btn_add_task': "➕ Assign Task",
        'btn_my_tasks': "📋 My Tasks",
        'btn_all_tasks': "📊 All Tasks",
        'btn_profile': "💼 My Position / Profile",
        'btn_admin_panel': "⚙️ Admin Panel",
        'btn_change_lang': "🌐 Change Language",
        'btn_open_app': "📱 Open App",
        'choose_lang': "🌐 <b>Select language / Tilni tanlang / Выберите язык:</b>",
        'enter_title': "📝 Enter <b>task description</b>:",
        'choose_dir': "🏢 Select <b>direction (GR/FR/BR/PR)</b>:",
        'choose_proj': "🎯 Select <b>project</b>:",
        'no_proj': "🚫 No project",
        'enter_assignee': "👤 Enter <b>employee name or title</b>:",
        'choose_assignee': "👤 Select the <b>assignee</b> (from users who started the bot):",
        'no_users': "⚠️ Nobody except you has started the bot yet. Ask employees to open the bot and press /start.",
        'enter_deadline': "📅 Enter <b>deadline</b>:",
        'task_created': "✅ <b>Task #{id} successfully created!</b>\n\n📌 <b>Title:</b> {title}\n🏛 <b>Direction:</b> {dir}\n👤 <b>Assignee:</b> {assignee}\n⏰ <b>Deadline:</b> {deadline}",
        'task_assigned': "🔔 <b>A new task #{id} was assigned to you!</b>\n\n📌 <b>Title:</b> {title}\n🏛 <b>Direction:</b> {dir}\n⏰ <b>Deadline:</b> {deadline}",
        'btn_done': "✅ Mark as Completed",
        'no_tasks': "🎉 No active tasks!",
        'task_done_msg': "✅ Task completed!",
        'admin_profile': "👑 <b>ADMIN PANEL</b>\n\n👤 <b>User:</b> {name}\n🏛 <b>Position:</b> Head / Leader\n🔑 <b>Permissions:</b> Full administrative access.",
        'worker_profile': "💼 <b>EMPLOYEE PROFILE</b>\n\n👤 <b>User:</b> {name}\n🏢 <b>Position:</b> Worker / Executive\n🔑 <b>Permissions:</b> View and complete tasks.",
        'no_access': "⛔ You do not have permission to execute this command.",
        'webapp_error': "⚠️ Could not process data from the app. Please try again.",
        'status_in_progress': "In progress",
        'status_done': "Done",
        'admin_granted': "👑 <b>Chief (Admin) rights granted!</b>",
        'admin_login_disabled': "⛔ Admin login is disabled. Set ADMIN_SECRET in .env.",
        'admin_login_wrong': "⛔ Wrong admin password."
    }
}


def status_label(t: dict, status: str) -> str:
    return t['status_done'] if status == STATUS_DONE else t['status_in_progress']


# --- БАЗА ДАННЫХ ---
@contextmanager
def db_conn():
    """Единая точка доступа к БД: коммит при успехе, закрытие всегда."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                role TEXT DEFAULT 'worker',
                lang TEXT DEFAULT 'ru'
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                direction_code TEXT,
                project_id INTEGER,
                assignee_name TEXT NOT NULL,
                assignee_id INTEGER,
                deadline TEXT NOT NULL,
                status TEXT DEFAULT 'in_progress',
                created_by INTEGER
            )
        ''')

        # --- Миграции для БД из прошлых версий ---
        cols = {row['name'] for row in conn.execute("PRAGMA table_info(tasks)")}
        if 'created_by' not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER")
        if 'assignee_id' not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN assignee_id INTEGER")

        conn.execute(
            "UPDATE tasks SET status = ? WHERE status IN ('Выполнено', 'Bajarildi', 'Done')",
            (STATUS_DONE,)
        )
        conn.execute(
            "UPDATE tasks SET status = ? WHERE status NOT IN (?, ?)",
            (STATUS_IN_PROGRESS, STATUS_IN_PROGRESS, STATUS_DONE)
        )


def ensure_user(user_id: int, full_name: str):
    """Создаёт пользователя (или обновляет имя). Первый в системе / из ADMIN_IDS — админ."""
    forced_admin = user_id in ADMIN_IDS
    with db_conn() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()['c']
        initial_role = 'admin' if (count == 0 or forced_admin) else 'worker'
        conn.execute(
            "INSERT INTO users (user_id, full_name, role, lang) VALUES (?, ?, ?, 'ru') "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name",
            (user_id, full_name, initial_role)
        )
        if forced_admin:
            conn.execute("UPDATE users SET role = 'admin' WHERE user_id = ?", (user_id,))


def get_user(user_id: int) -> dict:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT role, lang, full_name FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row:
        return {'role': row['role'], 'lang': row['lang'], 'full_name': row['full_name'] or ''}
    return {'role': 'worker', 'lang': 'ru', 'full_name': ''}


def get_worker(user_id: int):
    with db_conn() as conn:
        return conn.execute(
            "SELECT user_id, full_name, role FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def get_all_users():
    with db_conn() as conn:
        return conn.execute(
            "SELECT user_id, full_name, role FROM users ORDER BY full_name COLLATE NOCASE"
        ).fetchall()


def set_user_lang(user_id: int, lang: str):
    with db_conn() as conn:
        conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))


def set_user_role(user_id: int, role: str):
    with db_conn() as conn:
        conn.execute("UPDATE users SET role = ? WHERE user_id = ?", (role, user_id))


def save_task(title, direction_code, project_id, assignee_name, deadline,
              created_by, assignee_id=None) -> int:
    with db_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, direction_code, project_id, assignee_name, assignee_id, deadline, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (title, direction_code, project_id, assignee_name, assignee_id, deadline, STATUS_IN_PROGRESS, created_by)
        )
        return cursor.lastrowid


_TASK_COLS = "id, title, direction_code, project_id, assignee_id, assignee_name, deadline, status"


def get_all_tasks():
    with db_conn() as conn:
        return conn.execute(f"SELECT {_TASK_COLS} FROM tasks ORDER BY id DESC").fetchall()


def get_task(task_id: int):
    with db_conn() as conn:
        return conn.execute(f"SELECT {_TASK_COLS} FROM tasks WHERE id = ?", (task_id,)).fetchone()


def get_my_tasks(user_id: int, full_name: str, role: str):
    """Активные задания «для меня» (используется в чате)."""
    with db_conn() as conn:
        if role == 'admin':
            return conn.execute(
                f"SELECT {_TASK_COLS} FROM tasks WHERE created_by = ? AND status = ? ORDER BY id DESC",
                (user_id, STATUS_IN_PROGRESS)
            ).fetchall()
        return conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks "
            "WHERE status = ? AND (assignee_id = ? OR "
            "(assignee_id IS NULL AND LOWER(TRIM(assignee_name)) = LOWER(TRIM(?)))) ORDER BY id DESC",
            (STATUS_IN_PROGRESS, user_id, full_name)
        ).fetchall()


def get_worker_tasks(user_id: int, full_name: str):
    """Все задания работника (для мини-приложения): активные — сверху."""
    with db_conn() as conn:
        return conn.execute(
            f"SELECT {_TASK_COLS} FROM tasks "
            "WHERE assignee_id = ? OR (assignee_id IS NULL AND LOWER(TRIM(assignee_name)) = LOWER(TRIM(?))) "
            "ORDER BY (status = ?) ASC, id DESC",
            (user_id, full_name, STATUS_DONE)
        ).fetchall()


def set_task_done(task_id: int):
    with db_conn() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (STATUS_DONE, task_id))


def delete_task(task_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


class CreateTaskFSM(StatesGroup):
    title = State()
    direction = State()
    project = State()
    assignee = State()
    deadline = State()


# --- КЛАВИАТУРЫ ---
def lang_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="setlang_uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru"),
            InlineKeyboardButton(text="🇬🇧 English", callback_data="setlang_en")
        ]
    ])


def assignee_inline_kb():
    """Inline-клавиатура со списком всех, кто уже запускал бота (для выбора ответственного)."""
    rows = []
    for u in get_all_users():
        name = u['full_name'] or str(u['user_id'])
        tag = "👑" if u['role'] == 'admin' else "👷"
        rows.append([InlineKeyboardButton(text=f"{tag} {name}", callback_data=f"asg_{u['user_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard(lang: str, is_admin: bool):
    t = MESSAGES[lang]
    keyboard = []
    if is_admin:
        keyboard.append([KeyboardButton(text=t['btn_add_task']), KeyboardButton(text=t['btn_my_tasks'])])
        keyboard.append([KeyboardButton(text=t['btn_all_tasks']), KeyboardButton(text=t['btn_admin_panel'])])
    else:
        keyboard.append([KeyboardButton(text=t['btn_my_tasks']), KeyboardButton(text=t['btn_profile'])])

    last_row = []
    if WEBAPP_URL:
        sep = '&' if '?' in WEBAPP_URL else '?'
        last_row.append(KeyboardButton(text=t['btn_open_app'], web_app=WebAppInfo(url=f"{WEBAPP_URL}{sep}lang={lang}")))
    last_row.append(KeyboardButton(text=t['btn_change_lang']))
    keyboard.append(last_row)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# --- ОБРАБОТКА СТАРТА И СМЕНЫ ЯЗЫКА ---

@router.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.full_name)
    await message.answer(MESSAGES['ru']['choose_lang'], reply_markup=lang_inline_kb(), parse_mode="HTML")


@router.message(Command("admin_login"))
async def make_admin(message: Message, command: CommandObject):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    t = MESSAGES[user_data['lang']]

    try:
        await message.delete()
    except Exception:
        pass

    if not ADMIN_SECRET:
        await message.answer(t['admin_login_disabled'])
        return
    if (command.args or "").strip() != ADMIN_SECRET:
        await message.answer(t['admin_login_wrong'])
        return

    set_user_role(user_id, 'admin')
    await message.answer(t['admin_granted'], reply_markup=main_keyboard(user_data['lang'], True), parse_mode="HTML")


@router.callback_query(F.data.startswith("setlang_"))
async def select_language(callback):
    user_id = callback.from_user.id
    lang = callback.data.split("_")[1]
    set_user_lang(user_id, lang)

    user_data = get_user(user_id)
    t = MESSAGES[lang]
    is_admin = user_data['role'] == 'admin'
    role_text = t['role_admin'] if is_admin else t['role_worker']

    await callback.message.delete()
    await callback.message.answer(
        t['welcome'].format(name=esc(callback.from_user.full_name), role=role_text),
        reply_markup=main_keyboard(lang, is_admin),
        parse_mode="HTML"
    )


@router.message(F.text.in_(["🌐 Сменить язык", "🌐 Tilni o'zgartirish", "🌐 Change Language"]))
async def change_lang_btn(message: Message):
    await message.answer(MESSAGES['ru']['choose_lang'], reply_markup=lang_inline_kb(), parse_mode="HTML")


# --- ПРОФИЛЬ И АДМИН ПАНЕЛЬ ---

@router.message(F.text.in_(["⚙️ Админ-панель", "⚙️ Admin panel", "💼 Мой профиль / Должность", "💼 Mening lavozimim", "💼 My Position / Profile"]))
async def show_profile(message: Message):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    name = esc(message.from_user.full_name)
    text = t['admin_profile'].format(name=name) if user_data['role'] == 'admin' else t['worker_profile'].format(name=name)
    await message.answer(text, parse_mode="HTML")


# --- СОЗДАНИЕ ЗАДАНИЯ В ЧАТЕ ---

@router.message(F.text.in_(["➕ Дать задание", "➕ Topshiriq berish", "➕ Assign Task"]))
async def start_create_task(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    if user_data['role'] != 'admin':
        await message.answer(t['no_access'])
        return

    await state.set_state(CreateTaskFSM.title)
    await message.answer(t['enter_title'], parse_mode="HTML")


@router.message(CreateTaskFSM.title)
async def process_title(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    await state.update_data(title=message.text)
    await state.set_state(CreateTaskFSM.direction)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{d['code']} — {d['name']}", callback_data=f"dir_{d['code']}")]
        for d in DIRECTIONS
    ])
    await message.answer(t['choose_dir'], reply_markup=kb, parse_mode="HTML")


@router.callback_query(CreateTaskFSM.direction, F.data.startswith("dir_"))
async def process_direction(callback, state: FSMContext):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    await state.update_data(direction=callback.data.split("_")[1])
    await state.set_state(CreateTaskFSM.project)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p['name'], callback_data=f"proj_{p['id']}")] for p in PROJECTS
    ] + [[InlineKeyboardButton(text=t['no_proj'], callback_data="proj_none")]])
    await callback.message.edit_text(t['choose_proj'], reply_markup=kb, parse_mode="HTML")


@router.callback_query(CreateTaskFSM.project, F.data.startswith("proj_"))
async def process_project(callback, state: FSMContext):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    proj_val = callback.data.split("_")[1]
    await state.update_data(project_id=None if proj_val == "none" else int(proj_val))
    await state.set_state(CreateTaskFSM.assignee)
    await callback.message.edit_text(t['choose_assignee'], reply_markup=assignee_inline_kb(), parse_mode="HTML")


@router.callback_query(CreateTaskFSM.assignee, F.data.startswith("asg_"))
async def process_assignee_pick(callback, state: FSMContext):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    assignee_id = int(callback.data.split("_")[1])
    worker = get_worker(assignee_id)
    assignee_name = (worker['full_name'] if worker else None) or str(assignee_id)

    await state.update_data(assignee_id=assignee_id, assignee_name=assignee_name)
    await state.set_state(CreateTaskFSM.deadline)
    await callback.message.edit_text(t['enter_deadline'], parse_mode="HTML")


@router.message(CreateTaskFSM.assignee)
async def process_assignee_hint(message: Message):
    # Если админ пишет текстом вместо выбора — снова показываем список кнопок.
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]
    await message.answer(t['choose_assignee'], reply_markup=assignee_inline_kb(), parse_mode="HTML")


@router.message(CreateTaskFSM.deadline)
async def process_deadline(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    await state.update_data(deadline=message.text)
    data = await state.get_data()
    assignee_id = data.get('assignee_id')

    task_id = save_task(
        data['title'], data['direction'], data['project_id'],
        data['assignee_name'], data['deadline'],
        created_by=message.from_user.id, assignee_id=assignee_id
    )

    await state.clear()

    # Уведомляем назначенного работника в Telegram.
    if assignee_id and assignee_id != message.from_user.id:
        try:
            tw = MESSAGES.get(get_user(assignee_id)['lang'], MESSAGES['ru'])
            await bot.send_message(
                assignee_id,
                tw['task_assigned'].format(id=task_id, title=esc(data['title']),
                                           dir=esc(data['direction']), deadline=esc(data['deadline'])),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.info("Не удалось уведомить работника %s: %s", assignee_id, e)

    await message.answer(
        t['task_created'].format(
            id=task_id, title=esc(data['title']), dir=esc(data['direction']),
            assignee=esc(data['assignee_name']), deadline=esc(data['deadline'])
        ),
        reply_markup=main_keyboard(user_data['lang'], user_data['role'] == 'admin'),
        parse_mode="HTML"
    )


# --- ПРОСМОТР И ВЫПОЛНЕНИЕ ЗАДАЧ В ЧАТЕ ---

@router.message(F.text.in_(["📊 Все поручения", "📊 Barcha topshiriqlar", "📊 All Tasks"]))
async def show_all_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    if user_data['role'] != 'admin':
        await message.answer(t['no_access'])
        return

    tasks = get_all_tasks()
    if not tasks:
        await message.answer(t['no_tasks'])
        return

    res = f"📋 <b>{esc(t['btn_all_tasks'])}:</b>\n\n"
    for row in tasks:
        res += (
            f"🔹 <b>#{row['id']} {esc(row['title'])}</b>\n"
            f"• Direction: {esc(row['direction_code'] or '—')} | Assignee: {esc(row['assignee_name'])}\n"
            f"• Deadline: <code>{esc(row['deadline'])}</code> | "
            f"Status: <code>{esc(status_label(t, row['status']))}</code>\n\n"
        )
    await message.answer(res, parse_mode="HTML")


@router.message(F.text.in_(["📋 Мои задания", "📋 Mening topshiriqlarim", "📋 My Tasks"]))
async def show_my_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    full_name = user_data['full_name'] or message.from_user.full_name
    tasks = get_my_tasks(message.from_user.id, full_name, user_data['role'])

    if not tasks:
        await message.answer(t['no_tasks'])
        return

    for row in tasks:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t['btn_done'], callback_data=f"done_{row['id']}")]
        ])
        await message.answer(
            f"📌 <b>#{row['id']}: {esc(row['title'])}</b>\n"
            f"🏛 Direction: {esc(row['direction_code'] or '—')}\n"
            f"⏰ Deadline: <code>{esc(row['deadline'])}</code> | "
            f"Status: <code>{esc(status_label(t, row['status']))}</code>",
            reply_markup=kb,
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("done_"))
async def mark_done(callback):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    try:
        task_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return

    set_task_done(task_id)
    await callback.answer(t['task_done_msg'])
    base = esc(callback.message.text or "")
    await callback.message.edit_text(f"{base}\n\n✅ <b>STATUS: DONE</b>", parse_mode="HTML")


# --- ДАННЫЕ ИЗ МИНИ-ПРИЛОЖЕНИЯ ЧЕРЕЗ sendData (запасной путь) ---

def parse_webapp_payload(raw: str) -> dict:
    payload = json.loads(raw)
    if payload.get("action") != "create_task":
        raise ValueError("unknown action")

    title = str(payload.get("title", "")).strip()
    direction = str(payload.get("direction", "")).strip().upper()
    assignee = str(payload.get("assignee", "")).strip()
    deadline = str(payload.get("deadline", "")).strip()

    project_raw = payload.get("project_id")
    project_id = None if project_raw in (None, "", "none") else int(project_raw)

    if not title or not assignee or not deadline or direction not in DIRECTION_CODES:
        raise ValueError("missing fields")

    return {"title": title, "direction": direction, "project_id": project_id,
            "assignee": assignee, "deadline": deadline}


@router.message(F.web_app_data)
async def handle_webapp_data(message: Message):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    if user_data['role'] != 'admin':
        await message.answer(t['no_access'])
        return

    try:
        task = parse_webapp_payload(message.web_app_data.data)
    except (ValueError, TypeError, json.JSONDecodeError):
        await message.answer(t['webapp_error'])
        return

    task_id = save_task(
        task['title'], task['direction'], task['project_id'],
        task['assignee'], task['deadline'], created_by=message.from_user.id
    )
    await message.answer(
        t['task_created'].format(
            id=task_id, title=esc(task['title']), dir=esc(task['direction']),
            assignee=esc(task['assignee']), deadline=esc(task['deadline'])
        ),
        reply_markup=main_keyboard(user_data['lang'], user_data['role'] == 'admin'),
        parse_mode="HTML"
    )


# =========================================================================
# MINI APP (WebApp) — встроенный веб-сервер + JSON API
# =========================================================================

def validate_init_data(init_data: str, max_age_seconds: int = 24 * 3600):
    """
    Проверяет подпись Telegram.WebApp.initData (HMAC-SHA256 на основе токена).
    Возвращает словарь пользователя при валидной подписи, иначе None.
    Клиент НЕ может подделать, кто он — доверяем только данным отсюда.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    provided_hash = parsed.pop("hash", None)
    if not provided_hash:
        return None

    data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, provided_hash):
        return None

    auth_date = parsed.get("auth_date", "")
    if auth_date.isdigit() and (time.time() - int(auth_date)) > max_age_seconds:
        return None

    try:
        user = json.loads(parsed.get("user", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(user, dict) or "id" not in user:
        return None
    return user


def _tg_full_name(user: dict) -> str:
    parts = [user.get("first_name"), user.get("last_name")]
    return " ".join(p for p in parts if p) or str(user.get("id"))


def _pick_lang(requested, user, udata) -> str:
    for candidate in (requested, user.get("language_code"), udata.get("lang")):
        if candidate in MESSAGES:
            return candidate
    return "ru"


def task_to_dict(row, lang: str) -> dict:
    t = MESSAGES[lang]
    return {
        "id": row["id"],
        "title": row["title"],
        "direction_code": row["direction_code"],
        "project_id": row["project_id"],
        "assignee_id": row["assignee_id"],
        "assignee_name": row["assignee_name"],
        "deadline": row["deadline"],
        "status": row["status"],
        "status_label": status_label(t, row["status"]),
        "is_done": row["status"] == STATUS_DONE,
    }


def _authed_user(request):
    return validate_init_data(request.headers.get("X-Init-Data", ""))


async def wa_index(request):
    # no-store — чтобы Telegram не показывал устаревшую версию приложения после правок.
    return web.FileResponse(
        os.path.join(WEBAPP_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
    )


async def wa_bootstrap(request):
    tg_user = _authed_user(request)
    if not tg_user:
        return web.json_response({"error": "unauthorized"}, status=401)

    uid = int(tg_user["id"])
    ensure_user(uid, _tg_full_name(tg_user))
    udata = get_user(uid)
    lang = _pick_lang(request.rel_url.query.get("lang"), tg_user, udata)
    is_admin = udata["role"] == "admin"

    if is_admin:
        tasks = [task_to_dict(r, lang) for r in get_all_tasks()]
        users = [
            {"id": r["user_id"], "name": r["full_name"] or str(r["user_id"]), "role": r["role"]}
            for r in get_all_users()
        ]
    else:
        tasks = [task_to_dict(r, lang) for r in get_worker_tasks(uid, udata["full_name"])]
        users = []

    return web.json_response({
        "me": {"id": uid, "name": udata["full_name"] or _tg_full_name(tg_user),
               "role": udata["role"], "lang": lang},
        "tasks": tasks,
        "users": users,
        "directions": DIRECTIONS,
        "projects": PROJECTS,
    })


async def wa_create_task(request):
    tg_user = _authed_user(request)
    if not tg_user:
        return web.json_response({"error": "unauthorized"}, status=401)

    uid = int(tg_user["id"])
    udata = get_user(uid)
    if udata["role"] != "admin":
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad_json"}, status=400)

    title = str(body.get("title", "")).strip()
    direction = str(body.get("direction", "")).strip().upper()
    deadline = str(body.get("deadline", "")).strip()

    project_raw = body.get("project_id")
    project_id = None if project_raw in (None, "", "none") else int(project_raw)

    if not title or not deadline or direction not in DIRECTION_CODES:
        return web.json_response({"error": "invalid_fields"}, status=400)
    if project_id is not None and project_id not in PROJECT_IDS:
        return web.json_response({"error": "invalid_project"}, status=400)

    try:
        assignee_id = int(body.get("assignee_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid_assignee"}, status=400)

    worker = get_worker(assignee_id)
    if not worker:
        return web.json_response({"error": "unknown_assignee"}, status=400)
    assignee_name = worker["full_name"] or str(assignee_id)

    task_id = save_task(title, direction, project_id, assignee_name, deadline,
                        created_by=uid, assignee_id=assignee_id)

    # Уведомляем работника в Telegram (если он уже открывал бота).
    try:
        wl = get_user(assignee_id)["lang"]
        tw = MESSAGES.get(wl, MESSAGES["ru"])
        await bot.send_message(
            assignee_id,
            tw["task_assigned"].format(id=task_id, title=esc(title), dir=esc(direction), deadline=esc(deadline)),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.info("Не удалось уведомить работника %s: %s", assignee_id, e)

    return web.json_response({"task": task_to_dict(get_task(task_id), udata["lang"])})


async def wa_mark_done(request):
    tg_user = _authed_user(request)
    if not tg_user:
        return web.json_response({"error": "unauthorized"}, status=401)

    uid = int(tg_user["id"])
    udata = get_user(uid)
    try:
        task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad_id"}, status=400)

    row = get_task(task_id)
    if not row:
        return web.json_response({"error": "not_found"}, status=404)

    name_match = (row["assignee_id"] is None and
                  (row["assignee_name"] or "").strip().lower() == (udata["full_name"] or "").strip().lower())
    allowed = udata["role"] == "admin" or row["assignee_id"] == uid or name_match
    if not allowed:
        return web.json_response({"error": "forbidden"}, status=403)

    set_task_done(task_id)
    return web.json_response({"ok": True})


async def wa_delete_task(request):
    tg_user = _authed_user(request)
    if not tg_user:
        return web.json_response({"error": "unauthorized"}, status=401)

    uid = int(tg_user["id"])
    udata = get_user(uid)
    if udata["role"] != "admin":
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad_id"}, status=400)

    if not get_task(task_id):
        return web.json_response({"error": "not_found"}, status=404)

    delete_task(task_id)
    return web.json_response({"ok": True})


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", wa_index)
    app.router.add_get("/api/bootstrap", wa_bootstrap)
    app.router.add_post("/api/tasks", wa_create_task)
    app.router.add_post("/api/tasks/{task_id}/done", wa_mark_done)
    app.router.add_delete("/api/tasks/{task_id}", wa_delete_task)
    return app


# --- ЗАПУСК ---

async def main():
    init_db()

    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text=MENU_BUTTON_TEXT, web_app=WebAppInfo(url=WEBAPP_URL))
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    runner = web.AppRunner(build_web_app())
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()
    logging.info("Mini App сервер: http://%s:%s | публичный URL: %s",
                 WEBAPP_HOST, WEBAPP_PORT, WEBAPP_URL or "— (не задан)")

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
