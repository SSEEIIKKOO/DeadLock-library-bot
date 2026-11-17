"""Forum posts form a many-to-one relationship with one Drive replay asset.

SQLite is the searchable index; back up data/replay_library.sqlite3.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get('REPLAY_INDEX_DB', 'data/replay_library.sqlite3'))


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('''CREATE TABLE IF NOT EXISTS replay_posts (
        thread_id TEXT PRIMARY KEY,
        match_id TEXT NOT NULL,
        caption TEXT NOT NULL,
        author_id TEXT NOT NULL,
        author_name TEXT NOT NULL,
        thread_url TEXT NOT NULL,
        drive_url TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_replay_posts_match ON replay_posts(match_id)')
    return conn


def add_post(match_id: str, caption: str, author_id: int, author_name: str,
             thread_id: int, thread_url: str, drive_url: str):
    with _connect() as db:
        db.execute('''INSERT OR REPLACE INTO replay_posts
          (thread_id,match_id,caption,author_id,author_name,thread_url,drive_url,created_at)
          VALUES (?,?,?,?,?,?,?,?)''',
          (str(thread_id), match_id, caption, str(author_id), author_name,
           thread_url, drive_url, datetime.now(timezone.utc).isoformat()))


def search_posts(term: str, limit: int = 20):
    """Match by complete match ID or case-insensitive substring of ANY caption.

    LIKE wildcard characters are escaped to prevent arbitrary pattern matching.
    """
    escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    with _connect() as db:
        return [dict(r) for r in db.execute('''
            SELECT * FROM replay_posts
            WHERE match_id = ? OR caption LIKE ? ESCAPE '\\' COLLATE NOCASE
            ORDER BY created_at DESC LIMIT ?''', (term, f'%{escaped}%', limit))]


def posts_for_match(match_id: str, limit: int = 20):
    with _connect() as db:
        return [dict(r) for r in db.execute('''
            SELECT * FROM replay_posts WHERE match_id = ?
            ORDER BY created_at DESC LIMIT ?''', (match_id,limit))]
