"""Local CR index and snapshot state. No secrets are stored here."""
from __future__ import annotations
import json
import os
import sqlite3
import time
from pathlib import Path

DB = Path(os.getenv('CR_DB_PATH', 'data/cr_players.sqlite3'))

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('''CREATE TABLE IF NOT EXISTS players (
        discord_id INTEGER PRIMARY KEY, steam32 INTEGER NOT NULL UNIQUE,
        a REAL NOT NULL DEFAULT 1250, b REAL NOT NULL DEFAULT 0,
        c REAL NOT NULL DEFAULT 0, confidence INTEGER NOT NULL DEFAULT 0,
        badge INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 0, remote_message_id INTEGER,
        last_remote_error TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, discord_id INTEGER NOT NULL,
        admin_id INTEGER NOT NULL, old_c REAL NOT NULL, new_c REAL NOT NULL,
        reason TEXT NOT NULL, created_at REAL NOT NULL)''')
    db.commit()
    return db

def get(discord_id: int):
    with connect() as db:
        row = db.execute('SELECT * FROM players WHERE discord_id=?', (discord_id,)).fetchone()
        return dict(row) if row else None

def register(discord_id: int, steam32: int):
    now = time.time()
    with connect() as db:
        old = db.execute('SELECT steam32 FROM players WHERE discord_id=?', (discord_id,)).fetchone()
        if old and int(old['steam32']) != steam32:
            raise ValueError('登録済みのSteam IDの変更は管理者に相談してください。')
        db.execute('INSERT OR IGNORE INTO players(discord_id,steam32,updated_at) VALUES(?,?,0)', (discord_id, steam32))
        db.commit()
    return get(discord_id)

def update_rating(discord_id: int, a: float, confidence: int, badge: int, updated_at: float):
    with connect() as db:
        db.execute('UPDATE players SET a=?,confidence=?,badge=?,updated_at=?,version=version+1 WHERE discord_id=?',
                   (a, confidence, badge, updated_at, discord_id))
        db.commit()
    return get(discord_id)

def adjust(discord_id: int, admin_id: int, delta: float, reason: str):
    with connect() as db:
        row = db.execute('SELECT c FROM players WHERE discord_id=?', (discord_id,)).fetchone()
        if row is None:
            raise ValueError('先にプレイヤー登録が必要です。')
        old = float(row['c']); new = round(old + delta, 2)
        if abs(new) > 500:
            raise ValueError('管理補正Cは暫定的に±500までです。')
        db.execute('UPDATE players SET c=?,version=version+1 WHERE discord_id=?', (new,discord_id))
        db.execute('INSERT INTO adjustments(discord_id,admin_id,old_c,new_c,reason,created_at) VALUES(?,?,?,?,?,?)',
                   (discord_id,admin_id,old,new,reason,time.time()))
        db.commit()
    return get(discord_id)

def set_remote(discord_id: int, message_id: int | None, error: str | None):
    with connect() as db:
        db.execute('UPDATE players SET remote_message_id=COALESCE(?,remote_message_id),last_remote_error=? WHERE discord_id=?',
                   (message_id,error,discord_id))
        db.commit()

def history(discord_id: int, limit=10):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM adjustments WHERE discord_id=? ORDER BY id DESC LIMIT ?', (discord_id, limit))]

def restore_snapshot(snapshot: dict):
    # Applies only if local record is absent or older, never silently overwrite newer data.
    did=int(snapshot['discord_id']); sid=int(snapshot['steam32']); remote_ver=int(snapshot['version'])
    old=get(did)
    if old and int(old['version'])>=remote_ver:
        return False
    with connect() as db:
        db.execute('''INSERT INTO players(discord_id,steam32,a,b,c,confidence,badge,updated_at,version)
                      VALUES(?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(discord_id) DO UPDATE SET
                      steam32=excluded.steam32,a=excluded.a,b=excluded.b,c=excluded.c,
                      confidence=excluded.confidence,badge=excluded.badge,
                      updated_at=excluded.updated_at,version=excluded.version''',
                   (did,sid,float(snapshot['a']),float(snapshot.get('b',0)),float(snapshot['c']),
                    int(snapshot['confidence']),int(snapshot['badge']),float(snapshot['updated_at']),remote_ver))
        db.commit()
    return True

# ---------------------- B custom-match ledger ----------------------
def ensure_custom_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS cr_custom_matches (
        match_key TEXT PRIMARY KEY, winner TEXT NOT NULL,
        admin_id INTEGER NOT NULL, created_at REAL NOT NULL,
        team_a TEXT NOT NULL, team_b TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS cr_custom_events (
        match_key TEXT NOT NULL, discord_id INTEGER NOT NULL,
        delta REAL NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(match_key,discord_id),
        FOREIGN KEY(match_key) REFERENCES cr_custom_matches(match_key))''')


def events(discord_id: int) -> list[dict]:
    with connect() as db:
        ensure_custom_schema(db)
        return [dict(r) for r in db.execute(
            'SELECT match_key,discord_id,delta,created_at FROM cr_custom_events WHERE discord_id=? ORDER BY created_at',
            (discord_id,))]


def current_b(discord_id: int, now: float | None = None) -> float:
    from utils.cr_custom import b_from_events
    return b_from_events(events(discord_id), now)


def record_custom_match(match_key: str, winner: str, admin_id: int,
                        team_a: list[int], team_b: list[int],
                        delta: dict[int, float], created_at: float) -> None:
    import json
    if len(team_a) != 6 or len(team_b) != 6 or len(set(team_a + team_b)) != 12:
        raise ValueError('A/Bそれぞれ6人必要です。')
    with connect() as db:
        ensure_custom_schema(db)
        try:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM cr_custom_matches WHERE match_key=?', (match_key,)).fetchone():
                raise ValueError('この試合IDはすでに登録されています。')
            for did in team_a + team_b:
                if db.execute('SELECT 1 FROM players WHERE discord_id=?',(did,)).fetchone() is None:
                    raise ValueError(f'CR未登録ユーザー: {did}')
            db.execute('INSERT INTO cr_custom_matches VALUES(?,?,?,?,?,?)',
                       (match_key,winner,admin_id,created_at,json.dumps(team_a),json.dumps(team_b)))
            for did in team_a + team_b:
                db.execute('INSERT INTO cr_custom_events VALUES(?,?,?,?)',
                           (match_key,did,delta[did],created_at))
                # legacy b field kept as a convenient last-calculated display, not source of truth
                db.execute('UPDATE players SET version=version+1 WHERE discord_id=?',(did,))
            db.commit()
        except Exception:
            db.rollback()
            raise


def restore_events(discord_id: int, ledger: list[dict]):
    """Restore user's events from a valid remote snapshot, without creating fake match rows.

    Snapshot events are the authoritative source for that user's B; admin match
    history is still local-only. These rows permit correct decay after restore.
    """
    with connect() as db:
        ensure_custom_schema(db)
        for item in ledger:
            key = str(item['match_key'])
            if len(key) > 100 or not key:
                raise ValueError('Invalid match key')
            created=float(item['created_at']); delta=float(item['delta'])
            if not __import__('math').isfinite(created) or not __import__('math').isfinite(delta):
                raise ValueError('Invalid event')
            # Restore dummy metadata if needed. Real record remains unchanged if present.
            db.execute('INSERT OR IGNORE INTO cr_custom_matches VALUES(?,?,?,?,?,?)',
                       (key,'?',0,created,'[]','[]'))
            db.execute('INSERT OR IGNORE INTO cr_custom_events VALUES(?,?,?,?)',
                       (key,discord_id,delta,created))
        db.commit()
