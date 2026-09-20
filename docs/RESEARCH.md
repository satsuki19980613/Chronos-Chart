# Chronos Chart 事前調査メモ

調査日: 2026-09-20 / 土台: Autotechnical (satsuki19980613/Autotechnical)

各項目に一次情報の URL を付す。数値・仕様は変動するため、実装着手時に再確認すること。

---

## 1. EDINET API

### 1.1 APIキーはユーザー各自の取得が必須

- v1（キー不要）は **2024-03-29 で提供終了**。現行 v2 は APIキー必須。
- 取得は無料・個人可。ただし手続きは重い:
  1. EDINET閲覧サイトでサインアップ（Microsoft Azure AD B2C）
  2. メールアドレス登録 → CAPTCHA → 確認コード
  3. パスワード設定（12〜256文字 / 英大小・数字・記号から3種以上）
  4. **MFA必須** — 電話番号を登録し SMS または音声通話で確認
  5. 連絡先（所属・氏名・電話番号）入力 → APIキー表示
- 発行ページ: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1
- 削除ページ: https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=2
- **2年間利用がないキーは自動削除**される。
- キー共有の明文禁止はないが、規約が「短時間における大量のアクセス」「API機能の健全な運営を害する一切の行為」を禁止し、
  連絡先を**キー単位の利用状況照会に使う**旨が明記されている → アプリにキーを埋め込んで配布するのは規約リスクが高い。

→ **設計方針: 設定画面でユーザー自身にキーを入力させる。取得手順を画面内に表示する。**

### 1.2 エンドポイント

キーは **クエリパラメータ `Subscription-Key`**（ヘッダではない。`Ocp-Apim-Subscription-Key` が通るという情報は非公式）。

書類一覧:

    GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=<KEY>

- `type=1` メタデータのみ / `type=2` 提出書類一覧＋メタデータ
- `date` は当日以前、直近財務局営業日24時から **10年以内**
- クロスドメイン不可（ブラウザJSから直接は呼べない → Python側で叩く）

書類取得:

    GET https://api.edinet-fsa.go.jp/api/v2/documents/<docID>?type=<1-5>&Subscription-Key=<KEY>

| type | 内容 | 形式 |
|---|---|---|
| 1 | 提出本文書及び監査報告書（XBRL含む） | ZIP |
| 2 | PDF | PDF |
| 3 | 代替書面・添付文書 | ZIP |
| 4 | 英文ファイル | ZIP |
| 5 | **XBRL→CSV変換済** | ZIP |

### 1.3 documents.json のレスポンス項目（抜粋）

`seqNumber` `docID` `edinetCode` `secCode` `JCN` `filerName` `fundCode` `ordinanceCode` `formCode`
`docTypeCode` `periodStart` `periodEnd` `submitDateTime` `docDescription` `issuerEdinetCode`
`subjectEdinetCode` `subsidiaryEdinetCode` `currentReportReason` `parentDocID` `opeDateTime`
`withdrawalStatus` `docInfoEditStatus` `disclosureStatus` `xbrlFlag` `pdfFlag` `attachDocFlag`
`englishDocFlag` `csvFlag` `legalStatus`

`secCode` で銘柄と突き合わせる（5桁。末尾0付きの点に注意）。

### 1.4 docTypeCode（主要）

| コード | 書類名 |
|---|---|
| 120 / 130 | 有価証券報告書 / 訂正 |
| 140 / 150 | 四半期報告書 / 訂正 |
| 160 / 170 | 半期報告書 / 訂正 |
| 180 / 190 | 臨時報告書 / 訂正 |
| 220 / 230 | 自己株券買付状況報告書 / 訂正 |
| 235 / 236 | 内部統制報告書 / 訂正 |
| 240-320 | 公開買付関連（届出・報告・意見表明等） |
| 350 / 360 | 大量保有報告書 / 訂正 |
| 030 / 040 | 有価証券届出書 / 訂正 |

全一覧は API仕様書 4-1 参考資料。

### 1.5 DB正規化の可否

