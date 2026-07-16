import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-70b-8192")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты — полезный AI-ассистент. Отвечай кратко и по делу на русском языке.",
)

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не задан в .env")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не задан в .env")
