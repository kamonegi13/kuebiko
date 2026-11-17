"""台帳の過去の改訂に含意・指標・欠けている証拠の関門を当てる再現 (読み取りのみ、設計 §4・§7)。

``LEDGER_IMPL_GROUNDING`` と ``LEDGER_JP_IMPLICATION`` の検出器を ``situation_revisions`` の
過去の版に当て、違反を一覧する (DB へは書かない。接続は PGOPTIONS で read-only)。

照合の材料は「その版の時点まで (created_at 以前) にその状況へ評価された証拠の記事の見出し・本文
冒頭 + 抜粋 + claim + anchor の値」で、本番の「束ねた根拠」の近似 (版ごとの束ねた記事の記録は
無い)。事前登録の再現 (既知 4 件の検出) 用。しきい値を合わせにいかない。

使い方 (DATABASE_URL は PG の読み取り接続):
  uv run python scripts/ledger_revision_grounding_replay.py --sid s-0d2b261633bd --revs 9
  uv run python scripts/ledger_revision_grounding_replay.py --days 7 --limit 200
  --known  既知 4 件 (APT41 rev9 / ShinyHunters rev31 / TraderTraitor rev23-28 / wp2shell rev39-40)
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
os.environ.setdefault("PGOPTIONS", "-c default_transaction_read_only=on")

import src.storage.db_backend as _db  # noqa: E402

_db.ensure_pg_schema = lambda *a, **k: None  # DDL を流さない (読み取りのみ)

from src.assessment.revision_grounding import (  # noqa: E402
    JP_IMPLICATION_RE,  # noqa: E402
    GateContext,
    anchor_values_text,
    find_violations,
    has_japan_link,
    split_sentences,
    subject_owner_index,
)
from src.assessment.situation_store import RevisionRow, SituationStore  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

KNOWN: dict[str, tuple[int, ...]] = {
    "s-0d2b261633bd": (9,),
    "s-9203032bbb9a": (31,),
    "s-9733d7293e6a": tuple(range(23, 29)),
    "s-3286a9e3a747": (39, 40),
}
_BODY_CHARS = 6000
_MAX_EVIDENCE = 60


def _q(repo: RunHistoryRepository, sql: str, params: list[Any]) -> list[Any]:
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        return list(conn.execute(sql, params).fetchall())


def _material(repo: RunHistoryRepository, sid: str, until: str, claim: str, anchors: set[str]):
    rows = _q(
        repo,
        "SELECT article_id, excerpt FROM situation_evidence WHERE situation_id=?"
        " AND assessed_at IS NOT NULL AND assessed_at <= ? ORDER BY assessed_at DESC LIMIT ?",
        [sid, until, _MAX_EVIDENCE],
    )
    parts = [claim]
    titles: list[str] = []
    for r in rows:
        parts.append(str(r["excerpt"] or ""))
        g = repo.get_article_grounding(str(r["article_id"]))
        if g:
            titles.append(g["title"])
            parts.append(f"{g['title']} {g['text'][:_BODY_CHARS]}")
    parts.append(anchor_values_text(anchors))
    return "\n".join(parts), "\n".join(titles), [str(r["article_id"]) for r in rows]


def check_revision(
    repo: RunHistoryRepository,
    rev: RevisionRow,
    anchors: set[str],
    owners: dict[str, frozenset[str]],
) -> list[tuple[str, str, str, str]]:
    own, titles, aids = _material(repo, rev.situation_id, rev.created_at, rev.claim, anchors)
    keys = set(anchors)
    ctx = GateContext(
        situation_id=rev.situation_id,
        own_text=own,
        foreign_owners=owners,
        has_japan_link=has_japan_link(
            claim=rev.claim, anchors=anchors, entity_keys=keys, evidence_text=titles
        ),
    )
    out: list[tuple[str, str, str, str]] = []
    for s in split_sentences(rev.implication):
        for v in find_violations(s, ctx, check_numbers=True):
            out.append(("implication", v.kind, v.token, s[:60]))
        m = JP_IMPLICATION_RE.search(s)
        if m and not ctx.has_japan_link:
            out.append(("implication", "jp_implication", m.group(0), s[:60]))
    for field, raw in (("indicators", rev.indicators_json), ("missing", rev.missing_json)):
        for item in json.loads(raw or "[]"):
            for v in find_violations(str(item), ctx):
                out.append((field, v.kind, v.token, str(item)[:60]))
    _ = aids
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", action="append", default=[])
    ap.add_argument("--revs", default="", help="カンマ区切りの rev (--sid 指定時)")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--known", action="store_true")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    store = SituationStore()
    situations = store.load_situations(("active", "dormant"))
    owners = subject_owner_index(situations)
    anchors_by_sid = {r.situation_id: set(r.anchors) for r in situations}

    targets: list[tuple[str, int | None]] = []
    if args.known:
        targets += [(s, r) for s, revs in KNOWN.items() for r in revs]
    if args.sid:
        revs = [int(x) for x in args.revs.split(",") if x] or [None]  # type: ignore[list-item]
        targets += [(s, r) for s in args.sid for r in revs]
    if args.days:
        since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
        for sid, lst in store.revisions_since(since).items():
            targets += [(sid, r.rev) for r in lst[-3:]][: args.limit]
    detected: dict[str, int] = {}
    for sid, rev_no in targets[: max(args.limit, len(targets) if args.known else 0)]:
        rows = _q(
            repo,
            "SELECT * FROM situation_revisions WHERE situation_id=?"
            + (" AND rev=?" if rev_no is not None else " ORDER BY rev DESC LIMIT 1"),
            [sid] + ([rev_no] if rev_no is not None else []),
        )
        if not rows:
            print(f"{sid} rev={rev_no}: 該当なし")
            continue
        rev = store._row_to_revision(rows[0])  # noqa: SLF001
        found = check_revision(repo, rev, anchors_by_sid.get(sid, set()), owners)
        print(f"== {sid} rev{rev.rev}: 違反 {len(found)} 件")
        for field, kind, token, ctx_s in found:
            print(f"   [{field}] {kind}: {token}  <- {ctx_s}")
        if found:
            detected[sid] = detected.get(sid, 0) + 1
    if args.known:
        print(
            f"\n既知 4 件の検出: {len([s for s in KNOWN if s in detected])}/4  {sorted(detected)}"
        )


if __name__ == "__main__":
    main()
