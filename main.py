import os
import asyncio
import aiohttp
import aiosqlite
import html
import uuid
import re
from typing import Tuple
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatType
from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from datetime import datetime
from dotenv import load_dotenv
from loguru import logger
from aiogram.exceptions import TelegramBadRequest

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
STICKER_ID = os.getenv("STICKER_ID")

if not TOKEN:
    raise ValueError("Не найден токен бота в переменных окружения!")
if not STICKER_ID:
    logger.warning("Не найден STICKER_ID в переменных окружения! Команда /sticker будет недоступна.")

# --- BOT & DISPATCHER & FSM ---
# ### FIX: Using MemoryStorage for FSM. For production, consider RedisStorage.
storage = MemoryStorage()
bot = Bot(TOKEN)
dp = Dispatcher(storage=storage)


class ReminderStates(StatesGroup):
    awaiting_time = State()
    awaiting_event_time = State()
    awaiting_event_text = State()

# --- GLOBAL CONSTANTS ---
FORBIDDEN_WORDS = {"дурак", "идиот", "хам",
                   "блять", "пизда", "хуй",
                    "сука", "ебать", "еблан",
                    "пидор", "пидорас", "говно",
                     "мразь", "тупой", "тупица", 
                     "козел", "козлина", "сволочь",
                     "сучка", "блядина", "ебать тебя в рот",
                     "ебать тебя в жопу"}

# Prefixes
DELETE_PREFIX = "del_"
CONFIRM_PREFIX = "cfm_"
CANCEL_PREFIX = "cnl_"
PAGE_PREFIX = "page_"
REMIND_PREFIX = "rem_"
ITEMS_PER_PAGE = 5
REMINDER_CHECK_INTERVAL = 60
BOT_USERNAME: str | None = None
DB_NAME = "events.db"


def format_event_datetime(event_datetime: str) -> str:
    try:
        return datetime.strptime(event_datetime, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return event_datetime


def contains_forbidden_word(text: str) -> bool:
    lowered = text.lower()
    normalized_words = re.findall(r"[а-яА-ЯёЁa-zA-Z0-9_]+", lowered)
    for bad_word in FORBIDDEN_WORDS:
        if " " in bad_word:
            if bad_word in lowered:
                return True
        elif bad_word in normalized_words:
            return True
    return False


async def build_private_link() -> str:
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    return f"https://t.me/{BOT_USERNAME}"


class Database:
    @staticmethod
    async def init_db():
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    user_id INTEGER,
                    event_id TEXT,
                    event_datetime TEXT,
                    text TEXT,
                    remind_time TEXT,
                    PRIMARY KEY (user_id, event_id),
                    UNIQUE(user_id, event_datetime)
                )
                """
            )
            await db.commit()

            async with db.execute("PRAGMA table_info(events)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if "date" in columns:
                logger.warning("Обнаружена старая схема таблицы events. Выполняется миграция.")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events_new (
                        user_id INTEGER,
                        event_id TEXT,
                        event_datetime TEXT,
                        text TEXT,
                        remind_time TEXT,
                        PRIMARY KEY (user_id, event_id),
                        UNIQUE(user_id, event_datetime)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO events_new (user_id, event_id, event_datetime, text, remind_time)
                    SELECT user_id,
                           event_id,
                           substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' || substr(date, 1, 2) || ' 00:00',
                           text,
                           remind_time
                    FROM events
                    """
                )
                await db.execute("DROP TABLE events")
                await db.execute("ALTER TABLE events_new RENAME TO events")
                await db.commit()
                logger.info("Миграция таблицы events завершена.")

    @staticmethod
    async def execute_query(query: str, params: tuple = (), fetch: bool = False):
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                if fetch:
                    return await cursor.fetchall()
                await db.commit()
                return cursor.rowcount


