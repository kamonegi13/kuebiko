# weekly / monthly 状況総括の「報告の幅」設計 (2026-09-15、設計のみ・未実装)

## 1. 問題

weekly / monthly の render は、期間内に revision を持つ全 Situation を軌跡判定として
**全件**プロンプトに載せている (`src/assessment/stateful.py` 段 5c → `render.build_render_plan`)。
出力上限は 6,000 tok (≒ 4,000 字)。

| 期間 | 判定数 (中央 / 最大) | render プロンプト (中央 / 最大) | 1 判定あたりの出力 |
|---|---|---|---|
| daily | 10 / 20 | 8.7k / 15.8k tok | ~400 字 |
| **weekly** | **92 / 116** | **40.2k / 55.9k tok** | **~43 字** |
| **monthly** | **137 / 153** | **60.7k / 73.4k tok** | **~29 字** |

(2026-09-15 実測、Gemma-4 26B tokenizer、chars/token = 1.85。weekly 11 窓 / monthly 3 窓。)

1 判定あたり 30-40 字では分析は書けない。[SYNTHESIS.md §41](research/llm_training/SYNTHESIS.md)
が観察した「chain の因果/相関の峻別が同型列挙に退化」「発火した指標 vs 開いている指標の区別消滅」
は、モデルの能力ではなく **この幅** が原因である可能性が高い (26B/31B/Sonnet のどれでも
92 件を 4,000 字に分析的に書くことはできない)。

daily は 09-15 に幅ガード (salience 上位 12 + 件数明示) を入れたが、weekly/monthly は
**対象外にしてある** — 一律 12 に切ると PIR が落ちる (下記) ので、別に設計する。

## 2. 測定 — 単純な上位 N で何が落ちるか

weekly 11 窓で moved を salience 上位 12 に切ると:

| 窓 | moved | PIR 数 | 上位 12 で落ちる PIR | その優先順位 (0 = 最優先、全 20) | 保証で足す判定 |
|---|---|---|---|---|---|
| 09-06 | 84 | 19 | 2 | 10, 18 | 2 |
| 08-30 | 85 | 19 | 1 | 10 | 1 |
| 08-23 | 56 | 19 | 2 | 6, 18 | 2 |
| 08-16 | 60 | 19 | 3 | 6, 10, 18 | 3 |
| 08-09 | 64 | 18 | 2 | 10, 18 | 1 |
| 08-02〜06-28 (6 窓) | 47-92 | 17-20 | 0-1 | 9 / 18 | 0-1 |

monthly (上位 15): 0-2 PIR、優先順位 5, 6, 10, 18、保証で +0-2 件。

読み方:
- 落ちるのは **毎窓 1-3 個、多くは優先順位の低い PIR** (18 = `pir_general_agency_alert`、
  10 = `pir_apt_attribution`)。最優先帯 (0-4: 中国/北朝鮮/ロシア APT・日本標的・重要インフラ)
  は salience の PIR boost で残る。
- **PIR 別に最低 1 件を保証しても A 層は 13-15 件**にしかならない (毎窓 +1-3)。安い。
- daily とは非対称: daily は 73 窓中 1 窓しか落ちなかったので保証を入れなかった。
  weekly は 11 窓中 10 窓で落ちる → 入れる。

delta の内訳 (weekly): opened 10-49 / hypothesis_flip 6-26 / escalated 11-16 / reopened 0-24 /
strengthened・weakened 4-17。**flip が週 14-26 件**あるのは別問題 (判定の揺れ、SYNTHESIS §36 の
「flip 定着 48%」) で本設計の範囲外だが、幅を絞ると flip の多さがそのまま A 層を占有しうる
ため、下記 3 層で「A に入る flip は接地ゲート通過分のみ」とする。

## 3. 設計 — 3 層に分けて全件を「扱う」が、全件を「書かせない」

現行 daily の骨格 (moved / standing / 関係 / 指標 / PIR ロールアップ) は変えず、
**moved の供給を 3 層に分ける**。no-silent-caps (落とすなら件数と理由を書く) はそのまま。

