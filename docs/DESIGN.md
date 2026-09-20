# Chronos Chart 設計方針

前提となる調査結果は [RESEARCH.md](RESEARCH.md) を参照。
土台は Autotechnical（pywebview + yfinance + SQLite + Lightweight Charts v5）。

---

## 0. 確定した方針

| 論点 | 決定 |
|---|---|
| 信用残の取得 | **日証金CSV（`zandaka.csv`）＋ karauri.net の併用**。日証金は貸借銘柄のみが対象で全銘柄は網羅できない点を許容する |
| 空売り残高の取得 | **karauri.net をスクレイピング**（静的HTML） |
| AIへ送るデータ | **株価・テクニカル指標・EDINET開示のみ**。需給データ（空売り残高・信用残）は送信しない |
| 適時開示 | **EDINET のみ**。取得層を抽象化し、後から TDnet 実装を差し込める構造にする |
| 株価 | yfinance を継続 |
| チャート | Lightweight Charts v5 を継続 |
| 指標計算 | 土台の自前実装を継続（外部ライブラリを追加しない） |

### 送信データの境界（AIへ渡してよいもの・ダメなもの）

JPX の生成AI条項と Gemini 無料枠の学習利用を踏まえ、**コード上で境界を担保する**。

送ってよい:
- 銘柄コード・銘柄名・市場・通貨
- 株価（OHLCV）と、そこから計算したテクニカル指標
- EDINET の開示メタデータと、XBRL由来の財務数値

送らない:
- **空売り残高・信用残・貸借残高（JPX / 日証金 / karauri.net 由来のすべて）**
- ユーザーの保有株数・取得単価・損益・口座情報（そもそもアプリが保持しない）

実装では AI へ渡すペイロードを組み立てる関数を1箇所に集約し、
需給テーブルを参照しないことをテストで固定する。

---

## 1. データソースと取得方針

### 1.1 株価（既存）

yfinance。土台の `app/fetcher.py` をそのまま使う。

### 1.2 空売り残高 — karauri.net

- URL: `https://karauri.net/<証券コード4桁>/`
- パース対象: `<table id="sort" class="mtb2">`
  列は `計算日 / 空売り者 / 残高割合 / 増減率 / 残高数量 / 増減量 / 備考`
- 静的HTML。`requests` + `BeautifulSoup`（`beautifulsoup4` を依存に追加）

負荷対策（RESEARCH §3.5 に基づく）:
- **リクエスト間隔は既定 10 秒**（設定で変更可、下限 5 秒）
- **同時接続は 1**（銘柄をまたいでも直列）
- User-Agent は偽装せず、`ChronosChart/<version> (+contact)` の形式。連絡先はユーザーが設定画面で入力
- ETag / Last-Modified が返らないため HTTP キャッシュは使えない
  → **アプリ層で差分検知**: 取得済みの最新「計算日」を DB に持ち、
    取得後に新しい行が無ければその銘柄の再取得間隔を延ばす（既定 24 時間）
- 実行は手動トリガのみ。常駐ポーリングはしない

### 1.3 信用残（貸借取引残高） — 日証金

- URL: `https://www.taisyaku.jp/download/` 配下の `zandaka.csv`（日次・銘柄別）
- 併せて `meigara.csv`（貸借銘柄一覧）を取得し、対象銘柄かどうかを判定する
- ファイル単位の取得なので、**取得済み日付を DB に持ち未取得分のみ落とす**
- 規約が「私的利用の範囲」に限定しているため、**取得データの再配布・公開は行わない**旨を README と設定画面に明記

### 1.4 開示 — EDINET API v2

- APIキーは**ユーザーが設定画面で入力**（RESEARCH §1.1）。アプリに埋め込まない
- 取得フロー:
  1. `documents.json?date=YYYY-MM-DD&type=2` を**日付ごとに1回**取得（1日1リクエスト）
  2. `secCode` が登録銘柄と一致する行だけを抽出（`secCode` は5桁。末尾0の正規化が必要）
  3. `docTypeCode` で分類し、下記の方針で保存
- 取得間隔は**1リクエストあたり 1 秒以上**。当日分の再取得は1分に1回を上限とする
- 初回は登録銘柄の登録日以降を遡って埋める。以後は未取得日のみ

---

## 2. 開示書類の分類と保存方針

