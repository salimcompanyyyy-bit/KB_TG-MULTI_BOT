import asyncio
import sqlite3
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# --- КОНФИГ ---
API_TOKEN = 'REDACTED_HISTORICAL_LEAK'
CHANNEL_ID = '@KapitalBank_Assets' 
OWNER_ID = 120960192  

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
logging.basicConfig(level=logging.INFO)

# --- БД (Авто-создание правильной структуры) ---
def init_db():
    conn = sqlite3.connect('database.db')
    conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, role TEXT DEFAULT "staff")')
    conn.execute('CREATE TABLE IF NOT EXISTS stats (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    conn.commit()
    conn.close()

def is_allowed(u_id):
    if u_id == OWNER_ID: return True
    with sqlite3.connect('database.db') as conn:
        res = conn.execute("SELECT id FROM users WHERE id=?", (u_id,)).fetchone()
        return res is not None

init_db()

# --- СПИСКИ ГОРОДОВ ---
CITIES = {
    "Ташкент": ["Мирабадский", "Юнусабадский", "Мирзо-Улугбекский", "Чиланзарский", "Яккасарайский", "Шайхантахурский", "Алмазарский", "Сергелийский", "Яшнабадский", "Учтепинский"],
    "Самарканд": ["Сиабский", "Багишамальский", "Железнодорожный"],
    "Другой город": [] 
}

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

# --- ФУНКЦИЯ ЧИСТКИ ЧАТА ---
async def send_step(m_obj, text, reply_markup=None, state: FSMContext = None):
    try:
        # Определяем chat_id в зависимости от типа объекта
        chat_id = m_obj.chat.id if hasattr(m_obj, 'chat') else m_obj.message.chat.id
        
        data = await state.get_data()
        last_msg = data.get("last_msg_id")
        
        if last_msg:
            try:
                await bot.delete_message(chat_id, last_msg)
            except Exception as e:
                # Логируем ошибку, но не падаем
                logging.warning(f"Failed to delete message {last_msg}: {e}")
        
        new_msg = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML")
        await state.update_data(last_msg_id=new_msg.message_id)
    except Exception as e:
        logging.error(f"Error in send_step: {e}")
        # Если что-то пошло не так, отправляем сообщение без удаления предыдущего
        if hasattr(m_obj, 'chat'):
            await bot.send_message(m_obj.chat.id, text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await bot.send_message(m_obj.message.chat.id, text, reply_markup=reply_markup, parse_mode="HTML")

def main_menu_kb(u_id):
    kb = ReplyKeyboardBuilder()
    kb.button(text="➕ Создать карточку объекта")
    kb.button(text="👤 Личный кабинет")
    if u_id == OWNER_ID: kb.button(text="⚙️ Админ-панель")
    return kb.adjust(1).as_markup(resize_keyboard=True)

def back_btn():
    return InlineKeyboardBuilder().button(text="⬅️ Назад", callback_data="go_back").as_markup()

# --- ГЛОБАЛЬНЫЕ КНОПКИ (СБРОС ЗАВИСАНИЙ) ---
@dp.message(F.text == "⚙️ Админ-панель", F.from_user.id == OWNER_ID)
async def admin_panel(m: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ID сотрудника", callback_data="adm_add")
    kb.button(text="⚠️ Тех. перерыв (рассылка)", callback_data="adm_maint")
    kb.button(text="📊 Статистика", callback_data="adm_stats")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    await send_step(m, "⚙️ <b>Панель администратора</b>", kb.adjust(2).as_markup(), state)

@dp.message(F.text == "👤 Личный кабинет")
async def profile_handler(m: types.Message, state: FSMContext):
    await state.clear()
    with sqlite3.connect('database.db') as conn:
        p = conn.execute("SELECT name, phone FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    
    if p and p[0]:
        kb = InlineKeyboardBuilder().button(text="📝 Изменить данные", callback_data="edit_p").as_markup()
        await send_step(m, f"👤 <b>Профиль:</b> {p[0]}\n📞 <b>Тел:</b> {p[1]}", kb, state)
    else:
        await send_step(m, "Введите ваше Имя и Фамилию:", state=state)
        await state.set_state(ProfileState.name)

# --- ЛОГИКА АДМИНКИ ---
@dp.callback_query(F.data == "adm_maint")
async def maintenance(c: types.CallbackQuery):
    with sqlite3.connect('database.db') as conn:
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
        with sqlite3.connect('database.db') as conn:
            existing = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        
        if existing:
            await send_step(m, "❌ Пользователь с таким ID уже существует.", state=state)
            return
        
        # Добавляем пользователя
        with sqlite3.connect('database.db') as conn:
            conn.execute("INSERT INTO users (id, name, phone, role) VALUES (?, ?, ?, ?)", 
                        (user_id, "Новый сотрудник", "", "staff"))
        
        await state.clear()
        await send_step(m, f"✅ Сотрудник с ID {user_id} успешно добавлен!", reply_markup=main_menu_kb(m.from_user.id), state=state)
        
    except ValueError:
        await send_step(m, "❌ Неверный формат ID. Введите число.", state=state)

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(c: types.CallbackQuery):
    with sqlite3.connect('database.db') as conn:
        # Получаем статистику по сотрудникам
        stats = conn.execute("""
            SELECT u.name, u.phone, COUNT(s.id) as posts_count
            FROM users u
            LEFT JOIN stats s ON u.id = s.user_id
            WHERE u.role = 'staff'
            GROUP BY u.id
            ORDER BY posts_count DESC
        """).fetchall()
    
    if not stats:
        await send_step(c, "📊 Статистика публикаций:\n\n❌ Нет данных", state=None)
        return
    
    # Формируем сообщение со статистикой
    stats_text = "📊 Статистика публикаций сотрудников:\n\n"
    for i, (name, phone, count) in enumerate(stats, 1):
        stats_text += f"{i}. {name} ({phone}) - {count} публикаций\n"
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="back_to_admin")
    await send_step(c.message, stats_text, kb.adjust(1).as_markup(), state=None)
    await c.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ID сотрудника", callback_data="adm_add")
    kb.button(text="⚠️ Тех. перерыв (рассылка)", callback_data="adm_maint")
    kb.button(text="📊 Статистика", callback_data="adm_stats")
    kb.button(text="⬅️ Назад", callback_data="go_back")
    await send_step(c, "⚙️ <b>Панель администратора</b>", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- СОЗДАНИЕ КАРТОЧКИ ---
@dp.message(F.text == "➕ Создать карточку объекта")
async def start_post(m: types.Message, state: FSMContext):
    if not is_allowed(m.from_user.id): 
        await send_step(m, "❌ У вас нет доступа к этой функции.", state=state)
        return
    
    await state.clear()
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
    await state.set_state(PostState.desc)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="desc_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_useful_area")
    await send_step(c.message, "Введите описание объекта:", kb.adjust(2).as_markup(), state)
    await c.answer()

@dp.message(PostState.useful_area)
async def process_useful_area(m: types.Message, state: FSMContext):
    area = m.text.strip()
    valid, area_float = validate_area(area)
    if not valid:
        await send_step(m, "❌ Неверный формат площади. Введите число (например: 65 или 65.5):", state=state)
        return
    
    await state.update_data(useful_area=area_float)
    await state.set_state(PostState.desc)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="desc_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_useful_area")
    await send_step(m, "Введите описание объекта:", kb.adjust(2).as_markup(), state)

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
    kb.button(text="📸 Фото", callback_data="media_photo")
    kb.button(text="🎥 Видео", callback_data="media_video")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Выберите тип медиа:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ОБРАБОТЧИКИ МЕДИА ---
@dp.callback_query(F.data.startswith("media_"))
async def handle_media_choice(c: types.CallbackQuery, state: FSMContext):
    media_map = {
        "media_photo": "photo",
        "media_video": "video",
        "media_skip": "skip"
    }
    
    media_type = media_map.get(c.data)
    if not media_type:
        await c.answer("❌ Неизвестный тип медиа", show_alert=True)
        return
    
    await state.update_data(media_type=media_type)
    
    if media_type == "skip":
        await state.set_state(PostState.preview)
        await show_preview(c.message, state)
    else:
        await state.set_state(PostState.media_file)
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="back_to_media_choice")
        await send_step(c.message, f"Отправьте {media_type}:", kb.adjust(1).as_markup(), state)
    
    await c.answer()

@dp.message(PostState.media_file)
async def process_media(m: types.Message, state: FSMContext):
    data = await state.get_data()
    media_type = data.get('media_type')
    
    if media_type == "photo" and m.photo:
        # Сохраняем file_id фото
        photo_id = m.photo[-1].file_id
        await state.update_data(media_files=[photo_id])
        await state.set_state(PostState.preview)
        await show_preview(m, state)
    elif media_type == "video" and m.video:
        # Сохраняем file_id видео
        video_id = m.video.file_id
        await state.update_data(media_files=[video_id])
        await state.set_state(PostState.preview)
        await show_preview(m, state)
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="back_to_media_choice")
        await send_step(m, f"❌ Отправьте {media_type}. Попробуйте еще раз:", kb.adjust(1).as_markup(), state)

