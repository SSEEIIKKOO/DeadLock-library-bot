from utils.panel_reinstall import remember, tracked
import asyncio
import os
import sqlite3
import discord
from discord import app_commands
from discord.ext import commands
import config


DB_FILE = "data/library.db"
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
            INSERT INTO players (discord_id, account_id, call_name, steam_name, old_rank, current_rank, used_heroes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                account_id = excluded.account_id,
                call_name = excluded.call_name,
                steam_name = excluded.steam_name,
                old_rank = excluded.old_rank,
                current_rank = excluded.current_rank,
                used_heroes = excluded.used_heroes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (discord_id, account_id, call_name, steam_name, old_rank, current_rank, used_heroes),
        )


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

    def __init__(self, old_rank: str, current_rank: str):
        super().__init__()
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

        # SQLiteデータベースへ全プロフィールを保存
        save_player_profile(
            discord_id=interaction.user.id,
            account_id=account_id,
            call_name=call_name_val,
            steam_name=steam_name_val,
            old_rank=self.old_rank,
            current_rank=self.current_rank,
            used_heroes=heroes_val,
        )

        # 自己紹介カードを作成してチャンネルへ投稿
        embed = discord.Embed(
            title=f"👋 自己紹介：{call_name_val}",
            description=f"**一言**\n{comment_val}",
            color=discord.Color.teal(),
        )
        embed.set_author(
            name=f"{interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.add_field(name="🎮 プレイヤー名 (Steam名)", value=steam_name_val, inline=True)
        embed.add_field(name="🗣️ 呼んでほしい名前", value=call_name_val, inline=True)
        embed.add_field(
            name="🔗 Steamフレンドコード",
            value=f"`{account_id}`" if account_id is not None else "未登録",
            inline=True,
        )
        embed.add_field(name="🏅 旧ランク", value=self.old_rank, inline=True)
        embed.add_field(name="🏆 現ランク", value=self.current_rank, inline=True)
        embed.add_field(name="⚔️ 使用キャラ", value=heroes_val, inline=False)

        await interaction.channel.send(embed=embed)

        msg = "✅ 自己紹介をデータベースに保存し、投稿しました！"
        if account_id is not None:
            msg += f"\n🔗 フレンドコード `{account_id}` を連携したため、今後 `/match` のリザルトに「**{call_name_val}**」と表示されます。"

        await interaction.followup.send(msg, ephemeral=True)
        await asyncio.sleep(10)
        await interaction.delete_original_response()


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
    def __init__(self):
        super().__init__(timeout=180)
        self.old_rank: str | None = None
        self.current_rank: str | None = None

        self.add_item(OldRankSelect())
        self.add_item(CurrentRankSelect())

    @discord.ui.button(label="次へ（プロフィール詳細を入力）", style=discord.ButtonStyle.primary, row=2)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        old_val = self.old_rank if self.old_rank else "未選択"
        cur_val = self.current_rank if self.current_rank else "未選択"
        await interaction.response.send_modal(IntroModal(old_val, cur_val))


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
        record = tracked("intro", interaction.guild_id) if interaction.guild_id else None
        if record and (interaction.message is None or interaction.message.id != record[1]):
            await interaction.response.send_message("このパネルは更新済みです。新しい自己紹介パネルをご利用ください。",ephemeral=True)
            return
        await interaction.response.send_message(
            "まず**旧ランク**と**現ランク**をドロップダウンから選んで「次へ」を押してください。",
            view=IntroRankSelectView(),
            ephemeral=True,
        )


# ==========================================
# Cog クラス本体
# ==========================================
class ProfileIntroCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

    @app_commands.command(name="setup_intro", description="このチャンネルに自己紹介＆SteamID登録パネルを設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_intro(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🪪 メンバー自己紹介 ＆ SteamID連携",
            description=(
                "下のボタンを押すと、ランク選択のあとに自己紹介フォームが開きます！\n\n"
                "📋 **入力する内容**\n"
                "・旧ランク / 現ランク（ドロップダウン選択）\n"
                "・プレイヤー名（Steam名）\n"
                "・呼んでほしい名前\n"
                "・Steamフレンドコード（任意・`/match` のリザルトに名前が出ます）\n"
                "・使用キャラ / 一言"
            ),
            color=discord.Color.teal(),
        )

        await interaction.response.send_message("✅ 自己紹介パネルを設置しました！（5秒後に消えます）", ephemeral=True)
        sent = await interaction.channel.send(embed=embed, view=IntroPanelView())
        await asyncio.to_thread(remember, "intro", interaction.guild.id, interaction.channel.id, sent.id)

        await asyncio.sleep(5)
        await interaction.delete_original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(ProfileIntroCog(bot))
