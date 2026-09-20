# Chronos Chart 仕様書

| 項目 | 内容 |
|---|---|
| 版 | 1.0 |
| 作成日 | 2026-09-20 |
| 状態 | レビュー前 |
| 土台 | Autotechnical (satsuki19980613/Autotechnical) |
| 関連文書 | [RESEARCH.md](RESEARCH.md) 調査結果 / [DESIGN.md](DESIGN.md) 設計方針 / [PLAN.md](PLAN.md) 実装計画 |

**本書がデータモデルと機能仕様の正（single source of truth）である。**
DESIGN.md は「なぜその設計にしたか」の根拠を示す文書であり、仕様が食い違う場合は本書を優先する。

---

## 1. 概要

### 1.1 目的

日本株の個別銘柄について、株価・テクニカル指標・需給（空売り残高／貸借取引残高）・法定開示を
1つのチャート上で突き合わせて見られるローカルGUIツール。
任意のタイミングで Gemini API による総合分析レポート（HTML）を生成できる。

### 1.2 動作環境

- Windows 11（主）。macOS / Linux でも動作を妨げない実装とする
- Python 3.10 以上
- GUI は pywebview（画面は HTML/CSS/JS）
- ネットワークは各データソースへの HTTPS アウトバウンドのみ
- 完全ローカル動作。ユーザーのデータを外部に送信するのは Gemini API 呼び出しのみ（§7）

### 1.3 スコープ

**やること**

- 空売り残高の自動取得（karauri.net）
- 貸借取引残高の自動取得（日本証券金融）
- EDINET API v2 による法定開示の取得・分類・保存
- 需給のチャート表示（サブペイン）
- 開示イベントのチャートマーカーと、チャート下のイベント欄
- Gemini API による総合分析レポートの生成（手動実行・無料枠の自前管理）

**やらないこと**

| 項目 | 理由 |
|---|---|
| 決算短信・業績予想の上方修正の取得 | TDnet 管轄で EDINET に存在せず、無料かつ合法な取得手段が無い（RESEARCH §2） |
| 取得データの再配布・公開・サーバー設置 | JPX・日証金の規約が二次利用／私的利用超過を禁止（RESEARCH §3.4） |
| 需給データの AI への送信 | JPX の生成AI条項と Gemini 無料枠の学習利用の重なりを避ける（§7.2） |
| 自動売買・発注 | 本ツールは分析用途に限る |
| 常駐ポーリング・スケジュール実行 | 対象サーバーへの負荷を最小化するため、取得はすべて手動トリガ |
| ユーザーの保有株数・取得単価・損益の管理 | そもそも保持しない |

### 1.4 用語

| 用語 | 定義 |
|---|---|
| **空売り残高** | 金商法の空売り残高報告制度に基づく残高。発行済株式総数の 0.5% 以上で報告義務。**報告義務者（機関）ごと**に公表される |
| **貸借取引残高** | 日本証券金融が公表する貸借取引の残高。**貸借銘柄のみ**が対象 |
| **信用取引残高** | 信用買残・売残。JPX が週次で公表するが PDF のみのため本ツールでは扱わない |
| **計算日** | 空売り残高の基準日。報告者ごとに異なる |
| **docID** | EDINET の書類管理番号。開示書類の一意キー |
| **法定開示** | 金商法に基づく開示。EDINET で公開される |
| **適時開示** | 取引所規則に基づく開示。TDnet で公開される。本ツールの対象外 |

> **UI 上の表記に関する制約**
> 日証金から取得するのは貸借取引残高であって信用取引残高そのものではない。
> 画面・レポート・CSV のラベルは必ず「貸借取引残高（日証金）」と表記し、
> 「信用残」という語を単独で使わない。出典と対象銘柄の限定も併記する。

---

## 2. 機能仕様

### 2.1 設定（新規タブ）

#### 2.1.1 設定項目

| キー | 型 | 既定値 | 説明 |
|---|---|---|---|
| `edinet_api_key` | str | 空 | EDINET API v2 のサブスクリプションキー |
| `gemini_api_key` | str | 空 | Gemini API キー |
| `gemini_model` | str | 空 | 使用するモデル名。ユーザーが指定する |
| `gemini_rpm` | int | 0 | 1分あたりリクエスト上限。0 は「未設定＝AI機能を無効」 |
| `gemini_tpm` | int | 0 | 1分あたりトークン上限 |
| `gemini_rpd` | int | 0 | 1日あたりリクエスト上限 |
| `gemini_max_output_tokens` | int | 8192 | 出力トークン上限 |
| `scrape_interval_sec` | int | 10 | スクレイピングのリクエスト間隔（秒）。下限 5 |
| `scrape_contact` | str | 空 | User-Agent に含める連絡先 |
| `disclosure_fetch_pdf` | bool | false | 開示PDFを一括取得するか |
| `short_recheck_hours` | int | 24 | 新着が無かった銘柄の再取得を抑止する時間 |

#### 2.1.2 APIキーの扱い

