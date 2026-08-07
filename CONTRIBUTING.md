# Contributing to kuebiko

個人運用ツールですが、Issue / PR は歓迎します。小さく・検証可能な変更を好みます。

## 開発環境

```bash
uv sync                                  # Python 3.12+ / uv
cd frontend && npm install               # React SPA
```

## PR を出す前のチェック

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run mypy src/ tests/                  # strict (pyproject 既定、tests/ も型ゲート対象)
uv run pytest tests/unit -q
cd frontend && npx tsc --noEmit
```

## 規約 (詳細は [CLAUDE.md](CLAUDE.md))

- **新規モジュールはテストとセットで** (unit 中心、AAA パターン、カバレッジ目標 80%)
- 型ヒント必須 / `dataclass(frozen=True)`・pydantic frozen を既定 (immutable 優先)
- 関数 50 行以下・ファイル 400 行目安・早期 return でネスト 4 段以下
- コメント・ドキュメントは日本語可、識別子は英語

## 絶対の制約 (違反 PR は却下)

- **中国系 LLM / Embedding を使用・許可するコードを入れない** (CLAUDE.md §4。
  denylist `FORBIDDEN_MODEL_PREFIXES` はコード所有 — config 化しない)
- シークレット・実ドメイン・個人情報をコード / コミットメッセージ / テスト fixture に
  含めない (fixture のドメインは `kuebiko.example` を使う)
- 記事本文・認証情報をログに出さない (機密マスクを迂回しない)

## セキュリティ報告

脆弱性の疑いは公開 Issue ではなく GitHub の
[Private vulnerability reporting](https://github.com/kamonegi13/kuebiko/security) か
[@kamonegi13](https://github.com/kamonegi13) へ直接連絡してください。