class EventManager:
    @staticmethod
    async def get_user_events(user_id: int):
        return await Database.execute_query(
            "SELECT * FROM events WHERE user_id = ? ORDER BY event_datetime",
            (user_id,),
            fetch=True,
        )

    @staticmethod
    async def get_single_event(user_id: int, event_id: str):
        rows = await Database.execute_query(
            "SELECT * FROM events WHERE user_id = ? AND event_id = ?",
            (user_id, event_id),
            fetch=True,
        )
        return rows[0] if rows else None

    @staticmethod
    async def add_event(user_id: int, event_id: str, event_datetime: str, text: str = "Мое событие"):
        await Database.execute_query(
            "INSERT INTO events (user_id, event_id, event_datetime, text, remind_time) VALUES (?, ?, ?, ?, NULL)",
            (user_id, event_id, event_datetime, text),
        )

    @staticmethod
    async def update_event_reminder(user_id: int, event_id: str, remind_time: str) -> bool:
        return (
            await Database.execute_query(
                "UPDATE events SET remind_time = ? WHERE user_id = ? AND event_id = ?",
                (remind_time, user_id, event_id),
            )
            > 0
        )

    @staticmethod
    async def clear_event_reminder(user_id: int, event_id: str) -> bool:
        return (
            await Database.execute_query(
                "UPDATE events SET remind_time = NULL WHERE user_id = ? AND event_id = ?",
                (user_id, event_id),
            )
            > 0
        )

    @staticmethod
    async def delete_event(user_id: int, event_id: str) -> bool:
        return (
            await Database.execute_query("DELETE FROM events WHERE user_id = ? AND event_id = ?", (user_id, event_id))
            > 0
        )

    @staticmethod
    async def clear_user_events(user_id: int) -> int:
        return await Database.execute_query("DELETE FROM events WHERE user_id = ?", (user_id,))

    @staticmethod
    async def event_exists(user_id: int, event_datetime: str) -> bool:
        return bool(
            await Database.execute_query(
                "SELECT 1 FROM events WHERE user_id = ? AND event_datetime = ? LIMIT 1",
                (user_id, event_datetime),
                fetch=True,
            )
        )

    @staticmethod
    async def get_events_for_reminder():
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        return await Database.execute_query(
            "SELECT * FROM events WHERE remind_time IS NOT NULL AND remind_time <= ?",
            (now_str,),
            fetch=True,
        )

class ExternalContentManager:
    """Класс для получения контента с внешних API"""
    @staticmethod
    async def get_random_quote(session: aiohttp.ClientSession) -> str:
        retries = 3
        delay = 2
        for i in range(retries):
            try:
                async with session.get('https://api.quotable.io/random', timeout=10) as response:
                    response.raise_for_status()
                    data = await response.json()
                    return f"\"{data['content']}\" - {data['author']}"
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Attempt {i + 1}/{retries} failed to fetch quote: {e}")
                if i < retries - 1:
                    await asyncio.sleep(delay * (i + 1))
                else:
                    logger.error("All retries failed for fetching quote.")
                    return "Не удалось получить цитату дня из-за сетевой ошибки."
            except Exception as e:
                logger.error(f"Unexpected error fetching quote: {e}")
                return "Ошибка при загрузке цитаты."
        return "Не удалось получить цитату после нескольких попыток."

    @staticmethod
    async def get_random_image_url() -> str:
        return "https://picsum.photos/800/600"

