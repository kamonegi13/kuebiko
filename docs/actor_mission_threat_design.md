# アクター・ミッション脅威評価 (Actor Mission Threat) 設計書

作成: 2026-07-17。前セッション合意 (C+D 方針) の細部設計。実装前の多角的再評価を含む。
**実装済 (2026-07-17)**: `src/ui/services/actor_threat.py` + doctrine 拡張 + ThreatsTab 表示。
実装時の設計不変条件 (ユーザー指示): **対日脅威をあぶり出す強制バイアスを掛けない** —
関連度の証拠は公的 doctrine と観測事実 (victim 列) のみ、自前の routing 判断
(japan_watch 配信) は証拠に使わない (§8.1)。実データ較正結果は §9。

---

## 1. 目的と定義

**アクターの脅威度を「このツールのミッション (日本の防衛 CTI) にとっての脅威」として
本質的に表す。** 一般論の危険度ランキングではなく、対日ミッション脅威である。

- **threat ≠ risk**: 本機能はアクター側 (能力・意図・活動) のみを扱う。自組織の脆弱性・
  露出 (opportunity の受け手側) は資産インベントリの領域であり対象外
  (`threat_actor_doctrine.py` 冒頭の既定原則「攻撃者情報であって被害者の脆弱性情報ではない」)。
- **辞書=knowledge / アクター=observation** (確定原則): 辞書に静的 threat_tier は書かない。
  評価は **doctrine (公知) × 観測 (コーパス) から毎回計算**する。snapshot と同様のリフレッシュ。
- 統治原則の継承: `暗域=不明≠安全` (地図/CI board v3 と同一)。観測が薄いこと・知識が
  乏しいことを「脅威が低い」と混同しない。

## 2. 前セッション引き継ぎの多角的再評価

### 2.1 維持する骨格 (妥当と確認)

| 項目 | 検証結果 |
|---|---|
| 辞書静的 tier を書かず計算する | 妥当。154 actor 辞書は編集フロー (提案/承認) と衝突するし陳腐化する |
| doctrine 射影の必須化 (罠1) | **実データで再確認**: 直近90日 JP 被害の actor 別上位10は全てランサム (Qilin 49 / LockBit3 23 / …)。国家 APT は圏外 (Volt/Salt Typhoon の JP victim 0)。観測だけでは最危険アクターが最下位になる |
| 単一スカラー拒否 (罠2) | 妥当。ただし §2.2-1 のとおり構造でさらに強化する |
| 透明ティア + 駆動要因常時併記 | 妥当。salience / board v3 と同型の決定論・定数コード所有 |
| 既存部品の再利用 (複製しない) | 妥当。ActorActivity / doctrine / japan_relevance / kev_client / is_ics_technique が揃っている |

### 2.2 修正する 5 点 (引き継ぎからの変更)

1. **Tempo をティア導出から外す** (構造による罠2回避)。
   引き継ぎは「3軸からティアを導出」としていたが、活動性が式に入る限り重み調整で
   Qilin (高テンポ犯罪) が Volt Typhoon (低テンポ国家) を埋める危険が残る。
   → **ティア = 関連度 × 能力 の2次元**で導出し、**活動性は独立の「活動状態」として
   併記**する (`Critical・休眠` という表示が可能になる)。休眠はティアを一切下げない。
2. **事前配置 (prepositioning) は Capability でなく Relevance 側**。
   事前配置は「CI に対する意図・態勢」であり技術力ではない。意図系シグナルを関連度に、
   技術シグナル (TTP 幅 / カスタムマルウェア / KEV / ICS) を能力に分離する。
   STRATEGIC_INTENTS の優先順 (事前配置=最上位) とも整合。
3. **同盟判定に `situation._nation_role` を再利用しない**。
   `_nation_role` の "allied" は「辞書にその国のアクターが居る ∧ 敵性でない」という
   **辞書存在プロキシ**であり同盟の意味を持たない (de=1件で allied、ポーランドは other)。
   → doctrine モジュールに **`ALLIED_NATIONS` を新設** (コード所有・ミッション接地)。
