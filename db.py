"""
Слой работы с PostgreSQL (Render Postgres).

Таблицы создаются автоматически при первом импорте (через migrate()).
"""

import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    language_code TEXT DEFAULT 'ru',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    model TEXT,
    tokens_used INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro', 'enterprise')),
    messages_limit INT DEFAULT 50,
    messages_used INT DEFAULT 0,
    period_start TIMESTAMPTZ DEFAULT NOW(),
    period_end TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_history_user_id ON chat_history(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_history_created_at ON chat_history(created_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
"""


async def migrate(dsn: str) -> None:
    """Создать таблицы, если их нет."""
    conn = await asyncpg.connect(dsn, ssl="require")
    try:
        await conn.execute(SCHEMA_SQL)
        logger.info("Миграция БД выполнена успешно")
    finally:
        await conn.close()


async def init_pool(dsn: str, min_size: int = 1, max_size: int = 5) -> asyncpg.Pool:
    """Инициализировать пул соединений и выполнить миграцию."""
    global _pool
    await migrate(dsn)
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        ssl="require",
    )
    logger.info("Пул соединений с БД создан (min=%d, max=%d)", min_size, max_size)
    return _pool


async def close_pool() -> None:
    """Закрыть пул соединений."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Пул соединений с БД закрыт")


def get_pool() -> asyncpg.Pool:
    """Вернуть текущий пул."""
    if _pool is None:
        raise RuntimeError("Пул БД не инициализирован. Вызови init_pool()")
    return _pool


# ── Users ────────────────────────────────────────────────────────────────────


async def upsert_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    language_code: str | None = None,
) -> None:
    """Создать или обновить пользователя."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (id, username, first_name, last_name, language_code)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (id) DO UPDATE SET
                username = COALESCE($2, users.username),
                first_name = COALESCE($3, users.first_name),
                last_name = COALESCE($4, users.last_name),
                language_code = COALESCE($5, users.language_code),
                is_active = true
            """,
            user_id, username, first_name, last_name, language_code,
        )


async def get_user(user_id: int) -> dict | None:
    """Получить пользователя по ID."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


# ── Chat History ─────────────────────────────────────────────────────────────


async def save_message(
    user_id: int,
    role: str,
    content: str,
    model: str | None = None,
    tokens_used: int = 0,
) -> int:
    """Сохранить сообщение в историю. Вернуть ID записи."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO chat_history (user_id, role, content, model, tokens_used)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            user_id, role, content, model, tokens_used,
        )
        return row["id"]


async def get_history(
    user_id: int,
    limit: int = 20,
) -> list[dict]:
    """Получить последние N сообщений пользователя."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM chat_history
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id, limit,
        )
        # Возвращаем в хронологическом порядке
        result = [dict(r) for r in reversed(rows)]
        return result


async def clear_history(user_id: int) -> None:
    """Очистить историю диалога пользователя."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM chat_history WHERE user_id = $1",
            user_id,
        )


# ── Subscriptions ────────────────────────────────────────────────────────────


async def get_or_create_subscription(user_id: int) -> dict:
    """Получить подписку пользователя или создать бесплатную."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM subscriptions WHERE user_id = $1 AND is_active = true",
            user_id,
        )
        if row:
            return dict(row)
        # Создаём бесплатную подписку
        row = await conn.fetchrow(
            """
            INSERT INTO subscriptions (user_id, plan, messages_limit, messages_used)
            VALUES ($1, 'free', 50, 0)
            RETURNING *
            """,
            user_id,
        )
        return dict(row)


async def increment_messages_used(user_id: int) -> int:
    """Увеличить счётчик использованных сообщений. Вернуть новое значение."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE subscriptions
            SET messages_used = messages_used + 1
            WHERE user_id = $1 AND is_active = true
            RETURNING messages_used, messages_limit
            """,
            user_id,
        )
        if row:
            return row["messages_used"]
        return 0


async def can_send_message(user_id: int) -> tuple[bool, str]:
    """
    Проверить, может ли пользователь отправить сообщение.
    Возвращает (разрешено, причина_отказа).
    """
    sub = await get_or_create_subscription(user_id)
    if sub["plan"] == "free" and sub["messages_used"] >= sub["messages_limit"]:
        return False, "Лимит бесплатных сообщений исчерпан (50/50)."
    return True, ""
