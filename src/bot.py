import asyncio
import csv
import io
import html
import json
import os
import re
import sqlite3
import logging
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.types import InputMediaPhoto, InputMediaVideo, LinkPreviewOptions, BufferedInputFile
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

_REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass

from db_path import (
    SNAPSHOT_DB,
    export_live_to_repo_snapshot,
    get_db_path,
    import_repo_snapshot_to_live,
)

# --- КОНФИГ ---


def _env_token() -> str:
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            "Задайте BOT_TOKEN: переменная окружения или файл .env (шаблон — .env.example)."
        )
    return token


API_TOKEN = _env_token()
CHANNEL_ID = '@KapitalBank_Assets' 
OWNER_ID = 120960192  
DB_PATH = get_db_path()
# Рамка карточки в канале (одинаковая длина во всех объявлениях)
CARD_DECO_LINE = "━" * 24

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)


class DeleteCallbackMessageMiddleware(BaseMiddleware):
    """Удаляет сообщение с inline-кнопками сразу после нажатия."""

    async def __call__(self, handler, event: types.CallbackQuery, data):
        result = await handler(event, data)
        msg = event.message
        if msg:
            try:
                await msg.delete()
            except Exception:
                # Игнорируем ошибки удаления (например, если сообщение уже удалено).
                pass
        return result


dp.callback_query.middleware(DeleteCallbackMessageMiddleware())

CLEANUP_BOT_IDS_KEY = "cleanup_bot_message_ids"
CLEANUP_USER_IDS_KEY = "cleanup_user_message_ids"
CLEANUP_ORDER_IDS_KEY = "cleanup_order_message_ids"
CLEANUP_PRESS_COUNTER_KEY = "cleanup_press_counter"
AUTO_CLEANUP_PAUSED_KEY = "auto_cleanup_paused"
CLEANUP_MAX_IDS = 80
AUTO_CLEANUP_WINDOW = 40
AUTO_CLEANUP_KEEP_LAST = 2
AUTO_CLEANUP_EVERY_BUTTON_PRESSES = 5


async def _append_cleanup_id(state: FSMContext, key: str, message_id: int):
    data = await state.get_data()
    ids = data.get(key, [])
    if message_id in ids:
        return
    ids.append(message_id)
    if len(ids) > CLEANUP_MAX_IDS:
        ids = ids[-CLEANUP_MAX_IDS:]
    await state.update_data(**{key: ids})


async def remember_cleanup_message(state: FSMContext, message: types.Message, is_user: bool = False):
    if state is None or message is None:
        return
    key = CLEANUP_USER_IDS_KEY if is_user else CLEANUP_BOT_IDS_KEY
    await _append_cleanup_id(state, key, message.message_id)
    await _append_cleanup_id(state, CLEANUP_ORDER_IDS_KEY, message.message_id)


class TrackIncomingMessageMiddleware(BaseMiddleware):
    """Сохраняет входящие и запускает автоочистку по счётчику нажатий."""

    async def __call__(self, handler, event: types.Message, data):
        state: FSMContext | None = data.get("state")
        if state is not None:
            await remember_cleanup_message(state, event, is_user=True)
        return await handler(event, data)


dp.message.middleware(TrackIncomingMessageMiddleware())


async def clear_state_preserve_cleanup(state: FSMContext):
    data = await state.get_data()
    keep = {}
    for key in (CLEANUP_BOT_IDS_KEY, CLEANUP_USER_IDS_KEY, CLEANUP_ORDER_IDS_KEY):
        ids = data.get(key, [])
        if ids:
            keep[key] = ids
    keep[CLEANUP_PRESS_COUNTER_KEY] = data.get(CLEANUP_PRESS_COUNTER_KEY, 0)
    keep[AUTO_CLEANUP_PAUSED_KEY] = bool(data.get(AUTO_CLEANUP_PAUSED_KEY, False))
    last_msg_id = data.get("last_msg_id")
    if last_msg_id:
        keep["last_msg_id"] = last_msg_id
    await state.clear()
    if keep:
        await state.update_data(**keep)


async def purge_cleanup_messages(
    *,
    chat_id: int,
    state: FSMContext,
    try_delete_user_messages: bool = True,
):
    data = await state.get_data()
    bot_ids = list(data.get(CLEANUP_BOT_IDS_KEY, []))
    user_ids = list(data.get(CLEANUP_USER_IDS_KEY, [])) if try_delete_user_messages else []
    last_msg_id = data.get("last_msg_id")
    if last_msg_id:
        bot_ids.append(last_msg_id)

    for msg_id in sorted(set(bot_ids + user_ids)):
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logging.debug("cleanup skip delete_message chat=%s message=%s: %s", chat_id, msg_id, e)

    await state.update_data(
        **{
            CLEANUP_BOT_IDS_KEY: [],
            CLEANUP_USER_IDS_KEY: [],
            CLEANUP_ORDER_IDS_KEY: [],
            CLEANUP_PRESS_COUNTER_KEY: 0,
            "last_msg_id": None,
        }
    )


async def auto_cleanup_recent_messages(chat_id: int, state: FSMContext):
    """Оставляет только последние сообщения в tracked-окне + добивает хвост по диапазону ID."""
    data = await state.get_data()
    order = list(data.get(CLEANUP_ORDER_IDS_KEY, []))
    bot_ids = set(data.get(CLEANUP_BOT_IDS_KEY, []))
    user_ids = set(data.get(CLEANUP_USER_IDS_KEY, []))
    last_msg_id = data.get("last_msg_id")

    # Учитываем только последние AUTO_CLEANUP_WINDOW сообщений и сохраняем порядок.
    uniq_order: list[int] = []
    seen: set[int] = set()
    for msg_id in order[-AUTO_CLEANUP_WINDOW:]:
        if msg_id in seen:
            continue
        seen.add(msg_id)
        uniq_order.append(msg_id)

    keep_ids = set(uniq_order[-AUTO_CLEANUP_KEEP_LAST:]) if uniq_order else set()
    delete_ids = [msg_id for msg_id in uniq_order if msg_id not in keep_ids]
    for msg_id in delete_ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logging.debug("auto-cleanup skip delete chat=%s message=%s: %s", chat_id, msg_id, e)

    # Страховка: чистим диапазон последних ID, чтобы удалять и нетрекнутые сообщения.
    latest_id = max([last_msg_id or 0, *uniq_order]) if (last_msg_id or uniq_order) else 0
    if latest_id > 0:
        floor_id = max(1, latest_id - AUTO_CLEANUP_WINDOW + 1)
        keep_floor = max(1, latest_id - AUTO_CLEANUP_KEEP_LAST + 1)
        for msg_id in range(floor_id, keep_floor):
            if msg_id in keep_ids:
                continue
            try:
                await bot.delete_message(chat_id, msg_id)
            except Exception as e:
                logging.debug("auto-cleanup range skip chat=%s message=%s: %s", chat_id, msg_id, e)

    remain_order = [msg_id for msg_id in uniq_order if msg_id in keep_ids]
    await state.update_data(
        **{
            CLEANUP_ORDER_IDS_KEY: remain_order,
            CLEANUP_BOT_IDS_KEY: [msg_id for msg_id in remain_order if msg_id in bot_ids],
            CLEANUP_USER_IDS_KEY: [msg_id for msg_id in remain_order if msg_id in user_ids],
        }
    )


async def try_delete_trigger_message(message: types.Message):
    """Пытается убрать текущее текстовое сообщение-кнопку пользователя."""
    if message is None:
        return
    try:
        await message.delete()
        return
    except Exception:
        pass
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logging.debug(
            "cleanup skip trigger delete chat=%s message=%s: %s",
            message.chat.id,
            message.message_id,
            e,
        )


def db_connect():
    """Единая точка подключения к SQLite с таймаутом на конкуренцию."""
    return sqlite3.connect(DB_PATH, timeout=10)

# --- БД (Авто-создание правильной структуры) ---
def init_db():
    conn = db_connect()
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, role TEXT DEFAULT "staff")')
    conn.execute('CREATE TABLE IF NOT EXISTS stats (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT, realty_type TEXT, city TEXT, district TEXT, street TEXT, house TEXT, total_area REAL, useful_area REAL, rooms TEXT, desc TEXT, price_val REAL, price_cur TEXT, media_type TEXT, media_files TEXT, published_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT, details TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_city_category_date ON posts(city, category, published_date DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_posts_channel_message_id ON posts(channel_message_id)')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.commit()
    conn.close()
    _migrate_posts_channel_message_id()
    _migrate_users_tg_username()
    _migrate_staff_access_requests()


def _migrate_users_tg_username():
    try:
        with db_connect() as conn:
            conn.execute("ALTER TABLE users ADD COLUMN tg_username TEXT")
            conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_posts_channel_message_id():
    try:
        with db_connect() as conn:
            conn.execute('ALTER TABLE posts ADD COLUMN channel_message_id INTEGER')
            conn.commit()
    except sqlite3.OperationalError:
        pass


def _migrate_posts_lifecycle():
    with db_connect() as conn:
        try:
            conn.execute("ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'published'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE posts ADD COLUMN removed_reason TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE posts ADD COLUMN removed_at TIMESTAMP")
        except sqlite3.OperationalError:
            pass
        conn.execute("UPDATE posts SET status='published' WHERE status IS NULL OR TRIM(status)=''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_status_date ON posts(status, published_date DESC)")
        conn.commit()


