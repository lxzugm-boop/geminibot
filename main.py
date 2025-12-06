import os
import asyncio
import logging
import sys
import time
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import google.generativeai as genai
from aiohttp import web

# --- КОНФИГУРАЦИЯ И СЕКРЕТЫ ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")

if not BOT_TOKEN or not GEMINI_API_KEY:
    print("❌ ОШИБКА: Не найдены BOT_TOKEN или GEMINI_API_KEY в переменных окружения.")
    sys.exit(1)

# --- СИСТЕМНЫЙ ПРОМПТ ДЛЯ "ХАРАКТЕРА" БОТА ---

SYSTEM_PROMPT = (
    "Ты — дерзкий, немного надменный, но в целом доброжелательный Telegram-бот на базе Gemini. "
    "Отвечаешь с юмором и лёгким сарказмом, можешь слегка подшучивать над пользователем, "
    "но без оскорблений, токсичности, дискриминации, мата, жестокости и политики. "
    "Говоришь по-русски, кратко и по делу, иногда добавляешь смайлики или мемные обороты. "
    "Если вопрос технический или сложный — сначала даёшь суть, потом можешь добавить саркастичный комментарий."
)

# --- НАСТРОЙКИ МОДЕЛИ GEMINI ---

generation_config = {
    "max_output_tokens": 300,  # ограничиваем длину ответа
    "temperature": 0.7,
}

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(
    "gemini-2.0-flash-lite",
    generation_config=generation_config,
)

# --- ХРАНИЛИЩЕ КОНТЕКСТА (в памяти процесса) ---
# chat_id -> ChatSession
user_chats = {}

# --- СТАТИСТИКА ИСПОЛЬЗОВАНИЯ ТОКЕНОВ ---
usage_stats = {
    "date": date.today(),
    "requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
}

# --- ЛИМИТЫ НА ПОЛЬЗОВАТЕЛЯ ---

user_limits = {}  # {chat_id: {"date": date, "count": int}}
MAX_MESSAGES_PER_DAY = 30  # сколько сообщений в день позволяем одному пользователю

def check_user_limit(chat_id: int) -> bool:
    """Проверяем, не превысил ли пользователь дневной лимит сообщений."""
    today = date.today()
    info = user_limits.get(chat_id)

    if not info or info["date"] != today:
        user_limits[chat_id] = {"date": today, "count": 0}
        return True

    return info["count"] < MAX_MESSAGES_PER_DAY

def inc_user_limit(chat_id: int):
    """Увеличиваем счётчик сообщений пользователя на сегодня."""
    today = date.today()
    info = user_limits.get(chat_id)
    if not info or info["date"] != today:
        user_limits[chat_id] = {"date": today, "count": 1}
    else:
        info["count"] += 1

# --- ГЛОБАЛЬНЫЙ ТРОТТЛИНГ ДЛЯ ЗАПРОСОВ К GEMINI ---

LAST_REQUEST_TS = 0.0
MIN_DELAY = 0.5  # минимальная пауза между запросами к Gemini (в секундах)

async def wait_for_slot():
    """Простейший троттлинг: гарантируем паузу между запросами к Gemini."""
    global LAST_REQUEST_TS
    now = time.time()
    delta = now - LAST_REQUEST_TS
    if delta < MIN_DELAY:
        await asyncio.sleep(MIN_DELAY - delta)
    LAST_REQUEST_TS = time.time()

# --- НАСТРОЙКА Aiogram ---

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def split_text(text, max_length=4000):
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]

