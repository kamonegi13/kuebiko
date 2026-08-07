# モデル能力ティア アーキテクチャ (model tier)

作成: 2026-07-08 / 対象: `src/tools/model_tiers.py` を中心とした LLM モデル設定の本質再設計

## 1. 本質 (これが要点)

**ツールが「step → 能力ティア」を決め (code 所有)、ユーザは「ティア → 実モデル」を決める (config)。**

旧設計は逆に近く、コードが step ごとに生スロット (`ollama_main_model` / `ollama_synthesis_model` /
`ollama_spotlight_model` / `ollama_extract_model`) を直接ハードコード参照し、割当が 17 箇所に散在して
いた。2026-07-08 の「detect-new を 26B に手配線」(`fast_llm` plumb) はこの再設計の前触れだった。

本再設計で:
- step → ティアの写像を **1 レジストリ** (`STEP_REGISTRY`) に集約 (散在参照を撲滅)。
- ティア → 実モデルは **DB (config_store) を SSoT** とし UI で編集可 (再起動不要)。
- ユーザに「どの処理がどのモデルを使うか」を意識させない (per-step 割当は UI に出さない)。

## 2. ティアは 3 つ (reasoning / fast / embedding)

| ティア | 実体 (現) | 担う step | 根拠 |
|---|---|---|---|
| `reasoning` | Dense 31B | status synthesis narrative / 台帳 ACH / adversarial (`SYNTHESIS_NARRATIVE`) + PIR spotlight (`PIR_SPOTLIGHT`, 本番 31b 踏襲) | 深い多段推論・低頻度・品質律速 |
| `fast` | MoE 26B | 記事要約・翻訳, triage, detect-new, 抽出, digest deep-dive, PIR focus/compile, actor訳, 対話系 (検索/selector) | 高スループット・大量入力・実用品質 |
| `embedding` | snowflake-arctic-embed2 | 意味的重複排除・検索 (`EMBED`) | 別モダリティ |

### 「3 で十分か」の検討 (4 ティア案を steelman して却下)

全 step→model バインディング (バッチ 11 + 対話 4) を実データで棚卸しした結果、能力クラスは実質この
3 つに収束する。`fast` を「要約 (品質重視) / triage (選別のみ)」に割る 4 ティア案を検討したが:

- (a) 現状 triage も要約も同じ 26B で運用され、**分離要求の実シグナルが無い**。
- (b) detect-new を極小モデル (8B 等) に落とすと本プロジェクトの核心 **Recall** を毀損する。
- (c) 同一モデルを 2 ティアに割ると UX 混乱 (設定は同じ値になる)。

→ **3 ティアが正**。ただし潜在的 footgun (`fast` が「要約品質」と「triage 速度」を兼任) を残すため、
`STEP_REGISTRY` は **enum 1 行 + step 再マップ数行**で 4 ティア化できる構造にしてある (拡張点は
「② 拡張のしかた」)。

## 3. timeout は「ティア属性」でなく「step 属性」

実データが示す通り、同じ `fast` (26B) でも入力量で per-call timeout が変わる:

| step | timeout | 理由 |
|---|---|---|
| PIR daily focus | 120s | 1 PIR あたり ~5-10s の小入力 |
| PIR compile / precise search | 180s | 対話・単発 |
| 記事要約 / 抽出 / selector / actor訳 | 300s | 標準 |
| PIR spotlight | 600s | narrative 生成 |
| detect-new / digest deep-dive | 900s | 大量入力 (未割当 500 件) / rubric+12k tokens |

よって **timeout は `STEP_REGISTRY` (step) が所有**し、**ティアが所有するのは `{model, num_ctx}`** のみ。
散在していた 120/180/300/600/900 の magic number はこの 1 レジストリに集約された。

- `num_ctx` は現状全ティア `None` (Ollama 既定 ~262144 に委譲 = 現挙動維持)。我々の prompt (~21k
  char) は非律速のため設定しない。将来絞る必要が出たら `TIER_NUM_CTX` に値を入れれば
  `OllamaClient` 経由で反映される (seam のみ用意、値は YAGNI で未設定)。
