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

import aiohttp
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
# Общая база с платформой Assambleya OS (Next.js). Бот и платформа работают с
# ОДНИМИ пользователями и задачами. По умолчанию — ../data/assambleya.db.
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "assambleya.db")
DB_PATH = os.getenv("PLATFORM_DB", _DEFAULT_DB)

# Токен бота из BotFather. ХРАНИТЕ его в .env, НЕ в коде!
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Публичный HTTPS-адрес платформы — его открывает кнопка Mini App в Telegram.
PLATFORM_URL = os.getenv("PLATFORM_URL", os.getenv("WEBAPP_URL", "")).strip()
WEBAPP_URL = PLATFORM_URL  # совместимость со старыми обращениями
MENU_BUTTON_TEXT = os.getenv("MENU_BUTTON_TEXT", "Platforma")

# Локальный адрес платформы — по нему бот проверяет пароль (/link) и её же
# дёргает контроль-панель. Наружу НЕ выставляется.
PLATFORM_API = os.getenv("PLATFORM_API", "http://127.0.0.1:3000").rstrip("/")

# Встроенный веб-сервер бота: только вебхук /notify (платформа шлёт сюда
# уведомления при выдаче задачи), чтобы Telegram-пуш работал и из веб-интерфейса.
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "127.0.0.1")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))
NOTIFY_SECRET = os.getenv("BOT_NOTIFY_SECRET", "").strip()

# --- КОНТРОЛЬ ДОСТУПА ---
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# --- СТАТУСЫ ЗАДАЧ ПЛАТФОРМЫ ---
STATUS_NEW = "YANGI"          # выдана, ждёт принятия
STATUS_SUBMITTED = "TEKSHIRUVDA"  # исполнитель сдал на проверку
STATUS_DONE = "BAJARILDI"     # принято автором
# «Активные» для исполнителя — всё, что ещё не закрыто и не на проверке.
ACTIVE_STATUSES = ("YANGI", "QABUL_QILINDI", "BAJARILMOQDA", "QAYTARILDI")

# --- НАПРАВЛЕНИЯ = ОТДЕЛЫ ПЛАТФОРМЫ ---
DIRECTIONS = [
    {"code": "GR", "name": "Davlat aloqalari"},
    {"code": "FR", "name": "Xalqaro aloqalar"},
    {"code": "BR", "name": "Biznes aloqalari"},
    {"code": "PR", "name": "Jamoatchilik aloqalari"},
    {"code": "AI_LAB", "name": "AI Lab"},
]
DIRECTION_CODES = {d["code"] for d in DIRECTIONS}
# Проекты (loyihalar) грузятся из БД платформы функцией get_projects().

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
    # Показываем код статуса платформы (YANGI / TEKSHIRUVDA / BAJARILDI …).
    return status


# --- БАЗА ДАННЫХ ПЛАТФОРМЫ (общая с Assambleya OS) ---
# Роли-руководители платформы — им можно выдавать задачи через бота.
MANAGER_ROLES = {"RAIS", "BOLIM_RAHBARI", "AI_LAB", "UYUSHMA_RAISI", "LOYIHA_RAHBARI"}


@contextmanager
def db_conn():
    """Единая точка доступа к общей БД. WAL допускает параллельную работу с
    Next.js-платформой; busy_timeout ждёт, если писатель занят."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Таблицы создаёт сама платформа; боту нужен лишь столбец связи."""
    with db_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "telegram_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN telegram_id INTEGER")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _bot_lang(platform_lang: str) -> str:
    """Платформа хранит uz/uzc/ru/en; у бота словари ru/uz/en."""
    v = {"uzc": "uz"}.get(platform_lang, platform_lang)
    return v if v in MESSAGES else "ru"