| 分類 | docTypeCode | 保存方法 |
|---|---|---|
| **DB正規化する** | 120/130（有報）, 140/150（四半期）, 160/170（半期） | `type=5` の CSV（TSV・UTF-16LE）を取得し、`disclosure_facts` に要素ID単位で格納 |
| **メタデータのみDB＋文書をローカル保存** | 180/190（臨時報告書）, 350/360（大量保有）, 220/230（自己株買付）, 240-320（公開買付関連）, 030/040（届出書） | `disclosures` にメタデータ。本文は `type=2`(PDF) を `data/disclosures/` に保存 |
| **イベント欄に出す** | 上記すべて | `disclosures` から生成 |

- 臨時報告書は `currentReportReason`（提出事由）をイベントのラベルに使う
- 大量保有報告書は `issuerEdinetCode` で発行会社を引き、**需給イベント**としてマーカーの色を分ける
- PDF は容量が大きいため、**既定では取得せず、ユーザーが個別に要求したときだけ落とす**（設定で一括取得も可）

保存先:

    data/
    ├── chronos.db
    ├── csv/                          # 既存（人が見る用）
    ├── disclosures/<symbol>/<docID>/ # PDF・XBRL CSV の展開物
    ├── reports/                      # AI分析レポート（HTML）
    └── logs/

---

## 3. データベース

**DDL の正は [SPEC.md](SPEC.md) §3 に置く。** ここでは設計上の判断だけを記す。

- 既存の `stocks` / `prices` / `indicators` は変更しない
- 追加するテーブル: `settings` / `short_positions` / `short_totals` / `margin_balances` /
  `disclosures` / `disclosure_facts` / `fetch_log` / `ai_usage` / `ai_reports`
- 土台は `indicators` のカラム構成が変わったらテーブルを作り直して株価から再計算する方式をとっているが、
  新テーブルは**再取得コストが高い**ため同じ方式は使えない。
  `schema_version` を持ち、バージョンごとの移行処理を関数で持つ（SPEC §3.1）
- ただし `short_totals` と `disclosure_facts` は元データ（`short_positions` / ローカル保存した XBRL CSV）から
  再生成できるため、構成変更時は再生成でよい

---

## 4. AI分析（Gemini）

### 4.1 クォータ管理（ご要望「使い果たしたらそれ以上できないようにする」）

- 上限値（RPM / TPM / RPD）と**モデル名は設定ファイル／設定画面で指定**。コードに埋め込まない
  （公式ページから Free Tier の数値表が消えており、ユーザーの AI Studio 画面の値が正）
- `ai_usage` に**太平洋時間（`zoneinfo.ZoneInfo("America/Los_Angeles")`）基準の日付**で積算
- **送信前ガード**: `count_tokens` で見積もり、RPD / TPM の残量を超えるなら**送信せずに中止**し、
  「本日の無料枠を使い切りました。太平洋時間の0時（日本時間の午後4時または5時）にリセットされます」と表示
- 429 / `RESOURCE_EXHAUSTED`（`google.genai.errors.ClientError`）を受けた場合も
  その日はそのモデルへの送信を打ち切る
- RPM超過とRPD超過はレスポンスから区別できないため、**自前カウンタを正とする**

### 4.2 構造化出力と再依頼

Gemini には**HTMLを書かせず、JSONで分析結果だけを返させる**。HTML/CSS は Jinja2 で組み立てる。

```python
class AnalysisReport(BaseModel):
    # フィールド定義順が出力順に反映されるので「結論 → 根拠」の順に並べる
    verdict: Literal["bullish", "bearish", "neutral"]
    confidence: int                      # 0-100
    summary: str
    technical: SectionAnalysis
    disclosure: SectionAnalysis
    risks: list[str]
    watch_points: list[str]
```

- `response_mime_type="application/json"` + `response_schema=AnalysisReport`、`temperature=0.1`
- 受信後は必ず `model_validate_json`。失敗したら**バリデーションエラー本文をプロンプトに添えて再依頼**（上限2回）
- パース前に `finish_reason == "MAX_TOKENS"` を確認し、切れていたら分割生成に切り替える
- 再依頼もクォータを消費するので、リトライ分を事前見積りに含める

### 4.3 プロンプトに載せるデータ

トークン節約のため、自然文ではなく簡潔な表形式で渡す。

- 銘柄の基本情報
- 直近N日（既定60日）の OHLCV と主要指標を CSV ブロックで
- 最新の指標判定とシグナル（土台の `evaluate_latest` / `detect_signals` の出力を再利用）
- EDINET 開示の一覧（日付・種別・概要。本文は載せない）
- **需給データは載せない**（§0）

