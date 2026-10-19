"""発表そのものが出来事の記事の basis を reported → disclosed へ遡及する (2026-10-19)。

取込側の規則 (``src/cti/publication_event``) と同じ判定を既存行へ適用する。
対象 = basis=reported の行のうち publication_kind が当たる行 (event_date は触らない)。

既定 dry-run。--apply で書く。書く前に変更前の値を退避表 ``_backup_publication_event_basis_<時刻>``
(id, event_date_basis) へ複製する。戻し方 (--apply 後に出力される表名を使う):
  UPDATE articles a SET event_date_basis = b.event_date_basis
    FROM _backup_publication_event_basis_<時刻> b WHERE a.id = b.id;

  docker exec -w /app kuebiko python -m scripts.backfill_publication_event_basis [--apply]
      [--preview data/eval/publication_as_event_preview.md] [--sample 20]
"""

from __future__ import annotations

import argparse
import random
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.cti.publication_event import publication_kind
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_SELECT = (
    "SELECT id, article_id, title, feed_title, feed_url, category, article_type, account_class,"
    " event_date, event_date_basis, published_at FROM articles WHERE event_date_basis = 'reported'"
)
DEFAULT_SAMPLE = 30
SAMPLE_SEED = 20261020
UPDATE_CHUNK = 500


def select_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """publication_kind が当たる行に kind を付けて返す (入力は変更しない)。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        kind = publication_kind(
            article_type=r.get("article_type"),
            category=r.get("category"),
            feed_title=r.get("feed_title"),
            feed_url=r.get("feed_url"),
            account_class=r.get("account_class"),
        )
        if kind:
            out.append({**r, "kind": kind})
    return out


def stratified_sample(targets: list[dict[str, Any]], sample: int) -> list[dict[str, Any]]:
    """種別ごとに均等に (端数は先頭の種別から) 抽出する。seed 固定。"""
    rng = random.Random(SAMPLE_SEED)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for t in targets:
        by_kind.setdefault(str(t["kind"]), []).append(t)
    kinds = sorted(by_kind)
    picked: list[dict[str, Any]] = []
    for i, k in enumerate(kinds):
        n = sample // len(kinds) + (1 if i < sample % len(kinds) else 0)
        picked.extend(rng.sample(by_kind[k], min(n, len(by_kind[k]))))
    return picked


def render_preview(targets: list[dict[str, Any]], sample: int) -> str:
    picked = stratified_sample(targets, sample)
    lines = [
        "# publication_as_event プレビュー (dry-run)",
        "",
        f"対象 {len(targets)} 行 (見出しのみ・本文なし)。標本 {len(picked)} 件。",
        "",
        "| # | 種別 | article_type | source | event_date | 旧 → 新 | 見出し |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(picked, 1):
        title = str(r["title"]).replace("|", "/")[:90]
        src = str(r.get("feed_title") or "").replace("|", "/")[:30]
        lines.append(
            f"| {i} | {r['kind']} | {r.get('article_type') or ''} | {src} | {r['event_date']} "
            f"| reported → disclosed | {title} |"
        )
    return "\n".join(lines) + "\n"


def _apply(con: Any, ids: list[int]) -> str:
    backup = "_backup_publication_event_basis_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    con.execute(
        f"CREATE TABLE {backup} (id BIGINT PRIMARY KEY, event_date_basis TEXT)"  # noqa: S608
    )
    for i in range(0, len(ids), UPDATE_CHUNK):
        chunk = ids[i : i + UPDATE_CHUNK]
        ph = ",".join("?" for _ in chunk)
        con.execute(
            f"INSERT INTO {backup} (id, event_date_basis)"  # noqa: S608 — backup は自前生成
            f" SELECT id, event_date_basis FROM articles WHERE id IN ({ph})",
            chunk,
        )
        con.execute(
            f"UPDATE articles SET event_date_basis = 'disclosed' WHERE id IN ({ph})",  # noqa: S608
            chunk,
        )
    return backup


def _run(apply: bool, preview: Path | None, sample: int) -> None:
    repo = RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = [dict(r) for r in con.execute(_SELECT).fetchall()]
        targets = select_targets(rows)
        print(f"=== 発表そのものが出来事の basis 遡及 ({mode}) ===")
        print(f"basis=reported {len(rows)} 行 / 対象 {len(targets)} 行")
        for kind, n in Counter(t["kind"] for t in targets).most_common():
            print(f"  {kind:10s} {n}")
        if preview is not None and targets:
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_text(render_preview(targets, sample), encoding="utf-8")
            print(f"プレビュー: {preview}")
        if not apply or not targets:
            print("(dry-run — --apply で実行)" if targets else "(対象なし)")
            return
        backup = _apply(con, [int(t["id"]) for t in targets])
        print(f"UPDATE {len(targets)} 行 (退避 = {backup})")
    _log.info("backfill_publication_event_basis_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="発表そのものが出来事の basis 遡及")
    ap.add_argument("--apply", action="store_true", help="実際に書く (既定は dry-run)")
    ap.add_argument("--preview", type=Path, default=None, help="標本の出力先 (md)")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    a = ap.parse_args()
    _run(a.apply, a.preview, a.sample)


if __name__ == "__main__":
    main()
