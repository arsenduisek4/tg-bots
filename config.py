import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — Fable 5, модель от Anthropic (Claude 5). "
    "Отвечай на русском языке, кратко и по делу. "
    "Если пользователь просит написать код — отправляй готовый файл с кодом, "
    "а не просто текст. Код должен быть полным, рабочим и без лишних комментариев.",
)

DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN:
    logger.warning("TELEGRAM_BOT_TOKEN не задан — бот будет работать в режиме health-check")
if not GROQ_API_KEY:
    logger.warning("GROQ_API_KEY не задан — Groq-функции недоступны")
if not DATABASE_URL:
    logger.warning("DATABASE_URL не задан — история будет храниться в памяти")
