# ORG-D-JUDGE: 組織名の別名の辞書 (D) の判定 (事前登録)

- 下書き: 2026-11-10 21:0x JST。**この文書のコミットが、辞書の作成 (as-of T)・標本の抽出・veto の再現・Opus の判定より前**
  (台帳 ORG-D-JUDGE、`stats.prereg` = 本書)。作成時点で T までの辞書・標本・Opus の判定・veto の再現は 1 件も計算していない
  (見たのは前回の調べの件数 = 同一の組 145・pairs 235 キー / 辞書 224 キー・写像の違うキー 6・組の不一致 7 と、
  `org_identity_pairs` の日ごとの「同一」の件数・`event_merge_vetoes` の klass/action/detail の件数だけ)
- 設計は利用者が承認した案 (a) (2026-11-10): 本番の表 `org_alias_dict` (0 行) は作らず、ある時刻 T までの材料から
  **メモリ上で**辞書を作って判定する。ORG-D-SWITCH (11-17) は同じ T で本番の表を作る (`scripts/build_org_alias_dict.py --as-of`)
- 測定は本番の DB に読むだけ (read-only セッション、ホストの .venv)。学習・調整を伴わない。Opus は名前の組の正誤の判定だけ

## 1. 仮説

辞書 D (伏字のキーを除いて連結した写像) は、影の対 P (同じ材料を伏字を除かずに連結した写像) と比べて
(i) 「同一」とする名前の組の適合率が十分に高く (ii) P より悪くなく (iii) 統合の焦点の関門 (match_focus) の拒否を
目立って増やさない。向き = 非劣性。

## 2. T と 2 つの写像 (固定)

- **T = 2026-11-10T00:00:00+09:00** (= 2026-11-09T15:00:00+00:00)。T を変えない
- 材料: `org_identity_pairs` の `form = 'and_event12_org-4_v2'`・`logit >= 0`・**`judged_at < T`** の行 (key_a, key_b)
  (判定済みの組は再判定されないので `judged_at` は行が作られた時刻。`resolve_same_pairs` が DB に在る組を飛ばすことをコードで確認済み)
- 伏字のキー: `article_entities` の `entity_type='victim_org'` で値に `*` / `＊` を含み、**`created_at < T`** の値の照合キー
  (`org_alias_dict.masked_keys`)
- **P(T)** = `build_alias_map(材料).mapping` (本番の既定 `EVENTNEWS_ORG_ALIAS_SOURCE=pairs` の写像の T 時点)
- **D(T)** = `build_dict_mapping(材料, 伏字のキー)` (`scripts/build_org_alias_dict.py` と同じ関数。`--as-of T` で本番の表を作ると同じ写像になる)
- **pins** (台帳 `judged_pins.table:org_alias_dict` に書く値):
  - `version` = `asof:2026-11-10T00:00:00+09:00;form:and_event12_org-4_v2`
  - `rows` = D(T) のキーの数
  - `hash` = `sha256( json.dumps(sorted(D(T).items()), ensure_ascii=False, separators=(",", ":")).encode("utf-8") )`
  - ORG-D-SWITCH は出荷の直前に表から同じ式で計算し、3 つが等しいことを確かめる (`requires_pins_equal`)

## 3. 母集団と標本 (名前の組の正誤)

- **閉包の組**: 写像で同じ代表キーを持つ照合キーの、順序なしの組 (x < y) の全部。D の閉包を C_D、P の閉包を C_P とする
  (D は P の辺を除いただけなので C_D ⊆ C_P。逆向きの組が出たら報告して判定不能とする)
- **一致の層 A** = C_D (= C_D ∩ C_P、両方が同一とする組)
- **不一致の層 B** = C_P \ C_D (P は同一、D は同一としない組)
- **汚染の除外 (判定の問いの調整に使った組)**: `data/eval/org_identity_labels_opus.jsonl`・`data/eval/org_identity_labels_sonnet.jsonl` の `shown_name_a` / `shown_name_b`
  (10-12〜10-15 の問いの選定に使用) の名前の組を照合キーの組に直したものと一致する組は、抽出の前に層から外す。外した数を層別に報告する
