# SFT 転移失敗の診断 — 継続学習の不動・seed 分散・尾部欄の欠落 (調査: 2026-09-11)

n16 v4/v5 の不合格 (SYNTHESIS §40) を受け、**「何が起きているか」を文献と学習設定の事実で
説明し、次に何を変えるべきかを序列化する**。調査は 4 本の並列 survey (LoRA ハイパラ /
継続学習と分散 / トークン損失と長さ / 推論蒸留レシピ) + mlx_lm 0.31.3 のソース確認。
既存ノート ([[catastrophic_forgetting]] [[multitask_interference]] [[distillation_transfer]]
[[evaluation_methodology]]) と重複する部分は参照に留める。

---

## 0. 説明すべき実測 (3 つ)

| # | 実測 | 数値 |
|---|---|---|
| R1 | **under-transfer**: 最良の N1 でも教師の 1/2〜1/3。spotlight では教師 caveats 4.8 件 / outlook 3,400 字に対し生徒 0〜2 件 / 1,100 字 (483 露出で不動) | event 教師参照 caveats 3.03 / unknowns 6.49 vs N1 1.54 / 2.44 |
| R2 | **from-scratch 分散**: 近同一データ・同一レシピ 4 回で caveats 0.85〜1.54 (v1 = 最良の引き) | §25 |
| R3 | **継続学習の不動**: v1 resume (lr 5e-6) + 自データ replay で ~1.0 帯へ戻り、新タスクの分布は動かない。v4 (36 対×4) と v5 (161 対×3) が同値 | §40 |

## 1. 学習設定の事実 (ローカル確認、外部調査より優先)

- **LoRA の実装** (`mlx_lm/tuner/lora.py`): `y + scale · (x @ A) @ B`、A は uniform(±1/√d_in)、
  B はゼロ初期化、**scale = 20** (α/r ではなく直接乗数)。
- **⚠ 実効学習率の読み替え**: Adam は勾配の大きさを正規化するので、B の 1 ステップ更新量は
  lr にほぼ比例し、ΔW への寄与は **scale × lr** で効く。mlx 既定の (scale 20, lr 1e-5) は
  **実効 2e-4** に相当し、PEFT 慣行 (α16/r8 = scale 2, lr 2e-4 → 4e-4) の 1/2、
  Thinking Machines の LoRA 最適 (α32/r8 = scale 4 × lr ~1.5e-4 → 6e-4) の 1/3。
  **調査エージェントの「lr が 10〜15 倍低い」は scale を見落とした誤り**。低いのは
  事実だが 2〜3 倍で、n16 の 5e-6 (実効 1e-4) は最適の 1/6 かつ再加熱なし。
- rank 8 / **最後 8 層のみ** (30 層中) / 対象は mlx_lm 既定で attention・dense MLP・
  expert (switch_glu)・**router** まで全線形層 / trainable 0.35%。
- batch 1、warmup なし、decay なし (定数)、`grad_accumulation_steps` 1。
- 露出: v1 = 682 対 × ~1 epoch。n16 v5 = spotlight 161 対 × 3 複製 (≈3 epoch 相当)。
- 損失 = **トークン平均 CE** (`trainer.py:default_loss`、prompt はマスク)。リストを
  「もう 1 件足すか」の判断は `,` / `]` の数トークンに乗り、損失質量の大半は長文 outlook。
- 評価・本番の温度は 0.2〜0.3 (Modelfile の temp=1 は上書きされる)。

---

## 2. 問題点 — 実測ごとの原因候補 (文献対応、証拠強度つき)

### 2.1 R1: 分布が動かない (under-transfer)

| 原因候補 | 文献 | 強度 |
|---|---|---|
| **容量不足**: rank 8 は「plain instruction tuning でも full FT に並ぶには rank ≈256」の 1/32。LoRA は新規能力の獲得で full FT に劣り、データを増やすほど差が開く | Biderman et al. 2024 [arXiv:2405.09673] | 強 |
| **層の限定**: 部分層 LoRA は全層に劣る。attention-only は MLP-only に有意に劣る (MLP/expert が鍵) | LoRA Without Regret (Thinking Machines 2025, blog) | blog、ただし最も直接的 |
| **露出不足**: 同規模 curated SFT は 5〜15 epoch (s1: 1k×5 / LIMA: 1k×15 / LIMO: 817×15)。複製は ≤4 epoch までは新規データと等価だが、それ以上は減衰 | s1 [2501.19393] / LIMA [2305.11206] / LIMO [2502.03387] / Muennighoff [2305.16264] | 強 |
| **手順は密に示されないと移らない**: 模倣 FT は様式を移すが、模倣データに薄い課題の差は「ほぼ閉じない」 | Gudibande et al. 2023 [2305.15717] | 強 (既知、[[distillation_transfer]]) |
| **尾部リスト特有の機構**: 終端判断は少数トークンに集中し、EOS 候補が top-5 に 87.5% 入りながら選ばれない (蒸留推論モデルの実測)。トークン平均 CE は決定トークンの勾配を 1/N に希釈 | [2505.07961] (機構は一致、領域は別) | 単一論文・類推 |
| **mode collapse は温度で戻らない**: SFT の CE は分布の「形」を潰し、温度を上げても多様性は回復しない | [2602.07464] [2602.02244] | 単一論文 (2026 preprint) |
| exposure bias (teacher forcing) → on-policy 蒸留が長文で優位 | MiniLLM [2306.08543] / GKD [2306.13649] | 単一論文 (ICLR 2024) |

