# Claude Code 指示文集 - CTI Briefing Pipeline

このドキュメントは、Claude Codeに対して順番に投げるプロンプト集です。
上から順に使えば、Phase 1のMVPが完成するように構成されています。

各プロンプトは「目的」「前提」「指示文」「期待される成果物」「確認事項」で構成されます。
**指示文をコピペする前に、目的と前提を必ず読んでください。**

---

## 使い方

1. VS Codeでプロジェクトルートを開く
2. ターミナルで `claude` コマンドでClaude Code起動
3. 下記プロンプトを順番に投げる
4. 各プロンプト後に「確認事項」を自分で検証してから次へ進む
5. 期待と違う結果が出たら、修正指示を出す(無理に進めない)

---

## Step 0: 初期コンテキスト共有

### 目的
Claude Codeにプロジェクト全体像を理解させる。
このステップを飛ばすと、後続のすべてのプロンプトの精度が落ちます。

### 前提
- リポジトリclone済み
- CLAUDE.md がプロジェクトルートに配置済み
- VS Code でプロジェクトを開いている

### 指示文

```
プロジェクトルートのCLAUDE.mdを読んで、以下を答えてください:

1. このプロジェクトの目的を1段落で
2. 採用しているLLM選定の制約条件
3. 実装フェーズの一覧と、現在のフェーズ
4. ディレクトリ構造のうち、どこに何を配置するルールか

回答後、不明点や設計上の疑問があれば質問してください。
まだコードは書かないでください。
```

### 期待される成果物
- CLAUDE.mdの内容を正しく要約した回答
- 設計上の疑問点があれば指摘

### 確認事項
- 中国系LLMを使わない制約を理解しているか
- Phase 1の範囲を理解しているか
- 質問が筋の良いものか(的外れな質問が多いならCLAUDE.mdを補強)

---

## Step 1: 依存関係定義

### 目的
pyproject.tomlを作成し、Phase 1で必要な依存関係を定義する。

### 前提
- Step 0完了
- Python 3.11+ がインストール済み
- uv がインストール済み(推奨)

### 指示文

```
pyproject.toml を作成してください。

要件:
- Python 3.11以上
- パッケージ管理は uv 想定
- プロジェクト名: kuebiko
- ライセンス: 個人用途のためUNLICENSED

Phase 1で必要な依存関係(必須):
- httpx (Inoreader API用)
- trafilatura (本文抽出)
- discord-webhook (Discord投稿)
- pyyaml (設定ファイル)
- python-dotenv (環境変数)
- structlog (構造化ログ)
- ollama (LLMクライアント)
- pydantic (データクラス・バリデーション)

開発依存:
- pytest
- pytest-asyncio
- ruff
- mypy

ruffの設定:
- line-length: 100
- target-version: py311
- 主要なlintルール有効化(E, F, W, I, N, UP, B, A, C4, SIM)

mypyの設定:
- strict mode
- python_version: 3.11

作成後、依存関係の選定理由を簡潔に説明してください。
他に推奨する依存があれば提案してください(まだ追加はしないでください)。
```

### 期待される成果物
- pyproject.toml
- 選定理由の説明
- 追加提案(あれば)

### 確認事項
- 中国系ライブラリが含まれていないか
- バージョン制約が緩すぎないか(例: `httpx>=0.27` 程度の最低バージョン指定)

---

## Step 2: ロギング基盤

### 目的
すべてのコンポーネントから使われるロギング基盤を最初に整備する。
後から追加すると一貫性が崩れるため、最初に作る。

### 前提
- Step 1完了
- `uv sync` または `pip install -e .` で依存関係インストール済み

### 指示文