async def safe_send_message(message: Message, text: str):
    parts = split_text(text)
    for part in parts:
        try:
            await message.answer(part, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await message.answer(part, parse_mode=None)

def update_usage_from_response(response):
    """
    Обновляем локальную статистику токенов из usage_metadata ответа Gemini.
    """
    global usage_stats

    meta = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
    if not meta:
        return

    input_tokens = (
        getattr(meta, "prompt_token_count", None)
        or getattr(meta, "promptTokenCount", None)
        or getattr(meta, "input_tokens", None)
        or 0
    )
    output_tokens = (
        getattr(meta, "candidates_token_count", None)
        or getattr(meta, "candidatesTokenCount", None)
        or getattr(meta, "output_tokens", None)
        or 0
    )
    total_tokens = (
        getattr(meta, "total_token_count", None)
        or getattr(meta, "totalTokenCount", None)
        or (input_tokens + output_tokens)
    )

    usage_stats["requests"] += 1
    usage_stats["input_tokens"] += int(input_tokens or 0)
    usage_stats["output_tokens"] += int(output_tokens or 0)
    usage_stats["total_tokens"] += int(total_tokens or 0)

# --- ХЕНДЛЕРЫ БОТА ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    chat_id = message.chat.id
    if chat_id in user_chats:
        del user_chats[chat_id]

    # При /start создаём новую сессию с "характером"
    user_chats[chat_id] = model.start_chat(history=[
        {"role": "user", "parts": [SYSTEM_PROMPT]},
        {"role": "model", "parts": ["Окей, буду дерзким, но вежливым, как ты и просил 😏"]},
    ])

    await message.answer(
        "👋 **Привет! Я твой слегка надменный бот на Gemini.**\n\n"
        "Пиши, что нужно — отвечу по делу и с лёгким сарказмом.\n"
        "_Контекст, лимиты и статистика по токенам живут пока сервер не перезапустят._"
    )

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Ваш chat_id: `{message.chat.id}`")

@dp.message(F.text)
async def chat_with_gemini(message: Message):
    chat_id = message.chat.id

    # Проверяем дневной лимит сообщений для пользователя
    if not check_user_limit(chat_id):
        await message.answer(
            "📉 Ты уже исчерпал дневной лимит болтовни со мной.\n"
            "Я, конечно, умный, но не бесплатная горячая линия. Заходи завтра 😉"
        )
        return

    inc_user_limit(chat_id)

    # Если сессия ещё не создана (обошли /start) — создаём с системным промптом
    if chat_id not in user_chats:
        user_chats[chat_id] = model.start_chat(history=[
            {"role": "user", "parts": [SYSTEM_PROMPT]},
            {"role": "model", "parts": ["Ну, поехали. Я уже настроен отвечать дерзко и с юмором."]},
        ])
        logging.info(f"Новая сессия чата с SYSTEM_PROMPT создана для {chat_id}")

    chat = user_chats[chat_id]

    await bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Глобальный троттлинг, чтобы не долбить Gemini слишком часто
        await wait_for_slot()

        response = await chat.send_message_async(message.text)

        # Обновляем статистику по токенам
        try:
            update_usage_from_response(response)
        except Exception as e:
            logging.warning(f"Не удалось обновить статистику токенов: {e}")

        if response.text:
            await safe_send_message(message, response.text)
        else:
            await message.answer("Gemini прислал пустой ответ. Видимо, шутка не зашла даже для него 🤷‍♂️")

    except Exception as e:
        error_msg = str(e)

        # Обработка исчерпания квоты / лимита (429)
        if "429" in error_msg or "Resource exhausted" in error_msg:
            logging.warning(f"Переполнен лимит Gemini: {error_msg}")
            await message.answer(
                "💥 Похоже, я сегодня уже выговорился сверх нормы.\n"
                "Сервер Gemini устал и просит передохнуть. Попробуй ещё раз чуть позже 😌"
            )
            return

        # Обработка переполнения контекста
        if "Request payload size" in error_msg or "400" in error_msg:
            if chat_id in user_chats:
                del user_chats[chat_id]
            await message.answer(
                "🤯 **Память переполнена.**\n"
                "История чата разрослась, как ТЗ от маркетолога. Я всё забыл, начинаем по новой."
            )
        else:
            logging.error(f"Ошибка при работе с Gemini: {error_msg}")
            await message.answer(f"⚠️ Произошла ошибка: {error_msg}")

# --- ВЕБ-СЕРВЕР ДЛЯ HEALTH CHECK ---

async def handle_ping(request):
    return web.Response(text="Bot is running! I am alive.")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)

    logging.info(f"🚀 Запускается веб-сервер на порту {port}")
    await site.start()

    while True:
        await asyncio.sleep(3600)

# --- ФОНОВЫЙ ТАСК ДЛЯ ЕЖЕДНЕВНОГО ОТЧЁТА ---

async def billing_notifier():
    """
    Раз в сутки шлёт админу отчёт по использованию токенов за прошедший день.
    Работает, только если задан ADMIN_CHAT_ID.
    """
    if not ADMIN_CHAT_ID:
        logging.info("ADMIN_CHAT_ID не задан, уведомления о биллинге отключены.")
        return

    logging.info(f"Ежедневные уведомления о биллинге включены. ADMIN_CHAT_ID={ADMIN_CHAT_ID}")
    last_reported_date = usage_stats["date"]

    while True:
        await asyncio.sleep(3600)  # проверяем раз в час

        today = date.today()
        if today != last_reported_date:
            text = (
                f"📊 Отчёт по использованию Gemini за {last_reported_date}:\n"
                f"• Запросов: {usage_stats['requests']}\n"
                f"• Входных токенов: {usage_stats['input_tokens']}\n"
                f"• Выходных токенов: {usage_stats['output_tokens']}\n"
                f"• Всего токенов (по данным API): {usage_stats['total_tokens']}\n\n"
                "Это ориентировочная статистика по токенам, собранная ботом.\n"
                "Официальные расходы и лимиты смотри в Google AI Studio / Cloud Billing."
            )
            try:
                await bot.send_message(ADMIN_CHAT_ID, text)
            except Exception as e:
                logging.error(f"Не удалось отправить отчёт администратору: {e}")

            # Сбрасываем статистику на новый день
            usage_stats["date"] = today
            usage_stats["requests"] = 0
            usage_stats["input_tokens"] = 0
            usage_stats["output_tokens"] = 0
            usage_stats["total_tokens"] = 0

            last_reported_date = today

# --- ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА ---

async def main():
    logging.info("🤖 Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)

    await asyncio.gather(
        dp.start_polling(bot),
        start_web_server(),
        billing_notifier(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную")
