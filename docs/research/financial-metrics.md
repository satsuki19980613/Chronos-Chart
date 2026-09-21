# 財務指標の要約セット設計のための調査ノート

Chronos Chart で「生の財務諸表を丸ごと AI に渡す」代わりに使う、少数の要約指標を選定するための調査。
投資助言（「買うべき」等）は書かず、各指標が何を測っているかのみを記載する。出典のない主張はしていない。
通説・俗説は「通説」と明記する。

---

## 1. カテゴリ別・標準的な財務指標

### 1-1. 収益性（Profitability）

| 指標名 | 定義式 | 必要な科目 | 何を測るか | 日本株での注意点 |
|---|---|---|---|---|
| ROE（自己資本利益率） | 当期純利益 ÷ 自己資本（期首期末平均が望ましい） | 親会社株主に帰属する当期純利益、自己資本（純資産－非支配株主持分） | 株主資本1単位あたりの収益力。デュポン分解＝売上高純利益率×総資産回転率×財務レバレッジ | 日本の有報「主要な経営指標等の推移」の自己資本利益率は会社側が算式を開示する。分子は「親会社株主に帰属する当期純利益」を使うのが標準（非支配株主分を含む当期純利益ではない） [出典: cpa-noborikawa.net の親会社株主帰属利益の解説, https://cpa-noborikawa.net/oyakabu-rieki-toha/] |
| ROA（総資産利益率） | 当期純利益（または経常利益）÷ 総資産（期中平均） | 総資産、純利益または経常利益 | 総資産1単位あたりの収益力。財務レバレッジの影響を受けない | 分子を「経常利益」にするか「当期純利益」にするかで数値が変わる。日本の実務では「総資産経常利益率」がよく使われる（EDINET DB の主要指標にも ordinary_income ベースの表記がある）[出典: edinetdb.jp/docs/metrics] |
| ROIC（投下資本利益率） | NOPAT ÷ 投下資本（＝有利子負債＋自己資本、または純有形固定資産＋正味運転資本） | EBIT、実効税率、有利子負債、自己資本（または固定資産・運転資本） | 事業に投じた資本（負債＋自己資本）に対する税引後営業収益力。WACC と比較してこそ意味を持つ | NOPAT＝EBIT×(1－実効税率)。投下資本の定義は複数流派があり、算出方法を明示しないと数値がぶれる [出典: WallStreetPrep ROIC解説, https://www.wallstreetprep.com/knowledge/roic-return-on-invested-capital/] |
| 営業利益率 | 営業利益 ÷ 売上高 | 営業利益、売上高 | 本業の収益力（金融損益・特別損益を除く） | 日本基準ではのれん償却費が営業利益を圧迫するため、M&A を多用する企業は IFRS 採用企業と単純比較できない（後述）[出典: taxjudge.com のれん比較, https://taxjudge.com/2026/07/25/noren-ifrs-jgaap/] |
| 売上高成長率（YoY） | (当期売上高－前期売上高) ÷ 前期売上高 | 売上高（当期・前期） | トップラインの伸び | 単年の値は季節要因や一時的要因でぶれるため複数期の傾向と併用すべき |
| EPS成長率 | (当期EPS－前期EPS) ÷ 前期EPS | 親会社株主に帰属する当期純利益、期中平均発行済株式数（自己株式控除後） | 1株あたり利益の伸び。自社株買いの影響を受ける | 発行済株式数は自己株式を含めない。期末値の簡便計算と期中平均のどちらを使うか要統一 [出典: 松井証券 PER解説, https://www.matsui.co.jp/stock/study/article/per/] |

### 1-2. 安全性（Solvency / Liquidity）

| 指標名 | 定義式 | 必要な科目 | 何を測るか | 日本株での注意点 |
|---|---|---|---|---|
| 自己資本比率 | 自己資本 ÷ 総資産 | 純資産、非支配株主持分、総資産 | 財務基盤の安定性 | 「自己資本」は純資産から新株予約権・非支配株主持分を除いたもの。有報の主要指標でも定義開示あり |
| D/E レシオ | 有利子負債 ÷ 自己資本 | 有利子負債（短期・長期借入金、社債等）、自己資本 | 財務レバレッジの水準 | 「有利子負債」の範囲（リース債務を含むか等）を統一しないと通期比較がぶれる |
| 流動比率 | 流動資産 ÷ 流動負債 | 流動資産、流動負債 | 短期支払能力 | 業種により在庫の流動性が異なるため単独で判断しにくい（小売と装置産業で意味が違う） |
| インタレスト・カバレッジ・レシオ | EBIT ÷ 支払利息 | 営業利益（またはEBIT）、支払利息 | 利払い能力の余裕度。高いほど倒産リスクが低いとされる | EBIT の算出（営業利益＋受取利息配当金を含めるか等）に流派あり。日本では支払利息が「営業外費用」に計上される [出典: WallStreetPrep ICR解説, https://www.wallstreetprep.com/knowledge/interest-coverage-ratio/] |

### 1-3. 効率性（Efficiency）

| 指標名 | 定義式 | 必要な科目 | 何を測るか | 日本株での注意点 |
|---|---|---|---|---|
| 総資産回転率 | 売上高 ÷ 総資産（期中平均） | 売上高、総資産 | 資産をどれだけ効率的に売上に変換しているか | 装置産業とサービス業で水準が大きく異なるため業種内比較が前提 |
| 棚卸資産回転率 | 売上原価 ÷ 棚卸資産（期中平均） | 売上原価、棚卸資産 | 在庫の効率性 | 決算期末の一時的な積み増し・取り崩しに影響される |
| 売上債権回転日数 | 売上債権 ÷ 売上高 × 365 | 受取手形及び売掛金、売上高 | 回収サイトの長さ。Beneish M-Score の DSRI とも関連 | |

### 1-4. キャッシュフロー（Cash Flow）

| 指標名 | 定義式 | 必要な科目 | 何を測るか | 日本株での注意点 |
|---|---|---|---|---|
| 営業CFマージン | 営業活動によるキャッシュフロー ÷ 売上高 | 営業活動によるキャッシュフロー、売上高 | 利益がどれだけ現金を伴っているか | 会計発生高（アクルーアル）が大きい期は営業利益と乖離する→利益の質の手掛かり |
| FCF（フリーキャッシュフロー） | 営業CF－設備投資額（有形・無形固定資産の取得による支出） | 営業活動によるキャッシュフロー、投資活動によるキャッシュフロー内の設備投資 | 事業が生み出し株主・債権者に還元しうる現金 | 「投資CF全体」を引くか「設備投資額のみ」を引くかで定義が割れる。M&A 支出を含めると単年でぶれる |
| アクルーアル（会計発生高） | (当期純利益－営業CF) ÷ 総資産（Sloan 1996 の定義の一種） | 当期純利益、営業活動によるキャッシュフロー、総資産 | 利益のうち現金の裏付けがない部分の大きさ＝利益の質。低いほど質が高いとされる（詳細は§2参照） | |

---

## 2. 合成スコア（複合指標）

### 2-1. Piotroski F-Score（9項目、0〜9点）

- 出典（原論文）: Piotroski, J.D. (2000) "Value Investing: The Use of Historical Financial Statement Information to Separate Winners from Losers," Journal of Accounting Research.
  [解説: https://en.wikipedia.org/wiki/Piotroski_F-score, https://stablebread.com/piotroski-f-score/]
- 構成: 収益性4項目（ROA黒字、ROA黒字、営業CF黒字、営業CF＞純利益＝アクルーアル良好）＋レバレッジ・流動性3項目（長期負債比率低下、流動比率上昇、増資なし）＋効率性2項目（売上総利益率改善、総資産回転率改善）。各項目1点、満たせば加点。
- 必要科目: 当期純利益、総資産（前期比）、営業CF、長期有利子負債、流動資産・流動負債、発行済株式数、売上総利益率、売上高。**いずれも日本基準の連結財務諸表・有報の主要経営指標から算出可能。**
- 日本株への適用: Noma (2010) による日本市場での検証で年率+17.6%の結果が報告されている、との言及あり [出典: https://www.quant-investing.com/blog/piotroski-f-score-improves-global-stock-performance]。ただし当調査で原論文自体は未確認（二次情報）。
- 限界: 単年のトレンド判定（前期比）のみで、業種横断の絶対水準比較はしない。小型株・バリュー株を想定した設計であり、成長株や金融業には設計思想が合わない可能性がある（通説）。

### 2-2. Altman Z-Score

- 出典（原論文）: Altman, E.I. (1968) が製造業上場企業向けに開発。のちに非上場企業向け Z'、非製造業・新興国向け Z" が追加。
  [解説: https://en.wikipedia.org/wiki/Altman_Z-score, https://www.creditguru.com/index.php/bankruptcy-and-insolvency/altman-z-score-insolvency-predictor-for-non-manufacturers-emerging-markets]
- 新興国・非製造業版（Z"）: Z" = 3.25 + 6.56×X1 + 3.26×X2 + 6.72×X3 + 1.05×X4
  - X1 = 運転資本／総資産、X2 = 利益剰余金／総資産、X3 = EBIT／総資産、X4 = 自己資本（簿価）／総負債
- 必要科目: 流動資産・流動負債（運転資本）、利益剰余金、EBIT、総資産、純資産、総負債。**すべて日本の連結貸借対照表・損益計算書から算出可能。**
- 日本株への適用: 検索した範囲では「新興国向けモデルは特定国向けに再構築されたものではなく汎用モデル」との記述があり、日本（先進国市場）への直接適用の妥当性は文献上明確ではない [出典: creditguru.com]。**原論文の推定対象は主に米国製造業であり、日本基準そのままでの再検証は当調査では確認できなかった**（要追加調査、通説の域を出ない）。
- 限界: 倒産予測モデルであり、収益性やクオリティを測るものではない。係数は米国データで推定されたものであり国・時代を越えた汎化性には議論がある。

### 2-3. Beneish M-Score（8変数）

- 出典（原論文）: Beneish, M.D. (1999) "The Detection of Earnings Manipulation," Financial Analysts Journal。
  [解説: https://en.wikipedia.org/wiki/Beneish_M-score, https://stablebread.com/beneish-m-score/]
- 式: M = −4.84 + 0.92×DSRI + 0.528×GMI + 0.404×AQI + 0.892×SGI + 0.115×DEPI − 0.172×SGAI + 4.679×TATA − 0.327×LVGI
- 8変数: DSRI（売上債権回転日数の変化）、GMI（売上総利益率悪化）、AQI（資産の質＝非流動資産のうちPP&E・投資以外の比率変化）、SGI（売上高成長率）、DEPI（減価償却率の変化）、SGAI（販管費比率の変化）、TATA（総資産に対する会計発生高）、LVGI（レバレッジ変化）。
- 必要科目: 売上債権、売上高、売上原価、有形固定資産、減価償却費、販管費、総資産、当期純利益、営業CF、有利子負債。**いずれも日本の連結財務諸表から算出可能**（ただし2期分の詳細な内訳が必要で、EDINET の「主要な経営指標等の推移」だけでは不足し、貸借対照表・損益計算書本体とキャッシュフロー計算書の科目が必要）。
- 限界: 米国の粉飾決算事例（既に発覚した企業）で推定された係数であり、他国・他年代への一般化には注意が必要（通説として指摘されることが多い）。閾値（−2.22 や −1.78）も米国基準のデータに基づく。

### 2-4. Mohanram G-Score（8項目）

- 出典（原論文）: Mohanram, P.S. (2005) "Separating Winners from Losers among Low Book-to-Market Stocks using Financial Statement Analysis," Review of Accounting Studies。
  [解説: https://www.stockopedia.com/content/glamour-stocks-avoiding-falling-stars-using-mohanramrsquos-g-score-57623/, https://stablebread.com/mohanram-g-score/]
- 構成: 収益性・CF（ROA、営業CF＞純利益、CF対資産の水準）、利益・売上の安定性（変動性が小さいこと）、会計保守性（R&D比率、設備投資比率、広告費比率の傾向）の3系統・8シグナルの合計。
- 必要科目: 当期純利益、総資産、営業CF、売上高（複数期の変動性算出に5期分程度必要）、研究開発費、設備投資額、広告宣伝費。
- 日本株への適用の論点: **広告宣伝費・研究開発費は日本の連結財務諸表で開示水準にばらつきがある**（研究開発費は多くの製造業で注記開示されるが、広告宣伝費は販管費の内訳としては開示されない企業も多い）。この論点は今回のWeb調査では直接の文献確認はできておらず、EDINET開示の実態確認が必要（要追加調査）。
- 限界: 低PBR（バリュー）株を対象に設計されたスコアで、Piotroski F-Score と対をなす。グロース株のクオリティ選別が目的であり、汎用の財務健全性指標ではない。

### 2-5. Greenblatt Magic Formula

- 出典: Greenblatt, J. (2005)『The Little Book That Beats the Market』。
  [解説: https://www.wallstreetprep.com/knowledge/roic-return-on-invested-capital/ （ROIC定義の参考）, https://www.gurufocus.com/tutorial/article/57/greenblatts-earnings-yield-and-return-on-capital]
- 式: earnings yield = EBIT ÷ EV（企業価値）、return on capital = EBIT ÷（純固定資産＋正味運転資本）。両ランキングの合成順位で選別。
- 必要科目: EBIT、時価総額、有利子負債、現金同等物（EV算出）、有形固定資産、運転資本。
- 限界: 時価総額・有利子負債という市場データが必須で、財務諸表だけでは完結しない（バリュエーション指標との合成型）。業種（特に金融業）には設計上なじまないとされる（通説）。

### 2-6. Sloan のアクルーアル・アノマリー

- 出典（原論文）: Sloan, R.G. (1996) "Do Stock Prices Fully Reflect Information in Accruals and Cash Flows about Future Earnings?" The Accounting Review。
  [解説: https://quantpedia.com/strategies/accrual-anomaly, https://www.stockopedia.com/content/the-accrual-anomaly-why-investors-should-care-about-accruals-earnings-quality-63003/]
- 考え方: 利益＝会計発生高（アクルーアル）＋営業CF。アクルーアル部分は持続性が低く、投資家がその違いを十分織り込まないため、アクルーアルが小さい（＝利益が現金で裏付けられている）銘柄の方が翌期以降のリターンが高い傾向がある、という実証結果（通説として広く引用される）。
- 2つの算出アプローチ:
  - キャッシュフロー・アプローチ: (当期純利益－営業CF－投資CF) ÷ 総資産（平均）
  - バランスシート・アプローチ（Richardson, Sloan, Soliman, Tuna 2005）: (当期末純営業資産－前期末純営業資産) ÷ 純営業資産平均。純営業資産＝(総資産－現金)－(総負債－有利子負債)
  [出典: https://www.quant-investing.com/glossary/accrual-ratio-balance-sheet]
- 必要科目: 当期純利益、営業CF、投資CF（またはBS方式なら総資産・現金・総負債・有利子負債）。
- 限界: 「アノマリー」という名称通り、効率的市場仮説に対する例外事象としての実証研究であり、個別銘柄の質を機械的に測る保証はない。

---

## 3. バリュエーション指標の実務上の注意

| 指標 | 定義式 | 実務上の注意点 |
|---|---|---|
| PER | 株価 ÷ EPS | 実績PER（直近確定期のEPS）と予想PER（会社予想または市場コンセンサスEPS）を混同しない。日本の実務では予想PERが多用される [出典: https://www.matsui.co.jp/stock/study/article/per/] |
| PBR | 株価 ÷ BPS（自己資本 ÷ 発行済株式数） | 発行済株式数には自己株式を含めない。自社株買い・株式併合で分母が変わり、実態の資産価値が変わらなくてもPBRが動く [出典: 同上] |
| EV/EBITDA | (時価総額＋有利子負債－現金同等物) ÷ EBITDA | 有利子負債・現金の対象範囲（リース債務、持分法適用会社の扱い等）で数値がぶれる。連結ベースが原則 |
| 配当利回り | 1株配当 ÷ 株価 | 実績配当と予想配当（会社予想）の別、期中増配・減配の反映タイミングに注意 |
| PSR | 時価総額 ÷ 売上高 | 赤字企業でも算出できるため、利益が出ていない成長企業の相対評価に使われる（通説） |
| PEG | PER ÷ EPS成長率(%) | 成長率の算出期間（1年 vs CAGR）や、予想成長率のソースにより値が大きく変わる |

共通の論点:
- **期ずれ**: 決算期末と株価取得日のタイムラグ。特に会社予想は期初に出され、期末に近づくほど実績に近づく。
- **実績 vs 予想**: 財務諸表は確定値、株価は先読みなので、予想値との組み合わせが実務では主流。
- **連結 vs 単体**: 上場企業の評価は原則連結ベース。単体決算のみの開示科目（一部の中小型株）では連結指標が算出できない場合がある。
- **自己株式の扱い**: 発行済株式数から自己株式を控除した「期末自己株式控除後の発行済株式数」を使うのが基本 [出典: https://www.matsui.co.jp/money-satellite/column/beginner/stock/cl-pbr.html]。

---

## 4. 日本基準特有の論点

1. **経常利益は日本独自の区分**。営業利益に営業外収益・費用を加減したもので、IFRS にはこの段階損益がない（IFRSは事業活動・財務活動という区分）[出典: https://www.obc.co.jp/360/list/post415, https://globis.jp/article/8011/]。日本基準企業を横並びで見る場合は経常利益が使えるが、IFRS採用企業と比較する際は営業利益または税引前利益で揃える必要がある。
2. **特別損益**: 固定資産売却損益、減損損失、災害損失など臨時項目。日本基準では負ののれん発生益も特別利益に計上されるが、IFRSでは営業利益に含まれる（IFRSに特別損益の区分自体がないため）[出典: 上記IFRS比較記事]。
3. **親会社株主に帰属する当期純利益**: 連結子会社に非支配株主がいる場合、当期純利益は「親会社株主に帰属する部分」と「非支配株主に帰属する部分」に按分される。EPS・ROEの分子には前者を使うのが標準 [出典: https://cpa-noborikawa.net/oyakabu-rieki-toha/, https://biz.moneyforward.com/accounting/basic/53857/]。
4. **のれんの償却差異**: 日本基準はのれんを最長20年で規則的に償却するのに対し、IFRS・米国基準は非償却で年1回の減損テストのみ。そのため、大型M&Aを行う企業は日本基準だと営業利益・経常利益が圧迫され、IFRS採用企業と比べて見かけ上の利益率が低く出る傾向がある [出典: https://www.pwc.com/jp/ja/knowledge/column/goodwill-amortization-and-impairment.html, https://taxjudge.com/2026/07/25/noren-ifrs-jgaap/]。横並び比較をする場合は「のれん償却前営業利益」等の調整、または比較対象を同一基準の企業に絞る対応が考えられる。
5. **無形資産の認識範囲の違い**: 日本基準は「分離して譲渡可能」という要件、IFRS・米国基準は「契約・法律上の権利」または「分離可能性」のいずれかで足りるとする、より広い認識基準 [出典: 同上東海大学紀要]。M&A後の無形資産計上額が基準間で変わりうる。
6. 混在市場での横並び比較の実務対応（通説）: (a) 営業利益・売上高・売上総利益など基準差の影響が相対的に小さい段階の指標を優先する、(b) のれん償却の有無を注記で把握し必要なら調整する、(c) 会計基準（日本基準／IFRS／米国基準）をメタデータとして保持し、閾値判定を基準別に分けるかフラグを立てる。

---

## 5. 時系列（5期分）の圧縮方法

複数の情報源が共通して挙げる手法（通説を含む）:

| 手法 | 説明 | 用途 |
|---|---|---|
| YoY（前年同期比） | 直近期と前期の変化率 | 直近のモメンタムを見る。単年のノイズに弱い |
| CAGR（年平均成長率） | (終値/始値)^(1/年数)－1 | 開始点と終了点だけを使うため、途中の変動を無視して滑らかな平均成長を示す。ボラティリティが高い系列では実態を過小・過大評価しうる [出典: https://www.wallstreetprep.com/knowledge/cagr-compound-annual-growth-rate/] |
| トレンドの傾き（回帰） | log(値) を時間に回帰し、傾きを連続成長率として使う | 全期間のデータ点を使うため、外れ値1点に引っ張られにくい。CAGRと併用が推奨されるとの言及あり [出典: 検索結果の解説記事] |
| 変動係数（CV） | 標準偏差 ÷ 平均 | 収益のばらつき（安定性）を無次元で比較する。Mohanram G-Score の「利益の安定性」判定にも同種の考え方が使われる |
| 直近との乖離 | 直近値 － 過去平均（またはトレンド予測値） | 直近が過去の水準からどれだけ乖離しているか＝方向転換の兆候を見る |

「アナリストが1画面で見る」形としては、上記のうち (a) 直近実績値、(b) YoY、(c) 複数期CAGRまたはトレンド傾き、(d) 変動係数、を1指標につき4つ程度の数値に圧縮する構成が典型的（この4点セットの組み合わせ自体は本調査で見た個別記事の提案であり、業界標準として確立した単一の型があるわけではない点に留意）。

---

## 6. 業種比較・相対評価（外部データなしでの工夫）

- 伝統的な手法は業種平均・中央値との比較だが、これは大規模な同業他社データベース（RMA Annual Statement Studies 等）を前提とする [出典: https://www.prosightfa.org/insights/industry-financial-ratio-benchmarking-for-small-businesses-101-tips-for-business-owners-and-bankers/]。
- 中小規模データセットでは「業種内でもばらつきが大きく、平均より四分位を使うべき」との指摘がある（通説的な実務知見）[出典: https://www.researchgate.net/publication/345007710_FINANCIAL_RATIOS_BENCHMARKS_-_AVERAGE_OF_INDUSTRY_OR_SOME_OTHER_MEASURE]。
- **自社が数銘柄しか保持しないローカルツールで意味のある評価をする代替案**（本調査での整理。特定文献に基づく確立手法ではなく、一般的な統計的発想の応用）:
  1. **自己ヒストリカル比較**: その銘柄自身の過去5〜10期の分布（平均・標準偏差）を基準に、直近値がどこに位置するかをZスコア化する。外部データ不要で最も再現性が高い。
  2. **絶対的な閾値**: Piotroski/Altman のように「黒字か否か」「1倍を超えるか」など理論的・会計的に意味のある固定ラインを使う（自己資本比率50%、流動比率100%、インタレストカバレッジ1倍など）。
  3. **登録銘柄間の相対順位**: 業種が近い銘柄をユーザー自身が複数登録している場合、その中での順位・パーセンタイルを表示する（母集団は小さいが、ゼロよりは情報がある）。
  4. **市場全体の代理指標**: 東証が公表する規模別・業種別の平均PER/PBR等の公開統計（例: 東証統計月報）を静的なテーブルとして保持し、比較のアンカーにする（本調査では東証統計そのものへのアクセスはしていない。実装時に別途確認が必要）。

---

## 7. 決算のサプライズ・質を財務数値から捉える方法

- **進捗率**: 四半期累計の実績 ÷ 会社の通期予想（売上高・営業利益・経常利益・純利益それぞれ）。第1四半期時点の進捗率が過去の平均的な進捗パターンより高ければ、中間決算までに上方修正が出やすい、という実務上の目安がある（通説）[出典: https://moneyworld.jp/news/05_00080939_news]。
- **修正基準**: 東証の適時開示ルールでは、売上高で±10%、利益項目（営業利益・経常利益・純利益のいずれか）で±30%の予想比乖離が生じる見込みとなった場合に業績予想の修正開示が必要、とされる（具体的な数値基準は検索結果の要約に基づく。正確な数値は東証の開示規則原文で要確認）[出典: https://faq.jpx.co.jp/disclo/tse/web/knowledge6851.html]。
- **季節性**: 四半期ごとの売上・利益パターンを前年同期と比較することで、一時的な変動と構造変化を区別する（業種依存性が高い一般論）。
- **利益の質（CFとの乖離）**: §1-4・§2-6のアクルーアル指標がそのまま「質」の代理指標になる。営業利益や純利益が増えているのに営業CFがついてこない場合、売上債権や棚卸資産の積み上がりなど非現金の増益要因を疑う、というのが実務上のアプローチ（Sloan の枠組みの応用）。

---

## 出典一覧（本文中の番号は文中リンクを参照）

- Piotroski F-Score: https://en.wikipedia.org/wiki/Piotroski_F-score, https://stablebread.com/piotroski-f-score/, https://www.quant-investing.com/blog/piotroski-f-score-improves-global-stock-performance
- Altman Z-Score: https://en.wikipedia.org/wiki/Altman_Z-score, https://www.creditguru.com/index.php/bankruptcy-and-insolvency/altman-z-score-insolvency-predictor-for-non-manufacturers-emerging-markets
- Beneish M-Score: https://en.wikipedia.org/wiki/Beneish_M-score, https://stablebread.com/beneish-m-score/
- Mohanram G-Score: https://www.stockopedia.com/content/glamour-stocks-avoiding-falling-stars-using-mohanramrsquos-g-score-57623/, https://stablebread.com/mohanram-g-score/
- Greenblatt Magic Formula: https://www.gurufocus.com/tutorial/article/57/greenblatts-earnings-yield-and-return-on-capital
- Sloan アクルーアル: https://quantpedia.com/strategies/accrual-anomaly, https://www.stockopedia.com/content/the-accrual-anomaly-why-investors-should-care-about-accruals-earnings-quality-63003/, https://www.quant-investing.com/glossary/accrual-ratio-balance-sheet
- ROIC: https://www.wallstreetprep.com/knowledge/roic-return-on-invested-capital/
- インタレストカバレッジ: https://www.wallstreetprep.com/knowledge/interest-coverage-ratio/
- PER/PBR実務: https://www.matsui.co.jp/stock/study/article/per/, https://www.matsui.co.jp/money-satellite/column/beginner/stock/cl-pbr.html
- 日本基準・IFRS比較: https://www.obc.co.jp/360/list/post415, https://globis.jp/article/8011/
- 親会社株主帰属利益: https://cpa-noborikawa.net/oyakabu-rieki-toha/, https://biz.moneyforward.com/accounting/basic/53857/
- のれん償却差異: https://www.pwc.com/jp/ja/knowledge/column/goodwill-amortization-and-impairment.html, https://taxjudge.com/2026/07/25/noren-ifrs-jgaap/
- 業績修正・進捗率: https://moneyworld.jp/news/05_00080939_news, https://faq.jpx.co.jp/disclo/tse/web/knowledge6851.html
- 業種比較の限界: https://www.prosightfa.org/insights/industry-financial-ratio-benchmarking-for-small-businesses-101-tips-for-business-owners-and-bankers/, https://www.researchgate.net/publication/345007710_FINANCIAL_RATIOS_BENCHMARKS_-_AVERAGE_OF_INDUSTRY_OR_SOME_OTHER_MEASURE
- CAGR/トレンド: https://www.wallstreetprep.com/knowledge/cagr-compound-annual-growth-rate/
- EDINET XBRLタグ例（第三者サイトの参考情報。EDINET APIには本調査ではアクセスしていない）: https://edinetdb.jp/docs/metrics
