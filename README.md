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
  - メイン: SMA(5/25/75)・EMA(12/26)・ボリンジャーバンド・一目均衡表（雲の先行表示あり）・売買シグナル
  - サブ: 出来高・MACD・RSI・ストキャスティクス・DMI/ADX・ATR・乖離率・サイコロジカルライン
  - 表示する指標はチップで切り替え（設定は保存されます）
- 直近のシグナル一覧（ゴールデン/デッドクロス、MACD クロス、RSI 30/70）
- 直近60日の日次データ表

### 計算する指標（日ごとに DB と CSV に保存）

| 分類 | 指標 |
| --- | --- |
| トレンド | SMA(5, 25, 75)、EMA(12, 26)、乖離率(25)、MACD(12, 26, 9)、DMI / ADX(14)、一目均衡表(9, 26, 52) |
| オシレーター | RSI(14, Wilder)、ストキャスティクス(14, 3, 3)、サイコロジカルライン(12) |
| ボラティリティ | ボリンジャーバンド(20, ±1σ/±2σ, %B, バンド幅)、ATR(14) |
| 出来高 | 出来高移動平均(5, 25) |

> 指標の計算には一定期間のデータが必要なため、取得期間の先頭付近は空欄になります
> （例: SMA(75) は75日目から、一目の先行スパンB は77日目から）。

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

```bash
python main.py
```

開発者ツールを有効にする場合は `python main.py --debug`。

## データの保存先

```
data/
├── autotechnical.db              # SQLite（stocks / prices / indicators テーブル）
├── csv/
│   ├── 7203.T_prices.csv         # 日付・始値・高値・安値・終値・出来高
│   └── 7203.T_indicators.csv     # 日ごとのテクニカル指標
└── logs/app.log
```

- CSV は閲覧用です（Excel で開けるよう UTF-8 BOM 付き）。正となるデータは SQLite 側です。
- CSV を Excel で開いたまま更新すると CSV の書き込みだけスキップされ、画面に警告が出ます。
- 保存先は環境変数 `AUTOTECHNICAL_DATA_DIR` で変更できます。

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
├── csv_export.py    CSV 出力
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