⚠ **「構造化 JSON の尾部リスト件数が SFT で減る」を直接扱った論文は無い**。上表の尾部機構は
推論チェーン蒸留・要約蒸留からの類推。文法制約デコードは `,` と `]` の両方を許すので、
**制約が機械的に短くしているのではなく、学習された確率質量の問題** (推論、引用なし)。
「Format Tax」[2604.03616] は、劣化の大半が「構造を要求するプロンプト」由来で、文法
エンジン自体の寄与は小さいと報告 (単一 preprint)。

### 2.2 R2: seed 分散

- **few-step・no-warmup・Adam・batch 1 は文献が名指しする高分散レジーム**。Dodge et al.
  [2002.06305] は seed とデータ順だけで大きな分散を実測し「複数 seed を回して検証で選抜/
  早期打切り」を推奨 (強)。Mosbach et al. [2006.04884] は不安定性の主因を「ステップ不足 +
  bias 補正なし Adam + warmup 不足」と同定し、warmup・小 lr・長め学習で解消 (強、ICLR 2021)。
- batch 1 では Adam の per-parameter 正規化がノイズを増幅し得る → 勾配蓄積で実効 batch を
  作る方が良い [2507.07101] (単一論文)。
- **LoRA アダプタの seed 間平均 (soup) は使えない**: model soups [2203.05482] の前提は
  「同一初期化・同一 basin」だが、LoRA の A は seed ごとに乱数初期化され部分空間が揃わない。
  これを直接検証した論文は無く、LoRA-Soups [2410.13025] / TIES / DARE は素朴平均が失敗する
  前提で作られている (間接)。**v1 を「特別なチェックポイント」として温存する根拠は無い**。

### 2.3 R3: 継続学習の不動

- **再加熱 (re-warm) は必須**: 継続学習では lr を一度上げてから再減衰しないと新分布に
  適応しない。低 lr 定数 resume は under-adaptation の型 (Gupta et al. [2308.04014] /
  Ibrahim et al. [2403.08763] TMLR、強)。n16 の 5e-6 定数はまさにこれ。
- **「同データを replay しながら続けると trait が消える」の直接論文は無い**。間接証拠は
  warm-start の汎化ギャップ・loss of plasticity (Dohare et al. Nature 2024 / DASH [2410.23495]):
  収束済みチェックポイントからの再開は同一データでも from-scratch に劣り得る。
  R2 と合わせると、**v1 の 1.54 は最適化軌道の偶然であり、続けて学習すれば attractor
  (~1.0) に戻るのは文献の予測と整合**。
- LoRA を MoE の router に掛けることの安全性は**未研究** (肯定も否定も無し)。
  ESFT [2407.01906] は expert を選んで full FT する別軸。

---

## 3. 解決策の候補 (証拠 × コスト)

