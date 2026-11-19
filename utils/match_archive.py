"""Discord private text-channel archive of immutable match metadata.

Only archived matches are indexed; posting and verifying the attachment precedes
local cache deletion. Never put bot secrets or other personal files here.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord

LOG = logging.getLogger(__name__)
DB_PATH = Path(os.environ.get('REPLAY_INDEX_DB', 'data/replay_library.sqlite3'))
MAX_JSON_BYTES = 24 * 1024 * 1024
MAX_COMPRESSED_BYTES = 12 * 1024 * 1024
_locks: dict[str, asyncio.Lock] = {}


def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute('PRAGMA busy_timeout=30000')
    db.execute('''CREATE TABLE IF NOT EXISTS match_data_archive (
        match_id TEXT PRIMARY KEY,
        channel_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        checksum TEXT NOT NULL,
        saved_at TEXT NOT NULL
    )''')
    return db


def lookup(match_id: str):
    with _db() as db:
        row = db.execute('SELECT channel_id,message_id,checksum FROM match_data_archive WHERE match_id=?', (match_id,)).fetchone()
        return row


def _record(match_id: str, channel_id: int, message_id: int, digest: str):
    with _db() as db:
        db.execute('''INSERT OR REPLACE INTO match_data_archive
            (match_id,channel_id,message_id,checksum,saved_at) VALUES (?,?,?,?,?)''',
            (match_id, str(channel_id), str(message_id), digest, datetime.now(timezone.utc).isoformat()))


def _remove_index(match_id: str):
    with _db() as db:
        db.execute('DELETE FROM match_data_archive WHERE match_id=?', (match_id,))


def _encode(data: dict) -> bytes:
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('JSON is too large for archive')
    return raw


def _decode(compressed: bytes) -> dict:
    if len(compressed) > MAX_COMPRESSED_BYTES:
        raise ValueError('Archive attachment too large')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as f:
        raw = f.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('Decompressed JSON too large')
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError('Match metadata must be a JSON object')
    return obj


def _check_id(match_id: str, data: dict):
    if not match_id.isdecimal() or len(match_id) > 32:
        raise ValueError('Invalid match ID')
    info = data.get('match_info', data)
    if not isinstance(info, dict):
        raise ValueError('Invalid metadata')
    # Match ID can be absent in some API versions; compare when present.
    actual = info.get('match_id', data.get('match_id'))
    if actual is not None and str(actual) != match_id:
        raise ValueError('Metadata match ID mismatch')


async def _channel(bot: discord.Client):
    channel_id = int(os.getenv('MATCH_DATA_STORAGE_CHANNEL_ID', '0') or '0')
    if not channel_id:
        raise RuntimeError('MATCH_DATA_STORAGE_CHANNEL_ID is missing')
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise RuntimeError('Storage must be a text channel')
    return channel


async def get_archived(bot: discord.Client, match_id: str) -> dict | None:
    """Returns None on miss/broken archive; never silently deletes index."""
    row = await asyncio.to_thread(lookup, match_id)
    if row is None:
        return None
    ch_id, msg_id, digest = row
    try:
        channel = await _channel(bot)
        if channel.id != int(ch_id):
            raise RuntimeError('Archive channel changed; original index kept')
        message = await channel.fetch_message(int(msg_id))
        if not message.attachments:
            raise ValueError('Attachment missing')
        attachment = next((a for a in message.attachments if a.filename == f'match_{match_id}.json.gz'), None)
        if attachment is None or attachment.size > MAX_COMPRESSED_BYTES:
            raise ValueError('Attachment missing or too big')
        packed = await attachment.read()
        data = await asyncio.to_thread(_decode, packed)
        await asyncio.to_thread(_check_id, match_id, data)
        if hashlib.sha256(_encode(data)).hexdigest() != digest:
            raise ValueError('Archive checksum mismatch')
        return data
    except (discord.HTTPException, discord.Forbidden, ValueError, RuntimeError, OSError, EOFError, json.JSONDecodeError) as exc:
        LOG.warning('Match archive unavailable match=%s: %s', match_id, exc)
        return None


async def store_archived(bot: discord.Client, match_id: str, data: dict) -> bool:
    """Returns True only after a verified Discord copy and SQLite entry exist."""
    lock = _locks.setdefault(match_id, asyncio.Lock())
    async with lock:
        if await get_archived(bot, match_id) is not None:
            return True
        await asyncio.to_thread(_check_id, match_id, data)
        raw = await asyncio.to_thread(_encode, data)
        packed = await asyncio.to_thread(gzip.compress, raw, 9)
        if len(packed) > MAX_COMPRESSED_BYTES:
            raise ValueError('Compressed attachment is too large')
        digest = hashlib.sha256(raw).hexdigest()
        channel = await _channel(bot)
        posted = await channel.send(
            content=f'🔒 Match `{match_id}` | SHA256 `{digest[:16]}…`',
            file=discord.File(io.BytesIO(packed), filename=f'match_{match_id}.json.gz'),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # Confirm that the remote payload is actually retrievable and valid.
        verified = await channel.fetch_message(posted.id)
        attach = next((a for a in verified.attachments if a.filename == f'match_{match_id}.json.gz'), None)
        if not attach or attach.size > MAX_COMPRESSED_BYTES:
            raise RuntimeError('Archive verification failed: attachment missing')
        fetched = await attach.read()
        decoded = await asyncio.to_thread(_decode, fetched)
        if hashlib.sha256(_encode(decoded)).hexdigest() != digest:
            raise RuntimeError('Archive verification failed: checksum mismatch')
        await asyncio.to_thread(_record, match_id, channel.id, posted.id, digest)
        return True


def read_local(match_id: str) -> dict | None:
    path = Path('data/match_cache') / f'{match_id}.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return None
        _check_id(match_id, data)
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def delete_local_after_archive(match_id: str, data: dict) -> bool:
    """Delete only matching local cache; do not delete if file was replaced."""
    path = Path('data/match_cache') / f'{match_id}.json'
    try:
        original = read_local(match_id)
        if original is None:
            return False
        if hashlib.sha256(_encode(original)).hexdigest() != hashlib.sha256(_encode(data)).hexdigest():
            LOG.warning('Cache updated during archiving: retained %s', match_id)
            return False
        path.unlink()
        return True
    except OSError as exc:
        LOG.warning('Could not remove archived cache %s: %s', match_id, exc)
        return False
