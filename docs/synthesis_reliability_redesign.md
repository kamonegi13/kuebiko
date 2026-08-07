# Synthesis 信頼性 再設計 — 証拠接地 → ACH → 敵対的検証 → 較正 estimate → narrative 射影

> 2026-06-29 設計。synthesis (status_synthesis / 朝刊夕刊の上段ナラティブ) の信頼性問題への
> 本質的・根本的対処。物差しは **ICD 203 Analytic Standards** + **構造化分析技法 (ACH)**。
> 既存 [synthesis_assessment_architecture.md](synthesis_assessment_architecture.md) の状態中心
> アーキテクチャ (canonical estimate, 報告は射影) の正しい延長。

---

## 1. 根本原因 (確定)

synthesis は **1 回の LLM 生成 (31B, temp 0.25, 12k tokens) で見出し+全 narrative+全 tradecraft
(対立仮説含む) を一括生成**する。LLM が見るのは **記事タイトル (80字切詰) + 出典名 + 過剰帰属済み
タグ (intent/actor/importance) + 集計数だけ**。**本文 (DB の `body` 列に保存済) も要約も読んでいない**。

ICD 203 の中核を構造的に破る:
- **#4 全ソース依拠**: 本文を持つのに読まず、タイトル+タグで物語化。
- **#5 不確実性の適切表現**: 確度が物語の流暢さ由来。`source_basis` の確度は算出済だが**判定に未使用 (表示専用)**。
- **#6 "情報"と"前提"の分離**: 「意図的攻撃」前提が評価に焼き込まれ、観測・推論・前提が融合。
- **#7 代替仮説の正規評価**: 対立仮説が**本評価と同一パスの自作ワラ人形**。
- **#9 出典紐付け**: 主張が本文記述に束縛されず、`axes_evidence` の article_id も内容照合なし。

**上流の汚染源 (Locus A)**: 記事 intent は要約プロンプトが「espionage = 国家系 APT の主目的」と定義し、
エイリアスが APT/“intelligence” 言及を機械的に espionage へ寄せる (コモディティ vs 標的型の弁別子なし)。
actor も「名前が出れば付与」(「Mustang Panda が過去に使った同種」でも付く)。

**最深の一文**: synthesis は「過剰帰属タグとタイトルから、証拠そのものに触れずに一貫した脅威の物語を
一括生成し、書いた評価をソースで検証する機構がない」。だから一貫だが証拠の薄い評価が確信的に出る。

---

## 2. 設計原則 (背骨)