- pipeline wallclock timeout (`pipeline_runner._PIPELINE_TIMEOUT_OVERRIDES`) は別レイヤ (env override
  済) — 本再設計のスコープ外。

## 4. 中華系 whitelist の位置づけ (コード所有のセキュリティ基盤 + 3層防御)

denylist (`FORBIDDEN_MODEL_PREFIXES`) は **config 化しない**。UI でモデル選択を編集可能にしても、
§4 が義務化する防御自体を無効化できないようにするため、denylist は **コード所有** (JobDef のメタが
コード所有なのと同じ思想)。ユーザが編集できるのは **per-tier のモデル選択のみ**で、選択は許可集合に
制約される。

3 層防御 (すべて同一定数 `FORBIDDEN_MODEL_PREFIXES` を参照 = drift 不能):

1. **UI dropdown 生成** — `/api/tags` は qwen 等も返すため `is_model_allowed()` (非 raising) で除外。
2. **保存時検証** — `validate_model_tiers()` が中華系を弾き 400 (API 直叩き対策)。
3. **構築時** — `OllamaClient.__init__` / `OllamaEmbeddingClient.__init__` の `validate_model_name()`
   (最終防御・不変)。

denylist は 2026-07-08 に主要中華系ファミリ (baichuan/ernie/hunyuan/minimax/moonshot/kimi/skywork/
telechat/xverse) を追加して網羅性を上げた (より制限的な方向の安全な強化)。

## 5. 永続化・移行・後方互換

- config key `model_tiers` (config_store DB、版履歴)。`channels` / `product_routing` と同型
  (BUILTIN を base に DB で上書き、`.env`/yaml を runtime の正にしない)。
- **bootstrap 既定 = コードの `BUILTIN_MODEL_TIERS`** (`reasoning=gemma4:31b` / `fast=gemma4:26b` /
  `embedding=snowflake-arctic-embed2`)。**`.env` はモデル選択に一切関与しない** (2026-07-08 に
  MAIN/SYNTHESIS/EMBED/EXTRACT/SPOTLIGHT スロットを config_loader・env_editor・.env・.env.example
  から完全撤去)。
- **起動時 seed** (`seed_model_tiers_if_absent`): DB 未投入なら `BUILTIN_MODEL_TIERS` を version 1 に。
- **解決順** (`resolve_tier_model`): DB → `BUILTIN_MODEL_TIERS`。DB に当該ティアが無くても BUILTIN に
  fallback するため seed 前でも壊れない。
- **rollback flag** `MODEL_TIERS_CONFIG_DB=0`: DB を無視し BUILTIN 直行 (UI 編集が壊れた時の安全弁)。
- UI 編集後は DB が正 (BUILTIN より DB 値が勝つ = SSoT 性)。

### なぜ `.env` からモデルを外したか (2026-07-08)

初版は `.env` を bootstrap 源にしていたが、モデル設定が `.env` と「モデル」タブの **2 箇所**に分裂して
いた (ユーザ指摘)。既存の運用 config (`channels` / `product_routing` / `routing_rules`) は全て
「コード BUILTIN + DB 上書き」で `.env`/yaml を経由しない。model_tiers もこれに揃え、**モデル設定の
編集面を「モデル」タブ (DB) 一本に統一**した。

| env | 役割 | 残す? |
|---|---|---|
| `OLLAMA_BASE_URL` | Ollama 接続 URL (直接参照 live) | **必須** |
| `OLLAMA_EMBED_QUERY_PREFIX` | 埋込 query prefix (直接参照 live、UI 非露出) | **残す** |
| `OLLAMA_*_MODEL` (main/synthesis/embed/extract/spotlight) | ティア方式へ移行、bootstrap はコード BUILTIN | **全撤去** |

運用中に `.env` を編集する必要は無い。モデル変更は Web UI「設定 → モデル」タブ (DB 版履歴付き) のみ。

## 6. 主要ファイル

