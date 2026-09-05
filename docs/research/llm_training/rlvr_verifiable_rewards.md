# 検証可能報酬の RL (RLVR / GRPO) と構造化出力 (調査: 2026-09-05)

> ⚠ 出典は調査時に収集したもの。筆者が独立に再検証したものではない。

## 数学・コード以外への展開は実際に進んでいる

- JSON/YAML スキーマ準拠を段階的報酬で最適化、**350M モデルで 22.6%→29.7%**
  ([HF blog: GRPO + IFStruct](https://huggingface.co/blog/grpo-with-trl-ifstruct))
- NER 特化 RL (NE-R1 / LA-RL) が in-domain **F1 +2.5pt** 前後
  ([arXiv:2609.02366](https://arxiv.org/abs/2609.02366))
- 引用・根拠付けを NLI 検証器で報酬化する研究が複数

⚠ ただし玉石混交: 構造化出力への GRPO を扱った RL-Struct
([arXiv:2512.00319](https://arxiv.org/abs/2512.00319)) は**著者により撤回**されている。
査読前論文の数値を鵜呑みにしない。

## ⭐⭐ rule-based 検証器は reward hacking に構造的に強い

- LLM-as-Judge 型の報酬は判定者のバイアスを突くハッキングが研究対象になっている
  ([arXiv:2606.04923](https://arxiv.org/abs/2606.04923))
- **純粋な機械照合の検証器は「学習可能な代理指標が無い」ため突く隙が少ない**
  ([FutureAGI 2026](https://futureagi.com/blog/build-verifiable-reward-pipelines-rlvr-2026/))
- ⭐⭐ 引用検証タスクの実例: **引用不正ペナルティを外した途端に引用品質 F1 が 68%→23% に崩壊**
  ([arXiv:2506.15522](https://arxiv.org/html/2506.15522))。複合報酬は**成分ごとに on/off
  できる形で実装し、崩壊を検知できるようにする**
- 同論文では「推論トレースでは答えを見つけたと書き、最終出力では回答不能と拒否する」
  **推論-出力の矛盾型ハッキング**も観測されている

## 手法の選択

- GRPO = PPO から critic を排し、同一プロンプト複数サンプルの群内平均を baseline に
  ([DeepSeekMath, arXiv:2402.03300](https://arxiv.org/abs/2402.03300))
- RLOO = 「PPO の複雑さの大半は RLHF に不要」(ACL 2024,
  [arXiv:2402.14740](https://arxiv.org/abs/2402.14740))。小規模では GRPO とほぼ同等 (65.3 vs 65.7)
- 報酬設計: 段階的スコアは学習曲線を滑らかにするが**最終到達点は二値と変わらない**という
  比較もある ([arXiv:2601.03525](https://arxiv.org/html/2601.03525v3))

## ⭐ SFT → RL の順序は実証されている

- 根拠検証タスクで **SFT→GRPO は GRPO→SFT を 15% 上回る**
  ([arXiv:2506.15522](https://arxiv.org/html/2506.15522))
- DeepSeek-R1 の cold start SFT も同じ理由 (RL から始めると不安定)
- 役割分担「SFT で分布を形式へ寄せ、RL で契約違反を狙い撃ち」を文献は支持

## 小規模での成功例と、MLX の現実

- ⭐ **500 サンプル・100 ステップ・LoRA r=16 (パラメータ 1.66%) で有意に改善**、
  16GB GPU で完結 ([HF blog](https://huggingface.co/blog/grpo-with-trl-ifstruct))
  → 数百プロンプト規模でも rule-based reward があれば効く
- ⚠ **Apple Silicon は未成熟**: mlx-lm-lora の GRPO は 12 手法を実装済みだが
  **カスタム報酬つき GRPO が ~0.02 it/s** と遅い報告。ART-MLX は著者自身が実装途上と明言
  ([ART-MLX](https://themenonlab.blog/blog/art-mlx-grpo-apple-silicon-work-in-progress))。
  TRL / verl / Unsloth は実績豊富だが **CUDA 前提**

## この repo への適用判断

**現実的。ただし本格 GRPO からではなく段階を踏む。**

土台は揃っている — 識別子関門・引用実在関門・スキーマ検査・番号参照照合は、文献が
理想とする「学習可能な代理指標を持たない rule-based verifier」そのもの。DPO 用の
rejected 収集 (蛇口, event_draft_rejects) ともデータ経路を共有できる。

推奨順序:

1. **検証器自体のオフライン評価** — 既知の良い/悪い出力への偽陽性・偽陰性率を測る
   (⭐ 8月22日の引用関門で「照合器の偽陰性 2.7%」を実測済み — 同じ検査を報酬転用前に全関門へ)
2. SFT を完了させる
3. **数百プロンプト・LoRA の小規模 RLOO/GRPO PoC** を「契約違反の是正」に絞って
4. 報酬は二値主体 + 薄い段階的信号。**成分ごとに on/off 可能に**
5. RL 前後で**見逃し 0 件の negative control** を必ず通す

⚠ いきなり 26B の本番規模オンライン GRPO に投資するのは時期尚早。
