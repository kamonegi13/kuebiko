"""判定基準 (rubric) → summarizer プロンプト文字列 / ``jinja2.Template`` の合成。

**契約 (フィールド一覧・型・「JSON のみ出力」の指示) は一切生成しない**。LLM クライアント
4 実装すべてが ``SummaryOutput`` の JSON Schema を注入済 (Ollama=format / Anthropic=tool_use /
openai 互換・claudecode=末尾に schema を自動付加) であり、プロンプト側の再掲は schema 変更時に
必ずドリフトする。ここが出すのは **人間が編集する判定基準と few-shot 例だけ**。

``jinja2.Environment`` の構成は ``src.pipeline.dispatch._load_template`` と完全に同一である
必要がある (``_persona_local.j2`` の解決・autoescape 無効・StrictUndefined は既存挙動)。
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from src.prompts.rubric_model import SummarizerRubric

# rollback 用に据え置く legacy テンプレート (層分けの前身)。
LEGACY_TEMPLATE_PATH = Path("prompts/briefing/summarizer.j2")

# ---- コード所有層 (利用者が編集できない構造部分) ----

PERSONA_INCLUDE = '{% include ["_persona_local.j2", "_persona.j2"] %}'
ARTICLE_BLOCK = (
    "【記事タイトル】\n"
    "{{ article.title }}\n"
    "\n"
    "【記事 ソース】\n"
    "{{ article.feed_title }}\n"
    "【記事 本文】\n"
    "{{ body }}"
)
RUBRIC_HEADING = "# フィールド別の判定基準"


def compose(rubric: SummarizerRubric) -> str:
    """判定基準からプロンプト原文 (Jinja タグ未展開) を組み立てる。

    出力順序: persona → intro → 記事変数ブロック → ``---`` → 章見出し →
    rubric セクション (config 順) → few-shot 例 (config 順)。
    body が空のセクション (suppressed) は出力されない。
    """
    parts: list[str] = [PERSONA_INCLUDE]
    if rubric.intro.strip():
        parts.append(rubric.intro)
    parts += ["", ARTICLE_BLOCK, "", "---", "", RUBRIC_HEADING, ""]
    for section in rubric.sections:
        if not section.body.strip():  # suppressed で body 空 = 出力しない
            continue
        parts.append(f"- ``{section.field_id}``: {section.body}")
    for example in rubric.examples:
        heading = f"# 出力例 ({example.label})" if example.label.strip() else "# 出力例"
        parts += ["", heading, "", "```json", example.json_text, "```"]
    return "\n".join(parts) + "\n"


def loader_roots(path: Path) -> list[str]:
    """loader root = [テンプレートの親, prompts root] (dispatch._load_template と同一)。"""
    roots = [str(path.parent)]
    prompts_root = path.parent.parent if path.parent.name != "prompts" else path.parent
    if str(prompts_root) not in roots:
        roots.append(str(prompts_root))
    return roots


def build_environment(path: Path) -> jinja2.Environment:
    """dispatch._load_template と同一構成の Environment (差分ゼロが要件)。"""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(loader_roots(path)),
        autoescape=False,  # noqa: S701  プロンプトはテキストでありエスケープしない
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )


def build_template(rubric: SummarizerRubric, *, path: Path) -> jinja2.Template:
    """合成結果を ``jinja2.Template`` にする。

    ``from_string`` でも ``{% include %}`` は env の loader 経由で解決され、既定の
    ``auto_reload`` により ``_persona_local.j2`` の編集は再起動なしで反映される。
    """
    return build_environment(path).from_string(compose(rubric))
