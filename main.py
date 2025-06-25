import os
import asyncio
import uuid
from typing import Dict, Any, List, Tuple
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.enums import ChatType
from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger
import sqlite3
from contextlib import closing
import random
import aiohttp

# --- INITIAL SETUP ---
logger.add(
    "bot.log",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    rotation="1 week",
    compression="zip",
    level="DEBUG"
)

load_dotenv()
TOKEN = os.getenv("TOKEN")
ADMIN_USER_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_USER_IDS", "").split(',') if admin_id]
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")


if not TOKEN:
    raise ValueError("Не найден токен бота в переменных окружения!")
if not ADMIN_USER_IDS:
    logger.warning("Не найдены ID администраторов в переменных окружения! Некоторые команды будут недоступны.")
if not TARGET_CHANNEL_ID:
    logger.warning("Не найден ID целевого канала! Публикация в канал будет невозможна.")

# --- BOT & DISPATCHER ---
bot = Bot(TOKEN)
dp = Dispatcher()
dp['aiosession'] = aiohttp.ClientSession()

# --- GLOBAL STATE & CONSTANTS ---
remind_data = {}
FORBIDDEN_WORDS = {"дурак", "идиот", "хам"} # Simple profanity filter list

# Prefixes
DELETE_PREFIX = "del_"
CONFIRM_PREFIX = "cfm_"
CANCEL_PREFIX = "cnl_"
PAGE_PREFIX = "page_"
REMIND_PREFIX = "rem_"
ITEMS_PER_PAGE = 5
DB_NAME = "events.db"
REMINDER_CHECK_INTERVAL = 60 # Check every minute
DAILY_POST_TIME = "09:00" # Time for daily post in channel

# --- CUSTOM FILTERS ---
class IsAdmin(BaseFilter):
    """Фильтр для проверки, является ли пользователь администратором бота"""
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_USER_IDS

