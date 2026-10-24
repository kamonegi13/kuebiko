# CLAUDE.md — kuebiko

このファイルは Claude Code (および後続のエージェント) がこのリポジトリで作業する際の最上位ガイドです。**変更を加える前に必ず読み、矛盾する指示があった場合はこのファイルを優先**してください。

---

## 1. プロジェクト概要

- **名称**: kuebiko (久延毘古 — 「歩けぬが天下を悉く知る」知識の神。旧称 cti-briefing-pipeline、2026-07-31 改称)
- **目的**: 毎朝 06:30 JST に CTI (Cyber Threat Intelligence) ブリーフィングを自動生成し、Discord に BLUF 形式で投稿する個人運用のパイプライン。さらに **常駐 Web UI** で運用管理 (設定編集・ログ可視化・プロンプトチューニング・即時実行・スケジューラ管理) を行う
- **利用者**: 日本の CTI 担当者 (単独運用)
- **稼働環境**: MacBook Pro M5 Max 128GB を常駐サーバとして使用。Docker コンテナ + ホストネイティブ Ollama のハイブリッド構成
- **公開範囲**: コードは MIT で公開。運用データ・ログ・シークレット・運用者固有の設定は
  リポジトリ外 (.env / data/ / DB / CLAUDE.local.md / prompts/_persona_local.j2) に置き、
  コミットに含めない。**コミットメッセージ・コメントにも運用固有情報 (実ドメイン・組織帰属・
  実インシデント詳細) を書かない**

### 情報源

1. **Grok タスクのレポート** — Grok が生成するレポート URL がメールで通知される。IMAP で受信し、Playwright で本文取得 (Phase 2)
2. **直接 RSS フィード** — `config/sources/feeds.yaml` で宣言した 100+ feed を httpx 並列 fetch (Phase X-1)。 旧 Inoreader 経路は Phase Y 完了後 (2026-05-26) に完全撤去
3. **Web scraper (sitemap)** — `config/sources/watchers.yaml` で宣言した RSS のない一次ソース (ENISA / IPA / ISW 等) を sitemap 経由で監視 (Phase X-2)

### 主処理パイプライン

```
取得 → 全文抽出 → 重複排除 → ローカル LLM で要約・翻訳・脅威分析 → BLUF 整形 → Discord 投稿
```

スケジューラ (アプリ内 APScheduler) が毎朝 06:30 JST に上記を起動し、Web UI から手動でも即時実行可能。

---

## 2. アーキテクチャ概要

> **現状サマリ (2026-05-30 更新 — 以下の節の古い記述より優先)**
> - **UI は React SPA** (`frontend/`、Vite build → `frontend/dist`)。HTMX+Jinja2 は移行済で
>   `src/ui/routers/*.py` は SPA への redirect stub、`src/ui/templates/` は base.html のみ残存。
>   実 UI は `frontend/src/pages/*.tsx` + `src/ui/api/*` の JSON API。
> - **メタデータ/ベクトル DB は PostgreSQL** (Phase Y 移行済、SQLite は dev/tests fallback)。
> - **情報源 (source) は transport 透過に統合管理**: feeds.yaml(rss) / watchers.yaml(sitemap) /
>   scrapers.yaml(html_scraper) を `src/ui/api/_source_manager.py` が一元的に list/enable/disable/
>   delete/folder する。**runtime SSoT は DB** (config_store, key=feeds/watchers/scrapers)。yaml は
>   **初回 seed 専用** (起動時に `src/sources/source_store.py:seed_all_if_absent` が未投入 key を取込)、
>   UI 編集は DB に版保存され git/yaml は触らない。`SOURCES_CONFIG_DB=0` で yaml 直読み/直書きの
>   旧挙動に即時 rollback。読み書きの flag 分岐は source_store に集約 (loaders/writers は seam 経由)。
>   `web-scraper-watchers` cluster は scrapers/watchers の enabled 全件を **registry から auto-collect**
>   し、pipelines.yaml には宣言的 yaml で表現できない bespoke scraper (nicter / 38north 等の
>   Playwright/特殊系) のみ列挙する (pipelines.yaml は移行対象外、bespoke 宣言として git 管理のまま)。
> - 全体設計の見直し記録: [docs/source_pipeline_architecture_review.md](docs/source_pipeline_architecture_review.md)
>   (P0/P1/P5・P2-lite・P3 の stagger は実施済。ingest の merge・P4・P6 は見送り — 同文書 §8-9)。§5 のディレクトリ表に未記載の package (2026-10-10 に `ls src/` と照合):
>   `src/watchers/ src/digest/ src/taxonomy/ src/synthesis/ src/pir/ src/spotlight/ src/assessment/ src/assistant/
>   src/eval/ src/eventnews/ src/forecast/ src/graph/ src/pipeline/ src/search/ src/sources/ src/vocab/ src/prompts/`。
>   HTMX + Jinja2 前提の記述 (§1・§5・§10・§11) は初期設計の記録で、現行 UI は上記 React SPA。
> - §6 のフェーズ表は 2026-10-10 に完了状態へ更新済。現在は上記の統合再設計フェーズ。
> - 月次の状況総括 (`monthly-status-synthesis`) は 2026-09-29 に廃止 (長期の軌跡は PIR の 30 日推移)。

### 全体構成 (Phase 1.5 以降)

```
[macOS host]
  ├── Ollama (Metal 加速、ホストネイティブ)         ← :11434
  │
  └── Docker Container: kuebiko                ← 唯一のアプリコンテナ (常駐)
        port: 127.0.0.1:8001 (host) → :8000 (container, uvicorn/FastAPI)
        restart: unless-stopped
        internal:
          ├─ FastAPI app
          │    ├─ Web UI (HTMX + Jinja2)
          │    └─ 内部 API (run_pipeline 呼び出し、設定編集)
          └─ APScheduler (TZ=Asia/Tokyo)
              └─ cron: 06:30 JST → run_pipeline()
        volumes:
          ./data:/app/data:rw       (SQLite: jobstore + run_history + 将来の Chroma)
          ./config:/app/config:rw   (YAML、UI から編集)
          ./prompts:/app/prompts:rw (Jinja2、UI から編集)
          ./.env:/app/.env:rw       (シークレット、UI から編集)
        network: host.docker.internal:11434 → Ollama
        user: 1001:1001 (非 root)
```

### 責務別パッケージ構成 (Phase 5A 再構成済)

```
src/
├── main.py                  # エントリポイント、パイプラインオーケストレータ
├── config_loader.py         # 設定 (.env + config/**/*.yaml) ロード
├── logging_config.py        # structlog 構造化ログ + 機密マスク
├── tools/                   # 汎用 I/O アダプタ (CTI ドメイン非依存)
│   ├── article_model.py     # Article 正規化モデル (source 横断、 Phase X-1)
│   ├── direct_rss_source.py # 自前 RSS fetcher (config/sources/feeds.yaml、 Phase X-1)
│   ├── content_extractor.py # trafilatura
│   ├── llm_client.py        # Ollama (中華系ホワイトリスト)
│   ├── embedding_client.py  # Ollama embedding (Phase 3b)
│   ├── discord_publisher.py # Webhook
│   ├── url_normalizer.py
│   ├── source_router.py     # ArticleSource Protocol
│   ├── article_triage.py    # Phase 3.1 軽量重要度判定
│   └── imap_client.py       # Gmail IMAP (Grok 通知用)
├── grok/                    # Grok レポート取込 (JSONL 経路、2026-06-13 に markdown 経路撤去)
│   ├── fetcher.py           # Playwright 経由 DOM 抽出
│   ├── jsonl_parser.py      # JSONL output → TweetRecord
│   └── jsonl_to_briefings.py # theme→channel routing 込みの BriefingMessage 化
├── cti/                     # CTI ドメイン特化メタデータ (Phase 4)
│   ├── ioc_extractor.py     # CVE/IP/domain/hash 正規表現抽出
│   ├── actor_normalizer.py  # アクター名エイリアス正規化
│   ├── actor_editor.py      # actor_aliases.yaml の構造化編集 (alias 衝突検証が核心)
│   ├── mitre_sync.py        # MITRE ATT&CK 週次逐次同期 (追加系=自動適用+LLM和訳 / 新規actor・alias衝突=レビュー提案)
│   ├── diamond_model.py     # Diamond Model 2 meta-feature 軸 (socio-political intent / technical, Phase Diamond-Axes)
│   └── stix/                # STIX 2.1 書き出し (report 中心・主題にだけ関係・kuebiko 拡張、docs/stix_export.md)
├── storage/                 # SQLite (run_history + APScheduler jobstore)
├── scheduler/               # APScheduler ラッパ (interval / cron)
└── ui/                      # FastAPI + HTMX + Jinja2 (Phase 1.5)
prompts/                     # Jinja2 LLM プロンプト
config/                      # YAML 設定 (pipelines, agents, actor_aliases)
data/                        # SQLite 実体、Playwright state (gitignore)
```

### Phase 5B (将来検討、保留)

CrewAI 化 (エージェント協調) は現要件で明確な ROI がないため保留。必要が出てから着手。
**2026-08-15: 保留の残骸 (config/agents.yaml + AgentsConfig/load_agents) を削除済**。
値は一切使われず、記載モデル名がティア SSoT (`src/tools/model_tiers.py`) と矛盾して
「ここを編集すればモデルが変わる」誤認を招いていた。着手時はスケルトンから書き直す。
理由: 現プロセスは決定的かつ 7 分以内で完了しており、エージェント化は複雑性増加 / LLM コスト爆発 / 再現性低下のデメリットの方が大きい。

### LLM スタック

> ⚠ **`OLLAMA_*_MODEL` 系の環境変数は 2026-07-08 に撤去済**。`AppConfig` に残るのは
> `ollama_base_url` / `ollama_embed_query_prefix` だけで、`.env` に書いても
> pydantic-settings の `extra="ignore"` が黙って捨てる。**モデル割当の runtime SSoT は
> DB (config_store, key=model_tiers)**、fail-safe は `BUILTIN_MODEL_TIERS`。
> 割当は UI「設定 → モデル」タブから行う (2026-09-04 に本節の記述を実装へ同期)。

- **fast ティア (per-article 要約・翻訳 / triage / 分類系)**: 現用 Gemma 4 26B (MoE, active 4B)
  - daily-briefing で 1 article = 1 LLM 呼出を大量実行するため**速度重視**。26B の think=False で 5-15 秒/件
  - Dense 31B も指定可能だが per-article で 40-100 秒/件 → 30 分 timeout 内に処理不能なため非推奨
- **narrative ティア (状況総括 / 事象ニュース / Spotlight / 台帳精読)**: 既定 Gemma 4 31B Dense。
  1 run につき 1 呼出 (~21k char prompt) のため品質重視で Dense 採用、timeout 600-900s。
  外部割当時 (`claudecode:` / `anthropic:`) はローカル既定が自動フォールバックになる
- **reasoning ティア (ACH 分析)** / **dialog ティア (対話・検索・PIR compile)**: `src/tools/model_tiers.py`
  の `STEP_REGISTRY` が step → ティアの SSoT。step 一覧もそこを見る
