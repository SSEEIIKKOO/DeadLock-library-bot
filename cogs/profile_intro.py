from utils.panel_reinstall import remember, tracked
from utils.deployment_scope import is_main_guild
import asyncio
import os
import sqlite3
import json
import io
import gzip
import logging
import discord
from discord import app_commands
from discord.ext import commands
import config


DB_FILE = "data/library.db"
LOG = logging.getLogger(__name__)
PROFILE_ADDRESS_CHANNEL_ID = int(os.getenv("PROFILE_ADDRESS_CHANNEL_ID", "0") or 0)
PROFILE_STORAGE_CHANNEL_ID = int(os.getenv("PROFILE_STORAGE_CHANNEL_ID", "0") or 0)
_PROFILE_LOCKS: dict[int, asyncio.Lock] = {}
STEAM_ID_64_BASE = 76561197960265728


def normalize_friend_code(raw_input: str) -> int | None:
    """フレンドコード（または17桁SteamID64）を32ビットのaccount_idに変換する"""
    digits = "".join(ch for ch in raw_input.strip() if ch.isdigit())
    if not digits:
        return None
    num = int(digits)
    if num > STEAM_ID_64_BASE:
        num -= STEAM_ID_64_BASE
    return num if num > 0 else None


def init_db():
    """データベースとテーブルがなければ自動作成する"""
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                discord_id INTEGER PRIMARY KEY,
                account_id INTEGER UNIQUE,
                call_name TEXT NOT NULL,
                steam_name TEXT NOT NULL,
                old_rank TEXT,
                current_rank TEXT,
                used_heroes TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Existing installations can upgrade without deleting library.db.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(players)")}
        if "comment" not in columns:
            conn.execute("ALTER TABLE players ADD COLUMN comment TEXT DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS profile_messages (
            discord_id INTEGER PRIMARY KEY,
            address_channel_id INTEGER NOT NULL,
            address_message_id INTEGER NOT NULL,
            archive_channel_id INTEGER,
            archive_message_id INTEGER
        )""")


def load_players() -> dict[str, dict]:
    """match_result.py 連携用：account_id をキーにしたプレイヤー辞書を返す"""
    if not os.path.exists(DB_FILE):
        return {}
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT account_id, discord_id, call_name, steam_name FROM players WHERE account_id IS NOT NULL"
            ).fetchall()
            return {
                str(row["account_id"]): {
                    "discord_id": row["discord_id"],
                    "name": row["call_name"],
                    "steam_name": row["steam_name"],
                }
                for row in rows
            }
    except Exception as e:
        print(f"[ProfileIntroCog] DB読み込みエラー: {e}")
        return {}


def save_player_profile(
    discord_id: int,
    account_id: int | None,
    call_name: str,
    steam_name: str,
    old_rank: str,
    current_rank: str,
    used_heroes: str,
    comment: str = "",
):
    """自己紹介の全プロフィールをSQLiteに保存・上書き更新する"""
    init_db()
    with sqlite3.connect(DB_FILE) as conn:
        # 別の人が同じaccount_idを誤登録していた場合は重複エラーを防ぐため解除
        if account_id is not None:
            conn.execute(
                "UPDATE players SET account_id = NULL WHERE account_id = ? AND discord_id != ?",
                (account_id, discord_id),
            )

        conn.execute(
            """
            INSERT INTO players (discord_id, account_id, call_name, steam_name, old_rank, current_rank, used_heroes, comment, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                account_id = excluded.account_id,
                call_name = excluded.call_name,
                steam_name = excluded.steam_name,
                old_rank = excluded.old_rank,
                current_rank = excluded.current_rank,
                used_heroes = excluded.used_heroes,
                comment = excluded.comment,
                updated_at = CURRENT_TIMESTAMP
            """,
            (discord_id, account_id, call_name, steam_name, old_rank, current_rank, used_heroes, comment),
        )


def _get_profile_record(discord_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        return conn.execute(
            "SELECT address_channel_id,address_message_id,archive_channel_id,archive_message_id FROM profile_messages WHERE discord_id=?",
            (discord_id,),
        ).fetchone()


def _put_profile_record(discord_id: int, address_channel_id: int, address_message_id: int, archive_channel_id=None, archive_message_id=None):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""INSERT INTO profile_messages VALUES (?,?,?,?,?)
           ON CONFLICT(discord_id) DO UPDATE SET
             address_channel_id=excluded.address_channel_id,
             address_message_id=excluded.address_message_id,
             archive_channel_id=COALESCE(excluded.archive_channel_id,profile_messages.archive_channel_id),
             archive_message_id=COALESCE(excluded.archive_message_id,profile_messages.archive_message_id)""",
            (discord_id,address_channel_id,address_message_id,archive_channel_id,archive_message_id))