```
src/logging_config.py を作成してください。

要件:
- structlog を使った構造化ログ
- 出力先は2つ:
  1. コンソール(開発時用、人間可読フォーマット)
  2. logs/briefing-YYYY-MM-DD.jsonl(本番運用時用、JSON Lines)
- ログレベルは環境変数 LOG_LEVEL で制御(デフォルト INFO)
- すべてのログに以下のコンテキストを自動付与:
  - timestamp (ISO 8601, UTC)
  - logger名
  - モジュール名

セキュリティ要件:
- APIキー・認証トークンを含む可能性のあるキーは
  自動的にマスクするフィルタを実装すること
- マスク対象キー名: "api_key", "token", "password",
  "secret", "authorization", "cookie"
- マスク方法: 値の最初の4文字 + "***"

公開関数: get_logger(name: str) -> structlog.BoundLogger

テストも tests/test_logging_config.py に作成してください。
特に、機密情報マスクのテストは必ず含めること。
```

### 期待される成果物
- src/logging_config.py
- tests/test_logging_config.py

### 確認事項
- マスクテストが実際に通るか手元で実行
- `python -c "from src.logging_config import get_logger; get_logger('test').info('hello', api_key='secret123')"` で動作確認

---

## Step 3: 設定ローダ

### 目的
config/配下のYAMLファイルと.envをロードする統一インターフェースを作る。
ハードコード防止の要。

### 前提
- Step 2完了

### 指示文

```
src/config_loader.py を作成してください。

要件:
- pydantic v2 のBaseSettingsで.envをロード
- pyyaml で config/*.yaml をロード
- 設定スキーマはpydanticモデルで定義(型安全)

実装するスキーマ:

1. AppConfig (環境変数ベース、.envから):
   - inoreader_app_id: str
   - inoreader_app_key: str
   - inoreader_oauth_token: str
   - discord_webhook_default: str
   - ollama_base_url: str = "http://localhost:11434"
   - log_level: str = "INFO"

2. PipelineConfig (config/pipelines.yaml):
   - name: str
   - source: SourceConfig
   - processor: ProcessorConfig
   - sink: SinkConfig
   各サブ型は適切に定義

3. AgentsConfig (config/agents.yaml):
   - 各エージェントのrole/goal/backstory/llm_model

公開関数:
- load_app_config() -> AppConfig
- load_pipelines() -> list[PipelineConfig]
- load_agents() -> AgentsConfig

config/pipelines.yaml と config/agents.yaml の
スケルトンも同時に作成してください。
ただしダミー値で構わない、実値はあとで埋める。

テストも作成してください。
特に、設定ファイル不在・スキーマ不正時のエラーが
分かりやすいか確認すること。
```

### 期待される成果物
- src/config_loader.py
- config/pipelines.yaml (スケルトン)
- config/agents.yaml (スケルトン)
- tests/test_config_loader.py

### 確認事項
- スキーマがCLAUDE.mdの設計と整合しているか
- .envに無い必須項目があった時のエラーメッセージが親切か

---

## Step 4: Inoreader API クライアント

### 目的
Inoreader APIを叩いて未読記事リストを取得する層を作る。
最初の外部API連携。

### 前提
- Step 3完了
- Inoreaderの開発者ポータルでApp ID/App Keyを取得済み
- `scripts/inoreader_oauth.py` で refresh_token 取得済み (`.env` の `INOREADER_REFRESH_TOKEN`)

### 指示文

