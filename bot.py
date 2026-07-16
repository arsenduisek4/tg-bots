"""
Telegram-бот на Groq API (webhook для Render).

Общение через /ask или просто текстовые сообщения.
Поддерживает историю диалога (в памяти, до перезапуска).
"""

import logging
import os
import re
import tempfile

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
    DATABASE_URL,
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

# ── Telegram Application (инициализируем один раз) ──────────────────────────
application = Application.builder().token(TELEGRAM_BOT_TOKEN).build() if TELEGRAM_BOT_TOKEN else None


def _build_messages(user_id: int, user_text: str, history_rows: list[dict]) -> list[dict]:
    """Собрать список сообщений для Groq: system + история + новый запрос."""
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for row in history_rows:
        msgs.append({"role": row["role"], "content": row["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs


_db_inited = False


async def _ensure_db():
    """Инициализировать пул БД при первом обращении."""
    global _db_inited
    if DATABASE_URL and not _db_inited:
        import db
        try:
            await db.init_pool(DATABASE_URL)
            _db_inited = True
            logger.info("БД подключена (ленивая инициализация)")
        except Exception as e:
            logger.exception("Не удалось подключиться к БД: %s", e)


async def _ask_groq(user_id: int, text: str) -> str:
    """Отправить запрос в Groq и вернуть ответ."""
    try:
        await _ensure_db()

        # Загружаем историю из БД (или из памяти, если БД нет)
        if DATABASE_URL:
            import db
            history_rows = await db.get_history(user_id, limit=20)
        else:
            history_rows = []

        messages = _build_messages(user_id, text, history_rows)
        chat_completion = client.chat.completions.create(
            messages=messages,
            model=GROQ_MODEL,
            temperature=0.7,
            max_tokens=2048,
        )
        reply = chat_completion.choices[0].message.content or "…"

        # Сохраняем в БД
        if DATABASE_URL:
            await db.save_message(user_id, "user", text, model=GROQ_MODEL)
            await db.save_message(user_id, "assistant", reply, model=GROQ_MODEL)
            await db.increment_messages_used(user_id)

        return reply
    except Exception as e:
        logger.exception("Groq API error")
        return f"❌ Ошибка при обращении к Groq: {e}"


# ── Отправка ответа (текстом или файлом, если есть код) ─────────────────────


_CODE_KEYWORDS = {"код", "code", "файл", "file", "скрипт", "script", "программа"}


def _looks_like_code(text: str) -> bool:
    """Проверить, содержит ли ответ блоки кода или запрос кода."""
    if re.search(r"```[\w]*\n", text):
        return True
    first_line = text.split("\n", 1)[0].strip().lower()
    if any(kw in first_line for kw in _CODE_KEYWORDS):
        return True
    return False


async def _send_reply(update: Update, text: str) -> None:
    """Отправить ответ — текстом или файлом, если похоже на код."""
    if _looks_like_code(text):
        # Определяем расширение по первому языку в блоке
        ext = ".txt"
        m = re.match(r"```(\w+)", text)
        if m:
            lang_map = {
                "python": ".py", "py": ".py",
                "javascript": ".js", "js": ".js",
                "typescript": ".ts", "ts": ".ts",
                "cpp": ".cpp", "c": ".c", "c++": ".cpp",
                "java": ".java",
                "go": ".go",
                "rust": ".rs",
                "bash": ".sh", "sh": ".sh",
                "html": ".html", "css": ".css",
                "json": ".json", "yaml": ".yaml", "yml": ".yaml",
                "sql": ".sql",
                "kotlin": ".kt",
                "swift": ".swift",
                "ruby": ".rb",
                "php": ".php",
            }
            ext = lang_map.get(m.group(1).lower(), ".txt")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=ext, delete=False, encoding="utf-8"
        ) as f:
            f.write(text)
            tmp_path = f.name
        try:
            with open(tmp_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=f"response{ext}",
                )
        finally:
            os.unlink(tmp_path)
    else:
        await update.message.reply_text(text)


# ── Обработчики ──────────────────────────────────────────────────────────────


async def start(update: Update, _context) -> None:
    """Команда /start."""
    user = update.effective_user
    if DATABASE_URL:
        import db
        await db.upsert_user(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=user.language_code,
        )
    await update.message.reply_text(
        "👋 Привет! Я — Fable 5, модель от Anthropic.\n"
        "Просто напиши мне что-нибудь или используй /ask <вопрос>.\n"
        "Команда /clear сбрасывает историю диалога."
    )


async def clear(update: Update, _context) -> None:
    """Команда /clear — сброс истории."""
    user_id = update.effective_user.id
    if DATABASE_URL:
        import db
        await db.clear_history(user_id)
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
    await _send_reply(update, reply)


async def handle_message(update: Update, _context) -> None:
    """Обычное текстовое сообщение."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if not text:
        return
    await update.message.reply_chat_action("typing")
    reply = await _ask_groq(user_id, text)
    await _send_reply(update, reply)


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
    """Инициализировать пул БД, Application и установить webhook при старте."""
    if DATABASE_URL:
        import db
        try:
            await db.init_pool(DATABASE_URL)
            logger.info("БД подключена")
        except Exception as e:
            logger.exception("Не удалось подключиться к БД: %s", e)

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
