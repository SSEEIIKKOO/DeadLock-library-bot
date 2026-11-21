import asyncio
from datetime import datetime
import json
import os
import time
import aiohttp
from urllib.parse import urlsplit
import discord
from discord import app_commands
from discord.ext import commands
from cogs.profile_intro import load_players
from utils.gdrive_uploader import upload_replay_bytes
from utils.match_archive import get_archived, store_archived, read_local, delete_local_after_archive
from utils.street_brawl_result import is_street_brawl, street_brawl_description


DEADLOCK_API_BASE = "https://" + "api.deadlock-api.com"
DEADLOCK_ASSETS_BASE = DEADLOCK_API_BASE

CACHE_DIR = "data/match_cache"

# metadata は API/S3 保存データ優先で検索する。Steam 側のIP制限は3回/時。
# Steam問い合わせはBot内で控えめに2回/時に制限する（同一IPの他アプリは把握できない）。
STEAM_WINDOW_SECONDS = 3600
STEAM_LIMIT = 2
STEAM_STATE_FILE = "data/match_steam_budget.json"
_steam_lock = asyncio.Lock()
_match_locks: dict[str, asyncio.Lock] = {}


def _read_steam_state() -> dict:
    try:
        with open(STEAM_STATE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            return obj
    except (OSError, ValueError, TypeError):
        pass
    return {"attempts": [], "blocked_until": 0}


def _write_steam_state(obj: dict) -> None:
    os.makedirs(os.path.dirname(STEAM_STATE_FILE), exist_ok=True)
    tmp = STEAM_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, STEAM_STATE_FILE)


def _quota_wait_seconds(obj: dict) -> int:
    now = time.time()
    attempts = [float(t) for t in obj.get("attempts", []) if isinstance(t, (float, int)) and now - float(t) < STEAM_WINDOW_SECONDS]
    wait_block = max(0.0, float(obj.get("blocked_until", 0)) - now)
    wait_budget = max(0.0, min(attempts) + STEAM_WINDOW_SECONDS - now) if len(attempts) >= STEAM_LIMIT else 0.0
    return int(max(wait_block, wait_budget) + 0.999)


def _parse_retry_seconds(headers, body: object) -> int:
    # API の next_request_in / Retry-After を優先し、429 の場合だけ利用する。
    candidates = []
    try:
        candidates.append(float(headers.get("Retry-After", 0)))
    except (ValueError, TypeError):
        pass
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            quota = error.get("quota")
            if isinstance(quota, dict):
                try:
                    candidates.append(float(quota.get("next_request_in", 0)))
                except (ValueError, TypeError):
                    pass
    return max(60, min(7200, int(max(candidates or [3600]))))


async def fetch_json_via_browser(url: str) -> tuple[int, dict | list | None]:
    """既存の呼び出し名を維持。HTTP 429は再試行しない。"""
    status, data, _ = await _fetch_json(url)
    return status, data


