import asyncio
import re
import discord
from discord import app_commands
from discord.ext import commands
import config


# ==========================================
# Step 3: ビルド情報入力モーダル
# ==========================================
class BuildSubmitModal(discord.ui.Modal, title="Deadlock Library ビルド登録"):
    build_title = discord.ui.TextInput(
        label="ビルドタイトル（必須）",
        placeholder="例：セブン スピリット特化ウルト型",
        max_length=80,
        required=True,
    )
    build_code = discord.ui.TextInput(
        label="ビルドコード（半角数字6桁・必須）",
        placeholder="例：123456",
        min_length=6,
        max_length=6,
        required=True,
    )
    build_description = discord.ui.TextInput(
        label="ビルド説明（任意・立ち回りや強み弱みなど）",
        style=discord.TextStyle.paragraph,
        placeholder="序盤のレーン戦の立ち回り、コアアイテム、強い時間帯・苦手な相手などを自由に書いてください。",
        max_length=1500,
        required=False,
    )

    def __init__(
        self,
        forum: discord.ForumChannel,
        selected_tags: list[discord.ForumTag],
        selected_rank: str | None = None,
    ):
        super().__init__()
        self.forum = forum
        self.selected_tags = selected_tags
        self.selected_rank = selected_rank

    async def on_submit(self, interaction: discord.Interaction):
        code_str = self.build_code.value.strip()

        if not re.fullmatch(r"\d{6}", code_str):
            await interaction.response.send_message(
                "❌ ビルドコードは**半角数字6桁**で入力してください。（最初からやり直してください）",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        thread_title = f"【{code_str}】{self.build_title.value.strip()}"
        desc_text = self.build_description.value.strip() if self.build_description.value else "（説明なし）"
        tag_names = ", ".join([t.name for t in self.selected_tags]) if self.selected_tags else "なし"
        rank_text = self.selected_rank if self.selected_rank else "未指定"

        content = (
            f"**投稿者**: {interaction.user.display_name}\n"
            f"**投稿者ランク**: {rank_text}\n"
            f"**タグ**: {tag_names}\n\n"
            f"### ビルド解説\n"
            f"{desc_text}\n\n"
            f"### ビルドコード（タップ・クリックでコピー）\n"
            f"```\n{code_str}\n```"
        )

        try:
            thread_with_msg = await self.forum.create_thread(
                name=thread_title,
                content=content,
                applied_tags=self.selected_tags,
            )
            created_thread = thread_with_msg.thread

            await interaction.followup.send(
                f"📚 **{self.forum.name}** の書架にビルドを登録しました！\n"
                f"スレッドリンク: {created_thread.jump_url}",
                ephemeral=True,
            )
            # ▼ この2行を追記（30秒後に自動削除）
            await asyncio.sleep(30)
            await interaction.delete_original_response()
            
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 蓄積先フォーラムへの投稿権限がありません。Botのチャンネル権限を確認してください。",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ 登録中にエラーが発生しました: `{e}`",
                ephemeral=True,
            )


