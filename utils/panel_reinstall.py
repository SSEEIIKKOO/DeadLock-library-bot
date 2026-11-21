"""Repost the library/intro persistent panels once after each cold bot start.

Records channel and message IDs in a local SQLite index. Existing panels can be
found on migration by scanning a bounded number of recent messages. Reinstallation
sends first, stores the pointer, then removes the old post, avoiding data loss.
"""
from __future__ import annotations
import asyncio
import logging
import sqlite3
from pathlib import Path
import discord

LOG = logging.getLogger(__name__)
DB = Path("data/panel_registry.sqlite3")


def _connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute("CREATE TABLE IF NOT EXISTS panels (kind TEXT NOT NULL, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, PRIMARY KEY(kind,guild_id))")
    return conn


def remember(kind: str, guild_id: int, channel_id: int, message_id: int):
    with _connect() as conn:
        conn.execute("INSERT INTO panels VALUES(?,?,?,?) ON CONFLICT(kind,guild_id) DO UPDATE SET channel_id=excluded.channel_id,message_id=excluded.message_id", (kind, guild_id, channel_id, message_id))


def tracked(kind: str, guild_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT channel_id,message_id FROM panels WHERE kind=? AND guild_id=?", (kind,guild_id)).fetchone()
    return row


def panel_embed(kind: str) -> discord.Embed:
    if kind == "desk":
        e = discord.Embed(title="📚 Deadlock Library 受付カウンター", description=(
            "愛用ビルドの寄贈（登録）や、図書館に眠るビルドの検索ができます！\n\n"
            "📖 **ビルドを寄贈する**：ヒーロー・タグ・ランクを選んで6桁コードを登録\n"
            "🔍 **ビルドを探す**：ヒーローやタグから登録済みビルドを検索して表示"),color=discord.Color.blue())
        e.set_footer(text="※ ボタンを押した本人にのみ操作画面が表示されます")
        return e
    return discord.Embed(title="📝 プロフィール登録・編集", description=(
        "下のボタンから登録・編集できます。自己紹介はメインサーバーのアドレス帳に公開され、再登録時は同じ投稿を更新します。\n"
        "データのバックアップは図書館サーバーの管理者専用保管チャンネルに保存します。\n\n"
        "📋 **入力する内容**\n・旧ランク / 現ランク（ドロップダウン選択）\n"
        "・プレイヤー名（Steam名）\n・呼んでほしい名前\n"
        "・Steamフレンドコード（任意・`/match` のリザルトに名前が出ます）\n"
        "・使用キャラ / 一言"), color=discord.Color.teal())


def panel_view(kind: str):
    if kind == "desk":
        from cogs.library_desk import LibraryDeskView
        return LibraryDeskView()
    from cogs.profile_intro import IntroPanelView
    return IntroPanelView()


def is_target(message: discord.Message, kind: str, bot_id: int) -> bool:
    if message.author.id != bot_id:
        return False
    prefix = "library_desk:" if kind == "desk" else "profile_intro:"
    for row in message.components:
        for component in getattr(row, "children", ()):
            if str(getattr(component, "custom_id", "") or "").startswith(prefix):
                return True
    return False


async def _discover(guild: discord.Guild, kind: str, bot_id: int):
    # Initial migration only. Limit API work and don't search other guilds.
    scanned = 0
    for channel in guild.text_channels:
        if scanned >= 45:
            break
        if not channel.permissions_for(guild.me).read_message_history:
            continue
        scanned += 1
        try:
            async for msg in channel.history(limit=80):
                if is_target(msg, kind, bot_id):
                    remember(kind,guild.id,channel.id,msg.id)
                    return channel.id,msg.id
        except (discord.HTTPException, discord.Forbidden):
            continue
    return None


async def reinstall_for_guild(bot: discord.Client, guild: discord.Guild, kind: str):
    """Only edit the existing panel; do not post a new one on startup."""
    record = await asyncio.to_thread(tracked, kind, guild.id)
    if record is None:
        LOG.info('Panel %s not registered in guild %s; use /setup_%s once',kind,guild.id,kind)
        return
    channel = guild.get_channel(record[0])
    if not isinstance(channel, discord.TextChannel):
        LOG.warning('Panel channel missing: %s', record[0])
        return
    try:
        msg = await channel.fetch_message(record[1])
        if msg.author.id != bot.user.id or not is_target(msg,kind,bot.user.id):
            LOG.warning('Tracked %s message is not our panel; ignoring', kind)
            return
        await msg.edit(embed=panel_embed(kind), view=panel_view(kind),
                       allowed_mentions=discord.AllowedMentions.none())
        LOG.info('Edited %s panel message %s', kind, msg.id)
    except discord.NotFound:
        LOG.warning('Panel %s deleted; use /setup_%s manually',kind,kind)
    except (discord.Forbidden, discord.HTTPException):
        LOG.exception('Could not edit existing %s panel',kind)