**正規化に向く（type=5 のCSVでタクソノミ準拠の数値が取れる）**: 120/130, 140/150, 160/170

EDINET CSV の構造:

- ZIP内 `XBRL_TO_CSV` フォルダ
- 拡張子 .csv だが**実体はタブ区切り(TSV)、UTF-16LE、CRLF、各値をダブルクォートで囲む**
- 9列固定: `要素ID / 項目名 / コンテキストID / 相対年度 / 連結・個別 / 期間・時点 / ユニットID / 単位 / 値`
- 空白 = 日本語名未定義 / `-` = 値が0またはユニット未設定（両者は区別される）
- 値は30,000文字で切り詰められる

**正規化に向かない**: 臨時報告書(180) は提出事由ごとに様式が異なる自由記述。
ただし `currentReportReason`（提出事由）はメタデータから取れるので**イベントとしては登録可能**。
公開買付・意見表明・有価証券届出書も文章主体。

### 1.6 レート制限・規約

- 具体的な数値上限は**非公開**。429 Too Many Requests が定義されている。
- 当日データは日本時間 **8:30 過ぎから原則1分毎**に更新。過去分は24時過ぎに日次更新1回。
  → 1分に1回以上のポーリングは無意味。
- 利用時は**出典明記が必須**。
- 縦覧期間: 有報・半期は最大10年、四半期は最大10年。大量保有報告書等は延長期間なし。

### 1.7 公式URL

| 資料 | URL |
|---|---|
| API仕様書 v2 (2026年6月版) | https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf |
| 書類閲覧 操作ガイド | https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140133.pdf |
| API関連資料一覧 | https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html |
| 利用規約 | https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html |
| EDINETコードリスト(ZIP) | https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip |

---

## 2. 適時開示（TDnet）— EDINETでは取得できない

**EDINET = 法定開示（金融庁）/ TDnet = 適時開示（東証）** で管轄が別。
**決算短信・業績予想の上方修正は TDnet にしか無い。**

| 手段 | 個人可否 | コスト |
|---|---|---|
| TDnet API（JPX総研公式） | 約款契約が必要・法人前提 | 有料 |
| J-Quants API TDnetアドオン | 可（2026-05-18 開始） | 月額 11,000円 |
| 適時開示情報閲覧サービスのスクレイピング | 規約上グレー、**公開は約31日で消える** | 無料だが非推奨 |

→ **無料かつ合法な TDnet 取得手段は事実上存在しない。**
→ EDINET のみで実装し、取得層を抽象化して後から差し込めるようにする。

参考: 決算短信サマリにも XBRL があり、タクソノミは EDINET タクソノミの拡張。
https://www.jpx.co.jp/equities/listing/disclosure/xbrl/nlsgeu000005vk0b-att/File_Specification_for_TDnet_Filing.pdf

---

## 3. 空売り残高・信用残

### 3.1 用語の区別（混同しやすい）

| 用語 | 制度 | 公表 | 粒度 |
|---|---|---|---|
| **空売り残高**（カラ売り残） | 金商法の空売り残高報告制度。発行済株式総数の **0.5%以上**のポジションに報告義務 | JPXが**日次**公表 | 報告義務者（機関）ごと |
| **信用取引残高**（信用買残・売残） | 信用取引の現在高 | JPXが**週次**（週末残高）公表 | 銘柄ごと |
| **貸借取引残高** | 日証金の貸借取引（制度信用に付随） | 日証金が**日次**公表 | 貸借銘柄のみ |

### 3.2 karauri.net

robots.txt 全文:

    User-agent: MJ12bot
    Disallow: /
    User-agent: AhrefsBot
    Disallow: /
    User-agent: BLEXBot
    Disallow: /
    User-agent: SemrushBot
    Disallow: /
    User-agent: baiduspider
    Disallow: /

