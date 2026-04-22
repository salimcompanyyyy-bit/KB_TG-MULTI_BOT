import asyncio
import csv
import json
import sqlite3
import logging
from typing import Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InputMediaPhoto, InputMediaVideo
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# --- КОНФИГ ---
API_TOKEN = 'REDACTED_HISTORICAL_LEAK'
CHANNEL_ID = '@KapitalBank_Assets' 
OWNER_ID = 120960192  
DB_PATH = "database.db"

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)


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


def _migrate_posts_channel_message_id():
    try:
        with db_connect() as conn:
            conn.execute('ALTER TABLE posts ADD COLUMN channel_message_id INTEGER')
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


def save_published_post(user_id: int, data: dict, channel_message_id: Optional[int]):
    media_files = data.get('media_files') or []
    media_str = json.dumps(media_files) if isinstance(media_files, list) else str(media_files or '')
    useful = data.get('useful_area')
    if useful == '':
        useful = None
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO posts (user_id, category, realty_type, city, district, street, house,
            total_area, useful_area, rooms, desc, price_val, price_cur, media_type, media_files, channel_message_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
            ),
        )
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
        FROM posts WHERE {' AND '.join(where)} AND channel_message_id IS NOT NULL
        ORDER BY published_date DESC LIMIT ? OFFSET ?"""
    params.extend([limit, offset])
    with db_connect() as conn:
        return conn.execute(sql, params).fetchall()

def is_allowed(u_id):
    if u_id == OWNER_ID: return True
    with db_connect() as conn:
        res = conn.execute("SELECT id FROM users WHERE id=?", (u_id,)).fetchone()
        return res is not None

init_db()

# --- СПИСКИ ГОРОДОВ ---
CITIES = {
    "Ташкент": ["Мирабадский", "Юнусабадский", "Мирзо-Улугбекский", "Чиланзарский", "Яккасарайский", "Шайхантахурский", "Алмазарский", "Сергелийский", "Яшнабадский", "Учтепинский"],
    "Самарканд": ["Сиабский", "Багишамальский", "Железнодорожный"],
    "Другой город": [] 
}

CITY_ORDER = list(CITIES.keys())
SEARCH_CATEGORIES = ["Жилое", "Нежилое", "Спецтехника", "Оборудование"]

BTN_ROLE_STAFF = "👔 Сотрудник"
BTN_ROLE_CLIENT = "🛒 Клиент"
BTN_ROLE_SWITCH = "↩️ Сменить режим"
BTN_SEARCH = "🔍 Поиск объявлений"


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

class AdminState(StatesGroup):
    add_id = State()


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
    if u_id == OWNER_ID:
        kb.button(text="⚙️ Админ-панель")
    kb.button(text=BTN_ROLE_SWITCH)
    return kb.adjust(1).as_markup(resize_keyboard=True)

def back_btn():
    return InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data="go_back").as_markup()


def build_client_city_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Любой город", callback_data="cl_ci_any")
    for i, city in enumerate(CITY_ORDER):
        kb.button(text=city[:30], callback_data=f"cl_ci_{i}")
    kb.button(text="Назад", callback_data="cl_cancel")
    return kb.adjust(2).as_markup()


def build_client_category_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Любая категория", callback_data="cl_ca_any")
    for i, cat in enumerate(SEARCH_CATEGORIES):
        kb.button(text=cat, callback_data=f"cl_ca_{i}")
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
    kb.button(text="📤 Экспорт stats", callback_data="export_stats")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_logs_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Логи", callback_data="adm_logs")
    kb.button(text="📤 Экспорт logs", callback_data="export_logs")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_staff_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Сотрудники", callback_data="adm_staff_list")
    kb.button(text="📥 Заявки доступа", callback_data="adm_access_requests")
    kb.button(text="➕ Добавить ID сотрудника", callback_data="adm_add")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_data_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🗄 DB check", callback_data="adm_dbcheck")
    kb.button(text="📥 Импорт CSV", callback_data="adm_importcsv")
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    return kb.adjust(1).as_markup()


def build_admin_service_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="⚠️ Тех. перерыв (рассылка)", callback_data="adm_maint")
    kb.button(text="🔎 Поиск публикаций", callback_data="adm_search_posts")
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
        "📊 <b>Kapital Assets</b>\n\nВыберите, кто вы сейчас:",
        reply_markup=role_select_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_ROLE_STAFF)
async def role_pick_staff(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id):
        await m.answer(
            "❌ Доступ сотрудника не оформлен. Обратитесь к администратору или выберите «Клиент».",
            reply_markup=role_select_kb(),
            parse_mode="HTML",
        )
        return
    await state.update_data(app_mode="staff")
    await m.answer(
        "👔 <b>Режим сотрудника</b>\nПубликация карточек и личный кабинет.",
        reply_markup=main_menu_kb(m.from_user.id),
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_ROLE_CLIENT)
async def role_pick_client(m: types.Message, state: FSMContext):
    await state.update_data(app_mode="client")
    await m.answer(
        "🛒 <b>Режим клиента</b>\nНиже — поиск по объявлениям в канале (фильтры).",
        reply_markup=client_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(F.text == BTN_ROLE_SWITCH)
async def role_switch(m: types.Message, state: FSMContext):
    await state.clear()
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
        build_client_category_kb(),
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
        lines.append(f"• <b>{cat}</b> / {rtype}\n  📍 {rcity}, {district or '—'}\n  💰 {price_h}\n")
        url = channel_post_url(ch_mid)
        if url:
            short = (rtype or "объект")[:18]
            ib.button(text=f"📌 {short}", url=url)
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
@dp.message(F.text == "⚙️ Админ-панель", F.from_user.id == OWNER_ID)
async def admin_panel(m: types.Message, state: FSMContext):
    await state.clear()
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
    await send_step(c, "👥 <b>Раздел: Сотрудники</b>", build_admin_staff_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_menu_data")
async def adm_menu_data(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, "🗄 <b>Раздел: База / импорт</b>", build_admin_data_kb(), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_menu_service")
async def adm_menu_service(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, "⚙️ <b>Раздел: Служебное</b>", build_admin_service_kb(), state=state)
    await c.answer()


@dp.message(F.text == "👤 Личный кабинет")
async def profile_handler(m: types.Message, state: FSMContext):
    prev = (await state.get_data()).get("app_mode", "staff")
    await state.clear()
    await state.update_data(app_mode=prev)
    with db_connect() as conn:
        p = conn.execute("SELECT name, phone FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    
    if p and p[0]:
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Изменить данные", callback_data="edit_p")
        kb.button(text="📋 Мои публикации", callback_data="my_posts")
        kb.button(text="⬅️ Назад", callback_data="go_back")
        await send_step(m, f"👤 <b>Профиль:</b> {p[0]}\n📞 <b>Тел:</b> {p[1]}", kb.adjust(2).as_markup(), state)
    else:
        await send_step(m, "Введите ваше Имя и Фамилию:", state=state)
        await state.set_state(ProfileState.name)

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
            conn.execute("INSERT INTO users (id, name, phone, role) VALUES (?, ?, ?, ?)", 
                        (user_id, "Новый сотрудник", "", "staff"))
        
        await state.clear()
        await state.update_data(app_mode="staff")
        await send_step(m, f"✅ Сотрудник с ID {user_id} успешно добавлен!", reply_markup=main_menu_kb(m.from_user.id), state=state)
        
    except ValueError:
        await send_step(m, "❌ Неверный формат ID. Введите число.", state=state)

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(c: types.CallbackQuery):
    # Статистика временно скрыта
    await send_step(c, "📊 Статистика публикаций:\n\n⚠️ Функция в разработке. Доступна позже.", state=None)
    await c.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(app_mode="staff")
    await send_step(c, "⚙️ <b>Панель администратора</b>", build_admin_panel_kb(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ НОВЫХ ФУНКЦИЙ ---
@dp.callback_query(F.data == "my_posts")
async def my_posts(c: types.CallbackQuery):
    with db_connect() as conn:
        # Получаем последние 10 публикаций сотрудника
        posts = conn.execute("""
            SELECT s.date, u.name 
            FROM stats s 
            JOIN users u ON s.user_id = u.id 
            WHERE s.user_id = ? 
            ORDER BY s.date DESC 
            LIMIT 10
        """, (c.from_user.id,)).fetchall()
    
    if not posts:
        await send_step(c, "📋 Ваши публикации:\n\n❌ Нет данных", state=None)
        return
    
    # Формируем сообщение с публикациями
    posts_text = "📋 Ваши последние публикации:\n\n"
    for i, (date, name) in enumerate(posts, 1):
        posts_text += f"{i}. {name} - {date}\n"
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="go_back")
    await send_step(c.message, posts_text, kb.adjust(1).as_markup(), state=None)
    await c.answer()

@dp.callback_query(F.data == "adm_logs")
async def adm_logs(c: types.CallbackQuery):
    # Логи временно скрыты
    await send_step(c, "📋 Логи действий:\n\n⚠️ Функция в разработке. Доступна позже.", state=None)
    await c.answer()

@dp.callback_query(F.data == "export_stats")
async def export_stats(c: types.CallbackQuery):
    # Экспорт статистики временно скрыт
    await send_step(c, "📊 Экспорт статистики в Excel:\n\n⚠️ Функция в разработке. Доступна позже.", state=None)
    await c.answer()

@dp.callback_query(F.data == "export_logs")
async def export_logs(c: types.CallbackQuery):
    # Экспорт логов временно скрыт
    await send_step(c, "📊 Экспорт логов в Excel:\n\n⚠️ Функция в разработке. Доступна позже.", state=None)
    await c.answer()


@dp.callback_query(F.data == "adm_dbcheck")
async def adm_dbcheck(c: types.CallbackQuery, state: FSMContext):
    await send_step(c, build_dbcheck_text(10), state=state)
    await c.answer()


@dp.callback_query(F.data == "adm_importcsv")
async def adm_importcsv(c: types.CallbackQuery, state: FSMContext):
    await show_import_csv_prompt(c.message, state)
    await c.answer()


@dp.callback_query(F.data == "adm_access_requests")
async def adm_access_requests(c: types.CallbackQuery):
    await c.answer("Заявки доступа: функция в разработке", show_alert=True)


@dp.callback_query(F.data == "adm_staff_list")
async def adm_staff_list(c: types.CallbackQuery):
    await c.answer("Список сотрудников: функция в разработке", show_alert=True)


@dp.callback_query(F.data == "adm_search_posts")
async def adm_search_posts(c: types.CallbackQuery):
    await c.answer("Поиск публикаций: функция в разработке", show_alert=True)

# --- СОЗДАНИЕ КАРТОЧКИ ---
@dp.message(F.text == "➕ Создать карточку объекта")
async def start_post(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id): 
        await send_step(m, "❌ У вас нет доступа к этой функции.", state=state)
        return
    if (await state.get_data()).get("app_mode") != "staff":
        await send_step(m, "Сначала выберите режим <b>«👔 Сотрудник»</b>.", reply_markup=role_select_kb(), state=state)
        return
    
    await state.clear()
    await state.update_data(app_mode="staff")
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
    
    await state.update_data(city=city_key)
    await state.set_state(PostState.district)
    
    kb = InlineKeyboardBuilder()
    if CITIES[city_key]:
        for district in CITIES[city_key]:
            kb.button(text=district, callback_data=f"district_{district}")
    else:
        kb.button(text="Введите вручную", callback_data="district_manual")
    
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    await send_step(c.message, f"Выбран город: <b>{city_key}</b>\nВыберите район:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ РАЙОНОВ ---
@dp.callback_query(F.data.startswith("district_"))
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
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    await send_step(c.message, "Введите название района вручную:", kb.adjust(1).as_markup(), state)
    await c.answer()

@dp.callback_query(F.data == "back_to_district")
async def back_to_district(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.district)
    city = (await state.get_data()).get('city', '')
    
    kb = InlineKeyboardBuilder()
    if CITIES[city]:
        for district in CITIES[city]:
            kb.button(text=district, callback_data=f"district_{district}")
    else:
        kb.button(text="Введите вручную", callback_data="district_manual")
    
    kb.button(text="⬅️ Назад", callback_data="back_to_city")
    await send_step(c.message, f"Выбран город: <b>{city}</b>\nВыберите район:", kb.adjust(2).as_markup(), state)
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
@dp.callback_query(F.data.startswith("media_"))
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
        await state.update_data(media_type="mixed", media_files=[])
        await state.set_state(PostState.media_file)
        await send_step(
            c.message,
            "Отправьте до 10 фото/видео (можно одним альбомом).\n"
            "Если ошиблись — удалите последнюю или очистите список.",
            build_media_manage_kb(),
            state,
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
        await send_step(m, "⚠️ Достигнут лимит 10 файлов. Нажмите «✅ Готово».", build_media_manage_kb(), state)
        return

    current.append(item)
    await state.update_data(media_type="mixed", media_files=current)
    last_t = "фото" if item.get("type") == "photo" else "видео"
    await send_step(
        m,
        f"✅ Добавлен(о): <b>{last_t}</b>\n{media_progress_text(current)}\n\n"
        "Можно догружать дальше или нажмите «✅ Готово».",
        build_media_manage_kb(),
        state,
    )


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
    await state.update_data(media_files=current, media_type="mixed")
    removed_type = "фото" if isinstance(removed, dict) and removed.get("type") == "photo" else "видео"
    await send_step(
        c.message,
        f"🗑 Удалена последняя: <b>{removed_type}</b>\n{media_progress_text(current)}",
        build_media_manage_kb(),
        state,
    )
    await c.answer()


@dp.callback_query(F.data == "media_clear_all")
async def media_clear_all(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(media_files=[], media_type="mixed")
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


def build_card_text(data: dict, employee_name: str) -> str:
    """Единый рендер карточки (для превью и публикации)."""
    category = data.get('category', '')
    realty_type = data.get('realty_type', '')
    city = data.get('city', '')
    district = data.get('district', '')
    street = data.get('street', '')
    house = data.get('house', '')
    total_area = data.get('total_area', '')
    useful_area = data.get('useful_area', '')
    rooms = data.get('rooms', '')
    desc = data.get('desc', '')
    price_val = data.get('price_val', '')
    price_cur = data.get('price_cur', '')

    area_text = ""
    if total_area:
        if useful_area:
            area_text = f"📐 Площадь: {total_area} м² (полезная: {useful_area} м²)"
        else:
            area_text = f"📐 Площадь: {total_area} м²"

    rooms_text = f"🔢 Комнат: {rooms}" if rooms else ""
    price_text = f"💰 ЦЕНА: {price_val:,} {price_cur}".replace(",", " ") if price_val and price_cur else ""
    desc_text = f"📝 Детали: {desc}" if desc else ""

    card_text = "━━━━━━━━━━━━━━━━━━━━\n"
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
        card_text += f"{desc_text}\n\n"
    if price_text:
        card_text += f"{price_text}\n"
    card_text += f"📞 Контакт: {employee_name}\n"
    card_text += "━━━━━━━━━━━━━━━━━━━━"
    return card_text


# --- ФОРМИРОВАНИЕ КАРТОЧКИ И ПРЕВЬЮ ---
async def show_preview(m_obj, state: FSMContext):
    data = await state.get_data()

    # Получаем имя сотрудника
    with db_connect() as conn:
        user = conn.execute("SELECT name FROM users WHERE id=?", (m_obj.from_user.id,)).fetchone()
    
    employee_name = user[0] if user else "Сотрудник"
    card_text = build_card_text(data, employee_name)
    
    # Отправляем превью
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data="publish")
    kb.button(text="❌ Отмена", callback_data="cancel_publish")
    kb.button(text="⬅️ Назад", callback_data="back_to_media")
    
    await send_step(m_obj, f"Предпросмотр карточки:\n\n{card_text}", kb.adjust(2).as_markup(), state)

@dp.callback_query(F.data == "publish")
async def publish_post(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()

    category = data.get('category', '')
    realty_type = data.get('realty_type', '')
    # Получаем имя сотрудника
    with db_connect() as conn:
        user = conn.execute("SELECT name FROM users WHERE id=?", (c.from_user.id,)).fetchone()
    
    employee_name = user[0] if user else "Сотрудник"
    card_text = build_card_text(data, employee_name)
    
    # Публикуем в канал
    try:
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
                    sent_msg = await bot.send_video(CHANNEL_ID, only_fid, caption=card_text, parse_mode="HTML")
                else:
                    sent_msg = await bot.send_photo(CHANNEL_ID, only_fid, caption=card_text, parse_mode="HTML")
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
            sent_msg = await bot.send_message(CHANNEL_ID, card_text, parse_mode="HTML")
        ch_mid = sent_msg.message_id if sent_msg else None
        save_published_post(c.from_user.id, data, ch_mid)
        # Логируем публикацию
        with db_connect() as conn:
            conn.execute("INSERT INTO stats (user_id) VALUES (?)", (c.from_user.id,))
            conn.execute("INSERT INTO logs (user_id, action, details) VALUES (?, ?, ?)", 
                        (c.from_user.id, "Публикация", f"Опубликована карточка: {category} - {realty_type}"))
        await state.clear()
        await state.update_data(app_mode="staff")
        await send_step(c, "✅ Карточка успешно опубликована в канале!", reply_markup=main_menu_kb(c.from_user.id), state=state)
        
    except Exception as e:
        logging.error(f"Failed to publish post: {e}")
        await send_step(c, "❌ Ошибка при публикации. Попробуйте еще раз.", state=state)

@dp.callback_query(F.data == "cancel_publish")
async def cancel_publish(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(app_mode="staff")
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
    await state.clear()
    if mode == "client":
        await state.update_data(app_mode="client")
        await send_step(c, "Меню клиента.", reply_markup=client_menu_kb(), state=state)
    else:
        await state.update_data(app_mode="staff")
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
    
    with db_connect() as conn:
        conn.execute("INSERT OR REPLACE INTO users (id, name, phone) VALUES (?, ?, ?)", 
                    (m.from_user.id, (await state.get_data())['name'], phone))
    
    prev_mode = (await state.get_data()).get("app_mode", "staff")
    await state.clear()
    if prev_mode == "client":
        await state.update_data(app_mode="client")
        await send_step(m, "✅ Профиль успешно создан!", reply_markup=client_menu_kb(), state=state)
    else:
        await state.update_data(app_mode="staff")
        await send_step(m, "✅ Профиль успешно создан!", reply_markup=main_menu_kb(m.from_user.id), state=state)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())