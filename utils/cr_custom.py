"""Custom-match B: zero-sum allocations and independent, per-match decay.

Trial parameters (not Deadlock's official rating):
- expected win probability logistic on team mean CR, scale 400
- total winner gain = 120 * (1 - winning team's expected probability)
- shares within each team favor underdogs on a win / stronger players on a loss
- cap live B at +/-100 by shrinking the entire match's allocation
- match event stays unchanged; evaluation uses 7-day grace, 14-day half-life
"""
from __future__ import annotations
import math
import time

GRACE_SECONDS = 7 * 86400
HALF_LIFE_SECONDS = 14 * 86400
MAX_B = 100.0


def factor(created_at: float, now: float | None = None) -> float:
    now = time.time() if now is None else now
    elapsed = max(0.0, now - float(created_at) - GRACE_SECONDS)
    return 2 ** (-elapsed / HALF_LIFE_SECONDS)


def individual_delta(initial: float, created_at: float, now: float | None = None) -> float:
    result = float(initial) * factor(created_at, now)
    return result if abs(result) >= 1.0 else 0.0


def b_from_events(events: list[dict], now: float | None = None) -> float:
    return sum(individual_delta(e['delta'], e['created_at'], now) for e in events)


def allocate(team_a: list[dict], team_b: list[dict], winner: str,
             current_b: dict[int, float]) -> tuple[dict[int, float], float, float]:
    if winner not in ('A', 'B') or len(team_a) != 6 or len(team_b) != 6:
        raise ValueError('A/B各6人、勝利側A/Bを指定してください。')
    members = team_a + team_b
    ids = [int(r['discord_id']) for r in members]
    if len(set(ids)) != 12:
        raise ValueError('参加者12人は重複できません。')
    cr = {int(r['discord_id']): float(r['a']) + float(current_b[int(r['discord_id'])]) + float(r['c']) for r in members}
    mean_a = sum(cr[int(x['discord_id'])] for x in team_a) / 6
    mean_b = sum(cr[int(x['discord_id'])] for x in team_b) / 6
    # Numerically stable expected win probability from mean difference.
    diff = max(-4000.0, min(4000.0, mean_a - mean_b))
    expected_a = 1.0 / (1.0 + 10 ** (-diff / 400.0))
    winner_expected = expected_a if winner == 'A' else 1 - expected_a
    pool = 120.0 * (1 - winner_expected)

    def shares(team, won):
        values = [cr[int(x['discord_id'])] for x in team]
        average = sum(values) / len(values)
        # +/-20%, normalized to sum=1. Higher rating loses more, lower gains more.
        raw = [max(.8, min(1.2, 1 + ((average - x) if won else (x - average)) / 750.0)) for x in values]
        total = sum(raw)
        return {int(row['discord_id']): weight / total for row, weight in zip(team, raw)}

    delta = {}
    for team, won in ((team_a, winner == 'A'), (team_b, winner == 'B')):
        for did, share in shares(team, won).items():
            delta[did] = pool * share * (1 if won else -1)

    # Common scaling maintains strictly zero-sum when near B caps.
    scale = 1.0
    for did, d in delta.items():
        now_b = float(current_b[did])
        if d > 0:
            scale = min(scale, max(0.0, (MAX_B - now_b) / d))
        elif d < 0:
            scale = min(scale, max(0.0, (-MAX_B - now_b) / d))
    result = {did: d * scale for did, d in delta.items()}
    if abs(sum(result.values())) > 1e-7:
        raise AssertionError('Zero-sum allocation failed')
    return result, pool * scale, expected_a
