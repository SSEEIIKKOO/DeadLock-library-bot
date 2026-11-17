import asyncio
from datetime import datetime
import json
import os
import aiohttp
from urllib.parse import urlsplit
import discord
from discord import app_commands
from discord.ext import commands
from cogs.profile_intro import load_players
from utils.gdrive_uploader import upload_replay_bytes


DEADLOCK_API_BASE = "https://" + "api.deadlock-api.com"
DEADLOCK_ASSETS_BASE = DEADLOCK_API_BASE

CACHE_DIR = "data/match_cache"

# API通信はブラウザを起動せずaiohttpで行う。
# 429 / 500系の一時的なエラーのみ再試行し、403や404を無条件に再試行しない。
async def fetch_json_via_browser(url: str) -> tuple[int, dict | list | None]:
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"Accept": "application/json", "User-Agent": "DeadLockLibraryBot/1.0"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(3):
            try:
                async with session.get(url) as resp:
                    status = resp.status
                    if status == 200:
                        try:
                            data = await resp.json(content_type=None)
                        except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError) as e:
                            print(f"[DeadlockAPI] Invalid JSON: url={url}, error={e}")
                            return 502, None
                        if not isinstance(data, (dict, list)):
                            print(f"[DeadlockAPI] Unexpected JSON format: url={url}")
                            return 502, None
                        return 200, data

                    body = (await resp.text(errors="replace"))[:300].replace("\n", " ")
                    print(f"[DeadlockAPI] HTTP {status}: url={url}, body={body!r}")
                    if status not in (429, 500, 502, 503, 504) or attempt == 2:
                        return status, None

                    retry_after = resp.headers.get("Retry-After")
                    delay = min(15.0, 2.0 ** attempt)
                    if retry_after:
                        try:
                            delay = min(30.0, max(1.0, float(retry_after)))
                        except ValueError:
                            pass
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"[DeadlockAPI] Request error ({attempt + 1}/3): url={url}, error={e!r}")
                if attempt == 2:
                    return 0, None
                delay = 2.0 ** attempt
            await asyncio.sleep(delay)

    return 0, None


def _validated_replay_url(value: object) -> str | None:
    """APIから得たValveのデモURLのみを許可する（任意URLへのアクセスを防止）。"""
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    # Valveのリプレイサーバー: replay{cluster_id}.valve.net
    if (parsed.scheme not in ("http", "https")
            or not host.startswith("replay")
            or not host.endswith(".valve.net")
            or not host[6:-10].isdigit()
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 80, 443)
            or not parsed.path.endswith(".dem.bz2")):
        return None
    return value