- **キーはアプリに埋め込まない。** 必ずユーザーが入力する（RESEARCH §1.1）
- `settings` テーブルに保存する。DB ファイルは `.gitignore` 済みの `data/` 配下
- **画面ではマスク表示**（先頭4文字＋`****`）。「表示」ボタンで一時的に平文表示
- **ログにキーを出力しない。** URL をログに出す際は `Subscription-Key` の値を `***` に置換する
- 環境変数 `CHRONOS_EDINET_API_KEY` / `GEMINI_API_KEY` があれば設定値より優先する

#### 2.1.3 画面要件

- EDINET キーの欄には**取得手順を併記**する。手順が重いため（サインアップ → CAPTCHA → パスワード → **MFA（電話番号＋SMS/音声）** → 連絡先入力）、
  この5段階を明示し、発行ページ `https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1` へのリンクを置く
- 「2年間利用がないキーは自動削除される」旨を注記
- Gemini の上限値欄には「公式ドキュメントに無料枠の数値表は無い。`https://aistudio.google.com/rate-limit` で自分のアカウントの値を確認して入力すること」と明記
- 各データソースの規約要約と、**取得データを再配布しない**旨を表示
- 接続テストボタン（EDINET / Gemini）。EDINET は `documents.json` を1回、Gemini は最小プロンプトで疎通確認

### 2.2 空売り残高の取得

#### 2.2.1 取得元

`https://karauri.net/<証券コード4桁>/`

証券コードは `stocks.symbol` の `.T` を除いた4桁部分（`app/fetcher.py` の `code_from_symbol`）。
**国内銘柄（`.T` サフィックス）のみ対象。** 米国株等は対象外としてスキップする。

#### 2.2.2 パース仕様

- 対象テーブル: `<table id="sort" class="mtb2">`
- 列順: `計算日 / 空売り者 / 残高割合 / 増減率 / 残高数量 / 増減量 / 備考`
- データ行: `<tr class="obb">` と `<tr class="occ">`（ゼブラ用の交互クラス。両方をデータ行として扱う）
- 数値のパース:
  - 残高割合・増減率: `%` とカンマを除去して float。空文字・`-` は NULL
  - 残高数量・増減量: カンマを除去して int。空文字・`-` は NULL
  - 増減のマイナスは `class="ct co_br"`、プラスは `class="ct co_red"` で色分けされているが、**値の符号は文字列から読む**（クラスに依存しない）
- 備考: `<span class="after">報告義務消失</span>` や `再IN（前回YYYY-MM-DD）` をそのまま文字列で保存
- **テーブルが見つからない／列数が想定と違う場合はエラーとして扱い、部分的な保存をしない**（サイト構造変更の検知）

#### 2.2.3 残高合計の算出（`short_totals`）

計算日は報告者ごとに異なるため、単純な日付ごとの合計はできない。以下で算出する。

1. 対象銘柄の `short_positions` から計算日の集合 D を取る
2. 各 d ∈ D について、報告者ごとに `calc_date <= d` を満たす最新の1件を選ぶ
3. その1件の `note` が報告義務消失を示す場合、その報告者は当該日の合計に含めない
4. `total_ratio` = 選ばれたレコードの `ratio` の合計、`holders` = 件数、`total_qty` = `quantity` の合計
5. 結果を `short_totals` に全置換で保存する

> 合計値はサイトが公表している値ではなく**本ツールが算出した値**である。
> 画面とレポートにその旨を注記する。

#### 2.2.4 負荷対策（遵守必須）

- **リクエスト間隔は `scrape_interval_sec`（既定10秒、下限5秒）。** 銘柄をまたいでも直列で、同時接続は1
- User-Agent は `ChronosChart/<version> (+<scrape_contact>)`。**ブラウザの偽装をしない**
- `scrape_contact` が未設定の場合、スクレイピングを実行せずエラーを返す（連絡先を名乗れない自動取得はしない）
- `ETag` / `Last-Modified` が返らないため HTTP キャッシュは使えない。代わりに:
  - `fetch_log` に `source='karauri'`, `key=<symbol>` で最終取得時刻を記録
  - 前回取得で新しい計算日が増えなかった銘柄は、`short_recheck_hours`（既定24h）以内の再取得をスキップ
- 全銘柄更新はユーザーが明示的に実行したときのみ。実行前に**所要時間の見積り（銘柄数 × 間隔）を表示して確認を取る**
- 取得は深夜〜早朝を推奨する旨を画面に注記（公表は取引時間外）
- HTTP 4xx/5xx を受けたらその銘柄を中止し、連続3回失敗したらバッチ全体を中止する

### 2.3 貸借取引残高の取得

#### 2.3.1 取得元

`https://www.taisyaku.jp/download/` 配下の CSV。

- `meigara.csv` — 貸借銘柄一覧。対象銘柄かどうかの判定に使う
- `zandaka.csv` — 銘柄別残高（日次）

> **未確定事項（実装時に確定する）**
> `zandaka.csv` の列構成・文字コード・日付書式は今回の調査で実ファイルまで確認できていない。
> 実装の最初のタスクとして実ファイルを1件取得し、列定義を本書に追記してから実装する（PLAN P2-3）。
> それまで `margin_balances` のカラムは暫定である。

