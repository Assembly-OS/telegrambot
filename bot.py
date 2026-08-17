import asyncio
import hashlib
import hmac
import html
import json
import io
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
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
# Общая база с платформой Assembly OS (Next.js). Бот и платформа работают с
# ОДНИМИ пользователями и задачами. По умолчанию — ../data/assambleya.db.
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "assambleya.db")
DB_PATH = os.getenv("PLATFORM_DB", _DEFAULT_DB)
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()

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

# --- ФИЛЬТРЫ СПИСКА «ВСЕ ПОРУЧЕНИЯ» ---
# Порядок = от самого срочного к самому спокойному: руководитель почти всегда
# ищет верх списка, поэтому KRITIK стоит первым.
PRIORITY_ORDER = ("KRITIK", "YUQORI", "ORTA", "PAST")
PRIORITY_LABELS = {
    'ru': {"KRITIK": "Критичный", "YUQORI": "Высокий", "ORTA": "Средний", "PAST": "Низкий"},
    'uz': {"KRITIK": "Kritik", "YUQORI": "Yuqori", "ORTA": "O'rta", "PAST": "Past"},
    'en': {"KRITIK": "Critical", "YUQORI": "High", "ORTA": "Medium", "PAST": "Low"},
}
# Сколько поручений показывать на одной странице. Telegram режет сообщение на
# 4096 символах, а один блок занимает ~150 — восьми хватает с запасом.
TASKS_PAGE = 8
# Сколько исполнителей влезает в один экран выбора.
ASSIGNEES_PAGE = 8

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

if APP_ENV == "production" and len(NOTIFY_SECRET) < 32:
    raise SystemExit("BOT_NOTIFY_SECRET должен содержать не менее 32 символов в production.")