0. **対称な客観性 (ICD 203 #1, 最上位)**: 目的は「脅威過大バイアスの是正」であって「穏当側へ寄せる」
   ことではない。システムは**両方向の結論に等しく抵抗し、証拠だけで動く**。脅威 deflation も
   inflation と同じく禁忌。**確度 cap は方向中立 (弱い証拠ならどの結論でも抑える)、ガードレールは
   仮説を禁止せず過確信のみ防ぐ、敵対的パスは leading が何であれ対称に反証する**。最終的な客観性
   保証は「機械が客観」ではなく**推論の可視化 (証拠+ACH 行列) で人間が検める**こと (§10)。
1. **物語の一貫性は証拠ではない** (coherence ≠ evidence)。一貫性最大化をやめる。
2. **確度は証拠強度から導く** (出典 tier × 裏取り × ACH 反証余地)。物語の流暢さからではない。
3. **評価は反証で駆動** (ACH: 仮説は確証できず、棄却に失敗するだけ。反証最少の仮説が勝つ)。
4. **証拠 → 判定 → narrative の順** (現状の逆)。narrative は較正済み estimate の射影。
5. **段階・flag・shadow**: 各段は単独出荷可。新パイプラインは `SYNTHESIS_GROUNDED` flag 裏で構築し、
   旧 single-pass を fallback に残し、shadow 比較で品質検証してから flip (ROUTING_RULES_ENGINE と同型)。

---

## 3. 目標パイプライン (6 段)

`src/synthesis/grounded/` 新設 (旧 generator.py は flag off の fallback として温存)。

### 段0 — Claim nomination (集約から、安価)
- 入力: 既存集約 (high-importance articles の title+tags+tier、axis/nation/trend 集計)。
- LLM (31B) が**期間の key judgment 候補**を K 件ノミネート。各 claim に **支持 article_id** を付ける。
- K は期間でスケール: **daily K=3 / weekly K=6 / monthly K=8** (state-centric の「期間=render 軸」に整合)。
- 出力: `[{claim, domain(axis), supporting_article_ids}]`。

### 段1 — Evidence grounding (本文を読む)
- 各 claim の支持 article の **body を fetch** (`get_article_body`、truncate ~4k char/記事)。
- LLM (31B) が**構造化 evidence ledger**を抽出。各証拠に **attribution_basis** を型付け:
  `vendor_confirmed / govt_confirmed / victim_disclosed / tooling_similarity / claimed_by_actor /
  state_media_claim / researcher_assessed / unattributed / speculation`。
- **負の証拠も必須抽出**: 「防衛省は窃取・外部通信の証拠なしと発表」等の*不在*事実 (USB 事案の鍵)。
- 出典 tier・裏取り数は `source_basis` から決定論的に付与。
- 出力: per-claim `[EvidenceItem]`。本文抜粋 + article_id 紐付け (traceable)。

### 段2 — ACH (証拠 → 仮説採点)
- **固定の仮説メニュー** (config) から関連仮説を立て、各証拠 × 各仮説で整合/反整合を採点。
- 判定 = **反整合 (disconfirmation) が最少**の仮説。confidence は反証余地 + 段5 の cap。
- 段1+2 は**1 claim 1 LLM 呼出に統合**可 (本文を読み ledger と ACH 行列を同一スキーマで出力)。
- 出力: per-claim `[HypothesisScore]` + leading_hypothesis。

### 段3 — Adversarial verification (独立パス・対称)
- **別ロール (red-team) の LLM** が ledger + leading_hypothesis のみを受け (主見立ての論拠は渡さない)、
  **leading が何であれ** (穏当でも脅威でも) それを反証し、最有力の対立仮説を立てる。
  leading=「偶発」なら「実は組織的では?」を、leading=「組織的」なら「実はコモディティ/偶発では?」を
  **対称に**突く (穏当側への誘導をしない = 原則0)。
- 反証が証拠上成立すれば **confidence 降格 / leading 反転** (方向は問わない)。
- 期間内全 judgment を **1 バッチ呼出**で red-team (Ollama 直列を考慮)。
- 出力: per-judgment `{refuted: bool, strongest_counter, conf_adjust}`。

### 段4 — Estimate 組立 (決定論・LLM なし)
- 段0-3 を **canonical Estimate** (state object) に構造化。confidence は **min(ACH 由来, source_basis cap)**。
- これが「状態」。報告は以後その射影。

### 段5 — Narrative 射影 (render)
- LLM (31B) が **Estimate を忠実にレンダー** (headline/各 section/tradecraft)。
- **制約**: Estimate の confidence を超える主張禁止。tradecraft.alternatives = **ACH の実競合仮説** (反証付き)。
  key_assumptions/indicators/missing = Estimate 由来。source_caveat = 出典 tier 由来。
- 出力: 既存 `StatusSynthesisRecord` (後方互換) + `estimate` JSONB (canonical)。

---

## 4. Estimate スキーマ (`src/synthesis/grounded/estimate.py`)

```python
AttributionBasis = Literal[
    "govt_confirmed", "vendor_confirmed", "victim_disclosed", "researcher_assessed",
    "tooling_similarity", "claimed_by_actor", "state_media_claim", "unattributed", "speculation",
]
Confidence = Literal["high", "moderate", "low"]

@dataclass(frozen=True)
class EvidenceItem:
    article_id: str
    source_tier: str            # classify_source_tier
    attribution_basis: AttributionBasis
    excerpt: str                # body からの根拠抜粋
    polarity: Literal["supports", "contradicts", "neutral"]  # leading 仮説に対し

@dataclass(frozen=True)
class HypothesisScore:
    hypothesis: str             # config メニューの id
    consistent: int
    inconsistent: int           # ACH 反証カウント (判定の主軸)
    verdict: Literal["leading", "viable", "refuted"]

@dataclass(frozen=True)
class KeyJudgment:
    id: str
    claim: str
    domain: str
    leading_hypothesis: str
    confidence: Confidence
    confidence_basis: str       # cap 理由 (source_basis)
    hypotheses: tuple[HypothesisScore, ...]
    evidence: tuple[EvidenceItem, ...]
    key_assumptions: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    indicators: tuple[str, ...]
    adversarial_refuted: bool
    adversarial_note: str

@dataclass(frozen=True)
class Estimate:
    period_type: str
    period_start: datetime
    period_end: datetime
    judgments: tuple[KeyJudgment, ...]
    model: str
```

---

## 5. 仮説メニュー (`src/synthesis/grounded/hypotheses.py`, コード SSoT)

サイバー事案/地政学事案の**帰属・性質**に関する canonical 仮説 (LLM 自作ワラ人形でなく固定語彙)。
**固定の分析フレームワークゆえコード SSoT** (SocioPoliticalIntent enum / PROPERTY_CATALOG と同思想)。
頻繁な運用調整でなく分析規範レベルなので config 化しない (変更はレビュー付きコード変更)。

```
organized_state_op       # 国家指示の標的型作戦
opportunistic_commodity  # コモディティ malware/tooling の日和見展開 (非標的)
criminal_financial       # 金銭目的の犯罪
accidental_negligence    # 誤設定/供給網汚染/過失が原因の感染
hacktivism_influence     # 主義主張・影響工作
reporting_artifact       # 旧事案の表面化/再報道 (新規活動でない)
unverified_or_false      # 未実証/国営 framing/噂
```

各 id に説明 + 「この仮説を支持/反証する典型証拠」を付け、段1+2 のプロンプトに注入。
帰属を主張する {organized_state_op, criminal_financial, hacktivism_influence} は
ATTRIBUTION_SENSITIVE = 強い帰属根拠が無ければ確度上限 low (方向中立、§6)。

---

## 6. 確度の決定論的 cap (核心ガードレール)

**confidence = min(LLM が ACH から導いた確度, 方向中立な 2 つの上限)**。`final_confidence()` 純関数。

(a) **source_basis 上限** (`source_basis.confidence` 既存・未使用を昇格):
- single-source social/state_media → 上限 **low**。official/research × 裏取り ≥2 → 上限 high 可。
- 「弱いソースに高確度」を構造的に封じる (ICD 203 #5)。

(b) **帰属上限** (`attribution_confidence_cap()`):
- leading が帰属を主張する仮説 (organized_state_op) なのに**強い帰属根拠** (govt/vendor/researcher/
  victim_disclosed の supports) が無く `tooling_similarity / unattributed` 等のみ → 確度上限 **low**。
- **重要 (原則0 = 対称性)**: これは**仮説を禁止しない**。leading 選択は ACH (証拠駆動) に委ね、
  organized は証拠が示せば leading になり得る。上限が抑えるのは**過確信のみ**。
  「国家が/特定アクターが やった」という帰属主張に見合う帰属証拠を求める evidence-strength 規律で、
  穏当側の結論にも同じ source_basis 上限が等しく掛かる (方向中立)。
- = USB 事案の「Mustang Panda と同種ツール → espionage 確定 (高確度)」型の**過確信**を構造的に止める
  (organized 自体は禁止せず、tooling 類似だけなら低確度に留める)。

---

## 7. モデル / パス数 / 並列 / コスト

- モデル: 段0/1/2/3/5 とも **31B Dense (OLLAMA_SYNTHESIS_MODEL)** (推論重視)。段1 抽出のみ 26B 可。
- パス数 (K=key judgment 数): `1(nominate) + K(ground+ACH 統合) + 1(adversarial batch) + 1(render)` = **K+3 呼出**。
- Ollama 単一モデルは推論を直列化するため per-judgment は実質直列。K=6 で ~9 呼出 × ~90s ≒ **~13 分**。
- 予算: daily synthesis は 07:30 + 朝刊(06:30) で時間制約あり → **daily は K=3 で ~6 分**。weekly/monthly は K 大。
  PIPELINE_TIMEOUT 1800s 内。LLM 呼出 timeout は各 900s 内。
- ローカル Ollama ゆえ金銭コストなし (時間のみ)。

---

## 8. 後方互換・段階・flag

- env flag **`SYNTHESIS_GROUNDED`** (既定 0 = 現行 single-pass)。1 = 新パイプライン。
  algorithm flag ゆえ env が適切 (ROUTING_RULES_ENGINE と同型・deploy-dark)。docker-compose に plumbing。
- **shadow 比較**: flag off でも新パイプラインを**生成だけして estimate を保存・比較ログ**するモード
  (`SYNTHESIS_GROUNDED=shadow`) を設け、数期間 旧 narrative と新 estimate を並べて品質検証 → 確信後 flip。
- 旧 `generator.py` は温存。`StatusSynthesisRecord` 出力スキーマ不変 (sections/tradecraft) で後方互換。
  追加: `status_synthesis.estimate JSONB` (canonical 状態)。

---

## 9. 上流 (Locus A) の確度化 — 汚染遮断

- `prompts/summarizer.j2` の intent 指示を改訂: **espionage は「標的型の諜報意図の証拠」を要し、
  単に APT ツールが言及される/類似するだけでは付与しない**。根拠が commodity/tool-similarity/unattributed
  なら intent= opportunistic 相当 or unknown + **低 confidence**。
- 記事に `intent_confidence` + `attribution_basis` を保存。段1 grounding がこれを参照。
- actor 帰属に `attribution_basis` (conducted vs tooling-referenced) を区別 (別段で漸進)。

---

## 10. UI トレーサビリティ (「tradecraft vs 本文」検証の実現)

- Synthesis タブに **judgment ごとの ACH 行列 + 証拠 (article_id + 本文抜粋 + attribution_basis + tier)** を表示。
- 各 judgment の confidence と cap 理由、adversarial の反証を明示。
- アナリストが「主張 → どの本文のどの記述に依拠するか」を 1 クリックで照合可能に (= ユーザーの要求)。

---

## 11. テスト計画

- 純関数: `cap_confidence` (source_basis × LLM 確度 → cap)、attribution_basis → 許容仮説の制約、
  Estimate → StatusSynthesisRecord 射影 (tradecraft 写像)。
- パーサ: 各 LLM パスの JSON → dataclass (不正/欠落の頑健性)。
- ゴールデン: USB 事案を固定入力にした **回帰テスト** (organized_state_op が leading に選ばれないこと、
  confidence ≤ low、missing_evidence に「窃取証拠なし」)。
- shadow 比較ログの検証スクリプト。

---

## 12. USB 事案 worked example (受入基準)

- 段0: claim「自衛隊網への USB マルウェア混入」+ 支持 article 3 件。
- 段1: 証拠 = 偽造コモディティ USB/EC 流通 (basis=victim_disclosed+researcher_assessed)、原因=調達/運用不備
  (govt/報道)、Mustang Panda は **tooling_similarity のみ**、**窃取・外部通信の証拠なし (負の証拠)**。
- 段2 ACH: organized_state_op は「窃取証拠なし」「コモディティ広域流通」で**反整合多**、
  opportunistic_commodity / accidental_negligence が反証最少。
- 段3 red-team: organized 仮説は tooling_similarity に依存しすぎと反証 → 維持。
- 段4: leading = opportunistic_commodity/accidental、**confidence=low** (tooling_similarity cap)、
  missing=「窃取・C2 通信の証拠」。
- 段5: narrative は「組織的作戦の確証はなく、偽造コモディティ USB の日和見汚染＋調達過失が最有力。
  ただし中国系ツール類似と air-gap 到達ゆえ標的型を排除はしない (低確度)」= ユーザー分析と一致。

---

## 13. 実装段階 (build order)

1. **基盤 (本書と同時)**: 仮説メニュー config + `estimate.py` スキーマ + `cap_confidence` 純関数 + テスト。
2. 段0/1/2: nominate + grounded-ACH パス (プロンプト + パーサ + 並列オーケストレーション)。
3. 段3: adversarial パス。
4. 段4/5: estimate 組立 (決定論) + narrative 射影 render。
5. flag/shadow 配線 + `estimate` JSONB 永続化 + 後方互換確認。
6. Locus A (intent/actor 確度化)。
7. UI トレーサビリティ。
8. shadow 比較 → 品質検証 → flip → deploy。

各段 ruff/mypy/test green を維持。flag off の間は本番挙動不変 (deploy-dark)。

## 14. 判定整合の不変量 (2026-07-25 ACH 整合監査で追加)

**背景**: `leading_hypothesis` (スカラ) と `hypotheses[].verdict` (マトリクス) は同じ判定の
二重表現であり、旧 `apply_adversarial` がスカラだけを flip してマトリクスを更新しなかった
ため、自己矛盾 revision が systematic に発生した (全 388 revision 中 21 件、canonical 12 件。
最悪例 = マトリクスが反証した証拠ゼロ仮説が公式見立てになる ANCHOR-CI 事案 rev13)。

**不変量と規律** (実装 SSoT: `src/synthesis/grounded/estimate.py`):

1. **verdict は常に (採点カウント, leading) からの導出値** — `derive_verdict` が唯一の導出
   実装。`reconcile_hypotheses(hypotheses, leading)` で二重表現を必ず一致させる。
   leading がマトリクスに無い場合は 0/0 行を追記 (「今回未採点のまま見立てを維持」の可視化。
   カウントは捏造しない)。
2. **adversarial flip の規律** (`adversary.apply_adversarial`): 証拠採点で反証済み
   (反整合 > 整合) の仮説への flip は見送る — ACH の算術 (leading = 最少反整合) に反する
   主説交代を red-team の一存で許さない。反証の効果は確度降格 + 対立論拠の記録 (方向中立)。
   flip 成立時はマトリクスを新 leading で導出し直す。
3. **書込境界の最終ガード** (`ledger._revision_from_judgment`): 台帳書込の唯一の seam で
   reconcile を強制。是正が発生したら `revision_coherence_corrected` を warn (上流バグの可視化)。
4. **射影の現在地 overlay** (`v1.py /intel-graph/synthesis`): tradecraft スナップショットは
   生成時点で凍結されるため、各判定に台帳最新 revision の現在値 (`ledger_now`) を併記する。
   記録 (スナップショット) は改変しない — UI は「生成時点」と「台帳の現在値」を併記する。

既存データの是正は `scripts/backfill_ach_coherence.py` (dry-run 既定、訂正 revision 追記方式
= 履歴不改変・delta_note で理由明示)。判定規律は `src/assessment/coherence.py` (pure)。
