#!/usr/bin/env python3
"""legacy summarizer.j2 と rubric 合成結果の等価性検証 CLI (WP4)。

summarizer プロンプトの層分け (``src/prompts/``) が「契約散文の削除だけで、判定基準の
意味を変えていない」ことを機械的に検証する。差分は ``scripts/summarizer_diff_contract.py``
の期待差分表と突き合わせ、そこに載っていない差分 (= 未分類) が 1 件でもあれば fail する。

Usage::

    uv run python scripts/verify_summarizer_rubric.py --source seed --offline --write-golden
    uv run python scripts/verify_summarizer_rubric.py --source seed --offline
    uv run python scripts/verify_summarizer_rubric.py --source seed --n 20

``--offline`` は DB に触れず ``src.prompts.sample_article.SAMPLE_ARTICLE`` 1 件のみで
render 検証する。DB 記事取得に失敗した場合も同じ 1 件フォールバックに自動で倒れる
(無音にはしない — stderr に理由を出す)。

**これは移行専用の一時的な検証 script**。撤去条件は
``scripts/summarizer_diff_contract.py`` の docstring を参照。
"""

from __future__ import annotations

import argparse
import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from summarizer_diff_contract import (  # noqa: E402
    EXPECTED_DELETIONS,
    EXPECTED_REWRITES,
    LEGACY_PATH,
    MAX_WHITESPACE_DIFFS,
)

from src.prompts.rubric_model import SummarizerRubric  # noqa: E402
from src.prompts.rubric_store import SEED_PATH, load_rubric, load_seed_rubric  # noqa: E402
from src.prompts.sample_article import SAMPLE_ARTICLE  # noqa: E402
from src.prompts.summarizer_composer import build_environment, build_template, compose  # noqa: E402
from src.tools.article_model import Article  # noqa: E402

if TYPE_CHECKING:
    import jinja2

# render 検証で使う DB 記事の既定件数 (--n 未指定時)。
DEFAULT_ARTICLE_COUNT = 5
GOLDEN_PATH = _REPO / "tests" / "fixtures" / "summarizer_composed_expected.txt"


@dataclass(frozen=True)
class DiffCounts:
    """SequenceMatcher opcodes を期待差分の枠 (§7-1) に分類した結果。"""

    deletions: int = 0
    rewrites: int = 0
    whitespace: int = 0
    unclassified: int = 0
    unclassified_notes: tuple[str, ...] = ()


def _build_deletion_texts() -> dict[str, str]:
    """行番号ベースの ``EXPECTED_DELETIONS`` を、行内容ベースの辞書に変換する。

    記事本文を差し込んでレンダリングすると ``{{ body }}`` の行数ぶん後続行がずれるため、
    行番号ではなく **内容一致** で判定できるようにする (legacy 側の静的な行は記事に
    依存せず不変)。
    """
    legacy_lines = (_REPO / LEGACY_PATH).read_text(encoding="utf-8").splitlines()
    return {legacy_lines[lineno - 1]: reason for lineno, reason in EXPECTED_DELETIONS.items()}


def _build_rewrite_pairs() -> dict[str, str]:
    """``EXPECTED_REWRITES`` の値 (legacy 原文 → 合成後) だけを取り出した辞書。"""
    return dict(EXPECTED_REWRITES.values())


def _describe(tag: str, lineno: int, line: str, *, include_content: bool) -> str:
    if include_content:
        return f"{tag} @L{lineno + 1}: {line!r}"
    return f"{tag} @L{lineno + 1} (len={len(line)})"


