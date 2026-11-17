import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


DEADLOCK_API_BASE = "https://api.deadlock-api.com"
DEADLOCK_ASSETS_BASE = "https://assets.deadlock-api.com"

DEFAULT_HEADERS = {
    "User-Agent": "DeadlockLibraryBot/1.0 (Discord Bot)",
    "Accept": "application/json",
}


class MatchResultCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.hero_names: dict[int, str] = {}

    async def ensure_hero_names(self, session: aiohttp.ClientSession):
        """ヒーローIDからヒーロー名への変換辞書をAssets APIから取得・キャッシュ"""
        if self.hero_names:
            return
        for endpoint in (f"{DEADLOCK_ASSETS_BASE}/v2/heroes", f"{DEADLOCK_API_BASE}/v1/heroes"):
            try:
                async with session.get(endpoint, headers=DEFAULT_HEADERS, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # リスト形式と辞書形式の両方に対応
                        items = data if isinstance(data, list) else data.values()
                        for hero in items:
                            if not isinstance(hero, dict):
                                continue
                            hero_id = hero.get("id")
                            name = hero.get("name") or hero.get("class_name")
                            if hero_id is not None and name:
                                self.hero_names[int(hero_id)] = str(name)
                        if self.hero_names:
                            return
                    else:
                        print(f"[MatchResultCog] ヒーロー取得ステータス異常 ({endpoint}): {resp.status}")
            except Exception as e:
                print(f"[MatchResultCog] ヒーロー一覧の取得に失敗 ({endpoint}): {e}")

    async def fetch_player_names(self, session: aiohttp.ClientSession, account_ids: list[int]) -> dict[int, str]:
        """account_id のリストからSteamのプレイヤー名（personaname）を一括取得"""
        names_map: dict[int, str] = {}
        valid_ids = [str(aid) for aid in set(account_ids) if aid]
        if not valid_ids:
            return names_map

        params = {"account_ids": ",".join(valid_ids)}
        try:
            async with session.get(
                f"{DEADLOCK_API_BASE}/v1/players/steam",
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=10,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for profile in data:
                        aid = profile.get("account_id")
                        pname = profile.get("personaname") or profile.get("persona_name")
                        if aid is not None and pname:
                            names_map[int(aid)] = str(pname)
        except Exception as e:
            print(f"[MatchResultCog] プレイヤー名の取得に失敗: {e}")

        return names_map

    @app_commands.command(name="match", description="マッチIDからDeadlockの試合リザルトを表示します")
    @app_commands.describe(match_id="試合のマッチID（数字）を入力してください")
    async def match_result(self, interaction: discord.Interaction, match_id: str):
        clean_id = match_id.strip()
        if not clean_id.isdigit():
            await interaction.response.send_message(
                "❌ マッチIDは**半角数字のみ**で入力してください。（例：`12345678`）",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        url = f"{DEADLOCK_API_BASE}/v1/matches/{clean_id}/metadata"

        try:
            async with aiohttp.ClientSession() as session:
                await self.ensure_hero_names(session)

                async with session.get(url, headers=DEFAULT_HEADERS, timeout=15) as resp:
                    if resp.status == 404:
                        await interaction.followup.send(
                            f"📭 マッチID `{clean_id}` のデータが見つかりませんでした。\n"
                            "（※ 試合終了直後はAPIにデータが反映されるまで数分かかる場合があります）"
                        )
                        return
                    if resp.status == 403:
                        err_text = await resp.text()
                        print(f"[MatchResultCog] 403 Forbidden詳細: {err_text[:300]}")
                        await interaction.followup.send(
                            "⚠️ APIサーバーからアクセスが拒否されました（403）。APIキーが必要になったか、一時的な制限の可能性があります。"
                        )
                        return
                    if resp.status in (503, 504):
                        await interaction.followup.send(
                            "⚠️ 現在 Deadlock API サーバーが混雑しています。少し時間を置いてもう一度お試しください。"
                        )
                        return
                    if resp.status != 200:
                        await interaction.followup.send(
                            f"❌ APIエラーが発生しました（ステータスコード: `{resp.status}`）"
                        )
                        return

                    data = await resp.json()

                info = data.get("match_info", data)
                players = info.get("players", [])

                # 参加プレイヤー全員の account_id を集めてプレイヤー名を一括取得
                account_ids = [p.get("account_id") for p in players if p.get("account_id")]
                player_names_map = await self.fetch_player_names(session, account_ids)

        except Exception as e:
            await interaction.followup.send(
                f"❌ 外部APIとの通信中にエラーが発生しました: `{e}`"
            )
            return

        duration_sec = info.get("duration_s", 0)
        minutes, seconds = divmod(int(duration_sec), 60)
        duration_str = f"{minutes}分{seconds:02d}秒"

        winning_team = info.get("winning_team")
        if winning_team == 0:
            winner_str = "🟠 The Hidden King（ヒドゥンキング）勝利"
            embed_color = discord.Color.orange()
        elif winning_team == 1:
            winner_str = "🔵 The Archmother（アーチマザー）勝利"
            embed_color = discord.Color.blue()
        else:
            winner_str = "勝敗データなし"
            embed_color = discord.Color.light_grey()

        team_hidden_king = []
        team_archmother = []

        for p in players:
            hero_id = p.get("hero_id")
            hero_name = self.hero_names.get(int(hero_id), f"Hero#{hero_id}") if hero_id is not None else "Unknown"

            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            assists = p.get("assists", 0)
            net_worth = p.get("net_worth", 0)
            souls_k = f"{net_worth / 1000:.1f}k"

            account_id = p.get("account_id")
            player_name = (
                (player_names_map.get(int(account_id)) if account_id is not None else None)
                or p.get("persona_name")
                or p.get("account_name")
            )
            label = f"**{hero_name}** ({player_name})" if player_name else f"**{hero_name}**"

            line = f"{label}：`{kills}/{deaths}/{assists}` ｜ 💰`{souls_k}`"

            team_id = p.get("team")
            if team_id == 0:
                team_hidden_king.append(line)
            else:
                team_archmother.append(line)

        embed = discord.Embed(
            title=f"⚔️ Deadlock 試合リザルト（Match ID: {clean_id}）",
            description=(
                f"**結果**: {winner_str}\n"
                f"**試合時間**: {duration_str}\n"
                f"🔗 [Deadlock APIで詳細を見る](https://deadlock-api.com{clean_id})"
            ),
            color=embed_color,
        )

        embed.add_field(
            name="🟠 The Hidden King（ヒドゥンキング）",
            value="\n".join(team_hidden_king) if team_hidden_king else "データなし",
            inline=False,
        )
        embed.add_field(
            name="🔵 The Archmother（アーチマザー）",
            value="\n".join(team_archmother) if team_archmother else "データなし",
            inline=False,
        )
        embed.set_footer(text="Powered by deadlock-api.com")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchResultCog(bot))