4. **Capability の文書化バイアス対策** (引き継ぎ未考慮)。
   MITRE TTP 幅・マルウェア数は「よく研究されたアクター」ほど大きい (研究量∝辞書充実度)。
   新興 UNC クラスタは辞書データ皆無 → 素朴に数えると「能力低」= 誤り。
   → **能力バンドに Unknown を設け、Unknown ≠ Low。能力はティアを引き上げる方向にのみ
   作用**し、データ不足でティアが下がることはない (関連度がフロアを決める)。
5. **発見したデータ欠陥 → D の前提作業に組込み**。
   - `STATE_ACTOR_SECTORS` に **dead key が存在**: `tick` / `bronze butler` は
     154 actor 辞書に不在 (grep 確認済) — board の doctrine 射影が一度も発火していない。
   - 日本標的 doctrine の要である **BlackTech (CISA/NSA/FBI/JPCERT/NISC 共同勧告
     AA23-270A の主役) も辞書に不在**。
   - → D は「機械可読化」だけでなく**辞書への欠落アクター補充 + doctrine⇔辞書の
     整合テスト**を含む (テストが無ければ同じ drift が再発する)。

### 2.3 その他の正直さ (limitation を明示して受容)

- **Tempo = 報道テンポ**である (actor 抽出は title+summary の alias 走査 = 言及ベース)。
  ベンダ報告ラッシュで膨らむ。UI 文言も「観測 (報道) テンポ」とし活動実態と混同させない。
- actor-PIR 連携 (matched_pir_ids) は現状 3 PIR しか actors を持たない → 駆動要因表示
  のみの補助シグナルに留める (負荷のかかる入力にしない)。
- spike は threat_operations 側 (`is_spike`/`spike_ratio`) を正とする (同一 snapshot 内で
  一貫)。forecast 側 (`EntityTrend.is_spike`) は駆動要因の追加表示のみ。二重実装しない。

## 3. 実装前に決める 3 点への回答

1. **単位 = kind='group' の全アクター (133 件) を対象に計算**。人工的な絞り込み
   (敵性国家+JPランサムのみ) はしない — 式が自然に序列化する (非該当は Watch/未評価に
   落ちる) し、絞ると「対象外リスト」という新たな盲点とメンテ負債を作る。
   kind='organization'/'contractor' (21 件) は**ティア非付与** (機関は脅威主体でなく
   統制主体。既存の child_groups rollup で配下の動きを見せる)。
2. **ティア粒度 = 4段 (Critical/High/Moderate/Watch) + 「未評価 (データ不足)」を
   最初から導出**。ただし weighted-sum + 閾値ではなく **バンド × 小さな規則表** で導出
   (透明・説明可能・閾値際の flapping 耐性)。raw の軸バンドと駆動要因は詳細に常時併記。
   「Low」という語は使わない (`暗域≠安全`。最下段は Watch = 監視継続)。
3. **D の持ち方 = 別集合** (`JP_TARGETING_ACTORS`)。STATE_ACTOR_SECTORS の拡張は
   「どの分野を狙うか」と「日本を狙うか」という別の問いを混ぜるため不採用。

## 4. 評価モデル

### 4.1 出力 (frozen dataclass)

```python
@dataclass(frozen=True)
class ActorThreatAssessment:
    tier: str | None            # "critical"|"high"|"moderate"|"watch"|None(未評価)
    relevance_band: int         # 0-3 (R0..R3)
    capability_band: str        # "high"|"medium"|"low"|"unknown"
    activity_state: str         # "spiking"|"active"|"quiet"|"dormant"  (ティア非影響)
    relevance_factors: tuple[str, ...]   # 駆動要因 (常時併記、透明性の担保)
    capability_factors: tuple[str, ...]
    activity_factors: tuple[str, ...]
    coverage_note: str | None   # データ不足の注記 ("辞書に MITRE データ無し" 等)
```

