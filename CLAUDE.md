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
>   (P0/P1/P5 実施済、P2/P3+merge/P6/P4 は順次)。§5 のディレクトリ表に未記載の package:
>   `src/watchers/ src/digest/ src/taxonomy/ src/synthesis/ src/pir/ src/spotlight/`。
> - §6 のフェーズ表は歴史的記録 (Phase 2.6a「現在地」は古い)。現在は上記の統合再設計フェーズ。

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
│   └── stix_exporter.py     # STIX 2.1 Bundle 生成 (intent→primary_motivation 写像込み)
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

- **メイン (per-article 要約・翻訳)**: `OLLAMA_MAIN_MODEL`、現用 Gemma 4 26B (MoE, active 4B) on Ollama
  - daily-briefing で 1 article = 1 LLM 呼出を大量実行するため**速度重視**。26B の think=False で 5-15 秒/件
  - Dense 31B も指定可能だが per-article で 40-100 秒/件 → 30 分 timeout 内に処理不能なため非推奨
- **抽出・分類 (Phase 3.0)**: `OLLAMA_EXTRACT_MODEL`、現用 Gemma 4 26B (MoE, active 4B) — Grok レポートの構造化抽出 / IoC verifier。未設定なら main を流用
- **状況総括 (Phase 3 Synthesis)**: `OLLAMA_SYNTHESIS_MODEL`、現用 Gemma 4 31B Dense — Intel Graph の status_synthesis pipeline で narrative reasoning。1 run につき 1 呼出 (~21k char prompt) のため品質重視で Dense 採用、timeout 900s 設定済。未設定なら main を流用
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
3. **Web UI の設定タブ** または `.env` の `OLLAMA_MAIN_MODEL` / `OLLAMA_EXTRACT_MODEL` / `OLLAMA_SYNTHESIS_MODEL` を書き換え (用途別)
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
│   ├── architecture.md        # アーキテクチャ図と決定の根拠
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
| **Phase 2.6a** | **Grok 専用パーサ + LLM スキップ + セクション単位 Discord 投稿** | **現在地** |
| Phase 3a | URL 正規化 + SHA-256 ハッシュベースの重複排除 (SQLite dedup_seen_urls) | 完了 |
| Phase 3b | Embedding + SQLite blob + numpy コサイン類似度 | 完了 (実機は OLLAMA_EMBED_MODEL 設定後) |
| Phase 4 | CTI 観点メタデータ付与 (脅威アクター, MITRE ATT&CK, IOC) | 未着手 |
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
  - `OLLAMA_EMBED_MODEL=intfloat/multilingual-e5-large-instruct` を `.env` に設定 (先に `ollama pull`)
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
- **デプロイは app service 限定で** (2026-07-05): `docker compose up -d --build` を全 service に
  かけると **tunnel が毎回再作成され quick tunnel の URL が変わる** (→ 毎デプロイ ops に
  新 URL 投稿 = 「URL が頻繁に変わる」の正体)。app のみ更新するときは
  `docker compose up -d --build kuebiko readonly` で tunnel を触らない
  (URL 安定・ops 静粛)。恒久安定 URL が要るなら `.env` に `CLOUDFLARE_TUNNEL_TOKEN`
  (+ `CLOUDFLARE_TUNNEL_HOSTNAME`) を設定して named tunnel 化 (再起動で URL 不変)。手順は
  [docs/mobile-access.md](docs/mobile-access.md)
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
- **収集した記事の再配布**。要約と引用 URL に留める
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
  **配置の判断基準 = 対象画面を持たないものだけが設定ページに残る** (マッチリストは配信ルールの語彙なので
  情報フローへ移設、ソースは購読ソース、ジョブは実行管理)。.env タブは 2026-07-24 廃止 — 接続系キーは接続タブ+各対象画面 (チャンネル=情報フロー / IMAP=購読ソース / Ollama・外部LLM=モデルタブ) の双方から編集でき (同一 API)、.env ファイルは不可視の保存層として存続 (docs/deployment.md §7)
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
  - **例外: readonly mobile 公開 (Phase Diamond verify-mobile)**: 別 service `readonly` を `127.0.0.1:8002:8000` で起動 (full instance とは別 container、`READ_ONLY=1` 環境変数)。FastAPI middleware が POST/PUT/PATCH/DELETE を **すべて 403** で block。Cloudflare Tunnel が `127.0.0.1:8002` のみを HTTPS で外部公開し、外部から到達できるのは **閲覧専用 API のみ**。write 不可は構造的に保証 (認証ゲートではなく物理的隔離)。詳細手順は [docs/mobile-access.md](docs/mobile-access.md)
  - **公開 instance の到達範囲は 3 層 (2026-08-01)**。SSoT は `src/ui/read_only_policy.py` **1 箇所**:
    **Tier0 匿名** = 閲覧系 read API と SPA / **Tier1 認証済み (Cloudflare Access)** = 運用系 read API
    (`READ_ONLY_GET_DENYLIST`: ジョブ計画・設定・プロンプト・ルーティング・レビューキュー) の閲覧 +
    ジョブ即時実行 + 分析チャット・記事翻訳 / **Tier2 ローカル専用** = それ以外の全 write。
    - frontend の `nav.ts` `fullOnly` は**表示上の隠蔽にすぎない** — 遮断の実体は常にサーバ側の
      denylist。新しい運用系 API を足したら denylist にも 1 行足す (でなければ公開面に露出する)。
    - Tier1 の write は**ジョブ即時実行 1 つだけ**。readonly は scheduler を起動しないため、
      認証済みの `POST /api/v1/jobs/{id}/run` のみ full instance (`FULL_INSTANCE_URL`) へ
      narrow proxy する。**write の実行主体は常に full** で §12 の境界は不変。
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
config/delivery/pir.yaml の `spotlight.enabled=true` な PIR each に対して週次で
narrative を生成し、Intel Graph の Synthesis tab "Spotlight" sub-tab に表示。

- **pipeline**: `pir-spotlight` (月曜 03:30 JST cron)
- **LLM**: `OLLAMA_SPOTLIGHT_MODEL` (未設定なら MAIN_MODEL を流用、26B/31B 両対応)
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
- **LLM**: per-PIR で 1 call (~5 sec)、`OLLAMA_MAIN_MODEL` を使用
- **出力先**: brief ch (朝の通読チャンネル)、Discord 4096 字超は auto-split
- **旧 design (廃止)**: research-digest = watch ch + high+(apt/vuln/malware) 厳格 filter で月数件 yield に陥っていたため、PIR-driven daily 集約に再構築。`digest_research` Literal + `critical_research.py` + `fetch_for_research_digest` + `prompts/research_digest.j2` は完全削除。