#### 2.3.2 取得方針

- ファイル単位の取得なので、`fetch_log` に `source='taisyaku'`, `key=<日付>` を記録し**未取得日のみ取得**
- 全銘柄分が1ファイルに入るため、**1回の取得で登録銘柄すべてを更新できる**（銘柄ごとのリクエストは発生しない）
- 貸借銘柄でない銘柄は行が存在しない。その旨を画面に表示し、エラーにはしない

#### 2.3.3 規約遵守

- 日証金の規約は「私的利用の範囲を超えて利用することはできず、（中略）第三者の利用に供することを固く禁じます」
- 取得したデータを**エクスポート機能の対象に含めない**（`app/ai_export.py` の出力にも含めない）
- README と設定画面に出典と制限を明記する

### 2.4 開示の取得・分類・保存

#### 2.4.1 取得元

EDINET API v2。キーは**クエリパラメータ `Subscription-Key`**（ヘッダではない）。

書類一覧:

    GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=<KEY>

書類取得:

    GET https://api.edinet-fsa.go.jp/api/v2/documents/<docID>?type=<1-5>&Subscription-Key=<KEY>

| type | 内容 | 形式 |
|---|---|---|
| 1 | 提出本文書及び監査報告書（XBRL含む） | ZIP |
| 2 | PDF | PDF |
| 3 | 代替書面・添付文書 | ZIP |
| 4 | 英文ファイル | ZIP |
| 5 | XBRL→CSV変換済 | ZIP |

#### 2.4.2 銘柄の突き合わせ

- `documents.json` の `secCode` は**5桁**（4桁コード＋末尾0）
- `stocks.code`（4桁）から `f"{code}0"` を作って比較する
- `secCode` が NULL の行（ファンド等）はスキップ

#### 2.4.3 取得範囲と差分

- 初回: 登録銘柄の `stocks.registered_at` の日付以降、または過去1年のいずれか短い方から
- 以後: `fetch_log` の `source='edinet'`, `key=<日付>` に無い日付のみ
- 上限: `date` は当日以前かつ10年以内（API 制約）
- 土日祝はレスポンスが空になるだけなので、特別扱いせず取得して記録する

#### 2.4.4 レート制限

- **1リクエストあたり 1 秒以上空ける**
- 当日分の再取得は1分に1回を上限（当日データは8:30過ぎから原則1分毎更新のため、それ以上は無意味）
- 公式な数値上限は非公開。429 を受けたら指数バックオフ（1s → 2s → 4s、最大3回）し、なお失敗したらバッチを中止

#### 2.4.5 分類と保存方針

| 分類 | docTypeCode | 保存 |
|---|---|---|
| **A: DB正規化** | 120/130（有報）、140/150（四半期）、160/170（半期） | メタデータを `disclosures`、`type=5` の CSV をパースして `disclosure_facts` |
| **B: メタデータ＋文書** | 180/190（臨時報告書）、350/360（大量保有）、220/230（自己株買付）、240〜320（公開買付関連）、030/040（届出書）、235/236（内部統制） | メタデータを `disclosures`。本文は `type=2`(PDF) を要求時のみ取得 |
| **C: 記録のみ** | 上記以外 | メタデータを `disclosures` のみ |

- 分類 A でも `csvFlag != 1` の場合は分類 B と同じ扱いにフォールバックする
- `withdrawalStatus != 0`（取下げ）の書類はイベント欄に「取下げ」として表示し、`disclosure_facts` には取り込まない
- PDF は `disclosure_fetch_pdf` が true のときのみ一括取得。既定は false で、ユーザーが個別に要求したときだけ取得する

#### 2.4.6 EDINET CSV（type=5）のパース

- ZIP 内の `XBRL_TO_CSV` フォルダのファイルが対象
- **拡張子は .csv だが実体はタブ区切り(TSV)、文字コード UTF-16LE、改行 CRLF、各値はダブルクォートで囲まれる**
- 9列: `要素ID / 項目名 / コンテキストID / 相対年度 / 連結・個別 / 期間・時点 / ユニットID / 単位 / 値`
- 空白 = 日本語名未定義、`-` = 値が0またはユニット未設定。**この2つを区別して保存する**（`-` は文字列 `-` のまま、空白は NULL）
- 値は最大30,000文字で切り詰められている場合がある
- 全要素を保存するとレコード数が膨大になるため、**保存対象の要素IDをホワイトリストで絞る**
  - 初期セット: 売上高、営業利益、経常利益、当期純利益、総資産、純資産、自己資本比率、1株当たり当期純利益、営業活動によるキャッシュ・フロー
  - ホワイトリストは `app/disclosures.py` の定数で管理し、変更時は当該銘柄の再パースで反映できるようにする

#### 2.4.7 ローカル保存

    data/disclosures/<symbol>/<docID>/
    ├── meta.json        documents.json の該当行をそのまま保存
    ├── document.pdf     type=2（取得した場合のみ）
    └── xbrl_csv/        type=5 の ZIP を展開したもの（分類 A のみ）