class ReplayCaptionModal(discord.ui.Modal, title="リプレイを図書館に登録"):
    caption = discord.ui.TextInput(
        label="キャプション（試合の特徴・見どころなど）",
        placeholder="例：終盤の逆転試合、タロン視点の立ち回りが参考になる",
        style=discord.TextStyle.paragraph,
        required=True,
        min_length=2,
        max_length=600,
    )

    def __init__(self, match_id: str, result_embed: discord.Embed | None = None):
        super().__init__()
        self.match_id = match_id
        # 保存ボタンが押された時点の試合結果を保持する。
        self.result_embed = result_embed.copy() if result_embed is not None else None

    async def on_submit(self, interaction: discord.Interaction):
        import config
        from utils.gdrive_uploader import find_saved_replays, append_replay_caption
        from utils.replay_index import add_post

        forum_id = int(os.getenv("REPLAY_FORUM_CHANNEL_ID", "0"))
        if not forum_id:
            await interaction.response.send_message(
                "❌ .env に REPLAY_FORUM_CHANNEL_ID を設定してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        caption = str(self.caption.value).strip()
        forum = interaction.client.get_channel(forum_id)
        if forum is None:
            try:
                forum = await interaction.client.fetch_channel(forum_id)
            except discord.HTTPException:
                forum = None
        if not isinstance(forum, discord.ForumChannel):
            await interaction.followup.send(
                "❌ 登録先のフォーラムが見つかりません。Botの参加状況・チャンネルIDを確認してください。",
                ephemeral=True)
            return
        if getattr(config, 'TARGET_GUILD_ID', 0) and forum.guild.id != config.TARGET_GUILD_ID:
            await interaction.followup.send(
                "❌ 登録先フォーラムがTARGET_GUILD_IDの図書館サーバーにありません。", ephemeral=True)
            return
        match_id = self.match_id
        try:
            # First check Drive: do not spend an API request or download bytes
            # when the replay is already archived.
            saved = await asyncio.to_thread(find_saved_replays, match_id)
            if saved:
                entry = saved[0]
                drive_link = entry.get('webViewLink') or f"https://drive.google.com/file/d/{entry['id']}/view"
                drive_reused = True
            else:
                salts_url = f"{DEADLOCK_API_BASE}/v1/matches/{match_id}/salts"
                status, salts = await fetch_json_via_browser(salts_url)
                if status != 200 or not isinstance(salts, dict):
                    raise RuntimeError(f"リプレイ保存先情報の取得に失敗しました (HTTP {status})")
                replay_url = _validated_replay_url(salts.get('demo_url'))
                if not replay_url:
                    raise RuntimeError("有効なリプレイURLを取得できませんでした")
                max_bytes = 512 * 1024 * 1024
                timeout = aiohttp.ClientTimeout(total=240, connect=15, sock_read=60)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(replay_url, allow_redirects=False) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"リプレイダウンロードに失敗しました (HTTP {resp.status})")
                        if resp.content_length is not None and resp.content_length > max_bytes:
                            raise RuntimeError("リプレイが512MBを超えています")
                        chunks, size = [], 0
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise RuntimeError("リプレイが512MBを超えています")
                            chunks.append(chunk)
                file_bytes = b''.join(chunks)
                if file_bytes.startswith(bytes.fromhex('28b52ffd')):
                    ext = '.dem.zst'
                elif file_bytes.startswith(b'BZh'):
                    ext = '.dem.bz2'
                else:
                    raise RuntimeError("ダウンロード内容が既知のリプレイ圧縮形式ではありません")
                filename = f"{datetime.now().strftime('%Y%m%d')}_Match{match_id}{ext}"
                # upload_replay_bytes rechecks Drive under a lock.
                drive_link = await asyncio.to_thread(upload_replay_bytes, file_bytes, filename)
                drive_reused = False

            # Keep the full caption in the forum; each submission creates a NEW post.
            safe_author = discord.utils.escape_markdown(interaction.user.display_name)
            safe_caption = discord.utils.escape_mentions(caption)
            content = (f"**Match ID:** `{match_id}`\n"
                       f"**投稿者:** {safe_author}\n\n"
                       f"### キャプション\n{safe_caption}\n\n"
                       f"**保存済みリプレイ:** {drive_link}")
            thread_name = f"【Match {match_id}】{caption.replace(chr(10), ' ')[:55]}"
            # フォーラムの最初の投稿に /match と同じ試合結果Embedを付ける。
            # キャプション・Driveリンクは本文に残し、検索索引の仕様は変えない。
            thread_kwargs = {"name": thread_name[:100], "content": content}
            if self.result_embed is not None:
                thread_kwargs["embed"] = self.result_embed.copy()
            created = await forum.create_thread(**thread_kwargs)
            thread = created.thread
            # Index failure must be visible: a forum post may already exist.
            try:
                await asyncio.to_thread(
                    add_post, match_id, caption, interaction.user.id,
                    interaction.user.display_name, thread.id, thread.jump_url, drive_link)
            except Exception:
                import logging
                logging.exception('Replay indexed post failed: thread=%s', thread.id)
                await interaction.followup.send(
                    f"⚠️ 図書館への投稿は成功しましたが、検索索引への登録に失敗しました。\n"
                    f"投稿: {thread.jump_url}\n管理者にログの確認を依頼してください。", ephemeral=True)
                return
            try:
                await asyncio.to_thread(append_replay_caption, match_id, caption, thread.jump_url)
            except Exception:
                import logging
                logging.exception('Drive caption append failed: match=%s', match_id)
                # Search still works from SQLite; do not mark the submission failed.
            reused_note = '（Google Driveの保存済みファイルを再利用）' if drive_reused else '（新規にDriveへ保存）'
            await interaction.followup.send(
                f"✅ 図書館にリプレイを登録しました！{reused_note}\n"
                f"📚 投稿: {thread.jump_url}\n💾 Drive: {drive_link}", ephemeral=True)
        except Exception as exc:
            import logging
            logging.exception('Replay library submission failed: match=%s', match_id)
            await interaction.followup.send(
                f"❌ 登録に失敗しました: {str(exc)[:300]}\n"
                "※Driveに保存だけ成功している場合があります。再試行時は既存ファイルを再利用します。",
                ephemeral=True)