- **件数**: 層 B が 30 組以下なら全部 (n_B = |B|)、30 組を超えたら層 B から 30 組を無作為。層 A から n_A = 100 − n_B 組を無作為
  (|A| が n_A に満たなければ全部)。合計 100 組 (前回の調べの規模では B = 7・A = 93 の見込み。T で数え直した値を使う)
- **抜き方**: 層ごとに組を (x, y) の辞書順に並べ、`random.Random(20261110)` の `sample` で抜く (層 A → 層 B の順に同じ乱数器)
- **重み**: 層の件数 ÷ 抜いた件数 (逆確率重み)。全部を取った層は 1
- **組の id**: `sha256(x + "\x1f" + y)` の先頭 16 桁。出力・報告には id と判定だけを書き、名前・キーは書かない

## 4. Opus の問い (盲検)

- 付け手: claude-opus-5-5 (bridge 127.0.0.1:8010、effort high)。1 組 1 回 (失敗のときだけ同じ組を 1 回まで試し直す)、再採点なし
- **上限: bridge の呼び出し 110 回** (100 組 × 1.1)。上限に達したら止め、残りは未判定として報告する
- 裁定のロック `data/eval/adjudication.lock` (待たずに失敗。塞がっていれば 5 分おきに取り直す) を取ってから呼び、
  `data/eval/adjudication_ledger.jsonl` に回数を記録する
- **見せるもの**: 2 つの名前 (照合キーごとに、`created_at < T` の `article_entities.value` のうち最も多い表記、
  同数は辞書順) と、名前ごとに最大 2 件の記事の見出しと要約 (要約は先頭 600 字。その名前を victim_org に持つ記事を
  `published_at` の古い順 → article_id の順で、`created_at < T` の entity 行に限る)。本文は渡さない
- **見せないもの**: 層・D / P のどちらが同一とするか・logit・本書・他の組
- **問い**: 「2 つの名前は同じ組織を指すか」
  - `same` = 同じ組織 (表記ゆれ・翻訳・略称・旧社名・運営会社とそのサービス名・親会社と子会社)
  - `different` = 別の組織 (同業他社・同じ事案の別の被害者・名前の似た別組織)
  - `unclear` = 見出しと要約から判断できない (伏字で特定できない名前を含む)
  - 確信度 (high / low) と理由 1 文
- 正解: `same` → 同一が正しい / `different` → 同一は誤り。`unclear` は適合率の分母から外し、件数と重み付きの割合を報告する

## 5. 指標と合否の線 (結果を見る前に固定)

線の出どころ: 別名判定の 10-12〜10-15 の測定 (確認用 98 組で誤同一視 0/62、`data/eval/A1_org_identity_report.md`) と、
統合の適合率 95% 以上を優先する方針 (CLAUDE.md §7、`docs/event_identity_definition.md`)。辞書の線そのものは既存に無いので本書で決める。

- **L1 (辞書の適合率)**: C_D の組の適合率 (層 A の `same` ÷ (`same` + `different`)。層 A は C_D 全体の無作為標本なので重みは一定)
  の点推定 ≥ 0.95 **かつ** 95% 区間の下限 ≥ 0.90。区間は D(T) の連結成分 (代表キー) を単位とするクラスタ bootstrap
  (2,000 回、`random.Random(20261111)`、百分位法)。台帳の「事象の根の cluster」は名前の組に事象の根が一意に定まらないため、
  同じ成分の組の相関を受け止める成分を単位にする (台帳からの変更点として報告する)。Wilson の区間を参考に併記
- **L2 (D は P より悪くない)**: 名前の組ごとの正誤 (D が同一とするか P が同一とするかと Opus の正解が合うか)。
  層 A では両者の正誤は同じなので不一致は層 B だけに出る。
  - b = 「P が正しく D が誤り」 (層 B で `same`) の件数、c = 「D が正しく P が誤り」 (層 B で `different`) の件数
  - McNemar の正確検定 (片側、D が悪い向き): b > c かつ p < 0.05 なら不合格
  - 重み付きの正答率の差 (D − P、層 A と層 B を重みで合わせ `unclear` を除く) の点推定 ≥ −0.02
  - 両方を満たせば合格
