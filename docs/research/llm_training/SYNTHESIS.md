# 統合考察: この用途に特化した LLM を post-training で作る

最終更新: 2026-09-05 夜。実測 (v1/v2/v3) と調査ノート 11 本の統合。

## 0. 目標の定義

**このツールの各用途に特化した LLM を SFT / RL で作り、ローカルで運用する。**
gemma4-26B や Ollama は手段であって前提ではない。変えない前提は
中国系モデル不可 (§4) / ローカル完結 / 日本語の生成品質 / 決定論的関門との併用。

## 1. 実測の到達点 (2026-09-04〜05)

| 版 | 構成 | 結果 |
|---|---|---|
| v1 | 事象ニュース単独 746 対 | 事象ニュースで base に有意勝利 (caveats 13勝1敗)。**未学習スキーマで縮退** (`"null"` 72% / 重複 56.8%)。triage は独立参照で区別不能。**spotlight へ正の転移・31B の 4 倍速** |
| v2 | 3 課題混合 32:35:33 | 欠陥解消・抽出改善・triage の Sonnet 一致 55→71%。**事象ニュースの慎重さが base 以下へ悪化** (干渉) |
| v3 | 混合 2:1:1 (事象 50%) | 学習・評価中。単一モデル路線の成否を判定する |

確定した周辺事実: Q4→Q8 は差をほぼ埋めない (量子化は主因でない) / 教師は Opus 単一
(課題別に測って確定) / MoE ではアダプタ単体を配備できず fuse 全量のみ (ソース確認済) /
本文の文字化け 241 件は別件の取り込み不具合 (未修正)。

## 2. 文献との照合 — 実測は既知パターンの再現だった

- **課題型で勝敗が決まる**: 固定タクソノミーの抽出・分類は特化 SFT が GPT-4 級に勝ちうる
  (LoRA Land 25/31)。自由 narrative は汎用大規模が強い。**手元の苦戦配置はこの通り**
- **契約は速く (150 反復)、能力は遅い (700 反復で教師の 39%)**: LIMA 系の非対称性と一致
- **v2 の劣化は忘却でなく干渉**。均等混合は失敗しやすい (T5)。2 段階化するなら
  **繊細な生成を後段に** (Chained Tuning) + replay
- **CPT は見送り**: 実績下限 (数億トークン) に対し手持ち 28k 記事は一桁下
- **RLVR が噛み合う**: repo の関門 (識別子・引用・スキーマ) は reward hacking に強い
  rule-based 報酬そのもの。SFT→RL 順序に +15% の実証。⚠ MLX 実装は未成熟 → 小規模 PoC から
- **評価**: 現行を基準にしない (循環バイアス) / 39/86/150 件では稀少クラスの見逃しを統計で
  検出できない → 決定論的関門を併用 / 床を毎回取る
