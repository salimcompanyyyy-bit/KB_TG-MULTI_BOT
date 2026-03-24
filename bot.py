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
    cadastre = State()
    media_choice = State()
    media_file = State()

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
    await send_step(m, "⚙️ <b>Панель администратора</b>", kb.adjust(1).as_markup(), state)

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
        "type_room": "Комната",
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