- `User-agent: *` の包括規定なし。Crawl-delay なし。Sitemap なし（sitemap.xml は404）。
- SEO系クローラー5種のみ名指しで禁止 → 一般的なプログラム取得を明示的に禁じてはいない。
- `/help/about.php` にもスクレイピング禁止・再配布禁止の条項は**見当たらない**。
- 運営者情報の記載なし。連絡先はメールアドレスのみ（難読化表示）。
- 自サイトで「集計内容が万全であるとは限りません」と免責。

構造（実HTML確認済み）:

- 銘柄別ページ: `https://karauri.net/7203/`（証券コード4桁）
- 企業情報: `<table class="mtb1">`
- 空売り残高: `<table id="sort" class="mtb2">`
  - 列: `計算日 / 空売り者 / 残高割合 / 増減率 / 残高数量 / 増減量 / 備考`
  - 行は `<tr class="obb">` と `<tr class="occ">` の交互（ゼブラ）
  - 増減マイナス `class="ct co_br"` / プラス `class="ct co_red"`
  - 機関名は `<a href="/[コード]/?f=[機関ID]">`
  - 備考に `<span class="after">報告義務消失</span>` / `再IN（前回YYYY-MM-DD）`
- **サーバーサイドレンダリングの静的HTML。requests + BeautifulSoup で取得可能。ヘッドレスブラウザ不要。**
- tablesorter によるソートはUI上のみでデータ取得に無関係。
- ページネーションなし（履歴は1ページに全件展開）。
- **JSON API / CSV エンドポイントは見つからず**（/api/, /data.json, /export.csv, /csv/, /download/ すべて404）。
- **`Last-Modified` / `ETag` / `Cache-Control` を一切返さない。If-Modified-Since を付けても常に200で全文が返る**
  → HTTPキャッシュによる差分取得は不可能。アプリ層で差分検知するしかない。

**重大な制約: karauri.net に銘柄別の信用残の時系列は無い。**
`/sinyou/`（信用取引ランキング）と `/taisyaku/`（貸借残高ランキング）は**現時点のランキング一覧のみ**で、
`?date=` を付けても内容が変わらない（過去閲覧機能なし）。銘柄詳細ページにも信用残は含まれない。
→ karauri.net から取れるのは **空売り残高（機関別・時系列）のみ**。

### 3.3 一次ソース

| ソース | URL | 形式 | 更新 |
|---|---|---|---|
| JPX 空売り残高 | https://www.jpx.co.jp/markets/public/short-selling/index.html | **Excel (.xls)** `YYYYMMDD_Short_Positions.xls` | 日次 |
| JPX 銘柄別信用取引週末残高 | https://www.jpx.co.jp/markets/statistics-equities/margin/05.html | **PDF**（CSV/Excelは確認できず） | 週次 |
| 日証金 ダウンロード | https://www.taisyaku.jp/download/ | **CSV** (`zandaka.csv` 銘柄別残高, `meigara.csv`, `shina.csv`, `seigenichiran.csv`) | 日次 |

一次ソースの利点: ファイル名に日付が入るため**未取得日だけを取れる＝差分管理が容易**。データも信頼できる。
欠点: 信用残がPDF中心で機械可読性が低い。時系列化・増減計算は自前。

### 3.4 規約上の注意（重要）

JPX サイト利用規約 https://www.jpx.co.jp/term-of-use/ より:

> 当サイトに掲載されている情報について、有料・無料を問わずJPXからの許諾を得ている場合を除き、
> 商用目的によるデータ収集のほか如何なる用途に関わらず二次利用及び再配信はできません。

> 利用者が生成AI等を用いて当サイトの情報を学習・解析・生成に利用する場合であっても、
> 著作権及びその他の権利・利益を侵害しないことはもちろん、当サイトの趣旨や運営方針に反する
> 不適切な利用を含め、JPXが不利益を被る可能性のある一切の行為を禁止します。

日証金 https://www.taisyaku.jp/download/ より:

> 私的利用の範囲を超えて利用することはできず、また、権利者の許可なく改変、複製、賃貸、貸与、
> 販売、出版、送信、放送等、方法の如何を問わず第三者の利用に供することを固く禁じます。