土台に `app/ai_export.py`（AI向け Markdown/CSV 出力）が既にあるので、これを拡張して流用する。

### 4.4 実行タイミング

ご要望通り**任意のタイミングで手動実行**。自動実行はしない。
実行前に「今回の想定トークン数」と「本日の残量」を表示して確認を取る。

---

## 5. 画面

### 5.1 既存タブの拡張

**ダッシュボード**:
- サブペインに **空売り残高**（残高割合の合計）と **貸借残高**（貸付・借入）を追加
  - 既存のチップ切り替えの仕組みにそのまま追加する
  - 週次・欠損日は **whitespace data**（`{ time: d }`）で時間軸を保ち、**`lineType: 2`（階段状）** で描く
- ローソク足に**開示イベントのマーカー**を追加（`createSeriesMarkers`）
  - 書類種別で色と形を分ける（決算系 / 需給系 / その他）
- **チャート下にイベント欄**を新設
  - `timeScale().subscribeVisibleTimeRangeChange()` で表示範囲に連動してフィルタ
  - 行をクリックすると該当日にスクロールし、文書があればローカルファイルを開く
  - マーカーにヒットテストAPIは無いため、`subscribeClick` の `param.time` から引く

### 5.2 新規タブ

**設定**:
- EDINET APIキー（取得手順と発行ページへのリンクを併記）
- Gemini APIキー・モデル名・RPM/TPM/RPD
- スクレイピングの間隔と連絡先 User-Agent
- 各データソースの規約と、取得データを再配布しない旨の明示

**レポート**:
- 銘柄と期間を選んで AI 分析を実行
- 本日の残量表示、生成済みレポートの一覧、HTMLを開く

---

## 6. モジュール構成

```
app/
├── api.py            既存 + 新規メソッド（設定・開示・需給・レポート）
├── service.py        既存
├── fetcher.py        既存（yfinance）
├── indicators.py     既存
├── database.py       既存 + 新テーブル
├── csv_export.py     既存
├── ai_export.py      既存（AI向けデータ整形を流用）
├── config.py         既存 + 新規定数
├── settings.py       [新] settings テーブルの読み書き・APIキー管理
├── sources/          [新] 外部データ取得
│   ├── base.py         共通のHTTPクライアント（間隔制御・UA・リトライ）
│   ├── karauri.py      空売り残高
│   ├── taisyaku.py     日証金 貸借残高
│   └── edinet.py       EDINET API v2
├── disclosures.py    [新] 書類の分類・XBRL CSV のパース・ローカル保存
├── events.py         [新] disclosures → チャートイベントへの変換
└── ai/               [新]
    ├── client.py       google-genai のラッパ
    ├── quota.py        ai_usage によるクォータ管理
    ├── schema.py       Pydantic の出力スキーマ
    ├── prompt.py       プロンプト組み立て（需給を含めないことを保証）
    └── report.py       Jinja2 で単一HTML生成

web/
├── index.html        タブ追加（設定・レポート）
├── js/chart.js       ペイン追加・イベントマーカー・イベント欄連動
├── js/app.js         画面制御
└── vendor/           + NOTICE ファイルを追加（Apache-2.0 §4(d)）

templates/            [新] Jinja2（レポートHTML）
```

依存追加: `beautifulsoup4`, `lxml`, `requests`, `google-genai`, `pydantic`, `jinja2`

---

## 7. 実装順序

1. 基盤: `settings` テーブルと設定タブ、`sources/base.py`（間隔制御つきHTTPクライアント）、NOTICE 追加
2. 需給: karauri.net → `short_positions` / `short_totals`、日証金 → `margin_balances`
3. チャート: 需給サブペイン（whitespace data + 階段線）
4. 開示: EDINET 一覧取得 → `disclosures`、分類とローカル保存、XBRL CSV → `disclosure_facts`
5. イベント: マーカーとイベント欄、表示範囲連動
6. AI: クォータ管理 → スキーマ → プロンプト → 再依頼 → Jinja2 レポート
7. テスト: 各段階で pytest。外部通信は環境変数 `CHRONOS_LIVE` でゲート（土台の `AUTOTECHNICAL_LIVE` を改名）

各段階で動く状態を保ち、段階ごとにコミットする。