# ==========================================
# Step 2: ビルド傾向タグ ＋ 投稿者ランク（任意）選択 UI
# ==========================================
class TagSelect(discord.ui.Select):
    def __init__(self, forum: discord.ForumChannel):
        options = [
            discord.SelectOption(
                label=tag.name,
                value=str(tag.id),
                emoji=tag.emoji,
            )
            for tag in forum.available_tags[:25]
        ]

        super().__init__(
            placeholder="① ビルドの傾向タグを選択（必須・複数可）...",
            min_values=1,
            max_values=min(len(options), 4),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: BuildOptionsView = self.view
        selected_ids = {int(v) for v in self.values}
        view.selected_tags = [t for t in view.forum.available_tags if t.id in selected_ids]
        await interaction.response.defer()


class RankSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label="指定しない（非公開）", value="NONE")] + [
            discord.SelectOption(label=rank, value=rank) for rank in config.DEADLOCK_RANKS
        ]
        super().__init__(
            placeholder="② 投稿者のランクを選択（任意）...",
            min_values=1,
            max_values=1,
            options=options[:25],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: BuildOptionsView = self.view
        val = self.values[0]
        view.selected_rank = None if val == "NONE" else val
        await interaction.response.defer()


class BuildOptionsView(discord.ui.View):
    def __init__(self, forum: discord.ForumChannel):
        super().__init__(timeout=180)
        self.forum = forum
        self.selected_tags: list[discord.ForumTag] = []
        self.selected_rank: str | None = None

        if forum.available_tags:
            self.add_item(TagSelect(forum))

        self.add_item(RankSelect())

    @discord.ui.button(label="次へ（ビルド詳細を入力）", style=discord.ButtonStyle.primary, row=2)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.forum.available_tags and not self.selected_tags:
            await interaction.response.send_message(
                "⚠️ ビルドの傾向タグを1つ以上選択してから「次へ」を押してください。",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            BuildSubmitModal(self.forum, self.selected_tags, self.selected_rank)
        )


# ==========================================
# Step 1: ヒーロー（カテゴリ）選択 UI
# ==========================================
class HeroSelect(discord.ui.Select):
    def __init__(self, forums_page: list[discord.ForumChannel], page: int, total_pages: int):
        self.forums_map = {str(f.id): f for f in forums_page}
        options = [
            discord.SelectOption(label=f.name, value=str(f.id))
            for f in forums_page
        ]
        super().__init__(
            placeholder=f"ヒーローを選択してください（{len(forums_page)}キャラ表示中）...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        forum = self.forums_map[self.values[0]]

        await interaction.response.edit_message(
            content=(
                f"選択中のヒーロー: **{forum.name}**\n"
                "ビルドの傾向タグと、あなたのランク（任意）を選択して「次へ」を押してください。"
            ),
            view=BuildOptionsView(forum),
        )


class HeroPaginationView(discord.ui.View):
    def __init__(self, forums: list[discord.ForumChannel], page: int = 0):
        super().__init__(timeout=180)
        self.forums = forums
        self.page = page
        self.total_pages = max(1, (len(forums) - 1) // config.PAGE_SIZE + 1)

        start = page * config.PAGE_SIZE
        end = start + config.PAGE_SIZE
        current_slice = forums[start:end]

        self.add_item(HeroSelect(current_slice, page, self.total_pages))

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
            view=HeroPaginationView(self.forums, self.page - 1)
        )

    async def next_page(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=HeroPaginationView(self.forums, self.page + 1)
        )


# ==========================================
# Cog クラス本体
# ==========================================
class BuildPostCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.hero_forums: list[discord.ForumChannel] = []

    @commands.Cog.listener()
    async def on_ready(self):
        self.refresh_hero_forums()
        print(f"[BuildPostCog] 取得したヒーローフォーラム数: {len(self.hero_forums)}")

    def refresh_hero_forums(self):
        """蓄積先サーバーからヒーローのフォーラムチャンネル一覧を取得してキャッシュ"""
        guild = self.bot.get_guild(config.TARGET_GUILD_ID)
        if not guild:
            print("エラー: 蓄積先サーバーが見つかりません。TARGET_GUILD_IDを確認してください。")
            return

        forums = []
        for channel in guild.forums:
            if config.TARGET_CATEGORY_ID == 0 or channel.category_id == config.TARGET_CATEGORY_ID:
                forums.append(channel)

        forums.sort(key=lambda c: c.name)

        if config.TEST_HERO_LIMIT > 0:
            forums = forums[:config.TEST_HERO_LIMIT]

        self.hero_forums = forums

    @app_commands.command(name="post_build", description="Deadlockのビルドを図書館（Library）へ登録します")
    async def post_build(self, interaction: discord.Interaction):
        if not self.hero_forums:
            self.refresh_hero_forums()

        if not self.hero_forums:
            await interaction.response.send_message(
                "❌ 蓄積先のヒーローフォーラムが見つかりません。設定を確認してください。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "ビルドを登録する**ヒーロー（カテゴリ）**を選択してください。",
            view=HeroPaginationView(self.hero_forums, page=0),
            ephemeral=True,
        )

    @app_commands.command(name="sync_heroes", description="図書館のヒーローフォーラム一覧を再読み込みします")
    @app_commands.default_permissions(administrator=True)
    async def sync_heroes(self, interaction: discord.Interaction):

        self.refresh_hero_forums()
        await interaction.response.send_message(
            f"🔄 ヒーロー一覧を更新しました。（現在 **{len(self.hero_forums)}** キャラ登録済み）",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BuildPostCog(bot))
