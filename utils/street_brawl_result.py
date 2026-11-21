"""Strict Street Brawl match-info parsing, using verified Deadlock API samples.

Do not infer brawl from the player count: only explicitly tagged game_mode=4.
Keep mode-independent match archive as-is.
"""
from __future__ import annotations


def is_street_brawl(info: dict) -> bool:
    """Return True only when metadata explicitly identifies Street Brawl."""
    if not isinstance(info, dict):
        return False
    value = info.get("game_mode")
    # bool is an int subclass; reject it to avoid accidental classification.
    return type(value) in (str, int) and str(value) == "4"


def street_brawl_description(info: dict) -> str:
    """Format confirmed score and round winners, gracefully handling partial data."""
    result = []
    score = info.get("team_score")
    if isinstance(score, list) and len(score) == 2 and all(type(x) is int and x >= 0 for x in score):
        result.append(f"**ラウンドスコア**: 🟠 {score[0]} – {score[1]} 🔵")
    rounds = info.get("street_brawl_rounds")
    if isinstance(rounds, list) and rounds:
        outcomes = []
        for i, entry in enumerate(rounds, 1):
            if not isinstance(entry, dict):
                outcomes.append(f"R{i}:不明")
                continue
            winner = entry.get("winning_team")
            winner_label = "🟠" if winner == 0 else "🔵" if winner == 1 else "不明"
            outcomes.append(f"R{i}:{winner_label}")
        result.append("**各ラウンド**: " + " / ".join(outcomes))
    if not result:
        result.append("**ラウンド情報**: APIから取得できませんでした")
    return "\n".join(result)
