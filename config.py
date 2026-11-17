import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", "0"))
TARGET_CATEGORY_ID = int(os.getenv("TARGET_CATEGORY_ID", "0"))

# 【テスト用設定】表示するキャラ数の上限（全キャラ戻すときは 0 に変更）
TEST_HERO_LIMIT = 3

# Discordのドロップダウン上限
PAGE_SIZE = 25

# Deadlock ランク一覧
DEADLOCK_RANKS = [
    "Initiate（イニシエイト）",
    "Seeker（シーカー）",
    "Alchemist（アルケミスト）",
    "Arcanist（アーカニスト）",
    "Ritualist（リチュアリスト）",
    "Emissary（エミッサリー）",
    "Archon（アルコン）",
    "Oracle（オラクル）",
    "Phantom（ファントム）",
    "Ascendant（アセンダント）",
    "Eternus（エターナス）",
]