計算シグネチャは **knowledge × observation** を明示する:
`assess_actor(alias: ActorAlias, activity: ActorActivity, kev_cves: frozenset[str]) -> ActorThreatAssessment`

### 4.2 関連度 Relevance (R0-R3) — ユーザー案の核

引き継ぎの標的重み (自衛隊/防衛産業・政府 最高 / 日本国内 高 / 同盟国防衛・政府 中高 /
同盟国一般 中) を doctrine 射影込みでバンド化する。**上から評価し最初に該当したバンド**。

| バンド | 条件 (いずれか) | 根拠部品 |
|---|---|---|
| **R3 直接** | ① `JP_TARGETING_ACTORS` doctrine 該当 (公的勧告が日本標的を明示) | §5 の新設集合 |
| | ② 観測: victim=JP × sector∈{defense, government} の帰属記事 ≥ `JP_DEFGOV_MIN`(=1) | article_entities × victim_country_iso × victim_sector_canonical |
| | ③ 敵性国家 × 事前配置 (doctrine 分野に CI を持つ prepositioning アクター、または観測 dominant intent = prepositioning) | is_state_nation × dominant_strategic_intent × STATE_ACTOR_SECTORS |
| **R2 構造的** | ① 観測: JP 被害 (victim=JP) ≥ `JP_SUSTAINED_MIN`(=3) — Qilin 型 | japan_relevance SSoT |
| | ② 同盟国 (`ALLIED_NATIONS`) の defense/government 被害観測 | top_countries × victim_sector |
| | ③ 敵性国家 × doctrine/観測分野 ∩ {defense, government, CI 分野} | actor_target_niscs |
| | ④ 敵性国家 × dominant intent ∈ {disruption} | dominant_strategic_intent |
| **R1 周辺** | 敵性国家 (無条件ベースライン) / 同盟国一般の被害観測 / PIR actors 掲載 | is_state_nation / ALLIED_NATIONS / matched_pir_ids |
| **R0** | 上記いずれも非該当 | — |

R3-③ が罠1対策の本体: Volt Typhoon は JP victim 0 でも R3 に立つ
(CISA AA24-038A の CI 事前潜伏 doctrine。台湾有事で日本の CI が同時対象になる想定は
CI board v3 で確立済みの前提)。

### 4.3 能力 Capability (High/Medium/Low/Unknown)

辞書 (knowledge) ∪ 観測 (window) の技術シグナル 4 つの本数で決める:

- **S1 TTP 幅**: |mitre_ttps ∪ 観測 top_ttps| ≥ `CAP_TTP_BREADTH_MIN`(=15)
- **S2 カスタム装備**: |associated_malware ∪ 観測 top_malware_families| ≥ `CAP_MALWARE_MIN`(=3)
- **S3 実悪用脆弱性**: 観測 top_cves ∩ KEV ≠ ∅ (`kev_client.get_kev_cve_set`)
- **S4 ICS 能力**: 既知/観測 TTP に `is_ics_technique` 該当 (attack_catalog)

バンド: **High** = S4 該当 or シグナル ≥3 / **Medium** = 1-2 / **Low** = 0 かつ辞書に
TTP/malware データあり / **Unknown** = 辞書データ無し ∧ 観測無し。
S4 単独 High は意図的 (ICS 破壊能力は希少かつ重大 — Sandworm を正しい理由で上げる)。
**Unknown は Low と別物** (§2.2-4)。coverage_note に「辞書 MITRE データ無し (154 中 74
アクターが該当)」等を出す。

### 4.4 ティア導出規則表 (関連度 × 能力、活動性は入らない)

