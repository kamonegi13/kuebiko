"""Phase 5L 効果検証: 過去 24h の posted article を新パイプラインに通して旧分類と比較する。

dry-run mode (Discord 投稿なし、DB 書き込みなし、既読化なし)。
Ollama を実機で叩いて新 prompt + 新 router が現実のコンテンツに対してどう動くかを測定する。

実行:
    uv run python scripts/phase5l_replay.py
    uv run python scripts/phase5l_replay.py --limit 10  # まず 10 件で試す
    uv run python scripts/phase5l_replay.py --hours 6   # 6h ウィンドウに絞る

出力:
    - 標準出力: 進捗と最終サマリ
    - data/phase5l_replay_report.md: 詳細レポート (1 article 1 行)
    - data/phase5l_replay_report.json: 機械可読な比較結果
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

# repo root を sys.path に
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.main import _load_template, _process_article  # noqa: E402
from src.tools.article_model import Article  # noqa: E402
from src.tools.content_extractor import ContentExtractor  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

DB_PATH = Path("data/run_history.db")
REPORT_MD = Path("data/phase5l_replay_report.md")
REPORT_JSON = Path("data/phase5l_replay_report.json")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 5L 効果検証 replay")
    parser.add_argument("--hours", type=int, default=24, help="過去何時間分を replay するか")
    parser.add_argument("--limit", type=int, default=0, help="件数上限 (0=無制限)")
    parser.add_argument("--skip-grok", action="store_true", default=True, help="Grok 由来は除外")
    args = parser.parse_args()

    cfg = load_app_config()
    print(f"Ollama: {cfg.ollama_base_url}")
    print(f"main model: {cfg.ollama_main_model}")
    print(f"extract model: {cfg.ollama_extract_model or '(=main)'}")

    rows = _fetch_articles_from_db(hours=args.hours, limit=args.limit, skip_grok=args.skip_grok)
    if not rows:
        print("対象記事なし")
        return
    print(f"replay 対象: {len(rows)} 件")

    template = _load_template()
    llm = OllamaClient(base_url=cfg.ollama_base_url, model=cfg.ollama_main_model)

    results: list[dict[str, object]] = []
    async with ContentExtractor() as extractor:
        for idx, row in enumerate(rows, 1):
            article = _row_to_article(row)
            print(
                f"[{idx}/{len(rows)}] {row['article_id'][:40]}... → {article.url[:80]}",
                flush=True,
            )
            res: dict[str, object] = {
                "article_id": row["article_id"],
                "url": row["url"],
                "title_db": row["title"],
                "old_channel": row["posted_channel"],
                "old_importance": row["importance"],
                "old_category": row["category"],
            }
            try:
                briefing = await _process_article(article, extractor, llm, template, think=False)
            except Exception as e:  # noqa: BLE001
                res["new_channel"] = "ERROR"
                res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                results.append(res)
                continue
            res["new_channel"] = briefing.metadata.get("target_channel", "?")
            res["new_rule_id"] = briefing.metadata.get("routing_rule_id", "?")
            res["new_dedup_key"] = briefing.metadata.get("dedup_key", "")
            res["new_importance"] = briefing.importance
            res["new_category"] = briefing.category
            res["new_title"] = briefing.title
            res["routing_flags"] = briefing.metadata.get("routing_flags", {})
            results.append(res)

    _write_reports(results, args)
    _print_summary(results)


def _fetch_articles_from_db(*, hours: int, limit: int, skip_grok: bool) -> list[sqlite3.Row]:
    if not DB_PATH.exists():
        msg = f"DB not found: {DB_PATH}"
        raise FileNotFoundError(msg)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    where = ["status = 'posted'", f"created_at >= datetime('now', '-{hours} hours')"]
    if skip_grok:
        where.append("article_id NOT LIKE 'grok:%'")
    sql = (
        "SELECT article_id, url, title, importance, category, posted_channel, created_at "
        "FROM articles WHERE " + " AND ".join(where) + " ORDER BY created_at DESC"
    )
    if limit > 0:
        sql += f" LIMIT {limit}"
    rows = con.execute(sql).fetchall()
    con.close()
    return list(rows)


def _row_to_article(row: sqlite3.Row) -> Article:
    """DB 行から最小限の Article オブジェクトを構築 (replay 用)。

    summary_html は空 (extractor が URL から再取得する)。
    """
    return Article(
        id=row["article_id"],
        title=row["title"],
        url=row["url"],
        summary_html="",
        author=None,
        published=datetime.now(UTC),
        feed_title="",
        feed_url="",
    )


def _write_reports(results: list[dict[str, object]], args: argparse.Namespace) -> None:
    REPORT_JSON.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "hours": args.hours,
                "limit": args.limit,
                "total": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines: list[str] = []
    lines.append("# Phase 5L 効果検証 replay レポート")
    lines.append("")
    lines.append(f"- 生成日時: {datetime.now(UTC).isoformat()}")
    lines.append(f"- 対象期間: 過去 {args.hours} 時間")
    lines.append(f"- 件数: {len(results)}")
    lines.append("")

    # 集計
    old_counts: Counter[str] = Counter()
    new_counts: Counter[str] = Counter()
    transitions: Counter[tuple[str, str]] = Counter()
    error_count = 0
    dedup_clusters: dict[str, list[str]] = defaultdict(list)
    for r in results:
        old = str(r.get("old_channel") or "?")
        new = str(r.get("new_channel") or "?")
        old_counts[old] += 1
        new_counts[new] += 1
        transitions[(old, new)] += 1
        if new == "ERROR":
            error_count += 1
        key = str(r.get("new_dedup_key") or "")
        if key:
            dedup_clusters[key].append(str(r.get("article_id")))

    lines.append("## チャンネル分布の変化")
    lines.append("")
    lines.append("| ch | 旧 | 新 | 差分 |")
    lines.append("|---|---|---|---|")
    all_channels = set(old_counts.keys()) | set(new_counts.keys())
    for ch in sorted(all_channels):
        diff = new_counts[ch] - old_counts[ch]
        sign = "+" if diff > 0 else ""
        lines.append(f"| {ch} | {old_counts[ch]} | {new_counts[ch]} | {sign}{diff} |")
    lines.append("")

    lines.append("## 遷移マトリクス (旧 → 新)")
    lines.append("")
    lines.append("| 旧 ch | 新 ch | 件数 |")
    lines.append("|---|---|---|")
    for (old, new), c in sorted(transitions.items(), key=lambda x: -x[1]):
        marker = " ← 重要" if old != new else ""
        lines.append(f"| {old} | {new} | {c}{marker} |")
    lines.append("")

    multi_dedup = {k: v for k, v in dedup_clusters.items() if len(v) > 1}
    lines.append("## 同 dedup_key クラスタ (2 件以上、新規発見)")
    lines.append("")
    if multi_dedup:
        for key, ids in sorted(multi_dedup.items(), key=lambda x: -len(x[1])):
            lines.append(f"- `{key}`: {len(ids)} 件 ({', '.join(i[:30] for i in ids[:3])}...)")
    else:
        lines.append("- (なし)")
    lines.append("")

    lines.append(f"## エラー件数: {error_count}")
    lines.append("")

    lines.append("## 詳細 (各 article)")
    lines.append("")
    lines.append("| ID | 旧 ch | 新 ch | rule | dedup_key | title (新) |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        aid = str(r.get("article_id") or "")[-12:]
        old = r.get("old_channel") or "?"
        new = r.get("new_channel") or "?"
        rule = str(r.get("new_rule_id") or "")
        dkey = str(r.get("new_dedup_key") or "")[:40]
        title = str(r.get("new_title") or r.get("title_db") or "")[:60]
        marker = " **CHANGED**" if old != new and new != "ERROR" else ""
        lines.append(f"| {aid} | {old} | {new}{marker} | {rule} | {dkey} | {title} |")

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nレポート出力:\n  {REPORT_MD}\n  {REPORT_JSON}")


def _print_summary(results: list[dict[str, object]]) -> None:
    print()
    print("=" * 70)
    print("サマリ")
    print("=" * 70)

    old_counts: Counter[str] = Counter()
    new_counts: Counter[str] = Counter()
    changed = 0
    error_count = 0
    for r in results:
        old = str(r.get("old_channel") or "?")
        new = str(r.get("new_channel") or "?")
        old_counts[old] += 1
        new_counts[new] += 1
        if new == "ERROR":
            error_count += 1
        elif old != new:
            changed += 1

    print(f"\n総数: {len(results)} (うちエラー {error_count} 件)")
    print(f"channel 変化: {changed} 件\n")

    print("旧 channel 分布:")
    for ch, c in old_counts.most_common():
        print(f"  {ch:14s}: {c}")
    print("\n新 channel 分布:")
    for ch, c in new_counts.most_common():
        print(f"  {ch:14s}: {c}")


if __name__ == "__main__":
    asyncio.run(main())
