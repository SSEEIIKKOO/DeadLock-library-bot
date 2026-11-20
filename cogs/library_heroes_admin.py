"""図書館サーバー専用: 管理者が名前を指定してヒーロー用フォーラムを追加する。"""
import asyncio
import logging
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands

import config

LOGGER = logging.getLogger(__name__)

# (タグ名, Discord Unicode絵文字)。ヒーロー共通の固定テンプレート。
HERO_TAGS = (
    ("武器火力", "🔫"),
    ("スピリット火力", "✨"),
    ("パンチ火力", "👊"),
    ("耐久・タンク", "🛡️"),
    ("回復・支援", "💚"),
    ("アサシン", "🎯"),
    ("阻害・CC", "🧩"),
    ("オブジェクト重視", "🏗️"),
    ("初心者向け", "🌱"),
    ("実験・ネタ", "🧪"),
)


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def clean_name(value: str) -> str:
    name = unicodedata.normalize("NFKC", value).strip()
    if (not name or len(name) > 80 or any(c in name for c in ("\r", "\n", "\t"))
            or "@everyone" in name.casefold() or "@here" in name.casefold()):
        raise ValueError("ヒーロー名は改行・メンションを含まない1〜80文字で入力してください。")
    return name


class LibraryHeroesAdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    @app_commands.command(
        name="library_hero_create",
        description="図書館に10種類の共通タグ付きヒーローフォーラムを1つ作成します",
    )
    @app_commands.describe(name="追加するヒーロー名（そのままフォーラム名になります）")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def library_hero_create(self, interaction: discord.Interaction, name: str):
        if interaction.guild is None or interaction.guild_id != config.TARGET_GUILD_ID:
            await interaction.response.send_message("❌ 図書館サーバーでのみ実行できます。", ephemeral=True)
            return
        try:
            name = clean_name(name)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        category = interaction.guild.get_channel(config.TARGET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "❌ 図書館のビルド書架カテゴリーが見つかりません。TARGET_CATEGORY_IDを確認してください。",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._lock:
            existing = next(
                (ch for ch in category.channels
                 if isinstance(ch, discord.ForumChannel) and normalize_name(ch.name) == normalize_name(name)),
                None,
            )
            if existing is not None:
                await interaction.followup.send(
                    f"ℹ️ **{name}** の書架は既に存在します：{existing.mention}", ephemeral=True,
                )
                return
            if len(category.channels) >= 50:
                await interaction.followup.send(
                    "❌ カテゴリーのチャンネル数が上限（50）に達しています。", ephemeral=True,
                )
                return
            try:
                # Discordのフォーラムタグは名前と絵文字を別々に保持する。
                tags = [discord.ForumTag(name=tag, emoji=emoji) for tag, emoji in HERO_TAGS]
                forum = await interaction.guild.create_forum(
                    name=name,
                    category=category,
                    topic=f"{name} のビルド図書館。ビルドはメインサーバーの受付パネルから登録できます。",
                    available_tags=tags,
                    reason=f"図書館管理者 {interaction.user.id} によるヒーロー書架追加",
                )
            except discord.Forbidden:
                await interaction.followup.send(
                    "❌ フォーラムを作成できません。図書館でのチャンネル管理権限を確認してください。",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as exc:
                LOGGER.warning("Hero forum creation failed: %s", exc)
                await interaction.followup.send(
                    f"❌ Discord APIエラー（HTTP {exc.status}）。チャンネル上限や権限を確認してください。",
                    ephemeral=True,
                )
                return
            # 新規書架をビルド登録画面へ即座に反映する。
            build_cog = self.bot.get_cog("BuildPostCog")
            if build_cog is not None:
                try:
                    build_cog.refresh_hero_forums()
                except Exception:
                    LOGGER.exception("New forum created, but build list refresh failed")
                    await interaction.followup.send(
                        f"✅ フォーラムを作成しました：{forum.mention}\n"
                        "⚠️ ビルド登録画面の更新に失敗しました。`/sync_heroes` を実行してください。",
                        ephemeral=True,
                    )
                    return
        await interaction.followup.send(
            f"✅ **{name}** のフォーラムを作成しました：{forum.mention}\n"
            f"共通タグ **{len(HERO_TAGS)}種類** を設定しました。",
            ephemeral=True,
        )

    @library_hero_create.error
    async def library_hero_create_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            message = "❌ サーバーの管理者権限が必要です。"
        else:
            LOGGER.error("Hero create command failed", exc_info=(type(error), error, error.__traceback__))
            message = "❌ 書架管理コマンドでエラーが発生しました。Botのログを確認してください。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            LOGGER.exception("Could not send hero admin error response")


async def setup(bot: commands.Bot):
    await bot.add_cog(LibraryHeroesAdminCog(bot))
