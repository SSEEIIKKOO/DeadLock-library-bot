import os
from dotenv import load_dotenv

load_dotenv()

# 基本設定（すべて .env から読み込む）
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TARGET_GUILD_ID = int(os.getenv("TARGET_GUILD_ID", "0"))
TARGET_CATEGORY_ID = int(os.getenv("TARGET_CATEGORY_ID", "0"))

# テスト用制限（.env に書かなければデフォルト 0 ＝ 全キャラ表示になる）
TEST_HERO_LIMIT = int(os.getenv("TEST_HERO_LIMIT", "0"))

# Discordのドロップダウン上限
PAGE_SIZE = 25

# Deadlock 最新ランク一覧（低い順・全11ティア）
DEADLOCK_RANKS = [
    "Initiate（イニシエイト）",
    "Seeker（シーカー）",
    "Acolyte（アコライト）",
    "Sentinel（センチネル）",
    "Mystic（ミスティック）",
    "Ritualist（リチュアリスト）",
    "Emissary（エミッサリー）",
    "Oracle（オラクル）",
    "Phantom（ファントム）",
    "Ascendant（アセンダント）",
    "Eternus（エターナス）",
]