def link_telegram(tg_id: int, login: str):
    """Привязывает Telegram-аккаунт к учётке платформы по логину (пароль уже
    проверен через login-API). Возвращает данные пользователя или None."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, full_name, role FROM users WHERE login = ? COLLATE NOCASE AND is_active = 1",
            (login,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE users SET telegram_id = NULL WHERE telegram_id = ?", (tg_id,))
        conn.execute("UPDATE users SET telegram_id = ? WHERE id = ?", (tg_id, row["id"]))
        return {"pid": row["id"], "full_name": row["full_name"], "role": row["role"]}


def get_telegram_id(pid: int):
    with db_conn() as conn:
        row = conn.execute("SELECT telegram_id FROM users WHERE id = ?", (pid,)).fetchone()
    return row["telegram_id"] if row and row["telegram_id"] is not None else None


def get_assignables(exclude_pid=None):
    """Кому можно поставить задачу. Раис задания не получает (он только их
    раздаёт), автор сам себе тоже не ставит — оба исключаются."""
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, full_name, role, department FROM users "
            "WHERE is_active = 1 AND role != 'RAIS' AND id IS NOT ? "
            "ORDER BY full_name COLLATE NOCASE",
            (exclude_pid,),
        ).fetchall()


def get_worker(pid: int):
    with db_conn() as conn:
        return conn.execute(
            "SELECT id, full_name, role, department, uyushma_id FROM users WHERE id = ?", (pid,)
        ).fetchone()


def get_projects():
    with db_conn() as conn:
        return conn.execute("SELECT id, name FROM loyihalar ORDER BY name COLLATE NOCASE").fetchall()


def get_user(tg_id: int) -> dict:
    """Пользователь платформы, привязанный к этому Telegram-аккаунту.
    linked=False, если /link ещё не сделан. role: 'admin' для руководителей."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, login, full_name, role, department, lang FROM users WHERE telegram_id = ?",
            (tg_id,),
        ).fetchone()
    if not row:
        return {"linked": False, "role": "worker", "lang": "ru", "full_name": "", "pid": None}
    is_admin = (row["role"] in MANAGER_ROLES) or (tg_id in ADMIN_IDS)
    return {
        "linked": True,
        "pid": row["id"],
        "login": row["login"],
        "full_name": row["full_name"] or "",
        "role": "admin" if is_admin else "worker",
        "platform_role": row["role"],
        "department": row["department"],
        "lang": _bot_lang(row["lang"]),
    }


def set_user_lang(tg_id: int, lang: str):
    with db_conn() as conn:
        conn.execute("UPDATE users SET lang = ? WHERE telegram_id = ?", (lang, tg_id))


_TASK_COLS = (
    "t.id, t.code, t.title, t.to_department AS direction_code, t.loyiha_id AS project_id, "
    "t.to_user_id AS assignee_id, t.priority, t.deadline, t.status, "
    "s.full_name AS assignee_name, f.full_name AS author_name"
)
_TASK_JOIN = "FROM tasks t JOIN users s ON s.id = t.to_user_id JOIN users f ON f.id = t.from_user_id"


def create_task(title, department, loyiha_id, from_pid, to_pid, deadline, priority="ORTA"):
    """Создаёт задачу платформы (status YANGI) + событие. Возвращает (task_id, code)."""
    with db_conn() as conn:
        assignee = conn.execute(
            "SELECT department, uyushma_id FROM users WHERE id = ?", (to_pid,)
        ).fetchone()
        dep = department or (assignee["department"] if assignee else None)
        n = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] + 1
        code = "T-%04d" % n
        stamp = _now()
        cur = conn.execute(
            "INSERT INTO tasks (code, title, description, from_user_id, to_user_id, to_department, "
            "priority, status, deadline, loyiha_id, uyushma_id, created_at) "
            "VALUES (?,?,?,?,?,?,?, 'YANGI', ?,?,?,?)",
            (code, title, None, from_pid, to_pid, dep, priority, (deadline or None),
             loyiha_id, (assignee["uyushma_id"] if assignee else None), stamp),
        )
        task_id = cur.lastrowid
        conn.execute(
            "INSERT INTO task_events (task_id, user_id, action, comment, created_at) "
            "VALUES (?,?, 'YARATILDI', 'bot', ?)",
            (task_id, from_pid, stamp),
        )
        return task_id, code


def get_all_tasks():
    with db_conn() as conn:
        return conn.execute(f"SELECT {_TASK_COLS} {_TASK_JOIN} ORDER BY t.id DESC LIMIT 50").fetchall()


def get_task(task_id: int):
    with db_conn() as conn:
        return conn.execute(f"SELECT {_TASK_COLS} {_TASK_JOIN} WHERE t.id = ?", (task_id,)).fetchone()


def get_worker_tasks(pid: int):
    """Активные задачи, назначенные исполнителю (для «Мои задания»)."""
    marks = ",".join("?" for _ in ACTIVE_STATUSES)
    with db_conn() as conn:
        return conn.execute(
            f"SELECT {_TASK_COLS} {_TASK_JOIN} WHERE t.to_user_id = ? AND t.status IN ({marks}) "
            "ORDER BY t.id DESC",
            (pid, *ACTIVE_STATUSES),
        ).fetchall()


