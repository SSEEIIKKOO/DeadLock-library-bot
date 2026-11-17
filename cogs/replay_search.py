"""Search every caption from the replay library; Drive has one asset per match."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from utils.replay_index import search_posts, posts_for_match
from utils.gdrive_uploader import find_saved_replays

log = logging.getLogger(__name__)


class ReplaySearchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='replay', description='図書館のリプレイをマッチIDまたはキャプションから検索します')
    @app_commands.describe(keyword='マッチID、またはキャプションに含まれる言葉')
    async def replay(self, interaction: discord.Interaction, keyword: str):
        keyword = keyword.strip()
        if len(keyword) < 2 or len(keyword) > 100:
            await interaction.response.send_message('❌ 検索語は2～100文字にしてください。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            hits = await asyncio.to_thread(search_posts, keyword, 20)
            # Migration/legacy fallback: files uploaded before the caption index existed.
            drive_files = []
            if keyword.isascii() and keyword.isdecimal():
                drive_files = await asyncio.to_thread(find_saved_replays, keyword)
        except Exception:
            log.exception('Replay search failed: %r', keyword)
            await interaction.followup.send('❌ 検索に失敗しました。Botのログを確認してください。', ephemeral=True)
            return

        if not hits and not drive_files:
            await interaction.followup.send(
                f'📭 「{discord.utils.escape_mentions(keyword)}」に一致するリプレイはありません。',
                ephemeral=True)
            return

        embed = discord.Embed(title='📚 図書館のリプレイ検索',
                              description=f'検索語: {discord.utils.escape_markdown(keyword)}',
                              color=discord.Color.blurple())
        # Up to 10 forum posts displayed; all indexed posts participate in matching.
        for h in hits[:10]:
            caption = h['caption'].replace('\\n', ' ').replace('\n', ' ').strip()
            if len(caption) > 180:
                caption = caption[:179] + '…'
            embed.add_field(
                name=f"Match {h['match_id']} | {h['author_name'][:60]}",
                value=f"{discord.utils.escape_mentions(caption)}\n"
                      f"[図書館の投稿]({h['thread_url']}) ・ [Drive]({h['drive_url']})",
                inline=False)
        if not hits and drive_files:
            entry = drive_files[0]
            link = entry.get('webViewLink') or f"https://drive.google.com/file/d/{entry['id']}/view"
            embed.add_field(name=f'Match {keyword}',
                            value=f'既存のDriveファイル（図書館投稿の索引はありません）\n[Drive]({link})',
                            inline=False)
        if len(hits) > 10:
            embed.set_footer(text=f'{len(hits)}件見つかりました（先頭10件を表示。最大20件を集計）')
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReplaySearchCog(bot))