| 層 | 内容 | 件数 | プロンプト形 | 出力への指示 |
|---|---|---|---|---|
| **A 本文** | salience 上位 N (weekly 12 / monthly 15) **+ PIR 別最低 1 件** (A に無い PIR ごとに、その PIR を持つ salience 最上位を 1 件追加) | 13-17 | 現行どおり (claim / 見立て / 確度 / 変化の内容 / 根拠抜粋 3 / 含意 / 対立 / 欠落) ≒ 600 tok/件 | 各 1-3 文で分析 (現行の weight/chain/cog/spillover の指示) |
| **B 一覧** | A に入らなかった moved の残り | 30-120 | 1 行: `[id]【delta】claim (見立て / 確度)` ≒ 40 tok/件 | weight_section 末尾に「そのほかの動き」として delta 種別ごとの件数 + 代表 2-3 件を 1 段落。個別の分析はしない |
| **C 件数** | B が `_LIST_MAX` (weekly 60 / monthly 90) を超えた分 | 0-60 | 「ほかに N 件 (台帳に記録済み)」の 1 行 | 触れない |

A 層の選抜順序 (決定論):
1. `rank_judgments(moved)` で salience 降順。
2. headline 指名判定 (`pick_headline`) を必ず含める (daily の 09-15 ガードと同じ)。
3. 上位 N を取る。
4. **A に含まれる PIR の集合**を取り、moved 全体に現れる PIR のうち A に無いものごとに、
   その PIR を持つ salience 最上位の判定を 1 件追加 (1 件が複数 PIR を満たしてよい)。
   追加順は PIR の優先順位順 (config 順)。
5. A の上限を N + `_PIR_GUARANTEE_MAX` (weekly 6 / monthly 6) で止める。超えた PIR は
   B 層 + PIR ロールアップで扱われる (ロールアップは全判定から作るので関心領域は消えない)。

B 層の 1 行は **証拠抜粋を載せない** (それが 600 → 40 tok の差)。判定の存在と方向だけを
伝え、分析は A に集中させる。

### プロンプト長の見積り

| | 現行 | 設計後 |
|---|---|---|
| weekly (moved 92) | ~40k tok | 3k + A 14×600 + B 60×40 ≒ **13.8k tok** (max: 3k + 17×600 + 60×40 = 15.6k) |
| monthly (moved 137) | ~61k tok | 3k + A 17×600 + B 90×40 ≒ **16.8k tok** |

出力上限は 6,000 tok のまま (A 14 件 × ~250 字 + B 段落 + 他セクション で収まる)。
**学習の壁 (14.1k tok) は weekly では際どく、monthly では超える** — weekly/monthly を
CoT 教師の収穫対象に入れるなら `_LIST_MAX` を weekly 40 / monthly 40 に落とす
(B は 1 行 40 tok なので 20 件減で 0.8k tok)。収穫の前に決める。

## 4. 実装計画 (合計 ~150 行 + テスト、1 デプロイ)

1. `src/synthesis/grounded/render.py`
   - `_MOVED_SECTION_MAX` を `{"daily": 12, "weekly": 12, "monthly": 15}` に拡張。
   - `_PIR_GUARANTEE_MAX = {"weekly": 6, "monthly": 6}`、`_LIST_MAX = {"weekly": 60, "monthly": 90}`。
   - `build_render_plan` で A/B/C を決定論に分ける。`_judgment_view` とは別に
     `_judgment_line(j)` (1 行形) を足す。omitted のログに A/B/C の件数を出す。
2. `prompts/synthesis/render_skeleton.j2` + `render.j2` (golden 同期)
   - moved ループの後に `{% if moved_list %}【そのほかの動き (一覧)】 … {% endif %}` と
     C の件数行。`weekly/monthly` 専用の指示散文は **新 block** `other_moves_guidance`
     (seed yaml + DB rubric を同時更新 — hot-mount で先に skeleton だけ反映すると
     composer が legacy へ fallback する、09-15 の実例)。
3. テスト: A の PIR 保証 (A に無い PIR の最上位が入る / 上限で止まる / headline が残る)、
   B の 1 行形に抜粋が無い、C の件数、daily は不変 (回帰)。
4. 実データで測る: 11 weekly 窓を `build_render_plan` で再構築し、プロンプト長と
   A の PIR 被覆を印字 (LLM 呼出なし)。**目標: 中央 ≤14k tok、A の PIR 被覆 = moved の PIR 全部**。
5. デプロイ後の初回 weekly (09-21 01:10) を対読で読む: 「列挙に退化」が解消したか
   (chain の因果/相関の峻別、指標の発火/開放の区別が戻るか)。数字より中身。

## 4b. 実装後の実測 (2026-09-15、過去窓を再構築。LLM 呼出なし)

