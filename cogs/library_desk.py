import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from cogs.build_post import HeroPaginationView
from cogs.build_search import SearchHeroPaginationView


class LibraryDeskView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📖 ビルドを寄贈（登録）する",
        style=discord.ButtonStyle.primary,
        custom_id="library_desk:post_build",
    )
    async def post_build_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        build_cog = interaction.client.get_cog("BuildPostCog")
        if not build_cog:
            await interaction.response.send_message(
                "❌ システムエラー：ビルド登録機能が読み込まれていません。",
                ephemeral=True,
            )
            return

        if not build_cog.hero_forums:
            build_cog.refresh_hero_forums()

        if not build_cog.hero_forums:
            await interaction.response.send_message(
                "❌ 図書館のヒーロー書架が見つかりません。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "ビルドを登録する**ヒーロー（カテゴリ）**を選択してください。",
            view=HeroPaginationView(build_cog.hero_forums, page=0),
            ephemeral=True,
        )

    @discord.ui.button(
        label="🔍 ビルドを探す",
        style=discord.ButtonStyle.success,
        custom_id="library_desk:search_build",
    )
    async def search_build_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        build_cog = interaction.client.get_cog("BuildPostCog")
        if not build_cog:
            await interaction.response.send_message(
                "❌ システムエラー：ビルド管理機能が読み込まれていません。",
                ephemeral=True,
            )
            return

        if not build_cog.hero_forums:
            build_cog.refresh_hero_forums()

        if not build_cog.hero_forums:
            await interaction.response.send_message(
                "❌ 図書館のヒーロー書架が見つかりません。管理者に連絡してください。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🔍 ビルドを探したい**ヒーロー**を選択してください。",
            view=SearchHeroPaginationView(build_cog.hero_forums, page=0),
            ephemeral=True,
        )


class LibraryDeskCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup_desk", description="このチャンネルにビルド受付カウンター（常設パネル）を設置します")
    @app_commands.default_permissions(administrator=True)
    async def setup_desk(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📚 Deadlock Library 受付カウンター",
            description=(
                "愛用ビルドの寄贈（登録）や、図書館に眠るビルドの検索ができます！\n\n"
                "📖 **ビルドを寄贈する**：ヒーロー・タグ・ランクを選んで6桁コードを登録\n"
                "🔍 **ビルドを探す**：ヒーローやタグから登録済みビルドを検索して表示"
            ),
            color=discord.Color.blue(),
        )
        embed.set_footer(text="※ ボタンを押した本人にのみ操作画面が表示されます")

        await interaction.response.send_message("✅ 受付カウンターを設置しました！（5秒後に消えます）", ephemeral=True)
        await interaction.channel.send(embed=embed, view=LibraryDeskView())

        await asyncio.sleep(5)
        await interaction.delete_original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(LibraryDeskCog(bot))
