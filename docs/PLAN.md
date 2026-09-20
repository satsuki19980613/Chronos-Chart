# Chronos Chart 実装計画書 / 進捗管理

| 項目 | 内容 |
|---|---|
| 版 | 1.1 |
| 作成日 | 2026-09-20 |
| 最終更新 | 2026-09-20 |
| 関連文書 | [SPEC.md](SPEC.md) 仕様（**正**） / [REVIEW_RESULT.md](REVIEW_RESULT.md) 設計レビュー / [DESIGN.md](DESIGN.md) 設計方針 / [RESEARCH.md](RESEARCH.md) 調査結果 |

> **RESEARCH.md と DESIGN.md はレビュー前の記述を含み、一部に事実誤認がある**（各ファイル冒頭の訂正表を参照）。
> 実装の根拠には必ず SPEC.md を使うこと。

---

## 0. このファイルの使い方（セッション開始時に必ず読む）

本プロジェクトは複数のセッションに分かれて実装される。
**新しいセッションを始めたら、まず §1「現在地」と §4「申し送り事項」を読むこと。**

### セッション開始時の手順

1. §1「現在地」で、直前のセッションがどこまで進めたかを把握する
2. §4「申し送り事項」で、引き継ぎ事項・注意点を確認する
3. §5「未解決事項」で、判断待ちの項目がないか確認する
4. §2 のタスク表から、状態が `TODO` かつ依存がすべて `DONE` の最小IDのタスクを選ぶ（`WIP` があればそれを先に片付ける）
5. [SPEC.md](SPEC.md) の該当セクションを読んでから着手する

### 作業の進め方 — メインは指揮、実装は Sonnet のサブエージェント（ユーザー指示・2026-09-20）

**実装やテスト作成などの細かい作業は、Sonnet のサブエージェントを複数起動して任せる。メインのモデルは指揮に回る。**
（Agent ツールで `model: "sonnet"` を指定する。）

メイン（指揮）がやること:

- PLAN / SPEC を読み、次のタスクを選び、**並行できる単位に分けて**サブエージェントへ依頼文を書く
- 仕様の判断・変更、SPEC / PLAN / CLAUDE.md の更新
- 成果物のレビュー（差分を SPEC と §4 の不変条件に照らす）、`.venv\Scripts\python.exe -m pytest` の全件実行、必要なら開発サーバーでの画面確認
- コミット・マージ・push（サブエージェントにはさせない）
- **外部サイトへのアクセスを伴う作業**（実データの採取など「1回だけ」の制約があるもの）。回数を管理するためメインが自分で行う

サブエージェント（Sonnet）に任せること:

- モジュールの実装とそのテスト、合成フィクスチャの作成、画面の実装
- 依頼文には必ず次を入れる: (1) 対応する SPEC のセクション番号と要点、(2) 触ってよいファイルの範囲、(3) CLAUDE.md の不変条件のうち関係するもの、
  (4) テストの実行方法（`.venv`・ネットワーク禁止）、(5) **コミットしない・外部サイトへアクセスしない**こと、(6) 完了時に報告してほしい内容（変更ファイル・テスト結果・仕様と違えた点）

並行させるときの注意:

- **同じファイルを複数のサブエージェントに触らせない。** 特に `app/database.py` の `MIGRATIONS`、`app/api.py`、`web/index.html`、`web/js/app.js` は衝突しやすい。
  テーブル追加の移行（バージョン番号の割り当て）や API メソッドの骨組みは、**メインが先に入れてから**実装を分担させる。必要なら worktree 分離を使う
- P2 の例: P2-1・P2-3（実物の採取と合成フィクスチャの方針決め）はメイン → その後 **P2-2（karauri パーサ）と P2-4（日証金パーサ）を並行** → P2-5 → P2-6・P2-7
- サブエージェントの報告は鵜呑みにせず、メインがテストを実行し差分を読んで確認する

### タスク完了時の手順（必須・セッション終了時ではなく**タスクごと**）

セッションは予告なく終わることがある。終了時にまとめて更新する運用にしない。

1. タスクの状態列を `DONE` にする（**進捗の正はタスク表の状態列だけ**。件数やチェックボックスを別に持たない）
2. §1「現在地」の「次にやること」を書き換える
3. 仕様を変えた・決めた場合は **SPEC.md 本文と SPEC §12 変更履歴（何を・なぜ）** を更新する。設計判断は §6「決定記録」にも1行残す
4. 引き継ぐべき注意点があれば §4 に追記する
5. **これらの更新を、そのタスクの実装と同じコミットに含める**

### セッションの区切りで行うこと

- §3「セッションログ」に1行追記する
- 作業途中のタスクは状態を `WIP` にし、残作業を §4 に書く

### 状態の凡例

| 記号 | 状態 | 意味 |
|---|---|---|
| `TODO` | 未着手 | まだ手をつけていない |
| `WIP` | 作業中 | 着手済み。残作業を §4 に書く |
| `DONE` | 完了 | 完了条件をすべて満たし、テストが通っている |
| `BLOCKED` | 停止 | 依存または判断待ちで進められない。理由を §5 に書く |
| `SKIP` | 見送り | 実施しないと決めた。理由を備考に書く |

---

## 1. 現在地

> **このセクションは常に最新の状態に保つこと。**

| 項目 | 内容 |
|---|---|
| **現在のフェーズ** | P4（開示の取得・突合・分類） |
| **次にやること** | **P8 は完了。次は P6-1（Gemini クライアント）。** SPEC §2.7・§9-4。`google-genai` のラッパ、`count_tokens`、**SDK の `retry_options` を設定しない**。**実物を1回採取して** `usage_metadata` のフィールド名・429 の詳細（`QuotaFailure.quotaId` / `RetryInfo`）・thinking 予算の指定方法を確認し §5 の #3 を解消する（外部アクセスを伴うのでメインが自分で行う。Gemini の API キーはユーザーが設定タブに登録済み）。そのあと P6-2〜P6-6 →  P7-2（通しの動作確認） |
| **リポジトリ状態** | **`main` に `feature/p5-events` と `feature/p7-readme` をマージし、P5・P7-1 完了時点まで push 済み**（`60bb419`）。次の作業は `main` から新しいブランチを切って始める。※ P3 は `feature/p2-supply` の続きとしてコミットしてある（ブランチ名と中身がずれているが、マージ済みなので追わない）。コミットのメールアドレスはリポジトリ設定で GitHub の noreply アドレスにしてある（個人アドレスだと GitHub が push を拒否する） |
| **外部アクセスの消費** | 2026-09-20 に採取済み: karauri.net `/6920/` を1回、`taisyaku.jp` の `zandaka.csv` `meigara.csv` を各1回、**EDINET の利用規約ページ（閲覧）と `Edinetcode.zip` を各1回**。実物は `tests/fixtures/real/`（Git 対象外）。**以後これらへはアクセスしない**。2026-09-20 に API キー取得後、**EDINET API v2 の `documents.json` を31回**（疎通確認1 + 実測30日分）。これは API の正規の使い方なので回数制限は無いが、1秒間隔は守ること。karauri の User-Agent の連絡先はユーザー指定でリポジトリ URL `https://github.com/satsuki19980613/Chronos-Chart` |
| **動作確認** | 2026-09-20、P8 の修正後に `.venv\Scripts\python.exe -m pytest` は **671 passed / 15 skipped**。**ユーザーの実データ（`data/`。事前にバックアップ）に対して実 API で通しの確認を行った**: コードリストを取得して `edinet_codes` が **11,386 行**（`99840` → `E02778` ソフトバンクグループ）、開示ジョブが **366日分を取得（書類あり 242日）・エラー0**、`disclosures` に **26件**（有報・半期報 2／**需給関連 5**／その他 19）が 9984.T に紐づいた。**修正前は 0 件で、とくに大量保有報告書（`issuerEdinetCode` 突合）は構造上1件も入らなかった。** 実データのコピーを開発サーバーで開き、チャートの開示マーカー（臨報・大量保有・開示2件/3件のまとめ）とイベント欄（26件・提出事由の表示）を目視確認。karauri も**実サイトへ1リクエストだけ**行い、9984.T で 20 行を取得・`short_totals` まで算出できることを確認した（このとき備考 `ポジション解消` を観測 → P8-5）。空売りの導線（連絡先未設定の理由表示・設定後にボタンが有効になること）と、登録直後の開示取得ダイアログ（367日分・約6分・範囲 2025-09-18〜2026-09-20）も開発サーバーで確認。以前の記録: P5 完了時点で `.venv\Scripts\python.exe -m pytest` は **660 passed / 15 skipped**。**合成データを入れた開発サーバーでイベント欄を確認**（右カラムに「開示イベント」パネル、「表示範囲 9件 ／ 全 10件」、TDnet 対象外と EDINET 出典・加工主体の2行、提出日時＋種別バッジ＋概要＋提出事由＋`issuer` の提出者名、取下げ行が薄くバッジ付き、未来日の行に「株価の足がまだありません」、期間より前の1件は表示範囲外で出ない）。**表示範囲との連動**（「3ヶ月」で 5件に絞られる）、**マーカークリック**（臨報のマーカーを実際にクリックすると 2026-08-17 の行が強調される）、**行クリックでのスクロール**（2026-07-20 の行をクリックするとチャートが移動し、一覧が 2026-06-15 を含む並びに入れ替わる）を確認。`dashboard` を開いても `get_disclosures` が呼ばれなくなった（重複呼び出しの解消）ことを通信ログで確認。以前の記録: P5-3 完了時点で `.venv\Scripts\python.exe -m pytest` は **658 passed / 15 skipped**。**合成データを入れた開発サーバーで開示マーカーを目視確認**（同日3件が「開示3件」にまとまる／土曜提出が翌月曜の足に寄る／取下げ・未来日・期間より前の開示はマーカーが出ない／`supply` は橙の上向き矢印 belowBar・`report` は水色の丸・`other` は灰の四角／シグナルのチップを OFF にしても開示マーカーが残り、その逆も成り立つ）。チップ設定の移行も確認（`{overlays:['signals']}` の古い保存値から読み込むと、開示だけが ON で足され、ユーザーが OFF にした移動平均は OFF のまま）。種データは `scratchpad/seed.py`、`.claude/launch.json` の `CHRONOS_DATA_DIR` は本セッションのスクラッチ領域に付け替えた（`auto_update_on_start=0` も種データに入れてある）。以前の記録: P4 完了時点で `.venv\Scripts\python.exe -m pytest` は **602 passed / 15 skipped**（skip は実通信テストのみ）。**開発サーバーで実 API を使って画面を確認済み**（開示を取得→トースト「開示 3日分を取得（うち書類あり 1日）・開示 3件を登録」、ダッシュボードの件数「開示 3件（有報・半期報 1／需給関連 0／その他 2）」、開示ゼロの銘柄で「この銘柄の開示はまだありません（EDINET は 3 日分取得済み）」、設定タブの接続テスト「EDINET に接続できました（2026-09-18 の書類 402 件）」、2回目の「開示を取得」が確定済みを除いて当日1日分だけになること）。**起動時の自動更新も実 API で確認**し、トーストが SPEC §2.8.2 の例どおりの並び「株価 取得済み／日証金 取得済み／EDINET 3日分・3件登録／空売り スキップ（設定オフ）」になることを確認。**EDINET API の疎通をユーザーの実キーで確認済み**（資格情報ストアから読めること、`documents.json` が 200 を返すこと）。実データ30日分5,123件でパーサを検証し、`issuerEdinetCode` が 350/360 のみ・`subjectEdinetCode` が 240〜320 のみに入ることを確認。P4-2 は外部アクセス無しで、ジョブ登録（`start_job('disclosures')` がキー未設定で `UserFacingError` を返し、キャッシュフォルダを作らないこと）と `CHRONOS_DATA_DIR` 配下に `edinet_cache/` が解決されることを一時フォルダで確認。開発サーバーで需給の取得UI（連絡先未設定で一括取得が無効／設定後に有効／再取得抑止が効いて「取得が必要な銘柄はありません」／ダッシュボードの2ボタンと注記）を、**外部アクセス無し・一時データフォルダ**で確認。`auto_update_on_start` をオフにした起動で、JS からのジョブ開始が即座に skipped で終わり外部通信が発生しないことも確認。P1 完了時点では pywebview のウィンドウ（`start.bat`）での起動をユーザーが確認済み（「全て問題ない」） |