- **サブ**: Gemma 4 E4B / Llama 3.1 8B (軽量タスク・フォールバック用)
- **外部 LLM (任意、2026-07-18 開放)**: ティアに `anthropic:<model>` を明示割当すると当該処理を
  Anthropic API で実行 (`.env` の `ANTHROPIC_API_KEY` 必須)。既定はローカルのまま。§4 参照
- **Embedding**: snowflake-arctic-embed2 (Snowflake、多言語 SOTA) — Phase 3b から。代替候補: nomic-embed-text-v2-moe, granite-embedding (いずれも Ollama 公式ライブラリで pull 可)。intfloat/multilingual-e5-* 系は Ollama 公式ライブラリ外のため不採用
- **ベクトル ストア**: PostgreSQL に BYTEA + numpy 全件コサイン — Phase Y で SQLite → PG 移行 (個人運用規模では十分。 ChromaDB / pgvector は不採用)
- **メタデータ DB**: **PostgreSQL 16** (Phase Y で SQLite から完全移行) — Phase 1.5 で SQLite 導入、 Phase Y (2026-05-26) で macOS virtiofs WAL 衝突 corruption の根本対策として PG に移行。 named volume (postgres_data) で virtiofs 経路を排除
  - SQLite fallback は `DATABASE_URL` 未設定時のみ動作 (tests / dev 用、 production は PG 必須)
  - dialect 翻訳は `src/storage/db_backend.py:translate_sql()` で自動 (?→%s、 datetime('now',?)→NOW()+interval、 INSERT OR IGNORE→ON CONFLICT 等)

### LLM モデルの切替手順

1. `ollama pull <new-model>` で新モデルをローカルに pull
2. `ollama list` で取得を確認
3. **Web UI「設定 → モデル」タブ**でティア (fast / narrative / reasoning / dialog) に割り当てる。
   `.env` にモデル名を書いても効かない (上の警告を参照)
4. CLAUDE.md §2 の記述を新モデル名に同期
5. `uv run pytest tests/unit/test_llm_client.py` でホワイトリスト検証 (中華系排除) 通過確認
6. **Web UI の即時実行で 1 件 dry-run** し品質を目視確認 — 特に per-article main model 変更時は 1 件あたりの応答時間も計測 (40s 超なら daily-briefing が timeout する)
7. 必要に応じて旧モデルを `ollama rm <old-model>` で削除

**禁止事項**: コード内 (`src/tools/llm_client.py` など) のデフォルトモデル名を直接書き換えない。`.env` または Web UI で上書きすること。

---

## 3. 開発方針

### 言語・ツール

- Python 3.12+
- パッケージ管理: **uv** (`uv add`, `uv run`, `uv sync`)。`pip` / `poetry` は使わない
- フォーマット: `ruff format`
- Lint: `ruff check`
- 型チェック: `mypy --strict` (新規モジュールは strict 必須)
- コンテナランタイム: **OrbStack** (推奨) / Colima / Docker Desktop のいずれか。`docker compose` プロトコル準拠であれば動作する

### コーディング規約

- 命名: `snake_case` 関数・変数, `PascalCase` クラス, `UPPER_SNAKE_CASE` 定数
- **型ヒント必須**: パブリック関数は引数・戻り値すべてに型を付ける
- **イミュータブル優先**: `dataclass(frozen=True)` / `pydantic.BaseModel(frozen=True)` を既定とする
- 関数 50 行以下、ファイル 400 行を目安、800 行を上限
- 早期 return でネスト 4 段以下
- マジックナンバー禁止 → `config/**/*.yaml` か定数モジュールへ

### テスト方針

- フレームワーク: `pytest` + `pytest-cov` + `pytest-asyncio`
- カバレッジ目標: **80% 以上**
- TDD を推奨 (RED → GREEN → REFACTOR)
- ディレクトリ: `tests/unit/`, `tests/integration/`, `tests/e2e/` の 3 階層 (Phase 1.5 から)
- カテゴリ:
  - **unit**: 純粋ロジック (整形、ID 生成、プロンプト組み立て、マスクフィルタ等)。常時実行
  - **integration**: Ollama / RSS / Discord / IMAP に **モック越しに** 接続するテスト。`@pytest.mark.integration` マーカーで分離可能
  - **e2e**: 1 件の記事を取得→投稿まで通すスモーク。Discord は dry-run モードで標準出力に流す

### ロギング

- `structlog` で **構造化 JSON ログ** を **stdout に出力** (12-Factor App)
- ファイルへの書き出しはしない。ログ転送は Docker / OS 側に任せる
- 履歴の検索性は **SQLite (run_history テーブル)** で確保
- ログレベル: `DEBUG/INFO/WARNING/ERROR`
- **ログに記事本文・認証情報・メールアドレス・トークンを出力しない** (機密マスクフィルタで二重防御)

---

## 4. セキュリティ要件

下記は **絶対** の制約。違反する PR は無条件で却下:

- [ ] **中国系 LLM / Embedding を一切使用しない**
  - 禁止対象: Qwen, DeepSeek, Yi, GLM, BAAI 系 (bge-*), m3e, ChatGLM, InternLM 等
  - 理由: 本ツールの CTI 業務では中華系 APT を主要な監視対象とするため、サプライチェーンリスクとして不適切
  - **同一 Ollama インスタンスへの中国系モデル同居は許容するが、コード側で防御を必須**:
    - `src/tools/llm_client.py` の `OllamaClient.__init__` でモデル名ホワイトリスト検証
    - 禁止プレフィクス (`FORBIDDEN_MODEL_PREFIXES`): `qwen`, `deepseek`, `yi:`, `yi-`, `glm`, `chatglm`, `internlm`, `m3e`, `bge-`, `bge_`, `baichuan`, `ernie`, `hunyuan`, `minimax`, `moonshot`, `kimi`, `skywork`, `telechat`, `xverse`
    - 違反時は `LLMForbiddenModelError` で起動を中止
    - テスト: `tests/unit/test_llm_client.py` に「禁止系モデル名で例外が出ること」を必ず含める
    - **denylist はコード所有 (config 化しない)**: UI でモデル選択を編集可能にしても、この denylist が
      3層 (モデル選択 dropdown の除外 / 保存時検証 / 構築時 `validate_model_name`) で弾く。
      モデル割当は能力ティア方式 (`src/tools/model_tiers.py`, 詳細は
      [docs/model_tier_architecture.md](docs/model_tier_architecture.md)) — ツールが step→ティアを
      決め、ユーザは UI で ティア→実モデル のみ割り当てる。禁止 denylist の SSoT は
      `FORBIDDEN_MODEL_PREFIXES` 一つ (dropdown フィルタは `is_model_allowed` が同じ定数を参照)
- [ ] **認証情報をコードにハードコードしない**
  - すべて `.env` (gitignore 済み) または OS キーチェーン経由
  - `.env.example` は値を空にしてコミット可
- [ ] **ログに機密情報を出さない**
  - 出力禁止: API キー, トークン, パスワード, メールアドレス, IMAP 認証情報, Discord webhook URL, 記事本文の生テキスト
  - 必要なら ID とハッシュのみ
- [ ] **外部 LLM は利用者の明示割当時のみ (2026-07-18 改訂)**
  - 既定は **ローカル LLM** (BUILTIN_MODEL_TIERS)。外部 LLM (Anthropic 等) は、利用者が
    API キーを設定し **かつ** UI「モデル」タブでティアに `anthropic:<model>` を
    明示割当した場合のみ、そのティアの処理に使われる (ツールが勝手に外部送信しない)
  - キーの保存先は **.env** (UI「設定 → モデル」から設定/削除可、即時反映・再起動不要)。
    **DB (config_store) には置かない** — 版履歴 + 日次 pg_dump backup に秘密が残留するため
  - どの LLM を使うかは**利用者に委ねる** — ローカルを捨てる判断ではない。埋込ティアは
    ローカルのみ (外部不可、validate_model_tiers が拒否)
  - 旧原則 (2026-07-18 以前):「クラウド LLM API には記事本文を送らない」— 利用者判断で改訂
- [ ] **依存関係は事前に出処を確認**
  - `uv add` 前に PyPI ページ・GitHub リポジトリの提供元を確認
- [ ] **シェル実行・ファイル書き込みを行うコードは最小化**
  - 外部入力をシェルに渡さない。やむを得ない場合は `shlex.quote` + 引数リスト形式 (`shell=False`)

---

## 5. ディレクトリ構造

```
kuebiko/
├── CLAUDE.md
├── README.md
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── config/
│   └── pipelines.yaml
├── prompts/
│   └── briefing/summarizer.j2
├── src/
│   ├── __init__.py
│   ├── main.py                # CLI entry (debug 用)
│   ├── config_loader.py
│   ├── logging_config.py      # structlog → stdout
│   ├── tools/                 # Phase 1 で実装済み (Phase X-1 で RSS 一本化)
│   │   ├── article_model.py
│   │   ├── direct_rss_source.py
│   │   ├── content_extractor.py
│   │   ├── llm_client.py
│   │   └── discord_publisher.py
│   ├── storage/               # Phase 1.5
│   │   ├── __init__.py
│   │   └── run_history.py     # SQLite (run_history + APScheduler jobstore)
│   ├── scheduler/             # Phase 1.5
│   │   ├── __init__.py
│   │   └── scheduler.py       # APScheduler ラッパ
│   └── ui/                    # Phase 1.5
│       ├── __init__.py
│       ├── app.py             # FastAPI app (lifespan で APScheduler 起動)
│       ├── routers/           # /、/history、/runs、/prompts、/config、/schedule、/health、/oauth
│       ├── templates/         # Jinja2 + HTMX
│       ├── static/            # htmx.min.js, style.css
│       └── services/          # ファイル編集・git auto-commit 等
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── scripts/
│   └── setup.sh               # OrbStack/Colima/Docker Desktop 検出 + .env 雛形複製
├── data/                      # (gitignore default-deny) SQLite, ChromaDB の実体。compose が起動時に作成
├── docs/
│   ├── deployment.md          # Docker 込みの運用ガイド
│   └── archive/
│       └── claude_code_prompts.md   # Phase 1 実装ガイド (役目終了)
└── .github/                   # (将来用)
```

各ディレクトリの責務:

