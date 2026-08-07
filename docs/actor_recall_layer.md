# アクター・リコール層 (Actor Recall Layer)

closed-vocabulary 辞書照合の**構造的リコール欠落**を、過剰帰属を再発させずに埋めるための層。
発端: ハクティビスト (NoName057(16)/Anonymous 等) も新興アクター (Storm-####/UNC#### 等) も
辞書に未収録のため構造化アクター層 (`article_entities entity_type='actor'`) から不可視だった。

## 病巣

アクター検出は `actor_normalizer.find_all(body)` の**辞書一致のみ**。供給源は
`config/actor_aliases.yaml` (APT + 主要犯罪集団に curate)。辞書外は永遠に不可視:
- **LLM は既に `primary_actor_id` を本文から抽出している**が、辞書一致時しか意味を持たず、
  **辞書未一致 (=新顔) の信号は黙って捨てられる** (`threat_operations.py`: "まだ永続化していない")。
- MITRE 週次同期はごく一部の正式 Group だけ補完。ベンダ命名/ランサムブランドは数ヶ月先行。

## リコール欠落の3類型

| | 種類 | 例 | 対処 |
|---|---|---|---|
| **A** | 既知だが未登録 | NoName057(16) / KillNet | 辞書に seed + alias |
| **B** | 曖昧な名前 | Anonymous (≒匿名情報源) | 文脈判別マッチ (除外でなく disambiguation) |
| **C** | 新規・命名されたて | Storm-2372 / UNC5221 / 新ランサム / 自称 crew | 採取→提案→人承認→辞書化 |

## 確定原則 (再提案不可)

- **確定帰属は辞書ゲートのまま** (過剰帰属は [[data_quality_audit_2026_06_16]] で 39%→20% に削った
  当の問題。素朴な開放語彙抽出には戻さない)。
- **観測 → 候補 → 提案 → 人が承認 → 知識(辞書)** の昇格パイプライン。これは MITRE 同期 +
  `actor_update_proposals` が既にやっている型の**一般化** (MITRE 発 → コーパス発にも開く)。
- **暗定候補は「暗定・未承認」と明示して即可視** (`entity_type='actor_provisional'`)。確定 actor と
  構造的に区別 (混同しない)。リコールは即得て、確定昇格は人が律速。
- **Gap C は人承認のみ** (自動昇格しない)。却下は dedup_key で再提案されない (既存 MITRE と同型)。
- 「発見支援」撤去 ([[discovery_appraisal_removed]]) とは別物: あれは**収集の盲点** (集めていない物=
  自コーパスから不可視) の発見。本層は**既に集めたコーパス内に明示的に在る**実体の抽出 =
  「機械=検出 / 人=発見 / 機械=解決」の既存3段にアクター層が参加するだけ。
- grounded synthesis と整合: 暗定/新興アクターは attribution_basis 弱 (claimed_by_actor/unattributed)
  → [[synthesis_reliability_grounded_redesign]] が既に低確度 cap。recall↑ ∧ precision 維持。

## 実装

### Part A — ハクティビスト seed (データのみ)
`config/actor_aliases.yaml`: family `hacktivist` 新設 + 高精度・防衛関連・非曖昧な actor を seed
(NoName057(16)/KillNet/Anonymous Sudan/Cyber Army of Russia/IT Army of Ukraine 等)。
alias は数字付き/複合名のみ (誤帰属回避)。bare "Anonymous" は alias にせず Part B で扱う。

### Part B — 曖昧アクター文脈判別 (matcher)
`ActorAlias` に `ambiguous: bool` + `context_cues: tuple[str,...]` を追加。曖昧アクターは
**(名前一致) AND (ハクティビズム文脈 cue の共起)** で初めてマッチ。`anonymous` actor を
`ambiguous: true` で追加 → "anonymous source" 等の一般語誤検出を構造的に排除しつつ本物の
Anonymous 集団は拾う (偽陽性も偽陰性も減らす)。detection type 固定 / 曖昧フラグ・cue は値
([[vocabulary_expansion]] 原則)。

### Part C — 新興候補 採取→提案→承認
- **C1** `src/cti/actor_candidates.py`: `harvest_candidates(body, primary_actor_id, registry)` →
  辞書未一致の LLM primary_actor_id + ベンダ命名 regex (Storm-/UNC-/TA-/CL-XXX-/TAG-/DEV-) を
  `ActorCandidate(raw_name, key, signal, excerpt)` に。`normalize_actor_key`。
- **C2** harvest を `_build_briefing` の actor 検出段に注入 (flag `ACTOR_CANDIDATE_HARVEST`)、
  `metadata['provisional_actor_candidates']` → `_persist_article_entities` が
  `('actor_provisional', key)` で永続化 (即可視)。`propose_emerging_actors(repo, min_articles=3)`
  が N 記事裏取りの key を `actor_update_proposals(proposal_type='corpus_emerging_actor')` に投入
  (MITRE 週次 sync runner にフック)。dedup_key=key で重複/却下再提案を防止。
- **C3** `pages.py` approve に `corpus_emerging_actor` 分岐: payload の提案 actor を
  `validate_actor_edit`→`append_new_actor`→yaml write。承認時 `promote_provisional_actor(key,id)`
  が既存 `actor_provisional` entity を `actor` に backfill (歴史記事も確定帰属化)。
- **C4** News 検索 facet に `actor_provisional` を「暫定 (未承認)」ラベルで追加 (`_PIVOT_ENTITY_TYPES`
  + frontend facet)。

## フラグ / rollback
- A/B: 加法的・低リスク (A=データ、B=曖昧 actor のみ挙動変化、既存 actor は不変) → flag 無し。
- C: `ACTOR_CANDIDATE_HARVEST` (env、deploy-dark)。0 で harvest/persist を停止 (確定層は無影響)。