### フェーズの状態

| フェーズ | 内容 | 状態 |
|---|---|---|
| P0 | 準備・設計文書・レビュー反映 | DONE |
| P1 | 基盤（設定・HTTPクライアント・ジョブ・**起動時自動更新**） | DONE |
| P2 | 需給データの取得 | DONE |
| P3 | 需給のチャート表示 | DONE |
| P4 | 開示の取得・突合・分類 | DONE |
| P5 | イベントマーカーとイベント欄 | DONE |
| P6 | AI分析レポート | TODO |
| P7 | 仕上げ | WIP（P7-1 完了） |
| P8 | 実機テストで見つかった不備の修正 | DONE |

---

## 2. タスク

各タスクは**単独で完了でき、完了後もアプリが動く**粒度にしてある。
1セッションで複数タスクを進めてよい。

### P0 — 準備・設計文書

| ID | 状態 | タスク | 完了条件 | 依存 |
|---|---|---|---|---|
| P0-1 | `DONE` | 土台リポジトリのクローンとリモート設定 | `origin` が Chronos-Chart、`upstream-autotechnical` が元リポジトリ | — |
| P0-2 | `DONE` | ネットリサーチと調査結果の文書化 | `docs/RESEARCH.md` | P0-1 |
| P0-3 | `DONE` | 設計方針・仕様書・実装計画の作成 | `docs/DESIGN.md` `docs/SPEC.md` `docs/PLAN.md` | P0-2 |
| P0-4 | `DONE` | fable によるレビューと指摘の反映 | `docs/REVIEW_RESULT.md`。全指摘の反映先を §7 に記録。SPEC/PLAN を 1.1 に更新。RESEARCH/DESIGN に訂正表を追加 | P0-3 |
| P0-5 | `DONE` | 初期コミットと push | Chronos-Chart に土台＋docs が push されている。※ユーザーの指示により P0-6 より先に実施（2026-09-20）。実データのフィクスチャはまだ存在しないため支障なし | P0-4 |
| P0-6 | `DONE` | リポジトリの整備（**実データを採取する P2-1・P2-3 より前に必須**） | (1) `.gitignore` に `tests/fixtures/real/` を追加。(2) `web/vendor/NOTICE-lightweight-charts.txt` を追加（配布元 v5.2.1 タグの NOTICE 実物とバイト一致。著作権年は 2025）。(3) リポジトリ直下に `CLAUDE.md` を置き、「最初に `docs/PLAN.md` §0・§1・§4 を読む」と §4 の不変条件を書く | P0-4 |

### P1 — 基盤

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P1-1 | `DONE` | プロジェクト名の変更 | `Autotechnical` → `Chronos Chart`。環境変数 `AUTOTECHNICAL_*` → `CHRONOS_*`、DB名 `chronos.db`、`app.js` の `STORAGE_KEY`。README 更新。既存テスト（`test_dev_server.py:189`・`test_live_yahoo.py` の参照を含む）が通る。※現環境に `data/` は無く DB の移行は不要 | P0-6 | `config.py` `main.py` `start.bat` `README.md` `app.js` `tests/` |
| P1-2 | `DONE` | 依存追加 | `requirements.txt` に `requests` `beautifulsoup4` `lxml` `keyring` `tzdata` `google-genai` `pydantic` `jinja2`。クリーン環境で `pip install -r` が通る | P1-1 | `requirements.txt` |
| P1-3 | `DONE` | マイグレーション基盤 | `settings`（`schema_version`）と `fetch_log` を作成。バージョンごとの移行関数の枠組み。既存DBからの移行と再実行の冪等性。以後のテーブル追加は各フェーズで移行として足す。`test_migration.py` | P1-1 | `database.py` |
| P1-4 | `DONE` | 設定モジュール | SPEC §2.1 の全項目の読み書き。**APIキーは `keyring`**、環境変数優先、マスク。**キーが DB とログに出ないことをテストで確認**。同期フォルダ配下の検出。`errors.py`（`UserFacingError`）。`test_settings.py` | P1-2 P1-3 | `settings.py` `errors.py` |
| P1-5 | `DONE` | 共通HTTPクライアント | SPEC §4.1。間隔制御・直列化・UA・リトライ（**待機の下限はソースの最小間隔**）・中断フラグ対応の待機・キーのマスク。`test_sources_base.py` | P1-2 | `sources/base.py` |
| P1-6 | `DONE` | ジョブ基盤 | SPEC §2.8.1。`start_job` / `job_status` / `cancel_job` / `active_jobs`、同種の多重起動拒否、`Event.wait` による中断。画面側のポーリングとヘッダのステータス欄（進捗・中断ボタン）。ダミージョブで動作確認。`test_jobs.py` | P1-4 | `jobs.py` `api.py` `js/jobs.js` `index.html` `style.css` |
| P1-7 | `DONE` | **起動時の自動更新（株価）** | SPEC §2.8.2 のうち株価の部分。`init()` 完了後に JS が `auto_update` ジョブを開始。`fetch_log(source='yahoo')` の記録（手動 `update` / `update_all` でも更新）、最小間隔によるスキップ、銘柄間1秒、失敗してもダイアログを出さず結果をまとめて表示、完了後の一覧再読込と表示中銘柄の再描画、中断。`auto_update_on_start` オフで何もしない。`test_autoupdate.py` | P1-6 | `autoupdate.py` `service.py` `app.js` |
| P1-8 | `DONE` | 設定タブUI | SPEC §2.1.3。EDINETの取得手順5段階、Gemini上限の入力と注記、`scrape_contact` の注記、自動更新のオン／オフ、規約表示、同期フォルダ警告。**接続テストと打ち切り解除はここでは作らない**（P4-4・P6-6） | P1-4 | `index.html` `app.js` `style.css` `api.py` |