if APP_ENV == "production" and not PLATFORM_URL.startswith("https://"):
    raise SystemExit("PLATFORM_URL должен быть публичным HTTPS-адресом в production.")

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
        'welcome': "<b>ASSEMBLY AI OS — Система управления</b>\n\nПриветствуем, <b>{name}</b>!\nВаша роль: <b>{role}</b>",
        'role_admin': "Главный (Администратор)",
        'role_worker': "Работник / Исполнитель",
        'btn_add_task': "Дать задание",
        'btn_my_tasks': "Мои задания",
        'btn_all_tasks': "Все поручения",
        'btn_profile': "Мой профиль / Должность",
        'btn_admin_panel': "Админ-панель",
        'btn_change_lang': "Сменить язык",
        'btn_open_app': "Открыть приложение",
        'choose_lang': "<b>Выберите язык / Tilni tanlang / Choose language:</b>",
        'enter_title': "Введите <b>суть / описание задания</b>:",
        'choose_dir': "Выберите <b>направление (GR/FR/BR/PR)</b>:",
        'choose_proj': "Выберите <b>проект</b>:",
        'no_proj': "Без проекта",
        'enter_assignee': "Напишите <b>ФИО или должность работника</b>:",
        'choose_assignee': "Выберите <b>ответственного</b> (из тех, кто запускал бота):",
        'no_users': "Пока никто не запускал бота, кроме вас. Попросите сотрудников открыть бота и нажать /start.",
        'enter_deadline': "Укажите <b>срок исполнения (Дедлайн)</b>:",
        'task_created': "<b>Задание #{id} успешно создано!</b>\n\n<b>Суть:</b> {title}\n<b>Направление:</b> {dir}\n<b>Ответственный:</b> {assignee}\n<b>Срок:</b> {deadline}",
        'task_assigned': "<b>Вам назначено новое задание #{id}!</b>\n\n<b>Суть:</b> {title}\n<b>Направление:</b> {dir}\n<b>Срок:</b> {deadline}",
        'btn_done': "Отметить как выполненное",
        'no_tasks': "Нет активных заданий!",
        'task_done_msg': "Задание выполнено!",
        'admin_profile': "<b>АДМИН-ПАНЕЛЬ</b>\n\n<b>Пользователь:</b> {name}\n<b>Должность:</b> Главный руководитель / Раис\n<b>Права:</b> Полный доступ ко всем функциям.",
        'worker_profile': "<b>ПРОФИЛЬ СОТРУДНИКА</b>\n\n<b>Пользователь:</b> {name}\n<b>Должность:</b> Исполнитель / Работник\n<b>Права:</b> Просмотр и выполнение заданий.",
        'no_access': "У вас нет прав для выполнения этой команды.",
        'webapp_error': "Не удалось обработать данные из приложения. Попробуйте ещё раз.",
        'status_in_progress': "В процессе",
        'status_done': "Выполнено",
        'admin_granted': "<b>Права Главного (Администратора) получены!</b>",
        'admin_login_disabled': "Вход администратора отключён. Задайте ADMIN_SECRET в .env.",
        'admin_login_wrong': "Неверный пароль администратора.",
        'flt_priority': "Приоритет",
        'flt_assignee': "Исполнитель",
        'flt_all': "Все",
        'flt_pick_assignee': "<b>Выберите исполнителя:</b>",
        'flt_back': "← Назад к списку",
        'flt_empty': "По этому фильтру поручений нет.",
        'flt_shown': "показано {from}–{to} из {total}"
    },
    'uz': {
        'welcome': "<b>ASSEMBLY AI OS — Boshqaruv tizimi</b>\n\nXush kelibsiz, <b>{name}</b>!\nSizning rolingiz: <b>{role}</b>",
        'role_admin': "Boshliq (Admin)",
        'role_worker': "Xodim / Bajaruvchi",
        'btn_add_task': "Topshiriq berish",
        'btn_my_tasks': "Mening topshiriqlarim",
        'btn_all_tasks': "Barcha topshiriqlar",
        'btn_profile': "Mening lavozimim",
        'btn_admin_panel': "Admin panel",
        'btn_change_lang': "Tilni o'zgartirish",
        'btn_open_app': "Ilovani ochish",
        'choose_lang': "<b>Tilni tanlang / Выберите язык / Choose language:</b>",
        'enter_title': "Topshiriq <b>mazmunini</b> kiriting:",
        'choose_dir': "<b>Yo'nalishni tanlang (GR/FR/BR/PR):</b>",
        'choose_proj': "<b>Loyiha tanlang:</b>",
        'no_proj': "Loyihasiz",
        'enter_assignee': "Xodimning <b>F.I.SH yoki lavozimini</b> kiriting:",
        'choose_assignee': "<b>Mas'ulni</b> tanlang (botni ishga tushirganlardan):",
        'no_users': "Hozircha sizdan boshqa hech kim botni ishga tushirmagan. Xodimlardan botni ochib /start bosishlarini so'rang.",
        'enter_deadline': "Topshiriq <b>muddatini (dedlayn)</b> kiriting:",
        'task_created': "<b>Topshiriq #{id} muvaffaqiyatli yaratildi!</b>\n\n<b>Mazmuni:</b> {title}\n<b>Yo'nalish:</b> {dir}\n<b>Mas'ul:</b> {assignee}\n<b>Muddat:</b> {deadline}",
        'task_assigned': "<b>Sizga yangi topshiriq #{id} berildi!</b>\n\n<b>Mazmuni:</b> {title}\n<b>Yo'nalish:</b> {dir}\n<b>Muddat:</b> {deadline}",
        'btn_done': "Bajarildi deb belgilash",
        'no_tasks': "Faol topshiriqlar yo'q!",
        'task_done_msg': "Topshiriq bajarildi!",
        'admin_profile': "<b>ADMIN PANEL</b>\n\n<b>Foydalanuvchi:</b> {name}\n<b>Lavozim:</b> Bosh rahbar / Rais\n<b>Huquqlar:</b> To'liq kirish huquqi.",
        'worker_profile': "<b>XODIM PROFILI</b>\n\n<b>Foydalanuvchi:</b> {name}\n<b>Lavozim:</b> Ijrochi / Xodim\n<b>Huquqlar:</b> Topshiriqlarni ko'rish va bajarish.",
        'no_access': "Ushbu buyruqni bajarish uchun sizda huquq yo'q.",
        'webapp_error': "Ilovadan kelgan ma'lumotni qayta ishlab bo'lmadi. Qayta urinib ko'ring.",
        'status_in_progress': "Jarayonda",
        'status_done': "Bajarildi",
        'admin_granted': "<b>Boshliq (Admin) huquqlari berildi!</b>",
        'admin_login_disabled': "Administrator kirishi o'chirilgan. .env faylida ADMIN_SECRET ni belgilang.",
        'admin_login_wrong': "Administrator paroli noto'g'ri.",
        'flt_priority': "Muhimlik",
        'flt_assignee': "Ijrochi",
        'flt_all': "Hammasi",
        'flt_pick_assignee': "<b>Ijrochini tanlang:</b>",
        'flt_back': "← Ro'yxatga qaytish",
        'flt_empty': "Ushbu filtr bo'yicha topshiriq yo'q.",
        'flt_shown': "{total} tadan {from}–{to} ko'rsatilmoqda"
    },
    'en': {
        'welcome': "<b>ASSEMBLY AI OS — Management System</b>\n\nWelcome, <b>{name}</b>!\nYour role: <b>{role}</b>",
        'role_admin': "Chief (Admin)",
        'role_worker': "Employee / Worker",
        'btn_add_task': "Assign Task",
        'btn_my_tasks': "My Tasks",
        'btn_all_tasks': "All Tasks",
        'btn_profile': "My Position / Profile",
        'btn_admin_panel': "Admin Panel",
        'btn_change_lang': "Change Language",
        'btn_open_app': "Open App",
        'choose_lang': "<b>Select language / Tilni tanlang / Выберите язык:</b>",
        'enter_title': "Enter <b>task description</b>:",
        'choose_dir': "Select <b>direction (GR/FR/BR/PR)</b>:",
        'choose_proj': "Select <b>project</b>:",
        'no_proj': "No project",
        'enter_assignee': "Enter <b>employee name or title</b>:",
        'choose_assignee': "Select the <b>assignee</b> (from users who started the bot):",
        'no_users': "Nobody except you has started the bot yet. Ask employees to open the bot and press /start.",
        'enter_deadline': "Enter <b>deadline</b>:",
        'task_created': "<b>Task #{id} successfully created!</b>\n\n<b>Title:</b> {title}\n<b>Direction:</b> {dir}\n<b>Assignee:</b> {assignee}\n<b>Deadline:</b> {deadline}",
        'task_assigned': "<b>A new task #{id} was assigned to you!</b>\n\n<b>Title:</b> {title}\n<b>Direction:</b> {dir}\n<b>Deadline:</b> {deadline}",
        'btn_done': "Mark as Completed",
        'no_tasks': "No active tasks!",
        'task_done_msg': "Task completed!",
        'admin_profile': "<b>ADMIN PANEL</b>\n\n<b>User:</b> {name}\n<b>Position:</b> Head / Leader\n<b>Permissions:</b> Full administrative access.",
        'worker_profile': "<b>EMPLOYEE PROFILE</b>\n\n<b>User:</b> {name}\n<b>Position:</b> Worker / Executive\n<b>Permissions:</b> View and complete tasks.",
        'no_access': "You do not have permission to execute this command.",
        'webapp_error': "Could not process data from the app. Please try again.",
        'status_in_progress': "In progress",
        'status_done': "Done",
        'admin_granted': "<b>Chief (Admin) rights granted!</b>",
        'admin_login_disabled': "Admin login is disabled. Set ADMIN_SECRET in .env.",
        'admin_login_wrong': "Wrong admin password.",
        'flt_priority': "Priority",
        'flt_assignee': "Assignee",
        'flt_all': "All",
        'flt_pick_assignee': "<b>Choose an assignee:</b>",
        'flt_back': "← Back to the list",
        'flt_empty': "No tasks match this filter.",
        'flt_shown': "showing {from}–{to} of {total}"
    }
}