def _migrate_staff_access_requests():
    with db_connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS staff_access_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                tg_username TEXT NOT NULL,
                userinfo_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at TIMESTAMP,
                reviewed_by INTEGER
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_req_status_date ON staff_access_requests(status, submitted_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_req_user ON staff_access_requests(user_id)")
        conn.commit()
    try:
        with db_connect() as conn:
            conn.execute("ALTER TABLE staff_access_requests ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            conn.commit()
    except sqlite3.OperationalError:
        pass


def channel_post_url(message_id: int) -> Optional[str]:
    if not message_id:
        return None
    cid = str(CHANNEL_ID).strip()
    if cid.startswith('@'):
        return f"https://t.me/{cid[1:]}/{message_id}"
    if cid.startswith('-100'):
        return f"https://t.me/c/{cid.replace('-100', '')}/{message_id}"
    return f"https://t.me/c/{cid.lstrip('-')}/{message_id}"


def employee_contact_url(username: Optional[str], listing_no: int) -> Optional[str]:
    if not username:
        return None
    u = str(username).strip().lstrip("@")
    if not TG_USERNAME_RE.match(u):
        return None
    text = f"Здравствуйте! Пишу по объявлению №{listing_no}."
    return f"https://t.me/{u}?text={quote(text)}"


TG_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


def normalize_telegram_username(text: str) -> Optional[str]:
    """Возвращает username без @ или None если неверный формат. Пустая строка — снять username."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    low = s.casefold()
    if low in ("-", "нет", "none", "удалить", "очистить", "0"):
        return ""
    for prefix in ("https://t.me/", "http://t.me/", "https://telegram.me/", "http://telegram.me/"):
        if low.startswith(prefix):
            s = s[len(prefix) :].split("/")[0].split("?")[0].strip()
            low = s.casefold()
            break
    s = s.lstrip("@").strip()
    if not s or not TG_USERNAME_RE.match(s):
        return None
    return s


def profile_preview_dummy_data() -> dict:
    """Условный объект для предпросмотра блока контактов в личном кабинете."""
    return {
        "category": "Жилое",
        "realty_type": "Квартира",
        "city": "Ташкент",
        "district": "Пример района",
        "street": "ул. Примерная",
        "house": "1",
        "total_area": 68.5,
        "useful_area": "",
        "rooms": "3",
        "desc": "Пример описания (это не реальный объект, только шаблон).",
        "price_val": 150000000,
        "price_cur": "сум",
    }


def save_user_profile(user_id: int, name: str, phone: str) -> None:
    """Сохранить имя и телефон, не затирая tg_username и role."""
    with db_connect() as conn:
        row = conn.execute("SELECT tg_username, role FROM users WHERE id=?", (user_id,)).fetchone()
        tg = row[0] if row else None
        role = (row[1] if row and row[1] else None) or "staff"
        conn.execute(
            "INSERT OR REPLACE INTO users (id, name, phone, role, tg_username) VALUES (?,?,?,?,?)",
            (user_id, name, phone, role, tg),
        )
        conn.commit()


def save_user_name_only(user_id: int, name: str) -> None:
    """Обновить только ФИО, сохранив телефон и Telegram."""
    with db_connect() as conn:
        row = conn.execute("SELECT phone, tg_username, role FROM users WHERE id=?", (user_id,)).fetchone()
        phone = (row[0] if row else None) or ""
        tg = row[1] if row else None
        role = (row[2] if row and row[2] else None) or "staff"
        conn.execute(
            "INSERT OR REPLACE INTO users (id, name, phone, role, tg_username) VALUES (?,?,?,?,?)",
            (user_id, name, phone, role, tg),
        )
        conn.commit()


def save_user_phone_only(user_id: int, phone: str) -> None:
    """Обновить только телефон, сохранив имя и Telegram."""
    with db_connect() as conn:
        row = conn.execute("SELECT name, tg_username, role FROM users WHERE id=?", (user_id,)).fetchone()
        name = (row[0] if row else None) or "Сотрудник"
        tg = row[1] if row else None
        role = (row[2] if row and row[2] else None) or "staff"
        conn.execute(
            "INSERT OR REPLACE INTO users (id, name, phone, role, tg_username) VALUES (?,?,?,?,?)",
            (user_id, name, phone, role, tg),
        )
        conn.commit()


def set_user_telegram_username(user_id: int, username: Optional[str]) -> None:
    """username: строка без @ или None чтобы очистить поле."""
    with db_connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            conn.execute("UPDATE users SET tg_username=? WHERE id=?", (username, user_id))
        else:
            conn.execute(
                "INSERT INTO users (id, name, phone, role, tg_username) VALUES (?,?,?,?,?)",
                (user_id, "Сотрудник", "", "staff", username),
            )
        conn.commit()


def _post_row_tuple(user_id: int, data: dict, channel_message_id: Optional[int]):
    media_files = data.get('media_files') or []
    media_str = json.dumps(media_files) if isinstance(media_files, list) else str(media_files or '')
    useful = data.get('useful_area')
    if useful == '':
        useful = None
    return (
        user_id,
        data.get('category'),
        data.get('realty_type'),
        data.get('city'),
        data.get('district'),
        data.get('street') or '',
        data.get('house') or '',
        data.get('total_area'),
        useful,
        data.get('rooms') or '',
        data.get('desc') or '',
        data.get('price_val'),
        data.get('price_cur'),
        data.get('media_type') or '',
        media_str,
        channel_message_id,
    )


def insert_post(user_id: int, data: dict, channel_message_id: Optional[int] = None) -> int:
    """Вставка строки posts; возвращает id (номер объявления)."""
    with db_connect() as conn:
        cur = conn.execute(
            """INSERT INTO posts (user_id, category, realty_type, city, district, street, house,
            total_area, useful_area, rooms, desc, price_val, price_cur, media_type, media_files, channel_message_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _post_row_tuple(user_id, data, channel_message_id),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_post_channel_message_id(post_id: int, channel_message_id: Optional[int]) -> None:
    with db_connect() as conn:
        conn.execute(
            "UPDATE posts SET channel_message_id=? WHERE id=?",
            (channel_message_id, post_id),
        )
        conn.commit()


def delete_pending_post(post_id: int) -> None:
    """Удалить черновик, если публикация в канал не удалась (ещё нет channel_message_id)."""
    with db_connect() as conn:
        conn.execute("DELETE FROM posts WHERE id=? AND channel_message_id IS NULL", (post_id,))
        conn.commit()


def fetch_posts_filtered(city: Optional[str], category: Optional[str], realty_type: Optional[str], limit: int, offset: int):
    where = ['1=1']
    params: list = []
    if city:
        where.append('city = ?')
        params.append(city)
    if category:
        where.append('category = ?')
        params.append(category)
    if realty_type:
        where.append('realty_type = ?')
        params.append(realty_type)
    sql = f"""SELECT id, category, realty_type, city, district, price_val, price_cur, channel_message_id, published_date
        FROM posts WHERE {' AND '.join(where)} AND {ACTIVE_POSTS_WHERE}
        ORDER BY published_date DESC LIMIT ? OFFSET ?"""
    params.extend([limit, offset])
    with db_connect() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_category_counts(city: Optional[str]) -> dict:
    where = [ACTIVE_POSTS_WHERE]
    params = []
    if city:
        where.append("city = ?")
        params.append(city)
    sql = f"""SELECT category, COUNT(*) as cnt
              FROM posts
              WHERE {' AND '.join(where)}
              GROUP BY category"""
    with db_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    counts = {str(cat): int(cnt) for cat, cnt in rows if cat is not None}
    counts["_total"] = sum(counts.values())
    return counts


def fetch_city_counts() -> dict:
    sql = """SELECT city, COUNT(*) as cnt
             FROM posts
             WHERE channel_message_id IS NOT NULL AND COALESCE(status, 'published') = 'published'
             GROUP BY city"""
    with db_connect() as conn:
        rows = conn.execute(sql).fetchall()
    counts = {str(city): int(cnt) for city, cnt in rows if city is not None}
    counts["_total"] = sum(counts.values())
    return counts

def is_allowed(u_id):
    if u_id == OWNER_ID:
        return True
    with db_connect() as conn:
        res = conn.execute("SELECT role FROM users WHERE id=?", (u_id,)).fetchone()
        role = (res[0] if res else None) or ""
        return role in ("staff", "admin")


def get_user_role(u_id: int) -> Optional[str]:
    if u_id == OWNER_ID:
        return "owner"
    with db_connect() as conn:
        row = conn.execute("SELECT role FROM users WHERE id=?", (u_id,)).fetchone()
    if not row:
        return None
    return (row[0] or "").strip() or None


def can_open_admin_panel(u_id: int) -> bool:
    if u_id == OWNER_ID:
        return True
    return get_user_role(u_id) == "admin"


def display_user_in_logs(user_id: int) -> str:
    if int(user_id) == int(OWNER_ID):
        return f"Владелец (ID {user_id})"
    with db_connect() as conn:
        row = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    name = (row[0] if row and row[0] else "").strip()
    if name:
        return f"{name} (ID {user_id})"
    return f"ID {user_id}"


def get_pending_access_request(user_id: int):
    with db_connect() as conn:
        return conn.execute(
            """SELECT full_name, phone, tg_username, userinfo_id, submitted_at
               FROM staff_access_requests
               WHERE user_id=? AND status='pending'
               ORDER BY submitted_at DESC LIMIT 1""",
            (user_id,),
        ).fetchone()


def upsert_pending_access_request(user_id: int, full_name: str, phone: str, tg_username: str, userinfo_id: str) -> None:
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT id FROM staff_access_requests WHERE user_id=? AND status='pending' ORDER BY submitted_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE staff_access_requests
                   SET full_name=?, phone=?, tg_username=?, userinfo_id=?, submitted_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (full_name, phone, tg_username, userinfo_id, existing[0]),
            )
        else:
            conn.execute(
                """INSERT INTO staff_access_requests (user_id, full_name, phone, tg_username, userinfo_id, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (user_id, full_name, phone, tg_username, userinfo_id),
            )
        conn.commit()


def fetch_staff_members(limit: int = 12, offset: int = 0):
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, name, phone, tg_username, role
               FROM users
               WHERE role IN ('staff','admin')
               ORDER BY CASE WHEN role='admin' THEN 0 ELSE 1 END, name COLLATE NOCASE, id
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM users WHERE role IN ('staff','admin')").fetchone()[0]
    return rows, int(total or 0)


def count_user_posts(user_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM posts WHERE user_id=?", (user_id,)).fetchone()
    return int((row[0] if row else 0) or 0)


def fetch_pending_requests(limit: int = 12, offset: int = 0):
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, user_id, full_name, phone, tg_username, userinfo_id, submitted_at, priority
               FROM staff_access_requests
               WHERE status='pending'
               ORDER BY priority DESC, submitted_at ASC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM staff_access_requests WHERE status='pending'").fetchone()[0]
    return rows, int(total or 0)


def staff_overview_counts():
    with db_connect() as conn:
        staff_cnt = conn.execute("SELECT COUNT(*) FROM users WHERE role='staff'").fetchone()[0] or 0
        admin_cnt = conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] or 0
        pending_cnt = conn.execute("SELECT COUNT(*) FROM staff_access_requests WHERE status='pending'").fetchone()[0] or 0
    return int(staff_cnt), int(admin_cnt), int(pending_cnt)


def add_log(user_id: int, action: str, details: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO logs (user_id, action, details) VALUES (?, ?, ?)",
            (user_id, action, details),
        )
        conn.commit()


def build_admin_search_posts_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔢 По № объявления", callback_data="adm_search_post_id")
    kb.button(text="👤 По ID сотрудника", callback_data="adm_search_staff_id")
    kb.button(text="⬅️ Назад", callback_data="adm_menu_service")
    return kb.adjust(1).as_markup()


def build_admin_logs_filters_kb(filters: dict):
    mode = filters.get("mode", "all")
    days = int(filters.get("days", 7))
    uid = filters.get("user_id")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{'✅ ' if mode=='all' else ''}Все события", callback_data="adm_logs_mode_all")
    kb.button(text=f"{'✅ ' if mode=='bot' else ''}Действия бота", callback_data="adm_logs_mode_bot")
    kb.button(text=f"{'✅ ' if mode=='admin' else ''}Админ-действия", callback_data="adm_logs_mode_admin")
    kb.button(text=f"{'✅ ' if mode=='errors' else ''}Только ошибки", callback_data="adm_logs_mode_errors")
    kb.button(text=f"{'✅ ' if days==1 else ''}За 1 день", callback_data="adm_logs_days_1")
    kb.button(text=f"{'✅ ' if days==7 else ''}За 7 дней", callback_data="adm_logs_days_7")
    kb.button(text=f"{'✅ ' if days==30 else ''}За 30 дней", callback_data="adm_logs_days_30")
    kb.button(text=f"{'✅ ' if days==0 else ''}За все время", callback_data="adm_logs_days_0")
    kb.button(text=f"🆔 user_id: {uid if uid else 'любой'}", callback_data="adm_logs_set_uid")
    kb.button(text="♻️ Сбросить фильтры", callback_data="adm_logs_reset")
    kb.button(text="📤 Экспорт CSV", callback_data="export_logs_csv")
    kb.button(text="📗 Экспорт Excel", callback_data="export_logs_xlsx")
    kb.button(text="⬅️ Назад", callback_data="adm_menu_logs")
    return kb.adjust(1).as_markup()


def _logs_where_from_filters(filters: dict):
    where = ["1=1"]
    params = []
    mode = filters.get("mode", "all")
    if mode == "errors":
        where.append("(action LIKE ? OR details LIKE ?)")
        params.extend(["%Ошибка%", "%Ошибка%"])
    elif mode == "admin":
        where.append("(action LIKE ? OR action LIKE ? OR action LIKE ? OR action LIKE ? OR action LIKE ?)")
        params.extend(["%админ%", "%заявк%", "%роль%", "%сотрудник%", "%удален%"])
    elif mode == "bot":
        where.append("1=1")
    days = int(filters.get("days", 7))
    if days > 0:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        where.append("timestamp >= ?")
        params.append(since)
    uid = filters.get("user_id")
    if uid:
        where.append("user_id = ?")
        params.append(int(uid))
    return where, params


def fetch_logs_filtered(filters: dict, limit: int = 20):
    where, params = _logs_where_from_filters(filters)
    sql = f"""SELECT id, user_id, action, details, timestamp
              FROM logs
              WHERE {' AND '.join(where)}
              ORDER BY id DESC
              LIMIT ?"""
    params.append(limit)
    with db_connect() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_logs_for_export(filters: dict, limit: int = 5000):
    where, params = _logs_where_from_filters(filters)
    sql = f"""SELECT id, user_id, action, details, timestamp
              FROM logs
              WHERE {' AND '.join(where)}
              ORDER BY id DESC
              LIMIT ?"""
    params.append(limit)
    with db_connect() as conn:
        return conn.execute(sql, params).fetchall()


def build_stats_text() -> str:
    now = datetime.now()
    d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE}").fetchone()[0] or 0
        c1 = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d1,)).fetchone()[0] or 0
        c7 = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d7,)).fetchone()[0] or 0
        c30 = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d30,)).fetchone()[0] or 0
        pending = conn.execute("SELECT COUNT(*) FROM staff_access_requests WHERE status='pending'").fetchone()[0] or 0
        top = conn.execute(
            """SELECT p.user_id, COALESCE(u.name, 'Сотрудник'), COUNT(*) AS cnt
               FROM posts p
               LEFT JOIN users u ON u.id = p.user_id
               WHERE p.channel_message_id IS NOT NULL AND COALESCE(p.status, 'published') = 'published'
               GROUP BY p.user_id
               ORDER BY cnt DESC
               LIMIT 5"""
        ).fetchall()
    lines = [
        "📊 <b>Статистика</b>",
        f"• Всего публикаций: <b>{int(total)}</b>",
        f"• За сегодня: <b>{int(c1)}</b>",
        f"• За 7 дней: <b>{int(c7)}</b>",
        f"• За 30 дней: <b>{int(c30)}</b>",
        f"• Заявки доступа (pending): <b>{int(pending)}</b>",
        "",
        "🏆 <b>Топ сотрудников:</b>",
    ]
    if top:
        for idx, (uid, name, cnt) in enumerate(top, start=1):
            lines.append(f"{idx}. {html.escape(str(name))} (ID {uid}) — {int(cnt)}")
    else:
        lines.append("— данных пока нет")
    return "\n".join(lines)


def build_stats_export_rows():
    now = datetime.now()
    d1 = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    d7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    d30 = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    with db_connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE}").fetchone()[0] or 0)
        c1 = int(conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d1,)).fetchone()[0] or 0)
        c7 = int(conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d7,)).fetchone()[0] or 0)
        c30 = int(conn.execute(f"SELECT COUNT(*) FROM posts WHERE {ACTIVE_POSTS_WHERE} AND published_date >= ?", (d30,)).fetchone()[0] or 0)
        pending = int(conn.execute("SELECT COUNT(*) FROM staff_access_requests WHERE status='pending'").fetchone()[0] or 0)
        top = conn.execute(
            """SELECT p.user_id, COALESCE(u.name, 'Сотрудник'), COUNT(*) AS cnt
               FROM posts p
               LEFT JOIN users u ON u.id = p.user_id
               WHERE p.channel_message_id IS NOT NULL AND COALESCE(p.status, 'published') = 'published'
               GROUP BY p.user_id
               ORDER BY cnt DESC
               LIMIT 20"""
        ).fetchall()
    head = [
        ["метрика", "значение"],
        ["всего_публикаций", total],
        ["публикаций_сегодня", c1],
        ["публикаций_7д", c7],
        ["публикаций_30д", c30],
        ["pending_заявок", pending],
        [],
        ["top_user_id", "top_name", "top_posts"],
    ]
    for uid, name, cnt in top:
        head.append([uid, name, int(cnt)])
    return head


def csv_bytes_from_rows(rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8-sig")


def xlsx_bytes_from_rows(rows) -> bytes:
    try:
        from openpyxl import Workbook
    except Exception:
        return b""
    wb = Workbook()
    ws = wb.active
    ws.title = "export"
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()

init_db()
_migrate_posts_lifecycle()

# --- СПИСКИ ГОРОДОВ ---
CITIES = {
    "Ташкент": {
        "Ташкент": [
            "Бектемирский район",
            "Мирзо-Улугбекский район",
            "Мирободский район",
            "Сергелийский район",
            "Учтепинский район",
            "Чиланзарский район",
            "Шайхантахурский район",
            "Алмазарский район",
            "Яккасарайский район",
            "Яшнабадский район",
            "Юнусабадский район",
            "Янгихаётский район",
        ],
        "Область": [
            "Бекабадский район",
            "Бостанлыкский район",
            "Букинский район",
            "Зангиатинский район",
            "Кибрайский район",
            "Куйичирчикский район",
            "Паркентский район",
            "Пскентский район",
            "Ташкентский район",
            "Уртачирчикский район",
            "Чиназский район",
            "Юкоричирчикский район",
            "Янгиюльский район",
        ],
    },
    "Самарканд": ["Сиабский", "Багишамальский", "Железнодорожный"],
    "Каракалпакстан": [],
    "Другой город": []
}

CITY_ORDER = list(CITIES.keys())
SEARCH_CATEGORIES = ["Жилое", "Нежилое", "Спецтехника", "Оборудование"]


def city_district_groups(city: str) -> list:
    city_data = CITIES.get(city)
    if isinstance(city_data, dict):
        return list(city_data.keys())
    return []


def city_districts(city: str, group: Optional[str] = None) -> list:
    city_data = CITIES.get(city, [])
    if isinstance(city_data, dict):
        return city_data.get(group, [])
    return city_data

BTN_ROLE_STAFF = "👔 Сотрудник"
BTN_ROLE_CLIENT = "🛒 Клиент"
BTN_ROLE_SWITCH = "↩️ Сменить режим"
BTN_SEARCH = "🔍 Поиск объявлений"
POST_STATUS_PUBLISHED = "published"
POST_STATUS_DELETED = "deleted"
POST_STATUS_SOLD = "sold"
REMOVE_REASON_SOLD = "sold"
REMOVE_REASON_ERROR = "error"
REMOVE_REASON_FIX = "fix"
ACTIVE_POSTS_WHERE = "channel_message_id IS NOT NULL AND COALESCE(status, 'published') = 'published'"


def role_select_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_ROLE_STAFF)
    kb.button(text=BTN_ROLE_CLIENT)
    return kb.adjust(2).as_markup(resize_keyboard=True)


def client_menu_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_SEARCH)
    kb.button(text=BTN_ROLE_SWITCH)
    return kb.adjust(1).as_markup(resize_keyboard=True)

# --- СОСТОЯНИЯ ---
class PostState(StatesGroup):
    category = State()
    realty_type = State()
    city = State()
    district = State()
    street = State()
    house = State()
    total_area = State()
    useful_area = State()
    rooms = State()
    desc = State()
    price_val = State()
    price_cur = State()
    media_choice = State()
    media_file = State()
    preview = State()

class ProfileState(StatesGroup):
    name = State()
    phone = State()


class ProfileTgState(StatesGroup):
    value = State()


class ProfileEditState(StatesGroup):
    """Пошаговое изменение одного поля из меню «Изменить данные»."""
    name = State()
    phone = State()


class AdminState(StatesGroup):
    add_id = State()


class AdminDeletePostState(StatesGroup):
    """Удаление объявления из БД (владелец)."""
    wait_id = State()


class AccessRequestState(StatesGroup):
    full_name = State()
    phone = State()
    tg_username = State()
    userinfo_id = State()


class AdminStaffEditState(StatesGroup):
    value = State()


class AdminLogsState(StatesGroup):
    wait_user_id = State()


class AdminSearchPostsState(StatesGroup):
    wait_post_id = State()
    wait_staff_id = State()


class SearchState(StatesGroup):
    """Пошаговый поиск для клиента (город → категория)."""
    pick_city = State()
    pick_category = State()


class ImportState(StatesGroup):
    wait_csv_file = State()

# --- ФУНКЦИЯ ЧИСТКИ ЧАТА ---
async def send_step(m_obj, text, reply_markup=None, state: FSMContext = None):
    try:
        # Определяем chat_id в зависимости от типа объекта
        chat_id = m_obj.chat.id if hasattr(m_obj, 'chat') else m_obj.message.chat.id
        
        last_msg = None
        if state is not None:
            data = await state.get_data()
            last_msg = data.get("last_msg_id")
        
        if last_msg:
            try:
                await bot.delete_message(chat_id, last_msg)
            except Exception as e:
                # Логируем ошибку, но не падаем
                logging.warning(f"Failed to delete message {last_msg}: {e}")
        
        new_msg = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
        if state is not None:
            await state.update_data(last_msg_id=new_msg.message_id)
            await remember_cleanup_message(state, new_msg, is_user=False)
            counter_data = await state.get_data()
            cleanup_paused = bool(counter_data.get(AUTO_CLEANUP_PAUSED_KEY, False))
            if cleanup_paused:
                press_counter = 0
            else:
                press_counter = int(counter_data.get(CLEANUP_PRESS_COUNTER_KEY, 0)) + 1
                if press_counter >= AUTO_CLEANUP_EVERY_BUTTON_PRESSES:
                    await auto_cleanup_recent_messages(chat_id, state)
                    press_counter = 0
            await state.update_data(**{CLEANUP_PRESS_COUNTER_KEY: press_counter})
    except Exception as e:
        logging.error(f"Error in send_step: {e}")
        # Если что-то пошло не так, отправляем сообщение без удаления предыдущего
        if hasattr(m_obj, 'chat'):
            await bot.send_message(m_obj.chat.id, text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await bot.send_message(m_obj.message.chat.id, text, reply_markup=reply_markup, parse_mode="HTML")

def main_menu_kb(u_id):
    """Меню сотрудника (после выбора «Сотрудник»)."""
    kb = ReplyKeyboardBuilder()
    kb.button(text="➕ Создать карточку объекта")
    kb.button(text="👤 Личный кабинет")
    if u_id == OWNER_ID or get_user_role(u_id) == "admin":
        kb.button(text="⚙️ Админ-панель")
    return kb.adjust(1).as_markup(resize_keyboard=True)

def back_btn():
    return InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data="go_back").as_markup()


def build_client_city_kb():
    counts = fetch_city_counts()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Любой город ({counts.get('_total', 0)})", callback_data="cl_ci_any")
    for i, city in enumerate(CITY_ORDER):
        kb.button(text=f"{city} ({counts.get(city, 0)})"[:30], callback_data=f"cl_ci_{i}")
    kb.button(text="Назад", callback_data="cl_cancel")
    return kb.adjust(2).as_markup()


def build_client_category_kb(city: Optional[str] = None):
    counts = fetch_category_counts(city)
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Любая категория ({counts.get('_total', 0)})", callback_data="cl_ca_any")
    for i, cat in enumerate(SEARCH_CATEGORIES):
        kb.button(text=f"{cat} ({counts.get(cat, 0)})", callback_data=f"cl_ca_{i}")
    kb.button(text="Назад", callback_data="cl_cancel")
    return kb.adjust(2).as_markup()


def parse_limit_from_command(text: str, default: int = 10, max_limit: int = 50) -> int:
    parts = (text or "").split()
    if len(parts) < 2:
        return default
    try:
        value = int(parts[1])
        return max(1, min(value, max_limit))
    except ValueError:
        return default


def media_progress_text(items: list) -> str:
    photos = sum(1 for x in items if isinstance(x, dict) and x.get("type") == "photo")
    videos = sum(1 for x in items if isinstance(x, dict) and x.get("type") == "video")
    total = len(items)
    return f"📎 Загружено: <b>{total}/10</b>\n📸 Фото: {photos} | 🎥 Видео: {videos}"


def build_media_manage_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="media_done")
    kb.button(text="🗑 Удалить последнюю", callback_data="media_remove_last")
    kb.button(text="♻ Очистить всё", callback_data="media_clear_all")
    kb.button(text="⬅️ Назад", callback_data="back_to_media_choice")
    return kb.adjust(1).as_markup()


def build_admin_panel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика >", callback_data="adm_menu_stats")
    kb.button(text="📋 Логи >", callback_data="adm_menu_logs")
    kb.button(text="👥 Сотрудники >", callback_data="adm_menu_staff")
    kb.button(text="🗄 База / импорт >", callback_data="adm_menu_data")
    kb.button(text="⚙️ Служебное >", callback_data="adm_menu_service")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    return kb.adjust(1).as_markup()


def build_admin_stats_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="adm_stats")
    kb.button(text="📤 Экспорт stats CSV", callback_data="export_stats")
    kb.button(text="📗 Экспорт stats Excel", callback_data="export_stats_xlsx")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_logs_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Логи", callback_data="adm_logs")
    kb.button(text="📤 Экспорт logs CSV", callback_data="export_logs")
    kb.button(text="📗 Экспорт logs Excel", callback_data="export_logs_xlsx")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_staff_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Сотрудники", callback_data="adm_staff_list")
    kb.button(text="📥 Заявки доступа", callback_data="adm_access_requests")
    kb.button(text="➕ Добавить ID сотрудника", callback_data="adm_add")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_staff_entry_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Подать заявку на доступ", callback_data="req_start")
    kb.button(text="⬅️ К выбору режима", callback_data="req_cancel")
    return kb.adjust(1).as_markup()


def build_staff_list_kb(rows, page: int, total: int, page_size: int):
    kb = InlineKeyboardBuilder()
    for user_id, name, _phone, _tg, role in rows:
        role_mark = "👑" if role == "admin" else "👤"
        title = (name or "Сотрудник").strip()
        kb.button(text=f"{role_mark} {title} (ID {user_id})", callback_data=f"adm_staff_open_{user_id}")
    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"adm_staff_page_{page - 1}")
    if (page + 1) * page_size < total:
        kb.button(text="Вперед ➡️", callback_data=f"adm_staff_page_{page + 1}")
    kb.button(text="↩️ К разделу Сотрудники", callback_data="adm_menu_staff")
    return kb.adjust(1).as_markup()


def build_staff_member_actions_kb(user_id: int, role: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Изменить ФИО", callback_data=f"adm_staff_edit_name_{user_id}")
    kb.button(text="📞 Изменить телефон", callback_data=f"adm_staff_edit_phone_{user_id}")
    kb.button(text="🔗 Изменить телеграм", callback_data=f"adm_staff_edit_tg_{user_id}")
    if role == "admin":
        kb.button(text="⬇️ Сделать сотрудником", callback_data=f"adm_staff_role_staff_{user_id}")
    else:
        kb.button(text="⬆️ Сделать администратором", callback_data=f"adm_staff_role_admin_{user_id}")
    kb.button(text="🚫 Снять доступ сотрудника", callback_data=f"adm_staff_remove_{user_id}")
    kb.button(text="⬅️ К списку сотрудников", callback_data="adm_staff_list")
    return kb.adjust(1).as_markup()


def role_label_ru(role: str, user_id: Optional[int] = None) -> str:
    if user_id == OWNER_ID:
        return "Хозяин"
    role_norm = (role or "").strip().lower()
    if role_norm == "admin":
        return "Администратор"
    if role_norm == "staff":
        return "Сотрудник"
    return role or "—"


def build_access_requests_list_kb(rows, page: int, total: int, page_size: int):
    kb = InlineKeyboardBuilder()
    for req_id, user_id, full_name, _phone, _tg, _uid, _submitted, priority in rows:
        title = (full_name or "Без имени").strip()
        p = "⭐ " if int(priority or 0) > 0 else ""
        kb.button(text=f"{p}📥 {title} (ID {user_id})", callback_data=f"adm_req_open_{req_id}")
    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"adm_req_page_{page - 1}")
    if (page + 1) * page_size < total:
        kb.button(text="Вперед ➡️", callback_data=f"adm_req_page_{page + 1}")
    kb.button(text="↩️ К разделу Сотрудники", callback_data="adm_menu_staff")
    return kb.adjust(1).as_markup()


def build_access_request_actions_kb(req_id: int, is_priority: bool):
    kb = InlineKeyboardBuilder()
    if is_priority:
        kb.button(text="☆ Убрать приоритет", callback_data=f"adm_req_priority_0_{req_id}")
    else:
        kb.button(text="⭐ В приоритет", callback_data=f"adm_req_priority_1_{req_id}")
    kb.button(text="✅ Одобрить -> сотрудник", callback_data=f"adm_req_approve_{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_req_reject_{req_id}")
    kb.button(text="⬅️ К заявкам", callback_data="adm_access_requests")
    return kb.adjust(1).as_markup()


def build_request_submit_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 Подать заявку", callback_data="req_submit")
    kb.button(text="❌ Отмена", callback_data="req_cancel")
    return kb.adjust(1).as_markup()


def build_admin_data_kb(user_id: int = 0):
    kb = InlineKeyboardBuilder()
    kb.button(text="🗄 DB check", callback_data="adm_dbcheck")
    kb.button(text="📥 Импорт CSV", callback_data="adm_importcsv")
    if user_id and int(user_id) == int(OWNER_ID):
        kb.button(
            text="📤 Текущая БД бота → data/ (Git)",
            callback_data="adm_db_export",
        )
        kb.button(
            text="📥 data/ (Git) → в рабочую БД",
            callback_data="adm_db_import_ask",
        )
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_db_import_confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, заменить рабочую БД", callback_data="adm_db_import_yes")
    kb.button(text="❌ Отмена", callback_data="adm_db_import_no")
    return kb.adjust(1).as_markup()


def build_admin_service_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Тех. перерыв (рассылка)", callback_data="adm_maint")
    kb.button(text="🔎 Поиск публикаций", callback_data="adm_search_posts")
    kb.button(text="🗑 Удалить объявление (БД)", callback_data="adm_delete_post")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_dbcheck_text(limit: int) -> str:
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, city, category, realty_type, price_val, price_cur, channel_message_id, published_date
               FROM posts ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    if not rows:
        return "База объявлений пока пустая."

    lines = [f"📦 <b>Последние {len(rows)} записей posts:</b>\n"]
    for r in rows:
        post_id, city, cat, rtype, pval, pcur, ch_mid, pdate = r
        price = _format_price_row(pval, pcur)
        marker = "✅" if ch_mid else "⚠️"
        lines.append(f"{marker} #{post_id} | {city}/{cat}/{rtype} | {price} | ch_mid={ch_mid or '-'} | {pdate}")
    return "\n".join(lines)


async def show_import_csv_prompt(m_obj, state: FSMContext):
    await state.set_state(ImportState.wait_csv_file)
    await send_step(
        m_obj,
        "Пришлите CSV-файл документом.\n\n"
        "Обязательные колонки: city, category, realty_type\n"
        "Опционально: district, street, house, total_area, useful_area, rooms, desc, "
        "price_val, price_cur, media_type, media_files, channel_message_id, user_id\n\n"
        "Для выхода: /cancel или напишите «отмена».",
        state=state,
    )


# --- ВЫБОР РЕЖИМА: СОТРУДНИК / КЛИЕНТ ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) > 1:
        arg = parts[1].strip().lower()
        if arg in ("filter", "client", "search"):
            await state.update_data(app_mode="client")
            await m.answer(
                "🛒 <b>Режим клиента</b>\nИщите объявления по фильтрам. Канал: объекты открываются по ссылке.",
                reply_markup=client_menu_kb(),
                parse_mode="HTML",
            )
            return
    await m.answer(
        "📊 <b>Kapital Assets</b>",
        reply_markup=role_select_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_ROLE_STAFF)
async def role_pick_staff(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id):
        pending = get_pending_access_request(m.from_user.id)
        pending_note = ""
        if pending:
            pending_note = f"\n\n🕓 Ваша заявка уже ожидает проверки (от {pending[4]}). Можно отправить заново."
        await m.answer(
            "❌ Доступ сотрудника не оформлен.\n"
            "Чтобы получить доступ, пройдите регистрацию и отправьте заявку администратору."
            + pending_note,
            reply_markup=build_staff_entry_kb(),
            parse_mode="HTML",
        )
        return
    await state.update_data(app_mode="staff")
    await remember_cleanup_message(state, m, is_user=True)
    sent = await m.answer(
        "👔 <b>Режим сотрудника</b>\nПубликация карточек и личный кабинет.",
        reply_markup=main_menu_kb(m.from_user.id),
        parse_mode="HTML",
    )
    await remember_cleanup_message(state, sent, is_user=False)


@dp.callback_query(F.data == "req_cancel")
async def req_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer("Ок, выберите режим:", reply_markup=role_select_kb(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "req_start")
async def req_start(c: types.CallbackQuery, state: FSMContext):
    if is_allowed(c.from_user.id):
        await c.answer("У вас уже есть доступ сотрудника.", show_alert=True)
        return
    await state.set_state(AccessRequestState.full_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="req_cancel")
    await send_step(
        c.message,
        "📝 <b>Регистрация сотрудника — шаг 1/4</b>\nВведите ФИО:",
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.message(AccessRequestState.full_name)
async def req_full_name(m: types.Message, state: FSMContext):
    value = (m.text or "").strip()
    if len(value) < 5:
        await send_step(m, "❌ Введите полное ФИО (минимум 5 символов).", state=state)
        return
    await state.update_data(req_full_name=value)
    await state.set_state(AccessRequestState.phone)
    await send_step(m, "📝 <b>Шаг 2/4</b>\nВведите номер телефона в формате <code>+998XXXXXXXXX</code>:", state=state)


@dp.message(AccessRequestState.phone)
async def req_phone(m: types.Message, state: FSMContext):
    value = (m.text or "").strip()
    if not validate_phone(value):
        await send_step(m, "❌ Неверный формат. Пример: <code>+998901234567</code>.", state=state)
        return
    await state.update_data(req_phone=value)
    await state.set_state(AccessRequestState.tg_username)
    await send_step(
        m,
        "📝 <b>Шаг 3/4</b>\nВведите ваш Telegram username:\n"
        "• <code>@username</code> или <code>https://t.me/username</code>",
        state=state,
    )


@dp.message(AccessRequestState.tg_username)
async def req_tg(m: types.Message, state: FSMContext):
    norm = normalize_telegram_username(m.text or "")
    if norm is None or norm == "":
        await send_step(m, "❌ Неверный username. Укажите @username (5–32 символа).", state=state)
        return
    await state.update_data(req_tg=norm)
    await state.set_state(AccessRequestState.userinfo_id)
    await send_step(
        m,
        "📝 <b>Шаг 4/4</b>\nВведите ваш ID из @userinfobot (только цифры):",
        state=state,
    )


@dp.message(AccessRequestState.userinfo_id)
async def req_userinfo_id(m: types.Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await send_step(m, "❌ Нужен числовой ID из @userinfobot.", state=state)
        return
    if int(raw) != m.from_user.id:
        await send_step(
            m,
            "❌ ID не совпадает с вашим Telegram ID.\n"
            "Проверьте ID в @userinfobot и отправьте снова.",
            state=state,
        )
        return
    await state.update_data(req_userinfo_id=raw)
    d = await state.get_data()
    text = (
        "<b>Проверьте данные заявки:</b>\n\n"
        f"👤 ФИО: <b>{html.escape(d.get('req_full_name', ''))}</b>\n"
        f"📞 Телефон: <b>{html.escape(d.get('req_phone', ''))}</b>\n"
        f"🔗 Telegram: <b>@{html.escape(d.get('req_tg', ''))}</b>\n"
        f"🆔 ID: <b>{html.escape(d.get('req_userinfo_id', ''))}</b>\n\n"
        "Нажмите «Подать заявку»."
    )
    await send_step(m, text, build_request_submit_kb(), state)


@dp.callback_query(F.data == "req_submit")
async def req_submit(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    full_name = d.get("req_full_name")
    phone = d.get("req_phone")
    tg = d.get("req_tg")
    userinfo_id = d.get("req_userinfo_id")
    if not all([full_name, phone, tg, userinfo_id]):
        await c.answer("Сначала пройдите все шаги регистрации.", show_alert=True)
        return
    upsert_pending_access_request(c.from_user.id, full_name, phone, tg, userinfo_id)
    await state.clear()
    await c.message.answer(
        "✅ Заявка отправлена администратору.\n"
        "Ожидайте одобрения. После этого режим «Сотрудник» станет доступен.",
        reply_markup=role_select_kb(),
        parse_mode="HTML",
    )
    await c.answer("Заявка отправлена")


@dp.message(F.text == BTN_ROLE_CLIENT)
async def role_pick_client(m: types.Message, state: FSMContext):
    await state.update_data(
        **{
            CLEANUP_BOT_IDS_KEY: [],
            CLEANUP_USER_IDS_KEY: [],
            CLEANUP_ORDER_IDS_KEY: [],
            CLEANUP_PRESS_COUNTER_KEY: 0,
        }
    )
    await state.update_data(app_mode="client")
    await m.answer(
        "🛒 <b>Режим клиента</b>\nНиже — поиск по объявлениям в канале (фильтры).",
        reply_markup=client_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_ROLE_SWITCH)
async def role_switch(m: types.Message, state: FSMContext):
    await clear_state_preserve_cleanup(state)
    await state.update_data(
        **{
            CLEANUP_BOT_IDS_KEY: [],
            CLEANUP_USER_IDS_KEY: [],
            CLEANUP_ORDER_IDS_KEY: [],
            CLEANUP_PRESS_COUNTER_KEY: 0,
        }
    )
    await m.answer("Выберите режим:", reply_markup=role_select_kb(), parse_mode="HTML")


@dp.message(F.text == BTN_SEARCH)
async def client_search_start(m: types.Message, state: FSMContext):
    if (await state.get_data()).get("app_mode") != "client":
        await m.answer("Сначала нажмите «Клиент» или откройте бота по ссылке из канала.", reply_markup=role_select_kb(), parse_mode="HTML")
        return
    await state.set_state(SearchState.pick_city)
    await send_step(
        m,
        "🏙 <b>Шаг 1 из 2 — город</b>\nВыберите город или «Любой город».",
        build_client_city_kb(),
        state,
    )


@dp.callback_query(F.data == "cl_cancel")
async def client_search_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    d = await state.get_data()
    mode = d.get("app_mode", "client")
    await state.clear()
    if mode == "client":
        await state.update_data(app_mode="client")
    await c.message.answer("Ок, назад.", reply_markup=client_menu_kb() if mode == "client" else main_menu_kb(c.from_user.id), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("cl_ci_"))
async def client_search_after_city(c: types.CallbackQuery, state: FSMContext):
    raw = c.data.replace("cl_ci_", "")
    if raw == "any":
        city = None
    else:
        city = CITY_ORDER[int(raw)]
    await state.update_data(sf_city=city)
    await state.set_state(SearchState.pick_category)
    city_label = city if city else "любой"
    await send_step(
        c.message,
        f"🏷 <b>Шаг 2 из 2 — категория</b>\nГород: <b>{city_label}</b>",
        build_client_category_kb(city),
        state,
    )
    await c.answer()


def _format_price_row(pval, pcur) -> str:
    if pval is None or not pcur:
        return "—"
    try:
        return f"{float(pval):,.0f} {pcur}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


@dp.callback_query(F.data.startswith("cl_ca_"))
async def client_search_run(c: types.CallbackQuery, state: FSMContext):
    raw = c.data.replace("cl_ca_", "")
    city = (await state.get_data()).get("sf_city")
    if raw == "any":
        category = None
    else:
        category = SEARCH_CATEGORIES[int(raw)]
    rows = fetch_posts_filtered(city, category, None, 10, 0)
    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="Изменить фильтр", callback_data="cl_retry")
        kb.button(text="В меню клиента", callback_data="cl_done")
        await send_step(
            c.message,
            "По таким условиям объявлений нет.\n\n"
            "<i>В поиске только карточки, сохранённые при публикации сотрудником (с ссылкой на пост в канале).</i>",
            kb.adjust(1).as_markup(),
            state,
        )
        await c.answer()
        return
    lines = ["<b>Результаты:</b>\n"]
    ib = InlineKeyboardBuilder()
    for row in rows:
        _id, cat, rtype, rcity, district, pval, pcur, ch_mid, _pdate = row
        price_h = _format_price_row(pval, pcur)
        lines.append(
            f"• <b>№{_id}</b> <b>{cat}</b> / {rtype}\n  📍 {rcity}, {district or '—'}\n  💰 {price_h}\n"
        )
        url = channel_post_url(ch_mid)
        if url:
            short = (rtype or "объект")[:14]
            ib.button(text=f"№{_id} · {short}", url=url)
    ib.button(text="Новый поиск", callback_data="cl_retry")
    ib.button(text="В меню клиента", callback_data="cl_done")
    await send_step(c.message, "\n".join(lines), ib.adjust(1).as_markup(), state)
    await c.answer()


@dp.callback_query(F.data == "cl_retry")
async def client_search_retry(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.pick_city)
    await send_step(
        c.message,
        "🏙 <b>Шаг 1 из 2 — город</b>\nВыберите город или «Любой город».",
        build_client_city_kb(),
        state,
    )
    await c.answer()


@dp.callback_query(F.data == "cl_done")
async def client_search_done(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.clear()
    await state.update_data(app_mode="client")
    await c.message.answer("Меню клиента.", reply_markup=client_menu_kb(), parse_mode="HTML")
    await c.answer()


@dp.message(Command("dbcheck"))
async def dbcheck_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID:
        await m.answer("❌ Команда доступна только владельцу.")
        return
    limit = parse_limit_from_command(m.text or "", default=10, max_limit=50)
    await m.answer(build_dbcheck_text(limit), parse_mode="HTML")


@dp.message(Command("importcsv"))
async def import_csv_start(m: types.Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        await m.answer("❌ Команда доступна только владельцу.")
        return
    await show_import_csv_prompt(m, state)


@dp.message(ImportState.wait_csv_file, F.document)
async def import_csv_file(m: types.Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        await state.clear()
        return
    file = await bot.get_file(m.document.file_id)
    file_bytes = await bot.download_file(file.file_path)
    raw = file_bytes.read()
    text = None
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        await m.answer("❌ Не удалось прочитать CSV. Сохраните файл как UTF-8.")
        return
    reader = csv.DictReader(text.splitlines())
    required = {"city", "category", "realty_type"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        await m.answer("❌ В CSV не хватает обязательных колонок: city, category, realty_type")
        return

    inserted = 0
    with db_connect() as conn:
        for row in reader:
            try:
                def _to_float(v):
                    if v is None or str(v).strip() == "":
                        return None
                    return float(str(v).replace(",", "."))

                def _to_int(v):
                    if v is None or str(v).strip() == "":
                        return None
                    return int(v)

                conn.execute(
                    """INSERT INTO posts (
                        user_id, category, realty_type, city, district, street, house,
                        total_area, useful_area, rooms, desc, price_val, price_cur, media_type, media_files, channel_message_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _to_int(row.get("user_id")) or OWNER_ID,
                        (row.get("category") or "").strip(),
                        (row.get("realty_type") or "").strip(),
                        (row.get("city") or "").strip(),
                        (row.get("district") or "").strip(),
                        (row.get("street") or "").strip(),
                        (row.get("house") or "").strip(),
                        _to_float(row.get("total_area")),
                        _to_float(row.get("useful_area")),
                        (row.get("rooms") or "").strip(),
                        (row.get("desc") or "").strip(),
                        _to_float(row.get("price_val")),
                        (row.get("price_cur") or "").strip(),
                        (row.get("media_type") or "").strip(),
                        (row.get("media_files") or "").strip(),
                        _to_int(row.get("channel_message_id")),
                    ),
                )
                inserted += 1
            except Exception as e:
                logging.warning(f"CSV row skipped: {e}; row={row}")
        conn.commit()

    await state.clear()
    await state.update_data(app_mode="staff")
    await m.answer(f"✅ Импорт завершён. Добавлено строк: <b>{inserted}</b>", parse_mode="HTML", reply_markup=main_menu_kb(m.from_user.id))


@dp.message(ImportState.wait_csv_file, Command("cancel"))
@dp.message(ImportState.wait_csv_file, F.text.casefold() == "отмена")
@dp.message(ImportState.wait_csv_file, F.text.casefold() == "cancel")
async def import_csv_cancel(m: types.Message, state: FSMContext):
    await state.clear()
    await state.update_data(app_mode="staff")
    await m.answer("Импорт CSV отменён.", reply_markup=main_menu_kb(m.from_user.id))


@dp.message(ImportState.wait_csv_file)
async def import_csv_wrong_input(m: types.Message):
    await m.answer("Пришлите CSV именно как <b>документ</b> (файл), не текстом.", parse_mode="HTML")


# --- ГЛОБАЛЬНЫЕ КНОПКИ (СБРОС ЗАВИСАНИЙ) ---
@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(m: types.Message, state: FSMContext):
    if not can_open_admin_panel(m.from_user.id):
        await m.answer("Нет доступа к админ-панели.")
        return
    await remember_cleanup_message(state, m, is_user=True)
    await clear_state_preserve_cleanup(state)
    await state.update_data(app_mode="staff")
    await send_step(m, "⚙️ <b>Панель администратора</b>", build_admin_panel_kb(), state)


@dp.callback_query(F.data == "adm_menu_stats")
async def adm_menu_stats(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, "📊 <b>Раздел: Статистика</b>", build_admin_stats_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_menu_logs")
async def adm_menu_logs(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, "📋 <b>Раздел: Логи</b>", build_admin_logs_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_menu_staff")
async def adm_menu_staff(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    staff_cnt, admin_cnt, pending_cnt = staff_overview_counts()
    text = (
        "👥 <b>Раздел: Сотрудники</b>\n\n"
        f"👤 Сотрудники: <b>{staff_cnt}</b>\n"
        f"👑 Администраторы: <b>{admin_cnt}</b>\n"
        f"📥 Заявки на доступ: <b>{pending_cnt}</b>"
    )
    await send_step(c, text, build_admin_staff_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_menu_data")
async def adm_menu_data(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await send_step(
        c,
        "🗄 <b>Раздел: База / импорт</b>",
        build_admin_data_kb(c.from_user.id),
        state=state,
    )
    await c.answer()


@dp.callback_query(F.data == "adm_db_export")
async def adm_db_export(c: types.CallbackQuery, state: FSMContext):
    if int(c.from_user.id) != int(OWNER_ID):
        await c.answer("Только для владельца бота.", show_alert=True)
        return
    try:
        export_live_to_repo_snapshot()
    except Exception as e:
        logging.exception("adm_db_export")
        await c.answer(f"Ошибка: {e}", show_alert=True)
        return
    await c.answer("Готово: бот → data/", show_alert=True)
    p = str(SNAPSHOT_DB).replace("\\", "/")
    await c.message.answer(
        "✅ <b>Текущая рабочая БД бота</b> перелита в <code>data/database.db</code> (файл для Git).\n\n"
        f"<code>{html.escape(p)}</code>\n\n"
        "Сам Git не трогает: при необходимости <code>git add data/</code> и commit.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "adm_db_import_ask")
async def adm_db_import_ask(c: types.CallbackQuery, state: FSMContext):
    if int(c.from_user.id) != int(OWNER_ID):
        await c.answer("Только для владельца бота.", show_alert=True)
        return
    if not SNAPSHOT_DB.is_file():
        await c.answer("Нет data/database.db", show_alert=True)
        return
    await send_step(
        c,
        "⚠️ <b>Импорт снимка из data/database.db</b>\n\n"
        "Текущая <b>рабочая</b> база будет <b>полностью заменена</b> содержимым файла из репозитория.\n\n"
        "Продолжить?",
        build_db_import_confirm_kb(),
        state=state,
    )
    await c.answer()


@dp.callback_query(F.data == "adm_db_import_no")
async def adm_db_import_no(c: types.CallbackQuery, state: FSMContext):
    await send_step(
        c,
        "🗄 <b>Раздел: База / импорт</b>",
        build_admin_data_kb(c.from_user.id),
        state=state,
    )
    await c.answer()


@dp.callback_query(F.data == "adm_db_import_yes")
async def adm_db_import_yes(c: types.CallbackQuery, state: FSMContext):
    if int(c.from_user.id) != int(OWNER_ID):
        await c.answer("Только для владельца бота.", show_alert=True)
        return
    if not SNAPSHOT_DB.is_file():
        await c.answer("Нет data/database.db", show_alert=True)
        return
    try:
        import_repo_snapshot_to_live()
    except Exception as e:
        logging.exception("adm_db_import_yes")
        await c.answer(f"Ошибка: {e}", show_alert=True)
        return
    await c.answer("Рабочая БД обновлена", show_alert=True)
    await send_step(
        c,
        "✅ <b>Рабочая БД обновлена</b> из <code>data/database.db</code>.\n\n"
        "Если бот вёл себя странно, при тяжёлой нагрузке — перезапустите процесс бота.",
        build_admin_data_kb(c.from_user.id),
        state=state,
    )


@dp.callback_query(F.data == "adm_menu_service")
async def adm_menu_service(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await send_step(c, "⚙️ <b>Раздел: Служебное</b>", build_admin_service_kb(), state=state)
    await c.answer()


def _profile_inline_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Изменить данные", callback_data="edit_p")
    kb.button(text="📁 Мои публикации", callback_data="my_posts")
    kb.button(text="🔗 Указать Telegram", callback_data="profile_set_tg")
    kb.button(text="👁 Предпросмотр карточки", callback_data="profile_preview_card")
    kb.button(text=BTN_ROLE_SWITCH, callback_data="profile_switch_mode")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    return kb.adjust(2).as_markup()


async def send_profile_screen(m_obj, user_id: int, state: FSMContext):
    with db_connect() as conn:
        p = conn.execute("SELECT name, phone, tg_username FROM users WHERE id=?", (user_id,)).fetchone()
    if not p or not p[0]:
        await send_step(m_obj, "Введите ваше Имя и Фамилию:", state=state)
        await state.set_state(ProfileState.name)
        return
    parts = [f"👤 <b>Профиль:</b> {html.escape(p[0])}", f"📞 <b>Тел:</b> {html.escape(p[1] or '')}"]
    if p[2]:
        u = str(p[2]).strip().lstrip("@")
        parts.append(f"🔗 <b>Telegram:</b> @{html.escape(u)}")
    await send_step(m_obj, "\n".join(parts), _profile_inline_kb(), state)


@dp.message(F.text == "👤 Личный кабинет")
async def profile_handler(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id):
        await m.answer(
            "❌ Личный кабинет сотрудника доступен после одобрения заявки.",
            reply_markup=build_staff_entry_kb(),
            parse_mode="HTML",
        )
        return
    prev = (await state.get_data()).get("app_mode", "staff")
    await remember_cleanup_message(state, m, is_user=True)
    await clear_state_preserve_cleanup(state)
    await state.update_data(app_mode=prev)
    await send_profile_screen(m, m.from_user.id, state)


@dp.callback_query(F.data == "profile_switch_mode")
async def profile_switch_mode(c: types.CallbackQuery, state: FSMContext):
    await clear_state_preserve_cleanup(state)
    await state.update_data(
        **{
            CLEANUP_BOT_IDS_KEY: [],
            CLEANUP_USER_IDS_KEY: [],
            CLEANUP_ORDER_IDS_KEY: [],
            CLEANUP_PRESS_COUNTER_KEY: 0,
        }
    )
    await c.message.answer("Выберите режим:", reply_markup=role_select_kb(), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "edit_p")
async def edit_profile_start(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(app_mode=d.get("app_mode", "staff"))
    await state.set_state(None)
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 ФИО", callback_data="edit_field_name")
    kb.button(text="📞 Телефон", callback_data="edit_field_phone")
    kb.button(text="🔗 Telegram", callback_data="profile_set_tg")
    kb.button(text="⬅️ Отмена", callback_data="cancel_profile_edit")
    await send_step(
        c.message,
        "<b>Изменить данные</b>\nВыберите, что правим:",
        kb.adjust(2).as_markup(),
        state,
    )
    await c.answer()


@dp.callback_query(F.data == "edit_field_name")
async def edit_field_name_start(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(app_mode=d.get("app_mode", "staff"))
    await state.set_state(ProfileEditState.name)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="cancel_profile_edit")
    await send_step(
        c.message,
        "Введите новое <b>имя и фамилию</b> (одной строкой):",
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.callback_query(F.data == "edit_field_phone")
async def edit_field_phone_start(c: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await state.update_data(app_mode=d.get("app_mode", "staff"))
    await state.set_state(ProfileEditState.phone)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="cancel_profile_edit")
    await send_step(
        c.message,
        "Введите новый <b>номер телефона</b> (например +998901234567):",
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.message(ProfileEditState.name)
async def edit_field_name_save(m: types.Message, state: FSMContext):
    name = (m.text or "").strip()
    if len(name) < 2:
        await send_step(m, "❌ Слишком коротко. Введите полное имя и фамилию:", state=state)
        return
    save_user_name_only(m.from_user.id, name)
    mode = (await state.get_data()).get("app_mode", "staff")
    await state.clear()
    await state.update_data(app_mode=mode)
    await send_step(m, "✅ ФИО обновлено.", state=state)
    await send_profile_screen(m, m.from_user.id, state)


@dp.message(ProfileEditState.phone)
async def edit_field_phone_save(m: types.Message, state: FSMContext):
    phone = (m.text or "").strip()
    if not validate_phone(phone):
        await send_step(
            m,
            "❌ Неверный формат. Введите номер в формате <code>+998XXXXXXXXX</code> (9–15 цифр).",
            state=state,
        )
        return
    save_user_phone_only(m.from_user.id, phone)
    mode = (await state.get_data()).get("app_mode", "staff")
    await state.clear()
    await state.update_data(app_mode=mode)
    await send_step(m, "✅ Телефон обновлён.", state=state)
    await send_profile_screen(m, m.from_user.id, state)


@dp.callback_query(F.data == "cancel_profile_edit")
async def cancel_profile_edit(c: types.CallbackQuery, state: FSMContext):
    mode = (await state.get_data()).get("app_mode", "staff")
    await state.set_state(None)
    await state.update_data(app_mode=mode)
    await send_profile_screen(c.message, c.from_user.id, state)
    await c.answer()


@dp.callback_query(F.data == "profile_reload")
async def profile_reload(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await send_profile_screen(c.message, c.from_user.id, state)
    await c.answer()


@dp.callback_query(F.data == "profile_set_tg")
async def profile_set_tg_start(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    with db_connect() as conn:
        row = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row[0]:
        await c.answer("Сначала заполните профиль (имя и телефон).", show_alert=True)
        return
    d = await state.get_data()
    await state.update_data(app_mode=d.get("app_mode", "staff"))
    await state.set_state(ProfileTgState.value)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="cancel_profile_tg")
    await send_step(
        c.message,
        "Введите ваш <b>Telegram</b>:\n"
        "• username, например <code>@my_username</code>\n"
        "• или ссылку <code>https://t.me/my_username</code>\n\n"
        "Чтобы убрать username из профиля, отправьте: <code>-</code>",
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.callback_query(F.data == "cancel_profile_tg")
async def cancel_profile_tg(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await send_profile_screen(c.message, c.from_user.id, state)
    await c.answer()


@dp.callback_query(F.data == "profile_preview_card")
async def profile_preview_card(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    with db_connect() as conn:
        row = conn.execute("SELECT name, phone, tg_username FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row[0]:
        await c.answer("Сначала заполните профиль (имя и телефон).", show_alert=True)
        return
    name, phone, tg = row[0], (row[1] or "").strip() or None, (row[2] or "").strip() or None
    dummy = profile_preview_dummy_data()
    card = build_card_text(dummy, name, contact_phone=phone, contact_tg=tg)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В личный кабинет", callback_data="profile_reload")
    await send_step(
        c.message,
        "<b>Предпросмотр</b> — пример объекта; внизу карточки ваши контакты, как в посте канала:\n\n" + card,
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.message(ProfileTgState.value)
async def process_profile_tg(m: types.Message, state: FSMContext):
    norm = normalize_telegram_username(m.text or "")
    if norm is None:
        await send_step(
            m,
            "❌ Неверный формат. Укажите @username или ссылку <code>https://t.me/…</code>\n"
            "(латиница, цифры, подчёркивание, длина 5–32).",
            state=state,
        )
        return
    d = await state.get_data()
    mode = d.get("app_mode", "staff")
    if norm == "":
        set_user_telegram_username(m.from_user.id, None)
    else:
        set_user_telegram_username(m.from_user.id, norm)
    await state.clear()
    await state.update_data(app_mode=mode)
    await send_profile_screen(m, m.from_user.id, state)


# --- ЛОГИКА АДМИНКИ ---
@dp.callback_query(F.data == "adm_maint")
async def maintenance(c: types.CallbackQuery):
    with db_connect() as conn:
        users = conn.execute("SELECT id FROM users").fetchall()
    
    for u in users:
        try: 
            await bot.send_message(u[0], "⚠️ <b>Внимание!</b> Бот уходит на тех. обслуживание. Публикация временно недоступна.")
        except Exception as e:
            logging.warning(f"Failed to send maintenance message to {u[0]}: {e}")
    
    await c.answer("Рассылка завершена", show_alert=True)

@dp.callback_query(F.data == "adm_add")
async def adm_add_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.add_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    await send_step(c.message, "Введите ID нового сотрудника:", kb.adjust(1).as_markup(), state)
    await c.answer()

@dp.message(AdminState.add_id)
async def adm_add_process(m: types.Message, state: FSMContext):
    try:
        user_id = int(m.text.strip())
        
        # Проверяем, существует ли уже такой пользователь
        with db_connect() as conn:
            existing = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        
        if existing:
            await send_step(m, "❌ Пользователь с таким ID уже существует.", state=state)
            return
        
        # Добавляем пользователя
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO users (id, name, phone, role, tg_username) VALUES (?, ?, ?, ?, ?)",
                (user_id, "Новый сотрудник", "", "staff", None),
            )
            conn.commit()
        add_log(m.from_user.id, "Админ: добавление сотрудника", f"Добавлен сотрудник ID {user_id}")

        await state.clear()
        await state.update_data(app_mode="staff")
        await send_step(m, f"✅ Сотрудник с ID {user_id} успешно добавлен!", reply_markup=main_menu_kb(m.from_user.id), state=state)
        
    except ValueError:
        await send_step(m, "❌ Неверный формат ID. Введите число.", state=state)

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await send_step(c, build_stats_text(), build_admin_stats_kb(), state=state)
    await c.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(app_mode="staff")
    await send_step(c, "⚙️ <b>Панель администратора</b>", build_admin_panel_kb(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ НОВЫХ ФУНКЦИЙ ---
def _remove_reason_meta(reason: str):
    if reason == REMOVE_REASON_SOLD:
        return POST_STATUS_SOLD, "Продажа", "Продано"
    if reason == REMOVE_REASON_ERROR:
        return POST_STATUS_DELETED, "Ошибка", "Удалено (ошибка)"
    if reason == REMOVE_REASON_FIX:
        return POST_STATUS_DELETED, "Исправление", "Удалено (исправление)"
    return POST_STATUS_DELETED, "Удаление", "Удалено"


@dp.callback_query(F.data == "my_posts")
async def my_posts_folder(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    with db_connect() as conn:
        published_cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id=? AND COALESCE(status, 'published')='published'",
                (uid,),
            ).fetchone()[0]
            or 0
        )
        deleted_cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id=? AND COALESCE(status, 'published')='deleted'",
                (uid,),
            ).fetchone()[0]
            or 0
        )
        sold_cnt = int(
            conn.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id=? AND COALESCE(status, 'published')='sold'",
                (uid,),
            ).fetchone()[0]
            or 0
        )
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📌 Активные ({published_cnt})", callback_data="my_posts_tab_published_0")
    kb.button(text=f"🗂 Архив ({deleted_cnt})", callback_data="my_posts_tab_deleted_0")
    kb.button(text=f"✅ Проданные ({sold_cnt})", callback_data="my_posts_tab_sold_0")
    kb.button(text="⬅️ Назад", callback_data="profile_reload")
    await send_step(c, "📁 <b>Мои публикации</b>\nВыберите раздел:", kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.callback_query(F.data.startswith("my_posts_tab_"))
async def my_posts_tab(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    payload = (c.data or "").replace("my_posts_tab_", "", 1)
    parts = payload.rsplit("_", 1)
    tab_raw = parts[0]
    page = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0
    page = max(0, page)
    page_size = 5
    if tab_raw not in (POST_STATUS_PUBLISHED, POST_STATUS_DELETED, POST_STATUS_SOLD):
        await c.answer("Неизвестный раздел", show_alert=True)
        return
    with db_connect() as conn:
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM posts WHERE user_id = ? AND COALESCE(status, 'published') = ?",
                (uid, tab_raw),
            ).fetchone()[0]
            or 0
        )
        rows = conn.execute(
            """SELECT id, category, realty_type, city, district, price_val, price_cur,
                      channel_message_id, published_date, removed_reason, removed_at
               FROM posts
               WHERE user_id = ? AND COALESCE(status, 'published') = ?
               ORDER BY CASE WHEN removed_at IS NULL THEN published_date ELSE removed_at END DESC
               LIMIT ? OFFSET ?""",
            (uid, tab_raw, page_size, page * page_size),
        ).fetchall()

    titles = {
        POST_STATUS_PUBLISHED: "📌 <b>Активные</b>",
        POST_STATUS_DELETED: "🗂 <b>Архив</b>",
        POST_STATUS_SOLD: "✅ <b>Проданные</b>",
    }
    pages_total = max(1, (total + page_size - 1) // page_size)
    lines = [titles[tab_raw], f"Страница: <b>{page + 1}/{pages_total}</b>", ""]
    if not rows:
        lines.append("Пока пусто.")
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ К папкам", callback_data="my_posts")
        await send_step(c, "\n".join(lines), kb.adjust(1).as_markup(), state=state)
        await c.answer()
        return

    kb = InlineKeyboardBuilder()
    for row in rows:
        pid, cat, rtype, city, district, pval, pcur, _ch_mid, _pdate, rm_reason, rm_at = row
        price_h = _format_price_row(pval, pcur)
        extra = ""
        if tab_raw in (POST_STATUS_DELETED, POST_STATUS_SOLD):
            _, _, reason_human = _remove_reason_meta(rm_reason or "")
            stamp = rm_at or "—"
            extra = f"  📝 {reason_human} | {stamp}\n"
        lines.append(
            f"• <b>№{pid}</b> {html.escape(cat or '')} / {html.escape(rtype or '')}\n"
            f"  📍 {html.escape(city or '')}, {html.escape(district or '—')}\n"
            f"  💰 {html.escape(price_h)}\n"
            f"{extra}"
        )
        if tab_raw == POST_STATUS_PUBLISHED:
            kb.button(text=f"🗑 Выбрать №{pid} для удаления", callback_data=f"my_post_delete_{pid}_{page}")

    if page > 0:
        kb.button(text="⬅️ Назад", callback_data=f"my_posts_tab_{tab_raw}_{page - 1}")
    if (page + 1) * page_size < total:
        kb.button(text="Вперед ➡️", callback_data=f"my_posts_tab_{tab_raw}_{page + 1}")
    kb.button(text="⬅️ К папкам", callback_data="my_posts")
    await send_step(c, "\n".join(lines), kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.callback_query(F.data.startswith("my_post_delete_"))
async def my_post_delete_pick_reason(c: types.CallbackQuery, state: FSMContext):
    try:
        payload = (c.data or "").replace("my_post_delete_", "", 1)
        post_raw, page_raw = payload.rsplit("_", 1)
        post_id = int(post_raw)
        page = int(page_raw) if page_raw.isdigit() else 0
    except ValueError:
        await c.answer("Некорректный номер", show_alert=True)
        return
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id FROM posts WHERE id=? AND user_id=? AND COALESCE(status, 'published')='published'",
            (post_id, c.from_user.id),
        ).fetchone()
    if not row:
        await c.answer("Объявление не найдено или уже снято.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Продажа", callback_data=f"my_post_reason_{post_id}_{REMOVE_REASON_SOLD}_{page}")
    kb.button(text="⚠️ Ошибка", callback_data=f"my_post_reason_{post_id}_{REMOVE_REASON_ERROR}_{page}")
    kb.button(text="✏️ Исправление", callback_data=f"my_post_reason_{post_id}_{REMOVE_REASON_FIX}_{page}")
    kb.button(text="⬅️ Назад к активным", callback_data=f"my_posts_tab_published_{page}")
    await send_step(c, f"Причина удаления объявления №<b>{post_id}</b>:", kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.callback_query(F.data.startswith("my_post_reason_"))
async def my_post_delete_do(c: types.CallbackQuery, state: FSMContext):
    payload = (c.data or "").replace("my_post_reason_", "", 1)
    parts = payload.split("_")
    if len(parts) < 3 or not parts[0].isdigit():
        await c.answer("Некорректные данные", show_alert=True)
        return
    post_id = int(parts[0])
    reason = parts[1]
    page = int(parts[2]) if parts[2].isdigit() else 0
    new_status, reason_label, reason_log = _remove_reason_meta(reason)
    with db_connect() as conn:
        row = conn.execute(
            """SELECT channel_message_id, category, realty_type
               FROM posts
               WHERE id=? AND user_id=? AND COALESCE(status, 'published')='published'""",
            (post_id, c.from_user.id),
        ).fetchone()
    if not row:
        await c.answer("Объявление уже снято или не найдено.", show_alert=True)
        return
    ch_mid, category, realty_type = row
    deleted_in_channel = False
    if ch_mid:
        try:
            await bot.delete_message(CHANNEL_ID, ch_mid)
            deleted_in_channel = True
        except Exception as e:
            logging.warning("my_post_delete delete_message: %s", e)
    with db_connect() as conn:
        conn.execute(
            """UPDATE posts
               SET status=?, removed_reason=?, removed_at=CURRENT_TIMESTAMP, channel_message_id=NULL
               WHERE id=? AND user_id=? AND COALESCE(status, 'published')='published'""",
            (new_status, reason, post_id, c.from_user.id),
        )
        conn.commit()
    add_log(
        c.from_user.id,
        "Сотрудник: снятие объявления",
        f"post_id={post_id}, reason={reason_log}, deleted_in_channel={deleted_in_channel}, category={category}, type={realty_type}",
    )
    tail = (
        " Сообщение в канале удалено."
        if deleted_in_channel
        else " Сообщение в канале не удалось удалить (уже удалено вручную или нет прав)."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К активным", callback_data=f"my_posts_tab_published_{page}")
    kb.button(text="📁 К папкам", callback_data="my_posts")
    await send_step(
        c,
        f"✅ Объявление №{post_id} снято. Причина: <b>{reason_label}</b>.{tail}",
        reply_markup=kb.adjust(1).as_markup(),
        state=state,
    )
    await c.answer()

@dp.callback_query(F.data == "adm_logs")
async def adm_logs(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    filters = (await state.get_data()).get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    await state.update_data(adm_logs_filters=filters)
    rows = fetch_logs_filtered(filters, limit=20)
    lines = ["📋 <b>Логи</b> (последние 20)\n"]
    if rows:
        for rid, uid, action, details, ts in rows:
            lines.append(
                f"• #{rid} | {ts}\n"
                f"  {html.escape(display_user_in_logs(uid))} | {html.escape(str(action or ''))}\n"
                f"  {html.escape(str(details or ''))}\n"
            )
    else:
        lines.append("Логи по текущим фильтрам не найдены.")
    await send_step(c, "\n".join(lines), build_admin_logs_filters_kb(filters), state=state)
    await c.answer()

@dp.callback_query(F.data == "export_stats")
async def export_stats(c: types.CallbackQuery):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    data = csv_bytes_from_rows(build_stats_export_rows())
    file = BufferedInputFile(data, filename=f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    await c.message.answer_document(file, caption="📤 Экспорт статистики (CSV)")
    await c.answer()

@dp.callback_query(F.data == "export_logs")
async def export_logs(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    filters = (await state.get_data()).get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    export_rows = [["id", "user_id", "action", "details", "timestamp"]]
    export_rows.extend(fetch_logs_for_export(filters, limit=5000))
    data = csv_bytes_from_rows(export_rows)
    file = BufferedInputFile(data, filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm_logs")
    await c.message.answer_document(file, caption="📤 Экспорт логов (CSV)", reply_markup=kb.adjust(1).as_markup())
    await c.answer()


@dp.callback_query(F.data == "adm_dbcheck")
async def adm_dbcheck(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, build_dbcheck_text(10), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_importcsv")
async def adm_importcsv(c: types.CallbackQuery, state: FSMContext):
    await show_import_csv_prompt(c.message, state)
    await c.answer()


def _parse_page_from_callback(data: str, prefix: str) -> int:
    if not data.startswith(prefix):
        return 0
    raw = data.replace(prefix, "", 1)
    if not raw.isdigit():
        return 0
    return max(0, int(raw))


async def _render_staff_list(message_obj, page: int, state: FSMContext):
    page_size = 8
    rows, total = fetch_staff_members(limit=page_size, offset=page * page_size)
    if not rows:
        await send_step(
            message_obj,
            "👥 <b>Сотрудники</b>\n\nСписок пуст.",
            build_staff_list_kb([], page, total, page_size),
            state=state,
        )
        return
    text = f"👥 <b>Сотрудники</b>\nВсего: <b>{total}</b>\nСтраница: <b>{page + 1}</b>"
    await send_step(message_obj, text, build_staff_list_kb(rows, page, total, page_size), state=state)


async def _render_staff_card(message_obj, user_id: int, state: FSMContext):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT name, phone, tg_username, role FROM users WHERE id=? AND role IN ('staff','admin')",
            (user_id,),
        ).fetchone()
    if not row:
        await send_step(message_obj, "Сотрудник не найден.", state=state)
        return
    name, phone, tg, role = row
    posts_count = count_user_posts(user_id)
    tg_text = f"@{html.escape((tg or '').strip())}" if tg else "—"
    role_text = role_label_ru(role, user_id)
    text = (
        f"👤 <b>Карточка сотрудника</b>\n\n"
        f"🆔 ID: <b>{user_id}</b>\n"
        f"👤 ФИО: <b>{html.escape(name or '—')}</b>\n"
        f"📞 Телефон: <b>{html.escape(phone or '—')}</b>\n"
        f"🔗 Telegram: <b>{tg_text}</b>\n"
        f"🛡 Роль: <b>{role_text}</b>\n"
        f"📦 Публикаций: <b>{posts_count}</b>"
    )
    await send_step(message_obj, text, build_staff_member_actions_kb(user_id, role), state=state)


@dp.callback_query(F.data == "adm_staff_list")
@dp.callback_query(F.data.startswith("adm_staff_page_"))
async def adm_staff_list(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    page = _parse_page_from_callback(c.data or "", "adm_staff_page_")
    await _render_staff_list(c, page, state)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_staff_open_"))
async def adm_staff_open(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    try:
        user_id = int((c.data or "").replace("adm_staff_open_", ""))
    except ValueError:
        await c.answer("Некорректный ID", show_alert=True)
        return
    await _render_staff_card(c, user_id, state)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_staff_edit_name_"))
@dp.callback_query(F.data.startswith("adm_staff_edit_phone_"))
@dp.callback_query(F.data.startswith("adm_staff_edit_tg_"))
async def adm_staff_edit_start(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    data = c.data or ""
    field = "name" if "_name_" in data else ("phone" if "_phone_" in data else "tg")
    user_id = int(data.rsplit("_", 1)[1])
    await state.set_state(AdminStaffEditState.value)
    await state.update_data(adm_staff_edit_uid=user_id, adm_staff_edit_field=field)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=f"adm_staff_open_{user_id}")
    prompts = {
        "name": "Введите новое <b>ФИО</b> сотрудника:",
        "phone": "Введите новый <b>телефон</b> (пример <code>+998901234567</code>):",
        "tg": "Введите новый <b>Telegram</b> (@username или ссылку t.me). Для очистки отправьте <code>-</code>.",
    }
    await send_step(c, prompts[field], kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.message(AdminStaffEditState.value)
async def adm_staff_edit_save(m: types.Message, state: FSMContext):
    d = await state.get_data()
    user_id = int(d.get("adm_staff_edit_uid", 0))
    field = d.get("adm_staff_edit_field")
    if user_id <= 0 or field not in ("name", "phone", "tg") or not can_open_admin_panel(m.from_user.id):
        await state.clear()
        await send_step(m, "Сессия редактирования сброшена.", state=state)
        return
    raw = (m.text or "").strip()
    with db_connect() as conn:
        row = conn.execute("SELECT name, phone, tg_username, role FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            await state.clear()
            await send_step(m, "Сотрудник не найден.", state=state)
            return
        name, phone, tg, role = row
        if field == "name":
            if len(raw) < 2:
                await send_step(m, "❌ ФИО слишком короткое.", state=state)
                return
            name = raw
        elif field == "phone":
            if not validate_phone(raw):
                await send_step(m, "❌ Неверный формат телефона.", state=state)
                return
            phone = raw
        else:
            norm = normalize_telegram_username(raw)
            if norm is None:
                await send_step(m, "❌ Неверный формат Telegram.", state=state)
                return
            tg = None if norm == "" else norm
        conn.execute(
            "UPDATE users SET name=?, phone=?, tg_username=?, role=? WHERE id=?",
            (name, phone, tg, role, user_id),
        )
        conn.commit()
    add_log(m.from_user.id, "Админ: правка сотрудника", f"user_id={user_id}, field={field}")
    await state.clear()
    await send_step(m, "✅ Данные сотрудника обновлены.", state=state)
    await _render_staff_card(m, user_id, state)


@dp.callback_query(F.data.startswith("adm_staff_role_"))
async def adm_staff_change_role(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    data = c.data or ""
    parts = data.split("_")
    if len(parts) < 5:
        await c.answer("Некорректные данные", show_alert=True)
        return
    new_role = parts[3]
    user_id = int(parts[4])
    if user_id == OWNER_ID:
        await c.answer("Нельзя менять роль владельца.", show_alert=True)
        return
    if new_role not in ("staff", "admin"):
        await c.answer("Некорректная роль", show_alert=True)
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET role=? WHERE id=?", (new_role, user_id))
        conn.commit()
    add_log(c.from_user.id, "Админ: смена роли", f"user_id={user_id}, role={new_role}")
    await c.answer(f"Роль обновлена: {new_role}")
    await _render_staff_card(c, user_id, state)


@dp.callback_query(F.data.startswith("adm_staff_remove_"))
async def adm_staff_remove(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    try:
        user_id = int((c.data or "").replace("adm_staff_remove_", ""))
    except ValueError:
        await c.answer("Некорректный ID", show_alert=True)
        return
    if user_id == OWNER_ID:
        await c.answer("Нельзя убрать владельца.", show_alert=True)
        return
    with db_connect() as conn:
        conn.execute("UPDATE users SET role='client' WHERE id=?", (user_id,))
        conn.commit()
    add_log(c.from_user.id, "Админ: снятие доступа", f"user_id={user_id}")
    await send_step(c, "✅ Доступ сотрудника снят.", state=state)
    await _render_staff_list(c, 0, state)
    await c.answer()


async def _render_pending_requests(message_obj, page: int, state: FSMContext):
    page_size = 8
    rows, total = fetch_pending_requests(limit=page_size, offset=page * page_size)
    if not rows:
        await send_step(
            message_obj,
            "📥 <b>Заявки доступа</b>\n\nНовых заявок нет.",
            build_access_requests_list_kb([], page, total, page_size),
            state=state,
        )
        return
    text = f"📥 <b>Заявки доступа</b>\nНовых: <b>{total}</b>\nСтраница: <b>{page + 1}</b>"
    await send_step(message_obj, text, build_access_requests_list_kb(rows, page, total, page_size), state=state)


@dp.callback_query(F.data == "adm_access_requests")
@dp.callback_query(F.data.startswith("adm_req_page_"))
async def adm_access_requests(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    page = _parse_page_from_callback(c.data or "", "adm_req_page_")
    await _render_pending_requests(c, page, state)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_req_open_"))
async def adm_request_open(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    req_id = int((c.data or "").replace("adm_req_open_", ""))
    with db_connect() as conn:
        row = conn.execute(
            """SELECT user_id, full_name, phone, tg_username, userinfo_id, submitted_at, priority
               FROM staff_access_requests
               WHERE id=? AND status='pending'
               ORDER BY submitted_at DESC LIMIT 1""",
            (req_id,),
        ).fetchone()
    if not row:
        await c.answer("Заявка не найдена или уже обработана.", show_alert=True)
        return
    user_id, full_name, phone, tg, userinfo_id, submitted, priority = row
    text = (
        f"📥 <b>Заявка доступа</b>\n\n"
        f"№ заявки: <b>{req_id}</b>\n"
        f"🆔 Telegram ID: <b>{user_id}</b>\n"
        f"👤 ФИО: <b>{html.escape(full_name)}</b>\n"
        f"📞 Телефон: <b>{html.escape(phone)}</b>\n"
        f"🔗 Telegram: <b>@{html.escape(tg)}</b>\n"
        f"🪪 ID из @userinfobot: <b>{html.escape(userinfo_id)}</b>\n"
        f"🕓 Подана: <b>{submitted}</b>\n"
        f"⭐ Приоритет: <b>{'Да' if int(priority or 0) > 0 else 'Нет'}</b>"
    )
    await send_step(c, text, build_access_request_actions_kb(req_id, int(priority or 0) > 0), state=state)
    await c.answer()


@dp.callback_query(F.data.startswith("adm_req_priority_"))
async def adm_request_set_priority(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    parts = (c.data or "").split("_")
    if len(parts) < 5:
        await c.answer("Некорректные данные", show_alert=True)
        return
    value = 1 if parts[3] == "1" else 0
    req_id = int(parts[4])
    with db_connect() as conn:
        conn.execute("UPDATE staff_access_requests SET priority=? WHERE id=? AND status='pending'", (value, req_id))
        conn.commit()
        row = conn.execute(
            """SELECT user_id, full_name, phone, tg_username, userinfo_id, submitted_at, priority
               FROM staff_access_requests
               WHERE id=? AND status='pending'""",
            (req_id,),
        ).fetchone()
    if not row:
        await c.answer("Заявка не найдена.", show_alert=True)
        return
    user_id, full_name, phone, tg, userinfo_id, submitted, priority = row
    text = (
        f"📥 <b>Заявка доступа</b>\n\n"
        f"№ заявки: <b>{req_id}</b>\n"
        f"🆔 Telegram ID: <b>{user_id}</b>\n"
        f"👤 ФИО: <b>{html.escape(full_name)}</b>\n"
        f"📞 Телефон: <b>{html.escape(phone)}</b>\n"
        f"🔗 Telegram: <b>@{html.escape(tg)}</b>\n"
        f"🪪 ID из @userinfobot: <b>{html.escape(userinfo_id)}</b>\n"
        f"🕓 Подана: <b>{submitted}</b>\n"
        f"⭐ Приоритет: <b>{'Да' if int(priority or 0) > 0 else 'Нет'}</b>"
    )
    await send_step(c, text, build_access_request_actions_kb(req_id, int(priority or 0) > 0), state=state)
    add_log(c.from_user.id, "Админ: приоритет заявки", f"req_id={req_id}, priority={value}")
    await c.answer("Приоритет обновлен")


@dp.callback_query(F.data.startswith("adm_req_approve_"))
async def adm_request_approve(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    req_id = int((c.data or "").replace("adm_req_approve_", ""))
    with db_connect() as conn:
        req = conn.execute(
            """SELECT user_id, full_name, phone, tg_username
               FROM staff_access_requests
               WHERE id=? AND status='pending'
               ORDER BY submitted_at DESC LIMIT 1""",
            (req_id,),
        ).fetchone()
        if not req:
            await c.answer("Заявка не найдена.", show_alert=True)
            return
        user_id, full_name, phone, tg = req
        existing = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        role = "admin" if existing and (existing[0] or "") == "admin" else "staff"
        conn.execute(
            "INSERT OR REPLACE INTO users (id, name, phone, role, tg_username) VALUES (?, ?, ?, ?, ?)",
            (user_id, full_name, phone, role, tg),
        )
        conn.execute(
            """UPDATE staff_access_requests
               SET status='approved', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?
               WHERE id=? AND status='pending'""",
            (c.from_user.id, req_id),
        )
        conn.commit()
    add_log(c.from_user.id, "Админ: заявка одобрена", f"req_id={req_id}, user_id={user_id}")
    try:
        await bot.send_message(user_id, "✅ Ваша заявка одобрена. Теперь вам доступен режим «Сотрудник».")
    except Exception as e:
        logging.warning("Failed to notify approved user %s: %s", user_id, e)
    await c.answer("Заявка одобрена")
    await _render_pending_requests(c, 0, state)


@dp.callback_query(F.data.startswith("adm_req_reject_"))
async def adm_request_reject(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    req_id = int((c.data or "").replace("adm_req_reject_", ""))
    with db_connect() as conn:
        row = conn.execute("SELECT user_id FROM staff_access_requests WHERE id=? AND status='pending'", (req_id,)).fetchone()
        user_id = int(row[0]) if row else 0
        conn.execute(
            """UPDATE staff_access_requests
               SET status='rejected', reviewed_at=CURRENT_TIMESTAMP, reviewed_by=?
               WHERE id=? AND status='pending'""",
            (c.from_user.id, req_id),
        )
        conn.commit()
    add_log(c.from_user.id, "Админ: заявка отклонена", f"req_id={req_id}, user_id={user_id}")
    try:
        await bot.send_message(user_id, "❌ Ваша заявка на доступ сотрудника отклонена.")
    except Exception as e:
        logging.warning("Failed to notify rejected user %s: %s", user_id, e)
    await c.answer("Заявка отклонена")
    await _render_pending_requests(c, 0, state)


@dp.callback_query(F.data == "export_stats_xlsx")
async def export_stats_xlsx(c: types.CallbackQuery):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    data = xlsx_bytes_from_rows(build_stats_export_rows())
    if not data:
        await c.answer("Excel недоступен: установите openpyxl.", show_alert=True)
        return
    file = BufferedInputFile(data, filename=f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    await c.message.answer_document(file, caption="📗 Экспорт статистики (Excel)")
    await c.answer()


@dp.callback_query(F.data == "export_logs_xlsx")
async def export_logs_xlsx(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    filters = (await state.get_data()).get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    export_rows = [["id", "user_id", "action", "details", "timestamp"]]
    export_rows.extend(fetch_logs_for_export(filters, limit=5000))
    data = xlsx_bytes_from_rows(export_rows)
    if not data:
        await c.answer("Excel недоступен: установите openpyxl.", show_alert=True)
        return
    file = BufferedInputFile(data, filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm_logs")
    await c.message.answer_document(file, caption="📗 Экспорт логов (Excel)", reply_markup=kb.adjust(1).as_markup())
    await c.answer()


async def _render_logs_with_filters(c: types.CallbackQuery, state: FSMContext, answer_text: Optional[str] = None):
    filters = (await state.get_data()).get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    rows = fetch_logs_filtered(filters, limit=20)
    lines = ["📋 <b>Логи</b> (последние 20)\n"]
    if rows:
        for rid, uid, action, details, ts in rows:
            lines.append(
                f"• #{rid} | {ts}\n"
                f"  {html.escape(display_user_in_logs(uid))} | {html.escape(str(action or ''))}\n"
                f"  {html.escape(str(details or ''))}\n"
            )
    else:
        lines.append("Логи по текущим фильтрам не найдены.")
    await send_step(c, "\n".join(lines), build_admin_logs_filters_kb(filters), state=state)
    await c.answer(answer_text or "")


@dp.callback_query(F.data.startswith("adm_logs_mode_"))
async def adm_logs_set_mode(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    mode = (c.data or "").replace("adm_logs_mode_", "")
    if mode not in ("all", "bot", "admin", "errors"):
        await c.answer("Некорректный фильтр", show_alert=True)
        return
    d = await state.get_data()
    filters = d.get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    filters["mode"] = mode
    await state.update_data(adm_logs_filters=filters)
    await _render_logs_with_filters(c, state, "Фильтр применен")


@dp.callback_query(F.data.startswith("adm_logs_days_"))
async def adm_logs_set_days(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    raw = (c.data or "").replace("adm_logs_days_", "")
    if not raw.isdigit():
        await c.answer("Некорректный фильтр", show_alert=True)
        return
    d = await state.get_data()
    filters = d.get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    filters["days"] = int(raw)
    await state.update_data(adm_logs_filters=filters)
    await _render_logs_with_filters(c, state, "Фильтр применен")


@dp.callback_query(F.data == "adm_logs_set_uid")
async def adm_logs_set_uid(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminLogsState.wait_user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data="adm_logs_reset")
    await send_step(c, "Введите user_id для фильтра логов (или 0 для сброса):", kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_logs_reset")
async def adm_logs_reset(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(None)
    filters = {"mode": "all", "days": 7, "user_id": None}
    await state.update_data(adm_logs_filters=filters)
    await _render_logs_with_filters(c, state, "Фильтры сброшены")


@dp.message(AdminLogsState.wait_user_id)
async def adm_logs_wait_uid(m: types.Message, state: FSMContext):
    if not can_open_admin_panel(m.from_user.id):
        await state.clear()
        await m.answer("Нет доступа.")
        return
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await send_step(m, "❌ Введите числовой user_id (или 0).", state=state)
        return
    uid_val = int(raw)
    d = await state.get_data()
    filters = d.get("adm_logs_filters") or {"mode": "all", "days": 7, "user_id": None}
    filters["user_id"] = None if uid_val == 0 else uid_val
    await state.set_state(None)
    await state.update_data(adm_logs_filters=filters)
    rows = fetch_logs_filtered(filters, limit=20)
    lines = ["📋 <b>Логи</b> (последние 20)\n"]
    if rows:
        for rid, uid, action, details, ts in rows:
            lines.append(
                f"• #{rid} | {ts}\n"
                f"  {html.escape(display_user_in_logs(uid))} | {html.escape(str(action or ''))}\n"
                f"  {html.escape(str(details or ''))}\n"
            )
    else:
        lines.append("Логи по текущим фильтрам не найдены.")
    await send_step(m, "\n".join(lines), build_admin_logs_filters_kb(filters), state=state)


@dp.callback_query(F.data == "adm_search_posts")
async def adm_search_posts(c: types.CallbackQuery, state: FSMContext):
    if not can_open_admin_panel(c.from_user.id):
        await c.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(None)
    await send_step(c, "🔎 <b>Поиск публикаций</b>\nВыберите режим поиска:", build_admin_search_posts_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_search_post_id")
async def adm_search_post_id(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminSearchPostsState.wait_post_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm_search_posts")
    await send_step(c, "Введите № объявления:", kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_search_staff_id")
async def adm_search_staff_id(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminSearchPostsState.wait_staff_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm_search_posts")
    await send_step(c, "Введите ID сотрудника:", kb.adjust(1).as_markup(), state=state)
    await c.answer()


@dp.message(AdminSearchPostsState.wait_post_id)
async def adm_search_post_id_do(m: types.Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await send_step(m, "❌ Введите целое число (№ объявления).", state=state)
        return
    post_id = int(raw)
    with db_connect() as conn:
        row = conn.execute(
            """SELECT id, user_id, category, realty_type, city, district, price_val, price_cur, channel_message_id, published_date
               FROM posts WHERE id=?""",
            (post_id,),
        ).fetchone()
    if not row:
        await send_step(m, f"❌ Объявление №{post_id} не найдено.", state=state)
        return
    pid, uid, cat, rtype, city, district, pval, pcur, ch_mid, pdate = row
    price = _format_price_row(pval, pcur)
    url = channel_post_url(ch_mid) if ch_mid else None
    text = (
        f"🔎 <b>Результат поиска</b>\n\n"
        f"№: <b>{pid}</b>\n"
        f"Сотрудник ID: <b>{uid}</b>\n"
        f"Категория: <b>{html.escape(cat or '')}</b>\n"
        f"Тип: <b>{html.escape(rtype or '')}</b>\n"
        f"Город: <b>{html.escape(city or '')}</b>\n"
        f"Район: <b>{html.escape(district or '—')}</b>\n"
        f"Цена: <b>{html.escape(price)}</b>\n"
        f"Дата: <b>{pdate}</b>\n"
        f"channel_message_id: <b>{ch_mid or '—'}</b>"
    )
    kb = InlineKeyboardBuilder()
    if url:
        kb.button(text=f"Открыть №{pid} в канале", url=url)
    kb.button(text="⬅️ К поиску", callback_data="adm_search_posts")
    await state.set_state(None)
    await send_step(m, text, kb.adjust(1).as_markup(), state=state)


@dp.message(AdminSearchPostsState.wait_staff_id)
async def adm_search_staff_id_do(m: types.Message, state: FSMContext):
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await send_step(m, "❌ Введите целое число (ID сотрудника).", state=state)
        return
    staff_id = int(raw)
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, category, realty_type, city, district, price_val, price_cur, channel_message_id, published_date
               FROM posts
               WHERE user_id=?
               ORDER BY id DESC
               LIMIT 20""",
            (staff_id,),
        ).fetchall()
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="⬅️ К поиску", callback_data="adm_search_posts")
        await state.set_state(None)
        await send_step(m, f"❌ По сотруднику ID {staff_id} публикации не найдены.", kb.adjust(1).as_markup(), state=state)
        return
    lines = [f"🔎 <b>Публикации сотрудника ID {staff_id}</b> (последние {len(rows)}):\n"]
    for pid, cat, rtype, city, district, pval, pcur, ch_mid, pdate in rows:
        lines.append(
            f"• №<b>{pid}</b> | {html.escape(cat or '')}/{html.escape(rtype or '')}\n"
            f"  📍 {html.escape(city or '')}, {html.escape(district or '—')}\n"
            f"  💰 {html.escape(_format_price_row(pval, pcur))} | {pdate}"
        )
        url = channel_post_url(ch_mid) if ch_mid else None
        if url:
            kb.button(text=f"Открыть №{pid}", url=url)
    kb.button(text="⬅️ К поиску", callback_data="adm_search_posts")
    await state.set_state(None)
    await send_step(m, "\n".join(lines), kb.adjust(1).as_markup(), state=state)


@dp.callback_query(F.data == "adm_delete_post")
async def adm_delete_post_start(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id != OWNER_ID:
        await c.answer("Доступно только владельцу бота.", show_alert=True)
        return
    await state.set_state(AdminDeletePostState.wait_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="adm_menu_service")
    await send_step(
        c.message,
        "Введите <b>номер объявления</b> (№ на карточке в канале = id в таблице <code>posts</code>).\n\n"
        "Удалится запись в базе. Если пост в канале ещё есть и у бота есть право удалять сообщения — бот попробует удалить его "
        "(альбом из нескольких фото может удалиться не полностью — это ограничение Telegram).",
        kb.adjust(1).as_markup(),
        state,
    )
    await c.answer()


@dp.message(AdminDeletePostState.wait_id)
async def adm_delete_post_do(m: types.Message, state: FSMContext):
    if m.from_user.id != OWNER_ID:
        await state.clear()
        await state.update_data(app_mode="staff")
        await m.answer("Нет доступа.")
        return
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await send_step(m, "❌ Введите целое число — номер объявления.", state=state)
        return
    post_id = int(raw)
    with db_connect() as conn:
        row = conn.execute("SELECT channel_message_id FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row:
        await send_step(m, f"❌ Объявление №{post_id} в базе не найдено.", state=state)
        return
    ch_mid = row[0]
    deleted_in_channel = False
    if ch_mid:
        try:
            await bot.delete_message(CHANNEL_ID, ch_mid)
            deleted_in_channel = True
        except Exception as e:
            logging.warning("adm_delete_post delete_message: %s", e)
    with db_connect() as conn:
        conn.execute("DELETE FROM posts WHERE id=?", (post_id,))
        conn.commit()
    add_log(m.from_user.id, "Админ: удаление объявления", f"post_id={post_id}, deleted_in_channel={deleted_in_channel}")
    await state.clear()
    await state.update_data(app_mode="staff")
    tail = " Сообщение в канале удалено." if deleted_in_channel else (
        " Сообщение в канале не удалось удалить (уже удалено вручную или нет прав) — запись из базы всё равно убрана."
        if ch_mid else ""
    )
    await send_step(
        m,
        f"✅ Объявление №{post_id} удалено из базы.{tail}",
        reply_markup=main_menu_kb(m.from_user.id),
        state=state,
    )

# --- СОЗДАНИЕ КАРТОЧКИ ---
@dp.message(F.text == "➕ Создать карточку объекта")
async def start_post(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id): 
        await send_step(m, "❌ У вас нет доступа к этой функции.", state=state)
        return
    if (await state.get_data()).get("app_mode") != "staff":
        await send_step(m, "Сначала выберите режим <b>«👔 Сотрудник»</b>.", reply_markup=role_select_kb(), state=state)
        return

    await try_delete_trigger_message(m)
    await purge_cleanup_messages(chat_id=m.chat.id, state=state, try_delete_user_messages=True)
    await clear_state_preserve_cleanup(state)
    await state.update_data(app_mode="staff", **{AUTO_CLEANUP_PAUSED_KEY: True, CLEANUP_PRESS_COUNTER_KEY: 0})
    logging.debug("auto-cleanup paused: start post flow user_id=%s chat_id=%s", m.from_user.id, m.chat.id)
    await state.set_state(PostState.category)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Жилое", callback_data="cat_living")
    kb.button(text="🏢 Нежилое", callback_data="cat_commercial")
    kb.button(text="🚜 Спецтехника", callback_data="cat_tech")
    kb.button(text="⚙️ Оборудование", callback_data="cat_equip")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    await send_step(m, "Выберите категорию актива:", kb.adjust(2).as_markup(), state)

# --- ОБРАБОТЧИКИ КАТЕГОРИЙ ---
@dp.callback_query(F.data.startswith("cat_"))
async def handle_category(c: types.CallbackQuery, state: FSMContext):
    category_map = {
        "cat_living": "Жилое",
        "cat_commercial": "Нежилое", 
        "cat_tech": "Спецтехника",
        "cat_equip": "Оборудование"
    }
    
    category = category_map.get(c.data)
    if not category:
        await c.answer("❌ Неизвестная категория", show_alert=True)
        return
    
    await state.update_data(category=category)
    await state.set_state(PostState.realty_type)
    
    kb = InlineKeyboardBuilder()
    if category == "Жилое":
        kb.button(text="Квартира", callback_data="type_apartment")
        kb.button(text="Дом", callback_data="type_house")
    elif category == "Нежилое":
        kb.button(text="Офис", callback_data="type_office")
        kb.button(text="Магазин", callback_data="type_shop")
        kb.button(text="Склад", callback_data="type_warehouse")
    else:
        kb.button(text="Другое", callback_data="type_other")
    
    kb.button(text="⬅️ Назад", callback_data="back_to_category")
    await send_step(c.message, f"Выбрана категория: <b>{category}</b>\nТеперь выберите тип:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ ТИПОВ ---
@dp.callback_query(F.data.startswith("type_"))
async def handle_realty_type(c: types.CallbackQuery, state: FSMContext):
    type_map = {
        "type_apartment": "Квартира",
        "type_house": "Дом",
        "type_office": "Офис",
        "type_shop": "Магазин",
        "type_warehouse": "Склад",
        "type_other": "Другое"
    }
    
    realty_type = type_map.get(c.data)
    if not realty_type:
        await c.answer("❌ Неизвестный тип", show_alert=True)
        return
    
    await state.update_data(realty_type=realty_type)
    await state.set_state(PostState.city)
    
    kb = InlineKeyboardBuilder()
    for city in CITIES.keys():
        kb.button(text=city, callback_data=f"city_{city}")
    kb.button(text="⬅️ Назад", callback_data="back_to_type")
    await send_step(c.message, f"Выбран тип: <b>{realty_type}</b>\nТеперь выберите город:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data.startswith("city_"))
async def handle_city(c: types.CallbackQuery, state: FSMContext):
    city_key = c.data.replace("city_", "")
    if city_key not in CITIES:
        await c.answer("❌ Неизвестный город", show_alert=True)
        return
    
    await state.update_data(city=city_key, district_group=None)
    await state.set_state(PostState.district)
    
    kb = InlineKeyboardBuilder()
    groups = city_district_groups(city_key)
    if groups:
        for i, group_name in enumerate(groups):
            kb.button(text=group_name, callback_data=f"district_group_{i}")
    elif city_districts(city_key):
        for district in city_districts(city_key):
            kb.button(text=district, callback_data=f"district_{district}")
    else:
        kb.button(text="Введите вручную", callback_data="district_manual")
        kb.button(text="Пропустить", callback_data="skip_district")
    
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    if groups:
        await send_step(c.message, f"Выбран город: <b>{city_key}</b>\nВыберите часть региона:", kb.adjust(2).as_markup(), state)
    else:
        await send_step(c.message, f"Выбран город: <b>{city_key}</b>\nВыберите район:", kb.adjust(2).as_markup(), state)
    await c.answer()


@dp.callback_query(F.data.startswith("district_group_"))
async def handle_district_group(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city = data.get("city")
    groups = city_district_groups(city)
    idx_raw = c.data.replace("district_group_", "")
    if not idx_raw.isdigit():
        await c.answer("❌ Неизвестная группа района", show_alert=True)
        return
    idx = int(idx_raw)
    if idx < 0 or idx >= len(groups):
        await c.answer("❌ Неизвестная группа района", show_alert=True)
        return

    selected_group = groups[idx]
    await state.update_data(district_group=selected_group)
    districts = city_districts(city, selected_group)

    kb = InlineKeyboardBuilder()
    if districts:
        for district in districts:
            kb.button(text=district, callback_data=f"district_{district}")
    else:
        kb.button(text="Введите вручную", callback_data="district_manual")
        kb.button(text="Пропустить", callback_data="skip_district")
    kb.button(text="⬅️ Назад", callback_data="back_to_city_group")
    await send_step(
        c.message,
        f"Выбрано: <b>{selected_group}</b>\nВыберите район:",
        kb.adjust(2).as_markup(),
        state,
    )
    await c.answer()

# --- ОБРАБОТЧИКИ РАЙОНОВ ---
@dp.callback_query(F.data == "skip_district")
async def district_skip_cb(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(district="—")
    await state.set_state(PostState.street)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="street_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_district")
    await send_step(c.message, "Район пропущен.\nВведите улицу:", kb.adjust(2).as_markup(), state)
    await c.answer()


@dp.callback_query(
    F.data.startswith("district_")
    & ~F.data.startswith("district_group_")
    & (F.data != "district_manual")
)
async def handle_district(c: types.CallbackQuery, state: FSMContext):
    district_key = c.data.replace("district_", "")
    
    await state.update_data(district=district_key)
    await state.set_state(PostState.street)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="street_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_district")
    await send_step(c.message, f"Выбран район: <b>{district_key}</b>\nВведите улицу:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "district_manual")
async def district_manual(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.district)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="skip_district")
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    await send_step(c.message, "Введите название района вручную:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_district")
async def back_to_district(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.district)
    data = await state.get_data()
    city = data.get('city', '')
    district_group = data.get("district_group")
    
    kb = InlineKeyboardBuilder()
    districts = city_districts(city, district_group)
    if districts:
        for district in districts:
            kb.button(text=district, callback_data=f"district_{district}")
    else:
        kb.button(text="Введите вручную", callback_data="district_manual")
        kb.button(text="Пропустить", callback_data="skip_district")

    if district_group:
        kb.button(text="⬅️ Назад", callback_data="back_to_city_group")
        await send_step(c.message, f"Выбрано: <b>{district_group}</b>\nВыберите район:", kb.adjust(2).as_markup(), state)
    else:
        kb.button(text="⬅️ Назад", callback_data="back_to_city")
        await send_step(c.message, f"Выбран город: <b>{city}</b>\nВыберите район:", kb.adjust(2).as_markup(), state)
    await c.answer()


@dp.callback_query(F.data == "back_to_city_group")
async def back_to_city_group(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.district)
    city = (await state.get_data()).get("city", "")
    groups = city_district_groups(city)
    kb = InlineKeyboardBuilder()
    for i, group_name in enumerate(groups):
        kb.button(text=group_name, callback_data=f"district_group_{i}")
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    await send_step(c.message, f"Выбран город: <b>{city}</b>\nВыберите часть региона:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ УЛИЦ ---
@dp.callback_query(F.data == "street_skip")
async def street_skip(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(street="")
    await state.set_state(PostState.house)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="house_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_street")
    await send_step(c.message, "Введите номер дома:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.message(PostState.district)
async def process_district_manual(m: types.Message, state: FSMContext):
    district = m.text.strip()
    if len(district) < 2:
        await send_step(m, "❌ Название района слишком короткое. Введите корректное название:", state=state)
        return
    
    await state.update_data(district=district)
    await state.set_state(PostState.street)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="street_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_district")
    await send_step(m, f"Выбран район: <b>{district}</b>\nВведите улицу:", kb.adjust(2).as_markup(), state)

@dp.message(PostState.street)
async def process_street(m: types.Message, state: FSMContext):
    street = m.text.strip()
    await state.update_data(street=street)
    await state.set_state(PostState.house)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="house_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_street")
    await send_step(m, "Введите номер дома:", kb.adjust(2).as_markup(), state)

@dp.callback_query(F.data == "house_skip")
async def house_skip(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(house="")
    await state.set_state(PostState.total_area)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_house")
    await send_step(c.message, "Введите общую площадь (в м²):", kb.adjust(1).as_markup(), state)
    await c.answer()

@dp.message(PostState.house)
async def process_house(m: types.Message, state: FSMContext):
    house = m.text.strip()
    await state.update_data(house=house)
    await state.set_state(PostState.total_area)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_house")
    await send_step(m, "Введите общую площадь (в м²):", kb.adjust(1).as_markup(), state)

@dp.callback_query(F.data == "back_to_street")
async def back_to_street(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.street)
    district = (await state.get_data()).get('district', '')
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="street_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_district")
    await send_step(c.message, f"Выбран район: <b>{district}</b>\nВведите улицу:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_house")
async def back_to_house(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.house)
    street = (await state.get_data()).get('street', '')
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="house_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_street")
    await send_step(c.message, f"Улица: <b>{street}</b>\nВведите номер дома:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ ПЛОЩАДИ И ЦЕНЫ ---
@dp.message(PostState.total_area)
async def process_total_area(m: types.Message, state: FSMContext):
    area = m.text.strip()
    valid, area_float = validate_area(area)
    if not valid:
        await send_step(m, "❌ Неверный формат площади. Введите число (например: 75 или 75.5):", state=state)
        return
    
    await state.update_data(total_area=area_float)
    await state.set_state(PostState.useful_area)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="useful_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_total_area")
    await send_step(m, "Введите полезную площадь (в м²):", kb.adjust(2).as_markup(), state)

@dp.callback_query(F.data == "useful_skip")
async def useful_skip(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(useful_area="")
    await state.set_state(PostState.rooms)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="1", callback_data="room_1")
    kb.button(text="2", callback_data="room_2")
    kb.button(text="3", callback_data="room_3")
    kb.button(text="4", callback_data="room_4")
    kb.button(text="5", callback_data="room_5")
    kb.button(text="N", callback_data="room_n")
    kb.button(text="Пропустить", callback_data="rooms_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_useful_area")
    await send_step(c.message, "Выберите количество комнат:", kb.adjust(3).as_markup(), state)
    await c.answer()

@dp.message(PostState.useful_area)
async def process_useful_area(m: types.Message, state: FSMContext):
    area = m.text.strip()
    valid, area_float = validate_area(area)
    if not valid:
        await send_step(m, "❌ Неверный формат площади. Введите число (например: 65 или 65.5):", state=state)
        return
    
    await state.update_data(useful_area=area_float)
    await state.set_state(PostState.rooms)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="1", callback_data="room_1")
    kb.button(text="2", callback_data="room_2")
    kb.button(text="3", callback_data="room_3")
    kb.button(text="4", callback_data="room_4")
    kb.button(text="5", callback_data="room_5")
    kb.button(text="N", callback_data="room_n")
    kb.button(text="Пропустить", callback_data="rooms_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_useful_area")
    await send_step(m, "Выберите количество комнат:", kb.adjust(3).as_markup(), state)

@dp.callback_query(F.data == "desc_skip")
async def desc_skip(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(desc="")
    await state.set_state(PostState.price_val)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_desc")
    await send_step(c.message, "Введите цену:", kb.adjust(1).as_markup(), state)
    await c.answer()

@dp.message(PostState.desc)
async def process_desc(m: types.Message, state: FSMContext):
    desc = m.text.strip()
    await state.update_data(desc=desc)
    await state.set_state(PostState.price_val)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_desc")
    await send_step(m, "Введите цену:", kb.adjust(1).as_markup(), state)

# --- ОБРАБОТЧИКИ КОМНАТ ---
@dp.callback_query(F.data.startswith("room_"))
async def handle_rooms(c: types.CallbackQuery, state: FSMContext):
    if c.data == "room_n":
        await state.update_data(rooms_manual=True)
        await state.set_state(PostState.rooms)
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="back_to_rooms")
        await send_step(c.message, "Введите количество комнат вручную (например: 7):", kb.adjust(1).as_markup(), state)
        await c.answer()
        return

    room_map = {
        "room_1": "1",
        "room_2": "2",
        "room_3": "3",
        "room_4": "4",
        "room_5": "5",
        "room_6": "6+"
    }
    
    rooms = room_map.get(c.data)
    if not rooms:
        await c.answer("❌ Неизвестное количество комнат", show_alert=True)
        return
    
    await state.update_data(rooms=rooms)
    await state.set_state(PostState.desc)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="desc_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_rooms")
    await send_step(c.message, "Введите описание объекта:", kb.adjust(2).as_markup(), state)
    await c.answer()


@dp.callback_query(F.data == "rooms_skip")
async def rooms_skip(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(rooms_manual=False)
    await state.update_data(rooms="")
    await state.set_state(PostState.desc)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="desc_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_rooms")
    await send_step(c.message, "Введите описание объекта:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_rooms")
async def back_to_rooms(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.rooms)
    await state.update_data(rooms_manual=False)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="1", callback_data="room_1")
    kb.button(text="2", callback_data="room_2")
    kb.button(text="3", callback_data="room_3")
    kb.button(text="4", callback_data="room_4")
    kb.button(text="5", callback_data="room_5")
    kb.button(text="N", callback_data="room_n")
    kb.button(text="Пропустить", callback_data="rooms_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_useful_area")
    await send_step(c.message, "Выберите количество комнат:", kb.adjust(3).as_markup(), state)
    await c.answer()


@dp.message(PostState.rooms)
async def process_rooms_manual(m: types.Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("rooms_manual"):
        await send_step(m, "Выберите количество комнат кнопками.", state=state)
        return
    value = (m.text or "").strip()
    if not value.isdigit() or int(value) <= 0:
        await send_step(m, "❌ Введите количество комнат числом больше 0.", state=state)
        return
    await state.update_data(rooms_manual=False, rooms=value)
    await state.set_state(PostState.desc)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="desc_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_rooms")
    await send_step(m, "Введите описание объекта:", kb.adjust(2).as_markup(), state)

@dp.message(PostState.price_val)
async def process_price(m: types.Message, state: FSMContext):
    price = m.text.strip()
    valid, price_float = validate_price(price)
    if not valid:
        await send_step(m, "❌ Неверный формат цены. Введите число (например: 150000000 или 150 000 000):", state=state)
        return
    
    await state.update_data(price_val=price_float)
    await state.set_state(PostState.price_cur)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Сум", callback_data="cur_sum")
    kb.button(text="USD", callback_data="cur_usd")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_val")
    await send_step(m, "Выберите валюту:", kb.adjust(2).as_markup(), state)

@dp.callback_query(F.data.startswith("cur_"))
async def handle_currency(c: types.CallbackQuery, state: FSMContext):
    currency_map = {
        "cur_sum": "сум",
        "cur_usd": "USD"
    }
    
    currency = currency_map.get(c.data)
    if not currency:
        await c.answer("❌ Неизвестная валюта", show_alert=True)
        return
    
    await state.update_data(price_cur=currency)
    await state.set_state(PostState.media_choice)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 Добавить медиа", callback_data="media_add")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Добавьте до 10 фото/видео или пропустите:", kb.adjust(1).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ МЕДИА ---
@dp.callback_query(F.data.in_(["media_add", "media_skip"]))
async def handle_media_choice(c: types.CallbackQuery, state: FSMContext):
    media_map = {
        "media_add": "add",
        "media_skip": "skip"
    }
    
    media_type = media_map.get(c.data)
    if not media_type:
        await c.answer("❌ Неизвестный тип медиа", show_alert=True)
        return
    
    if media_type == "skip":
        await state.update_data(media_type="skip", media_files=[])
        await state.set_state(PostState.preview)
        await show_preview(c.message, state)
    else:
        await state.update_data(media_type="mixed", media_files=[], media_ui_ready=False)
        await state.set_state(PostState.media_file)
        await send_step(
            c.message,
            "Отправьте фото/видео (до 10). После первой загрузки появятся кнопки управления.",
            state=state,
        )
    
    await c.answer()

@dp.message(PostState.media_file)
async def process_media(m: types.Message, state: FSMContext):
    data = await state.get_data()
    current = data.get("media_files") or []
    item = None
    if m.photo:
        item = {"type": "photo", "file_id": m.photo[-1].file_id}
    elif m.video:
        item = {"type": "video", "file_id": m.video.file_id}

    if item is None:
        await send_step(m, "❌ Отправьте фото или видео.", build_media_manage_kb(), state)
        return

    if len(current) >= 10:
        await send_step(m, "⚠️ Лимит 10 файлов. Удалите лишнее или нажмите «✅ Готово».", build_media_manage_kb(), state)
        return

    was_empty = len(current) == 0
    current.append(item)
    await state.update_data(media_type="mixed", media_files=current)
    # Для альбомов не спамим сообщением на каждый элемент: показываем controls один раз.
    if was_empty or not data.get("media_ui_ready"):
        await state.update_data(media_ui_ready=True)
        await send_step(m, f"✅ Принято: <b>{len(current)}/10</b>", build_media_manage_kb(), state)


@dp.callback_query(F.data == "media_done")
async def media_done(c: types.CallbackQuery, state: FSMContext):
    media_files = (await state.get_data()).get("media_files") or []
    if not media_files:
        await c.answer("Сначала добавьте хотя бы одно фото/видео.", show_alert=True)
        return
    await state.set_state(PostState.preview)
    await show_preview(c.message, state)
    await c.answer()


@dp.callback_query(F.data == "media_remove_last")
async def media_remove_last(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    current = data.get("media_files") or []
    if not current:
        await c.answer("Список уже пуст.", show_alert=True)
        return
    removed = current.pop()
    await state.update_data(media_files=current, media_type="mixed", media_ui_ready=bool(current))
    removed_type = "фото" if isinstance(removed, dict) and removed.get("type") == "photo" else "видео"
    await send_step(c.message, f"🗑 Удалена последняя: <b>{removed_type}</b>.\n{media_progress_text(current)}", build_media_manage_kb(), state)
    await c.answer()


@dp.callback_query(F.data == "media_clear_all")
async def media_clear_all(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(media_files=[], media_type="mixed", media_ui_ready=False)
    await send_step(
        c.message,
        "♻ Список медиа очищен.\nОтправьте новые фото/видео.",
        build_media_manage_kb(),
        state,
    )
    await c.answer()

@dp.callback_query(F.data == "back_to_media_choice")
async def back_to_media_choice(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.media_choice)
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 Добавить медиа", callback_data="media_add")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Добавьте до 10 фото/видео или пропустите:", kb.adjust(1).as_markup(), state)
    await c.answer()


def build_card_text(
    data: dict,
    employee_name: str,
    listing_no: Optional[int] = None,
    contact_phone: Optional[str] = None,
    contact_tg: Optional[str] = None,
) -> str:
    """Единый рендер карточки (для превью и публикации). listing_no — номер объявления (= id в posts)."""
    category = html.escape(str(data.get('category') or ''))
    realty_type = html.escape(str(data.get('realty_type') or ''))
    city = html.escape(str(data.get('city') or ''))
    district = html.escape(str(data.get('district') or ''))
    street = html.escape(str(data.get('street') or ''))
    house = html.escape(str(data.get('house') or ''))
    total_area = data.get('total_area', '')
    useful_area = data.get('useful_area', '')
    rooms = html.escape(str(data.get('rooms') or ''))
    desc = html.escape(str(data.get('desc') or ''))
    price_val = data.get('price_val', '')
    price_cur = html.escape(str(data.get('price_cur') or ''))
    emp = html.escape(str(employee_name or ''))

    area_text = ""
    if total_area:
        if useful_area:
            area_text = f"📐 Площадь: {total_area} м² (полезная: {useful_area} м²)"
        else:
            area_text = f"📐 Площадь: {total_area} м²"

    rooms_text = f"🔢 Комнат: {rooms}" if rooms else ""
    price_text = f"💰 ЦЕНА: {price_val:,} {price_cur}".replace(",", " ") if price_val and price_cur else ""
    desc_text = f"📝 Доп.информация: {desc}" if desc else ""

    card_text = ""
    if listing_no is not None:
        card_text += f"🔢 <b>Объявление №{listing_no}</b>\n"
    card_text += f"🏠 {category}\n\n"
    card_text += f"🏙 Город: {city}\n"
    card_text += f"📍 Район: {district}\n"
    if street:
        street_line = f"{street}, {house}" if house else street
        card_text += f"🛣 Улица: {street_line}\n"
    card_text += f"🏷 Тип: {realty_type}\n"
    if rooms_text:
        card_text += f"{rooms_text}\n"
    if area_text:
        card_text += f"{area_text}\n"
    if desc_text:
        card_text += f"{desc_text}\n"
    card_text += "\n"
    if price_text:
        card_text += f"{price_text}\n\n"
    card_text += f"{emp}\n"
    contacts: list[str] = []
    if contact_phone and str(contact_phone).strip():
        contacts.append(f"<code>{html.escape(str(contact_phone).strip())}</code>")
    if contact_tg and str(contact_tg).strip():
        u = str(contact_tg).strip().lstrip("@")
        contacts.append(f"@{html.escape(u)}")
    if contacts:
        card_text += " ".join(contacts) + "\n"
    card_text = card_text.rstrip()
    return card_text


# --- ФОРМИРОВАНИЕ КАРТОЧКИ И ПРЕВЬЮ ---
async def show_preview(m_obj, state: FSMContext):
    data = await state.get_data()

    with db_connect() as conn:
        user = conn.execute(
            "SELECT name, phone, tg_username FROM users WHERE id=?",
            (m_obj.from_user.id,),
        ).fetchone()
    if user:
        employee_name, c_phone, c_tg = user[0], user[1], user[2]
    else:
        employee_name, c_phone, c_tg = "Сотрудник", None, None
    c_phone = (c_phone or "").strip() or None
    c_tg = (c_tg or "").strip() or None
    card_text = build_card_text(
        data,
        employee_name or "Сотрудник",
        contact_phone=c_phone,
        contact_tg=c_tg,
    )
    
    # Отправляем превью
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data="publish")
    kb.button(text="❌ Отмена", callback_data="cancel_publish")
    kb.button(text="⬅️ Назад", callback_data="back_to_media")
    
    await send_step(
        m_obj,
        "Предпросмотр карточки:\n"
        "<i>После публикации в канале карточке будет присвоен номер объявления (№ …).</i>\n\n"
        f"{card_text}",
        kb.adjust(2).as_markup(),
        state,
    )

@dp.callback_query(F.data == "publish")
async def publish_post(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    category = data.get('category', '')
    realty_type = data.get('realty_type', '')
    with db_connect() as conn:
        user = conn.execute(
            "SELECT name, phone, tg_username FROM users WHERE id=?",
            (c.from_user.id,),
        ).fetchone()
    if user:
        employee_name, c_phone, c_tg = user[0], user[1], user[2]
    else:
        employee_name, c_phone, c_tg = "Сотрудник", None, None
    c_phone = (c_phone or "").strip() or None
    c_tg = (c_tg or "").strip() or None
    post_id = insert_post(c.from_user.id, data, None)

    # Публикуем в канал
    try:
        card_text = build_card_text(
            data,
            employee_name or "Сотрудник",
            listing_no=post_id,
            contact_phone=c_phone,
            contact_tg=c_tg,
        )
        contact_kb = None
        media_type = data.get('media_type')
        media_files = data.get('media_files', [])
        sent_msg = None
        if media_type == "mixed" and media_files:
            prepared = []
            for item in media_files:
                if isinstance(item, dict):
                    mtype = item.get("type")
                    fid = item.get("file_id")
                else:
                    # fallback для старого формата: только фото file_id
                    mtype = "photo"
                    fid = item
                if not fid:
                    continue
                prepared.append((mtype, fid))
            if len(prepared) == 1:
                only_type, only_fid = prepared[0]
                if only_type == "video":
                    sent_msg = await bot.send_video(CHANNEL_ID, only_fid, caption=card_text, parse_mode="HTML", reply_markup=contact_kb)
                else:
                    sent_msg = await bot.send_photo(CHANNEL_ID, only_fid, caption=card_text, parse_mode="HTML", reply_markup=contact_kb)
            elif len(prepared) > 1:
                media_group = []
                for idx, (mtype, fid) in enumerate(prepared[:10]):
                    if mtype == "video":
                        media = InputMediaVideo(media=fid)
                    else:
                        media = InputMediaPhoto(media=fid)
                    if idx == 0:
                        media.caption = card_text
                        media.parse_mode = "HTML"
                    media_group.append(media)
                sent = await bot.send_media_group(CHANNEL_ID, media_group)
                sent_msg = sent[0] if sent else None
        else:
            sent_msg = await bot.send_message(
                CHANNEL_ID,
                card_text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=contact_kb,
            )
        ch_mid = sent_msg.message_id if sent_msg else None
        update_post_channel_message_id(post_id, ch_mid)
        # Логируем публикацию
        with db_connect() as conn:
            conn.execute("INSERT INTO stats (user_id) VALUES (?)", (c.from_user.id,))
            conn.execute("INSERT INTO logs (user_id, action, details) VALUES (?, ?, ?)", 
                        (c.from_user.id, "Публикация", f"Опубликована карточка №{post_id}: {category} - {realty_type}"))
        await state.clear()
        await state.update_data(app_mode="staff", **{AUTO_CLEANUP_PAUSED_KEY: False, CLEANUP_PRESS_COUNTER_KEY: 0})
        logging.debug(
            "auto-cleanup resumed: post published user_id=%s chat_id=%s post_id=%s",
            c.from_user.id,
            c.message.chat.id,
            post_id,
        )
        await send_step(
            c,
            f"✅ Карточка опубликована в канале.\n<b>Номер объявления: №{post_id}</b>",
            reply_markup=main_menu_kb(c.from_user.id),
            state=state,
        )
        await c.answer()
        
    except Exception as e:
        logging.error(f"Failed to publish post: {e}")
        delete_pending_post(post_id)
        await send_step(c, "❌ Ошибка при публикации. Попробуйте еще раз.", state=state)
        await c.answer()

@dp.callback_query(F.data == "cancel_publish")
async def cancel_publish(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(app_mode="staff", **{AUTO_CLEANUP_PAUSED_KEY: False, CLEANUP_PRESS_COUNTER_KEY: 0})
    logging.debug("auto-cleanup resumed: publish canceled user_id=%s chat_id=%s", c.from_user.id, c.message.chat.id)
    await send_step(c, "Публикация отменена. Вы вернулись в главное меню", reply_markup=main_menu_kb(c.from_user.id), state=state)
    await c.answer()

@dp.callback_query(F.data == "back_to_media")
async def back_to_media(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.media_choice)
    kb = InlineKeyboardBuilder()
    kb.button(text="📎 Добавить медиа", callback_data="media_add")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Добавьте до 10 фото/видео или пропустите:", kb.adjust(1).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_type")
async def back_to_type(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.realty_type)
    category = (await state.get_data()).get('category', '')
    
    kb = InlineKeyboardBuilder()
    if category == "Жилое":
        kb.button(text="Квартира", callback_data="type_apartment")
        kb.button(text="Дом", callback_data="type_house")
    elif category == "Нежилое":
        kb.button(text="Офис", callback_data="type_office")
        kb.button(text="Магазин", callback_data="type_shop")
        kb.button(text="Склад", callback_data="type_warehouse")
    else:
        kb.button(text="Другое", callback_data="type_other")
    
    kb.button(text="⬅️ Назад", callback_data="back_to_category")
    await send_step(c.message, f"Выбрана категория: <b>{category}</b>\nТеперь выберите тип:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_city")
async def back_to_city(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.city)
    kb = InlineKeyboardBuilder()
    for city in CITIES.keys():
        kb.button(text=city, callback_data=f"city_{city}")
    kb.button(text="⬅️ Назад", callback_data="back_to_type")
    await send_step(c.message, "Выберите город:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "go_back")
async def go_back(c: types.CallbackQuery, state: FSMContext):
    mode = (await state.get_data()).get("app_mode", "staff")
    if mode != "client":
        await purge_cleanup_messages(
            chat_id=c.message.chat.id,
            state=state,
            try_delete_user_messages=True,
        )
    await state.clear()
    if mode == "client":
        await state.update_data(app_mode="client")
        await send_step(c, "Меню клиента.", reply_markup=client_menu_kb(), state=state)
    else:
        await state.update_data(app_mode="staff", **{AUTO_CLEANUP_PAUSED_KEY: False, CLEANUP_PRESS_COUNTER_KEY: 0})
        logging.debug("auto-cleanup resumed: go_back to staff menu user_id=%s chat_id=%s", c.from_user.id, c.message.chat.id)
        await send_step(c, "Вы вернулись в главное меню", reply_markup=main_menu_kb(c.from_user.id), state=state)
    await c.answer()

@dp.callback_query(F.data == "back_to_category")
async def back_to_category(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.category)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Жилое", callback_data="cat_living")
    kb.button(text="🏢 Нежилое", callback_data="cat_commercial")
    kb.button(text="🚜 Спецтехника", callback_data="cat_tech")
    kb.button(text="⚙️ Оборудование", callback_data="cat_equip")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    await send_step(c, "Выберите категорию актива:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ВАЛИДАЦИЯ ДАННЫХ ---
def validate_phone(phone):
    """Проверка формата телефона"""
    import re
    pattern = r'^\+?\d{9,15}$'
    return re.match(pattern, phone) is not None

def validate_price(price):
    """Проверка формата цены"""
    try:
        price_float = float(price.replace(',', '.'))
        return price_float > 0, price_float
    except ValueError:
        return False, None

def validate_area(area):
    """Проверка формата площади"""
    try:
        area_float = float(area.replace(',', '.'))
        return area_float > 0, area_float
    except ValueError:
        return False, None

# --- ОБРАБОТЧИКИ ПРОФИЛЯ ---
@dp.message(ProfileState.name)
async def process_name(m: types.Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2:
        await send_step(m, "❌ Имя слишком короткое. Введите полное имя и фамилию:", state=state)
        return
    
    await state.update_data(name=name)
    await state.set_state(ProfileState.phone)
    await send_step(m, "📞 Введите ваш номер телефона:", state=state)

@dp.message(ProfileState.phone)
async def process_phone(m: types.Message, state: FSMContext):
    phone = m.text.strip()
    if not validate_phone(phone):
        await send_step(m, "❌ Неверный формат телефона. Введите номер в формате +998XXXXXXXXX:", state=state)
        return
    
    data = await state.get_data()
    prev_mode = data.get("app_mode", "staff")
    name = data["name"]

    save_user_profile(m.from_user.id, name, phone)

    await state.clear()
    await state.update_data(app_mode=prev_mode)

    if prev_mode == "client":
        await send_step(m, "✅ Профиль успешно создан!", reply_markup=client_menu_kb(), state=state)
    else:
        await send_step(m, "✅ Профиль успешно создан!", reply_markup=main_menu_kb(m.from_user.id), state=state)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())