### P2 — 需給データの取得

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P2-1 | `DONE` | karauri.net の合成フィクスチャ作成 | 実ページを**1回だけ**取得して `tests/fixtures/real/`（Git 対象外）に置き、構造を写した**合成 HTML** を `tests/fixtures/karauri_synthetic.html` として作る（SPEC §10.1 の行パターンを含む）。実物の採取は現役の報告者が多い銘柄で行う（7203 は約40行・最新 2022-04 で通常行が無い） | P1-5 | `tests/fixtures/` |
| P2-2 | `DONE` | 空売り残高の取得とパース | SPEC §2.2.1〜2.2.2a・§2.2.4。`holder_id` 抽出、**最小計算日以降だけの置換**（全置換はしない）、列数違いでエラー、403/429 で即中止。`test_karauri.py` | P2-1 | `sources/karauri.py` |
| P2-3 | `DONE` | **日証金の列定義の確定** | `zandaka.csv` `meigara.csv` を各1回取得して `tests/fixtures/real/` に置き、`cp932` で読んで全列名・速報/確報の区分値・区分列の意味を **SPEC §2.3.1 と §3 に追記**（§9-1 を解消）。合成 CSV を作る。※固定名・最新日のみ・約36列であることはレビューで確認済み | P0-6 | `docs/SPEC.md` `tests/fixtures/` |
| P2-4 | `DONE` | 貸借取引残高の取得とパース | SPEC §2.3。ヘッダ名で列を引く、**東証の行だけを採用**、登録銘柄の行だけ保存、確報が速報を上書き（逆はしない）、貸借銘柄でない場合はエラーにしない。`test_taisyaku.py` | P2-3 P1-5 | `sources/taisyaku.py` |
| P2-5 | `DONE` | 空売り残高合計の算出 | SPEC §2.2.3。`holder_id` ごとの最新採用、消失の二重条件、該当者なしで 0。`test_short_totals.py` | P2-2 | `sources/karauri.py` |
| P2-6 | `DONE` | 取得UI | 空売りの一括取得を `short_all` ジョブで。実行前に「対象銘柄数 × 間隔」を提示して確認、進捗と中断。`scrape_contact` 未設定なら実行不可。単一銘柄の取得、日証金の手動取得 | P2-2 P2-4 P1-6 | `api.py` `app.js` |
| P2-7 | `DONE` | 自動更新への組み込み（需給） | SPEC §2.8.2 の順2・順4。日証金は既定で含める。空売りは `auto_update_short`（既定 false）のときだけ、`short_recheck_hours` を守る。`test_autoupdate.py` に追加 | P2-6 P1-7 | `autoupdate.py` |

### P3 — 需給のチャート表示

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P3-1 | `DONE` | dashboard payload の拡張 | `chart.short` / `chart.taisyaku` を追加。**`prices.date` に存在する日付だけ**を載せる（whitespace は使わない）。空売りは最新の足まで据え置き点を足す | P2-4 P2-5 | `service.py` `api.py` |
| P3-2 | `DONE` | 空売り残高ペイン | SPEC §2.5.1〜2.5.2。**`LWC.LineType.WithSteps`**（数値リテラル禁止）。チップで切替。ラベルと注記は SPEC §1.4 | P3-1 | `chart.js` |
| P3-3 | `DONE` | 貸借取引残高ペイン | 融資残高・貸株残高の2本。**通常線。欠測をまたいで結ばない**（実装方法を決めて SPEC §9-2 を解消）。取得開始日以降のみである旨の注記 | P3-1 | `chart.js` |
| P3-4 | `DONE` | データなし時の表示 | 値が1件も無い銘柄はチップを無効化し理由を表示。**ラベルは「貸借取引残高（日証金）」** | P3-2 P3-3 | `chart.js` `app.js` |

### P4 — 開示の取得・突合・分類

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P4-1 | `DONE` | EDINET クライアントとコードリスト | SPEC §2.4.1・§2.4.5。キーはクエリパラメータ、1秒間隔、429バックオフ。**EDINET コードリストの取り込み**（`edinet_codes`、文字コード・列構成を確認して SPEC §9-6 を解消）。**利用規約の原文を再確認し SPEC §9-5 を解消** | P1-5 P1-4 | `sources/edinet.py` `database.py` |
| P4-2 | `DONE` | 書類一覧の取得ジョブと日次キャッシュ | SPEC §2.4.2・§2.4.4。`data/edinet_cache/` への保存、取得範囲（株価の最古日〜）、**確定済みの判定**（当日・エラーは再取得、翌日00:30以降で確定）、`disclosures` ジョブとして進捗・中断・再開。※キャッシュサイズの実測は API キー入手後（P4-6 に分離） | P4-1 P1-6 | `sources/edinet.py` `disclosures.py` |
| P4-3 | `DONE` | 突合・分類・キャッシュ再走査 | SPEC §2.4.3・§2.4.6。書類種別ごとの突合規則、`disclosures` / `disclosure_links`、取下げ、**銘柄登録時の再走査**、銘柄削除時の孤立書類の掃除。`test_edinet.py`（後から登録した銘柄・提出者としての大量保有が登録されないこと を含む） | P4-2 | `disclosures.py` `service.py` `database.py` |
| P4-4 | `DONE` | 開示取得UI | 取得ボタン（ジョブ）、分類別の件数、「直近90日を取り直す」、**EDINET 閲覧ページを開く**（URL 形式を確認して SPEC §9-7 を解消。不可なら PDF 一時取得方式に変更して SPEC 更新）、設定タブの EDINET 接続テスト | P4-3 P1-8 | `api.py` `app.js` |
| P4-5 | `DONE` | 自動更新への組み込み（開示） | SPEC §2.8.2 の順3。キー未設定は黙ってスキップ、未取得が30日を超える場合は直近30日分だけ。`test_autoupdate.py` に追加 | P4-3 P1-7 | `autoupdate.py` |
| P4-6 | `DONE` | キャッシュサイズの実測 | SPEC §9-8 を解消。30日分を実測し SPEC §8 を **1日 7.1KB・1年 2.5MB** に置き換えた（当初見積り「数十MB」は1桁大きかった）。あわせて実データ5,123件でパーサを検証し、**取下げの実構造の誤り**を発見して P4-3 を追補した | P4-2 | `docs/SPEC.md` |

### P5 — イベントマーカーとイベント欄

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P5-1 | `DONE` | イベント変換 | SPEC §2.5.3。**足のある日付への寄せ**（`prices.date` への二分探索。祝日表は持たない）、足がまだ無い場合、同日複数開示のまとめと優先順、昇順ソート。`dashboard` の `events`。`test_events.py` | P4-3 | `events.py` `service.py` |
| P5-2 | `DONE` | チャートマーカー | シグナルと同一配列にマージして1回で `createSeriesMarkers`。配列構築を `overlays.has("signals")` の外へ。OVERLAYS に「開示」チップ。マーカーに `id` | P5-1 | `chart.js` |
| P5-3 | `DONE` | チャートの連動用 API | `StockChart.render()` の戻り値に `onVisibleRangeChange(cb)` / `scrollToDate(date)` / `onMarkerClick(cb)` を追加。チップ切替による再生成をまたいで表示範囲と購読を引き継ぐ | P5-2 | `chart.js` `app.js` |
| P5-4 | `DONE` | イベント欄 | SPEC §2.6。**右カラムに配置**、表示範囲連動、行クリックでスクロール、提出時刻・role・提出者名の表示、「開く」、TDnet 対象外の注記 | P5-3 P4-4 | `index.html` `app.js` `style.css` |
| P5-5 | `DONE` | マーカークリック | `subscribeClick` の **`param.hoveredObjectId`** でマーカーを特定し、イベント欄の該当行へスクロール・強調 | P5-4 | `chart.js` `app.js` |

### P6 — AI分析レポート

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P6-1 | `TODO` | Gemini クライアント | `google-genai` ラッパ。`count_tokens`。**SDK の `retry_options` を設定しない**。実物を1回採取して `usage_metadata` のフィールド名・429 エラー詳細（`QuotaFailure.quotaId` / `RetryInfo`）・thinking 予算の指定方法を確認し **SPEC §9-4 を解消** | P1-4 | `ai/client.py` |
| P6-2 | `TODO` | クォータ管理 | SPEC §2.7.2。太平洋時間の日付境界と夏時間、**送信前に加算**、再依頼ごとのガード、429 の3分岐、打ち切りフラグと手動解除。`test_ai_quota.py` | P6-1 P1-3 | `ai/quota.py` `database.py` |
| P6-3 | `TODO` | スキーマとプロンプト | SPEC §2.7.3〜2.7.4。**根拠 → 結論の順**、リスト上限。読み取りファサードと `PromptInput`。**番兵値テスト**と `app/ai/` の識別子検査。`test_ai_prompt.py` | P6-1 P4-3 | `ai/schema.py` `ai/prompt.py` |
| P6-4 | `TODO` | 検証と再依頼 | SPEC §2.7.5。`MAX_TOKENS` 検知と案内文、エラー本文を添えた再依頼、上限2回。`test_ai_schema.py` | P6-3 P6-2 | `ai/client.py` |
| P6-5 | `TODO` | レポート生成 | SPEC §2.7.6。Jinja2 で単一HTML、表示順は結論が先、免責（TDnet 対象外を含む）、需給を含めない（番兵値）。`ai_reports`。`test_report.py` | P6-4 | `ai/report.py` `templates/` |
| P6-6 | `TODO` | レポートタブUI | `ai_analyze` ジョブ、実行前の確認ダイアログ（見積り・残量・送信データ種別）、進捗、レポート一覧と「開く」、設定タブの Gemini 接続テストと打ち切り解除ボタン | P6-5 P1-6 P1-8 | `api.py` `app.js` `index.html` |