```
src/tools/inoreader_client.py を作成してください。

要件:
- httpx の AsyncClient を使う
- Inoreader Stream Contents API のラッパ
- エンドポイント: https://www.inoreader.com/reader/api/0/stream/contents/
- 認証: OAuth 2.0 refresh_token 方式
  - 起動時に refresh_token から access_token を取得 (POST /oauth2/token grant_type=refresh_token)
  - access_token はメモリ内のみで保持 (24h 有効、ディスクに書かない)
  - API リクエストの Authorization ヘッダは "Bearer {access_token}"
  - 401 を受けたら 1 回だけ refresh して再試行

実装するメソッド:

class InoreaderClient:
    def __init__(
        self,
        app_id: str,
        app_key: str,
        refresh_token: str,
    )
    
    async def _refresh_access_token(self) -> None:
        # POST /oauth2/token で access_token を取得し self._access_token に保存
        ...
    
    async def get_unread_articles(
        self,
        stream_id: str = "user/-/state/com.google/reading-list",
        max_count: int = 100,
        newer_than_unix: int | None = None,
    ) -> list[Article]:
        # 未読記事のみ取得 (xt=user/-/state/com.google/read)
        # 401 受信時は自動 refresh + 1 回リトライ
        ...
    
    async def mark_as_read(self, article_ids: list[str]) -> None:
        ...

Article は pydantic モデル:
- id: str (Inoreader article ID)
- title: str
- url: str (canonical URLを優先、なければalternate)
- summary_html: str (summary.contentそのまま)
- author: str | None
- published: datetime
- feed_title: str
- feed_url: str

エラー処理:
- 401: 認証エラーとして InoreaderAuthError
- 429: レート制限として InoreaderRateLimitError
  (Retry-Afterヘッダがあれば従う)
- 5xx: 一時的エラーとして InoreaderServerError
- ネットワークエラー: InoreaderConnectionError

ログ出力:
- 各リクエストのURL、ステータス、件数を構造化ログ
- 認証トークンは絶対にログに出さないこと

テスト (tests/test_inoreader_client.py):
- httpx.MockTransport で全エンドポイントをモック
- 正常系・各エラー系のテスト
- ページネーション(continuation token)のテスト
- 401 → refresh → 再試行成功のテスト
- 401 → refresh も失敗時に InoreaderAuthError を伝播するテスト
```

### 期待される成果物
- src/tools/inoreader_client.py
- tests/test_inoreader_client.py

### 確認事項
- ログにトークンが漏れていないか目視確認
- レート制限の扱いが妥当か(Retry-After尊重)
- pydanticモデルのバリデーションが効いているか

---

## Step 5: 本文抽出ツール

### 目的
記事URLから本文を取得・抽出する層を作る。
最初は trafilatura のみ(Layer 1)。
Layer 2 (Playwright) は後のフェーズで追加。

### 前提
- Step 4完了

### 指示文

```
src/tools/content_extractor.py を作成してください。

要件:
- trafilatura で本文抽出
- httpx で記事URLをfetch
- User-Agent は設定可能(デフォルトは一般的なブラウザ風)

実装するクラス・関数:

class ContentExtractor:
    def __init__(
        self,
        timeout_seconds: float = 30.0,
        user_agent: str = "Mozilla/5.0 ...",
        min_content_length: int = 200,
    )
    
    async def extract(self, url: str) -> ExtractionResult:
        ...

ExtractionResult (pydantic):
- url: str
- title: str | None
- author: str | None
- published_date: datetime | None
- text: str  # 本文プレーンテキスト
- language: str | None  # 自動判定
- success: bool
- failure_reason: str | None
- extraction_method: str  # "trafilatura"

成功条件:
- HTTPステータス200
- 本文文字数 >= min_content_length
- 本文が抽出できている

失敗時:
- success=False
- failure_reasonに理由を入れる
- 例外を発生させず、ExtractionResultを返す
  (上位層で判断できるように)

エラーカテゴリ:
- "http_error_xxx" (4xx/5xx)
- "timeout"
- "connection_error"
- "extraction_failed" (HTMLは取れたが本文抽出失敗)
- "content_too_short"
- "paywall_suspected" (短すぎる + 特定キーワード検出)

ログ出力:
- 抽出開始・完了時にURL、結果、文字数を構造化ログ
- 失敗時は理由を明示

テスト:
- httpx.MockTransport を使ったモックテスト
- 成功・各失敗カテゴリのテスト
- ペイウォール検出のテスト

注意:
- 並列実行されることを想定するため、AsyncClientの
  接続プールを適切に管理すること
- robots.txt は今回は無視(個人利用前提だが、
  CLAUDE.mdに「将来的に対応検討」と追記してください)
```

### 期待される成果物
- src/tools/content_extractor.py
- tests/test_content_extractor.py
- CLAUDE.md にrobots.txt対応のTODOを追記