| 案 | 内容 | 証拠 | コスト | 備考 |
|---|---|---|---|---|
| **A. 学習レシピの是正** | 全 30 層 (最低 16) / rank 32〜64 / 実効 lr を 2〜3 倍 (scale 20 のまま lr 2〜3e-5、または scale 4 × lr 1.5e-4) / warmup 5% + cosine / 勾配蓄積 4 / **3〜5 epoch を複製でなく反復で** / router を対象から外す | 強 (Biderman・s1/LIMA/LIMO・Mosbach) + blog (TM) | 中。**層数 ↑ で 1 iter が 2.5〜3 倍遅い** | メモリ壁: grad-checkpoint 下では L² 項は 1 層分が peak なので層数で壁は大きく動かない見込み — **要実測** |
| **B. from-scratch 複数 seed + 凍結審判で選抜** | 同レシピ 3 seed → event 39 + spotlight 6 で選ぶ | 強 (Dodge) | 学習 ×3 | A で 1 本目が届けば省略 |
| **C1. 尾部の最小件数関門 (デコード時)** | caveats / unknowns が空 or 1 件なら再サンプル (最大 N 回)、それでも空なら「関門不通過」を記録 | 単一論文の類推 (EOS-logit 介入 [2505.07961]) + repo の関門原則 | 極小 | 学習と独立・即効。**指示で埋めさせるのではなく、関門で拾う** |
| **C2. 決定トークン重み付け** | `,`/`]`/欄境界トークンの損失を 3〜5 倍 | 類推のみ (Rho-1 [2404.07965] / MiLe [2310.19531] / critical tokens [2510.10974]) | 小 (loss 差替) | **未検証の実験扱い** |
| **C3. RAFT / 棄却サンプリング SFT** | 生徒を温度 1 で N サンプル → rubric (caveats ≥3・unknowns ≥3・outlook 長) 合格のみ SFT | 強-中 (STaR [2203.14465] / RAFT [2304.06767]) | 中 (推論 + 学習) | simplicity bias: 易しい記事型だけ強化される → 層化 |
| **C4. 教師の尾部だけ補正 (hybrid target)** | 生徒 draft の caveats / unknowns / outlook だけ Opus に補筆させ、その対で SFT | 直接引用なし (RAFT 隣接) | 小〜中 (Opus 枠) | 教師信号を欠落箇所に集中 |
| **D. schema 内 CoT (analysis_notes 先頭欄)** | §41 の設計。教師に 5 増分の推論過程を明示させ、本番で捨てる | 建築的支持 (In-Writing [2601.07525] / Think Inside the JSON [2502.14905]) — ただし **「学習 vs プロンプト」を分離した実験は無い** | 中 (収穫 + 再学習) | **A と併用が前提**。リスト長すら移らない経路の上には載らない |
| **E. rubric 報酬の RL (GRPO / RaR)** | 5 増分を各 1 rubric 項目にして報酬化 | **領域一致では最強** (RaR [2507.17746] / ACE-RL [2509.04903]、長文・非検証領域で SFT 超え) | 高 (judge の硬化 + RL ループ + reward hacking 対策 [2605.12474]) | 既存 [[preference_optimization]] [[rlvr_verifiable_rewards]] の延長 |
| **F. 推論時分解 (増分ごとに 1 呼出 + 合成)** | 1 日 2 呼出の面で 6 呼出化 | 両刃: 弱い基線には効き、強い基線を壊す [2607.26922] / CoVe [2309.11495] | 極小 | **診断実験として 1 回**。学習と独立 |

⚠ **本当の on-policy 蒸留 (GKD / MiniLLM / TM On-Policy Distillation) は Opus API が
token logprob を出さないため実装不能**。使えるのは C3/C4 の系列水準版のみ。

---

## 4. 推奨 (順序と合否線)

**原則: 経路を先に直す。増分 (CoT・RL) は「リスト長が移る経路」ができてから。**
一度に全部変えず、**梃子を特定できる順**で 1 変数ずつ (2026-09 の教訓: 単発 run の少数件差は
再走で切り分ける・全腕を同じ再構築で測る)。

1. **Stage 1 — レシピの梃子を event 単独で切り分ける** (最安の判別実験)
   - v1 と同じ event 682 対 **のみ**、8 層のまま、変更は **rank 8→32 / lr 1e-5→3e-5
     (実効 6e-4) / warmup 5% + cosine / 勾配蓄積 4 / 2 epoch (反復)**。1,364 iters ≈ 7h
     (現速度)。
   - 合否線 (event 凍結 39、温度 0.2): caveats ≥ 2.0 かつ unknowns ≥ 3.0 (教師 3.0 / 6.5 へ
     半分以上詰める)。**N1 同等 (1.54 / 2.44) 止まりなら容量でなく露出 or 尾部機構が主因**。
   - 同時に C1 (最小件数関門) を本番 spotlight に敷く — 学習と独立で即効、失敗しても害なし。
2. **Stage 2 — 層数を 8→30 (または 16) にして同条件** (Stage 1 が線を越えたら省略可)
   - 先に **メモリ壁を 12.5k 系列で実測** (grad-checkpoint 下で L² 項が 1 層分なら壁は不変の
     見込み、iter 速度は 2.5〜3 倍遅い)。
3. **Stage 3 — 混合 from-scratch (event 682 + spotlight 161) を Stage 1/2 の勝ちレシピで**、
   3 epoch 反復。合否線: event 退行なし (caveats ≥ 1.5 / unknowns ≥ 2.4) **かつ**
   spotlight caveats ≥ 3 / outlook ≥ 2,500 字。届かなければ **seed を 2 本追加して選抜** (B)。
4. **Stage 4 — 増分 5 種**: Stage 3 通過後に D (schema 内 CoT 教師 100〜200 対、状況総括
   パイロット) を同レシピに載せる。D でも移らなければ E (rubric RL) へ。
   **F は今すぐ 1 回だけ診断** (6 呼出分解 vs 単発を Opus 凍結審判 6 件で比較) —
   学習の結果を待たずに「分解で増分が出るか」を知れる。