- **config/**: 振る舞いを変えうる値はここに集約。**Web UI から編集 → atomic write**。ファイル直編集も維持
- **prompts/**: LLM プロンプト。**Web UI から編集 + 保存前 dry-run + git auto-commit**
- **src/tools/**: 汎用 I/O アダプタ (RSS, Discord, Ollama, IMAP, trafilatura, embedding 等。CTI ドメイン非依存)
- **src/grok/**: Grok レポート取込 (DOM 取得、JSONL parser、Briefing 変換)。Grok 情報は通常記事と同様に triage/routing される (専用 ch grok_daily は廃止済)
- **src/cti/**: CTI ドメインメタデータ (IOC 抽出、アクター名正規化、STIX 2.1 export)
- **src/storage/**: SQLite ラッパ。run_history / APScheduler jobstore
- **src/scheduler/**: APScheduler 統合。lifespan で起動・停止
- **src/ui/**: FastAPI + HTMX + Jinja2 の管理画面
- **scripts/setup.sh**: 初回セットアップ (Docker ランタイム検出、.env 雛形)
- **data/**: 永続データ。`.gitignore` で default-deny

---

## 6. 実装フェーズと現在地

| Phase | 内容 | 状態 |
|---|---|---|
| Phase 1 | RSS → 要約・翻訳 → Discord の最小ループ + 自動実行 (旧 Inoreader 経路は Phase X-1 で撤去) | **完了** |
| Phase 1.5 | Web UI (FastAPI+HTMX) + Docker 化 + APScheduler + SQLite history + ログ stdout 化 | **完了** |
| Phase 1.5b | SSE + DB 永続な live_log (タブ離脱に強い即時実行 UX) | **完了** |
| Phase 2 | Grok 対応 (IMAP 受信 + Playwright で本文取得) + 複数 source の統一インタフェース | **完了** |
| Phase 2.5 | Grok チャットページの DOM 抽出 (Playwright locator) | **完了** |
| Phase 2.6a | Grok 専用パーサ + LLM スキップ + セクション単位 Discord 投稿 | 完了 (JSONL 経路へ移行済) |
| Phase 3a | URL 正規化 + SHA-256 ハッシュベースの重複排除 (SQLite dedup_seen_urls) | 完了 |
| Phase 3b | Embedding + SQLite blob + numpy コサイン類似度 | 完了 (実機は embedding ティア割当後) |
| Phase 4 | CTI 観点メタデータ付与 (脅威アクター, MITRE ATT&CK, IOC) | 完了 (`src/cti/` で稼働) |
| Phase 5 | 責務別パッケージへの再構成 (完了) / CrewAI 化は保留・残骸撤去済 (2026-08-15) | 一部完了 |

> **Phase 1 における「翻訳」の位置付け**: 翻訳は独立ステップではなく、要約 (BLUF + 重要度 + カテゴリ) と同一の LLM プロンプト内で同時実行する (`prompts/briefing/summarizer.j2`)。

各 Phase の完了条件:

- **Phase 1**: 完了済み (`python -m src.main --dry-run` が通り、実環境で記事取得→要約→投稿が動作)
- **Phase 1.5**:
  - `docker compose up -d` で Web UI が `http://127.0.0.1:8001` (ホスト側) に起動
  - 06:30 JST に APScheduler が cron 起動して実投稿成功 (初日成功で完了。
    以降の運用観察は次フェーズと並行で行い、何かあれば対処)
  - Web UI 経由でプロンプト編集 → dry-run プレビュー → 保存 (git auto-commit) のサイクルが完結
  - 機密情報がログ・コミット・UI レスポンスに漏れていない
  - 重複投稿防止は URL 正規化 SHA-256 (dedup_seen_urls) + run_history (Phase 3 でベクトル類似度を追加)
- **Phase 2**:
  - `pipelines.yaml` の `source.type: grok_email` で Grok 通知メール経由の取得が動く
  - `scripts/grok_login.py` で初回ログイン → `data/playwright/state.json` が永続化される
  - `ArticleSource` Protocol + `build_source` ファクトリで複数 source が同一パスで処理される
  - `grok_unseen_only: false` で既読メールも対象にできる (テスト・初回キャッチアップ用)
- **Phase 3a**: 同一 URL の再投稿が起きない (UTM 等の揺らぎを正規化して SHA-256 で判定)
- **Phase 3b**:
  - 埋込モデルを設定 (先に `ollama pull`)。当時は `.env` の `OLLAMA_EMBED_MODEL`、
    **現在は UI「設定 → モデル」の embedding ティア** (`resolve_embedding_model`)
  - 中国系 embedding モデル (qwen-, bge-, m3e 等) は CLAUDE.md §4 と同じホワイトリストで起動段階で弾く
  - 同一インシデントを扱う英語記事と日本語記事のコサイン類似度が threshold (default 0.88) 以上で重複扱い
  - embedding は SQLite に float32 BLOB で保存、numpy で全件コサイン (個人運用規模では十分高速)
  - **ChromaDB は採用しない**: onnxruntime が macOS x86_64 で不在、依存重い、個人運用規模では過剰
- Phase 4: 投稿に「主アクター」「ATT&CK ID」「主要 IOC」が含まれる
- Phase 5: エージェント協調で再現でき、各エージェントの責務がコードに反映される + 責務別パッケージ再構成済み

---

## 7. 開発時の注意事項

- **新規モジュールは必ずテストとセットでコミットする** (TDD 推奨)
- **PR/コミット前のチェック**:
  - `uv run ruff check` / `uv run ruff format --check`
  - `uv run mypy src/ tests/` (strict は既定。**tests/ も型ゲート対象** — 2026-07-21 に
    tests/ を strict clean 化して以降、`src/` だけを見ると tests/ の型ドリフトが再蓄積して
    気付けない。`scripts/` 由来のアドホック import のみ pyproject の override で
    `ignore_missing_imports`。テストの mock 属性は `cast(AsyncMock, …)` 経由で読む /
    production へ渡す fixture の返り値型は実型のまま保つ / 意図的な型不一致のみ
    `# type: ignore[code]` を理由付きで付ける、が確立パターン)
  - `uv run pytest --cov=src`
  - 認証情報・本文がログに出ていないか目視
- **production DB は PostgreSQL** (Phase Y 以降):
  - `docker compose` の `postgres` service に named volume (`postgres_data`) でデータ永続化
  - 127.0.0.1:5433 で host から `psql` 可能 (5433 は既存 pg と衝突回避)
  - `DATABASE_URL=postgresql://kuebiko:${POSTGRES_PASSWORD}@postgres:5432/kuebiko` env で接続
  - SQLite (旧 `data/run_history.db`) は **legacy**、 dev / tests でのみ動作 (DATABASE_URL 未設定時 fallback)
  - macOS virtiofs WAL 衝突 corruption の根本解決 (2026-05-26 cutover)
  - host から SQL access する場合は `docker exec postgres psql -U kuebiko -d kuebiko` 経由 (2026-07-31 rename 以降 user も kuebiko。旧 `cti` role は nologin)
  - **バックアップ (Phase 0 F1)**: `backup` sidecar (postgres:16-alpine) が日次 `pg_dump -Fc` を
    `./data/backups/` に出力 (14 日 rotation)。`docker compose up -d backup` で起動、
    `docker logs backup` で成否確認。復元は `scripts/restore_db.sh <dump>`。
    named volume は corruption は防ぐが volume 削除/disk 障害は防げないため別 backup が必須
- **scheduler 起動時刻前後でのコンテナリビルドを避ける**:
  - cron 起動時刻の直前 / 実行中の `docker compose up -d --build` は in-flight
    subprocess を `cancelled by shutdown` で殺す。
  - **実行時刻の SSoT は DB (job_registry の schedule override)** — UI「実行管理」または
    `curl -s http://127.0.0.1:8001/api/v1/jobs | python3 -m json.tool` で next_run_at を
    確認してからデプロイする (この文書に時刻表を複製しない — 2026-07-16 に文書側の
    時刻表が実スケジュールと乖離していたため撤去)。
  - 不変の目安: 収集 (rss/scraper) は毎時 interval、朝夕ブリーフは 06:30/19:30 近傍、
    週次チェーンは深夜帯。**安全なデプロイ帯 = 毎時 :16-:29 / :35-:57、かつ朝夕ブリーフ
    (heavy) と深夜バッチ帯の前後を避ける**。
  - shutdown graceful wait (30s 既定) は実装済だが、長時間 LLM 推論中の救済は限定的
  - 影響時間: synthesis は最大 30 分 (PIPELINE_TIMEOUT_SECONDS 既定値)
- **新しい分析列 / entity_type / タグを追加する PR の必須 3 点セット** (有機的結合監査
  2026-07-12 の規約、R2/R3 恒久対処):
  1. **消費者を 1 つ以上同時に実装** (表示/フィルタ/KPI のいずれか。write-only 列を作らない)
  2. **fill-rate 週次監査へ登録** (`src/ui/services/fill_rate_audit.py` の METRICS に 1 行)
  3. **ラベルは SSoT を参照** (intent/nation=`src/cti/diamond_model.py`、sector=
     `config/cti/victim_sectors.yaml`、victim 国=`config/cti/countries.yaml`、日本判定=
     `src/cti/japan_relevance.py`、キーワード照合=`src/cti/keyword_match.py`。複製辞書を作らない)
- **投資の継続判定は「出荷した測定済みの利得」で書く。「作った部品の数」で書かない**
  (2026-08-22、較正格子の post-mortem 2R-H7): 「N 週で cutover した部品がゼロなら撤退」型の
  ゲートは、**判定当日に設計論拠だけの cutover を 1 件足せば自己充足する** (実際に起きた)。
  継続の条件は「本番の出力が測定可能に良くなった件数」に置く。
- **収集量を重要性の代理にしない** (第 3 回独立レビュー #4): 言及量の急増 (日次バースト等) は
  **収集網の観測**であって重要性の定義ではない。重要性の背骨は PIR → importance → channel
  (§13 設計原則 2)。バースト類を配信・重要度・記事選抜へ効かせると収集量が背骨を上書きする。
  境界は `tests/unit/test_burst_boundary.py` の import 関門が固定する (表示経路からのみ参照可)。
- **時系列の集計は「事象時刻」で行う。DB へ書いた時刻を事象時刻に使わない** (2026-08-22 根治):
  `article_entities.created_at` は entity 行を書いた時刻であり、バックフィル (再抽出 /
  別名昇格 / intent・axes backfill) は過去記事へ当日の日付で書くため事象時刻にならない。
  実測で言及の 44.3% が 1 日超・34.4% が 7 日超ずれ、週次 FC3 spike の 44% が偽陽性、
  日次バーストは単日最大 42 件の幻を出していた。錨の SSoT は
  `src/storage/repo_synthesis.py:_EVENT_TS_EXPR` (公開時刻・取込で上限・欠損は取込時刻)、
  fan-out 防止は `_DEDUP_ARTICLES` (articles は同一 article_id が複数行ありうる)。
  不変条件は `tests/unit/test_event_time_anchor.py` が固定する。
  **時間尺度を変えてデータ源を再利用するときは、必ず時刻意味論を再検証する** —
  週次 8 週窓では均されて見えない歪みが、日次窓では支配的になる。
- **デプロイは app service 限定で** (2026-07-05): `docker compose up -d --build` を全 service に
  かけると **tunnel が毎回再作成され quick tunnel の URL が変わる** (→ 毎デプロイ ops に
  新 URL 投稿 = 「URL が頻繁に変わる」の正体)。app のみ更新するときは
  `docker compose up -d --build kuebiko readonly` で tunnel を触らない
  (URL 安定・ops 静粛)。恒久安定 URL が要るなら `.env` に `CLOUDFLARE_TUNNEL_TOKEN`
  (+ `CLOUDFLARE_TUNNEL_HOSTNAME`) を設定して named tunnel 化 (再起動で URL 不変)。手順は
  [docs/mobile-access.md](docs/mobile-access.md)
- **detect ML と毎時の台帳再評価 (2026-09-17)**: 新規追跡の候補は ML (`config/models/detect_model.json`、
  審判ラベルで学習) が上位 30 件に絞り (09-18 に 15 → 30)、claim の選定は LLM に残す (`DETECT_ML_PREFILTER`、0 で従来)。
  更新すべき台帳が繰り越されないよう、毎時保守チェーンに増分再評価の段 (6 件/時、開設なし、
  `LEDGER_REASSESS_HOURLY`) を置く。**候補数 (30) / 開設上限 (8/run、ML 和集合込みで 12) / 更新上限 (12/run + 毎時 6) /
  報告の幅 (render 側) は別物**で、混同しない。設計と実測は
  [docs/research/llm_training/SYNTHESIS.md](docs/research/llm_training/SYNTHESIS.md) §47-48
- **2026-09-27 に入れた挙動の変更と戻し方** (実測の根拠は各 docs / コミット):
  | 変更 | 戻し方 |
  |---|---|
  | LLM の TTP は本文で裏付けられるものだけ (`cti/ttp_evidence`、Opus 盲検で精度 0.31→0.70) | `TTP_EVIDENCE_GATE=shadow` / `off` |
  | 関門より前の規則割当の弱い証拠を評価から外す (`weak_at`) | `UPDATE situation_evidence SET weak_at=NULL` |
  | 国家・地図・概況の集計から LLM medium の主題を外す (精度 61%)・LLM 経路で機関を主題にしない | `subject_gate.trusted_subject_clause` / `subject_actor.py` |
  | 台帳の 2 つの型 (actor は自動で閉じない) | `situations.track` を NULL に |
  | 軍事・政策・法執行の作戦名を campaign から外す | `config/cti/non_adversary_operations.yaml` |
  | 深掘りの選定 20→40 (要約の総量は固定) | `deep_dive_selector.DEFAULT_MAX_SELECT` |
  | チェーンの段の timeout で subprocess も止める | (戻さない — 並走の防止) |
  | 事象どうしの関係を画面に出す: 同じ出来事の関連 = 分類器 (`config/models/relation_model.json`、盲検で推定精度 0.85)・同じアクター = 信頼できる主題の共有 (精度 0.88、1 事象 5 件まで)。同一キャンペーン・共通の供給元は出さない | `relations.ENABLED_TYPES` を空に / モデルを消すと同じアクターだけ |
  | (09-29) fast ティア・ACH・深刻度の軸を s17 → **s21** (クラウド学習、int8 配備 27GB。要約の盲検 64:21・PIR の誤該当 48→19)。detect ML も s21 の軸で学習し直した版へ | UI「設定 → モデル」で前の版に戻す + `config/models/detect_model.json` を `data/model_backups/detect_model.json.bak_s17` から戻す |
  | (09-29) narrative ティア・事象ニュース・Spotlight・状況総括・深掘りを n17m30 / n17c → **n19** (n 系統合、クラウド学習 int8。事象 16:2・Spotlight 22:0・状況総括 14:0)、detect の LLM 段を n17c → s21 (開設すべき 9→18)。常駐 2 本 (s21 + n19)。深掘りは未学習のため n19 に接頭辞を付けない (`task_prefix.UNTRAINED_STEPS`) | UI「設定 → モデル」で前の版に戻す (n17m30 / n17c はディスクに保全) |
  | (10-02) 翻訳 (`article_translate`) と MITRE 同期の和訳 (`actor_sync`) を s21 → **`kuebiko-sft:26b`** (⚠ 素の 26B ではなく 09-04 の最初期の SFT 版。素の 26B は `gemma4:26b`、10-15 確認)。s21/n19 は未学習の自由文で JSON 形に崩れる (翻訳 637 件・PIR 別の要点が全件崩れていた)。壊れた訳は `body_translator.broken_translation_reason` が保存させない | UI「設定 → モデル」の step 上書きを外す |
  | (10-02) 投稿直前の重複判定に当たった記事を**分析に残す** (status='posted' / posted_channel=NULL / `duplicate_of`=一致先の根)。Discord へは流さない。規則の重複判定は新事実を見ず、続報が台帳・PIR・総括から消えていた | `DEDUP_KEEP_FOR_ANALYSIS=0` |
  | (10-02) triage の落選を理由つきで `triage_rejections` に記録 (180 日)。購読ソースの媒体詳細で件数と一覧 | (記録のみ — 戻す必要なし) |
  | (10-02) まとめ記事 (event_kind=roundup) を群化・統合・投稿直前の重複判定に参加させない。毎時の群化は候補の全件に種別を付ける (1 回 120 件まで、残りは持ち越し) | `models.ROUNDUP_KIND` の判定を外す (コード) |
  | (10-02) 地政学の SIR 4 本 (pir_geo_*、medium) を追加。triage が地政学・軍事・外交を理由に落とした記事を`kuebiko-sft:26b` (最初期の SFT 版) で判定し直す救済の段 (`triage_geo_rescue`、high にはしない) | `TRIAGE_GEO_RESCUE=0` / SIR は UI で無効化 |
  | (10-02) 統合の段で、頻出の名前 (当局・大手取引所の名前等) を共有し本文 cos 0.80 以上の別事象の組も ML で採点して統合する | `EVENTNEWS_MERGE_FREQUENT=0` |
  | (10-02) 台帳: 低確度の見立ての反転は 1 回では確定させない (確かな出所の反証が根拠なら即確定) | `LEDGER_FLIP_HYSTERESIS=0` |
  | (10-02) 配信の定型文 (「The post … appeared first on …」) だけの本文は要約しない / 高脅威の安全網を Web の日次ブリーフにも載せる | (コード) |
  | (10-10) M3b 段1: 配信の判断の時点で深刻度の軸を inline で取り (Step.SEVERITY_AXES、毎時の段と同じ client)、新しいルール (`cti/routing_v2.M3B_RULES`: alert=S3∧関連性 / vuln=S3∧悪用の根拠∧関連性なし / japan_watch / watch) の配信先を `routing_v2_shadow` に**記録だけ**する。本番の配信は v12 のまま。`GET /api/v1/routing-v2-shadow/summary` で新旧を比べる | `ROUTING_V2_INLINE_AXES=0` (inline の LLM 呼び出しだけ止まる) |
  | (10-10) 被害組織名の無い非インシデント記事 (advisory/press/research/opinion/tutorial) は被害国を付けない (`cti/victim_country_gate`)。監査 140 件で発火 30 件中 27 件が誤り (発行元の国・被害組織の無い記事)。直近 90 日の既存 2,319 行も遡及で空にした | `VICTIM_COUNTRY_GATE=0` / 既存行は `_backup_victim_country_gate_20261010_072036` から戻す |
  | (10-10) triage の PIR 駆動プロンプトに境界の型 A〜I の決まりを追加 (v4 境界除く 89.0→94.5%、i58) | 画面で triage_rubric を前の版に戻す (or boundary_rules block を空文字にして保存) |
  | (10-12) 被害組織名の**別名** (日本語 ⇔ 英語・略称・旧社名・親子会社) を logprob 判定 (gemma4:26b の 2 問の AND、`eventnews/org_identity.py`) で畳み、群化の `blocked_by_different_victims` と共有 entity に効かせる。**compose の既定は 1 だが、本番の `.env` が `EVENTNEWS_ORG_IDENTITY=shadow` で上書きしており、実態は記録のみ (10-22 利用者判断: 統合の適合率が目標未達のため、結合を増やすこの変更は保留して shadow を続ける)**。有効にするなら `.env` の上書きを外す + `EVENTNEWS_ORG_NORM=1` + UI「モデル」で `step:org_identity_judge`=`gemma4:26b` (SFT のままだと判定せず何も畳まない)。`shadow` は判定の記録のみ。確認用 98 組で誤同一視 0/62・回収 89%、replay 30 日で alert 重複の回収 22→25/52・新しい辺の適合 36/38 (暫定の正解は Sonnet、Opus 確認待ち) | `EVENTNEWS_ORG_IDENTITY=0` |
  | (10-11) alert の候補から当局の摘発・無力化・起訴・押収・逮捕の発表を watch に下げる関門 (語の条件のみ。logprob 判定は全件「はい」に偏り外した、185 件の正解で精度 10/11・回収 10/10) | `ALERT_EXCLUDE_LAW_ENFORCEMENT=0` (既定 off) |
  | (10-11) 過去 72 時間の alert と同じ事象 (群化 ML の確率 0.94 以上) の重複 alert を watch に下げる。Opus の正解 65 組で測り直すと誤抑止 12% (規則 contributed_new_facts を正解にした旧測定 1/21 は過小) のため既定 off。正解を週 ~10 組ずつ足して再評価 | `ALERT_DEDUP_EVENT=0` (既定 off) |
  | (10-11) 重複 alert の ML (木) の判断を `alert_dedup_shadow` に記録だけする (配信は不変)。集計は `GET /api/v1/alert-dedup-shadow/summary` (公開面には出さない) | `ALERT_DEDUP_SHADOW=0` |
  | (10-11) `decision_probe` が複数トークンに分かれる選択肢 (Gemma の「いいえ」= 「いい」+「え」) を最初のトークンで照合する (完全一致だと floor 扱いで P(はい)≈0.88 に化けていた。本番の Yes/No は各 1 トークンで影響なし) | (コード) |
  | (10-11) 薄い本文の関門 (`THIN_BODY_GATE`) と logprob の 2 段目 (`THIN_BODY_PROBE`)・SIR 別の要点の記事またぎ混同の関門 (`PIR_FOCUS_FIDELITY_GATE`) と文ごとの裏付け判定 (`PIR_FOCUS_CLAIM_GATE`)・被害国の語の関門 (`VICTIM_COUNTRY_GATE_KEYWORD`)・台帳の割当 ML への証拠確率の特徴量 (`ASSIGN_PROBE_FEATURE`) を on (compose の既定 1) | それぞれ `=0` |
  | (10-12) 翻訳の固有名詞・識別子・数字の化けを**検出**したら訳を保存せず (body_ja='' = 翻訳なし、画面は原文)、再試行もしない。修復はしない (語の修復は誤りが多かった)。標本 150 件で重い問題の回収 24/46・問題なしの誤発火 2/40 (5.0%)。on だと約 26% (過去 14 日の再現で 30.3%) の記事の訳が出なくなるため**既定 shadow** (log のみ)。`ident` = 識別子 (CVE・ハッシュ・IP 等) の化けだけで保存しない (14 日で 1.9%、識別子の検出は人手読みで本物 41/41 記事。語は約 5 割・数字は 1 割未満の精度で log のみ)。ログ `translate_fidelity_garbled` に mode / blocked / words / identifiers / numbers。再現は `scripts/eval_translate_fidelity_replay.py` (手順は冒頭 docstring)、本番の発火は `docker logs kuebiko 2>&1 \| grep translate_fidelity_garbled` を blocked 別に集計 | `TRANSLATE_FIDELITY_GATE=ident` / `on` で適用 / `off`。保存済みの '' は `UPDATE articles SET body_ja=NULL` で再翻訳キューへ |
  | (10-22) 被害国の取りこぼしを埋める決定論の規則 (`cti/victim_country_fill`): 速報のサイバー記事で LLM が国を出さなかった行に、日本の法人格・開示文の定型 (お詫びとお知らせ・第N報)・単一国の法人格 (Pty Ltd 等)・過去に同じ組織が同じ国で複数回記録された履歴、のどれかがあるときだけ足す。規則どうしが食い違えば足さない。raw に `inferred:<規則>` を残す。**既定 shadow** (ログ `victim_country_fill` のみ)。直近 30 日の空 4,249 行の大半は意図どおり (勧告・研究・論説は関門、リーク投稿の未特定被害者、法執行・政策) で、埋められるのは 90 日で 22 件。Opus 裁定 69 件 (補完 22 + 既存ラベルと一致した発火 47) で誤り 0・判断不能 2。遡及は `scripts/retro_victim_country_fill.py` (既定 dry-run、変更前を退避) | `VICTIM_COUNTRY_FILL=0` / 適用後の行は `UPDATE articles SET victim_country_iso=NULL, victim_country_raw=NULL WHERE victim_country_raw LIKE 'inferred:%'` |
  | (10-12) 関与国 (involved_countries) の付けすぎの関門 (`cti/involved_country_gate`): サイバー系カテゴリは空・4 件目以降は本文+要約での言及が 3 回未満の国を落とす (JP は対象外)。正解集 150 件で適合率 69%→87%・回収率 94%→82% (調整と測定が同じ標本)。**10-15 に on**: 調整に使わない Opus の正解 143 件で適合率 62%→82%・回収率 99%→94%・JP の誤落ち 0。しきい値の総当たりと協力語の例外は現行を超えず (data/eval/C1_icg_tune_report.md) | `INVOLVED_COUNTRY_GATE=shadow` / `0` |
  | (10-19) 台帳 claim の主語の関門 (`assessment/claim_guard.apply_subject_gate`) を **on**: 問いが対象国 (日本を含む問いは日本) を明示するのに新 claim が言及しない改訂は、新 claim を採らず**前回の claim を保持**して注記する (初回・前回と同文は対象外)。10-12〜10-19 の常設の問いの改訂 76 件で発火 1 件 (1.3%、on の基準 1 割未満) | `LEDGER_CLAIM_SUBJECT_GATE=shadow` (記録のみ) / `off` |
  | (10-19) 発表そのものが出来事の記事 (当局の発表・流出サイトへの掲載・発行元自身の勧告) は、報道日を発生日とみなす (`cti/publication_event`、basis reported → disclosed)。報道媒体が勧告を報じた記事・研究/論説/解説/まとめ・政府以外の X 投稿は対象外。層化標本 30 件で誤り・境界 3 件 → X を絞って 0。既存の 1,058 行も遡及した。あわせて `event_date_precision` (day/relative/unknown) を既存行へ埋めた | `EVENT_DATE_PUBLICATION_AS_EVENT=0` / 既存行は `_backup_publication_event_basis_20261019_054229` から戻す (`scripts/backfill_publication_event_basis.py` の docstring) |
  | (10-19) まとめ記事 (複数の独立した事案の束) の重要度を high にしない (上限 medium、`cti/roundup_detector`、見出しの型で判定。落とした件数はログ `roundup_importance_capped`) | `ROUNDUP_IMPORTANCE_CAP=shadow` (記録のみ) / `off` |
  | (10-19) 台帳 claim に台帳内部の文 (新着・今回・根拠への言及) を書かせない: プロンプトで禁止し、前回の claim に混入していれば持ち越さず書き直させる (書き直されなければ前回を保持) | `LEDGER_CLAIM_INTERNAL_RESET=0` |
  | (10-15) 伏字 (`*`) を含む被害組織名を群化・統合の結合と組織名照合から外す (`eventnews.grouping.build_join_entities`)。流出サイトの一覧の伏字ドメインで別々の被害者が同じ組織に見えて統合されていた。直近 14 日で 3 件止め、Opus 裁定で誤り 0/3。記事どうしの CVE 不一致の関門 (G1) は効果 0 で不採用 (`data/eval/C3_merge_gates_report.md`) | `EVENTNEWS_MASKED_VICTIM_GATE=0` |
  | (10-15) 統合で、片方だけが一括の勧告 (焦点の CVE の上限を超える記事) で根拠が CVE だけの辺を張らない (`eventnews.merge.is_bulletin_cve_bridge`)。14 日で 5 件止め Opus 裁定で誤り 0/5、正解 80 組で適合 78→81%。両端とも一括の勧告の辺は止めない (止めると誤り 3/13)。あわせて、事象の関係で見出しの CVE がハイフンの正規化差で照合できず CVE 経由の線が 0 本だった不具合を修正 (`relation_features.headline_cves`) | `EVENTNEWS_MERGE_BULLETIN_BRIDGE_GATE=0` |
  | (10-22) 統合の 2 つの関門 (適合率優先)。**ハブ関門**: 記事 8 件以上で CVE が 4 種超の事象 (複数 CVE を並記する大きな事象) へは、CVE だけを根拠にする辺のうち、その事象の主題の CVE (記事の 30% 以上が持つ CVE) と共有しない辺を使わない (`eventnews.merge.hub_core_cves` / `is_hub_minor_cve_edge`)。事象の CVE の和集合ではなく焦点の CVE で照合する。正解 80 組の再現で誤り 3 件を止め正しい統合 1 件も止める (調整と測定が同じ標本)・直近 14 日の統合 435 件で 3 件止まる (うち 1 件は正解で誤り、1 件は正解で正しい、1 件は未裁定)。**既定 off**、新しい Opus 正解で確認してから on。**月次レポート関門**: 見出しに期間 (YYYY年M月等) とレポートの語がある記事どうしで期間が違う辺を使わない (`roundup_detector.report_period`)。正解 80 組で誤り 1 件を止め正しい統合 0 件・14 日で 0 件、既定 on | ハブ: `EVENTNEWS_MERGE_HUB_GATE=0` (既定) / 月次: `EVENTNEWS_MERGE_REPORT_PERIOD_GATE=0` |
  | (10-16) detect の分けた形 (`DETECT_DECOMPOSE`、`assessment/detect_decompose.py`): 候補 (detect ML) → 塊化 (事象 id・強い鍵・cos) → 割当の判定器で追跡中の情勢に合流 → 残りの塊だけ LLM が「塊 → claim 1 文」(claim の固有名詞が塊の見出し・要約に無ければ開設しない関門つき)。**shadow で投入** = 本番の開設の後に予算 120 秒・書き手 6 件まで動き、結果は `detect_bundles` (variant=decompose_shadow) に記録だけ。あわせて detect の束ね・開設・合流・見送りを `detect_bundles` に常時記録 (180 日)。重複判定の救済は専用の `Step.DEDUP_CLUSTER_JUDGE` (triage の印を付けない) | `DETECT_DECOMPOSE=0` |
  | (10-12) 被害の件数の抽出 (`importance_v2.stated_victim_count`) を、17万7千・千万・億・全角数字・誤送付/表示の語・「対象は…」の文・関連記事行の除外に対応 (S3 の根拠 large_magnitude が増える) | (コード) |
  | (10-22) 深刻さ C (Jev 型の問い + ML) を **shadow** で記録: fast ティア (`Step.SEVERITY_AXES` の割当、いま s23i) に固定した 9 問 (`cti/severity_c.QUESTIONS`) を `decision_probe` で当て、付帯情報と合わせた RF (`config/models/severity_s3_model.json`、`scripts/train_severity_s3_model.py`、学習時のモデルを `probe_model` に持つ — 割当が変わったら**そのモデルの確率で学習し直す**。不一致は記録に `model_match=0`) で P(S3) を `severity_c_shadow` に残す。非サイバー・派生・まとめ記事は A の決まりごとで先に外し問いを当てない。毎時の軸の段の続き (予算 150 秒、約 5.5 件/時 × 3.8 秒)。**配信・重要度は不変**。集計は `GET /api/v1/severity-c-shadow/summary` (公開面には出さない)。同日、まとめ記事に深刻さを付けない設計を確定 (正解集の S3 のまとめ 4 件は評価・学習で外す) と、LLM の recap だけが根拠の侵害記事を派生にしない `importance_v2` 版 `2026-10-22.10` | `SEVERITY_C_SHADOW=0` (記録も問いも止まる) / 版は `RULE_VERSION` を戻す (recap は title を渡さなければ従来どおり派生) |
  | (10-22) 常設の問いの根拠の候補の範囲 (`assessment/standing_scope`): 国家の活動を問う問い (seed・subject が敵性国家) は国家系アクター (`is_state_actor`) に結び付く記事だけ (辞書 nation=ru の ransom_group だけが根拠の記事・関与国の言及だけの記事は外す)、破壊・妨害の問いは非サイバー category (geopolitical/policy/other) を外す (世界の CI へのサイバー事案は間接の根拠として残す)。直近 30 日の根拠 403 行のうち 129 行が候補から外れ、改訂 358 件中 110 件が候補 0 件になる (q-ru 36・破壊 54)。調査は data/eval/pir_answer_evidence_2026_10_22.md | `STANDING_EVIDENCE_SCOPE=0` |
  | (10-22) 統合の辺に記事対の時間差の上限 60 日を戻した (`merge.plan_merges(max_gap)`)。09-21 に「判定が厳しいので不要」と外したが、判定が緩む方向 (ML のずれ・ハブへの吸収) の歯止めが無かった。当時の実測で 30 日と無制限の差は畳む事象 1,015 対 1,038 (2%) なので 60 日なら正しい統合をほぼ失わない。ただし A–B・B–C が各 60 日以内なら A–C が 120 日離れても推移閉包で結ばれる (辺単位の上限) | `EVENTNEWS_MERGE_MAX_GAP_DAYS=0` (無制限) |
  | (10-22) 統合の判断を `event_merge_decisions` に記録 (吸収した事象 1 つ = 1 行: 辺の記事対・cos・ML 確率・出どころ・救済か・その回の関門の設定)。**統合を戻す操作は `eventnews_merge_undo.undo_merge` 1 か所** (吸収側の `merged_into` を外す・祖先から `retro_merge` の行を消す・祖先の状態を作り直し本文を再生成)。`scripts/undo_event_merge.py` は既定 dry-run (`--list` で根拠を見てから `--apply`)。遡及分割と状態の作り直しを共通化。週次監査に記録の被覆の 1 行 | (記録のみ — 戻す必要なし。誤統合は undo で戻す) |
  | (10-22) 統合の ML の入力を学習時の形に: 種別を DB の実値で渡し、要約埋込の欠けを毎時 500 件まで埋める (`eventnews_merge_job.fill_summary_embeddings`)。**既定 off**。正解 80 組の再現 (`scripts/merge_input_replay.py`、ハブ関門と分けて): 種別 other 固定 → 実値で誤統合 9/13 → 9/13・正しい統合 56/62 → 54/62 = **効果なし** (再現した記事の要約埋込の被覆は 97% で、欠けの影響は測れていない)。種別の精度 (付与モデルの混在) と学習時の分布を確かめてから有効化を判断する。ハブ関門 (別途 flag) は同じ再現で誤統合 9 → 6・正しい統合 56 → 55 | `EVENTNEWS_MERGE_REAL_KIND=0` (既定) |
  | (10-22) 深掘りの時事性の「関連事案」を dedup_key でなく事象の根で数える (`deep_dive_selector._cohort_key`)。dedup_key は 1 群 1.01 記事で実質何も束ねない最も粗い群化だった。事象に入っていない記事だけ dedup_key に退避 | (コード) |
  | (10-22) 「同じ事象」の定義を固定: 同じ事案の第 n 報 = 同じ事象 / 修正 ⇔ 悪用・KEV 追加と行政指導など派生 = 別の事象で関係 (系統) で結ぶ ([docs/event_identity_definition.md](docs/event_identity_definition.md)、判定のプロンプト・正解集・画面文言との食い違いの一覧つき)。群化の回収率の目標は 80% 以上 (統合の適合率 95% 以上を優先)。compose の `EVENTNEWS_ORG_IDENTITY` の既定を `.env` に揃え shadow に | (文書のみ。compose の既定は `EVENTNEWS_ORG_IDENTITY=1` で戻る) |
  | (10-22) 常設の問い「日本の組織に対する金銭目的 (犯罪組織) は悪化しているか」(対象は重要インフラ以外も含む `SCOPE_ALL_ORGS`) の台本 (型 = 趨勢 × financial、直接 = 日本被害 (全業種) / 間接 = 世界の CI) を `scripts/promote_standing_questions.py` の **opt-in** (`--only criminal-financial`、既定の一覧には入れない) に用意。根拠の候補は `STANDING_EVIDENCE_SCOPE` の型で「サイバーの事案 ∧ 非国家の犯罪アクター (ransom_group / spider)」に絞る (国家系・ハクティビスト・アクター無しは外す)。**本番へは未投入** (DB が SSoT。投入手順は報告書)。投入後の戻し: UI/API `POST /api/v1/config-history/standing_questions/revert` で前の版 + 台帳の situation を `status='closed'` に | 問い: 上記 revert / 絞り込み: `STANDING_EVIDENCE_SCOPE=0` |
  | (10-22) 日本の同盟・同志国の区分を国の SSoT に置く (`config/cti/countries.yaml` の `alliance: ally` = US・GB・AU・CA・NZ・KR・TW・PH、根拠は同ファイル冒頭。`ALLIED_NATIONS` はここから導出、値は不変)。常設の問いの根拠に**被害国で**「直接 (日本・同盟国) / 間接 (それ以外) / 不明」の印を付ける (`assessment/standing_proximity`・`cti/alliance`。列は足さず導出)。消費者 = 問いの画面の件数 (`evidence_proximity_30d`)・fill_rate 監査 (`standing_evidence_victim_country`)。**印だけで答えの重みは変えない**。直近 30 日の根拠 403 行は 直接 113 / 間接 107 / 不明 183 (被害国が空)。集計は `scripts/standing_proximity_report.py` (SELECT のみ) | `STANDING_EVIDENCE_PROXIMITY=0` (印を出さない) / yaml の `alliance` 行を外す |
  STIX 2.1 の書き出しは [docs/stix_export.md](docs/stix_export.md)、準拠は OASIS 検証器のテストが固定する
- **評価 (s20 等) を本番コンテナの中で回さない** (2026-09-27): GPU を取り合って RSS 取得が段の上限を超え、
  後続の段が「実行中」で 0 秒成功扱いになった。評価は使い捨てコンテナで、本番を止めるか空き時間に流す
  (`data/mlx/eval_s20.sh` の型)。Ollama の並列スロット (`OLLAMA_NUM_PARALLEL`) は本番では 1 のまま
  (大記事で利得ゼロ・timeout 悪化、2026-08-17)
- **モデル変更は CLAUDE.md / `.env.example` を同時更新**
- **Phase 1 の LLM 暫定運用**: Gemma 4 31B Dense が Ollama に未公開の間は `gemma3:27b` で代替可
- **依存追加は最小化**: 標準ライブラリで足りるものを安易にライブラリ化しない (YAGNI)
- **Discord Webhook の取り回し**: チャンネル別に `DISCORD_WEBHOOK_PRIORITY` / `_DAILY` / `_RESEARCH` / `_SYSTEM` の 4 本を `.env` に保持。`AppConfig.discord_webhooks: dict[Literal["priority","daily","research","system"], str]` のマッピングとしてロード
- **コミット前に `data/`, `.env` が含まれていないことを確認** (gitignore + `git status` 目視)
- **コーディングは日本語コメント可、識別子は英語**

---

## 8. 参考資料

### モデル / ランタイム
- Ollama: https://ollama.com/
- Gemma 公式: https://ai.google.dev/gemma
- multilingual-e5-large-instruct: https://huggingface.co/intfloat/multilingual-e5-large-instruct
- OrbStack: https://orbstack.dev/

### ライブラリ
- uv (Astral): https://docs.astral.sh/uv/
- FastAPI: https://fastapi.tiangolo.com/
- HTMX: https://htmx.org/
- APScheduler: https://apscheduler.readthedocs.io/
- trafilatura: https://trafilatura.readthedocs.io/
- Playwright for Python: https://playwright.dev/python/
- ChromaDB: https://docs.trychroma.com/
- structlog: https://www.structlog.org/
- CrewAI: https://docs.crewai.com/

### CTI フレームワーク
- MITRE ATT&CK: https://attack.mitre.org/
- STIX 2.1: https://oasis-open.github.io/cti-documentation/stix/intro
- BLUF (Bottom Line Up Front) ライティング: https://en.wikipedia.org/wiki/BLUF_(communication)

---

## 9. やらないこと (Out of Scope)

- **外部 LLM への無断送信**。外部 LLM (Anthropic 等) は利用者がティア割当で明示選択した
  場合のみ使用可 (§4、2026-07-18 改訂)。既定はローカル LLM で完結
- **中国系モデル / Embedding の利用** (§4 参照)
- **Discord 以外への配信** (Slack / Teams / メール等)。必要になった時点で別途検討
- **マルチテナント化 / 他ユーザーへの提供**。本プロジェクトは個人運用専用
- **収集した記事の再配布**。要約と引用 URL に留める — **匿名で読める公開面 (Tier0) の話**
  (2026-08-29 線引きを明確化)。ローカル (Tier2) だけが本文を持つ。**アドバンスド (旧「写し」、
  2026-10-05 に公開サイトと同じドメインの `/app` へ統合) は 2026-10-04 に匿名公開へ変えた
  (利用者判断)** ので本文を持たない (要約・出典 URL のみ)。あわせてメモ・購読ソース・配信
  チャンネル・実行時のフラグ・Grok も写さない (PIR・SIR・将来予測は出す)。
  関門は面ごとに独立している (標準面は `scripts/export_public_site.py` の
  `_FORBIDDEN_KEYS`、アドバンスドは `scripts/export_mirror.py` の `_final_gate`: 本文・メモの欄や
  外した endpoint が 1 つでも残れば書き出しを失敗させる) ため、片方を緩めても他方は緩まない。
  **同一ドメインへ統合してもこの関門は無関係** — `/app` 配下はあくまで `data/mirror_dist`
  (アドバンスド専用の書き出し) をそのまま置くだけで、標準面の書き出しとは混ざらない
- **クラウドへのデプロイ** (AWS/GCP/Azure)。MacBook 上での常駐運用に限定
- **Web UI (write 可能 instance) の外部公開**。port 8001 の full instance は 127.0.0.1 のみバインド、LAN/外部からの到達を不可とする (§12)。読み取り専用 instance (port 8002 + READ_ONLY=1, write API は middleware で 403 固定) のみ Cloudflare Tunnel 経由の外部公開を許容する
- **Web UI の認証実装** (Phase 1.5 では不採用、§12 のセキュリティ境界で防御)
- **launchd をスケジューラに使うこと**。スケジューラはアプリ内 APScheduler、コンテナ起動は
  Docker ランタイムの auto-start に任せる。例外: ホスト補助サービス (claude-code-bridge) の
  常駐化は LaunchAgent を使う (2026-07-19、`scripts/install_claude_bridge_launchagent.sh`)
- **リアルタイム配信**。1 日 1 回 (06:30 JST) のバッチが運用要件

---

## 10. 解決済みの方針判断 (記録)

過去の検討で結論が出た事項。再検討時にコンテキストを失わないよう履歴として残す。

### Docker 化 (採用、Phase 1.5)
- **動機**: 常駐 Web UI のホストとして必要となった
- **構成**: ホストネイティブ Ollama (Metal 加速) + 単一 Docker コンテナ (FastAPI + APScheduler)
- **launchd 不採用の理由**: cron は APScheduler、起動は `restart: unless-stopped` + Docker auto-start で完結。launchd は冗長
- **コンテナランタイム**: OrbStack 推奨 (Apple Silicon ネイティブ、起動 2 秒、メモリ ~200MB)。Colima / Docker Desktop も可
- **Linux 移植性**: ホストネイティブ Ollama を Linux ネイティブ Ollama に置き換えるだけで `docker compose up -d` がそのまま動く

### Web UI 採用 (採用、Phase 1.5)
- **動機**: 設定編集 / 動作管理 / ログ可視化 / プロンプトチューニングを長期運用で楽にする
- **スタック**: FastAPI + HTMX + Jinja2 (Streamlit / SPA は不採用)
- **判断根拠**: 3〜5 年運用、Phase 5 で CrewAI 連携 → API 再利用性、テスト容易性が決め手

### 外部 LLM の開放 (採用、2026-07-18)
- **動機**: 品質向上余地の大きい処理 (synthesis narrative / 分析チャット) で外部 LLM を選べるようにする
- **原則**: どの LLM を使うかは**利用者がモデルティア画面で選ぶ**。既定はローカル Ollama のまま
  (ローカルを捨てる判断ではない)。外部はティアに `anthropic:<model>` を明示割当した場合のみ
- **実装**: `src/tools/anthropic_client.py` (LLMClient 抽象の Messages API 実装、httpx 直・SDK 非依存)。
  factory は `model_tiers.build_llm_for` の prefix dispatch。API キーは `.env` の `ANTHROPIC_API_KEY`
- **不変の制約**: 中華系 denylist はプロバイダ横断で維持 / 埋込ティアはローカルのみ /
  API キー・プロンプト本文をログに出さない

### robots.txt の尊重 (保留)
- **現状**: `src/tools/content_extractor.py` は意図的に無視 (個人利用前提、配信は要約 + 引用 URL のみ)
- **再評価のトリガー**:
  1. 取得先サイトから明示的に「クローラ禁止」連絡があったとき
  2. 取得頻度を上げる (1 日 1 回 → 数時間に 1 回など) ことを検討するとき
  3. プロジェクトを公開する形態に変えるとき
- **着手するなら**: Phase 5 まで。`robotparser` (stdlib) で `kuebiko-bot` の crawl 可否をキャッシュ込みでチェック

---

## 11. ローカル管理画面 (Phase 1.5)

### スコープ
- **ダッシュボード** `/`: 直近 N 日の実行成否、投稿件数推移、LLM 平均応答時間、抽出失敗率、PIR Coverage widget
- **実行履歴** `/history`: SQLite の run_history を ページ付きで一覧。重要度・カテゴリでフィルタ。`/runs/{id}` 詳細ページへリンク
- **即時実行** `/runs`: dry-run プレビュー、本番投稿。**run_id 中心の API** で `/runs/{id}` 詳細ページへ遷移
- **実行詳細** `/runs/{id}`: live_log を SSE でリアルタイム配信。タブ離脱・再起動でも DB から復元。Last-Event-ID で再接続
- **プロンプト編集** `/prompts`: `prompts/**/*.j2` の編集。保存前 dry-run 必須、`*.j2.bak` 自動生成、git auto-commit
- **設定編集** `/config`: タブは**種類で 3 群**に分ける (2026-08-02 整理。同列に並べると「設定・エスケープハッチ・記録」が混ざって見通しが落ちる):
  **【設定】** 接続 (webhook/IMAP/LLM/モバイル公開 = **外部と繋ぐものだけ**) / モデル (ティア・接続先) /
  プロンプト / システム (LOG_LEVEL・TZ・**ホスト復旧 watchdog** = この端末固有) —
  **【記録】** 履歴・監査 (設定変更の版履歴 + Cloudflare Access のアクセス監査) —
  **【上級】** 設定ファイル (raw YAML。2026-06-10 に「raw 直編集は不適当」と方針決定済のため末尾へ隔離)。
  **配置の判断基準 (2026-09-16 改訂) = 読む面と書く面を分ける**:
  **専用ページは閲覧のみ** (状況・生成物・KPI・根拠)、**定義の変更はすべて設定カテゴリ**。
  - 移設対象は「**定義**の変更」= 作成/編集/削除/有効化/しきい値。
    **実行・再生成は閲覧面に残す** (古いと気付いた面でその場で直せることに意味がある —
    2026-09-16 に 18 日前の spotlight を SIR 詳細で見つけて再生成した実例)。
    **承認キュー・メモも閲覧面に残す** (判断対象を見ながら行う作業で、定義の変更ではない)
  - 各編集画面は **①効果 (preview) ②参照関係 ③成績 (KPI)** を揃えて出すこと。
    これが「対象を見ながら直す」の実体で、閲覧ページ固有のものではない
  - 閲覧ページからは編集への**リンクを置く**。読みから書きへの移行が明示される
  - 旧基準 (2026-08-02〜09-16) は「対象画面を持たないものだけが設定に残る」だった
    (マッチリストは配信ルールの語彙なので情報フローへ、等)。**廃止した理由**: 移設の根拠
    だった「見ながら直す」が実装されていなかった — マッチリスト編集画面は配信ルールを
    表示しておらず、保存後にキャッシュを無効化するだけだった。理屈だけが残り、
    変更の入口が主題ごとに分散していた。移設計画は
    [docs/settings_consolidation_plan.md](docs/settings_consolidation_plan.md)。
  - ⚠ **画面の所属は `frontend/src/components/nav.ts` の `NAV_GROUPS` が SSoT**。
    ルートの有無では判定できない — 情報フロー (`/app/flow`) は既に「設定」グループ内で、
    配信ルール・チャンネル・マッチリストを内蔵し**新基準に既に適合している**
    (2026-09-16 に Claude が専用ページと誤分類し、利用者が訂正).env タブは 2026-07-24 廃止 — 接続系キーは接続タブ+各対象画面 (チャンネル=情報フロー / IMAP=購読ソース / Ollama・外部LLM=モデルタブ) の双方から編集でき (同一 API)、.env ファイルは不可視の保存層として存続 (docs/deployment.md §7)
- **PIR 管理** `/pir`: Priority Intelligence Requirements の CRUD + KPI 表示 + LLM-assisted 構造化。詳細は [docs/pir_system.md](docs/pir_system.md) と §13
- **スケジュール管理** `/schedule`: 次回実行時刻表示、一時停止/再開、cron 式変更
- **死活監視**: 専用ページ `/health` は 2026-07-24 廃止。疎通状態は各対象画面 (チャンネル/購読ソース/モデルタブ) の疎通ドット + ダッシュボードの死活 widget に統合 (API `/api/v1/health-status` は存続)

### 技術スタック
- FastAPI (uvicorn) + HTMX + Jinja2 + APScheduler + SQLite
- 単一 Docker コンテナで運用、Streamlit / SPA は不採用
- 認証なし (単独 Mac / 単独ユーザ前提、§12 のセキュリティ境界で防御)

### live_log 設計 (Phase 1.5b)

- **DB 永続化**: subprocess の stdout を 1 行ずつ `run_logs(run_id, seq, ts, stream, line)` に書き込む
- **SSE 配信**: 実行中は `RunRegistry` (in-process pub/sub) で接続中のクライアントに増分配信
- **再接続耐性**: HTML5 EventSource の `Last-Event-ID` ヘッダで途中の seq から再開
- **クラッシュ復旧**: 起動時に `status='running'` のまま残った run を `failed` に倒す
- **retention**: 30 日より古い `run_logs` を起動時に purge
- **行数上限**: 1 run あたり 5000 行で打ち切り、超過時は `log_truncated=1` を立てる
- **行サイズ上限**: 8KB 超は末尾を切り詰め `... [truncated]` を付与
- **Phase 5 への投資**: `RunRegistry` Protocol を切ることで複数ワーカー化時に Redis pub/sub 実装に差し替え可能 (今回は in-memory のみ)

---

## 12. Web UI セキュリティポリシー (Phase 1.5 必須)

- [ ] **127.0.0.1 のみバインド**: docker-compose の `ports` は `127.0.0.1:8001:8000` 固定 (ホスト 8001 → コンテナ 8000)。LAN/外部公開禁止
  - **例外: readonly mobile 公開 (Phase Diamond verify-mobile)**: 別 service `readonly` を `127.0.0.1:8002:8000` で起動 (full instance とは別 container、`READ_ONLY=1` 環境変数)。FastAPI middleware が POST/PUT/PATCH/DELETE を **すべて 403** で block。Cloudflare Tunnel が `127.0.0.1:8002` のみを HTTPS で外部公開し、外部から到達できるのは **閲覧専用 API のみ**。write 不可は公開プロセス側で保証 (認証ゲートではなく、80 本のハンドラに
    到達する **前** の関門。⚠ DB・config ボリュームは full と共有しているので
    「物理的隔離」ではない — 正確には「公開されているプロセスが write を受け付けない」)。詳細手順は [docs/mobile-access.md](docs/mobile-access.md)
  - **層は「鮮度の契約」で分ける (2026-08-29 再定義)**。認証の強さではなく、
    *いつ時点の情報か* が層を決める。アカウントも層ごとに分ける (Access のポリシーは
    ホスト名単位なので、運用画面用と別アカウントを当てられる):
    | 層 | 面 | 鮮度 | 到達 |
    |---|---|---|---|
    | Tier0 | 公開面 (Pages、標準 `/news` + アドバンスド `/app`) | 標準は毎時・アドバンスドは 3 時間ごと | 匿名 |
    | Tier2 | リアルタイム (運用画面) | 常に最新 | 127.0.0.1 / tunnel (運用アカウント) |
    - ⚠ **2026-10-05: アドバンスド (旧「写し」) を公開サイトと同じドメインの `/app`
      配下へ統合した**。旧 Tier1 (別ドメイン `mirror.kuebiko.example` に匿名公開) は
      廃止 — 旧ドメインは `scripts/deploy_mirror_redirect.sh` が配る 301 リダイレクト
      だけが残る (`/app/* → <公開ドメイン>/app/:splat`、それ以外は `/app` へ)。配信は `scripts/deploy_site.sh`
      が標準面 (`data/public_site_dist`) とアドバンスド (`data/mirror_dist`) を 1 つの Pages プロジェクトへまとめて出す。
      **アドバンスドの静的ファイルは `/adv/`、画面の URL は `/app/…`** (`/app/* → /adv/` の
      書き換え)。⚠ `/app/` の下にファイルを置かない — Pages の書き換えは実在のファイルより先に
      当たり、資産まで HTML に化けて真っ白になる (2026-10-05 本番で確認)。`/app/* → /app/index.html`
      は行き先が同じ規則に当たるため「無限ループ」として捨てられる。定期配信の 2 本は
      ロック (`data/.deploy_site.lock`) で 1 本ずつ走る。片方の LaunchAgent
      (標準=毎時 `scripts/install_public_site_launchagent.sh` / アドバンスド=3 時間ごと
      `scripts/install_mirror_launchagent.sh`) が動いても、もう片方の dist は直前のまま
      組み込まれる — いずれかの dist が無ければ配信自体を失敗させる (半分だけの
      配信を防ぐ)
    - **アドバンスドは Mac 不達時の継続**が目的。ライブの代わりではない。画面上部に
      「○○時点の情報」を常時出す (`MirrorBanner`) — **古いことではなく、古いと
      分からないことが危険**。書き出しは `scripts/export_mirror.py`
    - tunnel は**層ではなく到達手段**。Tier2 に遠隔から届くための経路で、
      畳まない (将来の遠隔 write 要件と、写せない情報 — host-watchdog の
      現在状態など — のために要る)
    - **標準とアドバンスドはヘッダの「標準 / アドバンスド」切替で互いに導線を持つ
      (2026-10-04 導入。2026-10-05 に同一オリジン化したため、導線は常に相対パス
      `/news` ⇄ `/app/eventnews` で有効 — ビルド時のオリジン注入は不要になった)**。
      id をまたいだ詳細同士の対応付けはできない (標準面は事象ニュース単位の id、
      アドバンスドの `/app/article/{id}` は構成記事単位の別 id 空間で、アドバンスド
      自体も事象ニュース詳細への URL ルートを持たない) ため、常に相手サイトの
      トップへ落とす (`frontend/src/utils/siteSwitch.ts`)。運用画面 (Tier2) への
      導線はここには無い — Tier2 は認証済み運用者専用で、匿名公開面から直接は出さない
    - Access の Path 設定は公開ドメイン側に影響しない — 公開 Pages プロジェクトは
      `/auth` を一切持たず (匿名公開のみ)、Cloudflare Access は運用画面専用ドメイン
      (`ops.kuebiko.example`) の `/auth` にだけ適用する。⚠ 公開ドメインに Access を
      掛けると標準・アドバンスド両方が匿名で読めなくなる (§12 の前提が壊れる)
  - **公開 instance の到達範囲は 3 層 (2026-08-01)**。SSoT は `src/ui/read_only_policy.py` **1 箇所**:
    **Tier0 匿名** = 閲覧系 read API と SPA / **Tier1 認証済み (Cloudflare Access)** = 運用系 read API
    (`READ_ONLY_GET_DENYLIST`: ジョブ計画・設定・プロンプト・ルーティング・レビューキュー) の閲覧 +
    ジョブ即時実行 + 分析チャット・記事翻訳 / **Tier2 ローカル専用** = それ以外の全 write。
    - frontend の `nav.ts` `fullOnly` は**表示上の隠蔽にすぎない** — 遮断の実体は常にサーバ側の
      denylist。新しい運用系 API を足したら denylist にも 1 行足す (でなければ公開面に露出する)。
    - Tier1 の write は**ジョブ即時実行 1 つだけ**。readonly は scheduler を起動しないため、
      認証済みの `POST /api/v1/jobs/{id}/run` のみ full instance (`FULL_INSTANCE_URL`) へ
      narrow proxy する。**write の実行主体は常に full** で §12 の境界は不変。
    - **遠隔 write (2026-08-29 に開放、`READ_ONLY_ALLOW_REMOTE_WRITE=1`)**: 認証済みの
      設定変更を遠隔から行えるようにした。既定値は 0 のままで、`.env` で明示的に開ける。
      開けると防御が「公開プロセスが write を持たない」という構造から「Access の設定が
      正しいこと」へ移る。その移動を、以下の 4 つで受け止めている:
      - **線引きは書き先が DB かファイルか** (利用者判断)。DB 由来の運用設定は
        版履歴が残り revert できる (`config_history._KNOWN_KEYS` がその名簿)。
        ファイル由来 (.env / raw YAML / .j2 直編集 / actor_aliases.yaml /
        Playwright state) は版管理が無く、readonly では :ro マウントで物理的にも
        書けない。許可は `REMOTE_WRITE_ALLOWLIST` に **1 行ずつ明示**し、
        **未登録は拒否** (fail-closed) — 見落としは「遠隔で書けない」に倒れる。
        ⚠ 前方一致でまとめない。`/api/v1/channels` を前方一致で許すと
        `/api/v1/channels/{id}/webhook` (資格情報) まで巻き込む
      - **実行主体は常に full instance へ転送** (`_proxy_write`)。readonly 自身に
        書かせると (a) scheduler 不在でジョブ計画の変更が届かない (b) :ro マウントで
        ファイル書き込みが落ちる (c) §12 の構造が崩れる。転送なら readonly は
        **口を持つだけで能力は持たない**
      - 資格情報 (`CREDENTIAL_WRITE_PATHS`: anthropic-key / claudecode-token /
        endpoint-key / ollama-url / channels の webhook) は **flag に関わらず遮断**。
        webhook URL は URL の形をしているが**それ自体が資格情報**
      - すべての遠隔 write を `access_audit` に記録してから通す。名簿に無い write の
        拒否も記録する。**追えない遠隔 write は開いていないのと同じくらい危険**
      frontend の `canEditOperationalConfig` は**表示上の出し分けにすぎない** —
      遮断の実体は常にサーバ側の名簿 (`fullOnly` と同じ関係)
    - 認証は Cloudflare Access (`/auth/*` にのみ適用) の JWT を JWKS 検証 (`src/ui/services/cf_access.py`)。
      **fail-closed** (署名不正・期限切れ・aud/iss 不一致・鍵取得失敗はすべて未認証)。
      `.env` の `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` を消せば Tier1 が消えて従来挙動に戻る
    - **認証の監査証跡は DB (`access_audit`) に残す (2026-08-02)**。記録するのは
      `authenticated` (成功) / `rejected` (資格情報を提示したが検証失敗) / `tier1_write`
      (即時実行) の 3 事象。**成功も必ず記録する** — 失敗しか残さないと「認証層が
      使われている」と「一度も使われていない」を区別できない (実際に誤判定した)。
      **stdout だけでは監査にならない** (デプロイのコンテナ再作成とログローテーションで
      消える。同日 40 時間分を実際に失った)。§4 により **email は保存しない**
      (識別は subject の SHA-256 先頭 12 桁)。参照は `GET /api/v1/access-audit`
      (denylist 対象 = 公開面には出さない)、retention 180 日
- [ ] **編集対象 allowlist**: `prompts/**/*.j2`, `config/**/*.yaml`, `.env` のみ。それ以外のファイルは Web から編集不可
- [ ] **シークレットマスク**: `.env` 表示時に常に「先頭 4 文字 + ***」(structlog の機密マスクと同形式)
- [ ] **編集前バックアップ**: ファイル編集前に同階層 `*.bak` を必ず生成
- [ ] **入力検証**: pydantic スキーマで検証 → atomic rename で書き出し
- [ ] **危険操作の二段階確認**: 設定保存・本番投稿は確認ダイアログ必須
- [ ] **編集履歴は DB / .bak で残す (git auto-commit は廃止)**: 稼働中のアプリが開発 git
      リポジトリにコミットするのは anti-pattern のため廃止。**運用 config (routing / source_quality /
      PIR / channels / match_lists / ソース feeds・watchers・scrapers) は DB (config_store,
      app_config_versions) を SSoT とし保存ごとに版履歴を残す**。yaml は初回 seed 専用 (git 追跡 =
      ツール同梱の既定)。履歴/revert は `/api/v1/config-history/{key}` (whitelist は config_history.py
      `_KNOWN_KEYS`)。その他のファイル編集 (prompts/.env) は atomic write + `.bak` のみ (git 非介在)
- [ ] **死活通知**: 1 日 1 回 Discord `#system` に UI コンテナの稼働状況を送信
- [ ] **コンテナ非 root 実行**: Dockerfile で `USER 1001:1001`
- [ ] **既存 §4 機密マスクを Web UI レスポンス全体にも適用**: 特にログ表示、history 詳細表示で URL 以外の機密値を含めない
- [ ] **path traversal 防止**: 編集対象パスは Path.resolve() してから許可ディレクトリ配下を確認
- [ ] **live_log 機密マスク二重防御** (Phase 1.5b): subprocess 側 structlog の `mask_sensitive_processor` を必ず通すことを前提に、`run_logs` 永続化時にも 8KB 切り詰めで暴走を抑える。CLAUDE.md §4 の禁止項目 (api_key / token / password / secret / authorization / cookie / webhook) は subprocess 経路でも漏れない