### P7 — 仕上げ

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P7-1 | `DONE` | README・規約の明記 | 全データソースの出典・規約・免責、起動時の自動更新の説明とオフにする方法、データ保存先と同期フォルダの注意、キーの保存先。**別 PC への移行手順**（SPEC §2.1.4: `data/` をまるごとコピー＋APIキーの再入力。貸借取引残高は取り直せないので `data/` のコピーが必須であること）。（NOTICE の同梱は P0-6 で実施済みのこと） | P1-1 | `README.md` |
| P7-2 | `TODO` | 通しの動作確認 | 起動（自動更新が走る）→ 登録 → 需給取得 → 開示取得 → チャート表示 → AI分析 を実機で通す。オフラインでの起動も確認。`python -m pytest` が全通過 | P6-6 P5-5 P3-4 P7-1 | — |

### P8 — 実機テストで見つかった不備の修正（2026-09-20 ユーザーの実機テストで判明）

| ID | 状態 | タスク | 完了条件 | 依存 | 対象 |
|---|---|---|---|---|---|
| P8-1 | `DONE` | EDINET コードリストの自動取得 | **`fetch_and_save_code_list` がどこからも呼ばれておらず `edinet_codes` が常に空**だった。開示ジョブの先頭で `ensure_code_list()` が確保する（未取得・7日より古い・テーブルが空のいずれかなら取得）。失敗してもジョブは続行し summary に警告を出す。**空から埋まったときはジョブの最後に全キャッシュ日付を再走査**して、既存キャッシュの大量保有を紐づけ直す。`test_disclosures.py` | P4-3 | `disclosures.py` |
| P8-2 | `DONE` | 新規登録銘柄の過去開示の取り込み | 登録直後に `estimate_disclosures` を見て、取得対象があれば確認ダイアログを出して開示取得ジョブを実行する（銘柄の株価期間ぶん）。キー未設定・対象0件のときは何もしない | P8-1 P5-4 | `app.js` |
| P8-3 | `DONE` | 空売り残高が取れない理由の可視化 | 連絡先（`scrape_contact`）未設定を**ツールチップではなく画面の文字**で伝える。ダッシュボードの「空売り残高を取得」も未設定なら無効にする | P2-6 | `app.js` `index.html` `style.css` |
| P8-4 | `DONE` | 実機での確認 | 実 API でコードリスト取得 → 開示の過去取得 → 大量保有が紐づくこと、空売りの導線をユーザーの環境で確認 | P8-1 P8-2 P8-3 | — |
| P8-5 | `DONE` | karauri の備考「ポジション解消」 | 実サイトで観測した未知の備考。報告義務消失の文言に `解消` を足し（二重条件は維持）、既知の文言として警告を出さない。`test_karauri.py` `test_short_totals.py` | P2-2 | `sources/karauri.py` |

---

## 3. セッションログ

セッションの区切りで**1行追記**する。古い行は書き換えない。

| # | 日付 | 実施タスク | 結果・特記事項 |
|---|---|---|---|
| 1 | 2026-09-20 | P0-1, P0-2, P0-3 | 土台クローン・リモート設定。サブエージェント4本でリサーチ。RESEARCH/DESIGN/SPEC/PLAN/REVIEW_REQUEST 作成。方針3点をユーザー確定（日証金＋karauri併用 / AIへ需給を送らない / EDINETのみ）。ブランチ `docs/initial-design` にコミット |
| 2 | 2026-09-20 | P0-4 | fable による設計レビュー（重大4・中12・軽微5・提案3）。一次情報と同梱ライブラリで事実確認し、土台のテスト 252 passed を確認。ユーザーが推奨案をすべて承認し、**起動時の自動更新**を追加要望。SPEC/PLAN を 1.1 に更新、RESEARCH/DESIGN に訂正表を追加。タスクは 38 → 43。別 PC への移行手順を SPEC §2.1.4 に追加。ユーザーが全体を承認し、コミットして `main` にマージ、`origin` に push（P0-5）。push 時に GitHub のメール保護で拒否されたため、未 push の4コミットの作者メールを noreply アドレスに書き換えた（内容は不変） |
| 3 | 2026-09-20 | P0-5, P0-6, P1-1〜P1-8 | push（GitHub のメール保護で拒否されたため未 push の4コミットの作者メールを noreply に書き換え）。NOTICE・CLAUDE.md・`.gitignore`。P1（基盤）を完了: 改名、依存追加、マイグレーション（DDL を含めてロールバックできるよう明示トランザクション化）、設定（keyring）、共通HTTPクライアント、ジョブ基盤、起動時の自動更新（株価）、設定タブ。開発は `.venv` で行う。337 passed。レビューの NOTICE 著作権年の指摘は誤りと判明し撤回。`main` にマージして push 済み |
| 8 | 2026-09-20 | P4-1〜P4-6 | **P4（開示）完了。** EDINET クライアント・日次キャッシュ・突合・分類・UI・自動更新。ユーザーが API キーを取得し、資格情報ストア（設定タブ）に保存（`.env.local` は OneDrive とリポジトリに平文が残るため採らなかった）。**キー取得後に実データ30日分・5,123件で検証し、仕様の誤りを2つ発見**: (1) 取下げは元の書類にフラグが立つのではなく `docID` と `parentDocID` しか持たない空のスタブが後日現れる（旧仕様のままでは取下げを一度も検出できなかった。`scan_cache` を昇順処理にして引き当てる形に修正）、(2) `edinet_cache` は1年で約 2.5MB で、見積り「数十MB」は1桁大きかった。突合規則（350/360 は `issuerEdinetCode`、240〜320 は `subjectEdinetCode`）は実データと一致することを確認。閲覧ページの URL を実 docID で確認し §9-7 を解消。**SPEC の未確定事項から EDINET 関連はすべて解消**。開発サーバーで実 API を使い、取得・件数表示・接続テスト・確定済み判定・自動更新のトーストを確認。メインのレビューで、開示0件の文言（未取得と「提出が無い」の取り違え）、確認ダイアログの件数と期間の食い違い、取下げ適用の冪等性、`pending_dates` の接続開閉（最大3650回）を修正。602 passed / 15 skipped。`main` にマージして push 済み |
| 12 | 2026-09-20 | P8-5 | 実サイトの確認で観測した備考 `ポジション解消` を報告義務消失の文言に追加（`消失` または `解消`。`ratio < 0.5` との二重条件は維持）。既知の文言として警告を出さないようにし、未知の文言の警告は残した。メインのレビューで、サブエージェントが SPEC §12 の P8-2 の行を上書きして消していたのを戻した。673 passed / 15 skipped |
| 11 | 2026-09-20 | P8-1〜P8-4 | **ユーザーの実機テストで見つかった3件を修正。** (1) `fetch_and_save_code_list` がどこからも呼ばれておらず `edinet_codes` が常に空 → 開示ジョブの先頭で `ensure_code_list()` が確保（7日で期限切れ・空なら期限内でも取り直す・失敗しても続行・空から埋まったら全キャッシュ再走査）。**この不具合の間、大量保有報告書は構造上1件も紐づかなかった**。(2) 新規登録銘柄の過去開示を取る導線が無かった → 登録直後に日数と所要時間を出して取得を尋ねる。(3) 空売りが `scrape_contact` 未設定で無効なのに理由がツールチップだけ → 画面の文字で出し、ダッシュボードのボタンも無効にする。実データで通しの確認まで実施（上の動作確認欄）。671 passed / 15 skipped |
| 10 | 2026-09-20 | P7-1 | README を現状に合わせて全面更新（土台由来の説明のままだった）。設定タブ・需給・開示・起動時の自動更新（オフにする方法）・ジョブ、保存先と同期フォルダの注意、API キーの保存先、別 PC への移行手順、ソースごとの出典・規約・免責、構成とテスト一覧。メインのレビューで、空売りの凡例ラベルの引用と、日証金が東証の行だけを採る理由の説明を実装に合わせて直した。**P6（AI 分析）は Gemini の API キー待ちで、ユーザーの判断により P7-1 を先に実施した。** `main` にマージして push 済み |
| 9 | 2026-09-20 | P5-1〜P5-5 | **P5（イベントマーカーとイベント欄）完了。** `app/events.py`（開示→イベントの純粋な変換）、開示マーカー（シグナルと同一配列にマージして1回で設定・「開示」チップ）、`render()` の連動用 API（`onVisibleRangeChange` / `scrollToDate` / `onMarkerClick` / 論理範囲の getter・setter）、右カラムのイベント欄、マーカークリックでの行強調。Sonnet のサブエージェント3本（Python / チャート / 画面）で並行実装した。メインの判断で、**提出日がチャートの最初の足より前の開示はマーカーを出さない**ことを決めて SPEC に追記。SPEC §2.6 が求める提出事由（`reason`）が payload に無かったので `list_for_symbol` と `events.build` に追加。既存ユーザーの localStorage に新チップが入らない問題を、保存値に `knownOverlays`/`knownPanes` を持たせる移行で解決。メインのレビューで、イベント欄の空表示の分岐（取得済みかどうかを `fetched_days` だけで判断していた）と提出者名が空のときの「提出者: 」だけの行を直した。合成データの開発サーバーで通しで目視確認。660 passed / 15 skipped |
| 7 | 2026-09-20 | P3-1〜P3-4 | 需給のチャート表示。payload（`chart.short` / `chart.taisyaku`）、空売りの階段線（`LWC.LineType.WithSteps`）、貸借の2本を**連続区間ごとに別シリーズ**にして欠測で切る（§9-2 を解消）、データなし時のチップ無効化。メインのレビューで、空売りのシリーズを SPEC どおり疎な点列に戻した（サブエージェントは据え置き済みの密配列を渡していた。凡例だけ据え置く形に分離）。合成データを入れた開発サーバーで、階段・据え置き・欠測の切れ目・チップ無効化を目視確認。**P3 完了**。446 passed / 15 skipped |
| 6 | 2026-09-20 | P2-6, P2-7 | 取得UI（一括取得は `short_all` ジョブ・事前に所要時間を提示して確認・403/429 で即中止・連続3回の失敗で中止）と、起動時の自動更新への組み込み（日証金は既定で実行、空売りは `auto_update_short` がオンのときだけ。自動更新側の打ち切りは株価と同じ**連続2回**）。メインのレビューで、API 名を SPEC の `estimate_short_all` に統一し、日証金の要約に `None` が出る分岐を直した。**P2 完了**。440 passed / 15 skipped |
| 5 | 2026-09-20 | P2-1〜P2-5 | 実物を各1回採取（karauri `/6920/`・日証金 `zandaka.csv` `meigara.csv`）。**karauri の銘柄別ページは直近100件まで**と判明し、保存を全置換から「最小計算日以降だけの置換」に変更。**日証金 CSV は同一コードが市場ごとに複数行**あると判明し、東証の行だけを採用する規則を追加。`zandaka.csv` の全36列を SPEC に記載して §9-1 を解消。パーサ・保存・合計算出を Sonnet のサブエージェント2本（karauri / 日証金）で並行実装し、メインが差分をレビューして設定キー・日付書式の検証・浮動小数の丸めを修正。実物に対する構造テスト（`test_real_fixtures.py`）を追加。391 passed / 15 skipped |
| 4 | 2026-09-20 | （引き継ぎ） | ユーザーが P1 までの成果を承認。次のセッションから **メインは指揮、実装・テストは Sonnet のサブエージェントを複数起動して行う**方針を指示（§0 に手順を追加）。P2 の実サイトへの各1回のアクセスも了承済み。次は P2-1・P2-3 |

