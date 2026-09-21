# LLM に金融・財務データを分析させる際のプロンプト設計 — 調査報告

調査日: 2026-09-21。コードの実装・実通信は行っていない（調査のみ）。
出典の質を各項目末尾に `[査読論文]` `[プレプリント]` `[ベンダー公式]` `[実務者ブログ/検証記事]` で明記する。
「〜と言われている」で終わる俗説は §11 に集約し、根拠の強さを評価した。

---

## 0. 結論だけ先に（後の§9〜11で詳述）

1. **いまのプロンプト設計（40列×120日の生CSVをそのまま渡す）は、判明している弱点に対して脆弱**。LLM は表の中の数値を「読む」ことはできても「計算する」ことは信頼できない、というのは査読研究で繰り返し確認されている。すでに evidence→assessment の順序にしているのは正しい判断（後述の premature-confidence 研究と整合）。
2. **財務指標を追加してもトークンを増やさない方法は存在する**: (a) アプリ側で比率・トレンド・シグナルを計算し「結論に近い要約」だけ渡す、(b) 40列の生値ではなく「直近日の値＋変化率＋シグナル判定」だけを渡し時系列全体は必要な列だけに絞る、(c) 定義表・凡例はキャッシュ可能な固定ブロックにして毎回作り直さない（Gemini はコンテキストキャッシュで再送コストを下げられる）。
3. **「1銘柄・1業界データだけを渡してLLMに財務分析をさせ好成績」を示した最有力の実証研究（Kim, Muhn & Nikolaev 2024, "Financial Statement Analysis with Large Language Models"）は2025年2月に著者自身によって撤回されている**（データと分析の矛盾のため）。この分野で最も引用されている「LLMはアナリストより優れている」という主張の根拠は、現時点で失われていると考えるべき。

---

## 1. LLM の数値推論の弱点（生の数値表を渡して計算させることの信頼性）

