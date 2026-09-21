# Chronos Chart レポート視覚設計 調査報告

調査範囲: セルサイド／個人投資家向けアナリストレポートの定番図表、財務5期推移の見せ方、
スパークライン、KPIカード、表の可読性、情報の順序、印刷・PDF、和文タイポグラフィ、
やってはいけないこと。すべて Web 検索で確認できた一次・準一次情報に基づく。
`karauri.net`・`taisyaku.jp`・`api.edinet-fsa.go.jp` へのアクセス、LLM API 呼び出し、
ファイルダウンロード、git 操作、コード作成は行っていない。

---

## 0. 前提の再確認（この調査での解釈）

- 単一 HTML・外部リソース禁止 → 図はすべて**インライン SVG**。JS チャートライブラリ不可。
- 需給データは非表示。会社予想・同業比較データなし → **「予想比」「業界平均比」は描けない**。
  代わりに使えるのは「**自社の過去実績との比較**」（前期比、5期レンジ、自己の PER 過去レンジなど）。
- 投資助言・断定的予測はしない → 図もトーンも「事実の提示」に留める（矢印や色は「増減」であって
  「買い時／売り時」ではない）。

---

## 1. セルサイドアナリストレポートに実際に載る図表の型

### 1.1 定番の構成要素（複数の解説サイトが一致）

