"""Phase 1 のオーケストレータ (Step 8) — 薄い CLI エントリ + 後方互換 re-export。

エントリポイント: ``python -m src.main``。

フロー (CLAUDE.md §2 / claude_code_prompts.md Step 8 に準拠):
    1. 設定読み込み (.env + config/*.yaml)
    2. RSS / Grok / web scraper から記事を取得
    3. 各記事について:
       a. trafilatura で本文抽出
       b. 失敗時は summary_html を HTML タグ除去でフォールバック
       c. Ollama (Gemma 4) で要約・翻訳・BLUF 生成 (Jinja2 テンプレ)
       d. BriefingMessage に整形
    4. 重要度に応じたチャンネルへ Discord 投稿 (--dry-run 時は標準出力)
    5. 投稿結果を articles テーブルに永続化
    6. 集約結果を ``PipelineRunResult`` で返却

実装は ``src.pipeline`` パッケージへ責務別に分割済 (機械分割、挙動不変)。本ファイルは
CLI エントリ (``main`` / ``_build_arg_parser``) と、``from src.main import X`` を維持する
後方互換の re-export に徹する。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from src.config_loader import load_agents
from src.logging_config import get_logger

# ---------- 後方互換 re-export (旧 src.main の公開面を維持) ----------
# 分割前は本ファイルに全実装が集約されており、多数の呼び出し側が
# ``from src.main import X`` / ``src.main.X`` を使う。分割後もこれらを importable に
# 保つため、各実装モジュールから re-export する。
from src.pipeline.briefing import (  # noqa: F401
    DegenerateBodyError,
    _build_briefing,
    _degenerate_body_reason,
    _process_article,
    _resolve_body,
    _summarize_and_build,
)
from src.pipeline.dispatch import (  # noqa: F401
    DEFAULT_PIPELINE_NAME,
    DEFAULT_TEMPLATE_PATH,
    RUN_RESULTS_DIR,
    _find_pipeline,
    _load_template,
    _write_run_result_file,
    run_default,
)
from src.pipeline.filters import (  # noqa: F401
    _embedding_input_text,
    _filter_by_triage,
    _filter_duplicates,
    _filter_semantic_duplicates,
    _prefetch_thin_bodies,
    _try_build_embedder,
)
from src.pipeline.grok_convert import (  # noqa: F401
    _grok_article_to_briefings,
    _grok_jsonl_to_briefings,
    _grok_subarticle_id,
    _is_grok_article,
    _merge_grok_overlay,
    _strip_html_keep_newlines,
)
from src.pipeline.orchestrator import run_pipeline  # noqa: F401
from src.pipeline.persistence import (  # noqa: F401
    _classify_ioc_type,
    _persist_article_entities,
    _persist_article_outcomes,
    _persist_feed_health,
    _relevant_actors,
)
from src.pipeline.postprocess import (  # noqa: F401
    _dedup_briefings_by_source_url,
    _dedup_incidents_semantic,
    _is_ops_notify_recent_success,
    _maybe_post_system_notification,
    _sort_briefings_for_posting,
)
from src.pipeline.publish import (  # noqa: F401
    _build_publishers,
    _print_dry_run,
    _resolve_channel,
)
from src.pipeline.result import PipelineRunResult  # noqa: F401
from src.pipeline.runners import (  # noqa: F401
    _compose_daily_brief,
    _deliver_spotlight_discord,
    _deliver_synthesis_discord,
    _run_daily_brief_default,
    _run_digest_default,
    _run_mitre_actor_sync_default,
    _run_pir_spotlight_default,
    _run_status_synthesis_default,
    _run_taxonomy_review_default,
)
from src.pipeline.summary import (  # noqa: F401
    ArticleType,
    DiscordChannel,
    SummaryOutput,
    _cap_vuln_importance,
    _drop_self_url,
    _has_japanese_kana,
    _is_mostly_japanese,
    _normalize_iso_date,
    _normalize_temporal,
    _text_has_kev_cve,
)

# 一部の呼び出し側 (tests/unit/test_main.py) は ``from src.main import _strip_html`` を使う。
# 分割前は ``from src.tools.text_utils import strip_html as _strip_html`` を re-export していた。
from src.tools.text_utils import strip_html as _strip_html  # noqa: F401

_log = get_logger(__name__)

# mypy --strict は implicit re-export を禁じるため、後方互換 re-export を明示する。
# 呼び出し側 (src/, tests/, scripts/) の ``from src.main import X`` を維持するための面。
__all__ = [
    "DEFAULT_PIPELINE_NAME",
    "DEFAULT_TEMPLATE_PATH",
    "RUN_RESULTS_DIR",
    "ArticleType",
    "DegenerateBodyError",
    "DiscordChannel",
    "PipelineRunResult",
    "SummaryOutput",
    "_build_arg_parser",
    "_build_briefing",
    "_build_publishers",
    "_cap_vuln_importance",
    "_classify_ioc_type",
    "_compose_daily_brief",
    "_dedup_briefings_by_source_url",
    "_dedup_incidents_semantic",
    "_degenerate_body_reason",
    "_deliver_spotlight_discord",
    "_deliver_synthesis_discord",
    "_drop_self_url",
    "_embedding_input_text",
    "_filter_by_triage",
    "_filter_duplicates",
    "_filter_semantic_duplicates",
    "_find_pipeline",
    "_grok_article_to_briefings",
    "_grok_jsonl_to_briefings",
    "_grok_subarticle_id",
    "_has_japanese_kana",
    "_is_grok_article",
    "_is_mostly_japanese",
    "_is_ops_notify_recent_success",
    "_load_template",
    "_maybe_post_system_notification",
    "_merge_grok_overlay",
    "_normalize_iso_date",
    "_normalize_temporal",
    "_persist_article_entities",
    "_persist_article_outcomes",
    "_persist_feed_health",
    "_prefetch_thin_bodies",
    "_print_dry_run",
    "_process_article",
    "_relevant_actors",
    "_resolve_body",
    "_resolve_channel",
    "_run_daily_brief_default",
    "_run_digest_default",
    "_run_mitre_actor_sync_default",
    "_run_pir_spotlight_default",
    "_run_status_synthesis_default",
    "_run_taxonomy_review_default",
    "_sort_briefings_for_posting",
    "_strip_html",
    "_strip_html_keep_newlines",
    "_summarize_and_build",
    "_text_has_kev_cve",
    "_try_build_embedder",
    "_write_run_result_file",
    "main",
    "run_default",
    "run_pipeline",
]


# ---------- CLI ----------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kuebiko",
        description="毎朝 5 時実行用 CTI ブリーフィングパイプライン (Phase 1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discord に投稿せず標準出力にプレビュー表示",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="取得記事数の上限 (config/pipelines.yaml の値を上書き)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="ログレベルを DEBUG に設定",
    )
    parser.add_argument(
        "--pipeline",
        default=DEFAULT_PIPELINE_NAME,
        help=f"使用するパイプライン名 (default: {DEFAULT_PIPELINE_NAME})",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help=(
            "URL ハッシュ・意味類似度の重複排除を一時的に無効化する (再投稿テスト・見た目検証用)"
        ),
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help=(
            "PipelineRunner から渡される run_history.runs.id。"
            "指定時は articles テーブルに per-briefing 行を書き、終了時に "
            "data/run_results/{run_id}.json に PipelineRunResult を書き出す。"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.debug:
        os.environ["LOG_LEVEL"] = "DEBUG"

    # config_loader の load_agents は Phase 5 まで未参照だが、スケルトンの
    # 整合性チェックとして起動時に 1 度評価する。失敗しても続行。
    try:
        load_agents()
    except Exception as e:  # noqa: BLE001
        _log.warning("agents_yaml_load_failed", error=str(e))

    result = asyncio.run(
        run_default(
            dry_run=args.dry_run,
            max_articles=args.max_articles,
            pipeline_name=args.pipeline,
            skip_dedup=args.no_dedup,
            run_id=args.run_id,
        ),
    )

    if args.run_id is not None:
        _write_run_result_file(args.run_id, result)

    sys.stderr.write("\n=== Pipeline Summary ===\n")
    sys.stderr.write(f"  fetched     : {result.total_fetched}\n")
    sys.stderr.write(f"  skipped dup : {result.skipped_dup}\n")
    sys.stderr.write(f"  summarized  : {result.summarized}\n")
    sys.stderr.write(f"  posted      : {result.posted}\n")
    sys.stderr.write(f"  marked read : {result.marked_read}\n")
    sys.stderr.write(f"  errors      : {len(result.errors)}\n")
    sys.stderr.write(f"  dry-run     : {result.dry_run}\n")
    if result.errors:
        sys.stderr.write("--- errors ---\n")
        for err in result.errors:
            sys.stderr.write(f"  - {err}\n")

    # exit code: 部分失敗 (一部 article がエラーでも 1 件以上投稿成功) は **成功扱い (0)** とし、
    # runner 側で returncode==0 + errors → "partial_failure" status に倒させる。
    # 全件失敗 (投稿ゼロ かつ エラーあり) のみ 1 を返し "failed" とする。
    # (旧: errors が 1 件でもあれば 1 → 11/12 投稿成功でも run が "failed" 表示になっていた)
    if result.errors and result.posted == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
