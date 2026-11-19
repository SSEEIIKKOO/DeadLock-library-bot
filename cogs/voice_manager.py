"""使い捨てVC管理Cog (discord.py 2.3+)

.env: TEMP_VC_ENJOY_CREATE_ID, TEMP_VC_SERIOUS_CREATE_ID,
        TEMP_VC_OTHER_CREATE_ID (いずれも作成用の既存ボイスチャンネルID)
        TEMP_VC_LOG_CHANNEL_ID (任意)
Botには members / voice_states Intent と Manage Channels / Move Members が必要。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

LOG = logging.getLogger(__name__)
DB_PATH = Path(os.getenv("TEMP_VC_DB_PATH", "data/temp_voice.sqlite3"))
MAX_PER_CATEGORY = 10
COOLDOWN_SECONDS = 10
EMPTY_DELETE_SECONDS = 30


def _configured_triggers() -> dict[int, str]:
    values = {
        "TEMP_VC_ENJOY_CREATE_ID": "エンジョイ",
        "TEMP_VC_SERIOUS_CREATE_ID": "真剣",
        "TEMP_VC_OTHER_CREATE_ID": "別ゲー",
    }
    result = {}
    for key, label in values.items():
        raw = os.getenv(key, "").strip()
        if raw:
            try:
                cid = int(raw)
                if cid > 0:
                    result[cid] = label
            except ValueError:
                LOG.error("%s が数値ではありません: %r", key, raw)
    return result


def _is_human(member: discord.Member) -> bool:
    return not member.bot


def _humans(channel: discord.VoiceChannel) -> list[discord.Member]:
    return [m for m in channel.members if _is_human(m)]


def _key(target: discord.abc.Snowflake) -> str:
    return ("role:" if isinstance(target, discord.Role) else "member:") + str(target.id)


class VoiceDB:
    def __init__(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS rooms (
            channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            panel_message_id INTEGER, locked INTEGER NOT NULL DEFAULT 0,
            allow_json TEXT NOT NULL DEFAULT '[]',
            baseline_json TEXT NOT NULL DEFAULT '{}',
            join_json TEXT NOT NULL DEFAULT '{}',
            created REAL NOT NULL
        )""")
        self.conn.commit()

    def get(self, cid: int):
        return self.conn.execute("SELECT * FROM rooms WHERE channel_id=?", (cid,)).fetchone()

    def all(self):
        return self.conn.execute("SELECT * FROM rooms").fetchall()

    def by_category(self, guild_id: int, cat_id: int):
        return self.conn.execute("SELECT COUNT(*) FROM rooms WHERE guild_id=? AND category_id=?", (guild_id, cat_id)).fetchone()[0]

    def owned(self, guild_id: int, user_id: int):
        return self.conn.execute("SELECT COUNT(*) FROM rooms WHERE guild_id=? AND owner_id=?", (guild_id, user_id)).fetchone()[0]

    def create(self, channel: discord.VoiceChannel, owner: discord.Member):
        self.conn.execute("INSERT INTO rooms (channel_id,guild_id,category_id,owner_id,join_json,created) VALUES(?,?,?,?,?,?)",
                            (channel.id, channel.guild.id, channel.category_id, owner.id, json.dumps({str(owner.id): time.time()}), time.time()))
        self.conn.commit()

    def update(self, cid: int, **fields):
        allowed = {"owner_id", "panel_message_id", "locked", "allow_json", "baseline_json", "join_json"}
        if not fields or not set(fields).issubset(allowed):
            raise ValueError("Invalid database field")
        sql = ", ".join(f"{name}=?" for name in fields)
        self.conn.execute(f"UPDATE rooms SET {sql} WHERE channel_id=?", (*fields.values(), cid))
        self.conn.commit()

    def delete(self, cid: int):
        self.conn.execute("DELETE FROM rooms WHERE channel_id=?", (cid,))
        self.conn.commit()

    def close(self):
        self.conn.close()


