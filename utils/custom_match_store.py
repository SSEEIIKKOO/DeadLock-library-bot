"""Persistent one-active-custom-match-per-guild state (SQLite).

IMPORTANT: this SQLite is operational state and must be backed up.
"""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.getenv('CUSTOM_DB_PATH', 'data/custom_match.sqlite3'))
ACTIVE = ('recruiting', 'ready', 'playing')


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS custom_panels (
        guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS custom_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        host_id INTEGER NOT NULL, state TEXT NOT NULL,
        created_at REAL NOT NULL, changed_at REAL NOT NULL,
        ready_at REAL, started_at REAL, ended_at REAL,
        roster TEXT NOT NULL, teams TEXT NOT NULL DEFAULT '[]',
        frozen TEXT NOT NULL DEFAULT '{}', candidate_index INTEGER NOT NULL DEFAULT 0,
        rerolls INTEGER NOT NULL DEFAULT 0,
        ended_reason TEXT, winner TEXT, confirmed_by INTEGER)''')
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS custom_one_active_per_guild
        ON custom_sessions(guild_id) WHERE state IN ('recruiting', 'ready', 'playing')""")
    conn.commit()
    return conn


def decoded(row):
    if row is None:
        return None
    data = dict(row)
    for key in ('roster', 'teams', 'frozen'):
        data[key] = json.loads(data[key])
    return data


def active(guild_id):
    with connect() as db:
        return decoded(db.execute("SELECT * FROM custom_sessions WHERE guild_id=? AND state IN ('recruiting','ready','playing')", (guild_id,)).fetchone())


def get(session_id):
    with connect() as db:
        return decoded(db.execute('SELECT * FROM custom_sessions WHERE id=?', (session_id,)).fetchone())


def create(guild_id, creator_id):
    now = time.time()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM custom_sessions WHERE guild_id=? AND state IN ('recruiting','ready','playing')", (guild_id,)).fetchone():
            raise ValueError('現在の募集または試合が終了してから開始してください。')
        cur = db.execute('''INSERT INTO custom_sessions(guild_id,host_id,state,created_at,changed_at,roster)
            VALUES(?,?, 'recruiting', ?, ?, ?)''',
            (guild_id, creator_id, now, now, json.dumps([creator_id])))
        db.commit()
        return get(cur.lastrowid)


def save(s):
    with connect() as db:
        db.execute('''UPDATE custom_sessions SET host_id=?,state=?,changed_at=?,ready_at=?,
            started_at=?,ended_at=?,roster=?,teams=?,frozen=?,candidate_index=?,rerolls=?,
            ended_reason=?,winner=?,confirmed_by=? WHERE id=?''',
            (s['host_id'],s['state'],s['changed_at'],s['ready_at'],s['started_at'],s['ended_at'],
             json.dumps(s['roster']),json.dumps(s['teams']),json.dumps(s['frozen']),
             s['candidate_index'],s['rerolls'],s['ended_reason'],s['winner'],s['confirmed_by'],s['id']))
        db.commit()


def panel(guild_id):
    with connect() as db:
        row=db.execute('SELECT channel_id,message_id FROM custom_panels WHERE guild_id=?',(guild_id,)).fetchone()
        return dict(row) if row else None


def set_panel(guild_id, channel_id, message_id):
    with connect() as db:
        db.execute('''INSERT INTO custom_panels(guild_id,channel_id,message_id) VALUES(?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id''',
            (guild_id,channel_id,message_id))
        db.commit()


def pending(guild_id, limit=20):
    with connect() as db:
        return [decoded(r) for r in db.execute('''SELECT * FROM custom_sessions
            WHERE guild_id=? AND state='pending' ORDER BY id DESC LIMIT ?''',(guild_id,limit))]


def all_active():
    with connect() as db:
        return [decoded(r) for r in db.execute("SELECT * FROM custom_sessions WHERE state IN ('recruiting','ready','playing')")]


def mark_result(session_id, winner, admin_id):
    """Only after the CR ledger transaction succeeds. Called under guild lock."""
    with connect() as db:
        cur=db.execute('''UPDATE custom_sessions SET state='completed',winner=?,confirmed_by=?
            WHERE id=? AND state='pending' ''',(winner,admin_id,session_id))
        db.commit()
        return cur.rowcount == 1
