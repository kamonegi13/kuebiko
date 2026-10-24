"""事象の焦点の遡及と机上の数字の再現 (docs/eventnews_focus_redesign.md 付録、2026-10-24 F0)。

既定は **dry-run** (本番 DB は SELECT のみ。接続は PGOPTIONS で read-only、DDL も流さない)。
次の 3 つを出す:

  1. 正解つきの統合 (``--labels``: absorbed / target / merged_at / label を持つ jsonl。既定は
     data/eval の統合の正解 2 本) の焦点の照合。誤りのうち conflict+lineage で止まる数と、
     正しい統合のうち止まる数 (設計 §8.2・§8.3 の机上の数字が本番のコードで再現するか)。
     統合先は統合の時点より前に入っていたメンバーだけ、吸収側は全メンバー。
  2. 直近 ``--days`` 日の統合 (``merged_into`` のある事象、更新時刻を統合時刻とみなす) の
     照合の 4 値の分布 (設計 §8.5)。
  3. 直近 ``--days`` 日に動いた複数記事の生きた事象の焦点の大きさ・空の割合・外れの多い事象の数。

``--apply`` は **本番へ書く**: ``record_focuses`` で焦点を ``event_item_focus`` へ遡及する
(毎時の段と同じ関数。flag ``EVENTNEWS_FOCUS_RECORD=0`` なら何もしない)。
``--verify N`` は記録済みの焦点 N 件を再生して DB の値と突き合わせる (F0 の検算)。

⚠ 近似: 頻出ガードの分母は現在の窓 (記事の時期ではない)・被害組織の別名判定は使わない・
既に on の関門 (伏字・一括勧告の橋・月次レポート) との重なりは数えない。

使い方:
  set -a; . ./.env; set +a
  DATABASE_URL="postgresql://kuebiko:${POSTGRES_PASSWORD}@127.0.0.1:5433/kuebiko" \\
  uv run python scripts/focus_replay.py --days 30 [--labels a.jsonl b.jsonl] [--out result.jsonl]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 本番の既定 (compose)。import 前に置く — 結合信号の名前の作り方 (法人格の畳み・伏字の除外) が変わる
os.environ.setdefault("EVENTNEWS_ORG_NORM", "1")
os.environ.setdefault("EVENTNEWS_MASKED_VICTIM_GATE", "1")
_APPLY = "--apply" in sys.argv
if not _APPLY:
    os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import src.storage.db_backend as _db  # noqa: E402

if not _APPLY:
    _db.ensure_pg_schema = lambda *a, **k: None  # DDL を流さない

from src.eventnews.identity.focus import Focus, match_focus  # noqa: E402
from src.eventnews.identity.replay import (  # noqa: E402
    CLASSES,
    class_shares,
    final_label,
    focus_sizes,
    members_before,
    size_stats,
    tally_labeled,
)
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, MemberArticle  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.ui.services.eventnews_focus_job import (  # noqa: E402
    focus_of_members,
    order_members,
    record_focuses,
    verify_recorded_focuses,
)
from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members  # noqa: E402


def _default_labels() -> tuple[Path, ...]:
    """worktree には data/ が無いので、上へたどって最初の data/eval を使う。"""
    for base in (ROOT, *ROOT.parents):
        found = base / "data" / "eval"
        if found.is_dir():
            return (found / "merge_labels_opus.jsonl", found / "merge_labels_opus_post.jsonl")
    return ()


DEFAULT_LABELS = _default_labels()
_DISPLAY_LIMIT = 12


def _q(repo: RunHistoryRepository, sql: str, params: list[Any] | None = None) -> list[Any]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        return list(conn.execute(sql, params or []).fetchall())


def _load_label_rows(paths: list[Path]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            print(f"(正解ファイルが無い: {path})", file=sys.stderr)
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            key = (str(row["absorbed"]), str(row["target"]))
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


class Loader:
    """事象のメンバーを参加順に引き、MemberArticle にする (一括読み込みで往復を減らす)。"""

    def __init__(self, repo: RunHistoryRepository) -> None:
        self._repo = repo
        self._counts = _entity_counts(
            repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS)
        )
        self._rows: dict[str, list[tuple[str, str]]] = {}
        self._articles: dict[str, MemberArticle] = {}

    def prefetch(self, item_ids: list[str]) -> None:
        need = [i for i in dict.fromkeys(item_ids) if i not in self._rows]
        for i in range(0, len(need), 400):
            chunk = need[i : i + 400]
            got = self._repo.list_members_in_join_order(chunk)
            for item in chunk:
                self._rows[item] = got.get(item, [])
        want = sorted(
            {a for i in item_ids for a, _ in self._rows.get(i, []) if a not in self._articles}
        )
        for i in range(0, len(want), 1500):
            self._articles.update(_load_members(self._repo, want[i : i + 1500], self._counts))

    def focus(self, item_id: str, *, before: str | None = None) -> Focus:
        rows = self._rows.get(item_id, [])
        if before is not None:
            rows = members_before(rows, before)
        return focus_of_members(order_members(rows, self._articles))


def _labeled_section(
    repo: RunHistoryRepository, loader: Loader, paths: list[Path], out: list[dict[str, Any]]
) -> None:
    rows = _load_label_rows(paths)
    print(f"\n## 1. 正解つきの統合 ({len(rows)} 組)")
    if not rows:
        return
    loader.prefetch([str(r[k]) for r in rows for k in ("absorbed", "target")])
    results: list[tuple[str, str]] = []
    for row in rows:
        label = final_label(row)
        target = loader.focus(str(row["target"]), before=str(row["merged_at"]))
        absorbed = loader.focus(str(row["absorbed"]))
        verdict = match_focus(target, absorbed)
        results.append((verdict.klass, label))
        out.append(
            {
                "section": "labeled",
                "absorbed": row["absorbed"],
                "target": row["target"],
                "label": label,
                "class": verdict.klass,
                "reason": verdict.reason,
                "conflicts": list(verdict.conflicts),
            }
        )
    print("\n".join(tally_labeled(results).as_lines()))


def _recent_merges_section(
    repo: RunHistoryRepository, loader: Loader, since: datetime, out: list[dict[str, Any]]
) -> None:
    rows = _q(
        repo,
        "SELECT id, merged_into, updated_at FROM event_items"
        " WHERE merged_into IS NOT NULL AND updated_at >= ?",
        [since.isoformat()],
    )
    print(f"\n## 2. 直近の統合 ({len(rows)} 件、吸収された事象)")
    if not rows:
        return
    loader.prefetch([str(r["id"]) for r in rows] + [str(r["merged_into"]) for r in rows])
    classes: list[str] = []
    for r in rows:
        merged_at = _iso(r["updated_at"])
        target = loader.focus(str(r["merged_into"]), before=merged_at)
        verdict = match_focus(target, loader.focus(str(r["id"])))
        classes.append(verdict.klass)
        out.append(
            {
                "section": "recent",
                "absorbed": str(r["id"]),
                "target": str(r["merged_into"]),
                "class": verdict.klass,
                "reason": verdict.reason,
            }
        )
    shares = class_shares(classes)
    print(" / ".join(f"{k} {shares[k][0]} ({shares[k][1]:.0%})" for k in CLASSES))
    stopped = shares["conflict"][0] + shares["lineage"][0]
    print(f"F2 で止まる統合 (conflict+lineage): {stopped} ({stopped / len(rows):.0%})")


def _live_section(repo: RunHistoryRepository, loader: Loader, since: datetime) -> None:
    ids = [
        str(r["id"])
        for r in _q(
            repo,
            "SELECT i.id AS id FROM event_items i WHERE i.origin='live' AND i.merged_into IS NULL"
            " AND i.last_reported_at >= ? AND"
            " (SELECT COUNT(*) FROM event_item_members m WHERE m.item_id=i.id) >= 2",
            [since.isoformat()],
        )
    ]
    print(f"\n## 3. 生きた複数記事の事象 ({len(ids)} 件)")
    if not ids:
        return
    loader.prefetch(ids)
    focuses = [loader.focus(i) for i in ids]
    empty = sum(1 for f in focuses if f.is_empty)
    heavy = sum(
        1
        for f in focuses
        if (f.supporters + f.outsiders) and f.outsiders / (f.supporters + f.outsiders) >= 0.2
    )
    median, p90, biggest = size_stats(focus_sizes(focuses))
    print(f"焦点が空 {empty} ({empty / len(ids):.1%}) / 外れ 20% 以上 {heavy}")
    print(f"焦点の大きさ 中央値 {median:g} / P90 {p90:g} / 最大 {biggest}")


def _verify(repo: RunHistoryRepository, count: int) -> None:
    ids = [
        str(r["item_id"])
        for r in _q(
            repo, "SELECT item_id FROM event_item_focus ORDER BY updated_at DESC LIMIT ?", [count]
        )
    ]
    bad = verify_recorded_focuses(repo, ids)
    print(f"\n## 検算: 記録済み {len(ids)} 件を再生 → 食い違い {len(bad)} 件")
    for item in bad[:_DISPLAY_LIMIT]:
        print(f"  {item}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--labels", nargs="*", type=Path, default=list(DEFAULT_LABELS))
    parser.add_argument("--out", type=Path, default=None, help="組ごとの結果の jsonl")
    parser.add_argument("--apply", action="store_true", help="焦点を本番 DB へ書く (遡及)")
    parser.add_argument("--verify", type=int, default=0, metavar="N", help="記録済み N 件を検算")
    args = parser.parse_args()

    repo = RunHistoryRepository()
    since = datetime.now(UTC) - timedelta(days=args.days)
    if args.apply:
        stats = record_focuses(repo)
        print(f"遡及: {stats.as_dict()}")
        return 0
    loader = Loader(repo)
    out: list[dict[str, Any]] = []
    _labeled_section(repo, loader, args.labels, out)
    _recent_merges_section(repo, loader, since, out)
    _live_section(repo, loader, since)
    if args.verify:
        _verify(repo, args.verify)
    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8"
        )
        print(f"\n組ごとの結果: {args.out} ({len(out)} 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