@dp.callback_query(F.data == "back_to_media_choice")
async def back_to_media_choice(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.media_choice)
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data="media_photo")
    kb.button(text="🎥 Видео", callback_data="media_video")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Выберите тип медиа:", kb.adjust(2).as_markup(), state)
    await c.answer()

# --- ФОРМИРОВАНИЕ КАРТОЧКИ И ПРЕВЬЮ ---
async def show_preview(m_obj, state: FSMContext):
    data = await state.get_data()
    
    # Формируем текст карточки
    category = data.get('category', '')
    realty_type = data.get('realty_type', '')
    city = data.get('city', '')
    district = data.get('district', '')
    street = data.get('street', '')
    house = data.get('house', '')
    total_area = data.get('total_area', '')
    useful_area = data.get('useful_area', '')
    desc = data.get('desc', '')
    price_val = data.get('price_val', '')
    price_cur = data.get('price_cur', '')
    
    # Формируем адрес
    address_parts = [city, district]
    if street and house:
        address_parts.append(f"{street}, {house}")
    elif street:
        address_parts.append(street)
    
    address = ", ".join(address_parts)
    
    # Формируем площадь
    area_text = ""
    if total_area:
        if useful_area:
            area_text = f"📐 {total_area} м² (полезная: {useful_area} м²)"
        else:
            area_text = f"📐 {total_area} м²"
    
    # Формируем цену
    price_text = ""
    if price_val and price_cur:
        price_text = f"💰 {price_val:,} {price_cur}".replace(",", " ")
    
    # Формируем описание
    desc_text = ""
    if desc:
        desc_text = f"\n\n📝 {desc}"
    
    # Формируем итоговую карточку
    card_text = f"<b>{category}</b>\n"
    card_text += f"<b>{realty_type}</b>\n\n"
    card_text += f"📍 {address}\n"
    if area_text:
        card_text += f"{area_text}\n"
    if price_text:
        card_text += f"{price_text}\n"
    card_text += desc_text
    
    # Получаем имя сотрудника
    with sqlite3.connect('database.db') as conn:
        user = conn.execute("SELECT name FROM users WHERE id=?", (m_obj.from_user.id,)).fetchone()
    
    employee_name = user[0] if user else "Сотрудник"
    card_text += f"\n\n📞 {employee_name}"
    
    # Отправляем превью
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать", callback_data="publish")
    kb.button(text="❌ Отмена", callback_data="cancel_publish")
    kb.button(text="⬅️ Назад", callback_data="back_to_media")
    
    await send_step(m_obj, f"Предпросмотр карточки:\n\n{card_text}", kb.adjust(2).as_markup(), state)

