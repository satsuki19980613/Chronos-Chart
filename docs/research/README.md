# 調査メモ（P11 の設計のための下調べ・2026-09-21）

> **これらは調査の生メモであり、仕様ではない。仕様の正は [SPEC.md](../SPEC.md)。**
> `RESEARCH.md` / `DESIGN.md` と同じ扱いで、食い違ったら SPEC を優先すること。
> 各ファイルには出典 URL が付いているが、**査読論文・ベンダー公式・実務者ブログ・通説が混在している**。
> 実装の根拠に使う前に、そのファイル内の「確証が取れなかった点」の節を必ず読むこと。

ユーザーの指示（「XBRL 財務数値の DB 化をやる。指標にまとめて AI に渡す方法を専門家の知見から調べる」「見やすいレポートの書き方を調査して強化してほしい」）を受けて、
Sonnet のサブエージェントで並行調査した結果（前半4本＝P11、後半2本＝P12）。メインが実データで裏取りした事実は SPEC §2.9・§2.7.6 に書いてある。

| ファイル | 内容 |
|---|---|
| [xbrl-edinet.md](xbrl-edinet.md) | EDINET の書類取得 API の `type`、XBRL の要素IDとコンテキスト、会計基準ごとの差、訂正報告書、パース手段 |
| [financial-metrics.md](financial-metrics.md) | 収益性・成長性・安全性・効率性・CF の指標の定義式と必要科目、合成スコア（F-Score 等）、バリュエーション |
| [llm-prompting.md](llm-prompting.md) | LLM に金融データを渡すときの知見。数値推論の弱点、表形式、長文脈の劣化、幻覚対策、構造化出力 |
| [analysis-framework.md](analysis-framework.md) | アナリストレポートの構成、ファンダ×テクニカル、イベントの一般的解釈、リスクの類型、出力スキーマ改訂案 |
| [report-visual-design.md](report-visual-design.md) | レポートに載せる図表の型、KPI カード、スパークライン、表の畳み方、印刷と和文タイポグラフィ、配色（P12） |
| [svg-and-viewer.md](svg-and-viewer.md) | インライン SVG の作り方の比較と、アプリ内で HTML を表示する方法（iframe・sandbox・WebView2）（P12） |

## とくに重要な留保

- **「LLM は財務諸表分析でアナリストを超える」の根拠としてよく引かれる論文
  （Kim, Muhn & Nikolaev, Chicago Booth, arXiv:2407.17866）は、2025-02 に著者自身が撤回している。**
  撤回理由は「データと分析に矛盾が見つかった」。この主張を設計の前提にしないこと
- 合成スコア（Altman Z-Score・Beneish M-Score・Mohanram G-Score）は**係数が米国データで推定されたもの**で、
  日本基準での再検証を示す一次文献は確認できていない。採用するなら「参考値」の扱いにすること
- 調査時点で**サブエージェントには EDINET API・karauri.net・taisyaku.jp へのアクセスを禁じている**。
  実データによる裏取りはメインが別途2回だけ行った（SPEC §2.9・PLAN §1「外部アクセスの消費」）