def get_author_active(pid: int):
    """Активные задачи, которые выдал этот руководитель (для «Мои задания» админа)."""
    with db_conn() as conn:
        return conn.execute(
            f"SELECT {_TASK_COLS} {_TASK_JOIN} WHERE t.from_user_id = ? AND t.status != 'BAJARILDI' "
            "ORDER BY t.id DESC LIMIT 50",
            (pid,),
        ).fetchall()


def submit_task(task_id: int, pid: int):
    """Исполнитель сдаёт задачу на проверку (-> TEKSHIRUVDA) + событие."""
    stamp = _now()
    with db_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status = ?, submitted_at = ? WHERE id = ?",
            (STATUS_SUBMITTED, stamp, task_id),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, user_id, action, comment, created_at) "
            "VALUES (?,?, 'TOPSHIRILDI', 'bot', ?)",
            (task_id, pid, stamp),
        )


def delete_task(task_id: int):
    with db_conn() as conn:
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
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


def assignee_inline_kb(exclude_pid=None):
    """Inline-клавиатура со списком возможных исполнителей (без Раиса и автора)."""
    rows = []
    for u in get_assignables(exclude_pid):
        name = u['full_name'] or str(u['id'])
        tag = "👑" if u['role'] in MANAGER_ROLES else "👷"
        dep = f" · {u['department']}" if u['department'] else ""
        rows.append([InlineKeyboardButton(text=f"{tag} {name}{dep}", callback_data=f"asg_{u['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_keyboard(lang: str, is_admin: bool):
    t = MESSAGES[lang]
    keyboard = []
    if is_admin:
        keyboard.append([KeyboardButton(text=t['btn_add_task']), KeyboardButton(text=t['btn_my_tasks'])])
        keyboard.append([KeyboardButton(text=t['btn_all_tasks'])])
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

# --- ПРИВЯЗКА TELEGRAM ↔ УЧЁТКА ПЛАТФОРМЫ ---
LINK_PROMPT = (
    "👋 <b>Assambleya OS</b>\n\n"
    "Чтобы пользоваться ботом, привяжите Telegram к учётной записи платформы:\n"
    "<code>/link ЛОГИН ПАРОЛЬ</code>\n\n"
    "Например: <code>/link rais 12345678</code>\n"
    "Сообщение с паролем бот сразу удаляет."
)
LINK_USAGE = "Формат: <code>/link ЛОГИН ПАРОЛЬ</code>\nНапример: <code>/link rais 12345678</code>"
LINK_FAIL = "⛔ Неверный логин или пароль платформы. Ещё раз: <code>/link ЛОГИН ПАРОЛЬ</code>"
LINK_NOUSER = "⛔ Учётка не найдена или отключена."
LINK_OK = "✅ Telegram привязан к <b>{name}</b> (@{login}). Готово — пользуйтесь ботом."


async def _verify_platform_login(login: str, password: str) -> bool:
    """Пароль проверяет сама платформа (её login-API), а не бот."""
    url = f"{PLATFORM_API}/api/auth/login"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={"login": login, "password": password},
                              timeout=aiohttp.ClientTimeout(total=8)) as r:
                return r.status == 200
    except Exception as e:
        logging.warning("Платформа недоступна для проверки логина (%s): %s", url, e)
        return False


@router.message(CommandStart())
async def cmd_start(message: Message):
    u = get_user(message.from_user.id)
    if not u["linked"]:
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return
    t = MESSAGES[u["lang"]]
    role_text = t['role_admin'] if u['role'] == 'admin' else t['role_worker']
    await message.answer(
        t['welcome'].format(name=esc(u['full_name'] or message.from_user.full_name), role=role_text),
        reply_markup=main_keyboard(u['lang'], u['role'] == 'admin'), parse_mode="HTML")


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject):
    try:
        await message.delete()  # прячем пароль из чата
    except Exception:
        pass
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(LINK_USAGE, parse_mode="HTML")
        return
    login, password = parts[0].strip(), parts[1].strip()
    if not await _verify_platform_login(login, password):
        await message.answer(LINK_FAIL, parse_mode="HTML")
        return
    info = link_telegram(message.from_user.id, login)
    if not info:
        await message.answer(LINK_NOUSER, parse_mode="HTML")
        return
    u = get_user(message.from_user.id)
    await message.answer(
        LINK_OK.format(name=esc(info['full_name'] or login), login=esc(login)),
        reply_markup=main_keyboard(u['lang'], u['role'] == 'admin'), parse_mode="HTML")


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
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return
    t = MESSAGES[user_data['lang']]

    name = esc(user_data['full_name'] or message.from_user.full_name)
    text = t['admin_profile'].format(name=name) if user_data['role'] == 'admin' else t['worker_profile'].format(name=name)
    await message.answer(
        text + f"\n\n👤 <code>@{esc(user_data['login'])}</code> · {esc(user_data['platform_role'])}",
        parse_mode="HTML")


# --- СОЗДАНИЕ ЗАДАНИЯ В ЧАТЕ ---

@router.message(F.text.in_(["➕ Дать задание", "➕ Topshiriq berish", "➕ Assign Task"]))
async def start_create_task(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return
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
        [InlineKeyboardButton(text=p['name'], callback_data=f"proj_{p['id']}")] for p in get_projects()
    ] + [[InlineKeyboardButton(text=t['no_proj'], callback_data="proj_none")]])
    await callback.message.edit_text(t['choose_proj'], reply_markup=kb, parse_mode="HTML")