class ReplaySaveView(discord.ui.View):
    def __init__(self, match_id: str):
        super().__init__(timeout=600)
        self.match_id = match_id

    @discord.ui.button(label="💾 キャプションを付けて図書館へ保存", style=discord.ButtonStyle.success)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # /match で表示されたEmbedをそのまま引き継ぐ。
        result_embed = None
        if interaction.message and interaction.message.embeds:
            result_embed = interaction.message.embeds[0]
        await interaction.response.send_modal(ReplayCaptionModal(self.match_id, result_embed))


class MatchResultCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.hero_names: dict[int, str] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.hero_names:
            await self.ensure_hero_names()

    async def ensure_hero_names(self):
        if self.hero_names:
            return
        endpoint = f"{DEADLOCK_ASSETS_BASE}/v1/assets/heroes"
        try:
            status, data = await fetch_json_via_browser(endpoint)
            if status == 200 and data:
                items = data if isinstance(data, list) else data.values()
                for hero in items:
                    if not isinstance(hero, dict):
                        continue
                    hero_id = hero.get("id")
                    name = hero.get("name") or hero.get("class_name")
                    if hero_id is not None and name:
                        self.hero_names[int(hero_id)] = str(name)
                if self.hero_names:
                    print(f"[MatchResultCog] ヒーロー辞書をキャッシュしました ({len(self.hero_names)}体)")
        except Exception as e:
            print(f"[MatchResultCog] ヒーロー一覧の取得に失敗: {e}")
    async def fetch_player_names(self, account_ids: list[int]) -> dict[int, str]:
        names_map: dict[int, str] = {}
        valid_ids = [str(aid) for aid in set(account_ids) if aid]
        if not valid_ids:
            return names_map

        ids_query = ",".join(valid_ids)
        url = f"{DEADLOCK_API_BASE}/v1/players/steam?account_ids={ids_query}"
        try:
            status, data = await fetch_json_via_browser(url)
            if status == 200 and isinstance(data, list):
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

        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(CACHE_DIR, f"{clean_id}.json")
        data = None
        player_names_map: dict[int, str] = {}

        try:
            if not self.hero_names:
                await self.ensure_hero_names()

            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                url = f"{DEADLOCK_API_BASE}/v1/matches/{clean_id}/metadata"
                status, data = await fetch_json_via_browser(url)

                if status == 404:
                    await interaction.followup.send(
                        f"📭 マッチID `{clean_id}` のデータはAPIから返されませんでした。"
                        "試合が未登録・取得待ちの可能性があります。"
                    )
                    return
                if status != 200 or not isinstance(data, dict):
                    advice = (
                        "アクセス拒否の可能性があります。APIの利用条件・認証設定をご確認ください。" if status in (401, 403)
                        else "アクセス制限中です。しばらく時間を置いてください。" if status == 429
                        else "APIまたは通信の一時的な障害の可能性があります。" if status in (0, 500, 502, 503, 504)
                        else "サーバーログの詳細を確認してください。"
                    )
                    await interaction.followup.send(
                        f"⚠️ データ取得に失敗しました（HTTP: `{status}`）。{advice}"
                    )
                    return

                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)

            info = data.get("match_info", data)
            players = info.get("players", [])

            registered_players = load_players()
            unregistered_ids = [
                p.get("account_id")
                for p in players
                if p.get("account_id") and str(p.get("account_id")) not in registered_players
            ]
            if unregistered_ids:
                player_names_map = await self.fetch_player_names(unregistered_ids)

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
            reg_info = registered_players.get(str(account_id)) if account_id is not None else None

            if reg_info:
                label = f"★ **{hero_name}** ({reg_info['name']})"
            else:
                player_name = (
                    (player_names_map.get(int(account_id)) if account_id is not None else None)
                    or p.get("persona_name")
                    or p.get("account_name")
                )
                label = f"**{hero_name}** ({player_name})" if player_name else f"**{hero_name}**"

            line = f"{label}：`{kills}/{deaths}/{assists}` ｜ 💰`{souls_k}`"

            if p.get("team") == 0:
                team_hidden_king.append(line)
            else:
                team_archmother.append(line)


        embed = discord.Embed(
            title=f"⚔️ Deadlock 試合リザルト（Match ID: {clean_id}）",
            description=(
                f"**結果**: {winner_str}\n"
                f"**試合時間**: {duration_str}"
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

        await interaction.followup.send(
            embed=embed,
            view=ReplaySaveView(clean_id),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchResultCog(bot))