→ **「一次ソースを直接叩けば規約上クリーン」ではない。** 一次ソース側にこそ明確な制限がある。
→ **個人の私的な投資判断のための参照**に用途を限定し、取得データの再配布・公開は行わない。
→ JPX の生成AI条項は、**JPX由来データを外部LLMに送ることへの留意点**として扱う（§4.1 と併せて検討）。

### 3.5 負荷をかけない作法

- karauri.net は ETag/Last-Modified 非対応、Crawl-delay 未指定 → 許容量が外部から不明。
  **最低でも数秒〜十数秒に1リクエスト、同時接続は1本**。
- User-Agent は偽装せず、**連絡先を含む識別可能なUA**を設定する。
- 差分検知はアプリ層で行う（取得済みの計算日をDBに持ち、新しい行が無ければ再取得頻度を落とす）。
- 空売り残高の公表は取引時間外（夕方〜夜）→ **深夜〜早朝のバッチ取得**が望ましい。
- 一次ソース利用時は既知の日付リストと突合し、未取得分のみ取得。

### 3.6 法的論点（要点）

- 事実・数値データ自体に著作権は発生しない（著作権法は表現を保護し事実を保護しない）。
  ただしレイアウト・解説文・編集著作物としての構成には及び得る。
- **著作権法30条の4**（非享受目的の情報解析）は解析目的の複製に有利に働き得るが、
  取得コンテンツをそのまま表示・再配布する行為（享受目的）には適用されない。
- 利用規約違反は債務不履行・不法行為として損害賠償の対象になり得る。
- **岡崎市立図書館事件（Librahack, 2010）**: 悪意なきクローリングでもサーバーに過度な負荷をかければ
  刑事責任を問われるリスクがある（最終的に不起訴）。

---

## 4. Gemini API 無料枠

### 4.1 無料枠では入力が学習に使われる（最重要）

利用規約 https://ai.google.dev/gemini-api/terms より:

- 無料枠（Unpaid Services）: 送信内容と生成結果を Google 製品の提供・改善・開発に使用する。
  **人間のレビュアーが読み・注釈をつけ・処理する場合がある**（Googleアカウント等との紐付けは解除して処理）。
  「機密・個人情報を送信しないこと」と明記。
- 有料枠（Paid Services）: プロンプト・応答をモデル改善に使わない。人間レビューなし。
- EEA/スイス/英国は無料枠でも有料枠と同等の保護。**日本は対象外。**

→ **設計方針: 送信対象は銘柄コード・株価・指標・開示メタデータ・需給数値のみ。
保有株数・取得単価・口座情報など個人の投資状況は構造的に送信対象に含めない。**

### 4.2 レート制限はハードコードできない

公式レート制限ページ https://ai.google.dev/gemini-api/docs/rate-limits には
**モデル別 Free Tier の数値表がもはや掲載されておらず**、
「自分のアカウントの実際の上限は https://aistudio.google.com/rate-limit で確認せよ」という案内のみ。
2025年末〜2026年にかけて無料枠が大幅削減され、モデル世代交代も進行中で変動が激しい。

確定している事項:

- ティアは Free / Tier 1 / Tier 2 / Tier 3 の4段階（Cloud Billing の累計支出で自動昇格）
- 超過時は **429 / `RESOURCE_EXHAUSTED`**
- **RPD のリセットは太平洋時間の深夜0時**

→ **設計方針: モデル名と RPM/TPM/RPD を設定ファイルに外出しし、ユーザーが自分の値を入力できるようにする。**

### 4.3 クォータ管理は自前で行う必要がある

- SDK例外は `google.genai.errors.ClientError`（429 RESOURCE_EXHAUSTED）
- **RPM超過とRPD超過を区別するフィールドはレスポンスに無い**
  → サーバー応答だけでは「今日はもう打ち止め」を判定できない
- 事前見積り: `client.models.count_tokens(model=..., contents=prompt)`
- 使用量: `response.usage_metadata` の `prompt_token_count` / `candidates_token_count` / `total_token_count`
  （実装時に実レスポンスを print して確認すること）