def status_label(t: dict, status: str) -> str:
    # Показываем код статуса платформы (YANGI / TEKSHIRUVDA / BAJARILDI …).
    return status


# --- БАЗА ДАННЫХ ПЛАТФОРМЫ (общая с Assembly OS) ---
# Роли-руководители платформы — им можно выдавать задачи через бота.
MANAGER_ROLES = {"RAIS", "BOLIM_RAHBARI", "AI_LAB", "UYUSHMA_RAISI", "LOYIHA_RAHBARI"}


@contextmanager
def db_conn():
    """Единая точка доступа к общей БД. WAL допускает параллельную работу с
    Next.js-платформой; busy_timeout ждёт, если писатель занят."""
    if not os.path.isfile(DB_PATH):
        raise RuntimeError("backend schema is not initialized")
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
    """Verify backend-owned schema readiness; the bot never runs DDL."""
    with db_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "telegram_id" not in cols:
            raise RuntimeError("backend schema is not ready: users.telegram_id is missing")
        report_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bot_reports'"
        ).fetchone()
        if not report_table:
            raise RuntimeError("backend schema is not ready: bot_reports is missing")


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


def _task_filter(priority, assignee_id):
    """Общий WHERE для списка «Все поручения»: пустой фильтр = без условий."""
    where, params = [], []
    if priority:
        where.append("t.priority = ?")
        params.append(priority)
    if assignee_id:
        where.append("t.to_user_id = ?")
        params.append(assignee_id)
    return ("WHERE " + " AND ".join(where)) if where else "", params


def get_tasks_page(priority=None, assignee_id=None, offset=0, limit=TASKS_PAGE):
    """Страница отфильтрованного списка + общее число совпадений.

    Считаем и выбираем одним фильтром, чтобы счётчик на кнопках страниц не
    разошёлся с тем, что реально показано.
    """
    clause, params = _task_filter(priority, assignee_id)
    with db_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks t {clause}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {_TASK_COLS} {_TASK_JOIN} {clause} ORDER BY t.id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return rows, total


def get_task_assignees(priority=None):
    """Исполнители, у которых есть поручения при текущем фильтре приоритета.

    Список строится по фактическим задачам, а не по всем пользователям, чтобы
    в выборе не было заведомо пустых вариантов.
    """
    clause, params = _task_filter(priority, None)
    with db_conn() as conn:
        return conn.execute(
            f"SELECT t.to_user_id AS id, s.full_name AS name, COUNT(*) AS cnt "
            f"FROM tasks t JOIN users s ON s.id = t.to_user_id {clause} "
            "GROUP BY t.to_user_id, s.full_name ORDER BY s.full_name",
            params,
        ).fetchall()


def get_user_name(pid: int):
    with db_conn() as conn:
        row = conn.execute("SELECT full_name FROM users WHERE id = ?", (pid,)).fetchone()
    return row["full_name"] if row else None


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
            InlineKeyboardButton(text="O'zbekcha", callback_data="setlang_uz"),
            InlineKeyboardButton(text="Русский", callback_data="setlang_ru"),
            InlineKeyboardButton(text="English", callback_data="setlang_en")
        ]
    ])


def assignee_inline_kb(exclude_pid=None):
    """Inline-клавиатура со списком возможных исполнителей (без Раиса и автора)."""
    rows = []
    for u in get_assignables(exclude_pid):
        name = u['full_name'] or str(u['id'])
        dep = f" · {u['department']}" if u['department'] else ""
        rows.append([InlineKeyboardButton(text=f"{name}{dep}", callback_data=f"asg_{u['id']}")])
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
        # Ведём на /login, а не на корень. Корень отвечает редиректом, а
        # Telegram передаёт данные Mini App во фрагменте адреса
        # (#tgWebAppData=...) — часть клиентов теряет фрагмент при редиректе, и
        # приложение открывается «пустым». /login сам пропускает уже вошедшего
        # на /dashboard, так что лишнего шага для человека не появляется.
        last_row.append(KeyboardButton(
            text=t['btn_open_app'],
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/login?lang={lang}"),
        ))
    last_row.append(KeyboardButton(text=t['btn_change_lang']))
    keyboard.append(last_row)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


# --- ОБРАБОТКА СТАРТА И СМЕНЫ ЯЗЫКА ---

# --- ПРИВЯЗКА TELEGRAM ↔ УЧЁТКА ПЛАТФОРМЫ ---
LINK_PROMPT = (
    "<b>Assembly OS</b>\n\n"
    "Откройте приложение кнопкой ниже и войдите один раз — "
    "дальше Telegram будет узнавать вас сам, и уведомления начнут приходить сюда.\n\n"
    "Если кнопка не открывается, можно привязать вручную: "
    "<code>/link ЛОГИН ПАРОЛЬ</code> (сообщение с паролем бот сразу удалит)."
)


def link_keyboard(lang: str = "ru"):
    """Кнопка «открыть приложение» рядом с приглашением привязаться.

    Раньше здесь была только строчка «наберите /link ЛОГИН ПАРОЛЬ», и её не
    выполнил ни один из четырнадцати сотрудников — то есть весь канал
    уведомлений был подключён и не доставлял никому. Приложение привязывает
    учётную запись само, при первом входе, поэтому дорога внутрь должна быть
    кнопкой, а не инструкцией.
    """
    if not WEBAPP_URL:
        return None
    t = MESSAGES.get(lang, MESSAGES["ru"])
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(
            text=t["btn_open_app"],
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/login?lang={lang}"),
        )]],
        resize_keyboard=True,
    )