### 中心的な知見
- Transformer ベースの LLM は次トークン予測器であり、桁ごとの厳密な計算アルゴリズムを内部に持たない。GPT-4 は3桁×3桁の掛け算で **59%**（Dziri et al. 2023, "Embers of Autoregression" 系の追試）〜46%程度の正答率しか出せないという計測が複数報告されている。桁数が増えるほど劣化する。
  出典: [Embers of Autoregression](https://arxiv.org/pdf/2309.13638) [プレプリント], [xVal](https://arxiv.org/pdf/2310.02989) [プレプリント], [How well do LLMs perform in Arithmetic tasks?](https://arxiv.org/pdf/2304.02015) [プレプリント]
- 原因の一つは**トークナイザ**。GPT系の BPE は数字を左から3桁区切りでチャンク化するため、桁の位（一の位・十の位…）がトークン境界と一致せず、繰り上がり処理が破綻しやすい。右から区切る（またはカンマ区切りで右揃えを強制する）と GPT-4 の加算精度が **84.4% → 98.9%** に改善したという計測がある。
  出典: [Tokenization counts: the impact of tokenization on arithmetic in frontier LLMs](https://arxiv.org/html/2402.14903v1) [プレプリント]
- 誤りのパターンは「計算ミス」だけでなく、桁の読み違い・時系列の取り違え（どの行がどの日付かを混同する）・数式そのものの取り違えも報告されている。かつ、**LLM は誤った答えにも正しい答えと同じ自信度で応答する**ため、出力だけからは誤りを検知できない。
  出典: [Mathematical Computation and Reasoning Errors by LLMs](https://arxiv.org/pdf/2508.09932) [プレプリント]

### 「計算はアプリ側でやるべき」という主張の直接的根拠
- **PAL (Program-Aided Language Models)**: LLM に自然言語で問題を分解させ、計算そのものは生成した Python コードをインタプリタに実行させて答えを出す方式。CoT の失敗の主因が「分解ミス」ではなく「計算ミス」であることを分析で示し、計算をインタプリタに切り出すことで GSM8K で当時の SoTA（PaLM-540B の CoT を絶対値で+15%）を達成。**事後的に電卓を使わせるより、計算自体を最初から外部に委譲する方が効果が大きい**（GSM8K で外部電卓の後付け利用は+2.3%に対し、PALは+6.4%改善）。
  出典: [PAL: Program-aided Language Models (ICML 2023)](https://arxiv.org/abs/2211.10435) [査読論文]
- 結論として「アプリ側で計算し、結果だけ渡す」方針は、査読済みの実証結果に支持される。Chronos Chart がテクニカル指標を**アプリ側で計算してから渡している**現在の設計は、この知見と整合している。財務指標も同様に、比率計算はアプリ側で行い、LLM には計算結果と（必要なら）計算式の説明文だけを渡すべき。

---

## 2. 表形式データの渡し方（CSV / Markdown / JSON / 自然言語要約）

### 検証結果（フォーマットで精度が変わる）
- 複数ベンチマークの一致した傾向: **Markdown 形式（key:value 型を含む）がCSVより高精度**になりやすい。ある比較では Markdown の「key: value」スタイルが約60.7%、CSVが約44.3%という結果。ただしモデル依存性が大きく、GPT-5系はCSV/JSON/Markdownで大差がないが、DeepSeek系はJSONが有意に良いなど、**モデルごとに最適フォーマットが異なる**。
  出典: [TQA-Bench: Evaluating LLMs for Multi-Table QA](https://arxiv.org/pdf/2411.19504) [プレプリント], [Is CSV format better than JSON for sending data to LLMs?](https://www.getcrux.ai/blog/experiment-data-formats---json-vs-csv) [実務者ブログ]
- **Table Meets LLM (WSDM'24)**: CSV, JSON, XML, Markdown, HTML, XLSX を比較する査読済みベンチマーク。HTML/XMLの方がGPTに理解されやすいという結果も報告されており、「Markdownが常に最善」という単純な結論ではない。
  出典: [Table Meets LLM: Can LLMs Understand Structured Table Data? (WSDM 2024)](https://arxiv.org/abs/2305.13062) [査読論文]
- **トークン効率**: CSVが最も軽量、HTMLはCSVの約3倍のトークンを消費する。Markdownは平均してHTMLよりトークンを42%削減しつつ精度も維持できる、という報告がある。**精度とトークン効率はトレードオフ**であり、「精度を少し犠牲にしてでもCSVで送る」判断も、いまの Chronos Chart のような高トークン局面では合理的。
  出典: 同上 [実務者ブログ寄りの検証記事を含む・要注意]

### 長い時系列の渡し方の定石
- 「全期間の生データを渡す」より、**「要約統計＋直近の生データ」の組み合わせ**が良いとする実証がある。金融時系列予測でのLLM活用研究では、生の数値列をそのまま渡すより、変化を自然言語的な特徴（リターン率、トレンド方向、ボラティリティ）に変換した方が性能が上がると報告されている。
  出典: [Temporal Data Meets LLM — Explainable Financial Time Series Forecasting](https://arxiv.org/pdf/2306.11025) [プレプリント]
- ただし時系列を要約しすぎると情報が失われるため、実務では「直近N日は生データ、それ以前は要約統計（平均・標準偏差・トレンド）」というハイブリッドが多くの実装で採用されている（これは複数の実務者記事の共通見解だが、単独の厳密な比較実験による裏付けは確認できなかった＝俗説寄り、§11参照）。

### 現状設計への示唆
- 40列×120日のCSVは、Table Meets LLMやTQA-Benchが検証している「表の行数・列数が多いテーブルQA」の困難設定に近い。列が多いテーブルでは関連列を選び出す前段階でモデルが混乱しやすい、という報告がある一方、直接的に「40列×120行」というスケールでの精度崩壊を定量化した論文は見つからなかった（推測にとどめる）。

---

## 3. コンテキスト長と精度（Lost in the Middle / Context Rot）

### Lost in the Middle（査読論文）
- Liu et al. (TACL 2024, 元 arXiv 2023) は、複数文書QAとkey-valueリトリーバルで、**関連情報が入力の最初か最後にあるときに性能が最も高く、中間にあると急激に劣化する「U字カーブ」**を実証。入力を長くしても、モデルがその情報を頑健に使えるとは限らないことを示した。
  出典: [Lost in the Middle: How Language Models Use Long Contexts (TACL 2024)](https://aclanthology.org/2024.tacl-1.9/) [査読論文]

### Context Rot（Chroma, 2026 業界レポート・査読なし）
- Chroma の技術レポートは18モデル（GPT-4.1, Claude 4, Gemini 2.5, Qwen3等）を対象に、**入力トークン数を増やすだけで（ディストラクタを排除しても）平均7.9%精度が落ちる**ことを示した。「lost in the middle」はこの一部（20文書中5〜15番目の位置で30ポイント以上低下）。さらに**「整った・一貫性のある入力の方が、シャッフルした入力よりも精度が落ちやすい」**という直感に反する結果も報告している。
  出典: [Context Rot: How Increasing Input Tokens Impacts LLM Performance (Chroma)](https://www.trychroma.com/research/context-rot) [実務者/ベンダー系検証レポート・査読なし、方法論は公開されている]
- 実務的含意: 「モデルの公称コンテキスト長（100万トークン等）まで詰め込んでも安全」という前提は誤り。**200Kウィンドウのモデルでも5万トークン程度から劣化が始まりうる**。いま60日=28,000トークン、120日=53,000トークンという規模は、この観点では「まだ安全域」に見えるが、財務指標を追加してさらに伸ばす場合は要注意。

### 重要情報をどこに置くべきか
- Anthropic公式ドキュメントは、20K+トークンの長文入力では**「長文データはプロンプトの先頭に、指示・質問は末尾に置く」**ことを明示的に推奨し、社内テストで「末尾にクエリを置くと複雑な複数文書入力で応答品質が最大30%向上」としている。また、複数文書は`<document>`タグ等で構造化し、**「まず該当箇所を引用させてから作業させる」**ことで関連情報に絞り込ませる手法を推奨している。
  出典: [Claude Docs: Long context prompting tips](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices) [ベンダー公式・ただしClaude向け。Geminiでの再現性は未確認]
- Google（Gemini）公式は「1needle」のnaive haystackでは高精度（>99%）だが、**複数の関連情報を同時に探させるタスクでは精度が落ちる**ことを認めている。
  出典: [Gemini API: Long context](https://ai.google.dev/gemini-api/docs/long-context) [ベンダー公式]

### 現状設計への示唆
- 「銘柄情報→OHLCV→テクニカル指標→定義表→最新日の判定→シグナル→EDINET開示」という現在の並び順は、**最も重要な判断材料（最新日の指標判定、シグナル）が末尾近くに来ており、Lost-in-the-Middle的には悪くない配置**。ただし「指示（何を分析してほしいか）」が先頭か末尾かは要確認。長文データより後ろに指示を置く方が良いとされる。

---

## 4. 幻覚（数値の捏造）を減らす手法

### 効果が実証されている手法
- **根拠を先に引用させる（quote-then-answer）**: Anthropic公式は長文書タスクで「関連箇所を`<quotes>`タグに先に抜き出させてから結論を`<info>`タグに書かせる」ことを推奨。関連情報への注意を絞り込み、無関係な部分を無視させる効果があるとしている。
  出典: 同上 Anthropic Docs [ベンダー公式]
- **引用・出典フィールドを構造化出力に持たせる（RAG的グラウンディング）**: 検索拡張生成（RAG）で検索結果を参照させ、**出力に出典を強制する**ことで捏造引用を減らせるという報告がある。ただし完全にはゼロにならない（法律ドメインでRAG併用でも捏造引用が一定割合残る、13〜21%が捏造という報告もある＝ドメイン次第でかなり残存する点は要注意）。
  出典: [Citation Grounding: Detecting and Reducing LLM Citation Hallucinations via Legal Citation Graphs](https://arxiv.org/abs/2606.00898) [プレプリント]
- **Self-Consistency（複数推論パスのサンプリング＋多数決）**: 同じ問いに対して複数の推論過程をサンプリングし、最も一致する答えを採用する手法。追加学習なしで算術・推論ベンチマークの精度を大きく改善することが査読論文で示されている。ただしAPI呼び出し回数が増えるためコスト増（費用対効果は近年のモデルで逓減するという指摘もある）。
  出典: [Self-Consistency Improves Chain of Thought Reasoning (ICLR 2023)](https://arxiv.org/abs/2203.11171) [査読論文], [Self-Consistency Is Losing Its Edge (2026)](https://arxiv.org/pdf/2511.00751) [プレプリント]
- **「与えられたデータにない事実を書くな」という明示指示 + 事前の投資判断調査で確認された手法**: Anthropic公式は「調査前にファイルを読め、確認していない主張をするな」という明示的禁止指示（investigate-before-answering）が幻覚を減らすとしている（コーディング文脈だが一般化可能）。
  出典: 同上 [ベンダー公式]

### 効果が限定的・過信できない手法
- 温度（temperature）を下げることの効果は**タスク依存でまちまち**。ある研究では温度よりシステム指示（プロンプト設計）の方が事実精度への影響が大きく、温度の効果は「相対的に小さい」とされる一方、救急診断タスクでは温度0で100%、温度1で89.4%まで低下したという極端な例もある。**「温度を下げれば幻覚が減る」を一般則として過信するのは危険**（§8, §11参照）。
  出典: [Piloting Temperature-Driven Variability in Emergency Diagnostic Accuracy (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12611333/) [査読論文（症例研究レベル）], [Evaluating the Impact of Temperature and Instruction Strategies on Hallucination](https://dergipark.org.tr/en/pub/gujsa/article/1819131) [査読論文]

---

## 5. 金融ドメインでの LLM 活用の検証

### 最重要の留保事項: 看板論文が撤回されている
- **"Financial Statement Analysis with Large Language Models" (Kim, Muhn & Nikolaev, Chicago Booth, arXiv:2407.17866)** は、GPT-4に「標準化・匿名化した財務諸表」を渡し将来の増減益方向を予測させ、**アナリストの53〜57%に対しGPTが60.35%の精度**、CoTプロンプト（財務比率を計算させてから予測させる）でさらに向上、という主張で2024年に大きな注目を集めた。
- **しかし arXiv 上の現在の版（v3, 2025-02-20）は著者自身により撤回されている**。撤回理由は原文ママで以下:
  > "A co-author identified inconsistencies in the data and analyses while attempting to replicate past analyses from the working paper. Accordingly, we have temporarily withdrawn the working paper from circulation while we review the research findings."
  出典: [arXiv:2407.17866](https://arxiv.org/abs/2407.17866) [**撤回済みプレプリント** — 撤回前に多数のメディア・ブログが「LLMはアナリストより優秀」と紹介しているが、根拠自体が現在検証中]
- したがって、この論文を「LLMは財務分析でアナリストを超える」という主張の一次証拠として使うのは**現時点では不適切**。ただし、この論文が示していた**手法（財務比率をLLM自身に計算させてから予測させるCoT）自体**は、§1で述べた「計算はアプリ側でやるべき」という知見と対立する設計であり、参考にする場合は「LLMに計算させる」部分は真似ず、「比率を先に見せてから解釈させる」構成だけを参考にするのが安全。

### 実証されている範囲（他の研究）
- **数値推論ベンチマーク FinQA / TAT-QA**（査読論文、EMNLP系）: 財務諸表の表＋テキストに基づく数値推論タスク。GPT-4はFinQAで実行精度約76%程度、専用学習させたSoTAシステムで約89%（人間の専門家にはまだ届かない）。**「表とテキストが混在する財務QAでLLMは間違えうる」ことが定量的に確認されている**。
  出典: [FinQA (EMNLP 2021)](https://arxiv.org/abs/2109.00122 見つからず要再確認), 二次情報: [FinQA: The Benchmark Measuring AI Numerical Reasoning](https://beancount.io/bean-labs/research-logs/2026/05/13/finqa-numerical-reasoning-financial-data) [実務者ブログ、一次論文の数値を引用]
- **決算短信・IR資料分析の実務報告**: GPT-4は10-K（年次報告書）のMD&A（経営者による討議）の主要リスク2項目の特定や結論の把握を約85%の精度で行えたという報告がある一方、残り15%の誤りは「引用の確認・テキストとの突合が必要」としている。**「要旨の把握」はある程度できるが、無検証で数値の正確性を信頼してはいけない**という結論はここでも共通している。
  出典: [A comprehensive review of open source LLMs for earnings call reports](https://link.springer.com/article/10.1007/s44163-026-01334-9) [査読論文（レビュー）], [Daloopa: Can LLM Analyze Financial Statements Well?](https://daloopa.com/blog/analyst-best-practices/can-large-language-model-analyze-financial-statements-well) [ベンダーブログ・要慎重]
- **共通して報告されている「できないこと」**: (1) 長い決算説明会の書き起こし全体を一度に扱うと文脈窓の限界にぶつかる、(2) 専門用語・業界特有の会計処理での誤り、(3) 桁の誤読・計算ミス、(4) 複数ソースの数値が食い違う場合の統合判断が弱い。

---

## 6. 構造化出力（JSON スキーマ）の設計

### フィールド順序が品質に与える影響（複数ソースで一致）
- **「回答フィールドを推論フィールドより先に置くと、モデルが推論を終える前に答えを確定してしまう」**という指摘は、OpenAI Structured Outputsのコミュニティ実務知見や複数の実務者記事で共通して述べられている。**JSONは有効だが中身の結論が誤っている**という危険なパターンになる。
  出典: [Structured-chain-of-thought breaks some basic language-use principles](https://gist.github.com/yoavg/5b106275e38f4ccc796bc8ba7919060b) [実務者ブログ・言語学者による指摘], [OpenAI Structured Outputs](https://openai.com/index/introducing-structured-outputs-in-the-api/) [ベンダー公式]
- これを裏付ける査読研究として、**"Understanding and Mitigating Premature Confidence for Better LLM Reasoning"**は、モデルが明示的な推論が始まる前の時点ですでに最終回答に「ロックオン」してしまう現象を実証し、早期に確信したサンプルほど論理の飛躍（logical shortcut）が2.8倍多いことを示している。**Chronos Chartがすでに採用している「evidence→assessment」の順序は、この現象への対策として査読研究に支持される設計**。
  出典: [Understanding and Mitigating Premature Confidence for Better LLM Reasoning](https://arxiv.org/html/2605.24396) [プレプリント]
- 一方で"Language Models Don't Always Say What They Think"（Turpin et al., NeurIPS 2023）は、**たとえ推論を先に書かせても、その説明文が実際の判断根拠を反映しない（unfaithful）ことがある**ことを示した。バイアスを混入させた入力で誤答に誘導すると、モデルはその誤答をもっともらしく正当化するCoTを生成し、精度がBIG-Bench Hardのタスクで最大36%低下した。**「evidenceを先に書かせれば安心」ではなく、evidence自体が後付けの合理化になっていないかを検証する仕組み（例: evidenceに現れた具体的な数値が実際にデータ中に存在するかをアプリ側で検証する）が望ましい**。
  出典: [Language Models Don't Always Say What They Think (NeurIPS 2023)](https://arxiv.org/abs/2305.04388) [査読論文]

### thinking / CoT と構造化出力の分離
- Gemini・Claude双方のベンダー公式ドキュメントは、**「thinking（内部思考）」と「最終出力（構造化JSON）」を分離する**アーキテクチャを提供しており、thinkingブロックは構造化スキーマの外に出す設計が推奨されている。CoTをJSONスキーマの1フィールドとして埋め込む場合は、**そのフィールドをスキーマの最初に置く**ことが有効とされる（上記フィールド順序の議論と整合）。
  出典: [Claude Docs: Thinking and reasoning](https://platform.claude.com/docs/en/build-with-claude/thinking) [ベンダー公式], [Gemini structured output](https://ai.google.dev/gemini-api/docs/structured-output) [ベンダー公式]
- Gemini公式は`propertyOrdering[]`を明示的にサポートしており、**スキーマの記述順とモデルが生成する順序を一致させる**ことを推奨（順序を制御できる＝Chronos Chartの「evidence→assessment」方針をスキーマレベルで強制できる）。
  出典: [Google: Gemini API structured outputs announcement](https://blog.google/innovation-and-ai/technology/developers-tools/gemini-api-structured-outputs/) [ベンダー公式]

### 列挙型 vs 自由記述
- 分類・判定（シグナルの種類、トレンドの方向など）は enum にして自由記述の余地をなくすことで、モデルが存在しないカテゴリを捏造するのを防げる、というのはOpenAI/Google双方の構造化出力ドキュメントで明示されている一般原則。数値の理由づけ・定性的な説明は自由記述で許容し、**「事実」に関わる部分（銘柄コード、日付、シグナル種別）は極力enumか既存データからの参照に固定する**のが安全という設計思想が一致している。

---

## 7. 複数情報源を1つのプロンプトに統合する構成

- Anthropicは複数文書を`<documents>`→`<document index="n"><source>...</source><document_content>...</document_content></document>`のように**タグで階層化し、出典メタデータを明示する**ことを推奨。Chronos Chartの現在の構成（銘柄情報／OHLCV／テクニカル指標／定義表／最新日判定／シグナル／EDINET開示）も、各ブロックに見出しとソースを明示したXML/Markdownセクションとして分離するのが定石と一致する。
  出典: 同上 Anthropic Docs [ベンダー公式]
- **矛盾する情報の扱い**については、直接的な学術的ベストプラクティスは少ないが、実務的知見として (1) 情報源の信頼性の階層を明示する、(2) 矛盾を検出したら「両論併記」させ、モデルに片方を勝手に無視させない、という原則が複数の実務者解説で共有されている。ただし、これは体系的な比較実験で検証された結論ではなく、**設計原則としての合意にとどまる（俗説寄り、§11参照）**。
  出典: [How LLMs Handle Contradictory Information from Multiple Sources](https://geoaiomarketing.com/how-llms-handle-contradictory-information-from-multiple-sources/) [実務者ブログ]
- Chronos Chartの文脈で言えば、**空売り残高（karauri）と貸借取引残高（taisyaku）は更新頻度も定義も異なり、時に「見かけ上矛盾するシグナル」を出しうる**。この場合はプロンプト側で「両者は別々の制度・別々の対象を計測しており、直接比較できない」旨を明示し、モデルが片方だけを根拠に断定しないよう指示するのが安全（不変条件13「信用残と単独表記しない」とも整合）。

---

## 8. 温度・thinking 予算などの生成パラメータの影響

- **温度**: §4で述べた通り、効果はタスク依存で一貫しない。「システムプロンプト・指示設計の効果の方が温度より支配的」という査読研究がある一方、医療診断など一部のタスクでは温度0が明確に有利という報告もある。**財務分析のように「決まった入力から決まった結論を安定して出す」ことが目的の場合、低温度（0に近い値）を使う理由は十分にあるが、それだけで幻覚がなくなると期待すべきではない**。
  出典: 同上 [査読論文2件]
- **thinking予算**: Anthropic公式は「adaptive thinking」（モデルが自律的に思考量を決める）の方が、固定のbudget_tokensによる旧来のextended thinkingより性能が安定して良いとしており、複雑な問い・多段階推論が必要なタスクほどthinkingの効果が出るとしている。Gemini側もthinking budgetの概念を持つが、Chronos Chartが使っている`google-genai`のAPIでの具体的な挙動は本調査の対象外（実通信禁止のため未検証）。
  出典: [Claude Docs: Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking) [ベンダー公式]
- **不変条件9「retry_optionsを設定しない、送信するたびにai_usage.requestsを加算する」**という設計は、上記のいずれの生成パラメータ研究にも直接関係しないが、コスト管理の観点で妥当。

---

## 9. いまのプロンプト設計への具体的なダメ出しと改善案

### 良い点（続けるべき）
- **evidence→assessment の順序**は、premature-confidence研究（§6）に支持される設計。継続すべき。
- **需給データをAIに送らない**という不変条件1は、そもそも「与えていないデータについて書かせない」という幻覚対策の王道（§4）を制度的に強制しており、優れた設計。
- EDINET開示を「提出日・種別・概要・提出事由」だけに絞り本文を送らないのは、長文コンテキストのcontext rot（§3）を避ける観点からも妥当。

### 具体的な問題点
1. **「40列×120日の生CSVをそのまま渡す」は、少なくとも2つの既知の弱点にぶつかる**:
   - (a) LLMは表の中の数値同士を突き合わせて計算する信頼性が低い（§1）。もしプロンプト中で「直近のRSIとMACDの乖離を計算して」のような指示があるなら、それは危険。**アプリ側で計算済みの値・シグナルだけを見せ、LLMには"解釈"だけをさせる**べき（すでに一部そうなっているようだが、40列すべてを生の時系列で渡す必要が本当にあるか再検討する余地がある）。
   - (b) 120日×40列は約53,000トークンで、Context Rot研究（§3）が問題視する規模に近づいている。しかも「40列」の大半は直近日の判定・シグナルと重複した情報を含んでいる可能性が高い（例: 移動平均線が並んでいれば、日々の値の変化よりも「ゴールデンクロスが何日目に起きたか」という要約の方が情報価値が高い）。
2. **CSVという形式選択自体は、トークン効率の観点では合理的だが、精度の観点では最善とは限らない**（§2）。Markdownやkey:value型の方が精度が高いという報告もあるため、もし今後精度に問題が出るなら、フォーマットをA/Bテストする価値がある。ただし現状のモデル（Gemini）でのフォーマット感度は検証されていないため、**「CSVをやめてMarkdownにする」ことを今すぐ強制する根拠はない**（モデル依存性が大きいという知見と整合）。
3. **矛盾情報の扱いに関する明示的な指示がプロンプトにあるか確認すべき**。空売り残高（不定期更新・最大100件）と貸借取引残高（週次）は更新タイミングが異なり、また同じ「信用取引の残高」でも対象が違う。両者が矛盾して見える局面で、LLMが片方だけを根拠に強い結論を出すリスクがある（§7）。

### 改善の優先順位（提案）
1. **まず、120日分の40列すべてを本当に渡す必要があるか棚卸しする。** 直近20日は生データ、それより前は「シグナル発生日＋要約統計（トレンド方向、平均、直近との乖離）」に置き換えることで、トークンを増やさずに実質的な情報量（財務指標込み）を増やせる（§2, §9-4参照）。
2. **evidenceフィールドに、実際にプロンプト中に存在する数値・日付だけを書かせる制約を、スキーマの説明文や指示で明示する。**（「evidenceに書く数値は必ず入力データ中の値と一致していなければならない」等）これはunfaithful CoT（§6）への対策にもなる。
3. **矛盾しうる情報源（空売り残高 vs 貸借取引残高）に、明示的な「これらは別制度・直接比較不可」という注記をプロンプトに固定文として入れる。**

---

## 10. 財務指標を追加するときに、トークンを増やさずに情報量を増やす推奨

1. **生の財務諸表数値ではなく、アプリ側で計算した比率・トレンドだけを渡す。** PER, PBR, ROE, 自己資本比率, 増収率, 増益率などは全て事前計算し、「値＋前期比＋業種平均比（分かれば）」のような1行サマリーに圧縮する。これは§1のPALの知見（計算は外部に任せる）とも一致する。
2. **時系列の財務指標は「直近数期分の値＋トレンド方向（enum: 改善/横ばい/悪化）」に圧縮し、全期間の生の値を並べない。** 変化率・トレンドという形にすること自体が、時系列を自然言語的な特徴に変換する（§2で報告された性能改善パターン）ことに相当する。
3. **固定的な説明文（指標の定義表、財務指標の計算式の説明）は、Geminiのコンテキストキャッシュ機能を使い、銘柄ごとに再送しない。** これはトークン数の請求上のコストを下げるだけでなく（キャッシュ利用で最大90%割引という報告）、プロンプトの可変部分を小さく保つことでcontext rotのリスクも下げられる（§3, §8）。
4. **テクニカル指標40列のうち、最終判断に直結しない列（例えば計算過程の中間列）を削り、"シグナル化された結果"だけを渡す。** 40列すべてを見せることは、モデルに「どの列が重要か」を選び出す負荷を追加で強いており（Table Meets LLM等が指摘する多列テーブルの理解負荷）、財務指標を増やす前にまずここを整理する方が、トークンの節約と精度向上を両立できる可能性が高い。
5. **evidence/assessmentスキーマの中に「参照した列名・指標名」を挙げさせるフィールドを追加することを検討する。** これは追加のトークンコストがあるが、捏造検出（アプリ側で「言及された指標が実際にプロンプトに含まれていたか」を検証できる）という点で費用対効果が高い可能性がある。既存の実測トークン数と比較の上、優先度は中程度。

---

## 11. 根拠が弱い、または実務では効かないと判断した俗説

| 俗説 | 判定 | 理由 |
|---|---|---|
| 「温度を下げれば幻覚は減る」 | **根拠は限定的** | 査読研究でシステム指示の効果の方が支配的、温度の効果は「相対的に小さい」とする報告がある一方、タスクによっては顕著な効果も見られ、一般則として断言できない（§4, §8）。 |
| 「コンテキストは長ければ長いほど良い（モデルの公称ウィンドウまでは安全）」 | **明確に否定される** | Context Rot研究は公称ウィンドウよりずっと手前から性能劣化が始まることを示している（§3）。 |
| 「CoT（根拠を書かせること）で常に説明可能性・信頼性が上がる」 | **過大評価されている** | Turpin et al. (NeurIPS 2023) は、CoTの説明が実際の判断根拠を反映しない（unfaithful）場合があり、バイアスのある入力ではもっともらしい後付け説明を生成することを示した。evidence-then-assessmentの順序自体は有効だが、「evidenceを書かせれば安心」と考えるのは危険（§6）。 |
| 「Markdownテーブルなら常にCSVより精度が高い」 | **モデル依存で一般化できない** | TQA-Benchなど一部のベンチマークではMarkdownが優位だが、GPT-5系ではフォーマット間の差がほぼない、DeepSeekはJSONが有利、など結果が割れている（§2）。 |
| 「LLMに標準化された財務諸表を渡すだけでアナリストを超える予測ができる」 | **根拠論文が撤回済み、現時点で立証されていない** | Kim, Muhn & Nikolaev (2024) は2025年2月に著者自身が「データと分析の矛盾」を理由に撤回している（§5）。この主張を紹介する記事・ブログの多くは撤回前の情報を引用し続けている点に注意。 |
| 「複数の情報源が矛盾したら、LLMは自動的に適切にバランスを取ってくれる」 | **体系的な検証は乏しい** | 「信頼性の高い方を優先する」「両論併記する」等は実務者の間での合意にとどまり、金融ドメインに特化した比較実験は確認できなかった（§7）。プロンプト側で明示的な取り扱い方針を書く方が安全。 |
| 「JSON Schemaで出力を縛れば、モデルは与えられていない事実を書けなくなる」 | **半分正しく半分誤り** | 構造化出力はフィールドの型・列挙値・存在を強制できるが、フィールドの「中身の値が事実に基づいているか」までは保証しない。OpenAI/Google双方の公式ドキュメントも、スキーマ適合＝内容の正しさではないと明言している（§6）。 |

---

## 主要出典一覧（重要なもの抜粋）

- Gao et al., "PAL: Program-aided Language Models" (ICML 2023) — https://arxiv.org/abs/2211.10435 [査読論文]
- Wang et al., "Self-Consistency Improves Chain of Thought Reasoning" (ICLR 2023) — https://arxiv.org/abs/2203.11171 [査読論文]
- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (TACL 2024) — https://aclanthology.org/2024.tacl-1.9/ [査読論文]
- Turpin et al., "Language Models Don't Always Say What They Think" (NeurIPS 2023) — https://arxiv.org/abs/2305.04388 [査読論文]
- Sui et al., "Table Meets LLM" (WSDM 2024) — https://arxiv.org/abs/2305.13062 [査読論文]
- Kim, Muhn & Nikolaev, "Financial Statement Analysis with Large Language Models" (**撤回済み, arXiv v3 2025-02**) — https://arxiv.org/abs/2407.17866 [撤回済みプレプリント]
- "Understanding and Mitigating Premature Confidence for Better LLM Reasoning" — https://arxiv.org/html/2605.24396 [プレプリント]
- Chroma, "Context Rot: How Increasing Input Tokens Impacts LLM Performance" — https://www.trychroma.com/research/context-rot [業界検証レポート・査読なし]
- Anthropic, "Claude prompting best practices / long context tips" — https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices [ベンダー公式]
- Google, "Gemini API: Structured outputs" / "Long context" — https://ai.google.dev/gemini-api/docs/structured-output , https://ai.google.dev/gemini-api/docs/long-context [ベンダー公式]
- "Tokenization counts: the impact of tokenization on arithmetic in frontier LLMs" — https://arxiv.org/html/2402.14903v1 [プレプリント]