- SDKは一時的エラーを**最大4回、初回約1秒・最大60秒の指数バックオフで自動リトライ**する（組み込み動作）
  リトライ対象: 429, 408, 5xx / 非対象: 400, 402, 403

→ **設計方針: SQLite に「日付(`America/Los_Angeles`基準) + モデル + リクエスト数 + 消費トークン」を永続化。
送信前に count_tokens で見積もり、自前カウンタで上限に達していたら送信せず停止する。
夏時間があるので `zoneinfo.ZoneInfo("America/Los_Angeles")` を使う。**

### 4.4 構造化出力

SDK は **`google-genai`**（旧 `google-generativeai` は 2025-11-30 に廃止済み。バグ修正も 2025-08-31 で終了）。
PyPI: https://pypi.org/project/google-genai/ / 環境変数は **`GEMINI_API_KEY`**

```python
from google import genai
from google.genai import types
from pydantic import BaseModel

client = genai.Client()
response = client.models.generate_content(
    model="...",
    contents=prompt,
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=AnalysisReport,   # Pydantic モデルをそのまま渡せる
        temperature=0.1,
    ),
)
```

- 対応型: string, number, integer, boolean, object, array, null（`["string","null"]` で Optional 相当）
- enum は `Literal["a","b"]`、ネスト・配列・再帰（$ref）も可、`Field(description=...)` が反映される
- **Pydantic のフィールド定義順が出力順に反映される** → 「結論 → 根拠」の順に定義すると質が上がる
- スキーマ指定しても壊れることはある → `model_validate_json` の例外を捕捉し、
  **バリデーションエラー本文をプロンプトに添えて再依頼**（2〜3回上限）
- **`finish_reason == "MAX_TOKENS"` で切れた JSON** はパース前に検知し、分割生成へ切り替える
- thinking: 思考トークンも出力としてカウントされクォータを消費する → 低め設定から始める
  （2.5系は `thinking_budget` 整数、3.x系は `thinking_level` の段階指定に変更された模様）
- コンテキストキャッシュが無料枠で使えるかは**公式に明記なし（未確認）**。最小トークン数の縛りあり。

### 4.5 トークン節約

- 時系列は自然文ではなく CSV/TSV 的な簡潔表現で渡す
- 全期間ではなく直近N日＋計算済み指標のみ。計算はPython側で済ませ、モデルには解釈をさせる
- 開示の長文は要約してから渡す
- スキーマのフィールドと description は必要最小限に

---

## 5. チャート

### 5.1 ライブラリ

土台が既に採用している **TradingView Lightweight Charts v5.2.1（Apache-2.0）を継続**。
比較検討した結果、ビルドツール無し・CDN/同梱という制約下で
「複数pane + マーカー + スクリーンショット」を追加ライブラリ無しで満たせるのはこれだけ。

| ライブラリ | ライセンス | 備考 |
|---|---|---|
| Lightweight Charts | Apache-2.0 | 帰属表示の義務あり（後述） |
| Chart.js + chartjs-chart-financial | MIT | 複数pane非対応、financial拡張はメンテ停滞気味 |
| ECharts | Apache-2.0 | 汎用。金融特化が弱い |
| Highcharts Stock | 商用有償 | 機能は最強だが有料 |
| Plotly.js | MIT | 大量データ・多パネルで重い |

**帰属表示**: Apache-2.0 §4(d) により NOTICE の内容を配布物に含める義務がある。
NOTICE 実体は2行:

    TradingView Lightweight Charts™
    Copyright (c) 2025 TradingView, Inc. https://www.tradingview.com/

`attributionLogo` オプションは**既定 true** で、土台も未設定なのでロゴは表示されている（規約は満たす）。
ただし **`web/vendor/` に NOTICE ファイルが同梱されていない** → 追加が必要（TODO）。

### 5.2 v4 → v5 の変更点（土台は既に v5 準拠）