@router.callback_query(CreateTaskFSM.project, F.data.startswith("proj_"))
async def process_project(callback, state: FSMContext):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    proj_val = callback.data.split("_")[1]
    await state.update_data(project_id=None if proj_val == "none" else int(proj_val))
    await state.set_state(CreateTaskFSM.assignee)
    await callback.message.edit_text(
        t['choose_assignee'],
        reply_markup=assignee_inline_kb(user_data['pid']),
        parse_mode="HTML",
    )


@router.callback_query(CreateTaskFSM.assignee, F.data.startswith("asg_"))
async def process_assignee_pick(callback, state: FSMContext):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    assignee_id = int(callback.data.split("_")[1])
    worker = get_worker(assignee_id)

    # Защита от устаревшей клавиатуры: Раису задания не ставятся.
    if not worker or worker['role'] == 'RAIS':
        await callback.answer()
        await callback.message.edit_text(
            t['choose_assignee'],
            reply_markup=assignee_inline_kb(user_data['pid']),
            parse_mode="HTML",
        )
        return

    assignee_name = worker['full_name'] or str(assignee_id)

    await state.update_data(assignee_id=assignee_id, assignee_name=assignee_name)
    await state.set_state(CreateTaskFSM.deadline)
    await callback.message.edit_text(t['enter_deadline'], parse_mode="HTML")


@router.message(CreateTaskFSM.assignee)
async def process_assignee_hint(message: Message):
    # Если админ пишет текстом вместо выбора — снова показываем список кнопок.
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]
    await message.answer(t['choose_assignee'], reply_markup=assignee_inline_kb(user_data['pid']), parse_mode="HTML")