---

## 4. 申し送り事項

セッションを跨いで引き継ぐべき事項。**解決したら取り消し線ではなく削除し、§3 のログに結果を書く。**

### 実装時に忘れやすい制約（不変条件）

1. **需給データを AI に送らない。レポートにもエクスポートにも載せない。** プロンプト組み立ては `app/ai/prompt.py` の1関数に集約。
   `service.dashboard()` の payload をプロンプトに流用しない。番兵値テストで固定する（SPEC §2.7.3）
2. **第三者サイトの実データを Git にコミットしない。** フィクスチャは合成データ。実物は `tests/fixtures/real/`（Git 対象外）（SPEC §10.1）
3. **UI で「信用残」と単独表記しない。** ラベルは「貸借取引残高（日証金）」。貸借銘柄のみ・取得開始日以降のみである旨を併記（SPEC §1.4）
4. **階段線は `LWC.LineType.WithSteps` と定数で書く。** 数値の `2` は**曲線**である。階段にするのは空売り残高合計だけ。
   貸借取引残高は通常線で、欠測をまたいで結ばない（SPEC §2.5.2）
5. **マーカーは売買シグナルと開示を同一配列にマージし、time 昇順で1回で設定する。**
   理由は「消えるから」ではなく「別々に設定すると重なり回避が効かないから」。開示マーカーはシグナルのチップと独立に出す（SPEC §2.5.3）
6. **マーカーの日付を DB に持たない。** 表示時に `prices.date` から「提出日以降で最初に足がある日」を求める。祝日表を作らない（SPEC §2.5.3）
7. **EDINET の突合は EDINET コードで行う。** `secCode` は提出者のコード。大量保有は `issuerEdinetCode`、公開買付は `subjectEdinetCode`（SPEC §2.4.3）
8. **`documents.json` は銘柄で絞る前に日次キャッシュへ保存する。** 絞ってから捨てると、後から登録した銘柄の開示が取れなくなる（SPEC §2.4.2）
9. **「決算」という語を開示のラベルに使わない。** EDINET にあるのは有報・半期報で、決算発表日ではない。四半期報告書は廃止済み（SPEC §2.4.6）
10. **`short_totals` は本ツールの算出値。0 は「空売りなし」ではない。** 画面に注記する（SPEC §1.4）
11. **APIキーは `keyring` に保存し、DB・ログ・レポートに出さない。** URL をログ出力する箇所では `Subscription-Key` を必ずマスクする
12. **`scrape_contact` 未設定ならスクレイピングしない。karauri の 403/429 は即中止し、リトライしない**
13. **`google-genai` の `retry_options` を設定しない。** 再送はアプリ側で行い、送信を試みるたびに `ai_usage.requests` を加算する（SPEC §2.7.2）
14. **Gemini の RPM/TPM/RPD をコードに埋め込まない。** 設定値とする。AI 分析は自動実行しない
15. **スキーマのフィールドは「根拠 → 結論」の順。** 表示順はテンプレートで決める（SPEC §2.7.4）
16. **長時間処理はジョブで実行し、HTTP 待ちの間に DB の書き込みロックを持たない**（SPEC §2.8.1）
17. **自動更新は起動時の1回だけ。** 常駐ポーリングにしない。失敗してもダイアログで起動を妨げない（SPEC §2.8.2）
18. **`margin_balances` は再取得できない。** マイグレーションで DROP しない（SPEC §3.1）
19. **空売り残高を銘柄単位で全置換しない。** karauri の銘柄別ページは**直近100件まで**しか載らないので、
    全置換すると101件目より古い収集済みの行が消える。削除するのは「取得した行の最小計算日以降」だけ（SPEC §2.2.2a）
20. **日証金 CSV は同じ銘柄コードが市場ごとに複数行ある。** `取引所区分名` に「東証」を含む行だけを採用する。
    絞らないと PK `(symbol, date)` を市場違いの行で上書きし合う（SPEC §2.3.1）
21. **日証金の未知の速報/確報区分でエラーにして保存を止めない。** `prelim` として保存し警告ログを出す。
    取り直せないデータを表記ゆれで失わないため（SPEC §2.3.1）

### 実装メモ（後続タスク向け）

- `settings` テーブルには `schema_version` も入っている。P1-4 の設定モジュールは、ユーザー設定の一覧から `schema_version` を除外すること
- テーブルの追加は `app/database.py` の `MIGRATIONS` の末尾に `(バージョン, 関数)` を足す。既存の移行関数は書き換えない。
  各移行は明示的なトランザクションで囲まれ、失敗時は DDL ごとロールバックされる（Python の sqlite3 は DDL を暗黙にコミットするため）
- 開発・テストはプロジェクト直下の `.venv`（Git 対象外）で行う: `.venv\Scripts\python.exe -m pytest`。グローバルの Python には `keyring` が入っていない。`start.bat` は `.venv` があれば優先して使う
- 設定タブは `web/js/settings.js`。入力欄は `data-setting="<キー>"` を付けるだけで読み書きされる。API キーの行は `.secret-row[data-secret]`。API は `get_settings` / `save_settings` / `reveal_secret`。接続テスト（P4-4・P6-6）と Gemini 打ち切り解除（P6-6）のボタンは未実装
- 自動更新は `app/autoupdate.py` の `AutoUpdater`。ソースを足すときは `self.steps` に `(名前, 関数)` を追加する。関数は `(ctx, stocks) -> {"summary": str, "failed": bool, "updated_symbols": [...]}`。スキップ判定は `_fetched_recently(source, key)`。P2-7・P4-5 はこの形に従う
- ジョブは `app/jobs.py`。`jobs.register(kind, func)` で登録し、関数は `func(ctx, params)`。リクエストの合間に `ctx.check()`、待機は `ctx.wait(秒)`、進捗は `ctx.progress(current, total, label)`、`ctx.cancel` は `HttpClient.get(cancel=...)` にそのまま渡せる。画面側は `Jobs.run(kind, params)`（`web/js/jobs.js`）で開始〜終了待ち、進捗と中断ボタンはヘッダに自動で出る。動作確認用の `selftest` ジョブは `--debug` と開発サーバーでのみ登録される
- 画面の確認は開発サーバー（`python dev_server.py` → `http://127.0.0.1:8765/?dev`）で行える。データを汚さないよう `CHRONOS_DATA_DIR` を一時フォルダに向けること
- 外部取得は `app/sources/base.py` の `HttpClient(source, min_interval, agent=..., no_retry_statuses=...)`。間隔とロックはソース名で共有される。`get(url, params, cancel=Event)` は 2xx 以外で `HttpError(status)`、中断で `Cancelled`。karauri は `no_retry_statuses=frozenset({403, 429})` を渡すこと。テストでは `session` / `clock` / `sleep` を差し替える
- 設定は `app/settings.py` の `Settings`。キーの追加は `SPECS` に足す。API キーは `get_secret()`（環境変数 → keyring の順）。テストでは `keyring_backend` にフェイクを渡す
- 需給の3テーブル（`short_positions` `short_totals` `margin_balances`）はマイグレーション **v2**（`app/database.py` の `_migrate_v2`）で作成済み。
  各ソースの保存関数は `app/database.py` ではなく `app/sources/<ソース>.py` に置き、`Database` を引数で受ける（同じファイルを複数の担当が触らないため）