| | cap High | cap Medium | cap Low / Unknown |
|---|---|---|---|
| **R3** | Critical | Critical | High (+データ不足注記) |
| **R2** | intent∈{prepositioning, disruption} なら Critical、それ以外 High | High | Moderate |
| **R1** | Moderate | Moderate | Watch |
| **R0** | Watch | Watch | Watch |

- **未評価 (tier=None)**: nation 不明 ∧ doctrine 非該当 ∧ 観測が floor 未満 ∧ 辞書
  能力データ無し。「Watch (評価済みの低位)」と「未評価 (データ不足)」を区別する —
  評価情報の正直さ原則 (ICD 203 系譜) の適用。
- R2 × High の Critical 昇格を prepositioning/disruption に限る理由: 諜報専業
  (Turla/Salt Typhoon 等) を High に留め、Critical を「日本直接 or 破壊/事前配置 ×
  最高能力」に予約する。I&W 優先順 (STRATEGIC_INTENTS) と同一の序列。

### 4.5 活動状態 (併記専用、ティア非影響)

- **spiking**: `is_spike` (threat_operations) — forecast rising / `is_quiet_waking` は
  駆動要因に「再活性」として追記
- **active**: window 内記事 ≥ `ACTIVE_MIN_ARTICLES`(=3)
- **quiet**: 1-2 件 / **dormant**: 0 件

表示は `Critical・休眠` のようにティアとペアのチップ。**休眠中の R3 には駆動要因に
「休眠中だが戦略的関連度・高 (暗域≠安全)」を自動付記**する — 罠1の表示面での保証。

### 4.6 評価ウィンドウ

観測系入力は **90 日固定** (`ASSESSMENT_WINDOW_DAYS = 90`)。UI の表示窓 (7d/30d 等) に
追随させない — 窓切替でティアが flap すると「脅威度」の意味が壊れる。表示窓 ≠ 90d の
場合は評価用に 90d 集計を別途走らせる (個人運用規模で許容、既存 snapshot 関数を再利用)。

### 4.7 受け入れ基準 (実データでの較正チェック、実装時にテスト固定)

| アクター | 期待 | 導出経路 (正しい理由で正しい答えになること) |
|---|---|---|
| Volt Typhoon | **Critical・休眠** | R3-③ (state×prepositioning×CI doctrine) × cap Med+ — JP victim 0 でも最上位 |
| MirrorFace | Critical | R3-① (警察庁 2025-01 帰属公表) × cap Med+ |
| APT10 | Critical | R3-① (警察庁 JAXA 事案帰属) |
| Sandworm | Critical | R2-④ (disruption) × cap High (S4 ICS) — 対日 doctrine 無しでも破壊能力で昇格 |
| Qilin | **High・spiking** | R2-① (JP 被害 49 件) × cap Med — 高テンポでも Critical に届かない (テンポはティア外) |
| Turla | High | R2-③ × cap High、諜報専業なので Critical 非昇格 |
| 新興 UNC (辞書データ無し) | 未評価 or Watch + 注記 | Unknown≠Low の検証 |
| 米国系 actor (us) | Watch | R0/R1 に落ちる (敵性国家ベースラインなし) |

## 5. D: 日本標的 doctrine の機械可読化 (先行実装)

`src/cti/threat_actor_doctrine.py` に追加 (コード所有、STATE_ACTOR_SECTORS と同スタイル =
1 行 1 接地コメント):