LINK_USAGE = "Формат: <code>/link ЛОГИН ПАРОЛЬ</code>\nНапример: <code>/link rais 12345678</code>"
LINK_FAIL = "Неверный логин или пароль платформы. Ещё раз: <code>/link ЛОГИН ПАРОЛЬ</code>"
LINK_NOUSER = "Учётка не найдена или отключена."
LINK_OK = "Telegram привязан к <b>{name}</b> (@{login}). Готово — пользуйтесь ботом."


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
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
        return
    t = MESSAGES[u["lang"]]
    role_text = t['role_admin'] if u['role'] == 'admin' else t['role_worker']
    await message.answer(
        t['welcome'].format(name=esc(u['full_name'] or message.from_user.full_name), role=role_text),
        reply_markup=main_keyboard(u['lang'], u['role'] == 'admin'), parse_mode="HTML")


@router.message(Command("app"))
async def cmd_app(message: Message):
    """Свежая кнопка на текущий адрес платформы.

    Адрес временного туннеля меняется при каждом обрыве, а Telegram хранит и
    кнопку меню, и кнопки в отправленных сообщениях такими, какими они были в
    момент отправки. Клиент может держать мёртвый адрес часами, и человек
    видит «Error 1016» на исправной системе.

    Эта команда — выход из положения: бот перезапускается вместе со сменой
    адреса, поэтому WEBAPP_URL здесь всегда текущий, а кнопка приходит новым
    сообщением, мимо любого кэша.
    """
    if not WEBAPP_URL:
        await message.answer("Публичный адрес платформы сейчас не поднят.")
        return
    u = get_user(message.from_user.id)
    lang = (u or {}).get("lang", "ru")
    await message.answer(
        "Свежая ссылка на платформу. Прежние кнопки могли устареть.",
        reply_markup=link_keyboard(lang),
        parse_mode="HTML",
    )


async def _do_link(message: Message, login: str, password: str):
    """Проверяет пару логин/пароль и привязывает Telegram к учётной записи."""
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
    await _do_link(message, parts[0].strip(), parts[1].strip())


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


@router.message(F.text.in_(["Сменить язык", "Tilni o'zgartirish", "Change Language"]))
async def change_lang_btn(message: Message):
    await message.answer(MESSAGES['ru']['choose_lang'], reply_markup=lang_inline_kb(), parse_mode="HTML")


# --- ПРОФИЛЬ И АДМИН ПАНЕЛЬ ---

@router.message(F.text.in_(["Админ-панель", "Admin panel", "Мой профиль / Должность", "Mening lavozimim", "My Position / Profile"]))
async def show_profile(message: Message):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
        return
    t = MESSAGES[user_data['lang']]

    name = esc(user_data['full_name'] or message.from_user.full_name)
    text = t['admin_profile'].format(name=name) if user_data['role'] == 'admin' else t['worker_profile'].format(name=name)
    await message.answer(
        text + f"\n\n<code>@{esc(user_data['login'])}</code> · {esc(user_data['platform_role'])}",
        parse_mode="HTML")


# --- СОЗДАНИЕ ЗАДАНИЯ В ЧАТЕ ---

@router.message(F.text.in_(["Дать задание", "Topshiriq berish", "Assign Task"]))
async def start_create_task(message: Message, state: FSMContext):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
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
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
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

def _prio_label(lang, priority):
    return PRIORITY_LABELS.get(lang, PRIORITY_LABELS['ru']).get(priority, priority)


def render_all_tasks(lang, priority, assignee_id, page):
    """Один экран списка «Все поручения» — текст и клавиатура фильтров.

    Список живёт в одном сообщении, которое перерисовывается на каждое нажатие:
    иначе каждый выбор фильтра плодил бы новую простыню в чате.
    """
    t = MESSAGES[lang]
    rows, total = get_tasks_page(priority, assignee_id, offset=page * TASKS_PAGE)

    # Фильтр мог сузить выборку так, что текущая страница уехала за конец.
    pages = max(1, (total + TASKS_PAGE - 1) // TASKS_PAGE)
    if page >= pages:
        page = pages - 1
        rows, total = get_tasks_page(priority, assignee_id, offset=page * TASKS_PAGE)

    who = get_user_name(assignee_id) if assignee_id else None
    head = (
        f"<b>{esc(t['btn_all_tasks'])}</b>\n"
        f"• {esc(t['flt_priority'])}: <b>{esc(_prio_label(lang, priority) if priority else t['flt_all'])}</b>\n"
        f"• {esc(t['flt_assignee'])}: <b>{esc(who or t['flt_all'])}</b>\n"
    )

    if total == 0:
        return head + "\n" + t['flt_empty'], filters_keyboard(lang, priority, assignee_id, page, pages)

    first = page * TASKS_PAGE + 1
    head += "• " + esc(
        t['flt_shown'].replace("{from}", str(first))
                      .replace("{to}", str(first + len(rows) - 1))
                      .replace("{total}", str(total))
    ) + "\n\n"

    body = "".join(
        f"<b>{esc(row['code'])} {esc(row['title'])}</b>\n"
        f"• {esc(row['direction_code'] or '—')} | → {esc(row['assignee_name'])}\n"
        f"• <code>{esc(row['deadline'] or '—')}</code> | "
        f"{esc(_prio_label(lang, row['priority']))} | "
        f"Status: <code>{esc(row['status'])}</code>\n\n"
        for row in rows
    )
    return head + body, filters_keyboard(lang, priority, assignee_id, page, pages)


def filters_keyboard(lang, priority, assignee_id, page, pages):
    """Приоритет одной строкой, исполнитель — отдельным экраном, снизу страницы."""
    t = MESSAGES[lang]
    prio_code = priority or "ALL"
    uid = assignee_id or 0

    def mark(active, text):
        return f"• {text} •" if active else text

    rows = [[InlineKeyboardButton(
        text=mark(priority is None, t['flt_all']),
        callback_data=f"af:ALL:{uid}:0",
    )]]
    # Четыре приоритета в два ряда: в один ряд подписи вроде «Критичный» не влезают.
    for pair in (PRIORITY_ORDER[:2], PRIORITY_ORDER[2:]):
        rows.append([
            InlineKeyboardButton(
                text=mark(priority == code, _prio_label(lang, code)),
                callback_data=f"af:{code}:{uid}:0",
            )
            for code in pair
        ])

    who = get_user_name(assignee_id) if assignee_id else None
    rows.append([InlineKeyboardButton(
        text=f"{t['flt_assignee']}: {who or t['flt_all']}",
        callback_data=f"ap:{prio_code}:{uid}:0",
    )])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"af:{prio_code}:{uid}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="af:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"af:{prio_code}:{uid}:{page + 1}"))
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def assignees_keyboard(lang, priority, assignee_id, page):
    """Экран выбора исполнителя: по одному в ряд, чтобы влезали длинные ФИО."""
    t = MESSAGES[lang]
    prio_code = priority or "ALL"
    people = get_task_assignees(priority)
    pages = max(1, (len(people) + ASSIGNEES_PAGE - 1) // ASSIGNEES_PAGE)
    page = max(0, min(page, pages - 1))
    window = people[page * ASSIGNEES_PAGE:(page + 1) * ASSIGNEES_PAGE]

    rows = [[InlineKeyboardButton(
        text=("• " if not assignee_id else "") + f"{t['flt_all']}",
        callback_data=f"af:{prio_code}:0:0",
    )]]
    for person in window:
        mark = "• " if person["id"] == assignee_id else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{person['name']} ({person['cnt']})",
            callback_data=f"af:{prio_code}:{person['id']}:0",
        )])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"ap:{prio_code}:{assignee_id or 0}:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="af:noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"ap:{prio_code}:{assignee_id or 0}:{page + 1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(
        text=t['flt_back'], callback_data=f"af:{prio_code}:{assignee_id or 0}:0"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _parse_filter(data: str):
    """"af:KRITIK:12:1" -> ("KRITIK", 12, 1). ALL/0 означают «без фильтра»."""
    _, prio, uid, page = data.split(":", 3)
    return (None if prio == "ALL" else prio), (int(uid) or None), int(page)