| 役割 | ファイル |
|---|---|
| ティア/step 定義・レジストリ・解決・factory | `src/tools/model_tiers.py` |
| whitelist SSoT (`FORBIDDEN_MODEL_PREFIXES` / `validate_model_name` / `is_model_allowed`) | `src/tools/llm_client.py` |
| Ollama モデル列挙 (`list_ollama_models`) | `src/ui/services/health.py` |
| API (GET/POST, dropdown フィルタ) | `src/ui/api/model_tiers.py` |
| config 履歴登録 (`_KNOWN_KEYS` / `_invalidate`) | `src/ui/api/config_history.py` |
| 起動時 seed | `src/ui/app.py` (lifespan) |
| UI (モデルタブ) | `frontend/src/pages/ConfigPage.tsx` (`ModelTiersEditor`) |
| テスト | `tests/unit/test_model_tiers.py` |

## ② 拡張のしかた

- **新しい LLM step を足す**: `Step` enum に 1 行 + `STEP_REGISTRY` に `{tier, timeout}` を 1 行。
  呼び出し側は `build_llm_for(Step.X, config)`。(`test_every_step_registered` が漏れを検出)
- **4 ティア目を足す (将来 fast を triage/workhorse に割る等)**: `Tier` enum に 1 行 + `TIER_NUM_CTX`
  に 1 行 + 該当 step の `STEP_REGISTRY` の tier を差し替え + seed の `derive_tiers_from_legacy` に
  導出を 1 行 + UI `TIER_META` / `TIER_ORDER` に 1 行。

## やらないこと (確定・再提案不可)

- **per-step のモデル割当を UI に出さない** (粒度過多・誤設定の温床)。ティア粒度が正。
- **step→tier を散在させない** (`STEP_REGISTRY` 1 箇所集約)。
- **中華系 denylist を config 化しない / 外さない** (§4 セキュリティ)。
- **timeout をティア属性にしない** (step の入力量で変わるため step 属性が正)。

## ⑦ 外部 LLM プロバイダ (2026-07-18 開放)

CLAUDE.md §4 改訂により、ティア割当に **`anthropic:<model>`** (例:
`anthropic:claude-sonnet-5`) を指定すると当該ティアの処理を Anthropic Messages API で
実行できる。原則は次の通り:

- **既定はローカル Ollama のまま** (`BUILTIN_MODEL_TIERS` は変更しない)。外部送信は
  利用者が `.env` に `ANTHROPIC_API_KEY` を設定し、かつ UI「モデル」タブで明示割当した
  ティアのみで発生する (ツールが勝手に外部へ送らない)。
- **dispatch は `build_llm_for` の prefix 判定 1 箇所** — `anthropic:` なら
  `src/tools/anthropic_client.py` の `AnthropicClient` (httpx 直・SDK 非依存、構造化出力は
  tool use 強制)、それ以外は従来の `OllamaClient`。step→tier→timeout の機構は不変。
- **中華系 denylist はプロバイダ横断** — `validate_model_name` は `anthropic:qwen-*` の
  ような prefix 越しの禁止系も弾く (3層防御は従来通り)。
- **埋込ティアは外部不可** (`validate_model_tiers` が拒否。Anthropic に embedding API が
  無く、埋込は大量呼出でローカル前提)。
- UI dropdown の外部選択肢は `ANTHROPIC_MODEL_CHOICES` (model_tiers.py) が SSoT。
  API キー未設定時は選択肢に出ない = 従来のローカルのみ画面。

### API キーの UI 完結管理 (2026-07-19)

`ANTHROPIC_API_KEY` は UI「設定 → モデル」タブから設定/削除できる
(`POST /api/v1/model-tiers/anthropic-key`)。保存先は **`.env`** — config_store (DB) は
版履歴 (app_config_versions) と日次 pg_dump backup に値が残り続けるため**秘密情報を置かない**
(§4 の「認証情報は .env」を維持)。compose は env_file 注入をしておらず `.env` は
ファイルマウント + `AppConfig()` が request ごとに再読込するため、**保存は即時反映
(再起動不要)**。キー削除は `anthropic:*` 割当ティアが残っている間は 400 で拒否
(06:30 run で初めて壊れる事故を設定時に防ぐ)。API レスポンスにキー平文は返さない
(マスクのみ、readonly instance 対策)。

## ⑧ dialog ティア新設 (2026-07-19 — §2「3で十分」の予約拡張を発動)

