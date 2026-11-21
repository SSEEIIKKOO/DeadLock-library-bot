"""Deadlock 6v6 / Street Brawl 4v4 custom-match lobby for discord.py 2.x.

Per-guild one active lobby; pending results do not occupy the lobby slot.
The existing CRManager handles CR snapshots. This Cog only calls its known APIs.
"""
from __future__ import annotations
from utils.interaction_notices import temp_response, temp_followup
import asyncio
import logging
import os
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import cr_store, cr_custom, custom_match_store as store
from utils.custom_match_balance import candidates
from utils.team_balancer import Player

LOG = logging.getLogger(__name__)
PANEL_CHANNEL = int(os.getenv('CUSTOM_PANEL_CHANNEL_ID', '0'))
WAITING_VC = int(os.getenv('CUSTOM_WAITING_VC_ID', '0'))
TEAM_A_VC = int(os.getenv('CUSTOM_TEAM_A_VC_ID', '0'))
TEAM_B_VC = int(os.getenv('CUSTOM_TEAM_B_VC_ID', '0'))
RECRUIT_TIMEOUT = 60 * 60
READY_TIMEOUT = 15 * 60
PLAY_TIMEOUT = 3 * 60 * 60


def game_mode(s):
    return s.get('game_mode', 'normal')


def capacity(s):
    return 8 if game_mode(s) == 'street_brawl' else 12


def mode_label(s):
    return '🥊 ストリートブロウル（4対4）' if game_mode(s) == 'street_brawl' else '⚔️ 通常マッチ（6対6）'


def match_key(s):
    return f'CUSTOM-{int(s["id"]):06d}'


def is_admin(interaction):
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


def profile(did):
    row = cr_store.get(did)
    if row is None:
        return None
    b = cr_store.current_b(did)
    return {'a':float(row['a']), 'b':float(b), 'c':float(row['c']),
            'confidence':int(row['confidence']), 'cr':float(row['a'])+b+float(row['c'])}


def build_teams(roster):
    profiles = {str(did):profile(did) for did in roster}
    missing = [did for did in roster if profiles[str(did)] is None]
    if missing:
        raise ValueError('CR未登録の参加者がいます：' + ', '.join(f'<@{x}>' for x in missing))
    players = [Player(str(did), round(profiles[str(did)]['cr']), profiles[str(did)]['confidence']) for did in roster]
    return candidates(players)


