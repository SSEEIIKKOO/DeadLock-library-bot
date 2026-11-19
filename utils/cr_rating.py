"""Conservative provisional A estimator. Does not invent opponent ranks."""
from __future__ import annotations
import math
import time

STEAM64_OFFSET = 76561197960265728
RANKS=('Initiate','Seeker','Acolyte','Sentinel','Mystic','Ritualist',
       'Emissary','Oracle','Phantom','Ascendant','Eternus')

def steam_to_account_id(raw: str) -> int:
    raw=raw.strip()
    if not raw.isdecimal():
        raise ValueError('SteamID3の数値IDかSteamID64の数値IDを入力してください。')
    value=int(raw)
    if value>=STEAM64_OFFSET:
        value-=STEAM64_OFFSET
    if not 0<value<2**32:
        raise ValueError('Steam IDが不正です。SteamID3またはSteamID64を確認してください。')
    return value

def base_from_rank(rank_response: dict):
    """API rank=1..11, subrank=1..6; badge=11..116, 0 is unranked."""
    rank=rank_response.get('rank')
    sub=rank_response.get('subrank')
    badge=rank_response.get('badge',0)
    try:
        rank=int(rank); sub=int(sub)
        badge=int(badge or 0)
    except (ValueError,TypeError):
        return None,0
    if not (1<=rank<=11 and 1<=sub<=6):
        # Explicitly do NOT guess A for unranked players.
        return None,badge
    return 500+(rank-1)*180+(sub-1)*30,badge

def history_correction(entries: list) -> tuple[float, int]:
    """Score up to 20 recent public matches with an unambiguous outcome.

    Deadlock API match history supplies `player_team` and `match_result`,
    not necessarily a boolean `won`. Only team values 0 and 1 are accepted;
    incomplete/unknown results are skipped, not treated as defeats.
    """
    def stamp(e):
        for key in ('start_time', 'start_time_unix', 'match_start_time'):
            try:
                return int(e.get(key) or 0)
            except (TypeError, ValueError):
                pass
        return 0

    recent = sorted((x for x in entries if isinstance(x, dict)),
                    key=stamp, reverse=True)
    results = []
    for e in recent:
        # Exclude explicitly labeled private/bot/tutorial matches.
        mode = str(e.get('match_mode', '')).lower()
        if mode in ('private_lobby', 'coop_bot', 'tutorial'):
            continue
        won = e.get('won')
        if type(won) is bool:
            win = won
        else:
            try:
                team = int(e['player_team'])
                winner = int(e['match_result'])
            except (KeyError, TypeError, ValueError):
                continue
            if team not in (0, 1) or winner not in (0, 1):
                continue
            win = team == winner
        results.append(1.0 if win else 0.0)
        if len(results) == 20:
            break

    n = len(results)
    correction = max(-60, min(60, 240 * sum(v - 0.5 for v in results) / (n + 10))) if n else 0.0
    return round(correction, 2), n

def estimate(rank_response: dict, history: list, previous: dict | None, force: bool=False):
    base,badge=base_from_rank(rank_response)
    if base is None:
        if previous and previous['confidence']>0:
            return previous['a'],previous['confidence'],previous['badge'], 'ランク不明：前回値を維持'
        return 1250.0,0,0,'ランク未確定：暫定1250（低信頼度）'
    p,n=history_correction(history)
    proposed=float(base+p)
    old_timestamp=float(previous['updated_at']) if previous else 0
    if previous and old_timestamp and not force:
        days=max(1.0,(time.time()-old_timestamp)/86400)
        cap=90*days
        proposed=max(float(previous['a'])-cap,min(float(previous['a'])+cap,proposed))
    confidence=min(100, 50+int(n*2.5)) if n else 50
    return round(proposed,2),confidence,badge,f'ランク基礎点 {base}、確認済み勝敗 {n}/20試合、戦績補正 {p:+g}'