- **配信**: 複数 LoRA の常駐 hot-swap はどの基盤も未成熟 (llama.cpp は KV 汚染バグ #26207)。
  **fuse 済み全量モデル構成が最も堅い**
- **Format Tax**: 構造化出力の劣化はデコード制約 (-1.6pt) よりプロンプト側 (-3.9pt) が主因で、
  **訓練で緩和できる** — スキーマ下で学習する現方針を支持

## 3. 収束しつつあるアーキテクチャ

**出力様式の族で 2 つのローカル特化に分ける** (利用者提案・干渉調査の第一推奨と一致):

```
モデル N (narrative 族): 事象ニュース → spotlight → 総括へ拡張
  要求 = JP 生成品質 + 30-40 秒/件 (毎時バッチ)。軽量高速の MoE 級が適合
  初版は v1 が既に存在。31B (4 倍遅い) の退役が成立条件つきで視野
モデル S (構造化族): triage + 抽出 + 要約
  要求 = 速度×呼出数 (triage ~2,000/日)。小型 dense (Swallow 8B) が将来候補
```

- narrative の外部 (Opus/Sonnet) は**数字が出るまで段階的に維持** (完全ローカル化は
  品質差を測ってから)
- 鮮度は学習に持たせない (actor 辞書 / MITRE sync / rubric-in-DB が担う)
- 契約違反は RLVR の小規模 PoC で潰す (SFT 完了後)

## 4. 基盤は 26B に固定しない — 実測 A/B で選ぶ

パイプライン自動化により**基盤 1 候補の検証 ≈ SFT 2-4h + 評価 1.5h**。教師対 2,273 と
凍結評価 3 セットはモデル非依存。詳細は narrative_base_candidates.md:

1. **GPT-OSS-Swallow 20B** (MoE A3.6B・JP 特化・Apache 2.0) — 正面のライバル。
   検証点 = harmony 形式×schema 強制 / mlx-lm LoRA / fuse→GGUF
2. **CALM3-22B** (dense) — 反復の速さを取りに行く選択。速度実測 (1h) で足切り
3. Swallow 8B — 構造化族の第一候補 (narrative には容量リスク)
4. 31B dense — 速度で失格。夜間の教師・審判役として残す

## 5. 判定待ちの分岐 (2026-09-05 夜間 自動実行中)

```
v3 の 3 評価 → 31B の事象ニュース 39 件 → spotlight 3 PIR (v1 vs 31B)
 ├─ v3 が narrative 回復 → 単一モデル路線が残る (運用最簡)。RLVR PoC 並走
 ├─ v1 ≥ 31B (事象+spotlight) → 「軽量で全 narrative」成立、31B 退役へ
 └─ どちらも否 → 2 族分割で S を新規学習 (~2h) + 基盤 A/B (GPT-OSS-Swallow) へ
```

## 6. 確定事項 (再検討しない、理由つき)

- CPT 見送り (コーパス一桁不足) / 教師は混ぜない (平均を学ぶ) /
  移行判断は同一物差しの実測でのみ / rationale は学習時のみの補助信号として利用可
  (推論時スキーマは不変) / どの構成でも Step 単位のモデル割当 (Tier の「割当の単位」化) が
  共通工事


## 7. 全 LLM 呼出の事後方針 (2026-09-05 確定・残り 3 群を含む)

判定基準は 5 つ: **族 (出力様式) / 呼出量 (特化が割に合うか) / リスクと人手の関与 /
測定可能性 (評価セットを作れるか) / プロンプト-重み緊張** (運用で動く基準は重みに焼かない
— 2026-08-16 の設計原則)。

| 呼出 | 事後方針 | 根拠 |
|---|---|---|
| article_summary / triage | **S 第 1 陣 (学習中)** | 教師・評価とも整備済み |
| pir_llm_judge / event_kind / pair_judge / dedup_judge | **S 第 2 陣** — S 初版の実証後に教師生成 (半日級) | 同じ「短い構造化判定」型。既存ラベルが部分的にある |
| judgment_classifier / ioc_verifier | S 第 2-3 陣 | judgment は評価ハーネス既存 |
| **synthesis_detect** | **S 第 3 陣** — 台帳供給の Recall 感度が高く、専用評価セットを先に作ってから | 大量入力の判定 = S 型だが、見逃しの代償が大きい。呼出は日数回で急がない |
| **deep_dive 選抜** | ステップ分割後に S 第 3 陣 | 現行 Step が 2 族同居 (§区分の欠陥) |
| deep_dive 生成 / spotlight / synthesis_narrative / ledger_review | **N の拡張課題** — 転移で足りるかを先に測り、不足時のみ教師生成 | v1 の spotlight 正転移が根拠 |
| **article_translate** | **当面 base 26B 維持。S に触らせない** | 2026-07-25 に haiku vs 26B の品質比較で現行を確定済 (測定済みの意思決定を尊重)。忠実変換族で S の簡潔化バイアスは切り詰めリスク。**測定された品質問題が出るまで投資しない**。将来は entity 保存・長さ比の決定論検査 → RLVR 適用余地のメモのみ残す |
| **actor_sync** | **最低優先・base 維持** | 週次・低量・**人手承認キューが安全網として既に存在**。翻訳側の解が出たら継承 |
| **assistant_chat** | **特化しない (外部 haiku / base 維持)** | 自由対話は特化 SFT が最も弱い型。対話は human-in-the-loop で誤りが可視・低リスク。**UI と共に変わる課題で、重みに焼くとプロンプト調整が死ぬ** (設計原則の「運用で動く基準」側) |
| pir_compile / selector_proposal | 特化しない | 低頻度 + 人手レビュー前提。同上の緊張 |
| precise_search (planner/reranker) | 保留 — 構造化判定なので S 候補ではあるが、量・コスト圧が無い | 圧が出たときに第 3 陣へ |

原則: **「特化しない」も測定に基づく決定にする** — translate は比較済み、dialog 系は
課題型 + 人手関与 + プロンプト可変性の 3 点で除外。逆に S へ入れるものは全て
「教師対 → 凍結評価 → 再学習 → 前後比較」の同一関門を通す。


## 8. 夜間測定の結果 (2026-09-06 02:30 確定) — 2 族分割の実証

### N (v1 = kuebiko-sft:26b) vs 31B — 両関門通過

- **事象ニュース 39 件 (対応符号検定)**: caveats 1.54 vs 1.03 (19勝2敗 p<0.001)、
  discrepancies 0.97 vs 0.62 (15勝3敗 p=0.008)、他 3 指標は同等。機械検査は両者
  クリーン (重複 0 / placeholder 0)、引用メタデータの壊れ率も同等。31B 実測 143 秒/件。
- **spotlight 2 PIR 精読 + 全固有名詞の DB 突合**: 品質同等 + v1 は単一 SNS 源の主張に
  「未確認情報・確度は低い」と自発的な但し書き (31B は無条件提示)。速度 3.8-3.9 倍。
- **v1 の既知の瑕疵 = 固有名詞の低頻度破損**: "Nanjing Xinjiuwei" → "Xinjirazu"、
  ".hl.cn" → ".hl.conn" (2 回中 1 回)。**spotlight には識別子関門が無い** —
  narrative 全面移行の条件として関門延長が必要 (RLVR の報酬候補でもある)。

### S (s1、事象ニュース抜き 1,479 対・700 iters) — 混合モデルと同水準

| | 現行 26B | v2 (混合) | v3 (2:1:1) | **s1 (S 単独)** |
|---|---|---|---|---|
| triage Sonnet 一致 (150 件) | 55.3% | 70.7% | 67.3% | **68.0%** (vs v2 McNemar p=0.219 = 差なし) |
| 寛容バイアス | +0.413 | — | — | **+0.160** |
| IOC 平均 (86 件) | 2.70 | 4.45 | 4.48 | **4.42** |
| mitre 重複 / placeholder | — | 0 / 1 | 0 / 4 | **0 / 2** |

**結論: 分割仮説は成立**。S は narrative データ無しで構造化 2 課題の水準を維持し、
N は narrative 特化のまま希釈されない。v2/v3 の干渉問題は分割で回避された。

- s1 の判定で注目: 実悪用中 0day (SonicWall SMA1000 / PaperCut) を Sonnet の medium に
  対し high と判定 — 「不一致」計上だが Recall 重視の任務ドクトリンには整合。
- 3 アーム共通の残課題: トクリュウ遠隔解析 (high→low、Sonnet=medium) と三方面ジレンマ
  (3 者不一致)。→low 方針は利用者判断待ち。

### 次の実装 (利用者承認後)

1. narrative ティア → kuebiko-sft:26b (31B 退役)。条件: 識別子関門の spotlight 延長
2. step 単位割当の実装 → triage / article_summary のみ kuebiko-sft:s1 へ
3. S 第 2 陣 (pir_llm_judge / event_kind / pair_judge / dedup_judge) の教師生成


## 9. 配備の最終形 (2026-09-06 07:10 稼働中)

利用者の運用方針: **v1 は Sonnet 主腕の fallback の立場。十分な精度が出たら v1 単独へ。**
(まだ SFT のみ — DPO/RLVR は未実施。単独切替の判定は凍結評価 + 対応検定で行う)

| step / 腕 | 割当 | 変更 |
|---|---|---|
| triage / article_summary | **kuebiko-sft:s1** (主腕・ローカル) | 26B base → s1。初本番 (07:00 rss) 成功・出力健全 |
| event_news / pir_spotlight / synthesis_narrative | claudecode:sonnet (主腕・従来どおり) | 変更なし (蛇口 = 教師供給も継続) |
| **narrative の fallback** | **kuebiko-sft:26b (v1)** | 31B → v1 (`fallback:narrative` キー、外部拒否/cooldown 時に発動 ~12%) |
| reasoning の fallback | gemma4:31b | 変更なし (ACH は v1 の守備範囲外) |

機構: `step:<step名>` (主腕の step 単位上書き) + `fallback:<tier名>` (外部主腕のローカル
受け皿、ローカル限定) — いずれも model_tiers config doc の予約キー。spotlight には
識別子関門を敷設済み (SPOTLIGHT_IDENTIFIER_GATE=0 で rollback)。

31B の残る役割 = reasoning fallback + BUILTIN fail-safe (退役は v1 が ACH 相当まで
届くか、reasoning も外部単独で賄うと決めた時)。

v1 単独への関門 (§7 の次段): ①IPO/DPO (蛇口の rejected 草稿 + 教師 chosen) →
②RLVR PoC (識別子/引用/schema 関門 = 報酬)。判定基準は「Sonnet 主腕との一致 +
関門通過率」を凍結セットで。

**s1 も SFT のみ** (2026-09-06 利用者確認)。s1 が主腕なのは置換相手がローカル 26B base
だから (外部品質を失っていない) であって、完成ではない。s1 の post-training 道筋:
- IPO/DPO: 同一入力の Opus 教師 (chosen) × base/s1 (rejected) 乖離ペア — 教師 802 対と
  評価 run が既に対応済み。triage は Sonnet/Opus と割れた事例がペア候補
- RLVR: 抽出系のみ (schema 妥当性 / placeholder・重複ゲート / victim_orgs 接地検査が
  報酬)。triage は正解の決定論検証ができず RLVR 不向き → DPO 側で扱う
- 判定は同じ凍結 goldset (86/150) + 対応検定


## 10. 呼称規約 (2026-09-06 制定)

**narrative 族 = N / 構造化族 = S** と呼ぶ。世代は番号 (N1, S1, N2, ...)。

| 呼称 | 実体 (Ollama tag) | 旧称 |
|---|---|---|
| **N1** | kuebiko-sft:26b | v1 (narrative 特化 SFT 初版) |
| **S1** | kuebiko-sft:s1 | s1 (構造化族 SFT 初版) |
| (退役実験) | kuebiko-sft:v2 / :v2trial / :v3 | v2 (33:33:33 混合) / v3 (2:1:1) — 干渉の実証記録 |

- 学習手法の進行は世代番号で表す: N1/S1 = SFT のみ → N2/S2 = +IPO/DPO → 以降 RLVR。
- Ollama tag の N/S 揃え (kuebiko-n:1 / kuebiko-s:1 等) は**次の再学習時に**行う
  (稼働中の割当 config を呼称のためだけに触らない)。
- 以前の文書・メモリの「v1」「モデル N/S」表記は本表で読み替える。


## 11. N2/S2 (IPO/DPO) の着手記録 (2026-09-06)

### 実現可能性の関門: IPO × MoE 26B = **可** (疎通 PoC 済)

- mlx-lm-lora 3.1.2 (`--no-deps` 導入 — venv の gemma4_text.py stop_gradient パッチを保全)。
  `--train-mode dpo --dpo-cpo-loss-type ipo` が存在し、データ形式は
  `{"prompt","chosen","rejected"}` jsonl (chat template 自動適用)。
- smoke (6 対・triage 実プロンプト): **loss 0.693→0.06 / accuracy 1.0 / margin 2.8、
  adapters 保存成功**。MoE router VJP 問題なし・NaN なし。peak 44.6〜49.3GB。
- ⚠ **OOM は非決定的に発生し、正体は Ollama との GPU 共有圧** (iters 3/7=成功、6/8=失敗、
  失敗時は Ollama が 22GB 保持中)。epoch 境界説は 6/7 の反例で棄却。
  → 運用則: **本学習は Ollama 静穏時 (夜間) に単独で回す** (SFT と同じ)。
  長系列対では peak が上がるため、対の token 長に上限を敷く (組み立て時に実測して決める)。

### 材料 (蛇口だけでは足りない)

- 蛇口 (event_draft_rejects) = 3 日で 26 件 → 主材料にならない。補助扱い。
- 主材料 = **学生の実出力を rejected 側に使う乖離ペア** (on-policy に近い):
  `scripts/build_dpo_student_outputs.py` が教師プロンプトに対する S1/N1 出力を生成
  (本番同一条件: schema 制約・think=False・temp 0.2)。S1-triage 1013 / S1-summary 802 /
  N1-event 721 の 3 系列。ペア化 (乖離の判定基準) は学生出力が出揃ってから実データで決める。

### 主腕/fallback の学習カバレッジ規律 (利用者指摘 2026-09-06)

主腕は「学習+測定済み step だけ」(S1=2 step のみ、他は base 継続)。
ティア単位 fallback の穴 = **ledger_deep_review は N1 未学習の構造化出力** (縮退様式に
該当しうる) → **probe で測定済み・縮退なし** (実 Situation 3 件 × N1/31B):
- N1 は全欄充足・重複 0・placeholder 0・証拠 article_id 実形式 100%・仮説 verdict 分布が
  31B と完全一致 (leading 1 / viable 0-1 / refuted 5-6)。抜粋もむしろ長い。9 倍速。
- 観察項目: N1 の llm_confidence が 3 件とも moderate に平坦化 (31B は low/high/high)。
  n=3 で断定不可 + 本番は ACH 整合 seam が verdict を導出するため実害は限定。
  N2 の学習に ACH 課題を足すか判断する際の材料。
→ **fallback 席の学習カバレッジ問題はクローズ** (全 narrative step が測定済みになった)。


## 12. 系譜の再編成 — 拡張 SFT を IPO の前に置く (2026-09-06 利用者決定)

利用者の問い「未学習 step の教師データ SFT で精度向上があるのでは」→ ある。順序は
**「N1.5/S1.5 (task 拡張 SFT) → その上に N2/S2 (IPO)」の 1 本系譜**に再編成
(preference は SFT の上に載せるのが標準順)。走行中の学生出力生成 + 近日の IPO は
レシピ確定用 (学習率・ペア閾値・token 上限) として活かす。

### 教師データ収穫 (稼働中、Opus・リミット自動リトライ付き)

| task | 方式 | 量の目安 | train/eval 分離 |
|---|---|---|---|
| spotlight | 本番 generate_spotlight を過去日付駆動 + RecordingClient 捕獲 | ~150-200 対 (20 PIR × stride 3 日) | 直近 3 日を評価予約 |
| synthesis | 同方式 (足場 detect/ACH はローカル = 外部消費を narrative 段に限定) | ~19 窓 | 同上 |
| pair_judge | event_pair_shadow のラベル期 (09-01) 以降のみ | 700 | 時間分離 |
| event_kind | 60 日窓の記事 − ラベル 455 id | 600 | ラベル id 除外 |
| pir_judge | 本番選抜 (_load_posted_rows + state 空) ラウンドロビン | 800 | 直近 3 日予約 |

- **dedup_judge は第 3 陣へ**: 本番の候補対がどこにも記録されず忠実サンプリング不能。
  先に候補の shadow 記録を敷く (pair_shadow と同型)。
- spotlight 収穫は識別子関門 OFF (書き直し指示がプロンプト形を汚すため)。識別子検査は
  **組み立て時のオフライン関門**に移設 (教師出力にも適用)。
- 収穫スクリプト 3 本 = build_sft_teacher_{spotlight,synthesis,s2}.py (5c42019d)。


### IPO の rejected 側は「N1.5/S1.5 学習後に全カテゴリ再生成」(2026-09-06 利用者質疑で確定)

新カテゴリ (pair_judge/event_kind/pir_judge/spotlight/synthesis) の学生出力を**拡張 SFT 前の
モデルで作らない** — 未学習課題の出力は自明に悪く「明白マージンペア」になる (DPO を
過学習させる型 / 拡張 SFT 後のモデルはもう出さない誤り = 矯正対象として無意味)。
本走の IPO では**旧課題も含む全カテゴリ**を N1.5/S1.5 から再生成する (on-policy)。
いま走行中の S1/N1 学生出力はレシピ確定用。生成スクリプトは全 6 task 対応に拡張済み
(spotlight/synthesis の学生生成は教師ファイルのプロンプト+捕獲形式を確認してから追加)。


### spotlight 教師の規模と **長系列問題** (2026-09-06 実測)

- 規模: 1 日付あたり 20 PIR → **採用 17 + skip 3** (skip = 候補 5 件未満の材料不足)。
  1 run = 11 日付 (days 36 / stride 3 / 予約 3) = 220 枠 ≈ 187 対。
  ⚠ **日付グリッドは "now" 相対**なので、再起動すると 1 日ずれた別グリッドになり
  resume が効かない (実際に 2 グリッドが交互に埋まった)。結果は増量なので害はないが、
  「再開 = 同じ枠の続き」ではないことを前提にする。
- ⚠⚠ **token 長: 中央 14,570 / p90 16,228 / 最大 17,346**。既存 SFT 組み立ての除外閾値
  12,000 では **81% が捨てられる**。spotlight の本番プロンプトは候補 30 記事 × 要約
  300 字を含むため、これは構造的な長さであって外れ値ではない。
  → **N1.5 は max-seq-length 18000 で学習する**。実測 (実物 6 対・num-layers 8・
  grad-checkpoint): rc=0・NaN なし・**peak 72.1GB**・0.135 it/s (700 iters ≈ 86 分)。
  128GB 機なので単独なら可。**Ollama が 16-22GB 保持したままだと危険** → 学習前に
  ollama のモデルを落とす (`ollama stop` / 静穏帯)。
  組み立て側の除外閾値も N 族は 17,500 に上げる (S 族は従来どおり 12,000 で足りる)。


## 13. ⚠⚠ synthesis は narrative ティアを使っていない (2026-09-06 判明・要是正)

教師収穫が **19 窓すべてで 0 件**だったことから発覚。grounded モード (本番既定
`SYNTHESIS_GROUNDED=1`) では:

- `pipeline.generate_grounded_synthesis` は `ach_llm = analysis_llm or llm` を作り、
  **`build_estimate(llm=ach_llm)` も `render_record(llm=ach_llm)` も ach_llm を使う**。
- したがって引数 `llm` (= `Step.SYNTHESIS_NARRATIVE` で解決されるクライアント) は
  **一度も呼ばれない**。散文 (状況総括の各節) を書いているのは **reasoning ティア**。

### 二つの帰結

1. **2026-09-06 朝の「synthesis narrative 転移測定 (N1 vs 31B)」は無効**。両アームとも
   測定対象モデルを未使用の `llm` に渡しており、実際の生成は両方とも
   `build_llm_for(SYNTHESIS_ANALYSIS)` = claudecode:sonnet だった。出力差は Sonnet の
   run 間非決定性にすぎない。**「N1 は synthesis でも fallback 資格あり」という結論は撤回する**
   (再測定は analysis 側を差し替えて行う)。
2. **ティア乖離の再発** (2026-09-04 監査と同型)。UI の narrative ティア割当は synthesis に
   効かず、reasoning ティアが実効値。`fallback:narrative` も synthesis には効かない
   (event_news / spotlight には効く)。是正案は「render 段だけ narrative ティアで呼ぶ」
   (設計意図どおり) が本筋 — ただし本番挙動が変わるので別途 A/B が要る。

### 収穫スクリプト側の対処 (実施済)

`SelectiveTeacherClient` を導入し、**プロンプトが render.j2 由来のときだけ教師 (外部)**、
他はローカルへ振り分けて捕獲する (ACH 7-8 呼出/窓を外部に投げない)。捕獲 0 件は rc=1。