- **L3 (veto の両向きの再現、決定論・Opus 不要)**: §6 の再現で、P(T) → D(T) に替えたときの
  「通る → 拒否」の件数 (統合の判断 + 拒否の記録の拒否でなかった行) ≤ max(2, ⌈0.02 × 窓の統合の判断の件数⌉)。
  「拒否 → 通る」は件数を報告する (D は P の辺を除くだけなので構造上 0 の見込み。0 でなければ理由を調べて報告する。線には使わない)
- **総合**: L1 ∧ L2 ∧ L3 で合格 (ORG-D-SWITCH へ進める)。1 つでも満たさなければ不合格 (切替しない)
- **判定不能**: 判定できた組 (`same` / `different`) が 80 未満、`unclear` の重み付きの割合が 0.20 超、Opus が上限で止まり
  未判定が 10 組を超える、C_D ⊄ C_P、のどれか。判定不能のとき線を名乗らない。標本や線を変えるなら新しい事前登録を書く

## 6. veto の両向きの再現 (決定論)

- 窓 = **[T − 14 日, T)** = [2026-10-26T15:00:00+00:00, 2026-11-09T15:00:00+00:00)
- **統合の判断**: `event_merge_decisions` の `decided_at` が窓の中の行。`focus_json` に記録された統合前の両側の焦点
  (`target` / `absorbed`) を `focus_from_dict` で戻し、`match_focus(target, absorbed, org_alias=写像)` を P(T) と D(T) で当てる。
  `focus_json` が空の行は除外して件数を報告。`undone_at` のある行も含め、件数を別に報告
  - 「通る → 拒否」 = P(T) で conflict でなく D(T) で conflict。「拒否 → 通る」 = その逆
- **拒否の記録**: `event_merge_vetoes` の `last_seen_at` が窓の中の行 (klass を問わない)。両側の事象の焦点を
  `scripts/focus_replay.py` の `Loader` (本番と同じ記事の読み込み) で、`last_seen_at` より前に参加していたメンバーから作り直し、
  同じく 2 つの写像で照合する。「通る → 拒否」・「拒否 → 通る」を同じ定義で数える。
  ⚠ 近似: 頻出ガードの分母は現在の窓 (focus_replay.py と同じ近似)、メンバーの参加時刻で切るので当時の焦点と完全には一致しない
- **再現の確認 (報告のみ、線に使わない)**: P(T) で当て直した class と記録された class (`focus_class` / `klass`) の一致率
- 出力は id と class だけ (名前・焦点の値を書かない)

## 7. 汚染の鍵と判定集の同一性

- 学習・調整を伴わない測定。R18 の事象の閉包は対象外 (n/a)。判定の問いの調整に使った名前の組は §3 で外す
- 判定集 `gold:org_alias_fresh` = `data/eval/org_alias_d_judge/labels.jsonl` (組の id・層・判定・確信度)。作成者 ORG-D-JUDGE。
  path と sha256 を manifest と報告に書く。既存の tainted な正解集とは path も sha256 も異なる
- manifest に `prereg_path`・`prereg_git_sha`・`prereg_blob_sha256`・`head_sha`・T・pins・標本の sha256 を書き、
  実行前に `require_prereg` (prereg のコミットが HEAD の祖先で、実行の開始より前) を通す

## 8. 手順

1. 本書をコミット (prereg/1110)
2. `scripts/build_org_alias_dict.py` に `--as-of` を足し (feat/org-alias-asof、テストつき)、判定の道具
   `scripts/replay/org_alias_d_judge.py` を同じブランチに置く (本番では `--apply` しない)
3. `build` 段: T の P・D を作り pins を出し、層を数えて標本を固定 (sample.jsonl・manifest.json)。veto の再現 (veto.json)
4. `label` 段: Opus 100 組 (上限 110)。labels.jsonl・result.json
5. 出力: `data/eval/org_alias_d_judge/`

## 9. 変更履歴

- 2026-11-10 下書き (辞書・標本・判定・再現とも未実行)
