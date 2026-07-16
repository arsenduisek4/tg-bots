"""
Telegram-бот на Groq API (webhook для Render).

Общение через /ask или просто текстовые сообщения.
Поддерживает историю диалога (в памяти, до перезапуска).
"""

import logging
import os
from collections import defaultdict

from fastapi import FastAPI, Request
from groq import Groq
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import (
    GROQ_API_KEY,
    GROQ_MODEL,
    SYSTEM_PROMPT,
    TELEGRAM_BOT_TOKEN,
)

# ── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Groq-клиент ─────────────────────────────────────────────────────────────
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ── История диалогов (user_id -> список сообщений) ──────────────────────────
MAX_HISTORY = 20
history: dict[int, list[dict]] = defaultdict(list)

# ── Telegram Application (инициализируем один раз) ──────────────────────────
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build() if TELEGRAM_BOT_TOKEN else None


def _build_messages(user_id: int, user_text: str) -> list[dict]:
    """Собрать список сообщений для Groq: system + история + новый запрос."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs.extend(history[user_id])
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _update_history(user_id: int, user_text: str, reply: str) -> None:
    """Добавить пару вопрос-ответ в историю, обрезав до MAX_HISTORY."""
    history[user_id].append({"role": "user", "content": user_text})
    history[user_id].append({"role": "assistant", "content": reply})
    if len(history[user_id]) > MAX_HISTORY * 2:
        history[user_id] = history[user_id][-MAX_HISTORY * 2 :]


async def _ask_groq(user_id: int, text: str) -> str:
    """Отправить запрос в Groq и вернуть ответ."""
    try:
        messages = _build_messages(user_id, text)
        chat_completion = client.chat.completions.create(
            messages=messages,
            model=GROQ_MODEL,
            temperature=0.7,
            max_tokens=2048,
        )
        reply = chat_completion.choices[0].message.content or "…"
        _update_history(user_id, text, reply)
        return reply
    except Exception as e:
        logger.exception("Groq API error")
        return f"❌ Ошибка при обращении к Groq: {e}"


# ── Обработчики ──────────────────────────────────────────────────────────────


async def start(update: Update, _context) -> None:
    """Команда /start."""
    await update.message.reply_text(
        "👋 Привет! Я бот на Groq API.\n"
        "Просто напиши мне что-нибудь или используй /ask <вопрос>.\n"
        "Команда /clear сбрасывает историю диалога."
    )


async def clear(update: Update, _context) -> None:
    """Команда /clear — сброс истории."""
    user_id = update.effective_user.id
    if user_id in history:
        del history[user_id]
    await update.message.reply_text("🧹 История диалога очищена.")


async def ask(update: Update, context) -> None:
    """Команда /ask <текст>."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "ℹ️ Использование: /ask <ваш вопрос>"
        )
        return
    user_id = update.effective_user.id
    await update.message.reply_chat_action("typing")
    reply = await _ask_groq(user_id, text)
    await update.message.reply_text(reply)


async def handle_message(update: Update, _context) -> None:
    """Обычное текстовое сообщение."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return
    await update.message.reply_chat_action("typing")
    reply = await _ask_groq(user_id, text)
    await update.message.reply_text(reply)


# ── Регистрируем хендлеры ───────────────────────────────────────────────────
if application:
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(CommandHandler("ask", ask))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))


# ── FastAPI приложение ───────────────────────────────────────────────────────
app = FastAPI(docs_url=None, redoc_url=None)


@app.post(f"/{TELEGRAM_BOT_TOKEN}")
async def webhook(request: Request) -> dict:
    """Принимаем апдейты от Telegram."""
    if not application:
        return {"ok": False, "error": "not configured"}
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}


@app.get("/")
async def health():
    """Health-check для Render."""
    return {"status": "ok"}


async def lifespan_start():
    """Инициализировать Application и установить webhook при старте."""
    if not application:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — webhook не установлен")
        return
    await application.initialize()
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not webhook_url:
        logger.warning("RENDER_EXTERNAL_URL не задан — webhook не установлен")
        return
    url = f"{webhook_url.rstrip('/')}/{TELEGRAM_BOT_TOKEN}"
    await application.bot.set_webhook(url=url)
    logger.info("Webhook установлен: %s", url)


@app.on_event("startup")
async def on_startup():
    await lifespan_start()