```python
# 日本標的 doctrine: 公的勧告・当局帰属公表が「日本を標的」と明示するアクター。
# key は STATE_ACTOR_SECTORS 同様 canonical/主要 alias の lower。追加基準 = 公的
# 一次ソース (警察庁/NISC/JPCERT/CISA 共同勧告) が日本標的を名指すこと。ベンダ
# 報告単独では追加しない (curation を絞り R3 のインフレを防ぐ)。
JP_TARGETING_ACTORS: frozenset[str] = frozenset({
    "mirrorface", "earth kasha",  # 警察庁/NISC 2025-01 帰属公表 (政府/防衛/先端技術)
    "apt10", "menupass",          # 警察庁 2021 帰属公表 (JAXA 等 ~200 組織)
    "blacktech",                  # CISA/NSA/FBI/JPCERT/NISC 共同勧告 AA23-270A (2023-09)
    "tick", "bronze butler",      # 防衛産業標的 (JPCERT/Secureworks、実装時に一次ソース再確認)
    "lazarus",                    # 警察庁 2022-10 暗号資産注意喚起 / TraderTraitor 2024-12 日米共同
    "kimsuky",                    # JPCERT/CC 2024 注意喚起 (日本組織標的)
    "andariel",                   # (実装時に公的一次ソースを確認できた場合のみ)
})

# 同盟・同志国 (被害国重み付け用)。ミッション接地: 日米同盟 + Five Eyes + 準同盟/
# 地域パートナー。網羅リストではない (欧州 def/gov は各アクターの doctrine 分野で捕捉)。
ALLIED_NATIONS: frozenset[str] = frozenset({"us", "gb", "au", "ca", "nz", "kr", "tw", "ph"})
```

**前提作業 (このセッションで発見した欠陥の修復)**:
1. 辞書に不在の日本標的アクターを補充: **Tick (Bronze Butler)、BlackTech** —
   既存の actor_editor / 提案承認フローで追加 (alias 衝突検証を通す)。
2. **doctrine⇔辞書 整合テスト新設**: `STATE_ACTOR_SECTORS` と `JP_TARGETING_ACTORS` の
   全 key が `config/actor_aliases.yaml` (seed) のいずれかの canonical/alias に解決する
   ことを assert。現存 dead key (tick/bronze butler) はこのテストが恒久的に再発防止する。

## 6. 実装配置 (C 本体)

| 層 | 内容 |
|---|---|
| `src/cti/threat_actor_doctrine.py` | D: JP_TARGETING_ACTORS / ALLIED_NATIONS 追加 (§5) |
| `src/ui/services/actor_threat.py` (新規, <400行) | 評価ロジック本体。純関数 `assess_actor(alias, activity, kev_cves)`。定数はモジュール冒頭に UPPER_SNAKE (salience 前例に従いコード所有、config 化しない) |
| threat-operations API | snapshot 構築後に per-actor で assess し payload に `threat` を添付 (ActorActivity 自体は observation 純度を保ち変更しない)。評価は 90d 固定集計 (§4.6) |
| `frontend/.../ThreatsTab.tsx` | 一覧: ティアチップ (`Critical・休眠` 形式) + ティア順ソート/フィルタ。詳細: 「ミッション脅威評価」セクション = 2×2 位置 (関連度×能力) + 活動状態 + 駆動要因リスト + coverage_note |
| テスト | unit: バンド規則・規則表・Unknown≠Low・§4.7 受け入れ fixture / doctrine 整合テスト (§5-2) |

**やらないこと (スコープ外・確定)**:
- ティアの routing/triage への接続 (配信は PIR→importance→channel の背骨専属。
  本機能は board v3 と同じ**独立レンズ**)
- 辞書 yaml/DB への tier 永続化 (knowledge/observation 分離違反)
- LLM の使用 (全決定論)・重みの config UI 化 (salience 同様コード所有)
- v1 での tier 履歴 DB 永続化 (ティア遷移の I&W 化は観察後の拡張候補として記録のみ)

## 7. 実装順

1. **D**: 辞書補充 (Tick/BlackTech) → doctrine 集合追加 → 整合テスト (低リスク・単独価値:
   dead key 修復で board v3 の射影も直る)
2. **C**: actor_threat.py + テスト (§4.7 固定) → API 添付 → ThreatsTab 表示
3. **較正確認**: 実 snapshot 90d で全 133 group のティア分布を目視 (Critical が 10 件超
   なら R3/昇格条件を締める — 較正 knob は JP_DEFGOV_MIN / CAP_* / 規則表の3箇所のみ)