def classify_diff(
    legacy_text: str,
    composed_text: str,
    *,
    deletion_texts: dict[str, str],
    rewrite_pairs: dict[str, str],
    include_content: bool = False,
) -> DiffCounts:
    """legacy/composed テキストを行単位で diff し、期待差分の枠に分類する。

    **内容一致** で判定するため、記事本文を差し込んでレンダリングした後のテキスト
    同士でも (絶対行番号がずれても) 同じ分類結果になる。
    """
    legacy_lines = legacy_text.splitlines()
    composed_lines = composed_text.splitlines()
    matcher = difflib.SequenceMatcher(None, legacy_lines, composed_lines, autojunk=False)

    deletions = rewrites = whitespace = unclassified = 0
    notes: list[str] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        remaining_composed = list(composed_lines[j1:j2])
        for offset, legacy_line in enumerate(legacy_lines[i1:i2]):
            if legacy_line in deletion_texts:
                deletions += 1
                continue
            if legacy_line in rewrite_pairs:
                expected = rewrite_pairs[legacy_line]
                if expected in remaining_composed:
                    remaining_composed.remove(expected)
                    rewrites += 1
                else:
                    unclassified += 1
                    notes.append(
                        _describe(tag, i1 + offset, legacy_line, include_content=include_content)
                    )
                continue
            if legacy_line.strip() == "":
                whitespace += 1
                continue
            unclassified += 1
            notes.append(_describe(tag, i1 + offset, legacy_line, include_content=include_content))
        for leftover in remaining_composed:
            if leftover.strip() == "":
                whitespace += 1
            else:
                unclassified += 1
                notes.append(_describe(tag, j1, leftover, include_content=include_content))

    return DiffCounts(deletions, rewrites, whitespace, unclassified, tuple(notes))


def _load_rubric_for_source(source: str) -> SummarizerRubric | None:
    if source == "seed":
        return load_seed_rubric(_REPO / SEED_PATH)
    return load_rubric()


def _legacy_template() -> jinja2.Template:
    path = _REPO / LEGACY_PATH
    return build_environment(path).get_template(path.name)


def _render_pair(
    legacy_template: jinja2.Template,
    composed_template: jinja2.Template,
    article: Article,
) -> tuple[str, str]:
    from src.pipeline.briefing import MAX_LLM_BODY_CHARS

    body = (article.body_text or "")[:MAX_LLM_BODY_CHARS]
    legacy_rendered = legacy_template.render(article=article, body=body)
    composed_rendered = composed_template.render(article=article, body=body)
    return legacy_rendered, composed_rendered


def _fetch_db_articles(n: int) -> list[Article]:
    """直近記事から本文ありのものを最大 ``n`` 件、DB から取得する。"""
    from datetime import UTC, datetime

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    records = repo.list_articles(limit=max(n * 3, n))
    articles: list[Article] = []
    for record in records:
        if len(articles) >= n:
            break
        body = repo.get_article_body(record.article_id)
        if not body:
            continue
        articles.append(
            Article(
                id=record.article_id,
                title=record.title,
                url=record.url,
                summary_html="",
                published=record.published_at or record.created_at or datetime.now(UTC),
                feed_title=record.feed_title or "",
                feed_url=record.feed_url or "",
                body_text=body,
            ),
        )
    return articles


def _resolve_articles(*, offline: bool, n: int) -> list[Article]:
    if offline:
        return [SAMPLE_ARTICLE]
    try:
        articles = _fetch_db_articles(max(n, 1))
    except Exception as e:  # noqa: BLE001  DB 障害時は sample article にフォールバック
        print(
            f"DB 記事取得に失敗 (fallback: sample article 1 件): {type(e).__name__}",
            file=sys.stderr,
        )
        return [SAMPLE_ARTICLE]
    if not articles:
        print(
            "DB に render 可能な記事 (body あり) がありません (fallback: sample article 1 件)",
            file=sys.stderr,
        )
        return [SAMPLE_ARTICLE]
    return articles