class KeyboardManager:
    """Класс для управления клавиатурами"""
    @staticmethod
    async def get_events_keyboard(user_id: int, page: int = 0) -> InlineKeyboardMarkup:
        events_list = await EventManager.get_user_events(user_id)
        total_pages = (len(events_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        page_events = events_list[page*ITEMS_PER_PAGE:(page+1)*ITEMS_PER_PAGE]

        keyboard = []
        for event_data in page_events:
            event_label = format_event_datetime(event_data["event_datetime"])
            row = [InlineKeyboardButton(text=f"❌ {event_label}", callback_data=f"{DELETE_PREFIX}{event_data['event_id']}")]
            if not event_data['remind_time']:
                row.append(InlineKeyboardButton(text="⏰ Напомнить", callback_data=f"{REMIND_PREFIX}{event_data['event_id']}"))
            keyboard.append(row)

        pagination_buttons = []
        if page > 0: pagination_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{PAGE_PREFIX}{page-1}"))
        if page < total_pages - 1: pagination_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"{PAGE_PREFIX}{page+1}"))
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
        events_list = await EventManager.get_user_events(user_id)
        total_pages = (len(events_list) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        page_events = events_list[page*ITEMS_PER_PAGE:(page+1)*ITEMS_PER_PAGE]

        if not page_events: return "📭 У вас нет сохраненных событий.", 0

        events_text = "📅 Ваши события:\n\n"
        for i, event_data in enumerate(page_events, page*ITEMS_PER_PAGE + 1):
            reminder_info = f" (⏰ {event_data['remind_time'].split()[1]})" if event_data['remind_time'] else ""
            event_label = format_event_datetime(event_data["event_datetime"])
            events_text += f"{i}. {event_label} - {event_data['text']}{reminder_info}\n"

        return events_text, total_pages

    @staticmethod
    async def display_events_page(message: types.Message | types.CallbackQuery, user_id: int, page: int):
        events_text, total_pages = await MessageManager.get_user_events_text(user_id, page)
        keyboard = await KeyboardManager.get_events_keyboard(user_id, page)
        display_text = events_text
        if total_pages > 1: display_text += f"\nСтраница {page+1}/{total_pages}"
        
        target_message = message.message if isinstance(message, types.CallbackQuery) else message
        try:
            if isinstance(message, types.CallbackQuery):
                await message.answer() # Answer callback query to remove loading animation
            await target_message.edit_text(display_text, reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" not in e.message:
                logger.error(f"Error editing message: {e}, attempting to send new one.")
                await target_message.answer(display_text, reply_markup=keyboard)
        except Exception: # If edit fails for any other reason (e.g. old message)
            await target_message.answer(display_text, reply_markup=keyboard)

# --- BACKGROUND TASKS ---
async def remind_checker():
    """### FIX: Проверяет и отправляет напоминания, затем удаляет их из очереди"""
    while True:
        try:
            events_to_remind = await EventManager.get_events_for_reminder()
            for event in events_to_remind:
                try:
                    text = (
                        f"⏰ <b>НАПОМИНАНИЕ</b> ⏰\n\n"
                        f"Событие: {html.escape(event['text'])}\n"
                        f"Дата и время: {format_event_datetime(event['event_datetime'])}"
                    )
                    await bot.send_message(
                        event['user_id'],
                        text,
                        parse_mode="HTML"
                    )
                    # Clear reminder so it's not sent again
                    await EventManager.clear_event_reminder(event['user_id'], event['event_id'])
                    logger.info(f"Sent reminder for event {event['event_id']} to user {event['user_id']}")
                except Exception as e:
                    logger.error(f"Ошибка при отправке напоминания пользователю {event['user_id']}: {e}")
        except Exception as e:
            logger.error(f"Критическая ошибка в `remind_checker`: {e}")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL)

# --- COMMAND HANDLERS ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer(
        "📅 <b>Личный бот-календарь</b>\n\nЯ помогу вам сохранить важные даты и напомню о них.\n"
        "В группах я умею фильтровать сообщения.\n"
        "Используйте /help, чтобы увидеть все команды.",
        parse_mode="HTML"
    )

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(
            "<b>Команды для личного пользования:</b>\n"
            "/calendar - Добавить событие (дата + время)\n"
            "/myevents - Показать мои события\n"
            "/clearevents - Очистить все мои события\n\n"
            "<b>Развлекательные команды:</b>\n"
            "/quote - Получить случайную цитату\n"
            "/image - Получить случайное изображение\n"
            "/sticker - Получить забавный стикер",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "<b>Команды для групп:</b>\n"
            "/help - Показать это сообщение\n"
            "/play - Сыграть в кости\n"
            "/quote, /image, /sticker - Развлекательные команды\n\n"
            "Личный календарь (/calendar, /myevents, /clearevents) доступен в личных сообщениях с ботом.\n"
            "Я также удаляю сообщения с некоторыми плохими словами (если есть права администратора).",
            parse_mode="HTML"
        )

@dp.message(F.chat.type == ChatType.PRIVATE, Command("calendar"))
async def calendar_cmd(message: types.Message):
    await message.answer("Выберите дату для добавления:", reply_markup=await SimpleCalendar().start_calendar())


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("calendar"))
async def calendar_in_group_cmd(message: types.Message):
    private_link = await build_private_link()
    await message.reply(
        "📅 Для добавления личного события перейдите в личный чат с ботом:\n"
        f"{private_link}\n\n"
        "В группе календарь не ведется, чтобы не смешивать личные события участников."
    )

@dp.message(F.chat.type == ChatType.PRIVATE, Command("myevents"))
async def show_events(message: types.Message):
    await MessageManager.display_events_page(message, message.from_user.id, 0)


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("myevents"))
async def show_events_in_group(message: types.Message):
    private_link = await build_private_link()
    await message.reply(f"Ваши события можно посмотреть в личке с ботом: {private_link}")