async def _fetch_json(url: str) -> tuple[int, dict | list | None, int]:
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    headers = {"Accept": "application/json", "User-Agent": "DeadLockLibraryBot/1.1"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(2):
            try:
                async with session.get(url) as resp:
                    status = resp.status
                    raw = await resp.text(errors="replace")
                    try:
                        data = json.loads(raw)
                    except (ValueError, TypeError):
                        data = None
                    if status == 200:
                        if isinstance(data, (dict, list)):
                            return 200, data, 0
                        print(f"[DeadlockAPI] JSON形式が不正: {url}")
                        return 502, None, 0
                    print(f"[DeadlockAPI] HTTP {status}: {url}, body={raw[:240]!r}")
                    if status == 429:
                        return 429, None, _parse_retry_seconds(resp.headers, data)
                    if status not in (500, 502, 503, 504) or attempt == 1:
                        return status, None, 0
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                print(f"[DeadlockAPI] 通信エラー: {url}: {exc!r}")
                if attempt == 1:
                    return 0, None, 0
            await asyncio.sleep(1.5)
    return 0, None, 0


async def _get_match_metadata(match_id: str) -> tuple[int, dict | None, int, str]:
    """ローカル→API/S3→Steam(1IPにつきBot内2回/時) の順。"""
    cache_url = f"{DEADLOCK_API_BASE}/v1/matches/{match_id}/metadata?disable_steam=true"
    status, data, retry = await _fetch_json(cache_url)
    if status == 200 and isinstance(data, dict):
        return status, data, 0, "api-cache"
    # 保存データがない場合のみSteamフォールバックを許す。
    # 429/5xx/認証エラーでのSteamフォールバックは行わない。
    if status not in (404, 422, 424):
        return status, None, retry, "cache-error"

    async with _steam_lock:
        state = _read_steam_state()
        now = time.time()
        state["attempts"] = [float(t) for t in state.get("attempts", [])
                             if isinstance(t, (int, float)) and now - float(t) < STEAM_WINDOW_SECONDS]
        wait = _quota_wait_seconds(state)
        if wait:
            return 429, None, wait, "local-limit"
        # 失敗しても試行として記録し、再起動後も安全側に倒す。
        state["attempts"].append(now)
        _write_steam_state(state)
        steam_url = f"{DEADLOCK_API_BASE}/v1/matches/{match_id}/metadata"
        status, data, retry = await _fetch_json(steam_url)
        if status == 429:
            state = _read_steam_state()
            state["blocked_until"] = max(float(state.get("blocked_until", 0)), time.time() + retry)
            _write_steam_state(state)
        if status == 200 and isinstance(data, dict):
            return 200, data, 0, "steam"
        return status, None, retry, "steam-error"


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
            # The public donation is already complete; archive failure must not delete cache
            # or misreport the existing public forum post as failed.
            archive_note = ''
            try:
                local_data = await asyncio.to_thread(read_local, match_id)
                if local_data is not None:
                    archived = await store_archived(interaction.client, match_id, local_data)
                    if archived:
                        await asyncio.to_thread(delete_local_after_archive, match_id, local_data)
                        archive_note = '\n🔒 リザルトJSONを非公開保管庫へ保存しました。'
                elif await get_archived(interaction.client, match_id) is not None:
                    archive_note = '\n🔒 リザルトJSONは保管済みです。'
                else:
                    archive_note = '\n⚠️ ローカルJSONが見つからず、保管庫への移行は行われませんでした。'
            except Exception as archive_exc:
                import logging
                logging.exception('Match archive failed after forum donation: match=%s', match_id)
                archive_note = '\n⚠️ リザルトJSONの非公開保管に失敗しました。ローカルキャッシュは保持します。'
            reused_note = '（Google Driveの保存済みファイルを再利用）' if drive_reused else '（新規にDriveへ保存）'
            await interaction.followup.send(
                f"✅ 図書館にリプレイを登録しました！{reused_note}\n"
                f"📚 投稿: {thread.jump_url}\n💾 Drive: {drive_link}{archive_note}", ephemeral=True)
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

    @app_commands.command(name="match_archive_backfill", description="既存の試合JSONキャッシュを非公開保管庫へ移行（管理者専用）")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(limit="今回確認するキャッシュ数（最大50）")
    async def match_archive_backfill(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50] = 20):
        # default_permissions alone is not an authorization check.
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("管理者のみ利用できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        from pathlib import Path
        scanned = done = failures = 0
        for path in sorted(Path(CACHE_DIR).glob('*.json')):
            if scanned >= limit:
                break
            match_id = path.stem
            if not match_id.isdecimal():
                continue
            scanned += 1
            try:
                # リプレイ寄贈の有無を問わず、既存の試合キャッシュを保管する。
                data = await asyncio.to_thread(read_local, match_id)
                if data is None:
                    continue
                if await store_archived(self.bot, match_id, data):
                    await asyncio.to_thread(delete_local_after_archive, match_id, data)
                    done += 1
            except Exception:
                import logging
                logging.exception('Archive backfill failed match=%s', match_id)
                failures += 1
        await interaction.followup.send(
            f"照合: {scanned}件 / 非公開保管できた試合: {done}件 / エラー: {failures}件\n"
            "※リプレイ寄贈の有無にかかわらず保管します。保管・検証成功後だけローカルJSONを削除します。", ephemeral=True)

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

            # 同じmatch_idの並行リクエストが重複してAPIを消費しないようにする。
            lock = _match_locks.setdefault(clean_id, asyncio.Lock())
            async with lock:
                if os.path.exists(cache_path):
                    try:
                        with open(cache_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except (OSError, ValueError):
                        data = None
                if not isinstance(data, dict):
                    data = await get_archived(self.bot, clean_id)
                    if isinstance(data, dict):
                        print(f"[MatchResultCog] Match {clean_id}: source=discord-archive")
                if not isinstance(data, dict):
                    status, data, retry, source = await _get_match_metadata(clean_id)
                    if status != 200 or not isinstance(data, dict):
                        if source == "local-limit":
                            explanation = f"未保存の試合です。BotのSteam問い合わせ上限に達しました。約{max(1, (retry + 59) // 60)}分後に再試行してください。"
                        elif status == 429:
                            explanation = f"APIのアクセス制限中です。約{max(1, (retry + 59) // 60)}分後に再試行してください。"
                        elif status in (404, 422, 424):
                            explanation = "試合がAPIにまだ保存されていないか、試合IDが見つかりません。"
                        elif status in (401, 403):
                            explanation = "APIへのアクセスが拒否されました。"
                        else:
                            explanation = "APIまたは通信が一時的に利用できません。"
                        await interaction.followup.send(
                            f"⚠️ マッチID `{clean_id}` の取得に失敗しました（HTTP `{status}`）。\n{explanation}"
                        )
                        return
                    # 一時ファイルに書いてから原子的に置き換える。
                    tmp_path = cache_path + ".tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                    os.replace(tmp_path, cache_path)
                    print(f"[MatchResultCog] Match {clean_id}: source={source}")

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

        brawl = is_street_brawl(info)
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

            # ブロウルは両チーム全員が同じnet_worthを持つ場合があり、
            # 通常戦と同じ「獲得ソウル」の意味として表示しない。
            line = f"{label}：`{kills}/{deaths}/{assists}`"
            if not brawl:
                line += f" ｜ 💰`{souls_k}`"

            if p.get("team") == 0:
                team_hidden_king.append(line)
            else:
                team_archmother.append(line)


        embed = discord.Embed(
            title=(f"🥊 Deadlock ストリートブロウル（Match ID: {clean_id}）"
                   if brawl else f"⚔️ Deadlock 試合リザルト（Match ID: {clean_id}）"),
            description=(
                f"**結果**: {winner_str}\n"
                f"**試合時間**: {duration_str}"
                + ("\n" + street_brawl_description(info) if brawl else "")
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

        # 先にリザルトを表示する。保管庫の障害で /match 自体を失敗させない。
        await interaction.followup.send(
            embed=embed,
            view=ReplaySaveView(clean_id),
        )

        # /match の成功を契機に全試合を保管。リプレイ寄贈操作は不要。
        # store_archived は1試合1件の重複防止・Discord添付の再取得検証・
        # SQLite登録まで実施する。成功時だけ一致するローカルJSONを削除する。
        # 保存処理はこのコマンド内で完了させ、バックグラウンドには放置しない。
        try:
            if not await store_archived(self.bot, clean_id, data):
                raise RuntimeError("保管先への保存を確認できませんでした")
            await asyncio.to_thread(delete_local_after_archive, clean_id, data)
            print(f"[MatchResultCog] Match {clean_id}: Discord非公開保管庫へ保存確認済み")
        except Exception:
            import logging
            logging.exception("Match archive failed after /match: match=%s", clean_id)
            try:
                await interaction.followup.send(
                    "⚠️ リザルトは表示できましたが、非公開保管庫への保存に失敗しました。"
                    "ローカルキャッシュは削除していません。管理者はBotのログを確認してください。",
                    ephemeral=True,
                )
            except discord.HTTPException:
                logging.exception("Could not notify archive failure: match=%s", clean_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchResultCog(bot))
