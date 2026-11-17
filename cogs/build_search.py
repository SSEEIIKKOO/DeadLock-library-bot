import re
import discord
from discord import app_commands
from discord.ext import commands
import config


# ==========================================
# Step 2: タグ絞り込み ＆ 検索結果表示 UI
# ==========================================
class SearchTagSelect(discord.ui.Select):
    def __init__(self, forum: discord.ForumChannel):
        self.forum = forum
        options = [
            discord.SelectOption(
                label="すべてのタグ（絞り込みなし）",
                value="ALL",
                description="このヒーローの全ビルドを新しい順に表示します",
                emoji="📚",
            )
        ] + [
            discord.SelectOption(
                label=tag.name,
                value=str(tag.id),
                emoji=tag.emoji,
            )
            for tag in forum.available_tags[:24]
        ]

        super().__init__(
            placeholder="絞り込むタグを選択してください...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        selected_val = self.values[0]
        target_tag_id = None if selected_val == "ALL" else int(selected_val)

        # アクティブなスレッド ＋ アーカイブ済みの過去スレッドを両方取得
        threads: list[discord.Thread] = list(self.forum.threads)
        try:
            async for archived_thread in self.forum.archived_threads(limit=50):
                if archived_thread.id not in {t.id for t in threads}:
                    threads.append(archived_thread)
        except discord.Forbidden:
            pass  # アーカイブ閲覧権限がない場合はアクティブスレッドのみ対象

        # タグでフィルタリング
        if target_tag_id is not None:
            threads = [
                t for t in threads if any(tag.id == target_tag_id for tag in t.applied_tags)
            ]

        # 新しい順にソート（作成日時降順）
        threads.sort(key=lambda t: t.created_at or discord.utils.utcnow(), reverse=True)

        if not threads:
            await interaction.edit_original_response(
                content=f"📭 **{self.forum.name}** の書架には、該当するビルドがまだ登録されていません。",
                view=None,
            )
            return

        # 検索結果をEmbedカードに整形（最大10件表示）
        tag_label = (
            "すべて"
            if target_tag_id is None
            else next((t.name for t in self.forum.available_tags if t.id == target_tag_id), "指定タグ")
        )

        embed = discord.Embed(
            title=f"🔍 蔵書検索結果：{self.forum.name}（タグ：{tag_label}）",
            description=f"全 **{len(threads)}** 件中、新しい順の最大10件を表示しています。",
            color=discord.Color.green(),
        )

        for thread in threads[:10]:
            # スレッドタイトル【123456】〇〇 からビルドコードを抽出
            match = re.match(r"【(\d{6})】(.*)", thread.name)
            if match:
                code_str, title_str = match.group(1), match.group(2)
            else:
                code_str, title_str = "不明", thread.name

            applied_names = ", ".join([t.name for t in thread.applied_tags]) or "なし"
            created_str = thread.created_at.strftime("%Y/%m/%d") if thread.created_at else "日付不明"

            embed.add_field(
                name=f"📖 {title_str}",
                value=(
                    f"・**ビルドコード**: `{code_str}`\n"
                    f"・**タグ**: {applied_names} ｜ **登録日**: {created_str}\n"
                    f"・[図書館で解説を見る]({thread.jump_url})"
                ),
                inline=False,
            )

        embed.set_footer(text="※ コード部分をタップ・ドラッグでコピーできます")

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=None,
        )


class SearchTagView(discord.ui.View):
    def __init__(self, forum: discord.ForumChannel):
        super().__init__(timeout=180)
        self.add_item(SearchTagSelect(forum))


# ==========================================
# Step 1: 検索するヒーロー選択 UI（ページ切り替え対応）
# ==========================================
class SearchHeroSelect(discord.ui.Select):
    def __init__(self, forums_page: list[discord.ForumChannel], page: int, total_pages: int):
        self.forums_map = {str(f.id): f for f in forums_page}
        options = [
            discord.SelectOption(label=f.name, value=str(f.id))
            for f in forums_page
        ]
        super().__init__(
            placeholder=f"ビルドを探すヒーローを選択（ページ {page + 1}/{total_pages}）...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        forum = self.forums_map[self.values[0]]
        await interaction.response.edit_message(
            content=f"選択中のヒーロー: **{forum.name}**\n絞り込む傾向タグを選んでください。",
            view=SearchTagView(forum),
        )


class SearchHeroPaginationView(discord.ui.View):
    def __init__(self, forums: list[discord.ForumChannel], page: int = 0):
        super().__init__(timeout=180)
        self.forums = forums
        self.page = page
        self.total_pages = max(1, (len(forums) - 1) // config.PAGE_SIZE + 1)

        start = page * config.PAGE_SIZE
        end = start + config.PAGE_SIZE
        current_slice = forums[start:end]

        self.add_item(SearchHeroSelect(current_slice, page, self.total_pages))

        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀ 前の25キャラ",
                style=discord.ButtonStyle.secondary,
                disabled=(page == 0),
            )
            next_btn = discord.ui.Button(
                label="次の25キャラ ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(page >= self.total_pages - 1),
            )
            prev_btn.callback = self.prev_page
            next_btn.callback = self.next_page
            self.add_item(prev_btn)
            self.add_item(next_btn)

    async def prev_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=SearchHeroPaginationView(self.forums, self.page - 1)
        )

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=SearchHeroPaginationView(self.forums, self.page + 1)
        )


# ==========================================
# Cog クラス本体
# ==========================================
class BuildSearchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="search_build", description="図書館（Library）からヒーローのビルドを検索します")
    async def search_build(self, interaction: discord.Interaction):
        build_cog = self.bot.get_cog("BuildPostCog")
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
                "❌ 図書館のヒーロー書架が見つかりません。設定を確認してください。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🔍 ビルドを探したい**ヒーロー**を選択してください。",
            view=SearchHeroPaginationView(build_cog.hero_forums, page=0),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BuildSearchCog(bot))