sell-side のレポートは「表紙サマリー（1ページ目）→ 詳細」という構成が定番。1ページ目には
レーティング・目標株価・投資見解の要約が置かれ、それを裏付ける形で図表が続く
（[Wall Street Prep](https://www.wallstreetprep.com/knowledge/sample-equity-research-report/)、
[Corporate Finance Institute](https://corporatefinanceinstitute.com/resources/valuation/equity-research-report/)、
[Mergers & Inquisitions](https://mergersandinquisitions.com/equity-research-report/)）。

1ページ目に置かれる定番要素:

- **サマリーボックス**（銘柄名・コード・市場区分・株価・出来高などの基本情報）
- **投資判断・要約の文章**（Chronos Chart には該当しないが、代わりに「総括」文が該当）
- **株価チャートのミニチュア版**（本体のチャートで代替可能）
- **主要財務指標のスナップショット**（KPI カード相当）
- **バリュエーションのレンジ図**（football field。ただし自社は同業比較データを持たないため注 1.3 参照）

### 1.2 財務5期推移・セグメント構成などの定番図

- **業績推移（棒＋折れ線の複合図）**: 売上高・純利益を棒、利益率を折れ線で重ねる形が「income statement
  annual data」の定番構成として複数の解説で確認できる
  （[Exceljet: Combo chart example](https://exceljet.net/charts/income-statement-annual-data)、
  [Kamil Franek: 7 Best Charts for Income Statement](https://www.kamilfranek.com/best-charts-for-income-statement-presentation-and-analysis/)）。
- **キャッシュフロー3本の見せ方**: 営業・投資・財務 CF を並べた棒グラフ、または期首→期末の増減を
  積み上げて見せる**ウォーターフォールチャート**が定番
  （[Yellowfin: Waterfall Charts for Cash Flow](https://www.yellowfinbi.com/blog/how-to-perform-cash-flow-analysis-using-yellowfin-waterfall-charts)、
  [Zebra BI: Cash Flow Statement in Excel](https://zebrabi.com/different-way-present-cash-flow-statement/)）。
  ウォーターフォールは「合計がどう構成されたか」を見せる図なので、営業 CF → 投資 CF → 財務 CF →
  現金増減、という単年の内訳を見せるのに向く。ただし**5期の時系列比較には不向き**（各期でウォーター
  フォールを並べると逆に読みにくくなる）。Chronos Chart では「5期分の時系列比較」が主目的なので、
  **単純な3系列の棒グラフ（営業・投資・財務を並べて表示、0 ラインを跨ぐマイナス値も許容）**の方が
  実用的と判断する（ウォーターフォールは費用対効果が低いとして§3で見送り）。
- **バリュエーションのレンジ図（football field）**: 複数のバリュエーション手法の結果をバー状に
  並べて比較する図（[Wall Street Prep: Football Field Valuation Chart](https://www.wallstreetprep.com/knowledge/football-field-valuation-real-example-excel-template/)、
  [CFI: Football Field Chart Template](https://corporatefinanceinstitute.com/resources/financial-modeling/football-field-chart-template/)）。
  **ただし本来は複数のバリュエーション手法や同業他社の倍率を横に並べる図であり、
  Chronos Chart は同業比較データも DCF などの評価モデルも持たない**ため、この形はそのまま使えない。
  代替として「PER バンドチャート」がある（§1.3）。

### 1.3 同業比較データがない場合の代替: 自社 PER/PBR の過去レンジ

投資銀行・株式リサーチの実務では、同業比較が使えない場面で「自社の過去の PER/PBR レンジ」を
使う手法が定着している。PE バンドチャートは「その銘柄の過去の P/E レンジのなかで、
現在の P/E が高いのか低いのか」を見せる図で、業績の安定した銘柄ほど株価がバンド内で推移する
傾向がある、という解説がある
（[WallStreetMojo: PE Band Chart](https://www.wallstreetmojo.com/investment-banking-charts-pe-charts-pe-band-chart-football-field-scenario-graphs/)）。
Chronos Chart は5期分の EPS・PER を持つので、「現在 PER が過去5期のレンジのどこにあるか」を
横棒（レンジ）＋現在値マーカーで示す図（Stephen Few の**バレットグラフ**に近い形）が実現できる。

バレットグラフ自体は「単一指標を、定性的な性能帯（グレー階調のバンド）と目標値（縦の短い線）に
対して、太い横線で現在値を示す」図として Stephen Few が 2005 年に考案したもの
（[Tableau: What is a Bullet Graph?](https://www.tableau.com/chart/what-is-bullet-graph)、
[Domo: What Is a Bullet Graph?](https://www.domo.com/learn/charts/bullet-graphs)）。
Chronos Chart には「目標値」がないので、目標マーカーは省き、「5期レンジ＋現在値」だけの
簡略版（＝ PER バンドの1本版）にする。

---

## 2. 財務5期推移の定番の見せ方（詳細）

| データ | 定番の図 | 根拠 |
|---|---|---|
| 売上高・営業利益・純利益 | 棒（売上高）＋棒または折れ線（利益）の複合図。同一軸か二軸かはスケール差で判断 | [Kamil Franek](https://www.kamilfranek.com/best-charts-for-income-statement-presentation-and-analysis/)、[financealliance.io](https://www.financealliance.io/financial-charts-and-graphs/) |
| 利益率（売上高営業利益率など） | 棒（絶対額）に重ねた**折れ線**、または単独の折れ線 | [Exceljet](https://exceljet.net/charts/income-statement-annual-data) |
| 営業・投資・財務 CF | 3系列の棒グラフ（0ラインを跨ぐ）。単年内訳を見せたいときだけウォーターフォール | [Yellowfin](https://www.yellowfinbi.com/blog/how-to-perform-cash-flow-analysis-using-yellowfin-waterfall-charts) |
| 自己資本比率・配当性向などの比率 | 単独の折れ線、または面グラフ。**0始まりの軸**を厳守 | データビジュアライゼーションの通説（§9） |
| PER・PBR | 自社の過去レンジ＋現在値（簡略バレットグラフ） | [WallStreetMojo](https://www.wallstreetmojo.com/investment-banking-charts-pe-charts-pe-band-chart-football-field-scenario-graphs/) |

**複合図で棒と折れ線を重ねるときの注意**: 単位もスケールも違う（売上高は億円、利益率は%）ため、
二軸グラフになる。二軸グラフは「軸を読み違える」「意図的に相関を強調できてしまう」という批判が
あるため、**両軸の始点を0に揃える・凡例に単位を明記する・折れ線の色を棒と明確に区別する**、
という最低限の作法を守る（§9 の「切り詰めた軸」の回避と同じ理由）。

---

## 3. スパークライン（Tufte）

### 3.1 定義と有効性

Tufte は sparkline を「文章・数値・画像の中に埋め込める、データそのものだけでできた小さな
高解像度グラフィック」と定義した。データインク比（データ以外の装飾を含まない比率）が
理論上 1.0 になる、というのが Tufte の主張である
（[Edward Tufte: Sparkline theory and practice](https://www.edwardtufte.com/notebook/sparkline-theory-and-practice-edward-tufte/)）。
表の各行に添えることで「大量の数値を、ページ面積をほとんど使わずに走査できる」という利点が
複数の解説で確認できる（[Newfangled: Sparklines](https://www.newfangled.com/sparklines/)、
[Perceptual Edge: Best Practices for Scaling Sparklines](https://www.perceptualedge.com/articles/visual_business_intelligence/best_practices_for_scaling_sparklines.pdf)）。

### 3.2 やりすぎの弊害（通説として明記）

- **小さすぎて読めない**: 2〜3点しかないと直線になり「上がった／下がった」以上の情報を持たない。
- **行ごとにスケールが違うと誤読を招く**: 各セルが自動で軸をフィットさせる実装（Excel の
  スパークライン等）では、行間で値の大小を比較できない・してはいけないのに、比較したくなる
  見た目になってしまう。
- **過剰使用でかえって読みにくくなる**: すべての行に付けると視線がどこに向かえばよいか
  分からなくなる。
（以上、[LinkedIn advice: sparklines best practices](https://www.linkedin.com/advice/0/what-best-practices-using-sparklines-your-data-hyquc)、
[InetSoft: What Is a Sparkline Chart?](https://www.inetsoft.com/info/how-to-make-a-sparkline-chart-definition-example/) に基づく通説）。

### 3.3 Chronos Chart への適用方針

40行の指標表すべてにスパークラインを付けるのは§3.2の弊害に該当するため**見送る**（§13）。
付けるなら「5期の時系列データを持つ主要指標（売上高・営業利益・EPS・自己資本比率など
10行前後）」に限定し、**各行で同じ物差し（例: 各指標ごとに5期の最小〜最大で正規化するが、
軸の0/最大値をキャプションに明記）**を使う。単独の数値行（貸借対照表の一時点値など）には
付けない。

---

## 4. KPI カード / サマリーボックスの設計

### 4.1 三点セットの型

現在値・前期比・トレンドの三点セットは実務で定着した型で、監視系の KPI カードは
「現在値・前期との差分・トレンドライン」を並べる、という解説が確認できる
（[Anastasiya Kuznetsova: Anatomy of the KPI Card](https://nastengraph.substack.com/p/anatomy-of-the-kpi-card)、
[Flerlage Twins: How to Create a KPI Card](https://www.flerlagetwins.com/2025/09/how-to-create-kpi-card-with-two-tricks.html)）。

### 4.2 「良い／悪い」を色だけに頼らない作法

- 赤緑の判別ができない色覚特性を持つ人が一定数いる（Web解説では「男性の約8%」という数値が
  紹介されている。これは通説として広く引用される数値であり、本調査で一次統計を検証したわけではない）
  （[AudioEye: Colorblind-Friendly Palettes](https://www.audioeye.com/post/colorblind-friendly-palettes/) 系のまとめ記事に基づく）。
- 実務上の代替案として **青／オレンジ**の配色が挙げられている。
- **色だけでなく、矢印などの二次的な記号を併用する**ことで、色の知覚に依存せず意味が伝わる、
  という考え方が WCAG 2.1 AA の「色だけに頼らない」原則と整合する。
- 学術・科学図表分野で最も広く参照される色覚多様性対応パレットは **Okabe–Ito パレット**
  （岡部正隆・伊藤啓、Color Universal Design プロジェクト、2002年）。8色＋グレーで、
  3種類の色覚特性シミュレーションで判別できるよう設計されている
  （[Okabe-Ito Colorblind-Safe Palette](https://sci-draw.com/blog/colorblind-safe-palettes-okabe-ito-reference)、
  [Okabe-Ito Palette: Gold Standard for Science](https://vizcept.com/blog/okabe-ito-palette-guide)）。
  Chronos Chart のレポートでも、良化/悪化の2色に **オレンジ系／青系**（Okabe-Ito のオレンジ
  `#E69F00` とスカイブルー `#56B4E9`、あるいは Vermilion `#D55E00` とブルー `#0072B2` の
  組み合わせが4色以内では最も判別しやすいとされる）を使い、**矢印（▲▼）や記号（+/-）を必ず併記**する。
- なお赤という色そのものの文化的意味（東アジアでは慶事・上昇の色、欧米では警告の色）に
  ついても言及されており、和文レポートである点は踏まえつつ、**色に意味を負わせすぎない**
  設計が無難（[AudioEye](https://www.audioeye.com/post/colorblind-friendly-palettes/) の記述に基づく通説）。

---

## 5. 表を読みやすくする定石

### 5.1 数値の書式

- **右揃え**が数値比較には最も効果的（桁数や大小に関わらず位を揃えて比較できるため）
  （[uxcel: Best Practices for Designing Tables](https://uxcel.com/lessons/tables-best-practices-356)）。
- **等幅数字（tabular figures）**を使うと、桁数が同じ数値の見た目の幅が揃い、走査しやすくなる
  （[Matthew Ström: Design Better Data Tables](https://medium.com/mission-log/design-better-data-tables-430a30a00d8c)）。
  CSS では `font-variant-numeric: tabular-nums` が使える（OS 標準フォントでも多くが対応）。
- 桁区切り（3桁カンマ）は日本語レポートでも数値の桁読み取りを助けるため踏襲する。

### 5.2 ゼブラ縞と行強調

行の縞模様（ゼブラストライピング）については意見が分かれる。
「大きいデータセットには有効」とする肯定的な立場
（[wpdatatables: Data Table UI Design Guide](https://wpdatatables.com/table-ui-design/)）と、
「配置・余白・グルーピングだけで読みやすさを達成すべきで、縞や塗りは避けるべき」とする
否定的な立場（[Adrian Roselli: A Responsive Accessible Table](http://adrianroselli.com/2017/11/a-responsive-accessible-table.html)）の両方が実務で見られる。
折衷的な立場として「画面を回転させたり文脈を変えたりする際に行を見失わないための
グルーピング手段として有効」という指摘もある。
→ Chronos Chart（40行規模の表）では、**淡い縞は使うがコントラストを弱く**し、
**見出し行だけは背景色・太字で明確に区別する**、という穏当な線を採用するのが妥当。

### 5.3 40行の指標表をどう畳むか

- ダッシュボード設計の通説では「一画面で認知的に扱える指標は 5〜9個」とされ、それを超える
  場合は**サマリー階層を上に置き、詳細はドリルダウン／折りたたみで表示する段階的開示
  （progressive disclosure）**が推奨される
  （[UXPin: Dashboard Design Principles](https://www.uxpin.com/studio/blog/dashboard-design-principles/)）。
- 具体的な手法として、関連指標を見出し・罫線・背景色でグルーピングする、重要指標を
  先頭に出し詳細は折りたたみ可能なセクションに送る、という指摘がある
  （[Medium: Progressive Disclosure in Enterprise Design](https://medium.com/@theuxarchitect/progressive-disclosure-in-enterprise-design-less-is-more-until-it-isnt-01c8c6b57da9)）。
- **参考事例（会社四季報の誌面設計）**: 四季報は1銘柄あたり紙面1/4ページという極端な制約の中で
  「基本情報 → 記者コメント（見通し・材料） → 業績数字（売上・営業利益・経常利益・純利益・
  1株益・配当） → 財務・株価指標（PER/PBR/配当利回り・自己資本比率・CF）」という
  少数ブロックへのグルーピングと、前号からの増減を矢印で示す工夫を長年積み重ねている
  （[東洋経済: 会社四季報 徹底活用術 PDF](https://str.toyokeizai.net/files/user/images/magazine/shikiho/%E4%BC%9A%E7%A4%BE%E5%9B%9B%E5%AD%A3%E5%A0%B1%20%E5%BE%B9%E5%BA%95%E6%B4%BB%E7%94%A8%E8%A1%93.pdf)）。
  これは「限られた面積で40項目級の情報を扱う」という Chronos Chart と同じ課題への
  実務解であり、**カテゴリ見出し＋主要指標優先＋前期比矢印**という構成をそのまま参考にできる。
- Chronos Chart への適用案: 40行の指標表を
  「① トレンド・モメンタム／② 出来高・ボラティリティ／③ バリュエーション／④ その他」
  のようなカテゴリに分け、各カテゴリの見出し行を挟む。さらに html の `<details>` 要素
  （JS 不要でネイティブに開閉できる）で「主要指標だけ初期表示、残りは折りたたみ」を実現できる
  （`<details>` はどのブラウザでも動作し、外部 JS・CSS 不要なので単一 HTML の制約に反しない）。

---

## 6. 情報の順序（逆ピラミッド）

- 逆ピラミッドは報道の型だが、ビジネス文書にも援用される。「結論を先に、根拠は後に、
  背景はさらに後に」という順序が、時間のない読み手への配慮になるという説明が複数ある
  （[NN/g: Inverted Pyramid: Writing for Comprehension](https://www.nngroup.com/articles/inverted-pyramid/)、
  [MindTools: Inverted Pyramid Writing](https://www.mindtools.com/ays0vz7/inverted-pyramid-writing/)）。
  詳細な報告書でこの型がそのまま使えない場合は「エグゼクティブサマリーだけ逆ピラミッド化する」
  という折衷案も紹介されている（同上）。
- Chronos Chart の現状の並び（銘柄情報表 → 総括 → テクニカル → 開示 → リスク → 注目点 →
  指標値表 → シグナル → 開示一覧 → 免責）はテクニカルの詳細（指標表40行）が中盤に埋もれており、
  「結論 → 根拠 → 詳細」の順に沿っていない。改訂案は§11参照。
- 見出しレベルの設計は「大見出し＝レポート全体のセクション、中見出し＝観点（テクニカル／
  開示／財務）、小見出し＝個別カテゴリ」の3階層に揃え、視覚的な大きさの比は§8のタイポグラフィ
  指針（1.5〜2倍刻み）に従う。

---

## 7. 印刷・PDF 化への配慮

- `page-break-inside` は非推奨（レガシー）プロパティで、現行の標準は `break-inside`。
  ブラウザは互換のため `page-break-inside` を `break-inside` のエイリアスとして扱う
  （[MDN: page-break-inside](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/page-break-inside)）。
- 表・図・カードなど「分断されると意味を失う要素」には `break-inside: avoid` を指定するのが
  定石。ただし要素自体が1ページより高い場合はブラウザがルールを無視して分断する、という制約が
  ある（[CSS-Tricks: page-break](https://css-tricks.com/almanac/properties/p/page-break/) 系のまとめ）。
- 見出し前で強制改ページする場合は `break-before: page` を使う。
- Chronos Chart の実装方針: `@media print { .kpi-card, table, svg.chart { break-inside: avoid; } h2 { break-before: page; } }` を1つの `<style>` 内に収める（外部ファイル不要、単一 HTML の制約に適合）。
- **白黒印刷への配慮**: 色だけで良化/悪化を示さない（§4.2）に加えて、面グラフ・棒グラフの
  塗りにパターン（斜線・ドットなど）を選択的に使う手法がある。これは元々「色覚多様性への配慮」
  と「白黒印刷対応」の両方を目的として使われる、と複数の解説が指摘している
  （[Highcharts: Patterns and contrast](https://www.highcharts.com/docs/accessibility/patterns-and-contrast)、
  [Plotly: Patterns, hatching, texture](https://plotly.com/python/pattern-hatching-texture/)）。
  ただしパターンは線が多いと視認性がかえって落ちる（シマシマ視認による目の疲労）という
  注意点もあり、**多用は禁物**（[Plotly文書](https://plotly.com/python/pattern-hatching-texture/) 内の注意書きに基づく）。
  Chronos Chart では「系列が2〜3本までの図に限り、色分けに加えて線種（実線/破線）または
  マーカー形状を変える」程度に留めるのが費用対効果として妥当（パターン塗りのフル実装は
  §13で見送り）。
- **A4想定の余白・幅**: 印刷では左右マージンは最低 1.5cm、20mm 前後を使う例が多い。
  本文フォントは 10〜12pt、行間 1.4〜1.5、1行の文字数はおおよそ60〜80字（欧文の目安）が
  快適とされる（[NZ政府 Web Accessibility Guide: Designing documents for print](https://govtnz.github.io/web-a11y-guidance/ka/accessible-ux-best-practices/designing-documents-for-print/)、
  [Butterick's Practical Typography: Page margins](https://practicaltypography.com/page-margins.html)）。
  CSS では `@page { size: A4; margin: 20mm; }` を使う実装例が紹介されている
  （まとめ記事の一般的な実装知識として確認）。和文の場合は全角文字換算で
  **1行30〜40字程度**が目安になる（§8）。

---

## 8. 和文レポートのタイポグラフィ

- **フォントサイズ**: 本文は16px前後が広く使われる目安として紹介されている
  （[Yahoo! JAPAN Tech Blog: 文字と行間の大きさ](https://techblog.yahoo.co.jp/entry/2023052430423559/)）。
  印刷を意識する場合は本文相当を10.5〜12pt程度に落とすのが一般的（§7の印刷指針と整合）。
- **行間**: 字送り（行間）が狭すぎると圧迫感で読みにくく、広すぎると視線の移動が間延びする、
  という指摘があり、極端を避けた中庸値（本文で概ね 1.6〜1.8 倍程度）が和文では好まれる傾向が
  複数の Web デザイン解説で共通して述べられている（[b-risk: タイポグラフィを考える](https://b-risk.jp/blog/2023/05/typography/)、
  [japan-design.jp: WEBデザインの最適なフォントサイズ](https://japan-design.jp/design/0052/)）。
- **見出しと本文の比率**: 見出しは本文の1.5〜2倍程度が目安として紹介されている
  （例: 本文16pxなら見出し24〜32px）。色・太さ・書体を変えることでもコントラストを付けられる
  （[japan-design.jp](https://japan-design.jp/design/0052/)）。
- **フォントスタック（OS標準のみ）**: Windows・macOS ともに標準搭載され、明朝とゴシックの
  組み合わせの相性がよいとされる「游書体（游ゴシック／游明朝）」が基準として挙げられている
  （[japan-design.jp](https://japan-design.jp/design/0052/) の記述に基づく）。
  実装上は次のような CSS フォントスタックが妥当:
  `font-family: "Yu Gothic UI","YuGothic","Meiryo",system-ui,-apple-system,"Hiragino Kaku Gothic ProN",sans-serif;`
  （游ゴシック→メイリオ→システムUI→ヒラギノ、の順にフォールバック。すべて Windows/macOS の
  標準搭載フォントで外部フォント参照なし）。
- **数値と日本語の混植・等幅数字**: 財務指標の数値は等幅数字（`font-variant-numeric:
  tabular-nums`）を使い、和文とのベースライン・字面の高さが不揃いにならないよう、
  数値部分だけ欧文フォント（游ゴシックの数字グリフか、Segoe UI 等の等幅数字）に切り出す実装が
  一般的な Web タイポグラフィの作法として紹介されている（一次情報としては上記フォントサイズ系の
  記事群からの一般化であり、和文複合フォントに特化した個別の一次資料は今回の検索では
  見つからなかった。**通説レベルの実務知**として明記する）。

---

## 9. やってはいけないこと（データビジュアライゼーションの定説）

いずれも複数の独立した解説記事で共通して否定されている、広く合意された通説。

1. **3D 円グラフ**: 遠近法により手前のスライスが実際より大きく見え、面積比の正しい判断を
   妨げる（[Polymer: 10 Good and Bad Examples](https://www.polymersearch.com/blog/10-good-and-bad-examples-of-data-visualization)、
   [Empire Stats: 12 Shocking Misleading Pie Charts](https://empirestats.net/2026/05/21/misleading-pie-charts/)）。
2. **0から始まらない棒グラフ（切り詰めた軸）**: 微小な差を過大に見せる、最も典型的な
   「グラフでの嘘」の手法として Wikipedia の Misleading graph 項目でも整理されている
   （[Wikipedia: Misleading graph](https://en.wikipedia.org/wiki/Misleading_graph)、
   [Heap: How to Lie with Data Visualization](https://www.heap.io/blog/how-to-lie-with-data-visualization)）。
   ※ 折れ線グラフ（率の推移など）は0始まりでなくても許容される場合があるが、**棒グラフは
   面積・長さで大小を直感的に伝える図なので0始まりを厳守**するのが実務上の線引き。
3. **チャートジャンク（過剰な装飾）**: 認知負荷を上げ、理解を遅らせ、精度を下げ、
   本質的な情報から目をそらさせる、という評価が定着している
   （[usefuldatatips: Avoiding Chartjunk](https://usefuldatatips.com/tips/visualization/avoiding-chartjunk)）。
4. **意味のない色分け・レインボー配色**: カテゴリ数に対して過剰な色数を使うと、
   識別のための色が逆に判別できなくなる（虹色パレットは特に隣接色の判別が難しい）
   （[GoodData: 9 Bad Data Visualization Examples](https://www.gooddata.ai/blog/bad-data-visualization-examples-that-you-can-learn-from/)）。
5. **二軸グラフの誤用**（今回の追加調査事項ではないが§2の複合図に直結）: 軸のスケールを
   恣意的に選ぶと実際には無関係な2系列が相関しているように見えてしまう、という一般的な
   批判があり、複合図を使うときは軸の始点・スケール比を恣意的に操作しないことが求められる。

---

## 10. 参考にした「チャート選択」の枠組み

Financial Times の Visual Vocabulary は、Deviation（差分）・Correlation（相関）・
Ranking（順位）・Distribution（分布）・Change over Time（時系列変化）・Part-to-Whole
（部分と全体）・Magnitude（量）・Spatial（空間）という8分類でチャート選択を整理しており、
日本語版も含め GitHub 上に公開されている
（[Financial-Times/chart-doctor: visual-vocabulary](https://github.com/Financial-Times/chart-doctor/tree/main/visual-vocabulary)、
[data.europa.eu: visual vocabulary](https://data.europa.eu/apps/data-visualisation-guide/visual-vocabulary)）。
Chronos Chart の財務5期推移は「Change over Time」、比率の構成は「Part-to-Whole」に
該当し、この枠組みに沿ってチャート選定の妥当性を再確認した（本報告の§1・§2の推奨はこの
枠組みと整合する）。

---

## 11. 【成果物1】入れるべき図表の一覧（優先順位つき）

凡例: 優先度 A＝最初に入れる／B＝次点／C＝余裕があれば。すべて単一 SVG（インライン）で実装可能。

| 優先度 | 図表 | データ | 図の型 | 置き場所 |
|---|---|---|---|---|
| A | 業績5期推移 | 売上高・営業利益・純利益（棒）＋売上高営業利益率（折れ線） | 複合図（棒＋折れ線、軸は0始まり） | 「総括」直後の新設セクション「財務ハイライト」冒頭 |
| A | KPI サマリーカード群 | 直近期の売上高・営業利益・EPS・自己資本比率・ROE（各: 値／前期比／5期スパークライン） | KPIカード（3〜5枚横並び） | セクション先頭（表より前） |
| A | テクニカル評価の要約バッジ | 既存の「評価＋根拠」 | 色＋矢印記号を併用したバッジ（Okabe-Ito 系2色） | テクニカルの観点セクション冒頭（本文の前） |
| B | キャッシュフロー5期推移 | 営業・投資・財務CF | 3系列の棒グラフ（0ラインを跨ぐ、色分け） | 財務ハイライト内、業績推移の次 |
| B | 自己資本比率・配当性向の推移 | 比率2系列 | 単独の折れ線または面グラフ（0始まり） | 財務ハイライト内 |
| B | PER 5期レンジ＋現在値 | 過去5期の PER 高値・安値・現在値 | 簡略バレットグラフ（横棒レンジ＋現在値マーカー） | 財務ハイライトの末尾、またはバリュエーションの小見出し |
| B | 指標表のカテゴリ内スパークライン | 5期分の時系列を持つ主要指標（10行程度に限定） | ミニ折れ線（Tufte 型） | 「主要指標」テーブルの各行末尾（40行全部には付けない） |
| C | 開示イベントの年表（タイムライン） | 開示一覧の日付 | 横軸タイムライン＋マーカー | 開示一覧セクションの前に要約図として |
| C | シグナル発生頻度のミニ棒 | 期間内シグナルの種類別件数 | 横棒（ランキング型） | 「期間内のシグナル」セクション冒頭 |

---

## 12. 【成果物2】レポート全体のセクション構成の改訂案

### 現状

1. 銘柄情報の表
2. 総括と判定
3. テクニカルの観点（評価＋根拠の箇条書き）
4. 開示の観点
5. リスク
6. 注目点
7. 指標値の表（40行）
8. 期間内のシグナル
9. 開示一覧
10. 免責

### 改訂案（差分がわかる形）

1. 銘柄情報の表 **（変更なし、ただし右揃え・等幅数字に整形）**
2. 総括と判定 **（変更なし。ここが逆ピラミッドの「結論」に当たるので先頭を維持）**
3. **【新設】財務ハイライト（KPIカード＋業績5期推移＋CF推移＋比率推移＋PERレンジ）**
   — 現状「指標値の表」の後半に埋もれていた財務系の情報を、結論の直後・詳細の前に図で要約する。
   これから追加される財務5期データの主戦場。
4. テクニカルの観点 **（冒頭に評価バッジを追加。文章の構成は変更なし）**
5. 開示の観点 **（変更なし）**
6. リスク **（変更なし。結論に近い重要度なので前方に残す）**
7. 注目点 **（変更なし）**
8. **指標値の表（40行）を「①モメンタム・トレンド／②出来高・ボラティリティ／③バリュエーション
   ／④その他」にカテゴリ分けし、主要10行程度だけ初期表示、残りは `<details>` で折りたたみ**
   — 現状は40行がフラットに並んでいたのを、進め方2.でグルーピングし直す。財務指標
   （PER・PBR・配当性向等）は③にまとめ、④の「財務ハイライト」で図示した内容と重複する
   数値もここに残す（図はサマリー、表は詳細という役割分担）。
9. 期間内のシグナル **（冒頭に件数の要約棒を追加）**
10. 開示一覧 **（冒頭に年表を追加）**
11. 免責 **（変更なし）**

**変更の要旨**: 「総括 → 財務ハイライト（新設・図中心） → テクニカル（バッジ追加） → 開示 →
リスク → 注目点 → 指標表（グルーピング＋折りたたみ） → シグナル → 開示一覧 → 免責」という
順に、逆ピラミッド（結論→根拠→詳細データ）に沿わせる。図表は「結論に近いセクションほど
上に」「詳細な数値表は後方に」という原則で再配置している。

---

## 13. 【成果物3】提案したが費用対効果が低いので見送るべきもの

- **キャッシュフローのウォーターフォールチャート（単年内訳型）**: 単年の内訳を示すには
  優れているが、5期の時系列比較という Chronos Chart の主目的には合わない。5期分を
  ウォーターフォールで並べるとかえって読みにくくなる。→ 単純な3系列棒グラフで代替。
- **football field（複数バリュエーション手法の横並び比較）**: 同業比較データも DCF 等の
  評価モデルもないため、そもそも比較対象を作れない。→ 自社 PER 過去レンジ（簡略バレット
  グラフ）で代替。
- **40行全指標へのスパークライン付与**: §3.2の「過剰使用の弊害」に該当し、実装コストの
  割に可読性がむしろ落ちる。→ 主要10行程度に限定。
- **パターン塗り（ハッチング）の全面採用**: 白黒印刷・色覚多様性の両方に有効だが、
  系列数が増えると視認性がかえって落ちる、線が多いと目が疲れるという指摘がある。
  → 系列2〜3本の図に限定し、線種・マーカー形状の変更を優先し、パターン塗りは最小限に。
- **開示イベントのタイムライン図・シグナル件数の要約棒**（§11で優先度Cとした2件）:
  情報としては既存の「開示一覧」「期間内のシグナル」の文章・表で代替可能であり、
  「見づらい」というユーザー評価の主因（財務・テクニカルの数値が図になっていないこと）への
  効果は間接的。実装の優先順位は最後でよい。
- **ゼブラストライピングの全面採用**: §5.2で見たとおり是非が分かれる手法であり、
  「配置・余白・グルーピングだけで読みやすさを達成すべき」という否定的な立場も有力。
  40行表は縞よりもカテゴリ見出し（§5.3）による区切りの方が効果が大きいと判断し、
  縞は補助的（淡色）に留める。
- **和文複合フォントでの数値部分の欧文フォント切り出し**（§8末尾）: 効果はあるが
  一次資料に乏しく、実装の複雑さ（CSS のフォントフォールバックの微調整）に見合うか
  疑問が残る。まずは等幅数字指定（`tabular-nums`）だけで様子を見る優先度でよい。

---

## 参考文献一覧（本文中で引用した URL）

- [Wall Street Prep: Equity Research Report | Format + Example](https://www.wallstreetprep.com/knowledge/sample-equity-research-report/)
- [Corporate Finance Institute: Equity Research Report](https://corporatefinanceinstitute.com/resources/valuation/equity-research-report/)
- [Mergers & Inquisitions: Equity Research Report](https://mergersandinquisitions.com/equity-research-report/)
- [Exceljet: Combo chart example — Income statement annual data](https://exceljet.net/charts/income-statement-annual-data)
- [Kamil Franek: 7 Best Charts for Income Statement Presentation & Analysis](https://www.kamilfranek.com/best-charts-for-income-statement-presentation-and-analysis/)
- [financealliance.io: 16 of the best financial charts and graphs](https://www.financealliance.io/financial-charts-and-graphs/)
- [Yellowfin: Cash Flow Analysis Using Waterfall Charts](https://www.yellowfinbi.com/blog/how-to-perform-cash-flow-analysis-using-yellowfin-waterfall-charts)
- [Zebra BI: Cash Flow Statement in Excel](https://zebrabi.com/different-way-present-cash-flow-statement/)
- [Wall Street Prep: Football Field Valuation Chart](https://www.wallstreetprep.com/knowledge/football-field-valuation-real-example-excel-template/)
- [CFI: Football Field Chart Excel Template](https://corporatefinanceinstitute.com/resources/financial-modeling/football-field-chart-template/)
- [WallStreetMojo: PE Band Chart / Football Field / Scenario Graphs](https://www.wallstreetmojo.com/investment-banking-charts-pe-charts-pe-band-chart-football-field-scenario-graphs/)
- [Tableau: What is a Bullet Graph?](https://www.tableau.com/chart/what-is-bullet-graph)
- [Domo: What Is a Bullet Graph?](https://www.domo.com/learn/charts/bullet-graphs)
- [Edward Tufte: Sparkline theory and practice](https://www.edwardtufte.com/notebook/sparkline-theory-and-practice-edward-tufte/)
- [Newfangled: Sparklines: Intense, Simple Word-sized Graphics](https://www.newfangled.com/sparklines/)
- [Perceptual Edge: Best Practices for Scaling Sparklines](https://www.perceptualedge.com/articles/visual_business_intelligence/best_practices_for_scaling_sparklines.pdf)
- [LinkedIn: best practices for using sparklines](https://www.linkedin.com/advice/0/what-best-practices-using-sparklines-your-data-hyquc)
- [InetSoft: What Is a Sparkline Chart?](https://www.inetsoft.com/info/how-to-make-a-sparkline-chart-definition-example/)
- [Anastasiya Kuznetsova: Anatomy of the KPI Card](https://nastengraph.substack.com/p/anatomy-of-the-kpi-card)
- [Flerlage Twins: How to Create a KPI Card](https://www.flerlagetwins.com/2025/09/how-to-create-kpi-card-with-two-tricks.html)
- [AudioEye: Colorblind-Friendly Palettes for Web Design](https://www.audioeye.com/post/colorblind-friendly-palettes/)
- [Okabe-Ito Colorblind-Safe Palette (sci-draw.com)](https://sci-draw.com/blog/colorblind-safe-palettes-okabe-ito-reference)
- [Okabe-Ito Palette: Gold Standard for Science (vizcept.com)](https://vizcept.com/blog/okabe-ito-palette-guide)
- [uxcel: Best Practices for Designing Tables in UIs](https://uxcel.com/lessons/tables-best-practices-356)
- [wpdatatables: Data Table UI Design Guide](https://wpdatatables.com/table-ui-design/)
- [Matthew Ström: Design Better Data Tables](https://medium.com/mission-log/design-better-data-tables-430a30a00d8c)
- [Adrian Roselli: A Responsive Accessible Table](http://adrianroselli.com/2017/11/a-responsive-accessible-table.html)
- [UXPin: Dashboard Design Principles](https://www.uxpin.com/studio/blog/dashboard-design-principles/)
- [Medium: Progressive Disclosure in Enterprise Design](https://medium.com/@theuxarchitect/progressive-disclosure-in-enterprise-design-less-is-more-until-it-isnt-01c8c6b57da9)
- [東洋経済: 会社四季報 徹底活用術 (PDF)](https://str.toyokeizai.net/files/user/images/magazine/shikiho/%E4%BC%9A%E7%A4%BE%E5%9B%9B%E5%AD%A3%E5%A0%B1%20%E5%BE%B9%E5%BA%95%E6%B4%BB%E7%94%A8%E8%A1%93.pdf)
- [NN/g: Inverted Pyramid: Writing for Comprehension](https://www.nngroup.com/articles/inverted-pyramid/)
- [MindTools: Inverted Pyramid Writing](https://www.mindtools.com/ays0vz7/inverted-pyramid-writing/)
- [MDN: page-break-inside](https://developer.mozilla.org/en-US/docs/Web/CSS/Reference/Properties/page-break-inside)
- [CSS-Tricks: page-break](https://css-tricks.com/almanac/properties/p/page-break/)
- [Highcharts: Patterns and contrast](https://www.highcharts.com/docs/accessibility/patterns-and-contrast)
- [Plotly: Patterns, hatching, texture in Python](https://plotly.com/python/pattern-hatching-texture/)
- [NZ Government Web Accessibility Guide: Designing documents for print](https://govtnz.github.io/web-a11y-guidance/ka/accessible-ux-best-practices/designing-documents-for-print/)
- [Butterick's Practical Typography: Page margins](https://practicaltypography.com/page-margins.html)
- [Yahoo! JAPAN Tech Blog: 文字と行間の大きさは何が良い？](https://techblog.yahoo.co.jp/entry/2023052430423559/)
- [b-risk: タイポグラフィを考える](https://b-risk.jp/blog/2023/05/typography/)
- [japan-design.jp: WEBデザインの最適なフォントサイズ](https://japan-design.jp/design/0052/)
- [Polymer: 10 Good and Bad Examples of Data Visualization](https://www.polymersearch.com/blog/10-good-and-bad-examples-of-data-visualization)
- [Empire Stats: 12 Shocking Misleading Pie Charts](https://empirestats.net/2026/05/21/misleading-pie-charts/)
- [Wikipedia: Misleading graph](https://en.wikipedia.org/wiki/Misleading_graph)
- [Heap: How to Lie with Data Visualization](https://www.heap.io/blog/how-to-lie-with-data-visualization)
- [usefuldatatips: Avoiding Chartjunk](https://usefuldatatips.com/tips/visualization/avoiding-chartjunk)
- [GoodData: 9 Bad Data Visualization Examples](https://www.gooddata.ai/blog/bad-data-visualization-examples-that-you-can-learn-from/)
- [Financial-Times/chart-doctor: visual-vocabulary (GitHub)](https://github.com/Financial-Times/chart-doctor/tree/main/visual-vocabulary)
- [data.europa.eu: visual vocabulary](https://data.europa.eu/apps/data-visualisation-guide/visual-vocabulary)
- [holistic-r.org（証券リサーチセンター、日本の新興株向け無料アナリストレポート）](https://holistic-r.org/)