外部 LLM 開放 (⑦) により「**対話系だけ外部 (Claude)、収集系バッチはローカルのまま**」という
割当が実需になった (fast 全体の外部化は月 2 億入力トークン級でコスト不成立、対話系のみなら
~1% 未満)。§2 で予約していた「enum 1 行 + step 再マップ数行」の拡張を発動し 4 ティア化:

- ``dialog`` = PIR_COMPILE / SELECTOR_PROPOSAL / PRECISE_SEARCH / ASSISTANT_CHAT
  (user-facing・低頻度。timeout は従来値を踏襲)
- BUILTIN は ``gemma4:26b`` (fast と同モデル = 分離時の挙動保存)。既存 DB 設定に
  dialog キーが無くても resolve が BUILTIN に fallback するため **migration 不要**
- 推奨割当例: dialog=``anthropic:claude-haiku-4-5`` (または sonnet)、
  reasoning=``anthropic:claude-sonnet-5``、fast/embedding=ローカル継続

## ⑨ Claude Code サブスク経由 (2026-07-19)

API クレジットでなく **Claude サブスクリプション (Pro/Max)** の枠で外部推論する第 3 の経路。
ホスト側 bridge (`scripts/claude_code_bridge.py`、127.0.0.1:8010) が `claude -p` を HTTP 化し、
コンテナの `ClaudeCodeClient` が `host.docker.internal` 経由で呼ぶ (Ollama と同じパターン)。

- ティア割当: `claudecode:sonnet` / `claudecode:haiku` / `claudecode:opus`
  (UI には **bridge 疎通時のみ** 表示。opus は Max プランのみ)
- 前提: ホストに standalone CLI (`curl -fsSL https://claude.ai/install.sh | bash`) +
  サブスク認証。bridge 起動: `uv run python scripts/claude_code_bridge.py`
- **サブスクのレート上限 (5h 窓) を共有** — reasoning/dialog 向け。fast は不成立
- 構造化出力は schema 同梱プロンプト + pydantic 検証 + リトライ (tool 強制なし)
- bridge は 127.0.0.1 bind・model 引数許可リスト・空 cwd・--max-turns 1 で
  純テキスト生成に限定。denylist はここでもプロバイダ横断

### bridge の使用状況可視化 + 常用化 (2026-07-19)

- サブスクの**レート残量 API は存在しない**ため、bridge が全 call を自己記録
  (`data/claude_bridge_usage.jsonl`、7日 rotation) し、/health に 5h窓 (レート律速の
  単位) / 今日 / 7日 の集計 + **API 換算コスト** (total_cost_usd、サブスクでは非請求) を
  同梱。UI「設定 → モデル」の Claude Code カードに表示される
- 常用化: `bash scripts/install_claude_bridge_launchagent.sh` で LaunchAgent 化
  (ログイン時自動起動 + KeepAlive、log は ~/Library/Logs/claude-bridge.log)。
  §9 の「launchd 不使用」はスケジューラ用途の話 — ホスト補助サービスの常駐は本方式が正

## ⑩ 外部 → ローカル自動フォールバック (2026-07-19)

外部経路 (anthropic:/claudecode:) はレート制限・残高不足・bridge 停止・認証切れで
**利用できない瞬間がある**。`build_llm_for` は外部 ref を `FallbackLLMClient`
(src/tools/llm_fallback.py) で包み、可用性系の失敗時に**当該ティアのローカル既定
(BUILTIN_MODEL_TIERS) へ自動切替**して step を継続する。

- 発動対象 = LLMError 系全般。**LLMForbiddenModelError (中華系ゲート) だけは絶対に
  迂回しない** (そのまま raise)
- **cooldown 10 分**: 一度失敗したら外部を試さず直接ローカルへ (レート制限中の
  夜間バッチが毎 call 失敗を待つ無駄を排除)。primary モデル別に process 内共有
- **記録の正直さ**: fallback 発動後の `model` は `"claudecode:sonnet→gemma4:31b"` 表記
  (synthesis の llm_model 記録が実態と乖離しない)。発動は WARNING `llm_fallback_engaged`
- 構築段階の失敗 (API キー未設定等) もローカルで継続
- rollback: `LLM_LOCAL_FALLBACK=0` で旧挙動 (外部失敗 = step 失敗)