- 需給ソースの公開関数（P2-6・P2-7 から呼ぶ）:
  - `app/sources/karauri.py`: `make_client(settings)` / `fetch_html(client, code, cancel=None)` / `parse(html)` /
    `compute_totals(rows)` / `save(db, symbol, rows) -> {"rows", "dates", "since"}`
  - `app/sources/taisyaku.py`: `make_client(settings=None)` / `fetch_zandaka(client, cancel=None)` / `parse(content: bytes)` /
    `save(db, rows, symbols, fetched_at=None) -> {"saved", "skipped", "date", "missing"}` /
    `fetch_and_save(db, settings=None, symbols=None, cancel=None, client=None)`（取得〜保存。1リクエストなのでブロッキング）
  - karauri の取得の入口: `select_targets(db, settings, symbols=None, force=False)` / `estimate(db, settings, symbols=None)` /
    `fetch_one(db, settings, symbol, cancel=None, client=None)` / `short_all_job(db, settings)`（`jobs.register("short_all", ...)` 用）
  - **P2-7 はこれらを呼ぶこと**（取得ロジックを autoupdate 側に書き直さない）

### チャートの需給ペイン（P3 で実装済み）

- `web/js/chart.js` の `PANES` に `short` / `taisyaku` を追加済み。既定（`DEFAULTS.panes`）には入れていない
- **階段線は `LWC.LineType.WithSteps`**（`case "short"` の1か所だけ）。数値リテラルを使わない
- 貸借は連続区間ごとに `line()` を呼ぶ。凡例は `legendOnly()` で1本ぶんだけ登録する
  （区間シリーズに `legend` を渡すと同じ項目が区間の数だけ並んでしまう）
- `legendItems` は `[ラベル, 色, 値配列, フォーマッタ]`。フォーマッタ省略時は従来どおり価格表示
- データが無い銘柄では `app.js` の `updateSupplyChipAvailability()` がチップを無効化し、
  `chart.js` 側でも `panes` から外す（チップ ON のまま銘柄を切り替えても空のペインを作らない）

### EDINET（P4-1・P4-2 で実装済み。P4-3 以降はこれを呼ぶ）

`app/sources/edinet.py`:

- `make_client(settings=None)` — `source="edinet"`・1秒間隔。**429 はリトライ対象のまま**（karauri と違う）
- `fetch_documents(client, date, api_key, cancel=None) -> dict` — `documents.json` を取って JSON を返すだけ。
  キーは `params` で渡す（**自前で URL を組み立てない**。`mask_secrets` が効かなくなる）。
  `metadata.status` が `"200"` 以外なら `UserFacingError`
- `fetch_code_list` / `parse_code_list` / `save_code_list`（全置換）/ `fetch_and_save_code_list`
- `edinet_code_for(db, symbol)` — **4桁コード + `"0"`** で `edinet_codes` を引く
- `fetch_log` を書くのは `fetch_and_save_*` 側。`save_*` は DB だけ触る（`taisyaku` と同じ）

日次キャッシュ（P4-2）も `app/sources/edinet.py`:

- `cache_dir(base_dir=None)` / `cache_path(date, base_dir=None)` — `base_dir` 省略時は `config.EDINET_CACHE_DIR`。
  `cache_path` は日付を `YYYY-MM-DD` ちょうどの書式に限定する（`'../evil'` も `'2026-9-1'` も `UserFacingError`）
- `write_cache(date, data, base_dir=None) -> int` — gzip（`mtime=0`）+ 一時ファイル → `os.replace`
- `read_cache(date, base_dir=None) -> dict | None` — 無い・壊れているときは `None`（例外にしない）
- `cached_dates(base_dir=None) -> list[str]` — 昇順
- `fetch_day(db, date, api_key, cancel=None, client=None, base_dir=None)` —
  取得 → **キャッシュを書いてから** `fetch_log` に記録。0件の日も `empty` として書く

`app/disclosures.py`（P4-2・P4-3）:

- `today_jst()` / `is_finalized(db, date)` / `fetch_range(db, today=None)` /
  `pending_dates(db, *, today=None, max_days=None, redo_days=0, now=None)` / `estimate(db, settings, ...)`
- `disclosures_job(db, settings, base_dir=None)` — `jobs.register("disclosures", ...)` に渡す。
  **main.py と dev_server.py で登録済み**。戻り値は
  `{"days", "with_documents", "empty", "errors", "aborted", "summary"}`。
  `with_documents` が「書類が1件以上あった日付」で、**P4-3 の突合はここを走査すればよい**
- `pending_dates` は**降順**（新しい日付から）。`fetch_log` は1回のクエリでまとめて読む
  （最大10年ぶんを1日ずつ `db.get_fetch` すると接続の開閉が数千回になる）
- 確定済み判定のしきい値は JST で作ってからローカル素朴時刻に変換して `fetched_at` と比べる
  （`fetched_at` は `datetime.now()` 由来）

突合・分類（P4-3）:

- `classify(doc_type_code)` / `parse_document(raw)` / `link_targets(db, symbols=None)` /
  `match_roles(doc, targets)` / `save_documents(db, items)` / `scan_cache(...)` / `cleanup_orphans(db)`
- **350/360（大量保有）は `issuer_edinet_code` でしか突合しない。** `edinet_code`（提出者）や `sec_code` で
  拾うと、他社株を保有して提出しただけの銘柄が自分自身の需給イベントとして登録される
- `sec_code` の補助突合は **EDINET コードが解決できなかった銘柄だけ**・**350/360 以外**
- **取下げは別レコード（スタブ）で来る。** `docID` と `parentDocID` と `withdrawalStatus` しか無い。
  `is_withdrawal_stub` / `apply_withdrawals` が担当し、**`scan_cache` は日付を昇順に並べ替えてから**処理する
  （ジョブは新しい日付から取得するので、並べ替えないと取下げのほうが先に処理されて引き当たらない）
- **どの登録銘柄にも紐づかない書類は `disclosures` に入れない**（キャッシュには全国の書類が入っている）。
  後から登録した銘柄は `scan_cache(db, symbols=[新銘柄])` でキャッシュから埋める
- `service.register()` が新規登録のあとに `scan_cache` を、`service.delete()` が `cleanup_orphans` を呼ぶ。
  **どちらも失敗しても登録・削除は成功させる**（開示の紐付けはおまけ）
- `scan_cache` は登録銘柄ぶんのキャッシュ日付をすべて読む。`register()` はこれを同期で呼ぶので、
  キャッシュが数年分に育つと登録が数秒延びる。気になったらジョブに移すこと
- **テストは `tests/conftest.py` の autouse フィクスチャで `config.EDINET_CACHE_DIR` を一時フォルダに向けている。**
  向けないと `service.register()` のテストが開発機の `data/edinet_cache/` を読む

### イベント（P5 で実装済み。P6・P7 はこれを使う）

- `app/events.py` は **DB に触らない純粋な変換**。`build(disclosures.list_for_symbol(db, symbol), dates)` で
  `{"items", "markers", "counts", "fetched_days"}` を返し、`dashboard()` の**トップレベル** `events` に入る
  （`chart.short` / `chart.taisyaku` とは階層が違う）
- **マーカーの日付は保存しない。** `marker_date()` が毎回 `prices.date` へ二分探索する。
  足がまだ無い開示と、**提出日が最初の足より前の開示**は `marker_date` が `None`（マーカーを出さずイベント欄にだけ出す）
- 取下げ（`withdrawal` が 0 でも None でもない）は `items` に残すがマーカーには出さない
- `web/js/chart.js` の `render()` の戻り値: `setRange` / `scrollToDate` / `onVisibleRangeChange` /
  `onMarkerClick` / `visibleLogicalRange` / `setVisibleLogicalRange` / `destroy`。
  購読は `render()` ごとに閉じるので、`app.js` の `renderChart()` が毎回張り直している
- チップ設定の保存値には `knownOverlays` / `knownPanes`（保存時点のチップ id）が入る。
  **既定 ON のチップを追加したら、それだけが既存ユーザーにも足される**（ユーザーが OFF にしたものは復活しない）
- ダッシュボードは `get_disclosures` を呼ばない（`events.counts` / `events.fetched_days` を使う）。
  API 自体は残してある

### `dashboard()` の需給 payload（P3-1 で確定。P3-2〜P3-4 はこれ前提）

`chart.short` と `chart.taisyaku` はどちらも `{"available": bool, "reason": str|None, "points": [...]}`。
`available` が false のとき `reason` に画面へ出す理由が入る（SPEC §2.5.2 の文言）。

