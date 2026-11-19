"""Rank up to four distinct fair teams; no external dependencies beyond team_balancer.Player."""
from __future__ import annotations
from itertools import combinations
from math import sqrt
from utils.team_balancer import Player


def candidates(players: list[Player], tolerance: int = 60) -> list[dict]:
    if len(players) != 12 or len({p.name for p in players}) != 12:
        raise ValueError('登録済みの異なる12人が必要です。')
    all_idx = set(range(12))
    options = []
    for five in combinations(range(1,12),5):
        a_idx=(0,*five)
        b_idx=tuple(sorted(all_idx.difference(a_idx)))
        a=[players[i] for i in a_idx]
        b=[players[i] for i in b_idx]
        gap=abs(sum(x.cr for x in a)-sum(x.cr for x in b))
        options.append((gap,a,b))
    min_gap=min(x[0] for x in options)

    def std(team):
        avg=sum(p.cr for p in team)/6
        return sqrt(sum((p.cr-avg)**2 for p in team)/6)

    def score(option):
        gap,a,b=option
        top=abs(sum(sorted((p.cr for p in a),reverse=True)[:2])-sum(sorted((p.cr for p in b),reverse=True)[:2]))
        spread=abs(std(a)-std(b))
        unknown=abs(sum(100-p.confidence for p in a)-sum(100-p.confidence for p in b))
        penalty=2*top/100+spread/100+unknown/100
        return penalty,gap,tuple(p.name for p in a)

    eligible=sorted((o for o in options if o[0]<=min_gap+tolerance),key=score)
    return [{'a':[int(p.name) for p in a], 'b':[int(p.name) for p in b],
             'sum_a':sum(p.cr for p in a), 'sum_b':sum(p.cr for p in b),
             'gap':gap, 'minimum_gap':min_gap, 'penalty':score((gap,a,b))[0]}
            for gap,a,b in eligible[:4]]
