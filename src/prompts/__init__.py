"""プロンプトの層分け (判定基準 = 編集可能層 / 契約 = コード所有層)。

``prompts/**/*.j2`` を「LLM が読む 1 枚の文字列」として直接編集する運用は、契約
(出力スキーマ) と判定基準 (rubric) が同一ファイルに混ざるため、schema 変更時に
必ずドリフトする。本パッケージは summarizer プロンプトについて両者を分離し、
判定基準を DB (config_store) を SSoT とする構造化データとして扱う。

- ``rubric_model``: 判定基準のデータモデル + schema 由来の契約メタ + 検証
- ``summarizer_composer``: 判定基準 → プロンプト文字列 / ``jinja2.Template``
- ``rubric_store``: DB / seed yaml の読み書き + cache + env flag + fail-safe
- ``sample_article``: 検証・プレビュー共通の固定サンプル記事
"""