- `short.points`: `{"date", "ratio", "qty", "holders", "carried"}`。**`prices.date` に存在する日付だけ**・昇順。
  最後の報告が最新の足より前なら、最新の足の日付に同じ値の点を1つ足してある（その点だけ `carried: true`）
- `taisyaku.points`: `{"date", "yushi", "kashi", "net", "kind"}`。同じく足のある日付だけ・昇順。
  **据え置きはしない。欠測は欠測のまま**なので、線を切るのは画面側の仕事（`chart.dates` 上で隣り合うかで判定する）
- **EDINET の書類閲覧ページの URL 形式（§9-7 / P4-4 用の手がかり）**: 閲覧サイトの `js/WZEK0040_WindowOpen.js` が
  `window.open("./WZEK0040.aspx?" + 書類管理番号 + "," + 履歴番号 + "," + lang)` を呼んでいた（lang は 2=日本語 / 1=英語）。
  つまり `https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?<docID>,,2` になるはず。
  **実 docID での確認は API キーが入ってから**（P4-4）。規約上、閲覧ページは**ユーザーの既定ブラウザで開くだけ**にすること
- `.gitattributes` は既定で `* text=auto eol=lf`。**改行をそのまま保ちたいフィクスチャは `-text` を明示する**
  （日証金の合成 CSV は cp932・CRLF。放っておくと LF に正規化されて実物と構造が変わる。`test_taisyaku.py` が検知する）
- `tests/test_real_fixtures.py` は `tests/fixtures/real/` に実物があるときだけ走る（無ければ skip）。
  値は見ずに構造だけを検証し、合成フィクスチャが実物からずれていないかの保険にする

### 開発サーバーで画面を確認するとき

- **URL に `?dev` を付ける**（`http://localhost:8765/?dev`）。`web/js/bridge.js` は `?dev` が無いと
  pywebview のネイティブブリッジを待つので、付け忘れると画面が空のまま何も起きない
- `.claude/launch.json` の `chronos-dev` は `CHRONOS_DATA_DIR` をスクラッチ領域に向けてある。
  **ユーザーの `data/` を読み書きしない**。API キーは資格情報ストアにあるのでデータフォルダに関係なく使える
- 種データを入れるときは `auto_update_on_start` を `0` にしておく（画面を開いた瞬間に yfinance を叩かせない）

### 外部アクセスの作法

- 開発中にテストのたびに外部サイトを叩かないこと。**構造確認のための採取は1回だけ**にし、以後は合成フィクスチャを使う
- 実通信テストは `CHRONOS_LIVE=1` のときだけ動くようにする
- karauri.net への手動確認も最小限に留める（レビューで robots.txt と 7203 のページを各1回取得済み。結果は REVIEW_RESULT に記録）
- 自動更新の開発中は `auto_update_on_start` をオフにするか、フェイクの取得層で動かす（起動のたびに外部へアクセスしないため）

---

## 5. 未解決事項

| # | 状態 | 内容 | 解決方法 | 期限 |
|---|---|---|---|---|
| 1 | `CLOSED` | 日証金 `zandaka.csv` の全列名・速報/確報の区分値 | P2-3 で確定。SPEC §2.3.1 に全36列を記載。`速報` の実値だけは未観測だが、**未知の区分値は `prelim` として保存する**設計にしたので実装はブロックされない | 解消（2026-09-20） |
| 2 | `CLOSED` | 貸借取引残高の欠測で線を切る実装方法 | P3-3 で決定。**連続区間ごとに別シリーズ**（SPEC §2.5.2）。価格軸のラベルは最後の区間だけに出す | 解消（2026-09-20） |
| 3 | `OPEN` | `usage_metadata` のフィールド名、429 エラー詳細の実構造、thinking 予算の指定方法 | P6-1 で実物を1回採取 | P6-1 |
| 4 | `CLOSED` | EDINET 利用規約の正確な文言 | P4-1 で原文を確認。SPEC §2.4.7 に記載。**加工した旨と主体の記載が必要**、**サイトのスクレイピングは禁止で API を使う**（コードリストは API で取れないので但し書きに該当） | 解消（2026-09-20） |
| 5 | `CLOSED` | EDINET コードリスト CSV の文字コード・列構成 | P4-1 で実ファイルを確認。SPEC §2.4.1a に記載（`cp932`・1行目はダウンロード情報でヘッダは2行目・13列） | 解消（2026-09-20） |
| 6 | `CLOSED` | EDINET 書類閲覧ページの URL 形式 | P4-4 で実 docID を開いて確認。`https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?<docID>,,2`（SPEC §2.4.6）。PDF 一時取得方式は不要 | 解消（2026-09-20） |
| 9 | `CLOSED` | メタデータが空の書類の正体 | P4-6 の実測で判明。**不開示ではなく取下げのスタブ**だった（SPEC §2.4.6）。30日分に不開示（`disclosureStatus` ≠ 0）は1件も無かった。P4-3 を追補して `parentDocID` で引き当てる実装にした | 解消（2026-09-20） |
| 8 | `CLOSED` | `edinet_cache` の実サイズ（SPEC §8・§9-8） | P4-6 で30日分を実測。1日 7.1KB・1年 2.5MB（gzip）。当初見積り「1年あたり数十MB」は1桁大きかった | 解消（2026-09-20） |
| 7 | `OPEN` | karauri.net の銘柄別ページが本当に100件で打ち切られているか（ページャは無かったが、上限値そのものは公表されていない） | 上限を前提にした保存方法（SPEC §2.2.2a）は、上限が無かった場合でも正しく動く。再取得はしない。将来 101 件以上のページを観測したら本行を閉じる | なし（設計で吸収済み） |

---

## 6. 決定記録

設計上の判断を「何を・なぜ」で1行ずつ残す。仕様の詳細な変更履歴は SPEC §12。

