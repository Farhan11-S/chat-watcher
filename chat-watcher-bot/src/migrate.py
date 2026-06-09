"""
migrate.py — One-shot DB migrations for chat-watcher-bot.

Usage:
    python src/migrate.py              # run all pending migrations
    python src/migrate.py --idempotent # safe to re-run (checks IF EXISTS)
"""

import sys
from src.config import load_config
import src.db as db


def _column_exists(config, table: str, column: str) -> bool:
    conn = db.get_connection(config)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = %s AND column_name = %s
                """, (table, column))
                return cur.fetchone() is not None
    finally:
        conn.close()


def _table_exists(config, table: str) -> bool:
    conn = db.get_connection(config)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = %s AND table_schema = 'public'
                """, (table,))
                return cur.fetchone() is not None
    finally:
        conn.close()


# ── Migrations ─────────────────────────────────────────────────────────────────

def migrate_v1_create_chat_messages(config):
    """Persist all parsed chat messages so summarizer can retrieve them later."""
    if _table_exists(config, "chat_messages"):
        print("  ✅ chat_messages table already exists.")
        return

    conn = db.get_connection(config)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE chat_messages (
                        id               BIGSERIAL PRIMARY KEY,
                        stream_id         INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                        time_in_seconds   DOUBLE PRECISION NOT NULL,
                        stream_timestamp  TEXT NOT NULL,
                        author            TEXT NOT NULL,
                        message           TEXT NOT NULL
                    );
                    CREATE INDEX idx_chat_messages_stream_time
                        ON chat_messages (stream_id, time_in_seconds);
                """)
    finally:
        conn.close()
    print("  ✅ Created chat_messages table + index.")


def migrate_v2_add_context_summary(config):
    """Store LLM-generated spike context summary."""
    if _column_exists(config, "spike_results", "context_summary"):
        print("  ✅ context_summary column already exists.")
        return

    conn = db.get_connection(config)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE spike_results ADD COLUMN context_summary TEXT")
    finally:
        conn.close()
    print("  ✅ Added context_summary column to spike_results.")


MIGRATIONS = [
    ("v1 — chat_messages table",     migrate_v1_create_chat_messages),
    ("v2 — context_summary column",  migrate_v2_add_context_summary),
]


def run_all():
    config = load_config()
    for name, fn in MIGRATIONS:
        print(f"⚡ {name}")
        fn(config)
    print("\n✅ All migrations complete.")


if __name__ == "__main__":
    run_all()