class NameModal(discord.ui.Modal, title="VC名の変更"):
    new_name = discord.ui.TextInput(label="新しい部屋名", min_length=1, max_length=50)

    def __init__(self, manager: "VoiceManager", channel_id: int):
        super().__init__()
        self.manager, self.channel_id = manager, channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.manager.authorized(interaction, self.channel_id):
            return
        channel = interaction.guild.get_channel(self.channel_id)
        try:
            await channel.edit(name=str(self.new_name).strip(), reason=f"VC owner: {interaction.user.id}")
            await self.manager.refresh(self.channel_id)
            await interaction.response.send_message("VC名を変更しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("VC名の変更に失敗しました。", ephemeral=True)


class LimitModal(discord.ui.Modal, title="VC人数制限"):
    limit = discord.ui.TextInput(label="0=無制限、1〜99=上限人数", placeholder="0", max_length=2)

    def __init__(self, manager: "VoiceManager", channel_id: int):
        super().__init__()
        self.manager, self.channel_id = manager, channel_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.manager.authorized(interaction, self.channel_id):
            return
        raw = str(self.limit).strip()
        if not raw.isdecimal() or int(raw) > 99:
            await interaction.response.send_message("0〜99の数字を入力してください。", ephemeral=True)
            return
        try:
            await interaction.guild.get_channel(self.channel_id).edit(user_limit=int(raw))
            await self.manager.refresh(self.channel_id)
            await interaction.response.send_message("人数上限を更新しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("人数上限の変更に失敗しました。", ephemeral=True)


class MemberPicker(discord.ui.Select):
    def __init__(self, manager: "VoiceManager", cid: int, action: str, members: list[discord.Member]):
        self.manager, self.cid, self.action = manager, cid, action
        options = [discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in members[:25]]
        super().__init__(placeholder="メンバーを選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if not await self.manager.authorized(interaction, self.cid):
            return
        channel = interaction.guild.get_channel(self.cid)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("VCが見つかりません。", ephemeral=True)
            return
        member = next((m for m in channel.members if str(m.id) == self.values[0] and not m.bot), None)
        if member is None:
            await interaction.response.send_message("対象者は既に退出しています。", ephemeral=True)
            return
        try:
            if self.action == "kick":
                if member.id == interaction.user.id:
                    await interaction.response.send_message("自分自身はキックできません。", ephemeral=True)
                    return
                await member.move_to(None, reason=f"Temporary VC kick by {interaction.user.id}")
                await self.manager.audit(channel.guild, f"VCキック: {member.id} (VC {self.cid})")
                await interaction.response.send_message("VCから切断しました。再入室は禁止しません。", ephemeral=True)
            else:
                self.manager.db.update(self.cid, owner_id=member.id)
                await self.manager.reapply_lock(channel)
                await self.manager.refresh(self.cid)
                await self.manager.audit(channel.guild, f"VCオーナー譲渡: {member.id} (VC {self.cid})")
                await interaction.response.send_message(f"オーナーを{member.mention}に譲渡しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("操作に失敗しました。Bot権限を確認してください。", ephemeral=True)


class MemberPickerView(discord.ui.View):
    def __init__(self, manager, cid, action, members):
        super().__init__(timeout=90)
        self.add_item(MemberPicker(manager, cid, action, members))


class AllowUserPicker(discord.ui.UserSelect):
    def __init__(self, manager, cid):
        super().__init__(placeholder="入室を許可するユーザー", min_values=1, max_values=1)
        self.manager, self.cid = manager, cid

    async def callback(self, interaction: discord.Interaction):
        if not await self.manager.authorized(interaction, self.cid):
            return
        target = self.values[0]
        if target.bot:
            await interaction.response.send_message("Botは許可リストへ登録できません。", ephemeral=True)
            return
        row = self.manager.db.get(self.cid)
        allowed = set(json.loads(row["allow_json"]))
        allowed.add(target.id)
        self.manager.db.update(self.cid, allow_json=json.dumps(sorted(allowed)))
        try:
            await self.manager.reapply_lock(interaction.guild.get_channel(self.cid))
            await self.manager.refresh(self.cid)
            await interaction.response.send_message(f"{target.mention}を許可リストに追加しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("保存しましたが権限反映に失敗しました。", ephemeral=True)


class AllowPickerView(discord.ui.View):
    def __init__(self, manager, cid):
        super().__init__(timeout=90)
        self.add_item(AllowUserPicker(manager, cid))


class RemoveAllowPicker(discord.ui.Select):
    def __init__(self, manager, cid, members):
        self.manager, self.cid = manager, cid
        super().__init__(placeholder="許可を解除するユーザー", options=[discord.SelectOption(label=str(uid), value=str(uid)) for uid in members[:25]])

    async def callback(self, interaction: discord.Interaction):
        if not await self.manager.authorized(interaction, self.cid):
            return
        row = self.manager.db.get(self.cid)
        allowed = set(json.loads(row["allow_json"]))
        allowed.discard(int(self.values[0]))
        self.manager.db.update(self.cid, allow_json=json.dumps(sorted(allowed)))
        try:
            await self.manager.reapply_lock(interaction.guild.get_channel(self.cid))
            await self.manager.refresh(self.cid)
            await interaction.response.send_message("許可リストから削除しました。現在のVC参加者は切断しません。", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message("保存しましたが権限反映に失敗しました。", ephemeral=True)


class RemoveAllowView(discord.ui.View):
    def __init__(self, manager, cid, ids):
        super().__init__(timeout=90)
        self.add_item(RemoveAllowPicker(manager, cid, ids))


class ControlView(discord.ui.View):
    def __init__(self, manager: "VoiceManager", cid: int):
        super().__init__(timeout=None)
        self.manager, self.cid = manager, cid
        actions = [
            ("部屋名", "rename", discord.ButtonStyle.secondary, 0),
            ("人数", "limit", discord.ButtonStyle.secondary, 0),
            ("ロック切替", "lock", discord.ButtonStyle.primary, 0),
            ("キック", "kick", discord.ButtonStyle.danger, 0),
            ("許可追加", "allow_add", discord.ButtonStyle.secondary, 1),
            ("許可解除", "allow_remove", discord.ButtonStyle.secondary, 1),
            ("オーナー譲渡", "transfer", discord.ButtonStyle.secondary, 1),
        ]
        for label, action, style, row in actions:
            button = discord.ui.Button(label=label, custom_id=f"temporary_voice:{cid}:{action}", style=style, row=row)
            async def callback(interaction: discord.Interaction, chosen=action):
                await self.run_action(interaction, chosen)
            button.callback = callback
            self.add_item(button)

    async def run_action(self, interaction: discord.Interaction, action: str):
        if not await self.manager.authorized(interaction, self.cid):
            return
        channel = interaction.guild.get_channel(self.cid)
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("VCが見つかりません。", ephemeral=True)
            return
        if action == "rename":
            await interaction.response.send_modal(NameModal(self.manager, self.cid))
        elif action == "limit":
            await interaction.response.send_modal(LimitModal(self.manager, self.cid))
        elif action == "lock":
            await interaction.response.defer(ephemeral=True)
            try:
                new_state = not bool(self.manager.db.get(self.cid)["locked"])
                await self.manager.set_lock(channel, new_state)
                await self.manager.refresh(self.cid)
                await interaction.followup.send("ロックしました。" if new_state else "ロック解除しました。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("権限変更に失敗しました。", ephemeral=True)
        elif action in ("kick", "transfer"):
            candidates = [m for m in _humans(channel) if m.id != interaction.user.id]
            if action == "transfer":
                candidates = [m for m in candidates if not self.manager.db.owned(channel.guild.id, m.id)]
            if not candidates:
                await interaction.response.send_message("選択できるメンバーがいません。", ephemeral=True)
                return
            await interaction.response.send_message(
                "対象者を選択してください。" + ("（先頭25人を表示）" if len(candidates)>25 else ""),
                view=MemberPickerView(self.manager, self.cid, action, candidates), ephemeral=True)
        elif action == "allow_add":
            await interaction.response.send_message("許可するユーザーを選択してください。", view=AllowPickerView(self.manager, self.cid), ephemeral=True)
        elif action == "allow_remove":
            ids = json.loads(self.manager.db.get(self.cid)["allow_json"])
            if not ids:
                await interaction.response.send_message("許可リストは空です。", ephemeral=True)
                return
            await interaction.response.send_message("許可を解除するユーザーを選択してください。", view=RemoveAllowView(self.manager, self.cid, ids), ephemeral=True)


class VoiceManager(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers = _configured_triggers()
        self.db = VoiceDB()
        self.create_lock: dict[int, asyncio.Lock] = {}
        self.cooldowns: dict[tuple[int, int], float] = {}
        self.delete_tasks: dict[int, asyncio.Task] = {}
        self.recovered = False

    async def cog_load(self):
        # Restart後も旧パネルからの操作を受け付ける
        for row in self.db.all():
            if row["panel_message_id"]:
                self.bot.add_view(ControlView(self, row["channel_id"]), message_id=row["panel_message_id"])
        if not self.triggers:
            LOG.warning("使い捨てVC作成用チャンネルID未設定: VC生成は無効です")

    async def cog_unload(self):
        for task in self.delete_tasks.values():
            task.cancel()
        self.db.close()

    async def audit(self, guild: discord.Guild, message: str):
        LOG.info("[%s] %s", guild.id, message)
        raw = os.getenv("TEMP_VC_LOG_CHANNEL_ID", "")
        if raw.isdigit():
            channel = guild.get_channel(int(raw))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    LOG.warning("VCログ送信失敗: %s", message)

    async def authorized(self, interaction: discord.Interaction, cid: int) -> bool:
        row = self.db.get(cid)
        if not row or interaction.guild is None or interaction.guild.id != row["guild_id"]:
            await interaction.response.send_message("管理対象のVCではありません。", ephemeral=True)
            return False
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel or member.voice.channel.id != cid:
            await interaction.response.send_message("このVCに入室しているときのみ操作できます。", ephemeral=True)
            return False
        if member.id != row["owner_id"] and not member.guild_permissions.administrator:
            await interaction.response.send_message("操作できるのはオーナーかサーバー管理者のみです。", ephemeral=True)
            return False
        return True

    def _embed(self, channel: discord.VoiceChannel) -> discord.Embed:
        row = self.db.get(channel.id)
        e = discord.Embed(title=f"🎧 {channel.name} — VC管理", color=discord.Color.blurple())
        e.description = (f"**オーナー:** <@{row['owner_id']}>\n"
                        f"**参加人数:** {len(_humans(channel))}人\n"
                        f"**上限:** {'無制限' if channel.user_limit == 0 else str(channel.user_limit)+'人'}\n"
                        f"**入室:** {'🔒 ロック' if row['locked'] else '🔓 公開'}\n"
                        f"**許可リスト:** {len(json.loads(row['allow_json']))}人")
        e.set_footer(text="オーナーと管理者だけが操作可能／空室30秒で自動削除")
        return e

    async def refresh(self, cid: int, *, reinstall: bool = False):
        row = self.db.get(cid)
        if row is None:
            return
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel(cid) if guild else None
        if not isinstance(channel, discord.VoiceChannel):
            return
        if row["panel_message_id"] and not reinstall:
            try:
                msg = await channel.fetch_message(row["panel_message_id"])
                await msg.edit(embed=self._embed(channel), view=ControlView(self, cid))
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                LOG.warning("管理パネルの編集不可、再作成: %s", cid)
        try:
            sent = await channel.send(embed=self._embed(channel), view=ControlView(self, cid), allowed_mentions=discord.AllowedMentions.none())
            old_mid = row["panel_message_id"]
            self.db.update(cid, panel_message_id=sent.id)
            self.bot.add_view(ControlView(self, cid), message_id=sent.id)
            if reinstall and old_mid:
                try:
                    old = await channel.fetch_message(old_mid)
                    if old.author.id == self.bot.user.id:
                        await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOG.warning("VC旧パネルの削除に失敗: %s", old_mid)
        except discord.HTTPException:
            LOG.exception("VC管理パネル送信失敗: %s", cid)

    def _baseline(self, row) -> dict[str, object]:
        return json.loads(row["baseline_json"])

    async def _restore_overwrites(self, channel: discord.VoiceChannel, baseline: dict[str, object]):
        for key, val in baseline.items():
            typ, raw_id = key.split(":", 1)
            target = channel.guild.get_role(int(raw_id)) if typ == "role" else channel.guild.get_member(int(raw_id))
            if target is None and typ == "member":
                try:
                    target = await channel.guild.fetch_member(int(raw_id))
                except discord.HTTPException:
                    continue
            if target is None:
                continue
            overwrite = channel.overwrites_for(target)
            overwrite.connect = val  # None / True / False
            await channel.set_permissions(target, overwrite=overwrite, reason="Temporary VC unlock")

    async def set_lock(self, channel: discord.VoiceChannel, locked: bool):
        row = self.db.get(channel.id)
        if not row:
            return
        if locked:
            # 既存の明示的Connect許可を保存してからロックに切り替える
            baseline = {}
            targets = dict(channel.overwrites)
            targets.setdefault(channel.guild.default_role, channel.overwrites_for(channel.guild.default_role))
            for target, overwrite in targets.items():
                baseline[_key(target)] = overwrite.connect
            self.db.update(channel.id, baseline_json=json.dumps(baseline), locked=1)
            await self.reapply_lock(channel)
        else:
            baseline = self._baseline(row)
            # ロック時に追加したメンバー権限も解除する
            for target in list(channel.overwrites):
                baseline.setdefault(_key(target), None)
            await self._restore_overwrites(channel, baseline)
            self.db.update(channel.id, locked=0, baseline_json='{}')

    async def reapply_lock(self, channel: discord.VoiceChannel):
        row = self.db.get(channel.id)
        if not row or not row["locked"]:
            return
        baseline = self._baseline(row)
        targets = dict(channel.overwrites)
        targets.setdefault(channel.guild.default_role, channel.overwrites_for(channel.guild.default_role))
        # ロック以前の権限上書きを記録。接続権限以外は変更しない
        for target, overwrite in targets.items():
            baseline.setdefault(_key(target), overwrite.connect)
            if isinstance(target, discord.Role) or isinstance(target, discord.Member):
                overwrite.connect = False
                await channel.set_permissions(target, overwrite=overwrite, reason="Temporary VC lock")
        allowed = set(json.loads(row["allow_json"])) | {row["owner_id"]}
        for uid in allowed:
            member = channel.guild.get_member(uid)
            if member is None:
                try:
                    member = await channel.guild.fetch_member(uid)
                except discord.HTTPException:
                    continue
            baseline.setdefault(_key(member), channel.overwrites_for(member).connect)
            overwrite = channel.overwrites_for(member)
            overwrite.connect = True
            await channel.set_permissions(member, overwrite=overwrite, reason="Temporary VC allow member")
        self.db.update(channel.id, baseline_json=json.dumps(baseline))

    async def _delete_after(self, cid: int):
        try:
            await asyncio.sleep(EMPTY_DELETE_SECONDS)
            row = self.db.get(cid)
            if row is None:
                return
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(cid) if guild else None
            if isinstance(channel, discord.VoiceChannel) and _humans(channel):
                return
            if isinstance(channel, discord.VoiceChannel):
                await channel.delete(reason="Temporary VC empty for 30 seconds")
            self.db.delete(cid)
            if guild:
                await self.audit(guild, f"VC自動削除: {cid}")
        except asyncio.CancelledError:
            raise
        except discord.HTTPException:
            LOG.exception("VC削除失敗: %s", cid)
        finally:
            self.delete_tasks.pop(cid, None)

    def schedule_delete(self, cid: int):
        task = self.delete_tasks.get(cid)
        if task and not task.done():
            return
        self.delete_tasks[cid] = asyncio.create_task(self._delete_after(cid))

    def cancel_delete(self, cid: int):
        task = self.delete_tasks.pop(cid, None)
        if task and not task.done():
            task.cancel()

    async def _transfer_if_missing(self, channel: discord.VoiceChannel):
        row = self.db.get(channel.id)
        if not row:
            return
        members = _humans(channel)
        if not members or any(m.id == row["owner_id"] for m in members):
            return
        times = json.loads(row["join_json"])
        members.sort(key=lambda m: (float(times.get(str(m.id), float("inf"))), m.id))
        self.db.update(channel.id, owner_id=members[0].id)
        await self.reapply_lock(channel)
        await self.refresh(channel.id)
        await self.audit(channel.guild, f"VCオーナー自動譲渡: {channel.id} -> {members[0].id}")

    async def _notify_failure(self, member: discord.Member, previous: Optional[discord.VoiceChannel], reason: str):
        try:
            if previous and member.voice and member.voice.channel:
                await member.move_to(previous, reason="Temporary VC creation denied")
            elif member.voice and member.voice.channel:
                await member.move_to(None, reason="Temporary VC creation denied")
        except discord.HTTPException:
            LOG.warning("VC作成失敗後の戻し処理に失敗 %s", member.id)
        try:
            await member.send(f"使い捨てVCを作成できませんでした: {reason}")
        except discord.HTTPException:
            pass

    async def _create_room(self, member: discord.Member, trigger: discord.VoiceChannel, previous: Optional[discord.VoiceChannel]):
        guild = trigger.guild
        lock = self.create_lock.setdefault(guild.id, asyncio.Lock())
        async with lock:
            if not member.voice or member.voice.channel != trigger:
                return
            now = time.monotonic()
            cooldown_key = (guild.id, member.id)
            fail = None
            if self.db.owned(guild.id, member.id):
                fail = "すでにVCを所有しています"
            elif self.cooldowns.get(cooldown_key, 0) > now:
                fail = "作成クールダウン中です"
            elif not trigger.category:
                fail = "作成用VCにカテゴリが設定されていません"
            elif self.db.by_category(guild.id, trigger.category_id) >= MAX_PER_CATEGORY:
                fail = "このカテゴリは10部屋の上限に達しています"
            if fail:
                await self._notify_failure(member, previous, fail)
                return
            channel = None
            try:
                nickname = member.display_name.strip()[:35] or "ユーザー"
                # カテゴリ権限を継承したVCを作成。既存VCは絶対に削除しない
                channel = await guild.create_voice_channel(
                    name=f"🎮 {nickname}の部屋"[:50],
                    category=trigger.category,
                    overwrites=trigger.category.overwrites,
                    user_limit=0,
                    reason=f"Temporary VC created by {member.id}",
                )
                self.db.create(channel, member)
                await member.move_to(channel, reason="Temporary VC join")
                self.cooldowns[cooldown_key] = time.monotonic() + COOLDOWN_SECONDS
                await self.refresh(channel.id)
                await self.audit(guild, f"VC生成: {channel.id} オーナー={member.id}")
                if not _humans(channel):
                    self.schedule_delete(channel.id)
            except discord.HTTPException:
                LOG.exception("VC生成・移動失敗: %s", member.id)
                if channel:
                    try:
                        if not _humans(channel):
                            await channel.delete(reason="Temporary VC failed to initialize")
                            self.db.delete(channel.id)
                    except discord.HTTPException:
                        LOG.exception("失敗VCの掃除に失敗 %s", channel.id)
                await self._notify_failure(member, previous, "Botの権限またはDiscord APIのエラー")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.recovered:
            return
        self.recovered = True
        for row in self.db.all():
            guild = self.bot.get_guild(row["guild_id"])
            channel = guild.get_channel(row["channel_id"]) if guild else None
            if not isinstance(channel, discord.VoiceChannel):
                if guild:
                    self.db.delete(row["channel_id"])
                continue
            try:
                # 再起動中の入退室を補正し、在室時間不明者は後回しにする
                joins = json.loads(row["join_json"])
                joins = {str(m.id): joins.get(str(m.id), time.time()) for m in _humans(channel)}
                self.db.update(channel.id, join_json=json.dumps(joins))
                if not _humans(channel):
                    self.schedule_delete(channel.id)
                else:
                    await self._transfer_if_missing(channel)
                    await self.refresh(channel.id, reinstall=True)
            except Exception:
                LOG.exception("VC起動時復旧失敗: %s", row["channel_id"])

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return
        old, new = before.channel, after.channel
        if isinstance(old, discord.VoiceChannel) and self.db.get(old.id):
            row = self.db.get(old.id)
            joins = json.loads(row["join_json"])
            joins.pop(str(member.id), None)
            self.db.update(old.id, join_json=json.dumps(joins))
            try:
                if not _humans(old):
                    self.schedule_delete(old.id)
                elif member.id == row["owner_id"]:
                    await self._transfer_if_missing(old)
                await self.refresh(old.id)
            except discord.HTTPException:
                LOG.exception("退室処理失敗: %s", old.id)
        if isinstance(new, discord.VoiceChannel) and self.db.get(new.id):
            self.cancel_delete(new.id)
            row = self.db.get(new.id)
            joins = json.loads(row["join_json"])
            joins[str(member.id)] = time.time()
            self.db.update(new.id, join_json=json.dumps(joins))
            try:
                await self.refresh(new.id)
            except discord.HTTPException:
                LOG.exception("入室処理失敗: %s", new.id)
        if isinstance(new, discord.VoiceChannel) and new.id in self.triggers:
            previous = old if isinstance(old, discord.VoiceChannel) else None
            await self._create_room(member, new, previous)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if self.db.get(channel.id):
            self.cancel_delete(channel.id)
            self.db.delete(channel.id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        row = self.db.get(payload.channel_id)
        if row and row["panel_message_id"] == payload.message_id:
            self.db.update(payload.channel_id, panel_message_id=None)
            await self.refresh(payload.channel_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceManager(bot))