- `disclosures.body_path` にこのディレクトリの相対パスを保存する
- 銘柄削除時は `ON DELETE CASCADE` で DB 行が消える。**ディレクトリも併せて削除する**

### 2.5 チャート表示

#### 2.5.1 サブペイン追加

既存のチップ切り替え（`web/js/chart.js` の `PANES`）に以下を追加する。

| id | ラベル | 内容 |
|---|---|---|
| `short` | 空売り残高 | `short_totals.total_ratio`（%）の推移。副線として `holders`（報告者数） |
| `taisyaku` | 貸借残高（日証金） | 貸付残高・借入残高の2本 |

#### 2.5.2 欠損日の扱い

需給データは日足の全営業日に値があるとは限らない（貸借銘柄でない、報告が無い日など）。

- 値の無い日は **whitespace data**（`{ time: d }` のみ。`value` キーを持たせない）を入れて時間軸の連続性を保つ
- **線形補間をしない。** `lineType: 2`（階段状）で描画し、次の報告まで値が据え置かれることを視覚的に表す
- 値が1件も無い銘柄では、そのペインのチップを無効化し「データなし（貸借銘柄ではない可能性があります）」と表示

#### 2.5.3 イベントマーカー

`LWC.createSeriesMarkers(candleSeries, markers)` でローソク足に付ける。
既存の売買シグナルのマーカーと**同一の配列にマージして1回で設定する**（複数回呼ぶと上書きされるため）。

| 分類 | 対象 docTypeCode | 位置 | 形状 | 色 |
|---|---|---|---|---|
| 決算系 | 120/130, 140/150, 160/170 | aboveBar | circle | 青系 |
| 需給系 | 350/360, 220/230, 240〜320 | belowBar | arrowUp | 橙系 |
| その他 | 180/190, 030/040, 235/236 ほか | aboveBar | square | 灰系 |

- 同一日に複数の開示がある場合は**1つのマーカーにまとめ**、テキストに件数を付す（例: `開示3件`）
- マーカーの `time` は `submitDateTime` の日付部分。**非営業日に提出された開示は直後の営業日に寄せる**（その日のローソク足が存在しないと描画されないため）。寄せた事実はイベント欄に原日付として表示する

### 2.6 イベント欄

チャート直下に開示イベントの一覧を置く。

- **チャートの表示範囲と連動**する。`chart.timeScale().subscribeVisibleTimeRangeChange()` で範囲を取得し、範囲内のイベントだけを表示
- 各行: 日付 / 種別ラベル / 概要（`docDescription`）/ 提出事由（`currentReportReason`、臨時報告書のみ）
- 行をクリック → 該当日にチャートをスクロール（`timeScale().scrollToPosition()` または `setVisibleRange()`）
- 文書を保存済みなら「開く」ボタンで OS 既定アプリに渡す。未取得なら「取得」ボタンで PDF をダウンロード
- **マーカーにはヒットテストAPIが無い**ため、マーカーのクリック検出は `chart.subscribeClick` の `param.time` から自前のイベント配列を引く方式で実装する
- DB 化できない開示（分類 B / C）も**すべてイベント欄に出す**

### 2.7 AI分析レポート

#### 2.7.1 実行

- **手動実行のみ。** 自動実行・定期実行はしない
- 対象は1銘柄。期間（直近20/60/120日）を選択
- 実行前に確認ダイアログで以下を表示する:
  - 使用モデル名
  - `count_tokens` による**入力トークンの見積り**
  - 本日の残量（RPD 残り・TPM 残り）
  - 送信されるデータの種別（§7.2 の一覧）
- 確認後に実行。実行中は進捗を表示

#### 2.7.2 クォータ管理

- `ai_usage` に**太平洋時間（`zoneinfo.ZoneInfo("America/Los_Angeles")`）基準の日付**で積算する
  - 理由: RPD のリセットが太平洋時間の深夜0時。夏時間の切替があるため固定オフセットを使わない
- **送信前ガード**: 見積りトークン数とリトライ想定分を加えた上で、
  `gemini_rpd` / `gemini_tpm` の残量を超える場合は**送信せずに中止**する
  - メッセージ: 「本日の無料枠を使い切りました。太平洋時間0時にリセットされます（日本時間の当日16時または17時）」
- `gemini_rpm` は直近60秒のリクエスト数をメモリ上で数え、超える場合は待機する
- 429 / `RESOURCE_EXHAUSTED`（`google.genai.errors.ClientError`）を受けた場合:
  - レスポンスから RPM 超過か RPD 超過かは**判別できない**
  - 安全側に倒し、**その日はそのモデルへの送信を打ち切る**。`ai_usage` に打ち切りフラグを立てる
- SDK 組み込みの自動リトライ（最大4回・初回約1秒・最大60秒）が動くため、
  アプリ側で二重にリトライしない

#### 2.7.3 送信データ（厳密に定義する）

**送るもの**

