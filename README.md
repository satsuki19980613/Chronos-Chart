# Autotechnical

Yahoo! Finance から個別銘柄の株価（始値・高値・安値・終値・出来高）を取得し、
日ごとのテクニカル指標を計算・保存・チャート表示するローカル GUI ツールです。

- **GUI**: Python + [pywebview](https://pywebview.flowrl.com/)（画面は HTML / CSS / JavaScript）
- **データ取得**: [yfinance](https://github.com/ranaroussi/yfinance)
- **データベース**: SQLite
- **チャート**: [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)（同梱）

## 機能

### 登録画面
- 銘柄コード（例: `7203`）・ティッカー（例: `AAPL`）・英語の社名で検索
  - 全角数字でも入力可。4桁の国内コードは東証銘柄（`7203.T`）を先頭に表示
- 検索結果から選択して登録 → **初回は過去1年分の日足を取得**
- 登録済み銘柄の一覧・個別更新・全銘柄更新・削除
  - 更新は最終取得日以降の差分のみ取得（株式分割を検出した場合は全期間を取り直し）

### ダッシュボード画面
- 最新の株価・前日比
- 主要指標の最新値と判定（強気 / 弱気 / 中立）
- ローソク足チャート
  - メイン: 移動平均・指数平滑移動平均・ボリンジャーバンド・一目均衡表（雲の先行表示あり）・多重移動平均・パラボリック・売買シグナル
  - サブ: 出来高・MACD・RSI・RCI・DMI/ADX・ストキャスティクス・移動平均乖離率・サイコロジカルライン・標準偏差・モメンタム
  - 表示する指標はチップで切り替え（設定は保存されます）
- 直近のシグナル一覧（ゴールデン/デッドクロス、MACD クロス、RSI 30/70）
- 直近60日の日次データ表

### 出力画面（AI 向けファイル）
- 登録済み銘柄を複数選択し、AI（LLM）に読ませるためのファイルを `output/` に出力
- 形式
  - **テキスト（Markdown）**: 前提条件（データの出典・価格の調整方法・空欄の意味）、指標の定義、銘柄ごとの最新判定（`key`/`status` は英語）、期間内のシグナル、日次データ（CSV ブロック）
  - **CSV**: 全銘柄を 1 つの表に（`symbol` 列で区別）。英語 snake_case で期間入りの列名（`sma_25`, `rsi_14` など）、ISO 日付、古い順、桁区切りなし、UTF-8（BOM なし）
- 期間: 直近20日 / 60日 / 120日 / 全期間
- ファイル名: `technical_<日時>_<銘柄>.md` / `.csv`

### 計算する指標（日ごとに DB と CSV に保存）

| 指標 | 項目 | パラメータ |
| --- | --- | --- |
| 移動平均 | 短期 / 中期 / 長期 | 5 / 25 / 75 |
| ボリンジャーバンド | +2σ / 移動平均 / -2σ | 20日、母標準偏差 |
| MACD | MACD / シグナル | 12, 26 / 9 |
| RSI | 中期 | 14（Wilder） |
| 一目均衡表 | 基準線 / 転換線 / 先行線1 / 先行線2 / 遅行線 | 26 / 9 / (基準+転換)÷2 / 52 / 26日ずらし |
| 指数平滑移動平均 | 短期 / 中期 / 長期 | 5 / 25 / 75 |
| RCI | 短期 / 長期 | 9 / 26 |
| DMI/ADX | +DI / -DI / ADX | 14（Wilder） |
| 多重移動平均（GMMA） | 最短群 / 最長群 | EMA 3,5,8,10,12,15 / EMA 30,35,40,45,50,60 |
| パラボリック | SAR | 加速因子 0.02、上限 0.2 |
| ストキャスティクス | %K / %D | 9 / 3 |
| 移動平均乖離率 | 短期 / 長期 | 25 / 75 |
| サイコロジカルライン | — | 12 |
| 標準偏差 | — | 20 |
| モメンタム | モメンタム / シグナル | 10（当日終値−10日前終値） / 9日単純移動平均 |

パラメータは [app/indicators.py](app/indicators.py) の `PARAMS` でまとめて変更できます。
指標の構成を変えた場合、次回起動時に保存済みの株価から全銘柄の指標と CSV を自動で再計算します。

> 指標の計算には一定期間のデータが必要なため、取得期間の先頭付近は空欄になります
> （例: 移動平均 長期は75日目から、一目の先行線2 は77日目から）。

## セットアップ

Python 3.10 以上が必要です。

```bash
git clone https://github.com/<your-account>/Autotechnical.git
cd Autotechnical
python -m venv .venv
.venv\Scripts\activate        # macOS / Linux は source .venv/bin/activate
pip install -r requirements.txt
```

## 起動

Windows では **`start.bat` をダブルクリック**するだけで起動できます。

- `.venv` フォルダがあればその Python を、なければ PATH 上の Python を使います
- 必要なライブラリが入っていなければ、初回に `requirements.txt` から自動でインストールします
- コンソールを出さずにアプリだけが起動します（エラーは `data/logs/app.log` に記録）
- コマンドプロンプトから `start.bat --debug` で、コンソールと開発者ツール付きで起動します

コマンドから直接起動する場合:

```bash
python main.py
```

開発者ツールを有効にする場合は `python main.py --debug`。

## データの保存先

```
data/
├── autotechnical.db                  # SQLite（stocks / prices / indicators テーブル）
├── csv/
│   ├── 7203.T_株価.csv               # 日付・始値・高値・安値・終値・出来高
│   └── 7203.T_テクニカル指標.csv     # 日付・終値・日ごとのテクニカル指標
└── logs/app.log
output/                               # 出力タブで作成した AI 向けファイル
```

- `data/csv/` は人が Excel で見るための CSV です（UTF-8 BOM 付き、新しい日付が上、日付は yyyy/mm/dd、
  株価単位の指標は株価と同じ桁・その他は小数2桁）。正となるデータは SQLite 側です。
  - 一目均衡表の遅行線は「25営業日後の終値」をその日の行に置くため、直近25行は空欄になります。
- CSV を Excel で開いたまま更新すると CSV の書き込みだけスキップされ、画面に警告が出ます。
- 保存先は環境変数 `AUTOTECHNICAL_DATA_DIR`（データ）/ `AUTOTECHNICAL_OUTPUT_DIR`（出力）で変更できます。

## 構成

```
main.py              アプリ起動（pywebview ウィンドウ）
dev_server.py        開発用: ブラウザで画面を確認する簡易サーバー
app/
├── api.py           JavaScript に公開する API
├── service.py       検索・登録・更新・ダッシュボード用データ組み立て
├── fetcher.py       yfinance による検索・株価取得
├── indicators.py    テクニカル指標の計算・シグナル判定
├── database.py      SQLite 操作
├── csv_export.py    閲覧用 CSV 出力
├── ai_export.py     AI 向け CSV / Markdown 出力
└── config.py        パス・定数
web/
├── index.html
├── css/style.css
├── js/              app.js（画面制御）/ chart.js（チャート）/ bridge.js（API 呼び出し）
└── vendor/          Lightweight Charts
tests/               pytest
```

## 開発

```bash
pip install -r requirements-dev.txt
python -m pytest
```

| テスト | 内容 |
| --- | --- |
| `test_indicators.py` / `test_indicators_reference.py` | 指標の計算（ループで書いた独立実装との突き合わせ・境界値） |
| `test_service.py` / `test_api.py` | 登録・更新・分割・削除・出力、JS 向け API の正常系/異常系/並行実行 |
| `test_export.py` | AI 向け CSV/Markdown と閲覧用 CSV の形式 |
| `test_dev_server.py` | 開発用サーバーの HTTP 応答・不正リクエスト・パストラバーサル |
| `test_live_yahoo.py` | 実際の Yahoo! Finance との通信（`AUTOTECHNICAL_LIVE=1` のときだけ実行） |

```bash
AUTOTECHNICAL_LIVE=1 python -m pytest tests/test_live_yahoo.py
```

ブラウザで画面を確認したいときは `python dev_server.py` を起動し、`http://127.0.0.1:8765/?dev` を開きます。

## 注意事項

- yfinance は Yahoo! Finance の非公式ライブラリです。Yahoo! の利用規約に従い、個人の分析用途でご利用ください。
  取得データの正確性・継続的な提供は保証されません。
- 株価は分割調整済み・配当未調整の値です。
- 一目均衡表の将来の雲の日付は土日のみを除いた営業日で近似しています（祝日は考慮しません）。
- 本ツールの判定・シグナルは機械的な計算結果であり、投資助言ではありません。

## ライセンス

MIT License。同梱の TradingView Lightweight Charts は Apache License 2.0 です
（`web/vendor/LICENSE-lightweight-charts.txt`）。