5. **やらないこと**: v1-resume 系の派生 (倍率・iters 分割・lr 微調整) / LoRA seed soup /
   温度上げによる多様性回復 / rank だけ機械的に上げる (単調改善しない [[multitask_interference]] §4)。

### 時間予算の現実

| 構成 | iter/分 (実測・推定) | 1,364 iters | 2,529 iters (843×3) |
|---|---|---|---|
| 8 層 (現行) | 3.2 実測 | ~7h | ~13h |
| 30 層 | ~1.1 推定 | ~21h | ~38h |

30 層 × 3 epoch は夜間 1 帯に収まらず、帯ガード (05:50〜07:15 / 18:50〜20:15) を跨ぐ
**複数夜の resume 運用**になる。`--resume-adapter-file` + optimizer state は mlx_lm が
optimizer state を保存しないため **lr schedule も含めて分割学習の設計が要る** (Stage 2 の前に確認)。

---

## 5. 手元の実測との対応

| 実測 | 文献の説明 |
|---|---|
| N1 でも教師の 1/2〜1/3、spotlight は 0 | rank 8・8 層・1 epoch は容量・露出とも文献の下限外 (Biderman / s1・LIMA・LIMO)。尾部リストは終端判断の希薄化で最初に落ちる (類推) |
| from-scratch 4 回で 0.85〜1.54 | Dodge / Mosbach の高分散レジームそのもの (few-step・no-warmup・batch 1・Adam) |
| v1 resume + replay で ~1.0 に戻る | 再加熱なし低 lr = under-adaptation (Gupta / Ibrahim)。v1 は軌道の偶然で、続ければ attractor へ (warm-start gap、間接) |
| v4 (36 対) と v5 (161 対) が同値 | データ量は梃子でなかった — **経路 (lr・容量・露出) が律速** の裏付け |
| ach1 は 355 対×4 from-scratch (lr 1e-5) で懐疑性を移せた | 判定課題は「形式」に近く少データで移る非対称性 ([[multitask_interference]] §3)。生成の尾部とは別物 |
| Q4→Q8 で差が埋まらない (§SYNTHESIS 冒頭) | 量子化は主因でない、と整合 |

## 6. 未検証・注意 (引用が無いまま使わないこと)

- 「構造化 JSON の尾部リスト件数の under-production」を直接扱った論文は無い (全て類推)。
- 「同データ replay で trait が消える」の直接検証は無い (間接)。
- LoRA を MoE router に掛ける安全性は未研究。外すのは「リスク低減」であって「改善の根拠」ではない。
- LoRA seed soup の可否は未検証 (前提が崩れている方向)。
- **調査エージェントの「lr 10〜15 倍低い」は誤り** (mlx の scale 20 を未考慮)。実効 2e-4 で
  最適の 1/2〜1/3。外部調査は必ず手元の実装 (§1) と突き合わせる ([[multitask_interference]] 末尾の教訓と同型)。
- 全層 LoRA でメモリ壁が不変という見立ては grad-checkpoint の実装依存 — 実測前提。

## 参照 (主要)

LoRA Without Regret (Thinking Machines 2025, https://thinkingmachines.ai/blog/lora/) /
Biderman et al. 2024 [2405.09673] / LoRA+ [2402.12354] / rsLoRA [2312.03732] / QLoRA [2305.14314] /
Dodge et al. 2020 [2002.06305] / Mosbach et al. 2021 [2006.04884] / Small Batch [2507.07101] /
Model soups [2203.05482] / LoRA-Soups [2410.13025] / Gupta 2023 [2308.04014] / Ibrahim 2024 [2403.08763] /
DASH [2410.23495] / Muennighoff 2023 [2305.16264] / s1 [2501.19393] / LIMA [2305.11206] / LIMO [2502.03387] /
Gudibande 2023 [2305.15717] / Li 2025 [2502.12143] / EOS bias [2505.07961] / MiniLLM [2306.08543] /
GKD [2306.13649] / Rho-1 [2404.07965] / MiLe [2310.19531] / critical tokens [2510.10974] /
SED-SFT [2602.07464] / Let Me Speak Freely [2408.02442] / Format Tax [2604.03616] /
STaR [2203.14465] / RAFT [2304.06767] / ReST [2308.08998] / RaR [2507.17746] / ACE-RL [2509.04903] /
rubric reward hacking [2605.12474] / In-Writing [2601.07525] / Think Inside the JSON [2502.14905] /
Two Calls Beat Five Agents [2607.26922] / CoVe [2309.11495] / ESFT [2407.01906]