| 種別 | 内容 |
|---|---|
| 銘柄情報 | 証券コード、銘柄名、市場、通貨 |
| 株価 | 直近N日の OHLCV（CSVブロック形式） |
| テクニカル指標 | 直近N日の各指標値（CSVブロック形式）、最新の判定（`evaluate_latest`）、期間内のシグナル（`detect_signals`） |
| 開示 | `disclosures` の日付・種別ラベル・`docDescription`・`currentReportReason`。**本文は送らない** |
| 財務数値 | `disclosure_facts` のうちホワイトリスト要素の直近値 |

**送らないもの**

- `short_positions` / `short_totals` / `margin_balances` の**すべて**
- ユーザーの保有株数・取得単価・損益・口座情報（アプリが保持しない）
- APIキー、ローカルパス、ユーザー名

> **実装上の担保**
> プロンプト組み立ては `app/ai/prompt.py` の単一関数に集約する。
> この関数の引数に需給テーブルの値を渡せない型にし、
> 「生成されたプロンプト文字列に需給データが含まれないこと」をテストで固定する（§10）。

#### 2.7.4 構造化出力

Gemini には **HTML を書かせない。** JSON で分析結果だけを返させ、HTML は Jinja2 で組み立てる。

- SDK: `google-genai`（旧 `google-generativeai` は廃止済み）
- `response_mime_type="application/json"` + `response_schema=<Pydanticモデル>`
- `temperature=0.1`
- **Pydantic のフィールド定義順が出力順に反映される**ため、「結論 → 根拠」の順に定義する

スキーマ（`app/ai/schema.py`）:

```python
class SectionAnalysis(BaseModel):
    assessment: str            # その観点での評価
    evidence: list[str]        # 根拠（データ由来の事実のみ）

class AnalysisReport(BaseModel):
    verdict: Literal["bullish", "bearish", "neutral"]
    confidence: int            # 0-100
    summary: str               # 3行以内の総括
    technical: SectionAnalysis
    disclosure: SectionAnalysis
    risks: list[str]
    watch_points: list[str]
```

#### 2.7.5 検証と再依頼

1. `finish_reason` を確認する。`"MAX_TOKENS"` なら**パースを試みずに**期間を縮めて再実行を促す
2. `AnalysisReport.model_validate_json(response.text)` を試みる
3. 失敗したら、**バリデーションエラーの本文をプロンプトに添えて再依頼**する
   - 追加文: 「前回の出力は次の検証エラーで失敗した: {エラー}。スキーマに厳密に従って再生成せよ」
4. 再依頼は**最大2回**。3回目の失敗で中止し、生のレスポンスを `data/logs/` に保存してユーザーに通知する
5. 再依頼もクォータを消費する。§2.7.2 の事前見積りにリトライ2回分を含める

#### 2.7.6 レポート出力

- Jinja2 テンプレート（`templates/report.html.j2`）で**単一HTMLファイル**を生成
- CSS は `<style>` にインライン。外部ファイル参照を作らない
- 含める内容: 銘柄情報、生成日時、モデル名、AI の分析結果（スキーマの各項目）、
  根拠となった指標値の表、開示一覧
- **免責を必ず含める**: 本レポートは機械的な分析であり投資助言ではないこと、
  AI の出力を検証していないこと、データの出典
- **需給データはレポートにも載せない**（AI が分析していないため、載せると分析対象だったと誤認させる）
- 保存先 `data/reports/report_<symbol>_<YYYYmmdd_HHMMSS>.html`、`ai_reports` に記録

---

## 3. データモデル

既存の `stocks` / `prices` / `indicators` は変更しない。以下を追加する。

