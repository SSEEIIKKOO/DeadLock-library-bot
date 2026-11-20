"""Separate production main-community operations from the existing library guild.

MAIN_GUILD_ID is intentionally optional for backwards compatibility. Set it
before production deployment to prevent the old test guild from being targeted.
"""
import os

def main_guild_id() -> int:
    try:
        return int(os.getenv("MAIN_GUILD_ID", "0") or "0")
    except ValueError:
        return 0

def is_main_guild(guild_id: int | None) -> bool:
    target = main_guild_id()
    return target == 0 or (guild_id is not None and guild_id == target)