class ResultConfirmView(discord.ui.View):
    def __init__(self, cog: 'CustomMatchCog', session_id: int, winner: str):
        super().__init__(timeout=180)
        self.cog, self.session_id, self.winner = cog, session_id, winner

    @discord.ui.button(label='この勝敗を確定', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await temp_response(interaction, '❌ 管理者のみ確定できます。', ephemeral=True, delete_after=20)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            response = await self.cog.finalize(interaction, self.session_id, self.winner)
        except Exception:
            LOG.exception('custom finalize failed: %s', self.session_id)
            response = '❌ 結果確定に失敗しました。CR履歴を確認してから再実行してください。'
        await temp_followup(interaction, response, ephemeral=True, delete_after=20)
        self.stop()

    @discord.ui.button(label='取り消し', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await temp_response(interaction, '結果の確定を取り消しました。', ephemeral=True, delete_after=20)
        self.stop()


class CustomLobbyView(discord.ui.View):
    def __init__(self, cog: 'CustomMatchCog'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label='通常6対6募集', style=discord.ButtonStyle.success, custom_id='deadlock_custom:start', row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'start')

    @discord.ui.button(label='ブロウル4対4募集', style=discord.ButtonStyle.success, custom_id='deadlock_custom:start_brawl', row=0)
    async def start_brawl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'start_brawl')

    @discord.ui.button(label='参加', style=discord.ButtonStyle.primary, custom_id='deadlock_custom:join', row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'join')

    @discord.ui.button(label='辞退', style=discord.ButtonStyle.secondary, custom_id='deadlock_custom:leave', row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'leave')

    @discord.ui.button(label='再抽選', style=discord.ButtonStyle.secondary, custom_id='deadlock_custom:reroll', row=1)
    async def reroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'reroll')

    @discord.ui.button(label='チーム確定', style=discord.ButtonStyle.success, custom_id='deadlock_custom:confirm', row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'confirm')

    @discord.ui.button(label='試合終了申請', style=discord.ButtonStyle.primary, custom_id='deadlock_custom:end', row=2)
    async def end(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'end')

    @discord.ui.button(label='募集・試合中止', style=discord.ButtonStyle.danger, custom_id='deadlock_custom:cancel', row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.action(interaction, 'cancel')


class CustomMatchCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._panel_lock = asyncio.Lock()
        self.bot.add_view(CustomLobbyView(self))

    def lock(self, guild_id):
        return self._locks.setdefault(guild_id, asyncio.Lock())

    async def cog_load(self):
        self.expiry.start()

    def cog_unload(self):
        self.expiry.cancel()

    async def configured_channel(self, guild: discord.Guild):
        if not all((PANEL_CHANNEL, WAITING_VC, TEAM_A_VC, TEAM_B_VC)):
            raise ValueError('`.env` の CUSTOM_PANEL_CHANNEL_ID / CUSTOM_*_VC_ID が未設定です。')
        channel = self.bot.get_channel(PANEL_CHANNEL)
        if channel is None:
            channel = await self.bot.fetch_channel(PANEL_CHANNEL)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise ValueError('募集パネル用チャンネルが、このサーバーのテキストチャンネルではありません。')
        for vc_id in (WAITING_VC, TEAM_A_VC, TEAM_B_VC):
            vc = guild.get_channel(vc_id)
            if not isinstance(vc, discord.VoiceChannel):
                raise ValueError(f'VC ID {vc_id} がこのサーバーのボイスチャンネルではありません。')
        if len({PANEL_CHANNEL, WAITING_VC, TEAM_A_VC, TEAM_B_VC}) != 4:
            raise ValueError('パネルとVCのIDが重複しています。')
        return channel

    def panel_embed(self, s):
        e = discord.Embed(title='⚔️ Deadlock カスタムマッチ募集', color=discord.Color.blurple())
        if s is None:
            e.description = '現在の募集はありません。\n**「通常6対6募集」または「ブロウル4対4募集」から開始できます。'
        else:
            state_names = {'recruiting':'募集中', 'ready':'チーム確認待ち', 'playing':'試合進行中'}
            e.description = (f'**開催ID:** `{match_key(s)}`\n'
                             f'**状態:** {state_names.get(s["state"],s["state"])}\n'
                             f'**モード:** {mode_label(s)}\n'
                             f'**募集主:** <@{s["host_id"]}>\n'
                             f'**参加者:** **{len(s["roster"])}/{capacity(s)}人**')
            if s['state'] == 'recruiting':
                e.add_field(name='参加者',value='\n'.join(f'{i+1}. <@{did}>' for i,did in enumerate(s['roster'])) or 'なし',inline=False)
                e.add_field(name='放置時の終了',value=f'<t:{int(s["changed_at"]+RECRUIT_TIMEOUT)}:R>',inline=False)
            if s['state'] in ('ready', 'playing') and s['teams']:
                idx=s['candidate_index']
                option=s['teams'][idx]
                frozen = s['frozen'] if s['state']=='playing' else None
                def team_str(ids):
                    return '\n'.join(f'<@{did}>' + (f'  `{frozen[str(did)]["cr"]:.0f}`' if frozen else '') for did in ids)
                e.add_field(name=f'🟠 チームA（合計{option["sum_a"]:,}）',value=team_str(option['a']),inline=True)
                e.add_field(name=f'🔵 チームB（合計{option["sum_b"]:,}）',value=team_str(option['b']),inline=True)
                e.add_field(name='CR差・再抽選',value=f'差 **{option["gap"]}**／最小差 {option["minimum_gap"]}\n再抽選 {s["rerolls"]}/3回',inline=False)
                if s['state']=='ready':
                    e.add_field(name='確定期限',value=f'<t:{int(s["ready_at"]+READY_TIMEOUT)}:R>',inline=False)
                else:
                    e.add_field(name='試合の終了期限',value=f'<t:{int(s["started_at"]+PLAY_TIMEOUT)}:R>',inline=False)
        e.set_footer(text='募集主・管理者のみ確定／再抽選／中止できます。結果確定は管理者のみ。')
        return e

    async def refresh_panel(self, guild: discord.Guild, *, reinstall: bool = False):
        # Call under the per-guild lock. Never delete someone else's messages.
        async with self._panel_lock:
            channel=await self.configured_channel(guild)
            panel=await asyncio.to_thread(store.panel,guild.id)
            e=self.panel_embed(await asyncio.to_thread(store.active,guild.id))
            if not reinstall and panel and panel['channel_id']==channel.id:
                try:
                    msg=await channel.fetch_message(panel['message_id'])
                    await msg.edit(embed=e,view=CustomLobbyView(self), allowed_mentions=discord.AllowedMentions.none())
                    return msg
                except discord.NotFound:
                    pass
            msg=await channel.send(embed=e,view=CustomLobbyView(self),allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(store.set_panel,guild.id,channel.id,msg.id)
            if reinstall and panel:
                old_channel = guild.get_channel(panel['channel_id'])
                if isinstance(old_channel, discord.TextChannel):
                    try:
                        old = await old_channel.fetch_message(panel['message_id'])
                        if old.author.id == self.bot.user.id:
                            await old.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        LOG.warning('Old custom panel not removable: %s', panel['message_id'])
            return msg

    async def action(self, interaction: discord.Interaction, what: str):
        if interaction.guild is None:
            await temp_response(interaction, 'サーバーで操作してください。', ephemeral=True, delete_after=20)
            return
        # Reject old/duplicate/deleted panels, even though persistent buttons still respond.
        panel=await asyncio.to_thread(store.panel,interaction.guild.id)
        if panel and (interaction.channel_id != panel['channel_id'] or interaction.message is None or interaction.message.id != panel['message_id']):
            await temp_response(interaction, 'このパネルは古いものです。最新の募集パネルを使ってください。', ephemeral=True, delete_after=20)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        message = ''
        move = None
        async with self.lock(interaction.guild.id):
            try:
                await self.configured_channel(interaction.guild)
                s=await asyncio.to_thread(store.active,interaction.guild.id)
                did=interaction.user.id
                admin=is_admin(interaction)
                if what in ('start', 'start_brawl'):
                    if s: raise ValueError('募集・試合が既に存在します。')
                    if await asyncio.to_thread(profile,did) is None:
                        raise ValueError('最初に `/cr` を実行してCRを登録してください。')
                    s=await asyncio.to_thread(store.create,interaction.guild.id,did,('street_brawl' if what=='start_brawl' else 'normal'))
                    message=f'✅ {mode_label(s)} の募集を開始しました。募集主は自動登録されます。'
                elif s is None:
                    raise ValueError('現在募集中のカスタムはありません。')
                elif what=='join':
                    if s['state']!='recruiting': raise ValueError('現在は参加受付中ではありません。')
                    if did in s['roster']: raise ValueError('すでに参加しています。')
                    if await asyncio.to_thread(profile,did) is None: raise ValueError('先に `/cr` で登録してください。')
                    if len(s['roster'])>=capacity(s): raise ValueError('定員に達しています。')
                    s['roster'].append(did)
                    s['changed_at']=time.time()
                    if len(s['roster'])==capacity(s):
                        s['teams']=await asyncio.to_thread(build_teams,s['roster'])
                        s['state']='ready';s['ready_at']=time.time();s['candidate_index']=0;s['rerolls']=0
                        message=f'✅ {capacity(s)}人揃いました。CRによるチーム分けを表示しました。募集主が確定してください。'
                    else:
                        message=f'✅ 参加しました（{len(s["roster"])}/{capacity(s)}人）。'
                    await asyncio.to_thread(store.save,s)
                elif what=='leave':
                    if s['state'] not in ('recruiting','ready'): raise ValueError('チーム確定後は辞退できません。')
                    if did not in s['roster']: raise ValueError('参加していません。')
                    was_host=(did==s['host_id'])
                    s['roster'].remove(did)
                    now=time.time();s['changed_at']=now
                    if not s['roster']:
                        s['state']='cancelled';s['ended_at']=now;s['ended_reason']='全員辞退'
                    else:
                        if did==s['host_id']:s['host_id']=s['roster'][0]
                        if s['state']=='ready':
                            s['state']='recruiting';s['ready_at']=None;s['teams']=[];s['rerolls']=0;s['candidate_index']=0
                    await asyncio.to_thread(store.save,s)
                    message='✅ 辞退しました。' + (' 募集主は次の参加者に引き継がれました。' if s['roster'] and was_host else '')
                elif what=='reroll':
                    if s['state']!='ready':raise ValueError('定員が揃った確定待ちのときのみ再抽選できます。')
                    if did!=s['host_id'] and not admin:raise ValueError('募集主または管理者のみ再抽選できます。')
                    if s['rerolls']>=3 or s['candidate_index']+1>=len(s['teams']):
                        raise ValueError('公平性の条件を満たす別候補がありません（または3回抽選済み）。')
                    s['rerolls']+=1;s['candidate_index']+=1
                    await asyncio.to_thread(store.save,s)
                    message=f'🔁 再抽選しました（{s["rerolls"]}/3）。'
                elif what=='confirm':
                    if s['state']!='ready':raise ValueError('確定待ちの募集がありません。')
                    if did!=s['host_id'] and not admin:raise ValueError('募集主または管理者のみ確定できます。')
                    if len(s['roster'])!=capacity(s):raise ValueError(f'参加者が{capacity(s)}人ではありません。')
                    frozen={}
                    for uid in s['roster']:
                        row=await asyncio.to_thread(profile,uid)
                        if row is None:raise ValueError(f'<@{uid}> のCR登録がありません。')
                        frozen[str(uid)]=row
                    # Freeze the CR used for B; optimize from same snapshot immediately prior to confirmation.
                    s['frozen']=frozen;s['state']='playing';s['started_at']=time.time();s['changed_at']=s['started_at']
                    await asyncio.to_thread(store.save,s)
                    move=s
                    message='✅ チームを確定しました。待機VCにいる参加者だけを自動移動します。'
                elif what=='end':
                    if s['state']!='playing':raise ValueError('進行中の試合がありません。')
                    if did!=s['host_id'] and not admin:raise ValueError('募集主または管理者のみ終了申請できます。')
                    s['state']='pending';s['ended_at']=time.time();s['changed_at']=s['ended_at'];s['ended_reason']='終了申請'
                    await asyncio.to_thread(store.save,s)
                    message=f'✅ {match_key(s)} を結果待ちにしました。募集枠は解放済みです。管理者は `/custom_result` から勝敗を確定してください。'
                elif what=='cancel':
                    if did!=s['host_id'] and not admin:raise ValueError('募集主または管理者のみ中止できます。')
                    s['state']='cancelled';s['ended_at']=time.time();s['ended_reason']='募集主・管理者による中止'
                    await asyncio.to_thread(store.save,s)
                    message=f'✅ {match_key(s)} を中止しました。カスタム戦績ポイントの変動はありません。'
                else:
                    raise ValueError('不明な操作です。')
                await self.refresh_panel(interaction.guild)
            except (ValueError, sqlite3.Error, discord.HTTPException) as exc:
                LOG.warning('Custom action %s failed: %s',what,exc)
                message=f'❌ {str(exc)[:250]}'
            except Exception:
                LOG.exception('Custom action failed: %s',what)
                message='❌ 処理中にエラーが発生しました。管理者にBotログの確認を依頼してください。'
        if move:
            result=await self.move_waiting(interaction.guild,move)
            message+='\n'+result
        await temp_followup(interaction, message,ephemeral=True, delete_after=20,allowed_mentions=discord.AllowedMentions.none())

    async def move_waiting(self,guild,s):
        option=s['teams'][s['candidate_index']]
        moved=0;failed=0;unconnected=0
        for team,vc_id in (('a',TEAM_A_VC),('b',TEAM_B_VC)):
            vc=guild.get_channel(vc_id)
            for did in option[team]:
                member=guild.get_member(did)
                if member is None:
                    try:member=await guild.fetch_member(did)
                    except discord.HTTPException:member=None
                if not member or not member.voice or not member.voice.channel or member.voice.channel.id!=WAITING_VC:
                    unconnected+=1
                    continue
                try:
                    await member.move_to(vc,reason=f'{match_key(s)} 自動チーム振り分け')
                    moved+=1
                except discord.HTTPException:
                    failed+=1
        return (f'移動：{moved}人／待機VC以外・未接続：{unconnected}人／失敗：{failed}人\n'
                f'🟠 <#{TEAM_A_VC}>　🔵 <#{TEAM_B_VC}>\n'
                '未移動の参加者には公開パネルでチーム割り当てを確認してもらってください。')

    async def finalize(self, interaction, session_id, winner):
        guild=interaction.guild
        if guild is None or not is_admin(interaction):return '❌ 管理者権限が必要です。'
        if winner not in ('A','B','void'):return '❌ 勝利側を選択してください。'
        async with self.lock(guild.id):
            s=await asyncio.to_thread(store.get,session_id)
            if s is None or s['guild_id']!=guild.id or s['state']!='pending':
                return '❌ 試合が存在しないか、既に処理済みです。'
            key=match_key(s)
            if winner=='void':
                s['state']='void';s['winner']='void';s['confirmed_by']=interaction.user.id
                await asyncio.to_thread(store.save,s)
                return f'✅ {key} を無効試合として確定しました（CR変動なし）。'
            if len(s['teams'])==0 or len(s['frozen'])!=capacity(s):
                return '❌ 開始時点のCRスナップショットがありません。結果を登録できません。'
            option=s['teams'][s['candidate_index']]
            a,b=option['a'],option['b']
            ids=a+b
            snapshot=s['frozen']
            fake_rows=[{'discord_id':did,'a':snapshot[str(did)]['a'],'c':snapshot[str(did)]['c']} for did in ids]
            b_values={did:snapshot[str(did)]['b'] for did in ids}
            if game_mode(s) == 'street_brawl':
                # Separate-mode record only: the 4v4 CR-B rule has not been calibrated.
                if not await asyncio.to_thread(store.mark_result,s['id'],winner,interaction.user.id):
                    return '❌ 試合は既に確定された可能性があります。'
                return f'✅ {key}（ブロウル）の勝者：チーム{winner}。記録しました（CR戦績Bは変動なし）。'
            deltas,pool,expected=cr_custom.allocate(fake_rows[:6],fake_rows[6:],winner,b_values)
            manager=self.bot.get_cog('CRManager')
            if manager is None:
                return '❌ CRManagerが読み込まれていません。'
            async with manager.custom_lock:
                try:
                    # Use match start time for event decay, rather than admin approval time.
                    await asyncio.to_thread(cr_store.record_custom_match,key,winner,interaction.user.id,
                                            a,b,deltas,float(s['started_at']))
                except ValueError as exc:
                    # The CR ledger may be written while the lobby result state was not.
                    # Never rewrite or double-credit a conflicting ledger entry.
                    return f'⚠️ CR試合台帳への登録を中止しました：{str(exc)[:180]}。管理者が履歴を確認してください。'
                if not await asyncio.to_thread(store.mark_result,s['id'],winner,interaction.user.id):
                    LOG.error('CR recorded but lobby status update failed: %s',key)
                    return f'⚠️ CR台帳へ {key} を記録しましたが募集DBの完了更新に失敗しました。再登録せずログを確認してください。'
                ok=0
                for did in ids:
                    try:
                        saved,_=await manager.persist(did)
                        ok+=int(saved)
                    except Exception:
                        LOG.exception('CR remote snapshot failed for %s',did)
                return (f'✅ {key} の勝者：チーム{winner}。'
                        f'勝利側+{pool:.2f}／敗北側−{pool:.2f}（ゼロサム）\n'
                        f'CR保管庫への同期：{ok}/12人' + (' ⚠️未同期分はローカルDBに保持' if ok<12 else ''))

    @app_commands.command(name='custom_setup',description='管理者：常設カスタム募集パネルを設置・修復')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def custom_setup(self,interaction:discord.Interaction):
        if not interaction.guild:
            await temp_response(interaction, 'サーバー内で実行してください。',ephemeral=True, delete_after=20);return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            async with self.lock(interaction.guild.id):
                msg=await self.refresh_panel(interaction.guild)
            await temp_followup(interaction, f'✅ 常設パネルを確認しました：{msg.jump_url}',ephemeral=True, delete_after=20)
        except Exception as exc:
            LOG.exception('Custom setup failed')
            await temp_followup(interaction, f'❌ パネル設置失敗：{str(exc)[:220]}',ephemeral=True, delete_after=20)

    @app_commands.command(name='custom_results',description='管理者：勝敗未確定のカスタム一覧')
    @app_commands.checks.has_permissions(manage_guild=True)
    async def custom_results(self,interaction:discord.Interaction):
        if not interaction.guild:
            await temp_response(interaction, 'サーバー内で実行してください。',ephemeral=True, delete_after=20);return
        rows=await asyncio.to_thread(store.pending,interaction.guild.id)
        content='\n'.join(f'`{match_key(s)}` {mode_label(s)}：開催主 <@{s["host_id"]}> ／ <t:{int(s["started_at"])}:f>' for s in rows)
        await temp_response(interaction, content or '✅ 勝敗未確定の試合はありません。',ephemeral=True, delete_after=20,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='custom_result',description='管理者：終了済みカスタムの勝敗をプレビューして確定')
    @app_commands.describe(session_id='開催IDの数字部分（例 CUSTOM-000012 → 12）',winner='勝利チーム／無効試合')
    @app_commands.choices(winner=[app_commands.Choice(name='チームA勝利',value='A'),
                                  app_commands.Choice(name='チームB勝利',value='B'),
                                  app_commands.Choice(name='無効試合（ポイント変動なし）',value='void')])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def custom_result(self,interaction:discord.Interaction,session_id:int,winner:app_commands.Choice[str]):
        if not interaction.guild:
            await temp_response(interaction, 'サーバー内で実行してください。',ephemeral=True, delete_after=20);return
        s=await asyncio.to_thread(store.get,session_id)
        if s is None or s['guild_id']!=interaction.guild.id or s['state']!='pending':
            await temp_response(interaction, '❌ 該当する結果待ち試合はありません。',ephemeral=True, delete_after=20);return
        option=s['teams'][s['candidate_index']]
        if winner.value=='void':
            detail='無効試合として確定し、Bは変動させません。'
        else:
            ids=option['a']+option['b']
            snapshot=s['frozen']
            rows=[{'discord_id':did,'a':snapshot[str(did)]['a'],'c':snapshot[str(did)]['c']} for did in ids]
            b={did:snapshot[str(did)]['b'] for did in ids}
            if game_mode(s) == 'street_brawl':
                detail='ストリートブロウル（4対4）：勝敗だけを記録します。CR戦績Bは変動しません。'
            else:
                deltas,pool,_=cr_custom.allocate(rows[:6],rows[6:],winner.value,b)
                detail='\n'.join(f'<@{did}> `{deltas[did]:+.1f}`' for did in ids)
                detail+=f'\n**チーム全体：+{pool:.1f} ／ −{pool:.1f}**'
        e=discord.Embed(title=f'勝敗確定の確認：{match_key(s)} {mode_label(s)}',description=detail[:4000],color=discord.Color.orange())
        await interaction.response.send_message(embed=e,view=ResultConfirmView(self,session_id,winner.value),
                                                ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_member_remove(self, member:discord.Member):
        if not all((PANEL_CHANNEL,WAITING_VC,TEAM_A_VC,TEAM_B_VC)):
            return
        async with self.lock(member.guild.id):
            s=await asyncio.to_thread(store.active,member.guild.id)
            if not s or member.id not in s['roster']:
                return
            if s['state']=='playing':
                # Preserve frozen teams and historical roster. Only replace departed host.
                if member.id==s['host_id']:
                    remaining=[uid for uid in s['roster'] if uid!=member.id and member.guild.get_member(uid)]
                    if remaining:
                        s['host_id']=remaining[0]
                        await asyncio.to_thread(store.save,s)
                        try:await self.refresh_panel(member.guild)
                        except discord.HTTPException:LOG.exception('Could not refresh panel after host left')
                return
            if s['state'] not in ('recruiting','ready'):
                return
            s['roster'].remove(member.id)
            now=time.time();s['changed_at']=now
            if not s['roster']:
                s['state']='cancelled';s['ended_at']=now;s['ended_reason']='参加者が全員退出'
            else:
                if member.id==s['host_id']:s['host_id']=s['roster'][0]
                if s['state']=='ready':
                    s['state']='recruiting';s['ready_at']=None;s['teams']=[];s['candidate_index']=0;s['rerolls']=0
            await asyncio.to_thread(store.save,s)
            try:await self.refresh_panel(member.guild)
            except discord.HTTPException:LOG.exception('Could not refresh panel on member remove')

    @tasks.loop(seconds=30)
    async def expiry(self):
        for s in await asyncio.to_thread(store.all_active):
            guild=self.bot.get_guild(s['guild_id'])
            if not guild:continue
            async with self.lock(guild.id):
                current=await asyncio.to_thread(store.active,guild.id)
                if not current or current['id']!=s['id']:continue
                now=time.time();reason=None
                if current['state']=='recruiting' and now-current['changed_at']>=RECRUIT_TIMEOUT:
                    reason='参加人数の変化なし60分'
                elif current['state']=='ready' and now-float(current['ready_at'] or now)>=READY_TIMEOUT:
                    reason='チーム確定待ち15分'
                elif current['state']=='playing' and now-float(current['started_at'] or now)>=PLAY_TIMEOUT:
                    reason='試合開始から3時間'
                if reason:
                    current['state']='pending' if s['state']=='playing' else 'expired'
                    current['ended_at']=now;current['ended_reason']=reason
                    await asyncio.to_thread(store.save,current)
                    try:await self.refresh_panel(guild)
                    except Exception:LOG.exception('Could not refresh custom lobby after timeout')

    @expiry.before_loop
    async def before_expiry(self):
        await self.bot.wait_until_ready()
        # Restore existing persistent panel, or reinstall when its message is deleted.
        if not all((PANEL_CHANNEL,WAITING_VC,TEAM_A_VC,TEAM_B_VC)):
            LOG.warning('Custom lobby disabled: CUSTOM_* channel IDs missing')
            return
        channel=self.bot.get_channel(PANEL_CHANNEL)
        if isinstance(channel,discord.TextChannel):
            try:
                async with self.lock(channel.guild.id):
                    await self.refresh_panel(channel.guild, reinstall=True)
            except Exception:LOG.exception('Could not restore custom lobby panel')


async def setup(bot:commands.Bot):
    await bot.add_cog(CustomMatchCog(bot))