# --- SERVICE CLASSES ---
class Database:
    """Класс для работы с базой данных"""
    @staticmethod
    def init_db():
        with closing(sqlite3.connect(DB_NAME)) as conn:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        user_id INTEGER, event_id TEXT, date TEXT, text TEXT, remind_time TEXT,
                        PRIMARY KEY (user_id, event_id)
                    )
                """)

    @staticmethod
    async def execute_query(query: str, params: tuple = (), fetch: bool = False) -> Any:
        with closing(sqlite3.connect(DB_NAME)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query, params)
            if fetch:
                return cursor.fetchall()
            conn.commit()
            return cursor.rowcount

class EventManager:
    """Класс для управления событиями"""
    @staticmethod
    async def get_user_events(user_id: int) -> Dict[str, Dict[str, str]]:
        rows = await Database.execute_query("SELECT * FROM events WHERE user_id = ?", (user_id,), fetch=True)
        return {row["event_id"]: dict(row) for row in rows} if rows else {}

    @staticmethod
    async def add_event(user_id: int, event_id: str, date: str, text: str = "Мое событие", remind_time: str = None):
        await Database.execute_query(
            "INSERT INTO events (user_id, event_id, date, text, remind_time) VALUES (?, ?, ?, ?, ?)",
            (user_id, event_id, date, text, remind_time)
        )

    @staticmethod
    async def update_event_reminder(user_id: int, event_id: str, remind_time: str) -> bool:
        return await Database.execute_query(
            "UPDATE events SET remind_time = ? WHERE user_id = ? AND event_id = ?",
            (remind_time, user_id, event_id)
        ) > 0

    @staticmethod
    async def delete_event(user_id: int, event_id: str) -> bool:
        return await Database.execute_query("DELETE FROM events WHERE user_id = ? AND event_id = ?", (user_id, event_id)) > 0

    @staticmethod
    async def clear_user_events(user_id: int) -> int:
        count = (await Database.execute_query("SELECT COUNT(*) FROM events WHERE user_id = ?", (user_id,), fetch=True))[0][0]
        if count > 0:
            await Database.execute_query("DELETE FROM events WHERE user_id = ?", (user_id,))
        return count

    @staticmethod
    async def event_exists(user_id: int, date: str) -> bool:
        return bool(await Database.execute_query("SELECT 1 FROM events WHERE user_id = ? AND date = ? LIMIT 1", (user_id, date), fetch=True))

    @staticmethod
    async def get_events_for_reminder() -> List[Tuple[int, str, str]]:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        rows = await Database.execute_query("SELECT user_id, text, date FROM events WHERE remind_time = ?", (now,), fetch=True)
        return [(row['user_id'], row['text'], row['date']) for row in rows]

class ExternalContentManager:
    """Класс для получения контента с внешних API"""
    @staticmethod
    async def get_random_quote(session: aiohttp.ClientSession) -> str:
        try:
            async with session.get('https://api.quotable.io/random') as response:
                if response.status == 200:
                    data = await response.json()
                    return f"\"{data['content']}\" - {data['author']}"
                return "Не удалось получить цитату дня."
        except Exception as e:
            logger.error(f"Error fetching quote: {e}")
            return "Ошибка при загрузке цитаты."

    @staticmethod
    async def get_random_image_url() -> str:
        return "https://picsum.photos/800/600"

class KeyboardManager:
    """Класс для управления клавиатурами"""
    @staticmethod
    async def get_events_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
        events = await EventManager.get_user_events(user_id)
        events_list = list(events.items())
        total_pages = (len(events_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        page_events = events_list[page*ITEMS_PER_PAGE:(page+1)*ITEMS_PER_PAGE]
        
        keyboard = []
        for event_id, event_data in page_events:
            row = [InlineKeyboardButton(text=f"❌ {event_data['date']}", callback_data=f"{DELETE_PREFIX}{event_id}")]
            if not event_data['remind_time']:
                row.append(InlineKeyboardButton(text="⏰ Напомнить", callback_data=f"{REMIND_PREFIX}{event_id}"))
            keyboard.append(row)
        
        pagination_buttons = []
        if page > 0:
            pagination_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{PAGE_PREFIX}{page-1}"))
        if page < total_pages - 1:
            pagination_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"{PAGE_PREFIX}{page+1}"))
        
        if pagination_buttons: keyboard.append(pagination_buttons)
        if events_list: keyboard.append([InlineKeyboardButton(text="🗑️ Очистить все", callback_data="clear_all")])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    @staticmethod
    def get_confirmation_keyboard(confirm_data: str, cancel_data: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data=confirm_data),
            InlineKeyboardButton(text="❌ Нет", callback_data=cancel_data)
        ]])

    @staticmethod
    def get_back_keyboard(back_data: str = "back_to_events") -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=back_data)]])

class MessageManager:
    """Класс для управления сообщениями"""
    @staticmethod
    async def get_user_events_text(user_id: int, page: int) -> Tuple[str, int]:
        events = await EventManager.get_user_events(user_id)
        events_list = sorted(list(events.items()), key=lambda item: datetime.strptime(item[1]['date'], '%d.%m.%Y'))
        total_pages = (len(events_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        page_events = events_list[page*ITEMS_PER_PAGE:(page+1)*ITEMS_PER_PAGE]
        
        events_text = "📅 Ваши события:\n\n"
        if not page_events: return "📭 У вас нет сохраненных событий.", 0
        
        for i, (event_id, event_data) in enumerate(page_events, page*ITEMS_PER_PAGE + 1):
            reminder_info = f" (⏰ {event_data['remind_time'].split()[1]})" if event_data['remind_time'] else ""
            events_text += f"{i}. {event_data['date']} - {event_data['text']}{reminder_info}\n"
        
        return events_text, total_pages

    @staticmethod
    async def display_events_page(message: types.Message | types.CallbackQuery, user_id: int, page: int):
        events_text, total_pages = await MessageManager.get_user_events_text(user_id, page)
        keyboard = await KeyboardManager.get_events_keyboard(user_id, page)
        
        display_text = events_text
        if total_pages > 1:
            display_text += f"\nСтраница {page+1}/{total_pages}"
        
        if isinstance(message, types.CallbackQuery):
            await message.message.edit_text(display_text, reply_markup=keyboard)
        else:
            await message.answer(display_text, reply_markup=keyboard)

# --- BACKGROUND TASKS ---
async def remind_checker():
    """Проверяет и отправляет напоминания пользователям"""
    while True:
        try:
            events = await EventManager.get_events_for_reminder()
            for user_id, text, date in events:
                try:
                    await bot.send_message(user_id, f"⏰ **НАПОМИНАНИЕ** ⏰\n\nСобытие: {text}\nДата: {date}")
                except Exception as e:
                    logger.error(f"Ошибка при отправке напоминания пользователю {user_id}: {e}")
        except Exception as e:
            logger.error(f"Критическая ошибка в `remind_checker`: {e}")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)

async def daily_channel_post(session: aiohttp.ClientSession):
    """Ежедневно отправляет пост в канал"""
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == DAILY_POST_TIME:
            if not TARGET_CHANNEL_ID:
                logger.warning("Пропуск ежедневного поста: не задан TARGET_CHANNEL_ID.")
                await asyncio.sleep(60) # Проверить снова через минуту
                continue
            
            quote = await ExternalContentManager.get_random_quote(session)
            try:
                await bot.send_message(TARGET_CHANNEL_ID, f"**Цитата дня** ☀️\n\n{quote}")
                logger.info(f"Опубликована цитата дня в канале {TARGET_CHANNEL_ID}.")
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение в канал {TARGET_CHANNEL_ID}: {e}")
            await asyncio.sleep(86340) # Пауза почти на сутки
        await asyncio.sleep(30) # Проверять время каждые 30 секунд

# --- COMMAND HANDLERS ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "📅 **Личный бот-календарь**\n\nЯ помогу вам сохранить важные даты и напомню о них.\n"
            "Используйте /help, чтобы увидеть все команды."
        )
    else: # Group or Supergroup
        await message.answer(
            "👋 Привет, группа!\n\nЯ бот-помощник. В группах я умею фильтровать сообщения и играть в игры.\n"
            "Чтобы узнать больше, напишите /help."
        )

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "**Команды для личного пользования:**\n"
            "/calendar - Добавить событие в календарь\n"
            "/myevents - Показать мои события\n"
            "/today - Показать сегодняшнюю дату\n"
            "/clearevents - Очистить все мои события\n\n"
            "**Развлекательные команды:**\n"
            "/quote - Получить случайную цитату\n"
            "/image - Получить случайное изображение\n"
            "/sticker - Получить забавный стикер"
        )
    else: # Group or Supergroup
        await message.answer(
            "**Команды для групп:**\n"
            "/help - Показать это сообщение\n"
            "/play - Сыграть в кости\n\n"
            "Также я автоматически удаляю сообщения с нецензурной лексикой (если у меня есть права администратора)."
        )

@dp.message(F.chat.type == ChatType.PRIVATE, Command("today"))
async def today_cmd(message: types.Message):
    await message.answer(f"📆 Сегодня: {datetime.now().strftime('%d.%m.%Y')}")

@dp.message(F.chat.type == ChatType.PRIVATE, Command("calendar"))
async def calendar_cmd(message: types.Message):
    await message.answer("Выберите дату для добавления:", reply_markup=await SimpleCalendar().start_calendar())

@dp.message(F.chat.type == ChatType.PRIVATE, Command("myevents"))
async def show_events(message: types.Message):
    await MessageManager.display_events_page(message, message.from_user.id, 0)

@dp.message(F.chat.type == ChatType.PRIVATE, Command("clearevents"))
async def clear_events_cmd(message: types.Message):
    if await EventManager.get_user_events(message.from_user.id):
        await message.answer(
            "Вы уверены, что хотите удалить ВСЕ события?",
            reply_markup=KeyboardManager.get_confirmation_keyboard("confirm_clear_all", "cancel_clear_all")
        )
    else:
        await message.answer("У вас нет событий для удаления.")

# --- NEW CONTENT & GROUP COMMANDS ---
@dp.message(Command("quote"))
async def quote_cmd(message: types.Message, aiosession: aiohttp.ClientSession):
    quote = await ExternalContentManager.get_random_quote(aiosession)
    await message.answer(quote)

@dp.message(Command("image"))
async def image_cmd(message: types.Message):
    image_url = await ExternalContentManager.get_random_image_url()
    await message.answer_photo(image_url, caption=f"Ваше случайное изображение!")

@dp.message(Command("sticker"))
async def sticker_cmd(message: types.Message):
    # This is a hardcoded sticker ID. You can find IDs by sending a sticker to a bot like @JsonDumpBot
    sticker_id = "CAACAgIAAxkBAAEoD_ZmLc9r5U3g1tq7L3QXz55mMUSVjwACeAIAAladvQpG48o-sM84FTQE"
    await message.answer_sticker(sticker_id)

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("play"))
async def play_cmd(message: types.Message):
    await message.answer_dice(emoji="🎲")

@dp.message(IsAdmin(), Command("post"))
async def post_to_channel_cmd(message: types.Message):
    if not TARGET_CHANNEL_ID:
        await message.reply("Ошибка: ID целевого канала не настроен.")
        return
    
    command_parts = message.text.split(maxsplit=1)
    if len(command_parts) < 2:
        await message.reply("Пожалуйста, укажите текст для публикации. \nПример: `/post Привет, канал!`")
        return
        
    text_to_post = command_parts[1]
    try:
        await bot.send_message(TARGET_CHANNEL_ID, text_to_post)
        await message.reply("✅ Сообщение успешно отправлено в канал.")
        logger.info(f"Admin {message.from_user.id} posted to channel {TARGET_CHANNEL_ID}.")
    except Exception as e:
        await message.reply(f"❌ Не удалось отправить сообщение: {e}")
        logger.error(f"Failed to post to channel by admin {message.from_user.id}: {e}")

# --- GROUP MESSAGE HANDLER (PROFANITY FILTER) ---
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def filter_group_messages(message: types.Message):
    # Reminder state check for private chat (can be triggered from group if user replies to bot)
    if message.from_user.id in remind_data:
        await process_reminder_time(message) # Process reminder time first
        return
        
    text = message.text.lower()
    if any(word in text for word in FORBIDDEN_WORDS):
        try:
            await message.delete()
            await message.answer(f"Сообщение от {message.from_user.full_name} удалено за нарушение правил чата.")
            logger.info(f"Deleted message from {message.from_user.id} in group {message.chat.id} for profanity.")
        except Exception as e:
            logger.warning(f"Could not delete message in group {message.chat.id}. Maybe no admin rights? Error: {e}")

# --- CALLBACK HANDLERS (UNCHANGED CORE LOGIC) ---
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_simple_calendar(cb: types.CallbackQuery, callback_data: SimpleCalendarCallback):
    selected, date = await SimpleCalendar().process_selection(cb, callback_data)
    if not selected: return
    
    user_id = cb.from_user.id
    date_str = date.strftime('%d.%m.%Y')
    
    if await EventManager.event_exists(user_id, date_str):
        await cb.answer(f"⚠️ Дата {date_str} уже есть в календаре!", show_alert=True)
    else:
        event_id = str(uuid.uuid4())
        await EventManager.add_event(user_id, event_id, date_str)
        logger.success(f"User {user_id} added new date: {date_str} (ID: {event_id})")
        await cb.message.edit_text(f"✅ Добавлена дата: {date_str}", reply_markup=KeyboardManager.get_back_keyboard())

# --- Other callback handlers for event management ---
@dp.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_event_handler(cb: types.CallbackQuery):
    event_id = cb.data[len(DELETE_PREFIX):]
    user_id = cb.from_user.id
    events = await EventManager.get_user_events(user_id)
    if event_id not in events: return await cb.answer("Событие не найдено!", show_alert=True)
    await cb.message.edit_text(
        f"Вы точно хотите удалить событие на {events[event_id]['date']}?",
        reply_markup=KeyboardManager.get_confirmation_keyboard(f"{CONFIRM_PREFIX}{event_id}", f"{CANCEL_PREFIX}{event_id}")
    )

@dp.callback_query(F.data.startswith((CONFIRM_PREFIX, CANCEL_PREFIX)))
async def handle_confirmation(cb: types.CallbackQuery):
    prefix, event_id = cb.data.split("_", 1)
    user_id = cb.from_user.id
    events = await EventManager.get_user_events(user_id)
    if event_id not in events: return await cb.answer("Событие не найдено!", show_alert=True)

    event_date = events[event_id]['date']
    if prefix == CONFIRM_PREFIX[0:-1]: # 'cfm'
        if await EventManager.delete_event(user_id, event_id):
            logger.success(f"User {user_id} deleted event {event_id} ({event_date})")
            await cb.message.edit_text(f"🗑️ Событие на {event_date} удалено!", reply_markup=KeyboardManager.get_back_keyboard())
        else:
            await cb.message.edit_text("Ошибка при удалении", reply_markup=KeyboardManager.get_back_keyboard())
    else: # 'cnl'
        await cb.message.edit_text(f"❌ Удаление отменено", reply_markup=KeyboardManager.get_back_keyboard())

@dp.callback_query(F.data.startswith(REMIND_PREFIX))
async def set_reminder_handler(cb: types.CallbackQuery):
    event_id = cb.data[len(REMIND_PREFIX):]
    user_id = cb.from_user.id
    events = await EventManager.get_user_events(user_id)
    if event_id not in events: return await cb.answer("Событие не найдено!", show_alert=True)
    
    remind_data[user_id] = {"remind_event_id": event_id}
    await cb.message.answer(f"⏰ Введите время напоминания для {events[event_id]['date']} (в формате ЧЧ:ММ):")
    await cb.answer()

@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def process_reminder_time(message: types.Message):
    user_id = message.from_user.id
    if user_id not in remind_data or "remind_event_id" not in remind_data[user_id]:
        # This is not a reminder time, maybe some other text, ignore or handle differently
        await message.reply("Неизвестная команда. Используйте /help для списка команд.")
        return

    try:
        time_obj = datetime.strptime(message.text.strip(), "%H:%M").time()
        event_id = remind_data[user_id]["remind_event_id"]
        events = await EventManager.get_user_events(user_id)
        if event_id not in events:
            await message.answer("Событие не найдено.")
            return

        event_date = events[event_id]['date']
        remind_time_str = f"{event_date} {time_obj.strftime('%H:%M')}"
        
        if await EventManager.update_event_reminder(user_id, event_id, remind_time_str):
            await message.answer(f"✅ Напоминание для события {event_date} установлено на {time_obj.strftime('%H:%M')}.")
            logger.info(f"User {user_id} set reminder for {event_id} at {remind_time_str}")
        else:
            await message.answer("❌ Ошибка при установке напоминания.")
    except ValueError:
        await message.answer("Неправильный формат времени. Пожалуйста, используйте ЧЧ:ММ (например, 09:30 или 18:00).")
    except Exception as e:
        logger.error(f"Error setting reminder for user {user_id}: {e}")
        await message.answer("Произошла непредвиденная ошибка.")
    finally:
        if user_id in remind_data:
            del remind_data[user_id]


@dp.callback_query(F.data == "confirm_clear_all")
async def confirm_clear_all_handler(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    count = await EventManager.clear_user_events(user_id)
    text = f"🗑️ Удалено {count} событий!" if count > 0 else "У вас не было событий для удаления."
    await cb.message.edit_text(text, reply_markup=None)
    logger.success(f"User {user_id} cleared all events ({count} removed)")

@dp.callback_query(F.data == "cancel_clear_all")
async def cancel_clear_all_handler(cb: types.CallbackQuery):
    await cb.message.edit_text("❌ Удаление всех событий отменено.", reply_markup=KeyboardManager.get_back_keyboard())

@dp.callback_query(F.data.startswith(PAGE_PREFIX))
async def handle_pagination(cb: types.CallbackQuery):
    page = int(cb.data[len(PAGE_PREFIX):])
    await MessageManager.display_events_page(cb, cb.from_user.id, page)

@dp.callback_query(F.data == "back_to_events")
async def back_to_events_handler(cb: types.CallbackQuery):
    await MessageManager.display_events_page(cb, cb.from_user.id, 0)

# --- BOT LIFECYCLE ---
async def on_startup(bot: Bot, aiosession: aiohttp.ClientSession):
    Database.init_db()
    logger.info("База данных инициализирована.")
    
    # Start background tasks
    asyncio.create_task(remind_checker())
    asyncio.create_task(daily_channel_post(aiosession))
    logger.info("Фоновые задачи (напоминания, ежедневный пост) запущены.")
    
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Запустить бота"),
        types.BotCommand(command="help", description="Показать помощь"),
        types.BotCommand(command="calendar", description="Открыть календарь (только в лс)"),
        types.BotCommand(command="myevents", description="Мои события (только в лс)"),
        types.BotCommand(command="quote", description="Получить цитату"),
    ])
    logger.info("Бот запущен и готов к работе!")

async def on_shutdown(aiosession: aiohttp.ClientSession):
    logger.warning("Бот останавливается...")
    await aiosession.close()
    logger.info("Сессия aiohttp закрыта.")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        await dp.start_polling(bot, aiosession=dp['aiosession'])
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске polling: {e}")
    finally:
        logger.info("Бот остановлен.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")