| 項目 | v4 | v5 |
|---|---|---|
| シリーズ生成 | `chart.addCandlestickSeries(opts)` | `chart.addSeries(CandlestickSeries, opts, paneIndex?)` |
| マーカー | `series.setMarkers([...])` | `createSeriesMarkers(series, markers)`（別関数・プラグイン形式） |
| ウォーターマーク | createChart のオプション | `createTextWatermark(pane, opts)` |
| 複数pane | 無し（別チャートを手動同期） | ネイティブ対応（`paneIndex` / `series.moveToPane(n)` / `chart.panes()`） |

単一チャート内の pane は**時間軸が自動的に共有・同期**される（同期コード不要）。

### 5.3 週次データを日足に重ねる

値のない日は `{ time: d }` だけの **whitespace data** を入れて時間軸の連続性を保つ:

```js
const marginData = allDailyDates.map(d =>
  weekly[d] !== undefined ? { time: d, value: weekly[d] } : { time: d }
);
```

線で補間すると嘘のグラフになるため、信用残は **`lineType: 2`（階段状）** が適切。

### 5.4 イベントマーカー

```js
LWC.createSeriesMarkers(candleSeries, [
  { time: '2026-05-09', position: 'aboveBar', color: '#2196F3', shape: 'circle', text: '決算' },
]);
```

**マーカー自体にクリックのヒットテストAPIは無い。**
`chart.subscribeClick` / `subscribeCrosshairMove` で `param.time` を拾い、
自前のイベント配列から時刻一致で引くのが標準パターン（土台は既に subscribeCrosshairMove を使用中）。

下部のイベントタイムライン欄との連動は
`chart.timeScale().subscribeVisibleTimeRangeChange()` で表示範囲を取得してフィルタ、
逆方向は `setVisibleRange()` / `scrollToPosition()` で双方向連動。

### 5.5 株価ソース

| 取得源 | 信用残 | 遅延 | 備考 |
|---|---|---|---|
| yfinance（土台が採用） | なし | 約15分 | Yahoo非公式・規約上グレー。土台のREADMEに注意書き済み |
| J-Quants **Free** | **取得不可** | **12週間** | 3エンドポイントのみ。株価ソースとしても使えない |
| J-Quants Standard以上 | 取得可 | — | 有料 |
| stooq | なし | 約1日 | 2026年6月頃からBot対策導入との情報（未確認） |

→ **yfinance 継続が妥当。**

### 5.6 指標計算

土台は**自前実装（pandas の rolling/ewm）で15種を実装済み・外部依存なし**。これを継続する。

- `pandas-ta`: 最新は 0.4.71b0 のプレリリースで「本番非推奨」、**2026-07-01 までにアーカイブする旨の告知**あり → 採用しない
- `TA-Lib`: Windows で pip 単独だとビルドエラー。非公式wheel か conda が必要 → 避ける
- `ta`: 純Python・pip一発。必要になれば追加検討

### 5.7 HTMLレポート

- Jinja2 テンプレートで単一HTMLを生成
- 画像は matplotlib → BytesIO → base64 データURI でインライン化
- CSS は `<style>` に直接記述
- Lightweight Charts の `chart.takeScreenshot()` は v4/v5 とも標準搭載で `HTMLCanvasElement` を返す
  → `canvas.toDataURL('image/png')` でブラウザ側からチャート画像を取得し、Python に渡して埋め込むことも可能

---

## 6. 未確認・実装時に再確認すべき事項

1. Gemini 無料枠のモデル別 RPM/TPM/RPD の現在値（AI Studio のアカウント画面で要確認）
2. 429 レスポンスボディの正確なフィールド構造
3. `usage_metadata` のフィールド名（実レスポンスで確認）
4. コンテキストキャッシュの無料枠対応可否
5. EDINET 利用規約の正確な文言（AI要約経由で取得したため原文で再確認）
6. JPX 銘柄別信用取引週末残高に CSV/Excel 版が存在しないか（PDFしか確認できなかった）
7. Lightweight Charts の `localization` 日本語ロケール指定の詳細
