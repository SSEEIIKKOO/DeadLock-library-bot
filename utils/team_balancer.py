"""Pure-Python 6v6 CR team balancer (no Discord dependency).

Priority: find the minimum team-total CR gap; allow candidates within
minimum+60 CR; then choose the smallest secondary penalty. The 60-point
threshold is configurable and intentionally provisional.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import sqrt
from typing import Sequence


@dataclass(frozen=True)
class Player:
    name: str
    cr: int
    confidence: int = 100  # 0..100; higher means more reliable A estimate


@dataclass(frozen=True)
class BalanceResult:
    team_a: tuple[Player, ...]
    team_b: tuple[Player, ...]
    sum_a: int
    sum_b: int
    gap: int
    minimum_gap: int
    top_two_gap: int
    spread_gap: float
    uncertainty_gap: int
    penalty: float
    candidates: int


def parse_players(raw: str) -> list[Player]:
    """12 lines of `name,CR[,confidence]`, using half/full-width comma."""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 12:
        raise ValueError(f"12人分が必要です。現在は{len(lines)}人分です。")
    players = []
    for index, line in enumerate(lines, start=1):
        cols = [x.strip() for x in line.replace('，', ',').split(',')]
        if len(cols) not in (2, 3) or not cols[0]:
            raise ValueError(f"{index}行目は 名前,CR[,信頼度] の形式にしてください。")
        name = cols[0]
        if len(name) > 32:
            raise ValueError(f"{index}行目の名前は32文字以内にしてください。")
        try:
            cr = int(cols[1])
            confidence = int(cols[2]) if len(cols) == 3 else 100
        except ValueError as exc:
            raise ValueError(f"{index}行目のCRと信頼度は整数で指定してください。") from exc
        if not (0 <= cr <= 10000):
            raise ValueError(f"{index}行目のCRは0〜10000の範囲にしてください。")
        if not (0 <= confidence <= 100):
            raise ValueError(f"{index}行目の信頼度は0〜100の範囲にしてください。")
        players.append(Player(name, cr, confidence))
    if len({p.name.casefold() for p in players}) != 12:
        raise ValueError("同じ名前が重複しています。12人それぞれ異なる名前にしてください。")
    return players


def _stddev(values: Sequence[int]) -> float:
    mean = sum(values) / len(values)
    return sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def balance(players: Sequence[Player], *, gap_tolerance: int = 60) -> BalanceResult:
    if len(players) != 12:
        raise ValueError("チーム分けは12人ちょうどで実行してください。")
    if gap_tolerance < 0:
        raise ValueError("gap_toleranceは0以上にしてください。")

    # Fix member 0 in team A, avoiding mirrored duplicates: C(11,5)=462.
    candidates = []
    all_indices = set(range(12))
    for other in combinations(range(1, 12), 5):
        indices_a = (0, *other)
        indices_b = tuple(sorted(all_indices.difference(indices_a)))
        a = tuple(players[i] for i in indices_a)
        b = tuple(players[i] for i in indices_b)
        total_a = sum(p.cr for p in a)
        total_b = sum(p.cr for p in b)
        gap = abs(total_a - total_b)
        candidates.append((gap, a, b, total_a, total_b))

    minimum_gap = min(c[0] for c in candidates)
    finalists = [c for c in candidates if c[0] <= minimum_gap + gap_tolerance]

    def rate(candidate):
        gap, a, b, *_ = candidate
        top_gap = abs(sum(sorted((p.cr for p in a), reverse=True)[:2]) -
                      sum(sorted((p.cr for p in b), reverse=True)[:2]))
        spread_gap = abs(_stddev([p.cr for p in a]) - _stddev([p.cr for p in b]))
        uncertainty_gap = abs(sum(100 - p.confidence for p in a) -
                              sum(100 - p.confidence for p in b))
        # Secondary metrics normalized for a ~1000-2500 CR rating scale.
        # Top-two mismatch is weighted 2x, as discussed in requirements.
        penalty = 2.0 * top_gap / 100.0 + spread_gap / 100.0 + uncertainty_gap / 100.0
        return (penalty, gap, tuple(p.name for p in a),
                top_gap, spread_gap, uncertainty_gap)

    winner = min(finalists, key=rate)
    penalty, _, _, top_gap, spread_gap, uncertainty_gap = rate(winner)
    gap, a, b, total_a, total_b = winner
    return BalanceResult(
        team_a=a, team_b=b, sum_a=total_a, sum_b=total_b,
        gap=gap, minimum_gap=minimum_gap, top_two_gap=top_gap,
        spread_gap=spread_gap, uncertainty_gap=uncertainty_gap,
        penalty=penalty, candidates=len(finalists),
    )