```sql
-- 設定（APIキー等）
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 空売り残高（報告者別・karauri.net 由来）
CREATE TABLE short_positions (
    symbol      TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
    calc_date   TEXT NOT NULL,
    holder      TEXT NOT NULL,
    ratio       REAL,
    ratio_delta REAL,
    quantity    INTEGER,
    qty_delta   INTEGER,
    note        TEXT,
    PRIMARY KEY (symbol, calc_date, holder)
);
CREATE INDEX idx_short_positions_symbol_date ON short_positions(symbol, calc_date);

-- 空売り残高の銘柄合計（§2.2.3 の手順で算出。全置換で更新）
CREATE TABLE short_totals (
    symbol      TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
    date        TEXT NOT NULL,
    total_ratio REAL,
    total_qty   INTEGER,
    holders     INTEGER,
    PRIMARY KEY (symbol, date)
);

-- 貸借取引残高（日証金 zandaka.csv 由来）
-- ※ カラムは暫定。実ファイル確認後に確定する（PLAN P2-3）
CREATE TABLE margin_balances (
    symbol         TEXT NOT NULL REFERENCES stocks(symbol) ON DELETE CASCADE,
    date           TEXT NOT NULL,
    loan_balance   INTEGER,   -- 貸付（売り）残高
    borrow_balance INTEGER,   -- 借入（買い）残高
    loan_new       INTEGER,   -- 新規貸付
    borrow_new     INTEGER,   -- 新規借入
    ratio          REAL,      -- 倍率
    PRIMARY KEY (symbol, date)
);

-- 開示メタデータ（EDINET）
CREATE TABLE disclosures (
    doc_id         TEXT PRIMARY KEY,
    symbol         TEXT REFERENCES stocks(symbol) ON DELETE CASCADE,
    sec_code       TEXT,
    edinet_code    TEXT,
    filer_name     TEXT,
    doc_type_code  TEXT NOT NULL,
    form_code      TEXT,
    ordinance_code TEXT,
    description    TEXT,
    reason         TEXT,
    period_start   TEXT,
    period_end     TEXT,
    submit_at      TEXT NOT NULL,
    event_date     TEXT NOT NULL,  -- チャート表示用。非営業日は直後の営業日に寄せた日付
    parent_doc_id  TEXT,
    xbrl_flag      INTEGER,
    pdf_flag       INTEGER,
    csv_flag       INTEGER,
    withdrawal     INTEGER,
    disclosure     INTEGER,
    category       TEXT NOT NULL,  -- 'A' | 'B' | 'C'（§2.4.5）
    body_path      TEXT,
    fetched_at     TEXT
);
CREATE INDEX idx_disclosures_symbol_date ON disclosures(symbol, event_date);

-- XBRL 由来の財務数値（分類 A のみ・ホワイトリスト要素のみ）
CREATE TABLE disclosure_facts (
    doc_id      TEXT NOT NULL REFERENCES disclosures(doc_id) ON DELETE CASCADE,
    element_id  TEXT NOT NULL,
    context_id  TEXT NOT NULL,
    label       TEXT,
    rel_period  TEXT,
    scope       TEXT,        -- 連結 / 個別 / その他
    period_type TEXT,        -- 期間 / 時点
    unit_id     TEXT,
    unit        TEXT,
    value       TEXT,        -- 数値も文字列も来るため TEXT
    PRIMARY KEY (doc_id, element_id, context_id)
);

-- 取得済みの記録（差分取得用）
CREATE TABLE fetch_log (
    source     TEXT NOT NULL,   -- 'edinet' | 'taisyaku' | 'karauri'
    key        TEXT NOT NULL,   -- 日付 or 銘柄シンボル
    fetched_at TEXT NOT NULL,
    result     TEXT,            -- 'ok' | 'empty' | 'error:<理由>'
    PRIMARY KEY (source, key)
);

-- Gemini のクォータ管理
CREATE TABLE ai_usage (
    date_pt    TEXT NOT NULL,   -- 太平洋時間基準の日付
    model      TEXT NOT NULL,
    requests   INTEGER NOT NULL DEFAULT 0,
    in_tokens  INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    exhausted  INTEGER NOT NULL DEFAULT 0,  -- 429 を受けて打ち切った
    PRIMARY KEY (date_pt, model)
);

-- 生成したレポート
CREATE TABLE ai_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    model      TEXT NOT NULL,
    path       TEXT NOT NULL,
    in_tokens  INTEGER,
    out_tokens INTEGER
);
```

### 3.1 マイグレーション方針

土台は `indicators` のカラム構成が変わったらテーブルを作り直し、株価から再計算する方式をとっている。
新テーブルは**再取得コストが高い**ため、同じ方式は使えない。

- `CREATE TABLE IF NOT EXISTS` で追加する
- 既存 DB への追加カラムは `PRAGMA table_info` で確認し `ALTER TABLE ADD COLUMN` で足す
- `schema_version` を `settings` に持ち、バージョンごとの移行処理を関数で持つ
- `short_totals` と `disclosure_facts` は元データ（`short_positions` / ローカル保存した XBRL CSV）から
  再生成できるため、構成変更時は再生成でよい

---

## 4. 外部インターフェース

### 4.1 共通 HTTP クライアント（`app/sources/base.py`）

すべての外部取得はこのクライアントを経由する。

- ソースごとに**最小リクエスト間隔**を持ち、直前のリクエストからの経過時間で待機する
- 同時実行を防ぐためソースごとにロックを持つ
- User-Agent を設定する。ソースごとに上書き可
- タイムアウト: 接続10秒 / 読み取り30秒
- リトライ: 429・5xx に対して指数バックオフ（1s → 2s → 4s、最大3回）。4xx（429を除く）は即座に失敗
- **ログにクエリパラメータの APIキーを出力しない**

| ソース | 最小間隔 |
|---|---|
| `karauri` | `scrape_interval_sec`（既定10秒、下限5秒） |
| `taisyaku` | 5秒 |
| `edinet` | 1秒 |

### 4.2 JS ↔ Python API（`app/api.py`）

戻り値は土台の規約どおり `{"ok": bool, "data" | "error": ...}` に統一する。

**既存（変更なし）**: `search` / `register` / `update` / `update_all` / `delete` / `list_stocks` /
`export` / `list_exports` / `open_csv_folder` / `open_output_folder`

**既存（拡張）**

| メソッド | 変更 |
|---|---|
| `dashboard(symbol)` | 戻り値に `chart.short` / `chart.taisyaku` / `events` を追加 |

**新規**