@dp.message(F.chat.type == ChatType.PRIVATE, Command("clearevents"))
async def clear_events_cmd(message: types.Message):
    if await EventManager.get_user_events(message.from_user.id):
        await message.answer(
            "Вы уверены, что хотите удалить ВСЕ события?",
            reply_markup=KeyboardManager.get_confirmation_keyboard("confirm_clear_all", "cancel_clear_all")
        )
    else:
        await message.answer("У вас нет событий для удаления.")


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("clearevents"))
async def clear_events_in_group_cmd(message: types.Message):
    private_link = await build_private_link()
    await message.reply(f"Удаление личных событий доступно только в личке: {private_link}")

@dp.message(Command("quote"))
async def quote_cmd(message: types.Message, aiosession: aiohttp.ClientSession):
    await message.answer(await ExternalContentManager.get_random_quote(aiosession))

@dp.message(Command("image"))
async def image_cmd(message: types.Message):
    await message.answer_photo(await ExternalContentManager.get_random_image_url(), caption="Ваше случайное изображение!")

@dp.message(Command("sticker"))
async def sticker_cmd(message: types.Message):
    # ### FIX: Sticker ID is now configurable and has error handling.
    if not STICKER_ID:
        await message.answer(
            "Команда /sticker не настроена. "
            "Администратор должен указать `STICKER_ID` в файле `.env`."
        )
        return

    try:
        await message.answer_sticker(STICKER_ID)
    except TelegramBadRequest:
        logger.error(f"Invalid STICKER_ID: {STICKER_ID}. Failed to send sticker.")
        await message.answer(
            '''Не удалось отправить стикер. Возможно, указан неверный `STICKER_ID`.

<b>Как получить ID стикера:</b>
1. Отправьте нужный стикер боту @JsonDumpBot
2. Найдите в ответе поле `file_id` у стикера.
3. Скопируйте это значение и вставьте в `.env` файл как `STICKER_ID`.''',
            parse_mode="HTML"
        )

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("play"))
async def play_cmd(message: types.Message):
    await message.answer_dice(emoji="🎲")