@router.message(CreateTaskFSM.deadline)
async def process_deadline(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    t = MESSAGES[user_data['lang']]

    if not user_data.get('linked'):
        await state.clear()
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return

    await state.update_data(deadline=message.text)
    data = await state.get_data()
    assignee_pid = data.get('assignee_id')

    task_id, code = create_task(
        data['title'], data['direction'], data.get('project_id'),
        from_pid=user_data['pid'], to_pid=assignee_pid, deadline=data['deadline'],
    )
    await state.clear()

    # Уведомляем исполнителя в Telegram (если он привязал аккаунт через /link).
    tg = get_telegram_id(assignee_pid)
    if tg and tg != message.from_user.id:
        try:
            await bot.send_message(
                tg,
                MESSAGES['ru']['task_assigned'].format(
                    id=code, title=esc(data['title']),
                    dir=esc(data['direction']), deadline=esc(data['deadline'])),
                parse_mode="HTML")
        except Exception as e:
            logging.info("Не удалось уведомить исполнителя %s: %s", tg, e)

    await message.answer(
        t['task_created'].format(
            id=code, title=esc(data['title']), dir=esc(data['direction']),
            assignee=esc(data['assignee_name']), deadline=esc(data['deadline'])
        ),
        reply_markup=main_keyboard(user_data['lang'], user_data['role'] == 'admin'),
        parse_mode="HTML"
    )


# --- ПРОСМОТР И ВЫПОЛНЕНИЕ ЗАДАЧ В ЧАТЕ ---

@router.message(F.text.in_(["📊 Все поручения", "📊 Barcha topshiriqlar", "📊 All Tasks"]))
async def show_all_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return
    t = MESSAGES[user_data['lang']]

    if user_data['role'] != 'admin':
        await message.answer(t['no_access'])
        return

    tasks = get_all_tasks()
    if not tasks:
        await message.answer(t['no_tasks'])
        return

    # Telegram caps a message at 4096 chars, so send the list in chunks
    # instead of one giant message (which fails with "message is too long").
    header = f"📋 <b>{esc(t['btn_all_tasks'])}</b> ({len(tasks)}):\n\n"
    chunk = header
    for row in tasks:
        block = (
            f"🔹 <b>{esc(row['code'])} {esc(row['title'])}</b>\n"
            f"• {esc(row['direction_code'] or '—')} | → {esc(row['assignee_name'])}\n"
            f"• <code>{esc(row['deadline'] or '—')}</code> | "
            f"Status: <code>{esc(row['status'])}</code>\n\n"
        )
        if len(chunk) + len(block) > 3800:
            await message.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += block
    if chunk.strip():
        await message.answer(chunk, parse_mode="HTML")


@router.message(F.text.in_(["📋 Мои задания", "📋 Mening topshiriqlarim", "📋 My Tasks"]))
async def show_my_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, parse_mode="HTML")
        return
    t = MESSAGES[user_data['lang']]

    is_admin = user_data['role'] == 'admin'
    tasks = get_author_active(user_data['pid']) if is_admin else get_worker_tasks(user_data['pid'])

    if not tasks:
        await message.answer(t['no_tasks'])
        return

    for row in tasks:
        # Кнопка «Выполнено» = сдать на проверку — только исполнителю.
        kb = None if is_admin else InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t['btn_done'], callback_data=f"done_{row['id']}")]
        ])
        await message.answer(
            f"📌 <b>{esc(row['code'])}: {esc(row['title'])}</b>\n"
            f"🏛 {esc(row['direction_code'] or '—')}"
            + (f"  →  {esc(row['assignee_name'])}" if is_admin else "") + "\n"
            f"⏰ <code>{esc(row['deadline'] or '—')}</code> | "
            f"Status: <code>{esc(row['status'])}</code>",
            reply_markup=kb,
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("done_"))
async def mark_done(callback):
    user_data = get_user(callback.from_user.id)
    t = MESSAGES[user_data['lang']]

    if not user_data.get('linked'):
        await callback.answer()
        return
    try:
        task_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer()
        return

    row = get_task(task_id)
    if not row:
        await callback.answer()
        return
    # Сдать на проверку может только исполнитель задачи.
    if row['assignee_id'] != user_data['pid']:
        await callback.answer(t.get('no_access', 'Нет доступа'), show_alert=True)
        return

    submit_task(task_id, user_data['pid'])
    await callback.answer(t['task_done_msg'])
    base = esc(callback.message.text or "")
    await callback.message.edit_text(
        f"{base}\n\n📤 <b>→ TEKSHIRUVDA (на проверке у автора)</b>", parse_mode="HTML")

    # Уведомим автора задачи, если он привязал Telegram.
    try:
        with db_conn() as conn:
            author = conn.execute("SELECT from_user_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        atg = get_telegram_id(author['from_user_id']) if author else None
        if atg:
            await bot.send_message(
                atg, f"📤 <b>{esc(row['code'])}</b>: {esc(row['title'])} — сдано на проверку.",
                parse_mode="HTML")
    except Exception as e:
        logging.info("Не удалось уведомить автора: %s", e)


# --- ВЕБХУК УВЕДОМЛЕНИЙ: платформа -> бот ---
# Платформа при выдаче/смене задачи POST-ит сюда, чтобы Telegram прислал пуш
# исполнителю/автору. Защищено общим секретом BOT_NOTIFY_SECRET (если задан).

async def wa_notify(request):
    if NOTIFY_SECRET and request.headers.get("X-Notify-Secret", "") != NOTIFY_SECRET:
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad_json"}, status=400)

    text = str(body.get("text", "")).strip()
    tg_id = body.get("telegram_id")
    to_pid = body.get("to_pid")
    if not text:
        return web.json_response({"error": "no_text"}, status=400)

    if tg_id is None and to_pid is not None:
        try:
            tg_id = get_telegram_id(int(to_pid))
        except (TypeError, ValueError):
            tg_id = None
    if not tg_id:
        return web.json_response({"ok": False, "reason": "not_linked"})

    try:
        await bot.send_message(int(tg_id), text, parse_mode="HTML")
        return web.json_response({"ok": True})
    except Exception as e:
        logging.info("notify send failed: %s", e)
        return web.json_response({"ok": False, "reason": str(e)})


async def wa_health(request):
    return web.json_response({"ok": True, "db": DB_PATH})


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", wa_health)
    app.router.add_post("/notify", wa_notify)
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