| メソッド | 引数 | 戻り値 |
|---|---|---|
| `get_settings()` | — | 設定値（APIキーはマスク済み） |
| `save_settings(values)` | dict | 保存後の設定値 |
| `test_connection(target)` | `'edinet'｜'gemini'` | 疎通結果 |
| `fetch_short(symbol)` | — | 取得件数・最新計算日 |
| `fetch_short_all()` | — | 銘柄ごとの結果・スキップ理由 |
| `estimate_short_all()` | — | 対象銘柄数と所要時間の見積り（確認ダイアログ用） |
| `fetch_taisyaku()` | — | 取得した日付と反映銘柄数 |
| `fetch_disclosures(symbol=None)` | — | 取得件数・分類別内訳 |
| `get_disclosure(doc_id)` | — | メタデータと `disclosure_facts` |
| `fetch_disclosure_body(doc_id)` | — | 保存先パス |
| `open_disclosure(doc_id)` | — | OS既定アプリで開く |
| `ai_quota()` | — | モデル・本日の使用量・残量・打ち切りフラグ |
| `ai_estimate(symbol, days)` | — | 入力トークン見積り・実行可否・不可の理由 |
| `ai_analyze(symbol, days)` | — | レポートのパスと使用トークン |
| `list_reports()` | — | 生成済みレポート一覧 |
| `open_report(report_id)` | — | OS既定アプリで開く |

長時間かかる処理（`fetch_short_all` / `fetch_disclosures` / `ai_analyze`）は
土台の `update_all` と同じくブロッキングで実装し、画面側で進捗表示とキャンセル不可の旨を示す。

---

## 5. ファイル構成

```
main.py                  起動（変更: 新サービスの組み立て）
dev_server.py            開発用サーバー（変更なし）
app/
├── api.py               JS 公開 API（拡張）
├── service.py           株価・指標（既存）
├── fetcher.py           yfinance（既存）
├── indicators.py        指標計算（既存・変更なし）
├── database.py          SQLite（拡張）
├── csv_export.py        閲覧用CSV（既存）
├── ai_export.py         AI向けデータ整形（流用・拡張）
├── config.py            パス・定数（拡張）
├── settings.py          [新] 設定の読み書き・APIキー管理
├── sources/             [新]
│   ├── __init__.py
│   ├── base.py            間隔制御つきHTTPクライアント
│   ├── karauri.py         空売り残高
│   ├── taisyaku.py        貸借取引残高
│   └── edinet.py          EDINET API v2
├── disclosures.py       [新] 分類・XBRL CSV パース・ローカル保存
├── events.py            [新] disclosures → チャートイベント変換
└── ai/                  [新]
    ├── __init__.py
    ├── client.py          google-genai ラッパ
    ├── quota.py           ai_usage によるクォータ管理
    ├── schema.py          Pydantic 出力スキーマ
    ├── prompt.py          プロンプト組み立て（需給を含めない）
    └── report.py          Jinja2 で単一HTML生成
templates/               [新]
└── report.html.j2
web/
├── index.html           タブ追加（設定・レポート）
├── css/style.css        拡張
├── js/app.js            画面制御（拡張）
├── js/chart.js          ペイン・マーカー・イベント欄（拡張）
├── js/bridge.js         既存
├── js/format.js         既存
└── vendor/
    ├── lightweight-charts.standalone.production.js
    ├── LICENSE-lightweight-charts.txt
    └── NOTICE-lightweight-charts.txt   [新] Apache-2.0 §4(d) 対応
docs/
├── RESEARCH.md
├── DESIGN.md
├── SPEC.md
├── PLAN.md
└── REVIEW_REQUEST.md
tests/                   既存 + 新規
data/                    .gitignore 済み
```

追加依存: `requests`, `beautifulsoup4`, `lxml`, `google-genai`, `pydantic`, `jinja2`

---

## 6. エラー処理

| 状況 | 挙動 |
|---|---|
| APIキー未設定 | 該当機能のボタンを無効化し、設定タブへの導線を出す |
| EDINET 401/403 | 「APIキーが正しくないか失効しています。2年間未使用のキーは自動削除されます」と表示 |
| EDINET 429 | 指数バックオフ3回 → なお失敗ならバッチ中止し、取得済み分は保持 |
| karauri.net でテーブル構造が想定外 | **その銘柄の保存を行わず**エラー。「サイト構造が変わった可能性があります」と表示 |
| karauri.net 連続3回失敗 | バッチ全体を中止 |
| `scrape_contact` 未設定 | スクレイピングを実行せずエラー |
| 日証金 CSV の列が想定外 | 保存せずエラー。列定義の確認を促す |
| Gemini クォータ不足 | **送信せずに**中止。残量とリセット時刻を表示 |
| Gemini 429 | その日そのモデルを打ち切り、`ai_usage.exhausted = 1` |
| Gemini スキーマ不一致 | 最大2回再依頼 → 失敗なら生レスポンスを `data/logs/` に保存して通知 |
| `finish_reason == MAX_TOKENS` | パースせず、期間短縮を促す |
| 貸借銘柄でない | エラーにせず「データなし」と表示 |

ログは土台と同じ `data/logs/app.log`（RotatingFileHandler）。**APIキーは必ずマスクする。**

---

## 7. セキュリティ・コンプライアンス

### 7.1 規約遵守の要件