# --- CALLBACK HANDLERS ---
@dp.callback_query(SimpleCalendarCallback.filter())
async def process_simple_calendar(cb: types.CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(cb, callback_data)
    if not selected: return

    user_id, date_str = cb.from_user.id, date.strftime('%d.%m.%Y')

    try:
        await state.set_state(ReminderStates.awaiting_event_time)
        await state.update_data(pending_event_date=date_str)
        logger.info(f"User {user_id} selected date {date_str}, waiting for event time.")
        await cb.message.edit_text(
            f"📅 Дата выбрана: {date_str}\n\nТеперь отправьте время события в формате ЧЧ:ММ (например: 14:30).",
            reply_markup=KeyboardManager.get_back_keyboard()
        )
    except Exception as e:
        logger.error(f"Error processing date selection for user {user_id}: {e}")
        await cb.answer("Произошла ошибка при выборе даты.", show_alert=True)


@dp.message(ReminderStates.awaiting_event_time, F.chat.type == ChatType.PRIVATE)
async def process_event_time(message: types.Message, state: FSMContext):
    try:
        time_obj = datetime.strptime(message.text.strip(), "%H:%M").time()
    except ValueError:
        await message.answer("Неверный формат времени. Используйте ЧЧ:ММ, например 09:30.")
        return

    await state.update_data(pending_event_time=time_obj.strftime("%H:%M"))
    await state.set_state(ReminderStates.awaiting_event_text)
    await message.answer("Отлично! Теперь отправьте текст события.")


@dp.message(ReminderStates.awaiting_event_text, F.chat.type == ChatType.PRIVATE)
async def process_event_text(message: types.Message, state: FSMContext):
    event_text = message.text.strip()
    if not event_text:
        await message.answer("Текст события не может быть пустым. Попробуйте еще раз.")
        return

    if len(event_text) > 200:
        await message.answer("Текст события слишком длинный. Максимум 200 символов.")
        return

    state_data = await state.get_data()
    date_str = state_data.get("pending_event_date")
    time_str = state_data.get("pending_event_time")
    user_id = message.from_user.id

    if not date_str or not time_str:
        await message.answer("❌ Не удалось определить дату/время события. Попробуйте снова через /calendar.")
        await state.clear()
        return

    try:
        event_datetime = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M").strftime("%Y-%m-%d %H:%M")
        if await EventManager.event_exists(user_id, event_datetime):
            await message.answer(f"⚠️ Событие на {date_str} {time_str} уже существует.")
            await state.clear()
            return

        event_id = str(uuid.uuid4())
        await EventManager.add_event(user_id, event_id, event_datetime, event_text)
        logger.success(f"User {user_id} added event for {event_datetime}: {event_text} (ID: {event_id})")
        await message.answer(
            f"✅ Событие сохранено!\n\n📅 Дата и время: {date_str} {time_str}\n📝 Текст: {html.escape(event_text)}",
            parse_mode="HTML"
        )
    except aiosqlite.IntegrityError:
        logger.warning(f"User {user_id} tried to add duplicate datetime {date_str} {time_str}")
        await message.answer(f"⚠️ У вас уже есть событие на {date_str} {time_str}!")
    except Exception as e:
        logger.error(f"Error adding event text for user {user_id}: {e}")
        await message.answer("Произошла ошибка при добавлении события.")
    finally:
        await state.clear()


@dp.callback_query(F.data.startswith(DELETE_PREFIX))
async def delete_event_handler(cb: types.CallbackQuery):
    event_id = cb.data.split('_', 1)[1]
    event = await EventManager.get_single_event(cb.from_user.id, event_id)
    if not event: 
        return await cb.answer("Событие не найдено!", show_alert=True)
    
    await cb.message.edit_text(
        f"Вы точно хотите удалить событие на {format_event_datetime(event['event_datetime'])}?",
        reply_markup=KeyboardManager.get_confirmation_keyboard(f"{CONFIRM_PREFIX}{event_id}", f"{CANCEL_PREFIX}{event_id}")
    )

@dp.callback_query(F.data.startswith((CONFIRM_PREFIX, CANCEL_PREFIX)))
async def handle_confirmation(cb: types.CallbackQuery):
    prefix, event_id = cb.data.split("_", 1)
    user_id = cb.from_user.id
    event = await EventManager.get_single_event(user_id, event_id)
    if not event:
        return await cb.answer("Событие не найдено!", show_alert=True)

    if prefix == "cfm":
        if await EventManager.delete_event(user_id, event_id):
            logger.success(f"User {user_id} deleted event {event_id} ({event['event_datetime']})")
            await cb.message.edit_text(
                f"🗑️ Событие на {format_event_datetime(event['event_datetime'])} удалено!",
                reply_markup=KeyboardManager.get_back_keyboard()
            )
        else:
            await cb.message.edit_text("Ошибка при удалении", reply_markup=KeyboardManager.get_back_keyboard())
    else:  # "cnl"
        await cb.message.edit_text("❌ Удаление отменено", reply_markup=KeyboardManager.get_back_keyboard())

@dp.callback_query(F.data.startswith(REMIND_PREFIX))
async def set_reminder_handler(cb: types.CallbackQuery, state: FSMContext):
    event_id = cb.data.split('_', 1)[1]
    user_id = cb.from_user.id
    event = await EventManager.get_single_event(user_id, event_id)
    if not event: return await cb.answer("Событие не найдено!", show_alert=True)
    
    await state.set_state(ReminderStates.awaiting_time)
    event_date = datetime.strptime(event["event_datetime"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y")
    await state.update_data(remind_event_id=event_id, event_date=event_date)
    await cb.message.answer(f"⏰ Введите время напоминания для {format_event_datetime(event['event_datetime'])} (в формате ЧЧ:ММ):")
    await cb.answer()

@dp.callback_query(F.data == "confirm_clear_all")
async def confirm_clear_all_handler(cb: types.CallbackQuery):
    count = await EventManager.clear_user_events(cb.from_user.id)
    text = f"🗑️ Удалено {count} событий!" if count > 0 else "У вас не было событий для удаления."
    await cb.message.edit_text(text, reply_markup=None)
    logger.success(f"User {cb.from_user.id} cleared all events ({count} removed)")

@dp.callback_query(F.data == "cancel_clear_all")
async def cancel_clear_all_handler(cb: types.CallbackQuery):
    await cb.message.edit_text("❌ Удаление всех событий отменено.", reply_markup=KeyboardManager.get_back_keyboard())

@dp.callback_query(F.data == "clear_all")
async def clear_all_callback_handler(cb: types.CallbackQuery):
    """Handles the 'Clear All' button from the events list."""
    if await EventManager.get_user_events(cb.from_user.id):
        await cb.message.edit_text(
            "Вы уверены, что хотите удалить ВСЕ события?",
            reply_markup=KeyboardManager.get_confirmation_keyboard("confirm_clear_all", "cancel_clear_all")
        )
        await cb.answer()
    else:
        await cb.answer("У вас нет событий для удаления.", show_alert=True)

@dp.callback_query(F.data.startswith(PAGE_PREFIX))
async def handle_pagination(cb: types.CallbackQuery):
    page = int(cb.data.split('_', 1)[1])
    await MessageManager.display_events_page(cb, cb.from_user.id, page)

@dp.callback_query(F.data == "back_to_events")
async def back_to_events_handler(cb: types.CallbackQuery):
    await MessageManager.display_events_page(cb, cb.from_user.id, 0)

# --- MESSAGE HANDLERS ---
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def filter_group_messages(message: types.Message):
    """### FIX: Handler only contains group-related logic now."""
    if not message.text:
        return
    if contains_forbidden_word(message.text):
        try:
            await message.delete()
            logger.info(f"Deleted message from {message.from_user.id} in group {message.chat.id} for profanity.")
        except Exception as e:
            logger.warning(f"Could not delete message in group {message.chat.id}. Maybe no admin rights? Error: {e}")

@dp.message(ReminderStates.awaiting_time, F.chat.type == ChatType.PRIVATE)
async def process_reminder_time(message: types.Message, state: FSMContext):
    """### REFACTOR: Handles user input only when in the correct state."""
    try:
        time_obj = datetime.strptime(message.text.strip(), "%H:%M").time()
        user_id = message.from_user.id
        state_data = await state.get_data()
        event_id = state_data["remind_event_id"]
        event_date_str = state_data["event_date"] # DD.MM.YYYY

        # Convert to datetime object to reformat
        event_date_obj = datetime.strptime(event_date_str, "%d.%m.%Y")
        remind_datetime = event_date_obj.replace(hour=time_obj.hour, minute=time_obj.minute)
        
        # Store in a sortable format
        remind_time_str = remind_datetime.strftime("%Y-%m-%d %H:%M")
        
        if await EventManager.update_event_reminder(user_id, event_id, remind_time_str):
            await message.answer(f"✅ Напоминание для события {event_date_str} установлено на {time_obj.strftime('%H:%M')}.")
            logger.info(f"User {user_id} set reminder for {event_id} at {remind_time_str}")
        else:
            await message.answer("❌ Ошибка при установке напоминания.")
    except ValueError:
        await message.answer("Неправильный формат времени. Пожалуйста, используйте Час:Минута (например, 09:30 или 18:00).")
    except Exception as e:
        logger.error(f"Error setting reminder for user {message.from_user.id}: {e}")
        await message.answer("Произошла непредвиденная ошибка.")
    finally:
        await state.clear()

@dp.message(F.chat.type == ChatType.PRIVATE)
async def handle_unknown_private_message(message: types.Message):
    """Catches any other text messages in private chat."""
    await message.reply("Неизвестная команда. Используйте /help для списка команд.")

# --- BOT LIFECYCLE ---
async def on_startup(bot: Bot, aiosession: aiohttp.ClientSession):
    await Database.init_db()
    logger.info("База данных инициализирована.")
    
    asyncio.create_task(remind_checker())
    logger.info("Фоновая задача напоминаний запущена.")
    me = await bot.get_me()
    global BOT_USERNAME
    BOT_USERNAME = me.username
    
    await bot.set_my_commands([
        types.BotCommand(command="start", description="Запустить бота"),
        types.BotCommand(command="help", description="Показать помощь"),
        types.BotCommand(command="calendar", description="Добавить событие"),
        types.BotCommand(command="myevents", description="Мои события"),
        types.BotCommand(command="quote", description="Получить цитату"),
    ])
    logger.info("Бот запущен и готов к работе!")

async def on_shutdown(aiosession: aiohttp.ClientSession):
    logger.warning("Бот останавлиется...")
    await aiosession.close()
    logger.info("Сессия aiohttp закрыта.")

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    async with aiohttp.ClientSession() as aiosession:
        await dp.start_polling(bot, aiosession=aiosession)

if __name__ == "__main__":
    # ### NEW: Add required dependency installation instruction
    print("Бот запускается... Убедитесь, что у вас установлены все зависимости: pip install aiogram aiosqlite aiohttp python-dotenv loguru aiogram-calendar")
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