---

## 13. PIR-driven architecture (Phase Diamond verify-pir-driven)

CTI doctrine の中心概念 **PIR (Priority Intelligence Requirements)** を tool の
first-class entity として扱う仕組み。詳細は [docs/pir_system.md](docs/pir_system.md)。

### 設計原則

1. **PIR is canonical intent**: PIR の description が user の意図、
   structured fields (keywords / actors / sectors / countries / feed_titles) は
   LLM compile された中間表現。description 編集で再 compile 可能。
2. **PIR=関心の定義と評価 / routing=配信 (2026-06-13 役割分離)**: PIR は
   「何を集め・何を重要とし・何を語るか」を駆動し、配信チャンネルの決定権は
   routing rules が専属で持つ。PIR の優先度は triage の importance 評価を経由して
   配信に届く (背骨: PIR → importance → channel)。旧 R0 (PIR `target_channel` に
   よる channel 直接 override) は全 21 PIR が auto のまま一度も発動せず、緊急度
   ベースのチャンネル体系と噛み合わないため撤去 (過去データの target_channel
   キーは `loader.strip_legacy_pir_keys` が読み捨てる)。
3. **Shadow mode**: 期間限定 (valid_from/until) / tag / weak signals /
   自動 PIR 提案 etc. の高度機能は UI のみで入力可、logic は未注入。
   観察期間で必要性を判断してから採用。