| 対象 | 要件 |
|---|---|
| EDINET | 利用時は**出典を明記**する。レポートと画面に記載 |
| JPX / 日証金 | 取得データを**再配布・公開しない**。エクスポート機能の対象に含めない |
| karauri.net | robots.txt を尊重（対象外のクローラー名ではあるが、間隔・UA・直列アクセスを遵守） |
| Yahoo (yfinance) | 非公式ライブラリであること、個人の分析用途に限ることを README に明記（土台が記載済み） |
| TradingView Lightweight Charts | Apache-2.0 §4(d) に従い **NOTICE を同梱**。`attributionLogo` を無効化しない |

### 7.2 AI への送信データの境界

§2.7.3 に定義した通り。**需給データは送信しない。**

根拠:
- JPX 利用規約が生成AI による情報の学習・解析・生成利用に制限をかけている
- Gemini 無料枠は入力が学習に使われ、人間のレビュアーが読む可能性がある（日本は EEA 等の優遇対象外）

この2つが重なる領域を避ける。実装上はプロンプト組み立てを1関数に集約し、テストで固定する。

### 7.3 秘密情報

- APIキーは `data/chronos.db` の `settings` テーブルに平文で保存する
  （ローカル専用ツールであり、OS のユーザーアカウントで保護されることを前提とする）
- `data/` は `.gitignore` 済み
- ログ・エクスポート・レポートにキーを含めない
- 画面ではマスク表示

---

## 8. 非機能要件

| 項目 | 要件 |
|---|---|
| 起動時間 | 土台と同等（登録銘柄10件で3秒以内） |
| チャート描画 | 日足3年分（約730本）＋需給2ペイン＋マーカー100件で1秒以内 |
| 空売り残高の全銘柄取得 | 銘柄数 × `scrape_interval_sec` が所要時間。事前に見積りを表示する |
| DB サイズ | 登録20銘柄・3年分で 100MB 以内（`disclosure_facts` をホワイトリストで絞ることで達成） |
| オフライン | 取得済みデータの閲覧はネットワーク無しで動作する |

---

## 9. 未確定事項

実装前または実装中に確定する。確定したら本書を更新する。

| # | 項目 | 確定方法 | 関連タスク |
|---|---|---|---|
| 1 | 日証金 `zandaka.csv` の列構成・文字コード・日付書式 | 実ファイルを1件取得して確認 | PLAN P2-3 |
| 2 | 日証金のファイル名規則（日付入りか固定名か）と過去分の取得可否 | ダウンロードページを確認 | PLAN P2-3 |
| 3 | Gemini 無料枠のモデル別 RPM/TPM/RPD | ユーザーが AI Studio で確認して設定画面に入力 | 設計で吸収済み |
| 4 | `response.usage_metadata` の正確なフィールド名 | 実レスポンスを確認 | PLAN P6-1 |
| 5 | EDINET 利用規約の正確な文言 | 原文ページで再確認 | PLAN P4-1 |
| 6 | JPX 銘柄別信用取引週末残高に CSV/Excel 版が無いか | 再確認（あれば貸借残高より優れた選択肢） | PLAN P2-3 |
| 7 | `disclosure_facts` ホワイトリストの最終的な要素ID | 実際の XBRL CSV を見て確定 | PLAN P4-4 |

---

## 10. テスト方針

土台の方式（pytest、実通信は環境変数でゲート）を踏襲する。

| テスト | 内容 |
|---|---|
| `test_settings.py` | 設定の読み書き、マスク、環境変数の優先、ログにキーが出ないこと |
| `test_sources_base.py` | リクエスト間隔が守られること、リトライ、UA、キーがログに出ないこと |
| `test_karauri.py` | 保存した HTML フィクスチャのパース、列数違いでエラー、数値・NULL の扱い |
| `test_short_totals.py` | §2.2.3 の合計算出（報告義務消失の除外、報告者ごとの最新採用） |
| `test_taisyaku.py` | CSV フィクスチャのパース、貸借銘柄でない場合 |
| `test_edinet.py` | `documents.json` フィクスチャの分類、`secCode` の5桁突合、取下げ書類の扱い |
| `test_xbrl_csv.py` | UTF-16LE・TSV のパース、空白と `-` の区別、ホワイトリスト絞り込み |
| `test_events.py` | 非営業日の繰り越し、同日複数開示のまとめ |
| `test_ai_prompt.py` | **生成プロンプトに需給データが含まれないこと**（§7.2 の担保） |
| `test_ai_quota.py` | 太平洋時間の日付境界、夏時間切替、送信前ガード、429 での打ち切り |
| `test_ai_schema.py` | 不正JSONでの再依頼、`MAX_TOKENS` の検知、再依頼上限 |
| `test_report.py` | HTML が単一ファイルで完結すること、免責が含まれること、需給が含まれないこと |
| `test_migration.py` | 既存 DB からのマイグレーション、再実行の冪等性 |
| `test_live_*.py` | 実通信。`CHRONOS_LIVE=1` のときだけ実行 |

外部サイトの HTML / CSV / JSON はフィクスチャとして `tests/fixtures/` に保存し、
ネットワーク無しでテストが完走することを要件とする。
