"""CR v0.4: A official rank, B custom-match ledger with decay, C admin adjustments."""
from __future__ import annotations
import asyncio
import logging
import os
import sqlite3
import time
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from utils import cr_store, cr_rating, cr_archive, cr_custom
from utils.team_balancer import Player, balance

LOG=logging.getLogger(__name__)
API='https://api.deadlock-api.com'

# 自己紹介パネルの既存Steam IDを読み込む（既存データは変更しない）。
PROFILE_DB = 'data/library.db'

def lookup_profile_steam(discord_id: int) -> int | None:
    if not os.path.isfile(PROFILE_DB):
        return None
    with sqlite3.connect(f'file:{os.path.abspath(PROFILE_DB)}?mode=ro', uri=True, timeout=10) as db:
        row = db.execute('SELECT account_id FROM players WHERE discord_id=?', (discord_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return cr_rating.steam_to_account_id(str(row[0]))

class CRSteamModal(discord.ui.Modal, title='Steam IDを登録してCRを計算'):
    steam_id = discord.ui.TextInput(
        label='Steamフレンドコード（またはSteamID64）',
        placeholder='フレンドコードの数字、または17桁のSteamID64',
        min_length=1, max_length=25, required=True)

    def __init__(self, manager: 'CRManager'):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        try:
            steam32 = cr_rating.steam_to_account_id(str(self.steam_id.value))
        except ValueError as exc:
            await interaction.response.send_message(f'❌ {exc}', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.manager.show_cr(interaction, entered_steam32=steam32)

class SplitRoster(discord.ui.Modal,title='登録済みCRで6対6チーム分け'):
    roster=discord.ui.TextInput(
        label='12人のDiscordユーザーIDまたはメンション（各行1人）',
        placeholder='123456789012345678\n234567890123456789\n...',
        style=discord.TextStyle.paragraph,max_length=4000,required=True)
    async def on_submit(self,interaction:discord.Interaction):
        values=[x.strip().replace('<@!','').replace('<@','').replace('>','') for x in str(self.roster.value).splitlines() if x.strip()]
        if len(values)!=12 or any(not x.isdecimal() for x in values) or len(set(values))!=12:
            await interaction.response.send_message('❌ 重複のない12人のDiscordユーザーIDを1行ずつ指定してください。',ephemeral=True)
            return
        players=[]
        for value in values:
            did=int(value)
            row=await asyncio.to_thread(cr_store.get,did)
            if row is None:
                await interaction.response.send_message(f'❌ <@{did}> はCR未登録です。',ephemeral=True)
                return
            b = await asyncio.to_thread(cr_store.current_b, did)
            players.append(Player(str(did),round(row['a']+b+row['c']),row['confidence']))
        result=balance(players)
        def fmt(team):
            return '\n'.join(f'<@{p.name}> — **{p.cr:,} CR**' for p in sorted(team,key=lambda p:-p.cr))
        e=discord.Embed(title='⚖️ 登録済みCR・6対6',description=f'合計CR差 **{result.gap:,}** ／ 最小可能差 {result.minimum_gap:,}',color=discord.Color.blurple())
        e.add_field(name=f'🟠 チームA（{result.sum_a:,}）',value=fmt(result.team_a),inline=True)
        e.add_field(name=f'🔵 チームB（{result.sum_b:,}）',value=fmt(result.team_b),inline=True)
        e.set_footer(text='Bは登録済みのカスタム試合履歴から時間減衰を再計算しています。')
        await interaction.response.send_message(embed=e,allowed_mentions=discord.AllowedMentions.none())


class CustomResultModal(discord.ui.Modal, title='カスタム試合の結果を確定'):
    team_a = discord.ui.TextInput(
        label='チームA：DiscordユーザーIDを6人分（各行1人）',
        style=discord.TextStyle.paragraph, required=True, max_length=800,
        placeholder='123456789012345678\n234567890123456789\n...')
    team_b = discord.ui.TextInput(
        label='チームB：DiscordユーザーIDを6人分（各行1人）',
        style=discord.TextStyle.paragraph, required=True, max_length=800,
        placeholder='345678901234567890\n456789012345678901\n...')

    def __init__(self, manager: 'CRManager', match_key: str, winner: str):
        super().__init__()
        self.manager, self.match_key, self.winner = manager, match_key, winner

    @staticmethod
    def parse_ids(raw: str) -> list[int]:
        lines = [line.strip().replace('<@!', '').replace('<@', '').replace('>', '')
                 for line in raw.splitlines() if line.strip()]
        if len(lines) != 6 or any(not x.isdecimal() for x in lines):
            raise ValueError('各チームにDiscordユーザーIDまたはメンションを6行ずつ入力してください。')
        return [int(x) for x in lines]

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            a = self.parse_ids(str(self.team_a.value))
            b = self.parse_ids(str(self.team_b.value))
            if len(set(a+b)) != 12:
                raise ValueError('参加12人に重複があります。')
            # Only a guild admin can confirm results, even after modal opening.
            if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
                raise ValueError('サーバーの管理権限が必要です。')
            result = await self.manager.record_result(self.match_key, self.winner, interaction.user.id, a, b)
            winner_name = 'A' if self.winner == 'A' else 'B'
            lines = [f'<@{did}> `{delta:+.2f}`' for did,delta in sorted(result['deltas'].items(), key=lambda v: -v[1])]
            await interaction.followup.send(
                f'✅ 試合 `{self.match_key}` を記録しました。勝利チーム：**{winner_name}**\n'
                f'勝利側獲得合計：**+{result["pool"]:.2f}** / 敗北側減少合計：**−{result["pool"]:.2f}**\n'
                + '\n'.join(lines) + f'\n保管庫への同期：**{result["saved"]}/12人**'
                + ('\n⚠️ 一部のDiscord保存に失敗しました。ローカルDBは保持しています。`/cr` 等で再同期してください。' if result['saved']<12 else ''),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except (ValueError, sqlite3.Error) as exc:
            await interaction.followup.send(f'❌ 試合の登録に失敗しました: {str(exc)[:200]}', ephemeral=True)
        except Exception:
            LOG.exception('Custom CR result failed')
            await interaction.followup.send('❌ 予期しないエラーです。ログを確認してください。', ephemeral=True)

class CRManager(commands.Cog):
    def __init__(self,bot:commands.Bot):
        self.bot=bot
        self.locks:dict[int,asyncio.Lock]={}
        self.last_api_time=0.0
        self.api_lock=asyncio.Lock()
        self.custom_lock=asyncio.Lock()

    def lock(self,did):
        return self.locks.setdefault(did,asyncio.Lock())

    async def api_json(self,url):
        # Serialized calls, no aggressive retries when rate limited.
        async with self.api_lock:
            wait=max(0,3.0-(time.monotonic()-self.last_api_time))
            if wait: await asyncio.sleep(wait)
            self.last_api_time=time.monotonic()
            timeout=aiohttp.ClientTimeout(total=22)
            async with aiohttp.ClientSession(timeout=timeout,headers={'User-Agent':'DeadlockCRBot/0.1','Accept':'application/json'}) as session:
                async with session.get(url) as resp:
                    if resp.status!=200:
                        raise RuntimeError(f'API HTTP {resp.status} ({"制限中" if resp.status==429 else "取得不可"})')
                    return await resp.json(content_type=None)

    async def persist(self,did):
        row=await asyncio.to_thread(cr_store.get,did)
        hist=await asyncio.to_thread(cr_store.history,did,20)
        try:
            mid=await cr_archive.snapshot(self.bot,row,hist)
            await asyncio.to_thread(cr_store.set_remote,did,mid,None)
            return True,None
        except (discord.HTTPException,discord.Forbidden,RuntimeError,ValueError) as exc:
            LOG.exception('CR remote snapshot failed: %s',did)
            await asyncio.to_thread(cr_store.set_remote,did,None,str(exc)[:250])
            return False,str(exc)[:150]

    async def record_result(self, match_key: str, winner: str, admin_id: int,
                            team_a: list[int], team_b: list[int]):
        async with self.custom_lock:
            ids = team_a + team_b
            rows = []
            b_values = {}
            for did in ids:
                row = await asyncio.to_thread(cr_store.get, did)
                if row is None:
                    raise ValueError(f'<@{did}> はCR未登録です。先に `/cr` を実行してください。')
                rows.append(row)
                b_values[did] = await asyncio.to_thread(cr_store.current_b, did)
            deltas, pool, expected = cr_custom.allocate(rows[:6], rows[6:], winner, b_values)
            await asyncio.to_thread(cr_store.record_custom_match, match_key, winner, admin_id,
                                    team_a, team_b, deltas, time.time())
            saved = 0
            for did in ids:
                # Each participant's snapshot contains their full decay ledger.
                ok, _ = await self.persist(did)
                saved += int(ok)
            return {'deltas': deltas, 'pool': pool, 'expected_a': expected, 'saved': saved}

    @app_commands.command(name='cr_custom_result', description='管理者：6対6カスタムの勝敗をBに登録します')
    @app_commands.describe(match_key='重複しない試合識別子（例: custom-20261119-01）',winner='勝ったチーム')
    @app_commands.choices(winner=[app_commands.Choice(name='チームA',value='A'),
                                  app_commands.Choice(name='チームB',value='B')])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def custom_result(self, interaction: discord.Interaction, match_key: str,
                            winner: app_commands.Choice[str]):
        key = match_key.strip()
        if not 1 <= len(key) <= 80 or any(ch in key for ch in '\n\r'):
            await interaction.response.send_message('❌ 試合IDは1〜80文字で指定してください。', ephemeral=True)
            return
        await interaction.response.send_modal(CustomResultModal(self, key, winner.value))

    @custom_result.error
    async def custom_result_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message('❌ サーバーの管理権限が必要です。', ephemeral=True)
        else:
            LOG.exception('cr_custom_result error: %r', error)

    @app_commands.command(name='cr', description='Steam IDを自動照合し、最新のカスタムレートを表示します')
    async def cr(self, interaction: discord.Interaction):
        # モーダル表示前は defer 不可。既存登録がなければ入力画面を開く。
        did = interaction.user.id
        row = await asyncio.to_thread(cr_store.get, did)
        if row is None:
            try:
                profile_sid = await asyncio.to_thread(lookup_profile_steam, did)
            except (sqlite3.Error, ValueError, OSError) as exc:
                LOG.warning('profile lookup failure for %s: %s', did, exc)
                profile_sid = None
            if profile_sid is None:
                await interaction.response.send_modal(CRSteamModal(self))
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.show_cr(interaction)

    async def show_cr(self, interaction: discord.Interaction, entered_steam32: int | None = None):
        """共通処理: ID登録 -> Aの必要時更新 -> CR表示。通常のCR値は変えない。"""
        did = interaction.user.id
        async with self.lock(did):
            row = await asyncio.to_thread(cr_store.get, did)
            registered_now = False
            warning = None
            if row is None:
                try:
                    steam32 = entered_steam32
                    if steam32 is None:
                        steam32 = await asyncio.to_thread(lookup_profile_steam, did)
                    if steam32 is None:
                        await interaction.followup.send('❌ Steam IDが未登録です。もう一度 `/cr` を実行してください。', ephemeral=True)
                        return
                    row = await asyncio.to_thread(cr_store.register, did, steam32)
                    registered_now = True
                except (ValueError, sqlite3.Error) as exc:
                    LOG.warning('CR registration failed for %s: %s', did, exc)
                    await interaction.followup.send(f'❌ Steam IDの登録に失敗しました: {str(exc)[:160]}', ephemeral=True)
                    return

            last = float(row.get('updated_at') or 0)
            should_update = (not last or time.time() - last >= 86400)
            refreshed = False
            note = None
            if should_update:
                try:
                    sid = int(row['steam32'])
                    rank = await self.api_json(f'{API}/v1/players/{sid}/rank')
                    history = await self.api_json(f'{API}/v1/players/{sid}/match-history')
                    if not isinstance(rank, dict) or not isinstance(history, list):
                        raise ValueError('ランクまたは試合履歴のレスポンス形式が不正です。')
                    a, conf, badge, note = cr_rating.estimate(rank, history, row)
                    if conf > 0:
                        row = await asyncio.to_thread(cr_store.update_rating, did, a, conf, badge, time.time())
                        refreshed = True
                    else:
                        warning = '公式ランク未確定のため暫定評価を表示しています。'
                except Exception as exc:
                    LOG.warning('CR update fallback to local record for %s: %s', did, exc)
                    warning = f'公式戦データを更新できなかったため保存済みのCRを表示します（{str(exc)[:95]}）。'
            # ローカルに変更があった場合だけ、Discordの非公開保管庫へ同期する。
            if registered_now or refreshed or row.get("last_remote_error"):
                saved, error = await self.persist(did)
                if not saved:
                    warning = (warning + '\n' if warning else '') + f'非公開保管庫への同期に失敗: {error}'

            b = await asyncio.to_thread(cr_store.current_b, did)
            total = row['a'] + b + row['c']
            embed = discord.Embed(title=f'📊 {interaction.user.display_name} のカスタムレート',
                                  description=f'## {total:,.1f} CR', color=discord.Color.blue())
            embed.add_field(name='A：公式戦評価', value=f'{row["a"]:,.1f}', inline=True)
            embed.add_field(name='B：カスタム戦績', value=f'{b:+,.1f}', inline=True)
            embed.add_field(name='C：管理者補正', value=f'{row["c"]:+,.1f}', inline=True)
            embed.add_field(name='評価信頼度', value=f'{row["confidence"]}%', inline=True)
            if last := float(row.get('updated_at') or 0):
                embed.add_field(name='公式戦評価の更新', value=f'<t:{int(last)}:R>', inline=True)
            if note:
                embed.add_field(name='今回の計算', value=note[:1024], inline=False)
            if registered_now:
                embed.add_field(name='Steam ID', value='✅ 初回登録しました（次回から入力不要）', inline=False)
            embed.set_footer(text='Aは原則24時間ごとに更新／Bは試合7日後から半減期14日で個別減衰')
            await interaction.followup.send(content=(f'⚠️ {warning}' if warning else None), embed=embed, ephemeral=True)

    @app_commands.command(name='cr_register',description='自身のSteam IDをCRに登録します')
    @app_commands.describe(steam_id='SteamID3（短い数値）またはSteamID64（17桁）')
    async def register(self,interaction:discord.Interaction,steam_id:str):
        await interaction.response.defer(ephemeral=True)
        try:
            sid=cr_rating.steam_to_account_id(steam_id)
            async with self.lock(interaction.user.id):
                row=await asyncio.to_thread(cr_store.register,interaction.user.id,sid)
                saved,error=await self.persist(interaction.user.id)
            message=f'✅ 登録しました。SteamID3: `{sid}`。Aは未評価の暫定1250点です。`/cr_update` で更新してください。'
            if not saved: message+=f'\n⚠️ Discord保管庫への保存失敗：{error}。ローカル記録は保持しています。'
            await interaction.followup.send(message,ephemeral=True)
        except (ValueError,Exception) as exc:
            LOG.exception('CR register failed')
            await interaction.followup.send(f'❌ 登録失敗: {str(exc)[:150]}',ephemeral=True)

    @app_commands.command(name='cr_update',description='公式ランクと試合履歴からAを更新します（24時間ごと）')
    async def update(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        did=interaction.user.id
        async with self.lock(did):
            row=await asyncio.to_thread(cr_store.get,did)
            if not row:
                await interaction.followup.send('❌ `/cr_register` で先に登録してください。',ephemeral=True);return
            if row['updated_at'] and row['confidence'] > 50 and time.time()-row['updated_at']<86400:
                hours=(86400-(time.time()-row['updated_at']))/3600
                await interaction.followup.send(f'⏳ 次の更新まで約{hours:.1f}時間です。現在のA: {row["a"]:.1f}',ephemeral=True);return
            try:
                sid=row['steam32']
                rank=await self.api_json(f'{API}/v1/players/{sid}/rank')
                history=await self.api_json(f'{API}/v1/players/{sid}/match-history')
                if not isinstance(rank,dict) or not isinstance(history,list):
                    raise ValueError('APIレスポンス形式が想定と異なります。')
                a,conf,badge,note=cr_rating.estimate(rank,history,row)
                # Do not mark failed/absent rank data as fresh A; allow retry later.
                if conf==0 and row['confidence']==0:
                    await interaction.followup.send('⚠️ 公式ランクが未確定です。A=1250の暫定値を維持します。',ephemeral=True);return
                row=await asyncio.to_thread(cr_store.update_rating,did,a,conf,badge,time.time())
                saved,error=await self.persist(did)
                b = await asyncio.to_thread(cr_store.current_b, did)
                response=f'✅ A更新: **{a:.1f}**（信頼度 {conf}%）\n{note}\nCR合計: **{a+b+row["c"]:.1f}**'
                if not saved:response+=f'\n⚠️ 保管庫への同期に失敗：{error}'
                await interaction.followup.send(response,ephemeral=True)
            except Exception as exc:
                LOG.exception('CR update failed')
                await interaction.followup.send(f'❌ 更新失敗。前回のCRを維持しています：{str(exc)[:160]}',ephemeral=True)

    @app_commands.command(name='cr_view',description='自身または指定ユーザーのCRを確認します')
    @app_commands.describe(member='省略した場合は自分')
    async def view(self,interaction:discord.Interaction,member:discord.Member|None=None):
        target=member or interaction.user
        row=await asyncio.to_thread(cr_store.get,target.id)
        if not row:
            await interaction.response.send_message('❌ CRが未登録です。',ephemeral=True);return
        e=discord.Embed(title=f'📊 {target.display_name} のCR',color=discord.Color.blue())
        b = await asyncio.to_thread(cr_store.current_b, target.id)
        e.description=f'**合計 {row["a"]+b+row["c"]:.1f} CR**\nA: {row["a"]:+.1f} ／ B: {b:+.1f} ／ C: {row["c"]:+.1f}\n評価信頼度: {row["confidence"]}%'
        e.set_footer(text='Bは試合ごとに時間減衰。Cは管理者のみ変更できます。')
        await interaction.response.send_message(embed=e,ephemeral=True)

    @app_commands.command(name='cr_adjust',description='管理者がプレイヤーのC補正を加算または減算します')
    @app_commands.describe(member='対象ユーザー',delta='Cへの加算量（マイナス可）',reason='変更理由（必須）')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def adjust(self,interaction:discord.Interaction,member:discord.Member,delta:float,reason:str):
        if len(reason.strip())<3 or not -500<=delta<=500:
            await interaction.response.send_message('❌ 理由は3文字以上、1回の変更量は±500以内にしてください。',ephemeral=True);return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock(member.id):
                row=await asyncio.to_thread(cr_store.adjust,member.id,interaction.user.id,delta,reason.strip())
                saved,error=await self.persist(member.id)
            result=f'✅ {member.display_name} のC: **{row["c"]:+.1f}**（今回 {delta:+.1f}）\n理由：{discord.utils.escape_mentions(reason[:200])}'
            if not saved:result+=f'\n⚠️ 保管庫への同期失敗：{error}'
            await interaction.followup.send(result,ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f'❌ {str(exc)[:180]}',ephemeral=True)

    @adjust.error
    async def adjust_error(self,interaction:discord.Interaction,error:app_commands.AppCommandError):
        if isinstance(error,app_commands.MissingPermissions):
            await interaction.response.send_message('❌ サーバーの管理権限が必要です。',ephemeral=True)
        else:
            LOG.exception('cr_adjust app error: %r',error)

    @app_commands.command(name='cr_restore',description='管理者：Discord保管庫からCRを復元')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def restore(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            result=await cr_archive.restore_all(self.bot)
            await interaction.followup.send(f'✅ 保管庫: {result["found"]}人 ／ 復元: {result["restored"]}人 ／ 失敗: {result["failed"]}人',ephemeral=True)
        except Exception as exc:
            LOG.exception('cr_restore failed')
            await interaction.followup.send(f'❌ 復元失敗: {str(exc)[:160]}',ephemeral=True)

    @restore.error
    async def restore_error(self,interaction:discord.Interaction,error:app_commands.AppCommandError):
        if isinstance(error,app_commands.MissingPermissions):
            await interaction.response.send_message('❌ サーバーの管理権限が必要です。',ephemeral=True)

    @app_commands.command(name='cr_split',description='登録済みの12人のCRで6対6に分けます')
    async def split(self,interaction:discord.Interaction):
        await interaction.response.send_modal(SplitRoster())

async def setup(bot:commands.Bot):
    await bot.add_cog(CRManager(bot))
