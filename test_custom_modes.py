"""Smoke test for 6v6/4v4 balancing and SQLite migration.
Run from project root: py tests/test_custom_modes.py
Does not connect to Discord or modify the production database.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.team_balancer import Player
from utils.custom_match_balance import candidates

for n in (8, 12):
    players = [Player(str(i + 100), 1200 + i * 25, 75) for i in range(n)]
    options = candidates(players)
    assert options
    for option in options:
        assert len(option['a']) == len(option['b']) == n // 2
        assert len(set(option['a'] + option['b'])) == n

with tempfile.TemporaryDirectory() as temp:
    # Mock the pre-migration custom_sessions schema, including one pending match.
    from utils import custom_match_store as store
    store.DB = Path(temp) / 'test.sqlite3'
    conn = sqlite3.connect(store.DB)
    conn.execute('''CREATE TABLE custom_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        host_id INTEGER NOT NULL, state TEXT NOT NULL,
        created_at REAL NOT NULL, changed_at REAL NOT NULL,
        ready_at REAL, started_at REAL, ended_at REAL,
        roster TEXT NOT NULL, teams TEXT NOT NULL DEFAULT '[]',
        frozen TEXT NOT NULL DEFAULT '{}', candidate_index INTEGER NOT NULL DEFAULT 0,
        rerolls INTEGER NOT NULL DEFAULT 0,
        ended_reason TEXT, winner TEXT, confirmed_by INTEGER)''')
    conn.execute("INSERT INTO custom_sessions(guild_id,host_id,state,created_at,changed_at,roster) VALUES(1,100,'pending',1,1,'[100]')")
    conn.commit()
    conn.close()

    assert store.get(1)['game_mode'] == 'normal'
    assert store.create(2, 200, 'street_brawl')['game_mode'] == 'street_brawl'
    normal = store.create(3, 300)
    assert normal['game_mode'] == 'normal'
    assert store.mark_result(1, 'A', 999)
    assert not store.mark_result(1, 'A', 999)

print('PASS: 4v4/6v6 balance, old DB migration and mode persistence')