def _export_profile(discord_id: int) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM players WHERE discord_id=?", (discord_id,)).fetchone()
        return dict(row) if row else None


async def _resolve_text_channel(bot, channel_id: int):
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


async def publish_profile(bot, guild_id: int, user: discord.abc.User, embed: discord.Embed):
    """Post once, then edit that same bot-authored profile on subsequent updates."""
    address = await _resolve_text_channel(bot, PROFILE_ADDRESS_CHANNEL_ID)
    if not address or address.guild.id != guild_id:
        raise ValueError("PROFILE_ADDRESS_CHANNEL_ID がメインサーバーの有効なテキストチャンネルを指していません")
    existing = await asyncio.to_thread(_get_profile_record, user.id)
    if existing and existing[0] == address.id:
        try:
            msg = await address.fetch_message(existing[1])
            if msg.author.id == bot.user.id:
                await msg.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return msg, False
        except discord.NotFound:
            pass
    msg = await address.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    await asyncio.to_thread(_put_profile_record, user.id, address.id, msg.id)
    return msg, True


async def archive_profile(bot, discord_id: int, source_guild_id: int):
    """Store latest profile snapshot in the library's restricted Discord storage channel."""
    storage = await _resolve_text_channel(bot, PROFILE_STORAGE_CHANNEL_ID)
    if not storage:
        raise ValueError("PROFILE_STORAGE_CHANNEL_ID の保管先が見つかりません")
    if storage.guild.id == source_guild_id:
        raise ValueError("保管先はメインサーバーではなく、図書館サーバーの非公開チャンネルを指定してください")
    row = await asyncio.to_thread(_export_profile, discord_id)
    pointer = await asyncio.to_thread(_get_profile_record, discord_id)
    if not row or not pointer:
        raise ValueError("保存するプロフィール情報がありません")
    payload = {"schema_version": 1, "profile": row, "address_channel_id":pointer[0], "address_message_id":pointer[1]}
    file_data = gzip.compress(json.dumps(payload,ensure_ascii=False).encode("utf-8"))
    uploaded = await storage.send(
        content=f"Profile backup | discord_id={discord_id}",
        file=discord.File(io.BytesIO(file_data), filename=f"profile_{discord_id}.json.gz"),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    await asyncio.to_thread(_put_profile_record, discord_id, pointer[0], pointer[1], storage.id, uploaded.id)
    if pointer[2] == storage.id and pointer[3] and pointer[3] != uploaded.id:
        try:
            old = await storage.fetch_message(pointer[3])
            if old.author.id == bot.user.id:
                await old.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            LOG.warning("Could not delete previous profile backup for %d",discord_id)
    return uploaded


# ==========================================
# Step 2: 自己紹介テキスト入力モーダル（ちょうど5項目）
# ==========================================
class IntroModal(discord.ui.Modal, title="メンバー自己紹介フォーム"):
    steam_name = discord.ui.TextInput(
        label="プレイヤー名 (Steam名)（必須）",
        placeholder="例：SEIKO",
        max_length=40,
        required=True,
    )
    call_name = discord.ui.TextInput(
        label="呼んでほしい名前（必須）",
        placeholder="例：せーこ",
        max_length=40,
        required=True,
    )
    friend_code = discord.ui.TextInput(
        label="Steamフレンドコード（任意・リザルト連携用）",
        placeholder="例：1533263311",
        max_length=25,
        required=False,
    )
    used_heroes = discord.ui.TextInput(
        label="使用キャラ（必須）",
        placeholder="例：ペイジ、ソロモン、レム",
        max_length=100,
        required=True,
    )
    comment = discord.ui.TextInput(
        label="一言（任意）",
        style=discord.TextStyle.paragraph,
        placeholder="例：宜しくお願いします！サポート大好きです！",
        max_length=500,
        required=False,
    )

    def __init__(self, old_rank: str, current_rank: str, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.old_rank = old_rank
        self.current_rank = current_rank

    async def on_submit(self, interaction: discord.Interaction):
        raw_code = self.friend_code.value.strip() if self.friend_code.value else ""
        account_id = normalize_friend_code(raw_code) if raw_code else None

        if raw_code and account_id is None:
            await interaction.response.send_message(
                "❌ Steamフレンドコードは**半角数字**で入力してください。（もう一度パネルのボタンからやり直してください）",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        steam_name_val = self.steam_name.value.strip()
        call_name_val = self.call_name.value.strip()
        heroes_val = self.used_heroes.value.strip()
        comment_val = self.comment.value.strip() if self.comment.value else "よろしくお願いします！"

        embed = discord.Embed(
            title=f"👋 自己紹介：{call_name_val}",
            description=f"**一言**\n{comment_val}",
            color=discord.Color.teal(),
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="🎮 プレイヤー名 (Steam名)", value=steam_name_val, inline=True)
        embed.add_field(name="🗣️ 呼んでほしい名前", value=call_name_val, inline=True)
        embed.add_field(name="🔗 Steamフレンドコード", value=f"`{account_id}`" if account_id is not None else "未登録", inline=True)
        embed.add_field(name="🏅 旧ランク", value=self.old_rank, inline=True)
        embed.add_field(name="🏆 現ランク", value=self.current_rank, inline=True)
        embed.add_field(name="⚔️ 使用キャラ", value=heroes_val, inline=False)
        embed.set_footer(text=f"User ID: {interaction.user.id} | 再登録時はこの投稿を更新")

        # Guard against simultaneous form submissions creating duplicate address cards.
        lock = _PROFILE_LOCKS.setdefault(interaction.user.id, asyncio.Lock())
        async with lock:
            try:
                published, created = await publish_profile(self.bot, interaction.guild.id, interaction.user, embed)
            except (discord.HTTPException, ValueError) as exc:
                LOG.exception("Profile publish failure: %s", exc)
                await interaction.followup.send(
                    f"❌ アドレス帳に登録できませんでした。設定・権限を確認してください。\n`{type(exc).__name__}`",
                    ephemeral=True,
                )
                return

            # Only update local data after a successful public post/edit.
            await asyncio.to_thread(save_player_profile,
                interaction.user.id, account_id, call_name_val, steam_name_val,
                self.old_rank, self.current_rank, heroes_val, comment_val)
            archived = True
            try:
                await archive_profile(self.bot, interaction.user.id, interaction.guild.id)
            except (discord.HTTPException, ValueError) as exc:
                archived = False
                LOG.exception("Profile archive failure: %s", exc)

        msg = ("✅ アドレス帳に自己紹介を新規登録しました！" if created
               else "✅ アドレス帳の自己紹介を更新しました！")
        msg += f"\n🔗 {published.jump_url}"
        if account_id is not None:
            msg += f"\nSteamアカウント `{account_id}` を紐付けました。"
        if not archived:
            msg += "\n⚠️ 図書館の非公開保管庫へのバックアップに失敗しました。管理者へお知らせください。"
        await interaction.followup.send(msg, ephemeral=True)


# ==========================================
# Step 1: 旧ランク・現ランク選択ドロップダウン UI
# ==========================================
def build_rank_options() -> list[discord.SelectOption]:
    options = [
        discord.SelectOption(label="未ランク / 未プレイ / 非公開", value="未ランク・非公開"),
    ]
    for rank in config.DEADLOCK_RANKS:
        options.append(discord.SelectOption(label=rank, value=rank))
    return options[:25]


class OldRankSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="① 旧ランクを選択してください...",
            min_values=1,
            max_values=1,
            options=build_rank_options(),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: IntroRankSelectView = self.view
        view.old_rank = self.values[0]
        await interaction.response.defer()


class CurrentRankSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="② 現ランクを選択してください...",
            min_values=1,
            max_values=1,
            options=build_rank_options(),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: IntroRankSelectView = self.view
        view.current_rank = self.values[0]
        await interaction.response.defer()


class IntroRankSelectView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=180)
        self.bot = bot
        self.old_rank: str | None = None
        self.current_rank: str | None = None

        self.add_item(OldRankSelect())
        self.add_item(CurrentRankSelect())

    @discord.ui.button(label="次へ（プロフィール詳細を入力）", style=discord.ButtonStyle.primary, row=2)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        old_val = self.old_rank if self.old_rank else "未選択"
        cur_val = self.current_rank if self.current_rank else "未選択"
        await interaction.response.send_modal(IntroModal(old_val, cur_val, self.bot))


# ==========================================
# 常設パネル用の永続View
# ==========================================
class IntroPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📝 自己紹介を書く（ID連携）",
        style=discord.ButtonStyle.primary,
        custom_id="profile_intro:open_modal",
    )
    async def open_intro_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_main_guild(interaction.guild_id):
            await interaction.response.send_message("本番メインサーバー専用の操作です。", ephemeral=True)
            return
        record = tracked("intro", interaction.guild_id) if interaction.guild_id else None
        if record and (interaction.message is None or interaction.message.id != record[1]):
            await interaction.response.send_message("このパネルは更新済みです。新しい自己紹介パネルをご利用ください。",ephemeral=True)
            return
        await interaction.response.send_message(
            "まず**旧ランク**と**現ランク**をドロップダウンから選んで「次へ」を押してください。",
            view=IntroRankSelectView(interaction.client),
            ephemeral=True,
        )


# ==========================================
# Cog クラス本体
# ==========================================
class ProfileIntroCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    @app_commands.command(name="profile_restore", description="図書館の非公開保管庫から自己紹介データと投稿IDを復元")
    @app_commands.default_permissions(administrator=True)
    async def profile_restore(self, interaction: discord.Interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.administrator or not is_main_guild(interaction.guild_id):
            await interaction.response.send_message("管理者のみ実行できます。", ephemeral=True)
            return
        storage = await _resolve_text_channel(self.bot, PROFILE_STORAGE_CHANNEL_ID)
        if storage is None or storage.guild.id == interaction.guild.id:
            await interaction.response.send_message("保管先を図書館サーバーに設定してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        restored = 0
        skipped = 0
        # The channel should contain one newest backup per user; cap the scan.
        try:
            async for msg in storage.history(limit=5000):
                if msg.author.id != self.bot.user.id:
                    continue
                files = [a for a in msg.attachments if a.filename.startswith("profile_") and a.filename.endswith(".json.gz")]
                if not files:
                    continue
                try:
                    raw = await files[0].read()
                    if len(raw) > 1024 * 1024:
                        raise ValueError("Unexpectedly large attachment")
                    item = json.loads(gzip.decompress(raw))
                    if item.get("schema_version") != 1:
                        raise ValueError("Unsupported version")
                    profile = item["profile"]
                    discord_id = int(profile["discord_id"])
                    if discord_id <= 0 or discord_id != int(files[0].filename.removeprefix("profile_").removesuffix(".json.gz")):
                        raise ValueError("Profile ID mismatch")
                    await asyncio.to_thread(save_player_profile,
                        discord_id,profile.get("account_id"),profile["call_name"],profile["steam_name"],
                        profile.get("old_rank") or "未選択", profile.get("current_rank") or "未選択",
                        profile.get("used_heroes") or "",profile.get("comment") or "")
                    await asyncio.to_thread(_put_profile_record,discord_id,
                        int(item["address_channel_id"]),int(item["address_message_id"]),storage.id,msg.id)
                    restored += 1
                except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                    skipped += 1
                    LOG.exception("Invalid profile backup message %d", msg.id)
        except (discord.HTTPException, discord.Forbidden):
            await interaction.followup.send(f"⚠️ 保管庫の読み取りに失敗しました。復元件数: {restored}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ 復元完了: {restored} 件（読み込み不可: {skipped} 件）。アドレス帳には新規投稿しません。", ephemeral=True)

    @app_commands.command(name="setup_intro", description="このチャンネルに自己紹介＆SteamID登録パネルを設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_intro(self, interaction: discord.Interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.administrator or not is_main_guild(interaction.guild_id):
            await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
            return
        address = await _resolve_text_channel(self.bot, PROFILE_ADDRESS_CHANNEL_ID)
        storage = await _resolve_text_channel(self.bot, PROFILE_STORAGE_CHANNEL_ID)
        if (not address or address.guild.id != interaction.guild.id or not storage
                or storage.guild.id == interaction.guild.id or interaction.channel_id == address.id):
            await interaction.response.send_message(
                "❌ チャンネル設定を確認してください。入力パネルとアドレス帳は別チャンネル、非公開保管庫は別サーバーに指定してください。",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="🪪 メンバー自己紹介 ＆ SteamID連携",
            description=(
                "下のボタンを押すと、ランク選択のあとに自己紹介フォームが開きます！\n\n"
                "✅ 入力した自己紹介は、メインサーバーのアドレス帳に公開されます。\n"
                "🔒 データのバックアップは図書館サーバー内の管理者専用チャンネルへ保存されます。\n\n"
                "📋 **入力する内容**\n"
                "・旧ランク / 現ランク（ドロップダウン選択）\n"
                "・プレイヤー名（Steam名）\n"
                "・呼んでほしい名前\n"
                "・Steamフレンドコード（任意・`/match` のリザルトに名前が出ます）\n"
                "・使用キャラ / 一言"
            ),
            color=discord.Color.teal(),
        )

        await interaction.response.defer(ephemeral=True)
        try:
            sent = await interaction.channel.send(embed=embed, view=IntroPanelView())
        except discord.Forbidden:
            await interaction.followup.send("❌ このチャンネルへアクセス・投稿できません。Botの権限を確認してください。", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ パネル設置失敗（Discord HTTP {exc.status}）。ログをご確認ください。", ephemeral=True)
            return
        await asyncio.to_thread(remember, "intro", interaction.guild.id, interaction.channel.id, sent.id)
        await interaction.followup.send("✅ プロフィール入力パネルを設置しました。", ephemeral=True)



async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileIntroCog(bot))