async def _safe_edit(message, text, keyboard):
    """Нажатие на уже активный фильтр ничего не меняет, а Telegram считает
    правку без изменений ошибкой. Для пользователя это не ошибка — молчим."""
    try:
        await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    except TelegramBadRequest as err:
        if "not modified" not in str(err):
            raise


@router.message(F.text.in_(["Все поручения", "Barcha topshiriqlar", "All Tasks"]))
async def show_all_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
        return
    t = MESSAGES[user_data['lang']]

    if user_data['role'] != 'admin':
        await message.answer(t['no_access'])
        return

    text, keyboard = render_all_tasks(user_data['lang'], None, None, 0)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data.startswith("af:"))
async def filter_all_tasks(callback):
    user_data = get_user(callback.from_user.id)
    if not user_data.get('linked') or user_data['role'] != 'admin':
        await callback.answer()
        return
    if callback.data == "af:noop":       # счётчик страниц — не кнопка
        await callback.answer()
        return

    priority, assignee_id, page = _parse_filter(callback.data)
    text, keyboard = render_all_tasks(user_data['lang'], priority, assignee_id, page)
    await _safe_edit(callback.message, text, keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:"))
async def pick_assignee(callback):
    user_data = get_user(callback.from_user.id)
    if not user_data.get('linked') or user_data['role'] != 'admin':
        await callback.answer()
        return

    priority, assignee_id, page = _parse_filter(callback.data)
    t = MESSAGES[user_data['lang']]
    await _safe_edit(
        callback.message,
        t['flt_pick_assignee'],
        assignees_keyboard(user_data['lang'], priority, assignee_id, page),
    )
    await callback.answer()


@router.message(F.text.in_(["Мои задания", "Mening topshiriqlarim", "My Tasks"]))
async def show_my_tasks(message: Message):
    user_data = get_user(message.from_user.id)
    if not user_data.get('linked'):
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
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
            f"<b>{esc(row['code'])}: {esc(row['title'])}</b>\n"
            f"{esc(row['direction_code'] or '—')}"
            + (f"  →  {esc(row['assignee_name'])}" if is_admin else "") + "\n"
            f"<code>{esc(row['deadline'] or '—')}</code> |"
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
        f"{base}\n\n<b>→ TEKSHIRUVDA (на проверке у автора)</b>", parse_mode="HTML")

    # Уведомим автора задачи, если он привязал Telegram.
    try:
        with db_conn() as conn:
            author = conn.execute("SELECT from_user_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        atg = get_telegram_id(author['from_user_id']) if author else None
        if atg:
            await bot.send_message(
                atg, f"<b>{esc(row['code'])}</b>: {esc(row['title'])} — сдано на проверку.",
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
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT 1").fetchone()
        if not row or row[0] != 1:
            raise RuntimeError("database readiness check failed")
        return web.json_response({"ok": True})
    except Exception:
        return web.json_response({"ok": False}, status=503)


def build_web_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", wa_health)
    app.router.add_post("/notify", wa_notify)
    return app



# --- ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ---
# Пятница, 18:00 по времени сервера: сводка недели уходит Раису и всем
# руководителям, у кого привязан Telegram. Отчёт считает платформа — бот только
# забирает готовый текст, чтобы цифры на сайте и в сообщении не разошлись.

REPORT_WEEKDAY = int(os.getenv("REPORT_WEEKDAY", "4"))   # 0=понедельник, 4=пятница
REPORT_HOUR = int(os.getenv("REPORT_HOUR", "18"))
REPORT_MINUTE = int(os.getenv("REPORT_MINUTE", "0"))


def _report_marker(when: datetime) -> str:
    """Год и номер недели — чтобы за одну неделю отчёт ушёл ровно один раз."""
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def _report_already_sent(marker: str) -> bool:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT marker FROM bot_reports WHERE marker = ?", (marker,)
        ).fetchone()
        return row is not None


def _mark_report_sent(marker: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bot_reports (marker, sent_at) VALUES (?, ?)",
            (marker, _now()),
        )


async def fetch_weekly_report(offset: int = 0):
    """Забирает готовый текст отчёта и список получателей у платформы."""
    url = f"{PLATFORM_API}/api/reports/weekly?format=text&offset={offset}"
    headers = {"X-Notify-Secret": NOTIFY_SECRET} if NOTIFY_SECRET else {}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                logging.warning("Отчёт недоступен: HTTP %s", response.status)
                return None
            return await response.json()


async def send_weekly_report(offset: int = 0) -> int:
    """Рассылает отчёт. Возвращает число доставленных сообщений."""
    data = await fetch_weekly_report(offset)
    if not data:
        return 0

    text = data.get("text") or ""
    delivered = 0
    for pid in data.get("recipients", []):
        tg_id = get_telegram_id(pid)
        if not tg_id:
            continue
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
            delivered += 1
        except Exception as exc:  # заблокировал бота, удалил чат и т.п.
            logging.warning("Отчёт не доставлен pid=%s: %s", pid, exc)
    return delivered


# --- НАПОМИНАНИЕ О ВЫВОДАХ СОВЕЩАНИЙ ---
# Каждое утро Раис и его помощник получают то, чем закончились совещания
# последних дней. Вывод рассылается один раз при разборе — но именно он через
# неделю нужен, а не в тот момент; напоминание возвращает его в поле зрения.
# Каждому на его языке: анализ пишет вывод сразу на трёх.

DIGEST_HOUR = int(os.getenv("MEETING_DIGEST_HOUR", "9"))
DIGEST_MINUTE = int(os.getenv("MEETING_DIGEST_MINUTE", "0"))
DIGEST_DAYS = int(os.getenv("MEETING_DIGEST_DAYS", "7"))
DIGEST_LOGINS = [
    login.strip().lower()
    for login in os.getenv("MEETING_BRIEF_LOGINS", "muslimbek.komiljonov").split(",")
    if login.strip()
]

DIGEST_LABELS = {
    "ru": ("Выводы совещаний за последние дни", "Ключевые пункты", "Решения"),
    "en": ("Meeting conclusions from recent days", "Key points", "Decisions"),
    "uz": ("So'nggi kunlardagi uchrashuv xulosalari", "Asosiy nuqtalar", "Qarorlar"),
}

# Ради чего вообще ведётся запись: держать в поле зрения не совещания, а
# отношения. Что кому уже предлагали и что стоит предложить теперь.
IDEAS_LABELS = {
    "ru": "Что можно предложить",
    "en": "Worth proposing",
    "uz": "Taklif qilish mumkin",
}


def _digest_recipients():
    """Раис плюс те, кого назвали в MEETING_BRIEF_LOGINS."""
    marks = ",".join("?" for _ in DIGEST_LOGINS) or "''"
    with db_conn() as conn:
        rows = conn.execute(
            f"SELECT id, lang FROM users "
            f"WHERE is_active = 1 AND (role = 'RAIS' OR LOWER(login) IN ({marks}))",
            tuple(DIGEST_LOGINS),
        ).fetchall()
    return [(row[0], row[1] or "uz") for row in rows]


def _digest_text(lang: str) -> str:
    """Выводы за последние дни на языке читателя. Пусто — значит нечего слать."""
    code = lang if lang in ("ru", "en") else "uz"
    title, points_label, decisions_label = DIGEST_LABELS[code]
    since = f"-{DIGEST_DAYS} days"
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT m.title, m.created_at, c.summary, c.key_points, c.decisions "
            "  FROM meetings m "
            "  JOIN meeting_conclusions c ON c.meeting_id = m.id AND c.lang = ? "
            " WHERE m.created_at >= datetime('now', ?) "
            " ORDER BY m.id DESC LIMIT 10",
            (code, since),
        ).fetchall()
    with db_conn() as conn:
        ideas = conn.execute(
            f"SELECT p.name, i.proposal_{code}, i.why_{code}, m.name "
            f"  FROM partner_ideas i "
            f"  JOIN partners p ON p.id = i.partner_id "
            f"  LEFT JOIN partners m ON m.id = i.match_id "
            f" WHERE i.status = 'yangi' ORDER BY i.id DESC LIMIT 6",
        ).fetchall()

    if not rows and not ideas:
        return ""

    parts = [f"<b>{title}</b>"]
    for meeting_title, created_at, summary, key_points, decisions in rows:
        day = (created_at or "")[:10]
        parts.append(f"\n<b>{meeting_title}</b> · {day}")
        if summary:
            parts.append(summary)
        for label, raw in ((points_label, key_points), (decisions_label, decisions)):
            try:
                items = json.loads(raw or "[]")
            except (TypeError, ValueError):
                items = []
            if items:
                lines = "\n".join(f"• {item}" for item in items)
                parts.append(f"<i>{label}:</i>\n{lines}")
    if ideas:
        parts.append(f"\n<b>{IDEAS_LABELS[code]}</b>")
        for partner, proposal, why, match in ideas:
            if not proposal:
                continue
            line = f"• <b>{partner}</b> — {proposal}"
            if match:
                line += f" ↔ {match}"
            if why:
                line += f"\n  <i>{why}</i>"
            parts.append(line)

    # Telegram обрывает сообщение длиннее 4096 символов.
    return "\n".join(parts)[:3800]


