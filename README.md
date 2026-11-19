# DeadLock Library Bot

Deadlockコミュニティと専用の図書館Discordサーバーを連携する、Python製のDiscord Botです。ヒーロービルドの共有、試合リザルトの表示、リプレイの図書館登録、使い捨てVC、カスタムマッチ向けのカスタムレート（CR）管理をまとめています。

> **開発・試験運用中**：本READMEは提供されたファイルと修正パッチで確認したプロジェクト構成と、2026年11月時点のローカル最新版（CR管理・カスタム募集統合版、および通知修正v3）をもとに記載しています。GitHubの公開ブランチには新しい変更がまだ反映されていない場合があります。Deadlock APIやDiscord側の仕様変更により、一部機能は動作しなくなる可能性があります。

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
| カスタムマッチ募集 | 全員が募集開始可能な常設パネル、12人でチーム分け、募集主による確定、待機VCからの移動、手動勝敗確定 |

## カスタムレート（CR）

CRは次の3要素を合算して計算します。

```text
CR = A（公式戦評価） + B（カスタム戦績） + C（管理者補正）
```

- **A**：Deadlock APIのランク情報と直近の試合履歴をもとに評価。原則24時間ごとに更新します。情報が足りない場合は暫定評価になります。
- **B**：カスタムマッチの勝敗から、チームの期待勝率と個人のCRを考慮して配分します。基本はゼロサム方式で、各試合の獲得・減少ポイントは7日後から減衰し、14日で半減します。
- **C**：管理者が例外的な実力差を補正するための値です。

カスタム募集パネルから参加受付・チーム分け・VC移動まで対応しています。**試合の勝敗は管理者が手動で確認・確定**します。Deadlock APIによる勝敗自動判定は採用していません。

## カスタムマッチ募集システム

専用テキストチャンネルに**常設の募集パネル**を設置します。募集のたびに新しいパネルを作らず、1件の募集状態を更新します。

1. 全メンバーが「募集開始」を押す（募集主は参加者として自動登録）。同時募集は1件だけです。
2. 参加・辞退ボタンで12人（6対6）を集めます。募集主が辞退・サーバーを退出した場合、最も早く登録した残りの参加者に権限を譲渡します。
3. 12人集まるとCRで自動チーム分け。CR合計差を最優先し、上位戦力・CR分布なども評価します。募集主は条件を満たす別候補へ**最大3回**変更できます。
4. 募集主がチームを確定すると、**待機・観戦VCにいる参加者だけ**を固定のチームVCへ移動します。ほかのVCにいる人・未接続者には移動先を案内します。
5. 募集主が試合終了を申請すると**直ちに募集枠を解放**します。勝敗未確定の履歴は残り、管理者が後で結果を確定します。
6. 管理者が `/custom_results` で未確定試合を確認し、`/custom_result` で勝利チームまたは無効試合を確認・確定すると、必要に応じてカスタム戦績のCRへ反映されます。

| 状態 | 自動終了ルール |
| --- | --- |
| 募集中 | 参加人数の変化が60分間ない場合、募集終了 |
| 12人到達後 | 募集主が15分以内にチームを確定しない場合、募集終了 |
| 試合進行中 | 開始から3時間で募集枠を解放。未確定の結果履歴は保持し、CRは自動変動させない |

参加者・試合状態・確定時のCRはSQLiteに記録します。Botの再起動後も復旧する設計です。結果登録は開催ID（`CUSTOM-000001` など）で識別し、二重登録を防ぎます。

### 常設パネルと通知

Bot起動時、カスタム募集・図書館受付・自己紹介のパネルを再設置する仕組みを追加しています。使用中の使い捨てVC管理パネルも対象です。旧パネルの自動削除にはBotの閲覧・履歴取得・メッセージ管理などの権限が必要な場合があり、削除できず残るときは手動確認してください。

**カスタム募集パネルの一時通知は約20秒後に削除されることを実機確認済み**です。CR関連にも同じ一時通知処理を導入しましたが、ほかの全機能への統一は未完了です。操作中の勝敗確認画面など、自動削除の対象外となるメッセージもあります。

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
| `/custom_setup` | 常設カスタム募集パネルの設置・修復（管理者用） |
| `/custom_results` | 結果が未確定のカスタム試合一覧（管理者用） |
| `/custom_result` | カスタム試合の勝敗をプレビュー・確定（管理者用） |

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
│   ├── cr_manager.py
│   └── custom_match.py
├── utils/
│   ├── gdrive_uploader.py
│   ├── match_archive.py
│   ├── replay_index.py
│   ├── team_balancer.py
│   ├── cr_rating.py
│   ├── cr_store.py
│   ├── cr_archive.py
│   ├── cr_custom.py
│   ├── custom_match_store.py
│   ├── custom_match_balance.py
│   ├── interaction_notices.py
│   └── panel_reinstall.py
└── data/                 # 実行時に作成されるDB・キャッシュ（Git管理対象外）
```

`bot.py` は `cogs` 以下の拡張モジュールを読み込みます。上記は、カスタム募集統合版と永続パネル修正パッチを適用したローカル構成の例です。GitHubにまだ反映されていないファイルがある場合があります。

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

# カスタム募集の常設パネルと固定VC
CUSTOM_PANEL_CHANNEL_ID=YOUR_CUSTOM_PANEL_TEXT_CHANNEL_ID
CUSTOM_WAITING_VC_ID=YOUR_CUSTOM_WAITING_VOICE_CHANNEL_ID
CUSTOM_TEAM_A_VC_ID=YOUR_CUSTOM_TEAM_A_VOICE_CHANNEL_ID
CUSTOM_TEAM_B_VC_ID=YOUR_CUSTOM_TEAM_B_VOICE_CHANNEL_ID

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
- **カスタム募集・試合記録**：ローカルSQLiteに参加者、開催状態、チーム分け、CRスナップショット、勝敗未確定履歴を保存します。

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
- [x] 常設カスタム募集パネル・CRチーム分け・待機VCからの移動（試験版）
- [x] 管理者による勝敗の手動確定と結果待ち履歴の管理（試験版）
- [x] 永続パネルの再設置処理（試験版）
- [ ] 一時通知の20秒自動削除をその他のコマンドにも統一
- [ ] カスタム募集を複数人で通し検証・運用改善
- [ ] CRの実戦データに基づく継続的な調整
- [ ] 自動テスト・依存関係固定・バックアップ運用の強化

---

本プロジェクトはコミュニティ向けの非公式ツールです。ValveおよびDeadlock公式とは関係ありません。
