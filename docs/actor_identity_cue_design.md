# 曖昧アクター照合の同一性 cue 再設計 (設計案)

- 起点: 2026-07-30 ユーザー報告「OctLurk 記事に tick が誤表示」→ 対症療法 (Tick の cue から汎用語を除去、
  commit a8c2e07) の後、「これは本質的解決か」の問いに答える再設計案。
- 状態: **設計レビュー待ち (未実装)**。承認後に TDD で実装する。
- 関連: docs/subject_actor_attribution_design.md (主題判定層) / docs/entity_pipeline_inventory.md §一般語衝突 /
  src/cti/generic_alias_words.py (収穫系の一般語ゲート) / memory[dedup-recycle-and-tick-false-positive]。

## 1. 問題 (実測)

`actor_normalizer` の曖昧アクター (ambiguous=true) ゲートは「名前一致 + context cue 共起」でマッチさせるが、
cue が **ジャンル語** (ransomware / malware / espionage / 諜報 / threat group …) に汚染されている。
CTI 専用コーパスではジャンル語はほぼ常に出現するため、ゲートは実質無力化し、一般語と衝突する
アクター名が **本文にその英単語が出現しただけで** 言及アクター (article_entities type=actor) に登録される。

実測 (直近 90 日、被害者レコード補正済みの同一性証拠不在率 = FP 疑い率):

| actor | n | FP疑い | 誤検出例 |
|---|---|---|---|
| play | 150 | **88%** | 広告詐欺記事 (動詞 play)・買収記事 |
| chaos | 150 | 77% | 「混乱」の意の chaos |
| deadlock | 150 | 42% | Siemens CPU 脆弱性 (技術用語 deadlock) |
| cloak | 19 | 79% | 英政治記事 (覆い隠す) |
| anonymous / axiom | 10 / 8 | 100% | 「匿名の」/ Axiom 社 |
| kairos / morpheus / everest / interlock / lynx / warlock / anubis | — | 14〜65% | エベレスト山 等 |
| gallium (対照群: 希少語) | 33 | **3%** | — |
| **合計** | **685** | **59%** | |

汚染経路は 2 つ:
1. 明示 `context_cues` にジャンル語が混入 (Tick=修正済 / polonium は 9 個残存 / chaos 等に 1 個ずつ)
2. cue 未定義の曖昧アクターは fallback `CYBERCRIME_CONTEXT_CUES` を使うが、**この定数自体が全部ジャンル語**

下流影響: 記事詳細の「言及された組織・関係者」表示 / PIR top_actors / STIX export (無ゲート消費者、
inventory 指摘) / actor 検索の逆引き。

## 2. 根因

> cue 機構が「この記事はサイバー記事か (**ジャンル**)」を検査しており、
> 「この記事は**このアクター**の話か (**同一性**)」を検査していない。

ジャンル cue は CTI コーパスにおいて識別力ゼロ。曖昧解消に使えるのは**同一性の証拠**だけである。

## 3. 設計: 同一性 cue 原則

### 3.1 照合条件の変更 (actor_normalizer)

ambiguous actor のマッチ条件を「名前一致 + ジャンル cue」から「名前一致 + **同一性証拠**」に変える。
同一性証拠は以下のいずれか (OR):

| # | 証拠 | 例 (Play の場合) | 出所 |
|---|---|---|---|
| E1 | 他の別名の共起 | PlayCrypt | entry.aliases (名前自身は除く) |
| E2 | 関連マルウェアの共起 | — | entry.associated_malware |
| E3 | MITRE Group ID の共起 | G1040 | entry.mitre_group |
| E4 | **隣接パターン**: `<名前> + (ransomware\|ransom\|group\|gang\|actor\|apt\|ランサム)` | "Play ransomware" | 名前から構造的に導出 |
| E5 | 隣接パターン (和文語順): `(ランサムウェア\|グループ\|攻撃グループ)<名前>` | 「ランサムウェア Play」 | 同上 |
| E6 | 被害者レコード形式: タイトル `^<名前>:` | "play: Acme Corp" | ransomware.live 取込形式 |
| E7 | 明示 `context_cues` (残すが §3.2 で検証) | daserf (Tick) | entry.context_cues |

- E1-E3 は**辞書エントリ自身から自動導出** — 手書き cue リストのドリフト (今回の汚染の発生源) を構造的に排除。
- E4-E6 は「正当な言及は必ず修飾付きで書かれる」という報道の実態に基づく (Play 単独の動詞用法は E4 不成立、
  "Play ransomware" は成立)。
- **証拠ゼロなら fail-closed** (マッチさせない)。`CYBERCRIME_CONTEXT_CUES` は同一性判定から撤去
  (定数自体は他用途がなければ削除)。
- 非曖昧アクター (希少語名) は従来どおり名前一致のみ (変更なし)。

### 3.2 ジャンル語 denylist + guard test

- `src/cti/genre_words.py` (新規、または generic_alias_words.py に併設) にジャンル語集合を定義:
  ransomware/malware/backdoor/espionage/threat group/攻撃グループ/諜報/スパイ/サイバー攻撃/標的型 等。
- guard test: **全アクターの `context_cues` にジャンル語が含まれないこと** を pytest で強制
  (mitre_sync 再発ループ遮断と同じ「SSoT を実装と test の双方が参照」パターン)。
- 併せて既存 yaml の汚染 cue を一括除去 (polonium 9 / chaos/warlock/kairos/cloak/morpheus 各1)。

### 3.3 既存誤 entity のクリーンアップ

- 対象: ambiguous 全 17 アクターの article_entities (type=actor/actor_provisional)。
- 方法: 新照合器で各記事の title+body を再評価し、**同一性証拠が無い行を削除** (tick で実施した手順の一般化)。
- actor_observed_profile 等の集計は「判定=導出・集計=射影」原則により再構築で追随
  (actor_dictionary_design の 4 層パイプライン)。削除前に件数レポートを出し確認を挟む。

## 4. 影響とリスク

- **期待効果**: FP 疑い率 59% → gallium 水準 (~3%) へ。表示/PIR/STIX の言及ノイズ除去。
- **under-attribution リスク**: 修飾なし裸名のみの正当言及 (稀) を落とす。E4/E5 の語彙は拡張可能な定数とし、
  取りこぼし発見時に追加する。Recall 生命線は主題アクター層・非曖昧アクターに担われており影響しない。
- **rollback**: 照合条件は 1 関数 (`_matched_name`) の分岐なので、env flag
  `ACTOR_IDENTITY_CUES=0` で旧挙動 (ジャンル cue) に即時復帰可能にする。
- **再発防止**: guard test が新規アクター追加時のジャンル cue 混入を CI で遮断。
  mitre_sync は context_cues を書き戻さないことを確認済 (再発ループなし)。

## 5. 実装ステップ (承認後)

1. RED: FP 実例 (play=動詞記事 / deadlock=Siemens 記事 / tick=OctLurk 記事) を固定化した回帰テスト
2. `has_identity_evidence()` 実装 + `_matched_name` 差替 (flag 付き)
3. genre_words denylist + guard test + yaml 汚染 cue 除去
4. クリーンアップ script (dry-run → 件数レポート → 実削除)
5. 90 日 FP 再監査で効果測定 (§1 と同じ手法)