### 統合 layer

| Layer | PIR 注入 | fallback |
|---|---|---|
| triage | `article_triage.py._build_prompt_pir_driven()` が PIR の title + description を high/medium criteria として動的注入 | `_build_prompt_legacy_hardcoded()` (env `PIR_DRIVEN_TRIAGE=0` で強制活性化) |
| synthesis | `generator.py._render_prompt()` が `pir_context` を Jinja に注入 | 空 list (legacy 挙動) |
| Spotlight | PIR `spotlight.enabled=true` から自動生成 (Phase 2) | デフォルト無効 |

routing への直接注入は無し (R0 撤去済)。記事 × PIR の対応は post-hoc に
`src/pir/evaluator.py` が計算する (daily focus / KPI / 情報フロー画面)。

### Migration + verification

```bash
# 既存 hardcoded 13 high + 4 medium criteria を PIR yaml に投入
uv run python scripts/migrate_existing_pir.py --apply

# A/B test で behavior preservation を検証
uv run python scripts/verify_pir_migration.py --n 300
```

検証基準 (CTI mission に基づく): agreement >= 90%、high→low flip = 0 件、
medium→low flip <= 2%、JP feeds medium+ 維持率 >= 95%。

### 緊急 rollback

```bash
# env で PIR-driven triage を disable → legacy hardcoded prompt に完全 fallback
PIR_DRIVEN_TRIAGE=0
```

