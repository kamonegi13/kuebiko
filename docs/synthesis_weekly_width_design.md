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

## 5. 触らないもの / 別件

- 台帳側 (`_MAX_UPDATES_BY_PERIOD["weekly"]=12`) は据置。weekly の幅の問題は台帳ではなく
  軌跡射影 (期間内 revision 全件) にあるため。
- flip の多さ (週 14-26 件) は判定の揺れの問題で、幅の設計では解決しない (§36)。
- `_FALLBACK_STANDING = 3` の名前は実態 (standing の幅) と合っていないが、本設計と同時に
  触ると差分が混ざるので後回し。