async def send_meeting_digest() -> int:
    delivered = 0
    for pid, lang in _digest_recipients():
        tg_id = get_telegram_id(pid)
        if not tg_id:
            continue
        text = _digest_text(lang)
        if not text:
            continue
        try:
            await bot.send_message(tg_id, text, parse_mode="HTML")
            delivered += 1
        except Exception as exc:
            logging.warning("Дайджест не доставлен pid=%s: %s", pid, exc)
    return delivered


async def meeting_digest_loop():
    """Раз в минуту проверяет, не пора ли напомнить о выводах."""
    while True:
        try:
            now = datetime.now()
            if now.hour == DIGEST_HOUR and now.minute >= DIGEST_MINUTE:
                marker = f"digest-{now:%Y-%m-%d}"
                if not _report_already_sent(marker):
                    _mark_report_sent(marker)
                    count = await send_meeting_digest()
                    logging.info("Дайджест совещаний %s: %s получателей",
                                 marker, count)
        except Exception as exc:
            logging.exception("Сбой в цикле дайджеста совещаний: %s", exc)
        await asyncio.sleep(60)


# --- ДОСТАВКА НАПОМИНАНИЙ ---
# Платформа сама разбирает очередь напоминаний, когда кто-нибудь открыл сайт.
# Ночью и в выходные там никого нет, а срок наступает всё равно — поэтому раз
# в минуту бот дёргает ту же процедуру. Она идемпотентна: кто успел первым, тот
# и отправил, повторов нет.