def _print_notes(label: str, notes: tuple[str, ...]) -> None:
    if not notes:
        return
    print(f"  [{label}]")
    for note in notes:
        print(f"    {note}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="legacy summarizer.j2 と rubric 合成結果の等価性検証",
    )
    parser.add_argument(
        "--source",
        choices=("seed", "db"),
        default="seed",
        help="判定基準の取得元 (既定: seed yaml。db は DB → seed の順に解決)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_ARTICLE_COUNT,
        help=f"DB から取得して render 検証する記事件数 (既定 {DEFAULT_ARTICLE_COUNT})",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="DB に触れず固定サンプル記事のみで検証する",
    )
    parser.add_argument(
        "--print-diff",
        action="store_true",
        help="未分類差分の行内容を表示する (既定は件数のみ、プロンプト本文を出力に含めない)",
    )
    parser.add_argument(
        "--write-golden",
        action="store_true",
        help=(
            "seed 合成結果 (Jinja タグ未展開) を "
            "tests/fixtures/summarizer_composed_expected.txt に書き出す"
        ),
    )
    args = parser.parse_args()

    rubric = _load_rubric_for_source(args.source)
    if rubric is None:
        print(f"判定基準を読み込めません (source={args.source})", file=sys.stderr)
        return 1

    deletion_texts = _build_deletion_texts()
    rewrite_pairs = _build_rewrite_pairs()

    # 主検証: legacy .j2 (未レンダリング) vs 合成結果 (未レンダリング) — 契約の本体。
    legacy_raw = (_REPO / LEGACY_PATH).read_text(encoding="utf-8")
    composed_raw = compose(rubric)
    primary = classify_diff(
        legacy_raw,
        composed_raw,
        deletion_texts=deletion_texts,
        rewrite_pairs=rewrite_pairs,
        include_content=args.print_diff,
    )
    legacy_chars = len(legacy_raw)
    composed_chars = len(composed_raw)

    # 副検証: 記事を render した後 (テンプレート変数展開後) も差分構造が同じであること。
    articles = _resolve_articles(offline=args.offline, n=args.n)
    legacy_template = _legacy_template()
    composed_template = build_template(rubric, path=_REPO / LEGACY_PATH)
    per_article_ok = True
    for article in articles:
        legacy_rendered, composed_rendered = _render_pair(
            legacy_template, composed_template, article
        )
        counts = classify_diff(
            legacy_rendered,
            composed_rendered,
            deletion_texts=deletion_texts,
            rewrite_pairs=rewrite_pairs,
            include_content=args.print_diff,
        )
        same_structure = (
            counts.unclassified == 0
            and counts.deletions == primary.deletions
            and counts.rewrites == primary.rewrites
            and counts.whitespace == primary.whitespace
        )
        if not same_structure:
            per_article_ok = False
            print(f"記事 {article.id} で render 後の差分構造が主検証と異なります", file=sys.stderr)
            if args.print_diff:
                _print_notes(article.id, counts.unclassified_notes)

    delta = composed_chars - legacy_chars
    sign = "+" if delta > 0 else ""
    print(
        f"記事 {len(articles)} 件で検証: 差分 = "
        f"削除 {primary.deletions} / 書換 {primary.rewrites} / "
        f"空白 {primary.whitespace} / 未分類 {primary.unclassified}",
    )
    print(f"プロンプト長: legacy {legacy_chars:,} → 合成 {composed_chars:,} ({sign}{delta:,} 字)")
    if args.print_diff:
        _print_notes("主検証", primary.unclassified_notes)

    if args.write_golden:
        golden_rubric = load_seed_rubric(_REPO / SEED_PATH)
        if golden_rubric is None:
            print("golden 書き出し失敗: seed yaml を読み込めません", file=sys.stderr)
            return 1
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(compose(golden_rubric), encoding="utf-8")
        print(f"golden 更新: {GOLDEN_PATH.relative_to(_REPO)}")

    ok = (
        primary.unclassified == 0
        and primary.whitespace <= MAX_WHITESPACE_DIFFS
        and composed_chars <= legacy_chars
        and per_article_ok
    )
    if not ok:
        print(
            "NG: 意味を変える差分または契約逸脱があります (--print-diff で詳細表示)",
            file=sys.stderr,
        )
        return 1

    print("OK: 意味を変える差分はありません")
    return 0


if __name__ == "__main__":
    sys.exit(main())
