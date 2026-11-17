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
        for v in find_violations(s, ctx, check_terms=True):
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


def _items(rev: RevisionRow) -> list[tuple[str, str]]:
    out = [("implication", s) for s in split_sentences(rev.implication)]
    for field, raw in (("indicators", rev.indicators_json), ("missing", rev.missing_json)):
        out += [(field, str(i)) for i in json.loads(raw or "[]")]
    return out


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"{path.name}: {len(rows)} 行")


def dump_adjudication(
    repo: RunHistoryRepository,
    out: Path,
    days: int,
    owners: dict[str, frozenset[str]],
    anchors_by_sid: dict[str, set[str]],
) -> None:
    """直近 days 日の全版を検査し、裁定用の JSONL を書く (読み取りのみ)。"""
    from src.assessment.occurrence_floor import _relevant_ids  # noqa: PLC0415
    from src.synthesis.grounded.estimate import EvidenceItem, occurrence_floor

    out.mkdir(parents=True, exist_ok=True)
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    titles = {
        str(r["situation_id"]): str(r["title"])
        for r in _q(repo, "SELECT situation_id, title FROM situations", [])
    }
    revs = [
        SituationStore._row_to_revision(r)  # noqa: SLF001
        for r in _q(
            repo,
            "SELECT * FROM situation_revisions WHERE created_at >= ? ORDER BY situation_id, rev",
            [since],
        )
    ]
    fired: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    clean: dict[tuple[str, str, str], dict[str, Any]] = {}
    jp: dict[tuple[str, str], dict[str, Any]] = {}
    checked: dict[str, int] = {}
    hits: dict[str, int] = {}
    for rev in revs:
        sid = rev.situation_id
        anchors = anchors_by_sid.get(sid, set())
        own, ttl, _ = _material(repo, sid, rev.created_at, rev.claim, anchors)
        link = has_japan_link(
            claim=rev.claim, anchors=anchors, entity_keys=set(anchors), evidence_text=ttl
        )
        ctx = GateContext(sid, own, owners, link)
        base = {"situation_id": sid, "rev": rev.rev, "situation_title": titles.get(sid, "")}
        for field, text in _items(rev):
            vs = find_violations(text, ctx, check_terms=field == "implication")
            checked[field] = checked.get(field, 0) + 1
            if vs:
                hits[field] = hits.get(field, 0) + 1
                for v in vs:
                    fired.setdefault(
                        (sid, field, text, v.kind, v.token),
                        {
                            **base,
                            "field": field,
                            "item": text,
                            "violation": v.kind,
                            "token": v.token,
                        },
                    )
            else:
                clean.setdefault((sid, field, text), {**base, "field": field, "item": text})
            m = JP_IMPLICATION_RE.search(text) if field == "implication" else None
            if m and not link:
                jp.setdefault(
                    (sid, text),
                    {
                        **base,
                        "sentence": text,
                        "token": m.group(0),
                        "why": "証拠・anchor・claim に日本との接点なし",
                    },
                )
    occ: list[dict[str, Any]] = []
    since14 = (datetime.now(UTC) - timedelta(days=14)).isoformat()
    for rev in revs:
        if rev.rev != 1 or rev.created_at < since14:
            continue
        rows = _q(
            repo,
            "SELECT e.article_id, e.attribution_basis, e.source_tier, e.polarity, a.title"
            " FROM situation_evidence e LEFT JOIN articles a ON a.article_id=e.article_id"
            " WHERE e.situation_id=? AND e.assessed_at IS NOT NULL",
            [rev.situation_id],
        )
        ev = tuple(
            EvidenceItem(
                str(r["article_id"]),
                str(r["source_tier"] or ""),
                str(r["attribution_basis"]),
                "",
                str(r["polarity"]),
            )  # type: ignore[arg-type]
            for r in rows
        )
        relevant = _relevant_ids(ev, rev.claim, repo)
        floor = occurrence_floor(rev.claim_type, ev, relevant=relevant)
        if floor is None:
            continue
        seen: set[str] = set()
        basis_rows = []
        for r in rows:
            aid = str(r["article_id"])
            if (
                str(r["attribution_basis"]) in ("victim_disclosed", "govt_confirmed")
                and aid in relevant
                and aid not in seen
            ):
                seen.add(aid)
                basis_rows.append(
                    {
                        "article_id": aid,
                        "attribution_basis": str(r["attribution_basis"]),
                        "polarity": str(r["polarity"]),
                        "title": str(r["title"] or ""),
                    }
                )
        occ.append(
            {
                "situation_id": rev.situation_id,
                "situation_title": titles.get(rev.situation_id, ""),
                "claim": rev.claim,
                "claim_type": rev.claim_type,
                "current_confidence": rev.confidence,
                "leading_hypothesis": rev.leading_hypothesis,
                "occurrence_confidence": floor[0],
                "basis": floor[1],
                "evidence": basis_rows,
            }
        )
    _write(out / "impl_fired.jsonl", list(fired.values()))
    _write(out / "impl_not_fired.jsonl", list(clean.values()))
    _write(out / "jp_fired.jsonl", list(jp.values()))
    _write(out / "occurrence.jsonl", occ)
    print(f"版 {len(revs)} 件 / 対象 {days} 日")
    for f, n in checked.items():
        print(f"  {f}: 検査 {n} 件 / 発火 {hits.get(f, 0)} 件 ({hits.get(f, 0) / n:.1%})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", action="append", default=[])
    ap.add_argument("--revs", default="", help="カンマ区切りの rev (--sid 指定時)")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--known", action="store_true")
    ap.add_argument(
        "--adjudicate", default="", help="出力ディレクトリ (直近 --days 日の全版を JSONL へ)"
    )
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
    if args.adjudicate:
        dump_adjudication(repo, Path(args.adjudicate), args.days or 30, owners, anchors_by_sid)
        return
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