DISPATCH_EVERY = int(os.getenv("REMINDER_DISPATCH_SECONDS", "60"))


async def dispatch_reminders() -> int:
    url = f"{PLATFORM_API}/api/reminders/dispatch"
    headers = {"X-Notify-Secret": NOTIFY_SECRET} if NOTIFY_SECRET else {}
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers) as response:
                if response.status != 200:
                    return 0
                data = await response.json()
                return int(data.get("fired", 0))
    except Exception:
        # Платформа перезапускается или недоступна — попробуем через минуту.
        return 0


async def reminder_loop():
    while True:
        try:
            fired = await dispatch_reminders()
            if fired:
                logging.info("Напоминаний отправлено: %s", fired)
        except Exception as exc:
            logging.exception("Сбой в цикле напоминаний: %s", exc)
        await asyncio.sleep(DISPATCH_EVERY)


async def weekly_report_loop():
    """Раз в минуту проверяет, не наступило ли время еженедельной рассылки."""
    while True:
        try:
            now = datetime.now()
            due = (
                now.weekday() == REPORT_WEEKDAY
                and now.hour == REPORT_HOUR
                and now.minute >= REPORT_MINUTE
            )
            if due:
                marker = _report_marker(now)
                if not _report_already_sent(marker):
                    # Метка ставится до отправки: даже если рассылка упадёт на
                    # середине, повтора через минуту не будет.
                    _mark_report_sent(marker)
                    count = await send_weekly_report(0)
                    logging.info("Еженедельный отчёт %s отправлен: %s получателей",
                                 marker, count)
        except Exception as exc:
            logging.exception("Сбой в цикле еженедельного отчёта: %s", exc)
        await asyncio.sleep(60)


@router.message(Command("hisobot"))
async def cmd_report_now(message: Message):
    """Ручная отправка отчёта — для проверки и для «пришли прямо сейчас»."""
    user = get_user(message.from_user.id)
    if not user or (user["role"] not in MANAGER_ROLES and message.from_user.id not in ADMIN_IDS):
        return
    data = await fetch_weekly_report(0)
    if not data:
        await message.answer("Hisobotni olish imkoni bo'lmadi.")
        return
    await message.answer(data.get("text") or "—", parse_mode="HTML")


# --- ЗАПУСК ---


VOICE_WAIT = "Принял запись. Распознаю и разбираю — это может занять минуту."
VOICE_NOLINK = "Сначала привяжите аккаунт: пришлите <code>ЛОГИН ПАРОЛЬ</code> одним сообщением."
VOICE_FAIL = {
    "NOT_LINKED": "Аккаунт не привязан к платформе.",
    "FORBIDDEN": "У вашей роли нет доступа к разбору совещаний.",
    "TRANSCRIPT_TOO_SHORT": "Слишком короткая запись — распознавать нечего.",
    "EMPTY": "В записи не разобрано ни слова. Проверьте, что говорили в микрофон.",
    "STT_UNAVAILABLE": "Распознавание речи на сервере сейчас недоступно.",
    "TOO_LONG": "Запись слишком длинная.",
    "BAD_AUDIO": "Не удалось прочитать аудио.",
    "FAILED": "Распознавание не удалось.",
}


# Предел Telegram на скачивание ботом. Часовая запись его превысит, и это
# надо сказать прямо, а не молча уронить разбор.
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# Диктофон на телефоне отдаёт запись файлом, и Telegram присылает её как
# документ, а не как аудио. Ловим по типу и по расширению: iPhone шлёт m4a,
# Android — чаще m4a или mp3, а тип иногда приходит пустым.
AUDIO_EXT = (".m4a", ".mp3", ".ogg", ".oga", ".wav", ".aac", ".opus",
             ".webm", ".amr", ".flac", ".mp4", ".caf", ".aiff", ".aif")


def _audio_document(message: Message):
    """Документ, который на деле является записью. Иначе None."""
    doc = message.document
    if not doc:
        return None
    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    if mime.startswith("audio/") or mime.startswith("video/"):
        return doc
    if name.endswith(AUDIO_EXT):
        return doc
    return None