@dp.callback_query(F.data == "publish")
async def publish_post(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    # Формируем карточку для публикации (аналогично show_preview)
    category = data.get('category', '')
    realty_type = data.get('realty_type', '')
    city = data.get('city', '')
    district = data.get('district', '')
    street = data.get('street', '')
    house = data.get('house', '')
    total_area = data.get('total_area', '')
    useful_area = data.get('useful_area', '')
    desc = data.get('desc', '')
    price_val = data.get('price_val', '')
    price_cur = data.get('price_cur', '')
    
    address_parts = [city, district]
    if street and house:
        address_parts.append(f"{street}, {house}")
    elif street:
        address_parts.append(street)
    
    address = ", ".join(address_parts)
    
    area_text = ""
    if total_area:
        if useful_area:
            area_text = f"📐 {total_area} м² (полезная: {useful_area} м²)"
        else:
            area_text = f"📐 {total_area} м²"
    
    price_text = ""
    if price_val and price_cur:
        price_text = f"💰 {price_val:,} {price_cur}".replace(",", " ")
    
    desc_text = ""
    if desc:
        desc_text = f"\n\n📝 {desc}"
    
    card_text = f"<b>{category}</b>\n"
    card_text += f"<b>{realty_type}</b>\n\n"
    card_text += f"📍 {address}\n"
    if area_text:
        card_text += f"{area_text}\n"
    if price_text:
        card_text += f"{price_text}\n"
    card_text += desc_text
    
    with sqlite3.connect('database.db') as conn:
        user = conn.execute("SELECT name FROM users WHERE id=?", (c.from_user.id,)).fetchone()
    
    employee_name = user[0] if user else "Сотрудник"
    card_text += f"\n\n📞 {employee_name}"
    
    # Публикуем в канал
    try:
        media_type = data.get('media_type')
        media_files = data.get('media_files', [])
        
        if media_type == "photo" and media_files:
            await bot.send_photo(CHANNEL_ID, media_files[0], caption=card_text, parse_mode="HTML")
        elif media_type == "video" and media_files:
            await bot.send_video(CHANNEL_ID, media_files[0], caption=card_text, parse_mode="HTML")
        else:
            await bot.send_message(CHANNEL_ID, card_text, parse_mode="HTML")
        
        # Логируем публикацию
        with sqlite3.connect('database.db') as conn:
            conn.execute("INSERT INTO stats (user_id) VALUES (?)", (c.from_user.id,))
        
        await send_step(c, "✅ Карточка успешно опубликована в канале!", reply_markup=main_menu_kb(c.from_user.id), state=state)
        
    except Exception as e:
        logging.error(f"Failed to publish post: {e}")
        await send_step(c, "❌ Ошибка при публикации. Попробуйте еще раз.", state=state)

@dp.callback_query(F.data == "cancel_publish")
async def cancel_publish(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await send_step(c, "Публикация отменена. Вы вернулись в главное меню", reply_markup=main_menu_kb(c.from_user.id), state=state)
    await c.answer()

@dp.callback_query(F.data == "back_to_media")
async def back_to_media(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(PostState.media_choice)
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Фото", callback_data="media_photo")
    kb.button(text="🎥 Видео", callback_data="media_video")
    kb.button(text="Пропустить", callback_data="media_skip")
    kb.button(text="⬅️ Назад", callback_data="back_to_price_cur")
    await send_step(c.message, "Выберите тип медиа:", kb.adjust(2).as_markup(), state)
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
    await state.clear()
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

@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("📊 <b>Kapital Assets</b>", reply_markup=main_menu_kb(m.from_user.id), parse_mode="HTML")
    await state.update_data(last_msg_id=msg.message_id)

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
    
    with sqlite3.connect('database.db') as conn:
        conn.execute("INSERT OR REPLACE INTO users (id, name, phone) VALUES (?, ?, ?)", 
                    (m.from_user.id, (await state.get_data())['name'], phone))
    
    await state.clear()
    await send_step(m, "✅ Профиль успешно создан!", reply_markup=main_menu_kb(m.from_user.id), state=state)

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())