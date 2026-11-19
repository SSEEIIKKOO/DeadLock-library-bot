# DeadLock Library Bot

めんどくさいからREADMEは全部AIにかかせてるよん

Deadlockコミュニティと専用の図書館Discordサーバーを連携する、Python製のDiscord Botです。ヒーロービルドの共有、試合リザルトの表示、リプレイの図書館登録、使い捨てVC、カスタムマッチ向けのカスタムレート（CR）管理をまとめています。

> **開発・試験運用中**：本READMEは手元で確認したプロジェクト構成と、2026年11月時点で追加したCR関連コード（v0.4）をもとに記載しています。GitHubの公開ブランチには新しい変更がまだ反映されていない場合があります。Deadlock APIやDiscord側の仕様変更により、一部機能は動作しなくなる可能性があります。

## 主な機能

| 機能 | 概要 |
| --- | --- |
| ビルド図書館 | ヒーロー別ビルドの投稿・検索、受付パネル |
| 試合リザルト | マッチIDから試合結果を表示。取得済みデータを優先して再利用 |
| リザルト保管庫 | 図書館サーバーの非公開チャンネルへ、試合データを圧縮JSONで保存 |
| リプレイ図書館 | Google Driveへのリプレイ保存、キャプション付きフォーラム投稿、検索 |
| 使い捨てVC | 指定VCへの入室で自動生成。オーナー管理、ロック、人数設定、空室時の自動削除 |
| カスタムレート（CR） | ランク・直近戦績による評価A、カスタム戦績B、管理者補正Cを合算 |
| 自動チーム分け | CRに基づく6対6の組み合わせ探索・バランス評価 |

## カスタムレート（CR）

CRは次の3要素を合算して計算します。

```text
CR = A（公式戦評価） + B（カスタム戦績） + C（管理者補正）
```

- **A**：Deadlock APIのランク情報と直近の試合履歴をもとに評価。原則24時間ごとに更新します。情報が足りない場合は暫定評価になります。
- **B**：カスタムマッチの勝敗から、チームの期待勝率と個人のCRを考慮して配分します。基本はゼロサム方式で、各試合の獲得・減少ポイントは7日後から減衰し、14日で半減します。
- **C**：管理者が例外的な実力差を補正するための値です。

現在のカスタム勝敗登録は**管理者による手動確定**です。参加募集から試合終了までの完全自動化は今後の拡張候補です。

## 主なスラッシュコマンド

| コマンド | 用途 |
| --- | --- |
| `/match` | マッチIDから試合リザルトを表示 |
| `/match_archive_backfill` | 既存の試合キャッシュを非公開保管庫へ移行（管理者用） |
| `/replay` | 保存済みリプレイを検索 |
| `/post_build` | ビルドを投稿 |
| `/search_build` | ビルドを検索 |
| `/setup_desk` | ビルド受付パネルを設置（管理者用） |
| `/setup_intro` | 自己紹介・Steam ID連携パネルを設置（管理者用） |
| `/team_split` | CRを手入力して6対6のチーム分けを試す |
| `/cr` | Steam IDを自動照合し、CRを更新・表示。未登録なら入力フォームを表示 |
| `/cr_register` | Steam IDをCRに登録 |
| `/cr_update` | 公式戦評価Aを更新 |
| `/cr_view` | CRの内訳を表示 |
| `/cr_adjust` | 管理者が補正Cを変更 |
| `/cr_restore` | CR保管庫からデータを復元（管理者用） |
| `/cr_split` | 登録済みの12人からCRでチーム分け |
| `/cr_custom_result` | 管理者がカスタム試合の勝敗を登録し、Bを更新 |

コマンドの引数・権限はDiscordのコマンド候補画面を参照してください。

## ディレクトリ構成

```text
DeadLock-library-bot/
├── bot.py
├── config.py
├── cogs/
│   ├── build_post.py
│   ├── build_search.py
│   ├── library_desk.py
│   ├── match_result.py
│   ├── replay_search.py
│   ├── profile_intro.py
│   ├── voice_manager.py
│   ├── team_balance.py
│   └── cr_manager.py
├── utils/
│   ├── gdrive_uploader.py
│   ├── match_archive.py
│   ├── replay_index.py
│   ├── team_balancer.py
│   ├── cr_rating.py
│   ├── cr_store.py
│   ├── cr_archive.py
│   └── cr_custom.py
└── data/                 # 実行時に作成されるDB・キャッシュ（Git管理対象外）
```

`bot.py` は `cogs` 以下の拡張モジュールを読み込みます。上記はCR v0.4を含むローカル構成の例です。

## セットアップ

### 1. 必要な環境

- Python 3.10以降を目安にした環境（コードの型注釈・依存ライブラリに応じて調整）
- Discord Botアプリケーションと、Botを導入したDiscordサーバー
- 試合・ランク情報を取得するためのDeadlock APIへの接続
- リプレイ保存を使う場合はGoogle Drive APIのOAuth設定