@router.message(F.voice | F.audio | F.video_note | F.document)
async def on_voice(message: Message):
    """Голосовое сообщение как запись совещания.

    Telegram не всегда отдаёт микрофон мини-приложению — на iPhone не отдаёт
    вовсе. Но записывать голосовые он умеет всегда и своими средствами, так
    что самый надёжный способ занести совещание с телефона — прислать его
    сюда. Дальше всё то же, что при загрузке с сайта: распознавание,
    выводы, черновики поручений руководителю на проверку.
    """
    u = get_user(message.from_user.id)
    if not u:
        await message.answer(VOICE_NOLINK, reply_markup=link_keyboard(), parse_mode="HTML")
        return

    media = message.voice or message.audio or message.video_note or _audio_document(message)
    if not media:
        await message.answer(
            "Пришлите запись: голосовое сообщение или файл с диктофона "
            "(m4a, mp3, ogg, wav). Другие файлы бот не разбирает — "
            "документы загружаются на самой платформе."
        )
        return

    if (media.file_size or 0) > TG_DOWNLOAD_LIMIT:
        mb = round((media.file_size or 0) / 1024 / 1024)
        await message.answer(
            f"Запись {mb} МБ — Telegram не даёт боту скачивать файлы больше 20 МБ.\n"
            "Загрузите её на платформе: раздел «Совещания» → новая запись. "
            "Там предел 200 МБ."
        )
        return

    file = await message.bot.get_file(media.file_id)
    buf = io.BytesIO()
    await message.bot.download_file(file.file_path, buf)
    buf.seek(0)

    note = await message.answer(VOICE_WAIT, parse_mode="HTML")

    # Подпись к голосовому становится названием совещания — это единственное
    # место, где человек может его назвать, не выходя из чата.
    title = (message.caption or "").strip()[:160] or "Ovozli xabar"

    data = aiohttp.FormData()
    data.add_field("telegram_id", str(message.from_user.id))
    data.add_field("title", title)
    data.add_field("lang", "auto")
    data.add_field("audio", buf.read(),
                   filename=(getattr(media, "file_name", None) or "voice.ogg"),
                   content_type=(getattr(media, "mime_type", None) or "audio/ogg"))

    url = f"{PLATFORM_API}/api/ai/meeting/telegram"
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, data=data,
                                 headers={"X-Notify-Secret": NOTIFY_SECRET},
                                 timeout=aiohttp.ClientTimeout(total=1800)) as r:
                payload = await r.json(content_type=None)
                status = r.status
    except Exception as e:
        logging.warning("Разбор голосового не удался (%s): %s", url, e)
        await note.edit_text("Платформа не отвечает. Попробуйте ещё раз.")
        return

    if status != 200:
        code = (payload or {}).get("error", "")
        await note.edit_text(VOICE_FAIL.get(code, f"Не удалось разобрать запись ({code or status})."))
        return

    lines = [f"<b>{esc(title)}</b>"]
    summary = (payload.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append(esc(summary))
    points = payload.get("keyPoints") or []
    if points:
        lines.append("")
        lines.append("<b>Ключевые пункты</b>")
        for point in points[:8]:
            lines.append(f"• {esc(str(point))}")

    created = payload.get("created") or 0
    drafts = payload.get("drafts") or 0
    if created or drafts:
        lines.append("")
        if drafts:
            lines.append(f"Черновиков поручений: {drafts} — ушли руководителю на проверку.")
        if created:
            lines.append(f"Поручений создано: {created}.")

    await note.edit_text("\n".join(lines)[:4000], parse_mode="HTML")


@router.message(F.text)
async def fallback(message: Message):
    """Последний обработчик: всё, что не подошло остальным.

    Здесь две задачи, и обе выросли из наблюдения за живой перепиской.

    Первая — пароли. Подсказка показывает «/link rais 12345678», и человек
    естественно набирает «rais 12345678» без команды. Раньше такое сообщение
    не подходило ни одному обработчику: бот молчал, а пароль оставался
    висеть в переписке навсегда. Теперь пара «слово слово» принимается как
    попытка привязки и сообщение удаляется первым же действием — до всякой
    проверки, потому что удалять надо и неверный пароль тоже.

    Вторая — молчание. Бот, не отвечающий ни на что незнакомое, выглядит
    сломанным. Лучше короткая подсказка, чем тишина.
    """
    text = (message.text or "").strip()
    parts = text.split()

    # Похоже на «логин пароль»: два слова, без пробелов внутри, не команда.
    if len(parts) == 2 and not text.startswith("/") and len(parts[1]) >= 6:
        try:
            await message.delete()
        except Exception:
            pass
        await _do_link(message, parts[0], parts[1])
        return

    u = get_user(message.from_user.id)
    if not u:
        await message.answer(LINK_PROMPT, reply_markup=link_keyboard(), parse_mode="HTML")
        return
    t = MESSAGES[u['lang']]
    await message.answer(
        "Не понял команду. Пользуйтесь кнопками ниже или /start.",
        reply_markup=main_keyboard(u['lang'], u['role'] == 'admin'),
    )


async def main():
    init_db()

    if WEBAPP_URL:
        await bot.set_chat_menu_button(
            # На /login, а не на корень: корень отвечает редиректом, а Telegram
            # передаёт данные Mini App во фрагменте адреса (#tgWebAppData=...),
            # который часть клиентов при редиректе теряет. Уже вошедшего
            # /login сам пропускает дальше, так что лишнего шага нет.
            menu_button=MenuButtonWebApp(
                text=MENU_BUTTON_TEXT,
                web_app=WebAppInfo(url=f"{WEBAPP_URL}/login"),
            )
        )
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())

    runner = web.AppRunner(build_web_app())
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()
    logging.info("Mini App сервер: http://%s:%s | публичный URL: %s",
                 WEBAPP_HOST, WEBAPP_PORT, WEBAPP_URL or "— (не задан)")

    reporter = asyncio.create_task(weekly_report_loop())
    digester = asyncio.create_task(meeting_digest_loop())
    reminders = asyncio.create_task(reminder_loop())

    try:
        await dp.start_polling(bot)
    finally:
        reporter.cancel()
        digester.cancel()
        reminders.cancel()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