| 日付 | 決定 | 理由 | 決定者 |
|---|---|---|---|
| 2026-09-20 | 需給は日証金CSV（貸借取引残高）＋ karauri.net（空売り残高）。JPX 週次 PDF も J-Quants 課金も使わない | コストと機械可読性。※当初の表現「信用残は日証金＋karauri の併用」は、karauri から信用残は取れないため上記の意味に読み替える | ユーザー |
| 2026-09-20 | Gemini には株価・指標・EDINET 開示のみ送る。需給は送らない | JPX の生成AI条項と Gemini 無料枠の学習利用の重なりを避ける | ユーザー |
| 2026-09-20 | 開示は EDINET のみ。TDnet は対象外で、後から差し込める構造にする | 無料かつ合法な取得手段が無い | ユーザー |
| 2026-09-20 | 日証金は蓄積型（レビュー指摘3の A 案）。欠測は線を切る | ファイルが固定名・最新日のみで、過去分を取り直せない | ユーザー（推奨案を承認） |
| 2026-09-20 | **起動時に登録銘柄を自動更新する**（株価・日証金・EDINET。空売りは設定でオンにした場合のみ） | 土台に自動更新が無い。日証金の取りこぼし対策にもなる。karauri は個人運営サイトへの毎日のアクセスになるため既定オフ | ユーザー要望＋レビュアー提案 |
| 2026-09-20 | XBRL 財務数値の DB 化と開示 PDF のローカル保存を初期リリースから外す（SPEC §11） | 1人で完遂するため。前者は利用先が AI プロンプトだけで難所が集中している | ユーザー（推奨案を承認） |
| 2026-09-20 | 実装・テストなどの細かい作業は Sonnet のサブエージェントを複数起動して行い、メインのモデルは指揮に回る | ユーザーの指示。手順と注意点は §0「作業の進め方」 | ユーザー |
| 2026-09-20 | 自動更新でソースを打ち切るのは「連続2回の失敗」 | 最初の失敗で打ち切ると、先頭の銘柄が銘柄固有の理由で失敗するだけで残りが更新されなくなる | 実装時の判断（P1-7） |
| 2026-09-20 | APIキーは `keyring`、`data/` の既定位置は変えず同期フォルダ配下なら警告 | 作業ディレクトリが OneDrive 配下。既定位置の変更は土台の README・運用との差が大きいため警告に留めた | レビュアー提案を承認 |
| 2026-09-20 | karauri の `scrape_contact` はリポジトリ URL（`https://github.com/satsuki19980613/Chronos-Chart`）にする | 連絡先は名乗るが、個人のメールアドレスを第三者サイトのログに残さない | ユーザー |
| 2026-09-20 | 空売り残高の保存は「取得した行の最小計算日以降だけを置換」に変更（全置換をやめる） | P2-1 の採取で、銘柄別ページが**直近100件まで**と判明。全置換だと古い収集済みの行が消える | 実装時の判断（P2-1） |
| 2026-09-20 | 日証金 CSV は `取引所区分名` に「東証」を含む行だけを採用する | P2-3 の採取で、同一コードが市場ごとに複数行あると判明（4759 行中 377 コード）。PK が `(symbol, date)` なので絞らないと上書きし合う | 実装時の判断（P2-3） |
| 2026-09-20 | 日証金の未知の速報/確報区分は、エラーにせず `prelim` として保存する | `速報` の実値を観測できなかった。`margin_balances` は取り直せないので、表記ゆれで1日分を失うほうが害が大きい | 実装時の判断（P2-3） |
| 2026-09-20 | `meigara.csv` は構造を記録するだけで、初期リリースでは取得しない | 貸借銘柄区分 `1`/`2` の意味が確定できず、更新のたびに1リクエスト増えるわりに得られるのは画面の文言の精度だけ | 実装時の判断（P2-3） |
| 2026-09-20 | 各ソースの保存関数は `app/database.py` ではなく `app/sources/<ソース>.py` に置く | サブエージェントを並行させるときに `database.py` が衝突点になるため。移行（DDL）だけをメインが `database.py` に入れる | 実装時の判断（P2） |
| 2026-09-20 | 貸借取引残高の欠測は連続区間ごとに別シリーズで切る | 透明色で1点だけ消す案は `lastValueVisible` と凡例の値が1シリーズに紐づき、複数区間の現在値の扱いが複雑になる | 実装時の判断（P3-3） |
| 2026-09-20 | 凡例だけは空売り残高を据え置いて表示する（シリーズのデータは据え置かない） | 階段線は画面上ずっと値を保持しているのに、報告のない日の凡例が「—」になると画面と矛盾する | 実装時の判断（P3-2） |
| 2026-09-20 | EDINET の書類一覧は**新しい日付から**取得する | 初回は数分かかり中断され得る。直近が先に揃えばチャートの右端から埋まる。自動更新の「直近30日分」も同じ規則で表せる | 実装時の判断（P4-2） |
| 2026-09-20 | 日次キャッシュは0件の日も書き、書き込みは一時ファイル→`os.replace` で原子的に行う | 0件の日を書かないと再走査で「未取得」と区別できない。原子的でないと、落ちたときに壊れたファイルが「取得済み」として残る | 実装時の判断（P4-2） |
| 2026-09-20 | 確定済み判定のしきい値（翌日00:30 JST）は、`fetch_log.fetched_at` と同じローカル素朴時刻に変換して比較する | `fetched_at` は `datetime.now()` 由来のローカル時刻。PC が JST でなくても仕様どおり日本時間で判断できる | 実装時の判断（P4-2） |
| 2026-09-20 | `disclosures` には**登録銘柄に紐づく書類だけ**入れる（キャッシュには全国の書類が入っている） | 全部入れると DB が肥大する。後から登録した銘柄はキャッシュ再走査で埋められるので、捨てても取り返しがつく | 実装時の判断（P4-3） |
| 2026-09-20 | 必須項目が欠けた書類は捨てるだけにし、登録済みの行は消さない | 不開示の書類はメタデータが空で返る。欠落を一律「削除」と解釈すると、EDINET の仕様変更で登録済みの開示を全消ししかねない（§5 の #9） | 実装時の判断（P4-3） |
| 2026-09-20 | 再走査（`scan_cache`）は中断時にも必ず走らせてから `Cancelled` を投げ直す | 走査しないとキャッシュと DB がずれる。その日付は確定済みなので、放っておくと二度と走査されない | 実装時の判断（P4-3） |
| 2026-09-20 | 取下げは `parentDocID`（と自分の `docID`）で既存行を引き当てて `withdrawal` を更新する。再走査は日付の昇順 | 実データで、取下げが**中身の空なスタブとして後日の日付に現れる**と判明。元の書類にフラグが立つという旧仕様のままでは取下げを一度も検出できなかった | 実装時の判断（P4-3 追補・実データ検証） |
| 2026-09-20 | 自動更新の EDINET ステップは、スキップ時も summary に出す（「EDINET スキップ（API キー未設定）」） | SPEC §2.8.2 の summary 例が「空売り スキップ（設定オフ）」を含んでおり、スキップの理由が見えるほうが親切。既存の自動更新テスト4件の期待文字列はこれに合わせて更新した | 実装時の判断（P4-5） |
| 2026-09-20 | チャート設定の保存値に「保存時点で存在したチップ id」（`knownOverlays` / `knownPanes`）を持たせ、既定 ON で未知の id だけを読み込み時に追加する | 新しいチップ（開示）を既定 ON で足しても、既存ユーザーの localStorage には入っていないため OFF で出てしまう。単純に既定を混ぜ直すと、ユーザーが意図的に OFF にしたチップまで復活する | 実装時の判断（P5-2） |
| 2026-09-20 | **提出日がチャートの最初の足より前の開示はマーカーを出さない**（イベント欄には出す） | 「提出日以降で最初に足がある日」にそのまま寄せると、期間外の古い開示が最左のローソクに付き、その日に起きた出来事のように見える | 実装時の判断（P5-1） |
| 2026-09-20 | API キーは OS の資格情報ストア（設定タブから入力）に置く。`.env.local` は使わない | 作業フォルダが OneDrive 配下にあり、ファイルに平文で置くとクラウドへ同期される。`.gitignore` 漏れで公開リポジトリへ push される事故も避けたい | ユーザー（推奨案を承認） |

---

## 7. レビュー指摘の反映状況

[REVIEW_RESULT.md](REVIEW_RESULT.md) の指摘番号と反映先。見送りは無い。

| # | 重要度 | 指摘 | 反映先 |
|---|---|---|---|
| 1 | 重大 | EDINET の差分管理で開示が欠落 | SPEC §2.4.2・§2.4.4・§3 ／ P4-2・P4-3 |
| 2 | 重大 | `secCode` のみの突合 | SPEC §2.4.3・§3（`edinet_codes`・`disclosure_links`）／ P4-1・P4-3 |
| 3 | 重大 | 日証金は固定名・最新日のみ | SPEC §2.3・§2.5.2・§2.8.2・§3 ／ P2-3・P2-4・P2-7・P3-3 |
| 4 | 重大 | 実データのフィクスチャをコミットする計画 | SPEC §10.1 ／ P0-6・P2-1・P2-3 |
| 5 | 中 | `lineType: 2` は曲線 | SPEC §2.5.1〜2.5.2 ／ P3-2 ／ §4-4 |
| 6 | 中 | whitespace の前提と階段線の範囲 | SPEC §2.5.2 ／ P3-1・P3-3 |
| 7 | 中 | ブロッキング・進捗なし・中断不可 | SPEC §2.8.1・§4.2 ／ P1-6 |
| 8 | 中 | Gemini 429 は区別できる | SPEC §2.7.2・§6 ／ P6-1・P6-2 |
| 9 | 中 | SDK は既定でリトライしない | SPEC §2.7.2 ／ P6-1 ／ §4-13 |
| 10 | 中 | スキーマは根拠 → 結論 | SPEC §2.7.4 ／ P6-3 |
| 11 | 中 | `MAX_TOKENS` の対策・再依頼の見積り | SPEC §2.1.1・§2.7.2・§2.7.5 ／ P6-4 |
| 12 | 中 | `event_date` を DB に持たない | SPEC §2.5.3・§3 ／ P5-1 |
| 13 | 中 | 「決算系」の実態・四半期報告書の廃止 | SPEC §2.4.6・§2.5.3・§2.6・§2.7.3・§2.7.6 |
| 14 | 中 | `holder_id`・全置換・消失の二重条件 | SPEC §2.2.2・§2.2.3・§3 ／ P2-2・P2-5 |
| 15 | 中 | `disclosure_facts` のホワイトリスト | 機能ごと SPEC §11 へ移動（提案23-1） |
| 16 | 中 | キーが OneDrive 配下に平文で置かれる | SPEC §2.1.2・§2.1.3・§7.3 ／ P1-4・P1-8 |
| 17 | 軽微 | マーカーの事実誤認2点 | SPEC §2.5.3・§2.6 ／ P5-2・P5-5 |
| 18 | 軽微 | スクレイピング作法の細部 | SPEC §2.2.4・§4.1 ／ P1-5・P2-1・P2-2 |
| 19 | 軽微 | 文書間の食い違い | SPEC §1.4・§7.1、依存に `tzdata`、RESEARCH/DESIGN の訂正表、§6 決定記録。※NOTICE の著作権年の指摘だけはレビュアーの誤りで、P0-6 で撤回（実物は 2025） |
| 20 | 軽微 | PLAN の依存関係と粒度 | 本書 §2（接続テストを P4-4・P6-6 へ、P5-3/P6-5 を分割、依存の追加、NOTICE を P0-6 へ） |
| 21 | 軽微 | 複数セッション運用 | 本書 §0（タスク単位の更新・進捗の一元化）、§6 決定記録、SPEC §12、P0-6（`CLAUDE.md`） |
| 22 | 提案 | 需給を送らない担保の強化 | SPEC §2.7.3・§10.2 ／ P6-3・P6-5 |
| 23 | 提案 | 削るべき機能 | 1・2 を採用（SPEC §11）。3 は不採用（A 案で貸借ペインを残す）。4 は `confidence` の3段階化と空売りの副線削除のみ採用 |
| 24 | 提案 | イベント欄の配置 | SPEC §2.6 ／ P5-3・P5-4 |

---

## 8. 変更履歴

| 日付 | 版 | 内容 |
|---|---|---|
| 2026-09-20 | 1.0 | 初版作成 |
| 2026-09-20 | 1.1 | レビュー反映。起動時の自動更新（P1-7・P2-7・P4-5）とジョブ基盤（P1-6）を追加。EDINET を再設計（P4 を組み替え）。P0-6 を追加。P5・P6 を分割。進捗管理をタスク単位の更新に変更。決定記録と反映状況を追加。38 → 43 タスク |