`article_triage.py._build_prompt_legacy_hardcoded()` が常に保持されているため
即座に元の挙動に戻せる。

### PIR Spotlight (Phase Diamond verify-spotlight)

global synthesis (P+M+E+S+I+T 横断) と棲み分ける **PIR 縦断 narrative**。
config/delivery/pir.yaml の `spotlight.enabled=true` な PIR each に対して毎日 (直近 7 日窓で)
narrative を生成し、Intel Graph の Synthesis tab "Spotlight" sub-tab に表示。

- **pipeline**: `pir-spotlight` (毎日、直近 7 日窓。2026-08-29 に週次から変更。時刻は job_registry が SSoT)
- **LLM**: narrative ティア (`Step.PIR_SPOTLIGHT`)。26B/31B 両対応
- **構造**: headline (150-280 字、actor+TTP+標的) + key_events (5-8 件) + outlook (600-1000 字、4観点 a/b/c/d)
- **DB**: `pir_spotlight` table (pir_id × period_type × period_start で UPSERT)
- **API**: GET `/api/v1/spotlight`、`POST /api/v1/spotlight/{id}/regenerate`
- **比較**: `scripts/compare_spotlight_models.py <pir_id>` で 26B vs 31B 並列生成

初期 Spotlight 対象 (5 件):
- pir_china_apt (中国 APT 動向)
- pir_dprk_apt (北朝鮮 APT 動向)
- pir_russia_apt (ロシア APT 動向)
- pir_jp_targeted (日本標的)
- pir_geopolitical_cyber (地政学・国家戦略サイバー)

### PIR Daily Focus (Phase Diamond pir-daily-focus)

Spotlight (週次 narrative) の **daily 版**。全 enabled PIR の直近 24h match を
PIR each に section + 上位 3 article + LLM 1-2 文要点 で集約、brief ch に 1 post。

- **pipeline**: `morning-brief` に統合 (毎日 06:30 JST。旧 research-digest → 独立 pipeline pir-daily-focus を経て統合)
- **対象**: enabled な全 PIR (現在 21 件)、24h match (importance ≥ medium) >= 1 件のみ
- **LLM**: per-PIR で 1 call (~5 sec)、fast ティア (`Step.PIR_DAILY_FOCUS`)
- **出力先**: brief ch (朝の通読チャンネル)、Discord 4096 字超は auto-split
- **旧 design (廃止)**: research-digest = watch ch + high+(apt/vuln/malware) 厳格 filter で月数件 yield に陥っていたため、PIR-driven daily 集約に再構築。`digest_research` Literal + `critical_research.py` + `fetch_for_research_digest` + `prompts/research_digest.j2` は完全削除。
