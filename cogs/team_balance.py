"""Trial /team_split command. Doesn't alter MMR, VC, or match results."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from utils.team_balancer import balance, parse_players


class TeamSplitModal(discord.ui.Modal, title="CR自動チーム分け・試験版"):
    roster = discord.ui.TextInput(
        label="12人分：名前,CR[,信頼度]（1行につき1人）",
        placeholder="Alice,1250\nBob,1420\nCharlie,1200,75\n...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            players = parse_players(str(self.roster.value))
            result = balance(players)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        def format_team(members):
            return "\n".join(
                f"{i}. {discord.utils.escape_markdown(p.name)} — **{p.cr:,} CR**"
                for i, p in enumerate(sorted(members, key=lambda x: x.cr, reverse=True), 1)
            )

        embed = discord.Embed(
            title="⚖️ CRカスタム自動チーム分け（試験版）",
            description=(
                f"**合計CR差：{result.gap:,}** ｜ 最小可能差：{result.minimum_gap:,}\n"
                f"最小差+60以内の候補：{result.candidates}通り"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name=f"🟠 チームA｜{result.sum_a:,} CR",
            value=format_team(result.team_a), inline=True,
        )
        embed.add_field(
            name=f"🔵 チームB｜{result.sum_b:,} CR",
            value=format_team(result.team_b), inline=True,
        )
        embed.add_field(
            name="補助評価（小さいほど良い）",
            value=(
                f"上位2人の合計差：{result.top_two_gap:,}\n"
                f"CR標準偏差の差：{result.spread_gap:.1f}\n"
                f"低信頼度の偏り：{result.uncertainty_gap}\n"
                f"総合ペナルティ：{result.penalty:.2f}"
            ), inline=False,
        )
        embed.set_footer(text="試験運用：手入力CR。レート更新・VC移動・試合記録は行いません。")
        await interaction.response.send_message(embed=embed)


class TeamBalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="team_split", description="12人のCRを入力して6対6のチーム分けを試します")
    async def team_split(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TeamSplitModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(TeamBalanceCog(bot))