## 8. 実装記録 (2026-07-17)

### 8.1 実装時に確定した追加の設計不変条件 (ユーザー指示)

**強制バイアス禁止**: 「対日脅威をあぶり出すための下駄」を置かず、脅威をそのまま
捉えたときに必要な脅威度が出るロジックとする。具体化:
- 関連度の証拠 = **公的一次ソースの名指し (doctrine)** と **観測された被害事実
  (victim_country_iso / victim_sector_canonical)** のみ。
- **`japan_targeted_count` (victim ∪ japan_watch 配信) は証拠に使わない** —
  配信チャンネルは自前の routing 判断 (= 自分の関心) であり、関心が脅威度を
  釣り上げる自己参照ループになるため。専用の観測スライス
  `ActorActivity.victim_country_sectors` (切り詰めない country×sector 集計) を新設した。
- 対称性テスト (`TestRoutingIsNotEvidence` / `TestNonTargets` /
  `test_sandworm_critical_via_ics_and_disruption`) で固定。

### 8.2 実装中の発見と対処

- **dead key は 3 件だった**: tick / bronze butler (既知) に加え **lazarus** も辞書に
  単独名 "Lazarus" が無く不発だった (canonical は "Lazarus Group")。alias 追加で修復 —
  記事の「Lazarus」単独言及の取りこぼしも同時に直った。
- **threat_operations の抽出が ambiguous gate を通っていなかった**: actor_normalizer の
  ambiguous (文脈 cue 要求) はこのページの alias 走査に未適用で、一般語アクターが
  過剰計上され得た。`_ActorMatcher.ambiguous_cues` として移植 (Tick 追加の前提)。
- **休眠アクターの可視化**: snapshot は「観測ありのみ」だったため、`include_dormant`
  (kind=group の 0 件アクターを包含) と `fetch_actor_detail` の 0 件詳細を追加。
  これ無しでは「Critical・休眠」が一覧に存在できない。

### 8.3 配置

- 評価: `src/ui/services/actor_threat.py` (純関数 assess_actor + 90d 固定 TTL cache)
- doctrine: `src/cti/threat_actor_doctrine.py` (JP_TARGETING_ACTORS /
  PREPOSITIONING_DOCTRINE_ACTORS / ALLIED_NATIONS、接地 citation 付き)
- API: `/api/v1/intel-graph/threats` (+actor detail) が `threat` を添付
- UI: ThreatsTab — ティアチップ (Critical・休眠 形式) / 脅威度順ソート (既定) /
  詳細「ミッション脅威評価」セクション (3軸バンド + 駆動要因 + coverage note)
- テスト: `tests/unit/test_actor_threat.py` (§4.7 受け入れ基準)、doctrine⇔辞書
  整合テスト (`TestDoctrineDictionaryConsistency`)

## 9. 実データ較正 (2026-07-17、production PG 90日窓)

group 135 件: **Critical 10 / High 19 / Moderate 47 / Watch 50 / 未評価 9** — 目標 (≤10) どおり。

Critical: volt_typhoon (doctrine+観測prep×11)、mirrorface (**休眠**、警察庁帰属)、
apt10・lazarus・kimsuky・andariel (JP doctrine)、salt_typhoon (観測prep×2)、
sandworm (R2×high×disruption)、mustang_panda (**観測: JP 防衛/政府被害 2件** —
データ駆動昇格の実例)、dragonfly (観測prep×3 + ICS)。
受け入れ基準の実データ照合: Qilin=High・spiking ✓ / Turla=High ✓ / APT28=High
(諜報は Critical 非昇格) ✓ / tick・blacktech=High R3×unknown (辞書 MITRE データ待ち、
mitre-actor-sync が週次で backfill → medium 以上になれば Critical へ自然昇格予定)。