### 確認事項
- trafilaturaの設定が妥当か(`favor_recall` vs `favor_precision`の選択)
- 実際のニュースサイトで動作確認
  (例: https://www.bleepingcomputer.com/ の記事URL)

---

## Step 6: Ollamaクライアント

### 目的
ローカルLLM(Gemma 4)に処理を投げる層を作る。
要約・翻訳・分析の共通基盤。

### 前提
- Step 5完了
- Ollamaが起動している
- Gemma 4 (or 暫定的にGemma 3 27B) がpull済み

### 指示文

```
src/tools/llm_client.py を作成してください。

要件:
- ollama パッケージのAsyncClient を使う
- OpenAI互換APIではなく、ollama公式SDKを使うこと
- (将来的に切り替えやすいよう、抽象クラスで包む)

抽象クラス:

class LLMClient(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        ...
    
    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        schema: type[BaseModel],
        system: str | None = None,
    ) -> BaseModel:
        # JSON出力を強制してpydanticモデルにパース
        ...

具象クラス:

class OllamaClient(LLMClient):
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "gemma3:27b",  # Gemma 4対応待ち
        timeout_seconds: float = 300.0,
    )

LLMResponse (pydantic):
- text: str
- model: str
- input_tokens: int
- output_tokens: int
- duration_seconds: float

エラー処理:
- モデル未存在: LLMModelNotFoundError
- タイムアウト: LLMTimeoutError
- Ollamaサーバ未起動: LLMConnectionError
- JSON出力失敗: LLMStructuredOutputError

ログ出力:
- 各推論の所要時間、トークン数、温度を構造化ログ
- プロンプト本文はDEBUGレベルのみ(本番INFOには出さない)
  → 機密情報を含む可能性があるため

テスト:
- 実Ollamaは使わず、httpxレベルでモック
- 正常系・各エラー系のテスト
- structured outputのスキーマバリデーションテスト

CLAUDE.mdに以下のセクションを追記:
- LLMモデル切替方法(設定ファイルで指定)
- Gemma 4降臨時のアップデート手順
```

### 期待される成果物
- src/tools/llm_client.py
- tests/test_llm_client.py
- CLAUDE.mdへの追記

### 確認事項
- 実Ollamaに繋いで動作確認
  ```python
  client = OllamaClient(model="gemma3:27b")
  result = await client.generate("こんにちは、自己紹介してください")
  print(result.text)
  ```
- structured outputが日本語スキーマでも動くか

---

## Step 7: Discord 投稿ツール

### 目的
Discord WebhookでBLUF形式メッセージを投稿する層。

### 前提
- Step 6完了
- Discord Webhook URL を取得済み

### 指示文

```
src/tools/discord_publisher.py を作成してください。

要件:
- discord-webhook パッケージを使う
- 2000文字制限を意識した分割投稿
- レート制限対応(秒間5リクエスト程度に抑制)

実装するクラス:

class DiscordPublisher:
    def __init__(
        self,
        webhook_url: str,
        username: str = "CTI Briefing Bot",
        rate_limit_per_second: int = 4,
    )
    
    async def post(self, message: BriefingMessage) -> None:
        ...
    
    async def post_batch(
        self,
        messages: list[BriefingMessage],
    ) -> PostResult:
        # 内部でレート制限を遵守
        ...

BriefingMessage (pydantic):
- title: str
- bluf: str  # 結論先出し1-2行
- importance: Literal["high", "medium", "low"]
- category: str  # "apt", "vulnerability", "policy"等
- summary: str
- iocs: list[str] = []
- mitre_techniques: list[str] = []  # T1566.001 等
- sources: list[Source]  # url, title
- analyst_note: str | None = None
- metadata: dict[str, Any] = {}  # 拡張用

Source (pydantic):
- title: str
- url: str
- language: str = "en"

整形ルール:
- importance="high" は赤い縦線(Embedのcolor)
- importance="medium" は黄色
- importance="low" は灰色
- BLUFは太字で先頭
- IOCsはコードブロック
- MITREテクニックはリンク化
  (https://attack.mitre.org/techniques/T1566/001/)
- 2000文字超過時は要点だけ残し、詳細は次メッセージに分割

レート制限:
- asyncio.Semaphore で同時実行制御
- 投稿間に最低250ms の間隔

テスト:
- discord-webhookをモック
- 各importance別の整形テスト
- 長文分割テスト
- レート制限の挙動テスト
```

### 期待される成果物
- src/tools/discord_publisher.py
- tests/test_discord_publisher.py

### 確認事項
- 実際にテスト用Discordチャンネルへ投稿してみる
- 日本語含むメッセージが文字化けしないか
- 太字・コードブロックが期待通りレンダリングされるか

---

## Step 8: Phase 1 オーケストレータ

### 目的
ここまでに作った部品を組み合わせて、エンドツーエンドで動くMVPを作る。

### 前提
- Step 1〜7すべて完了
- 各部品が単体で動作確認済み

### 指示文

```
src/main.py を作成してください。

これはPhase 1のMVPで、以下のフローを実行します:

1. 設定読み込み (.env + config/*.yaml)
2. InoreaderClientで未読記事リスト取得 (最大20件)
3. 各記事について:
   a. ContentExtractorで本文抽出
   b. 抽出失敗時はsummary_htmlをフォールバック使用
   c. OllamaClientでBLUF形式の日本語要約を生成
   d. BriefingMessageに整形
4. DiscordPublisherで一括投稿
5. 投稿成功した記事をInoreaderで既読マーク
6. サマリーログを出力

要件:

- すべてのステップを構造化ログに記録
- どこかで失敗しても、可能な限り処理を継続
- 1記事の失敗が全体を止めないこと
- 最終的に成功・失敗件数のサマリを返す

LLMプロンプトはハードコードせず、
prompts/summarizer.j2 にJinja2テンプレートとして外出し。

prompts/summarizer.j2 の内容:
- 入力: 記事タイトル、本文、ソース情報
- 出力: BLUF + 要約 + 重要度 + カテゴリ
  (JSON形式で返させてpydanticパース)

CLI:
- python -m src.main で実行
- --dry-run オプション(Discord投稿せず標準出力)
- --max-articles N オプション
- --debug オプション(ログレベルDEBUG)

テスト:
- tests/test_main.py で統合テスト
- 各部品をモックして全体フローを検証
- --dry-run の動作確認

実装後、README.mdに以下を追記:
- Phase 1 の使い方
- 必要な環境変数
- 動作確認手順
```

### 期待される成果物
- src/main.py
- prompts/summarizer.j2
- tests/test_main.py
- README.md 更新

### 確認事項
- 実環境(自分のInoreader + 自分のDiscord)で動作確認
- `--dry-run` で安全に試す
- 1記事だけでEnd-to-End動作するか
- 10記事程度でもエラーで止まらないか

---

## Step 9: launchd 設定

### 目的
毎朝5時に自動実行されるようにmacOSのlaunchdに登録する。

### 前提
- Step 8完了、手動実行で安定動作を確認済み

### 指示文

```
launchd/com.user.ctibriefing.plist を作成してください。

要件:
- 毎朝05:00 (JST) に自動実行
- 失敗時の自動リスタートはしない(誤投稿リスク回避)
- ログを logs/launchd-stdout.log と launchd-stderr.log に出力
- ProgramArgumentsで uv run python -m src.main を実行
- 作業ディレクトリはプロジェクトルート
- 環境変数 PATH を適切に設定(uvが見つかるように)

注意:
- WorkingDirectory は絶対パスにする必要がある
  → プレースホルダ {PROJECT_ROOT} で記述しておき、
     ユーザがインストール時に置換する想定
- StartCalendarInterval で毎朝5時を指定

同時に、launchd/install.sh も作成してください:
- plistの {PROJECT_ROOT} を実際のパスに置換
- ~/Library/LaunchAgents/ にコピー
- launchctl load でロード
- 動作確認コマンドの案内を出力

launchd/uninstall.sh も作成:
- launchctl unload
- plistを削除

docs/deployment.md を作成し、以下を記載:
- インストール手順
- 動作確認方法 (launchctl list | grep ctibriefing)
- ログの見方
- トラブルシューティング(よくあるハマりポイント)
- caffeinateで省電力スリープを抑止する設定

注意点として、Macが完全にスリープしている場合は
launchdが動作しない場合がある旨を明記してください。
```

### 期待される成果物
- launchd/com.user.ctibriefing.plist
- launchd/install.sh
- launchd/uninstall.sh
- docs/deployment.md

### 確認事項
- 翌朝、実際に自動投稿されるか確認
- `launchctl list | grep ctibriefing` で登録状態確認
- スリープ抑止が効いているか

---

## Phase 1 完了後のチェックリスト

ここまで完了したら、以下を確認してPhase 2に進む:

- [ ] 5日連続で朝5時にDiscord投稿が成功している
- [ ] エラー時のログが追える状態にある
- [ ] 重複投稿が起きていない(同じ記事が翌日も来ない)
- [ ] 記事抽出失敗率が30%以下
- [ ] LLM要約の品質が実用に足る
- [ ] 機密情報がログ・コミットに漏れていない

---

## Phase 2 以降のプロンプト(概略のみ)

Phase 1が安定してから書き起こす想定で、ここでは概略のみ。

### Phase 2: Grok対応
- IMAPクライアント追加 (src/tools/imap_client.py)
- Playwright経由のGrok全文取得 (src/tools/grok_fetcher.py)
- セッション管理 (data/playwright_state.json)

### Phase 3: 重複排除
- Embedding生成 (multilingual-e5-large-instruct)
- ChromaDB導入
- 類似度判定ロジック

### Phase 4: CTI観点メタデータ
- 脅威アクター抽出
- MITRE ATT&CKマッピング
- IOC抽出
- 用語集適用 (config/terminology.yaml)

### Phase 5: CrewAIエージェント主導化
- Phase 1〜4の処理をエージェントに分解
- Orchestrator中心の動的フロー

---

## トラブル時のテンプレ指示

### 期待と違うコードが出た時

```
出力されたコードは [具体的な問題] という点で要件を満たしていません。
特に [具体的なファイル名:行番号] の部分が問題です。
[正しい挙動] になるよう修正してください。
他のファイルは変更しないでください。
```

### テストが落ちた時

```
tests/test_xxx.py を実行したところ、以下のエラーが出ました:

[エラーメッセージ全文]

このテスト失敗の原因を分析してください。
直接コードを修正する前に、原因の仮説を3つ挙げてください。
```

### スコープが広がりそうな時

```
それは現在のフェーズの範囲外です。
TODOコメントとしてコードに残し、
docs/backlog.md に追記するに留めてください。
今は [現在のタスク] に集中してください。
```

### セキュリティが心配な時

```
作成されたコードについて、以下の観点でセキュリティレビューしてください:
1. 認証情報の取り扱い
2. ログへの機密情報漏洩リスク
3. 外部入力のバリデーション
4. エラーメッセージからの情報漏洩

問題があれば修正案を提示してください。
```

---

## Claude Code との協業のコツ

### 大きすぎるタスクを投げない
1ファイル、1モジュールに絞る。コンテキストが膨れると品質が落ちる。

### コミット前に必ずdiffを見る
Claude Codeが意図しない変更をしていないか確認。
特にCLAUDE.mdや.gitignoreの勝手な変更に注意。

### テストをスキップさせない
「テストは後で」を許すと、後で書かれない。
プロンプトで明示的に「テストも作成」と指示する。

### CLAUDE.mdを育てる
開発中に得た知見・ハマりどころは、Claude Codeに
「これをCLAUDE.mdに追記してください」と指示して育てる。
次回以降のClaude Codeの精度が上がる。

### 動かないものを溜めない
1ステップ毎に動作確認する。3ステップ溜まると
原因特定が指数関数的に難しくなる。