| 期間 | 実装前 | 実装後 (中央 / 最大) | A / B / C の内訳 | PIR 落ち |
|---|---|---|---|---|
| weekly (11 窓) | 40.2k tok | **18.2k / 23.3k** | A 13-14 / B ≤60 / C 0-19 | **0** |
| monthly (3 窓) | 60.7k tok | **17.2k / 23.1k** | A 15-17 / B ≤90 / C 0-30 | **0** |
| daily (20 窓、回帰) | 8.7k | 10.9k / 15.8k | A ≤12 / — / — | 0 |

- **PIR 落ちはゼロ** (保証が全窓で効いた。コストは A 層 +1-2 件)。
- **14k の目標には届かなかった** (中央 18.2k)。§3 の見積り (1 行 40 tok) が低すぎたのが主因で、
  claim を 60 字に切っても 1 行 ~45 tok、60 行で 2.7k tok かかる。
- 106 判定の週 (21.8k tok 時点) の内訳実測: 変化 6.3k (A 14 件 = **451 tok/件**) /
  一覧 6.0k → 3.6k (短縮後) / **継続 3.2k (standing 22 件 = 145 tok/件、無上限)** /
  監視指標 2.4k / PIR 対応 2.0k。
- ⚠ **新たに判明: standing (継続中の判定) が weekly では無上限**。daily は上流の
  `_FALLBACK_STANDING=3` で 3 件に絞られるが、weekly の軌跡射影では no_change の判定が
  22 件そのまま載る。moved を絞った今、次に大きいのはここ (3.2k tok)。**本設計の承認範囲外
  なので手を付けていない** — 上限を入れるなら moved と同じ salience 上位 + 件数明示。

## 4c. 再設計 (2026-09-15 夕、利用者指摘): 制約は長さではなく**内容**

利用者指摘:「長さというより内容。インプットすべき事象が多いから総括が長くなるのはむしろ良い。
**真に重要な事象を総括するもの**だから」。これを受けて §3 の固定 N を破棄した。

### 「2,100 字」の正体 — 制約はモデルでなく我々の指示だった

出力上限 6,000 tok (≒11,000 字) に対し実出力は daily 2,103 / weekly 2,168 / monthly 2,531 字。
**上限は効いていない**。governor は rubric の `sections_intro`「各 2-5 文」で、しかも
`other_sections_guidance` の「各判定 1-2 文」と**矛盾**しており、モデルは小さい方に従っていた。
実測 weight_section の文数: daily 6 文 (判定 ~5 件 = 1 文/件) に対し **weekly 9 文 (判定 92 件
= 0.1 文/件)**。period 非依存の文数指示が、判定件数に連動すべき節を握りつぶしていた。

### 変更

| 対象 | 旧 | 新 |
|---|---|---|
| A 層の選抜 | salience 上位 **固定 12/15** | **重要度基準**: salience ≥ 最高の 50%。下限 12/12/15・上限 40 |
| 出力の文数指示 | 全 period 「各 2-5 文」 | daily 2-5 / weekly・monthly **3-8 文** |
| weight_section | 「各判定 1-2 文」(死文) | 「本文に挙げた **N 件すべて**を 1 件あたり 1-2 文」= 件数に連動 |
| `max_tokens` | 6,000 | daily 6,000 / weekly・monthly **10,000** (JSON 途中閉じ防止) |

実測 (過去窓を再構築): weekly A = **24-37 件** (旧 13-14)、monthly A = 28-41 件、
**C (件数のみ) はほぼ 0 = 全件が本文か一覧で扱われる**。daily は不変 (下限 12 > moved 5-7)。
プロンプトは weekly 中央 23k tok へ伸びたが、これは**意図した結果** — 制約は長さではない
(context 262k / prefill は週 1 回 / MLX の壁は学習データ側の話)。

⚠ Jinja の新変数は `| default(...)` を付ける。**prompts はホットマウントでコードより先に
反映される**ため、既定値が無いと本番の描画が UndefinedError で落ちる
(`test_template_renders_without_headline_mode_variable` が検出した)。

## 5. 触らないもの / 別件

- 台帳側 (`_MAX_UPDATES_BY_PERIOD["weekly"]=12`) は据置。weekly の幅の問題は台帳ではなく
  軌跡射影 (期間内 revision 全件) にあるため。
- flip の多さ (週 14-26 件) は判定の揺れの問題で、幅の設計では解決しない (§36)。
- `_FALLBACK_STANDING = 3` の名前は実態 (standing の幅) と合っていないが、本設計と同時に
  触ると差分が混ざるので後回し。