このリポジトリには依存関係の固定ファイルが整備されていない場合があります。主に `discord.py`、`aiohttp`、`python-dotenv`、`google-api-python-client`、`google-auth-oauthlib`、`google-auth-httplib2` などを利用しています。利用するファイルのimportに応じて導入してください。

```bash
python -m pip install -U discord.py aiohttp python-dotenv google-api-python-client google-auth-oauthlib google-auth-httplib2
```

### 2. `.env` を設定

プロジェクト直下に `.env` を作成し、使う機能に必要な項目を設定します。次の数値はすべて**例示用のプレースホルダー**です。

```dotenv
DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
TARGET_GUILD_ID=YOUR_LIBRARY_GUILD_ID
TARGET_CATEGORY_ID=YOUR_BUILD_CATEGORY_ID
REPLAY_FORUM_CHANNEL_ID=YOUR_REPLAY_FORUM_ID

# 試合結果の非公開保管先
MATCH_DATA_STORAGE_CHANNEL_ID=YOUR_MATCH_STORAGE_CHANNEL_ID

# CRの非公開保管先
CR_DATA_STORAGE_CHANNEL_ID=YOUR_CR_STORAGE_CHANNEL_ID

# 使い捨てVCの作成トリガー
TEMP_VC_ENJOY_CREATE_ID=YOUR_ENJOY_CREATE_VC_ID
TEMP_VC_SERIOUS_CREATE_ID=YOUR_SERIOUS_CREATE_VC_ID
TEMP_VC_OTHER_CREATE_ID=YOUR_OTHER_CREATE_VC_ID

# 任意：使い捨てVCの操作ログ
TEMP_VC_LOG_CHANNEL_ID=
```

不要な機能の設定は、該当Cogを使わない場合に限り省略できます。`config.py` が参照する項目と実際のサーバー構成を照合してください。

### 3. Botの権限・Intent

Botが使う機能に応じて、チャンネル閲覧、メッセージ送信、履歴閲覧、ファイル添付、チャンネル管理、メンバー移動などの権限を付与してください。非公開保管庫は一般ユーザーからの閲覧を禁止し、Botと管理者のみアクセス可能にします。

`bot.py` のIntentsに `voice_states` と `members` が必要です。`members` を使用する際はDiscord Developer Portalの **Server Members Intent** も有効にしてください。既存のBot機能に必要なIntentsは維持します。

### 4. 起動

```bash
python bot.py
```

起動ログで各Cogの読み込みを確認し、まずはテスト用の試合ID・VC・CR登録で動作確認してください。

## データの保存先

- **試合リザルト**：取得成功時に図書館サーバーの非公開チャンネルへ圧縮JSONを保存し、再検索時に再利用します。未保存分はローカルキャッシュを使う場合があります。
- **リプレイ**：Google Driveへ保存し、図書館フォーラムにキャプションとリンクを投稿します。
- **CR**：ローカルSQLiteを計算・検索用に使い、非公開Discordチャンネルへ保存・復元できるようにします。
- **カスタム戦績B**：試合ごとのポイント変動を履歴として保存し、経過日数に応じて減衰を計算します。

**Discordの非公開チャンネルはバックアップそのものではありません。** チャンネルや投稿が消えるとデータを失う可能性があります。SQLiteなどの重要データは別途バックアップしてください。

## セキュリティ・注意事項

- `.env`、`token.json`、`client_secret.json`、`chrome_bot_profile/`、`data/`、`.git/` を配布ZIPや公開コミットに含めないでください。
- Discord BotトークンやGoogle OAuth認証ファイルを誤って公開した場合は、適切な失効・再発行を行ってください。
- Deadlock APIはエンドポイントやデータ取得経路ごとにレート制限があります。連続再試行を避け、保存済みデータを優先します。
- CRの公式戦評価・戦績補正はコミュニティ用の独自指標です。Deadlock公式のMMRを再現・保証するものではありません。
- Bの勝敗登録、チーム分け、Aの計算は引き続き試験運用中です。実際の試合結果を見て調整してください。

## 開発状況と今後の予定

- [x] 試合結果の検索とDiscordへの自動保管
- [x] リプレイ図書館との連携
- [x] 使い捨てVCの基本管理
- [x] CRの表示、公式戦評価A、管理者補正C
- [x] カスタム戦績Bの計算・時間減衰（試験版）
- [x] CRを使った6対6自動チーム分け（試験版）
- [ ] カスタム参加募集パネル・VC連携
- [ ] カスタム試合結果の自動確定
- [ ] CRの実戦データに基づく継続的な調整
- [ ] 自動テスト・依存関係固定・バックアップ運用の強化

---

本プロジェクトはコミュニティ向けの非公式ツールです。ValveおよびDeadlock公式とは関係ありません。
