import os
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import google.generativeai as genai
from aiohttp import web

# --- КОНФИГУРАЦИЯ И СЕКРЕТЫ ---
# Пытаемся загрузить .env для локального тестирования
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Проверка наличия ключей
if not BOT_TOKEN or not GEMINI_API_KEY:
    print("❌ ОШИБКА: Не найдены BOT_TOKEN или GEMINI_API_KEY в переменных окружения.")
    sys.exit(1)

# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash-lite')

# --- ХРАНИЛИЩЕ КОНТЕКСТА ---
# Словарь для хранения сессий чата: {user_id: ChatSession object}
# ПРИМЕЧАНИЕ: Это хранилище в ОПЕРАТИВНОЙ ПАМЯТИ. Контекст будет сброшен при перезапуске бота!
user_chats = {} 

# Настройка Aiogram
# ParseMode.MARKDOWN делает текст красивым
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
dp = Dispatcher()

# Настройка логирования
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def split_text(text, max_length=4000):
    """Разбивает длинный текст на части для Telegram (лимит ~4096)"""
    return [text[i:i+max_length] for i in range(0, len(text), max_length)]

async def safe_send_message(message: Message, text: str):
    """
    Пытается отправить сообщение с Markdown. 
    Если формат не проходит проверку Telegram, отправляет как обычный текст.
    """
    parts = split_text(text)
    
    for part in parts:
        try:
            await message.answer(part, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Отправка без форматирования в случае ошибки
            await message.answer(part, parse_mode=None)

# --- ХЕНДЛЕРЫ БОТА ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    # При сбросе контекста /start может быть использован для создания новой сессии
    chat_id = message.chat.id
    if chat_id in user_chats:
         del user_chats[chat_id]
         
    await message.answer(
        "👋 **Привет! Я бот с искусственным интеллектом Gemini.**\n\n"
        "Я помню контекст вашего разговора. Начните беседу!\n"
        "_(Помните: память сбрасывается при перезапуске сервера.)_"
    )

@dp.message(F.text)
async def chat_with_gemini(message: Message):
    chat_id = message.chat.id
    
    # 1. Получаем или создаем сессию чата
    if chat_id not in user_chats:
        # Если это первый запрос от пользователя, начинаем новую сессию
        user_chats[chat_id] = model.start_chat()
        logging.info(f"Новая сессия чата создана для {chat_id}")
        
    chat = user_chats[chat_id]
    
    # Показываем статус "печатает..."
    await bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # 2. Отправляем сообщение в чат-сессию (это сохраняет контекст)
        response = await chat.send_message_async(message.text)
        
        if response.text:
            await safe_send_message(message, response.text)
        else:
            await message.answer("Gemini прислал пустой ответ (возможно, контент был заблокирован).")
            
    except Exception as e:
        error_msg = str(e)
        
        # Обработка ошибки переполнения контекста (слишком много токенов)
        if "Request payload size" in error_msg or "400" in error_msg:
             # Удаляем старую сессию и создаем новую
             del user_chats[chat_id]
             await message.answer(
                 "🤯 **Память переполнена.**\n"
                 "История чата стала слишком длинной. Контекст сброшен. Пожалуйста, начните новый диалог."
             )
        else:
             logging.error(f"Ошибка при работе с Gemini: {error_msg}")
             await message.answer(f"⚠️ Произошла ошибка: {error_msg}")

# --- ВЕБ-СЕРВЕР (HEALTH CHECK) ---
# Нужен, чтобы бесплатный хостинг не "усыплял" приложение

async def handle_ping(request):
    """Отвечает "OK" на любой входящий запрос"""
    return web.Response(text="Bot is running! I am alive.")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render автоматически задает переменную PORT
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    logging.info(f"🚀 Запускается веб-сервер на порту {port}")
    await site.start()
    
    # Бесконечный цикл, чтобы сервер продолжал работать
    while True:
        await asyncio.sleep(3600)

# --- ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА ---

async def main():
    logging.info("🤖 Бот запускается...")
    # Удаляем вебхуки, если они были, и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем поллинг бота и веб-сервер параллельно
    await asyncio.gather(
        dp.start_polling(bot),
        start_web_server()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную")
