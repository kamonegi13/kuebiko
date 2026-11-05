#!/usr/bin/env python3
"""GPU-B / GPU-A2 の detect の入力の固定: 両腕が共有する 20 窓の入力を inputs.json に凍結する。

仕様は docs/replay_prereg/detect_decompose.md (§2.1・§4・§9)。``DETECT_GPU_PROFILE=rejudge`` なら
docs/replay_prereg/detect_decompose_rejudge.md (§2.1〜2.3・§4・§9、区間の開始時点の台帳を再構成)。
**DB は読み取り専用** (接続を
read-only セッションにし、SHOW で確かめる)。LLM は呼ばない (題名の埋込だけ Ollama を使う)。

    python scripts/gpu_window/detect_snapshot.py --attest          # ホスト: git の検査 + 証明書
    docker compose run --rm --no-deps -T kuebiko \\
        python scripts/gpu_window/detect_snapshot.py --dry-run
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/detect_snapshot.py

窓の入力 = 事象時刻 (``EVENT_TS_EXPR``) が区間に入る posted・high/medium の記事 → 区間の開始前に
開いていた情勢への規則割当 (``match_situation``) の残余 → detect ML で絞った候補。
本番との差 (manifest にも残す): 割当の埋込の関門 (assign gate) は当てない / 追跡中の情勢の
証拠 (seed 重心) は現在の台帳から読む / 種別・軸は保存済みの値だけ (欠けは other・軸なし)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import detect_attest  # noqa: E402
import detect_common as dc  # noqa: E402

dc.install_read_only_env()  # 以降の import が接続しても read-only (PGOPTIONS)

POOL_LIMIT = 500  # stateful._STATEFUL_POOL_LIMIT / _DETECT_INPUT_MAX と同値
_AXIS_COLS = (
    ("P", "pmesii_p"),
    ("M", "pmesii_m"),
    ("E", "pmesii_e"),
    ("S", "pmesii_s"),
    ("I-infra", "pmesii_i_infra"),
    ("I-cyber", "pmesii_i_cyber"),
    ("P-env", "pmesii_p_env"),
    ("T", "pmesii_t"),
)


def first_value(row: Any) -> Any:
    if row is None:
        return None
    if hasattr(row, "values"):
        return next(iter(row.values()))
    return row[0]


def open_repo() -> tuple[Any, Any]:
    """読み取り専用の repo / store。ctor の DDL を避け、接続を read-only にする。"""
    dc.install_read_only_env()
    from src.assessment.situation_store import SituationStore
    from src.storage import db_backend
    from src.storage.run_history import DEFAULT_DB_PATH, RunHistoryRepository

    if not db_backend.is_pg_enabled():
        raise SystemExit("DATABASE_URL が無く SQLite に落ちる。使い捨てのコンテナで実行する")
    dc.prefer_read_only_role(os.environ)  # 専用の読み取りロールがあればそれで繋ぐ
    dc.install_read_only_connect()
    repo = RunHistoryRepository.__new__(RunHistoryRepository)
    repo._db_path = Path(DEFAULT_DB_PATH)  # noqa: SLF001
    store = SituationStore.__new__(SituationStore)
    store._repo = repo  # noqa: SLF001
    with repo._connect() as conn:  # noqa: SLF001
        dc.assert_read_only_setting(
            first_value(conn.execute("SHOW default_transaction_read_only").fetchone())
        )
    return repo, store


def pool_for_bin(repo: Any, b: dc.Bin) -> list[dict[str, Any]]:
    """本番の ``_build_high_importance_cross_axis`` と同じ形の dict を、区間 (事象時刻) で引く。"""
    from src.cti.source_basis import classify_source_tier
    from src.storage.event_time import EVENT_TS_EXPR
    from src.synthesis.grounded.chronology import article_chronology

    anchor = EVENT_TS_EXPR.format(a="articles")
    sql = (
        "SELECT article_id, title, feed_title, feed_url, importance, editorial_stance,"  # noqa: S608
        " created_at, event_date, event_date_basis,"
        " pmesii_p, pmesii_m, pmesii_e, pmesii_s,"
        " pmesii_i_infra, pmesii_i_cyber, pmesii_p_env, pmesii_t"
        " FROM articles WHERE status='posted' AND importance IN ('high','medium')"
        f" AND {anchor} >= ? AND {anchor} < ?"
        " ORDER BY CASE importance WHEN 'high' THEN 0 ELSE 1 END, created_at DESC LIMIT ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql, (b.start.isoformat(), b.end.isoformat(), POOL_LIMIT)).fetchall()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        aid = str(r["article_id"])
        if aid in seen:  # articles は同一 article_id が複数行ありうる
            continue
        seen.add(aid)
        chrono = article_chronology(
            report=str(r["created_at"] or ""),
            event_date=str(r["event_date"] or ""),
            event_date_basis=str(r["event_date_basis"] or ""),
        )
        out.append(
            {
                "title": r["title"],
                "feed_title": r["feed_title"],
                "article_id": aid,
                "importance": str(r["importance"] or ""),
                "axes": [k for k, col in _AXIS_COLS if r[col]],
                "reliability": classify_source_tier(
                    r["feed_title"] or "", str(r["feed_url"] or "")
                ),
                "stance": str(r["editorial_stance"] or ""),
                "chronology": chrono.label,
                "resurfaced": chrono.resurfaced,
            }
        )
    return out


def open_at(rows: Sequence[Any], start: str) -> list[Any]:
    """区間の開始より前に開き、その時点で閉じていなかった情勢 (今の status では絞らない)。"""
    return [r for r in rows if r.opened_at < start and (r.closed_at is None or r.closed_at > start)]


def dormant_after_days(domain: str) -> int:
    """本番の休眠の日数 (``ledger._sweep_lifecycle`` と同じ表)。"""
    from src.assessment import ledger

    table: dict[str, int] = ledger._DORMANT_AFTER_DAYS  # noqa: SLF001
    default: int = ledger._DORMANT_DEFAULT_DAYS  # noqa: SLF001
    return table.get(domain, default)


def as_of_status(*, kind: str, domain: str, last_evidence_iso: str, start: datetime) -> str:
    """区間の開始時点の active / dormant (rejudge §2.3)。standing は休眠しない。"""
    if kind == "standing":
        return "active"
    try:
        last = datetime.fromisoformat(last_evidence_iso)
    except ValueError:
        return "active"
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    idle_days = (start - last).total_seconds() / 86400
    return "dormant" if idle_days >= dormant_after_days(domain) else "active"


def as_of_title(current: str, revision: Any) -> str:
    """区間の開始時刻以前の最新 revision の claim (空なら今の題名)。"""
    claim = str(getattr(revision, "claim", "") or "").strip() if revision is not None else ""
    return claim or current


def last_evidence_before(repo: Any, start: str) -> dict[str, str]:
    """情勢ごとの、区間の開始より前の最後の証拠時刻。"""
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT situation_id, MAX(added_at) AS last FROM situation_evidence"
            " WHERE added_at < ? GROUP BY situation_id",
            (start,),
        ).fetchall()
    return {str(r["situation_id"]): str(r["last"]) for r in rows}


def situations_as_of(
    repo: Any, store: Any, b: dc.Bin
) -> tuple[list[Any], dict[str, Any], dict[str, int]]:
    """(区間の開始時点の情勢, situation_id → 開始時刻以前の最新 revision, 再構成の計数)。

    decompose (前回) は今の題名のまま全件を active にする。rejudge は §2.3 の再構成
    (題名 = 開始時刻以前の最新 revision の claim、休眠 = 開始前の最後の証拠からの経過日数)。
    """
    start = b.start.isoformat()
    rows = open_at(store.load_situations(("active", "dormant", "closed")), start)
    revs = {
        r.situation_id: store.latest_revision_before(r.situation_id, until_iso=start) for r in rows
    }
    if not dc.PROFILE.as_of_strict:
        return [replace(r, status="active") for r in rows], revs, {}
    last = last_evidence_before(repo, start)
    out: list[Any] = []
    counts = {"no_revision": 0, "retitled": 0, "dormant": 0}
    for r in rows:
        rev = revs[r.situation_id]
        title = as_of_title(r.title, rev)
        status = as_of_status(
            kind=r.kind,
            domain=r.domain,
            last_evidence_iso=last.get(r.situation_id, r.opened_at),
            start=b.start,
        )
        counts["no_revision"] += int(rev is None)
        counts["retitled"] += int(title != r.title)
        counts["dormant"] += int(status == "dormant")
        out.append(replace(r, title=title, status=status))
    return out, revs, counts


CURRENT_AXES_TAG = "s24ci65"


def axes_reannotated(repo: Any, ids: Sequence[str]) -> int:
    """深刻度の軸が今のモデル (s24ci65) で付いている記事の数 (rejudge §10 の偏り 2)。"""
    if not ids:
        return 0
    ph = ",".join("?" for _ in ids)
    sql = (
        f"SELECT COUNT(*) AS n FROM article_severity_axes WHERE article_id IN ({ph})"  # noqa: S608
        " AND model LIKE ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute(sql, (*ids, f"%{CURRENT_AXES_TAG}%")).fetchone()
    return int(first_value(row) or 0)


def stage1(repo: Any, store: Any, b: dc.Bin) -> dict[str, Any]:
    """Ollama を使わない段: プール → 規則割当の残余 → detect ML で絞った候補。"""
    from src.assessment.assignment import build_article_keys, build_situation_keys, match_situation
    from src.synthesis.grounded import detect_ml

    pool = pool_for_bin(repo, b)
    ids = [str(a["article_id"]) for a in pool]
    entities = repo.entity_keys_for_articles(ids)
    sits, revs, as_of_counts = situations_as_of(repo, store, b)
    sit_keys = []
    for r in sits:
        rev = revs.get(r.situation_id)
        sit_keys.append(
            build_situation_keys(r, claim_type=rev.claim_type if rev and rev.claim_type else "")
        )
    art_keys = {}
    unassigned: list[dict[str, Any]] = []
    for a in pool:
        aid = str(a["article_id"])
        keys = build_article_keys(
            article_id=aid, title=str(a["title"]), entity_keys=frozenset(entities.get(aid, set()))
        )
        art_keys[aid] = keys
        if match_situation(keys, sit_keys) is None:
            unassigned.append(a)
    detect_input = unassigned[:POOL_LIMIT]
    scores: dict[str, float] = {}
    kinds_out: dict[str, str] = {}
    floor: set[str] = set()
    missing_kinds = 0
    model = detect_ml.load_detect_model()
    dids = [str(a["article_id"]) for a in detect_input]
    if model is not None and dids:
        cached = repo.get_article_kinds(dids)
        missing_kinds = sum(1 for d in dids if d not in cached)
        kinds = {d: cached.get(d, "other") for d in dids}
        arts = detect_ml.build_detect_articles(repo, dids, kinds)
        scores = detect_ml.score_articles(model, arts)
        kinds_out = {a: x.kind for a, x in arts.items()}
        floor = detect_ml.floor_article_ids(arts)
    top_k = detect_ml.prefilter_top_k()
    rollup = {
        str(a["article_id"])
        for a in detect_input
        if detect_ml.is_rollup_title(str(a.get("title", "") or ""))
    }
    high = floor or {str(a["article_id"]) for a in detect_input if a["importance"] == "high"}
    keep = set(
        detect_ml.compose_llm_candidates(
            scores if scores else dict.fromkeys(dids, 0.0),
            top_k=top_k if scores else 0,
            high_ids=high,
            excluded=rollup,
        )
    )
    llm_input = [a for a in detect_input if str(a["article_id"]) in keep]
    return {
        "pool": pool,
        "entities": {a: sorted(v) for a, v in entities.items()},
        "situations": sits,
        "sit_keys": sit_keys,
        "art_keys": art_keys,
        "detect_input": detect_input,
        "llm_input": llm_input,
        "ml_scores": scores,
        "ml_kinds": kinds_out,
        "stats": {
            "pool": len(pool),
            "unassigned": len(unassigned),
            "candidates": len(llm_input),
            "situations_as_of": len(sits),
            "active_as_of": sum(1 for r in sits if r.status == "active"),
            "detect_input": len(detect_input),
            "missing_kinds": missing_kinds,
            "rollup_dropped": len(rollup),
            "top_k": top_k if scores else 0,
            "axes_reannotated": axes_reannotated(repo, dids) if dc.PROFILE.as_of_strict else None,
            **{f"as_of_{k}": v for k, v in as_of_counts.items()},
        },
    }


#: rejudge §2.2 (4)(5) の上限
MAX_NO_REVISION_SHARE = 0.10
MAX_MISSING_KIND_SHARE = 0.25


def eligibility(stats: dict[str, Any], *, strict: bool | None = None) -> str | None:
    """§2.1 の除外 (1)(2) (+ rejudge の (4)(5))。適格なら None。(3) は凍結時に検出する。"""
    strict = dc.PROFILE.as_of_strict if strict is None else strict
    if stats["candidates"] < dc.MIN_CANDIDATES:
        return "candidates_lt5"
    if stats.get("active_as_of", stats["situations_as_of"]) == 0:
        return "no_active_situations"
    if not strict:
        return None
    n_sits = stats["situations_as_of"]
    if n_sits and stats.get("as_of_no_revision", 0) / n_sits > MAX_NO_REVISION_SHARE:
        return "as_of_title_unrecoverable"
    n_in = stats.get("detect_input", 0)
    if n_in and stats.get("missing_kinds", 0) / n_in > MAX_MISSING_KIND_SHARE:
        return "missing_kinds"
    return None


def situation_dict(r: Any, claim_type: str) -> dict[str, Any]:
    d = asdict(r)
    d["anchors"] = sorted(r.anchors)
    d["pir_ids"] = list(r.pir_ids)
    d["claim_type"] = claim_type
    return d


async def stage2(repo: Any, store: Any, b: dc.Bin, s1: dict[str, Any]) -> dict[str, Any]:
    """Ollama (埋込) を使う段: 追跡中の題名の絞り・合流の採点・repo の代役に渡す材料。"""
    from src.assessment import detect_decompose as dd
    from src.assessment import stateful

    llm_ids = [str(a["article_id"]) for a in s1["llm_input"]]
    all_ids = [str(a["article_id"]) for a in s1["detect_input"]]
    active_titles = await stateful.detect_active_titles(s1["situations"], s1["detect_input"], repo)
    merge: dict[str, Any] | None = None
    merge_error = ""
    try:
        scores, sids = await dd.score_merge_pairs(
            candidate_ids=llm_ids,
            active_titles=active_titles,
            situation_keys=s1["sit_keys"],
            art_keys=s1["art_keys"],
            repo=repo,
            store=store,
            save_fresh=False,
        )
        merge = {"sids": sids, "scores": [[s, a, ok, p] for (s, a), (ok, p) in scores.items()]}
    except Exception as exc:  # noqa: BLE001 - 合流なしで凍結し、記録を残す
        merge_error = type(exc).__name__
    recs = repo.get_articles_by_ids(all_ids)
    bodies = repo.get_article_bodies(all_ids)
    emb = repo.load_summary_embeddings(all_ids)
    items = repo.event_item_ids_by_article(all_ids)
    articles = {
        a: {
            "summary": str(rec.summary or ""),
            "feed_title": str(rec.feed_title or ""),
            "created_at": str(rec.created_at or ""),
            "body_prefix": str(bodies.get(a, ""))[: dc.BODY_PREFIX_CHARS],
        }
        for a, rec in recs.items()
    }
    return {
        "active_titles": active_titles,
        "merge": merge,
        "merge_error": merge_error,
        "articles": articles,
        "event_items": {a: sorted(v) for a, v in items.items()},
        "embeddings": {a: dc.encode_vec(v) for a, v in emb.items()},
        "missing_articles": [a for a in all_ids if a not in articles],
    }


def build_window(b: dc.Bin, index: int, s1: dict[str, Any], s2: dict[str, Any]) -> dict[str, Any]:
    claim_types = {k.row.situation_id: k.claim_type for k in s1["sit_keys"]}
    return {
        "window_id": b.key,
        "index": index,
        "start": b.start.isoformat(),
        "end": b.end.isoformat(),
        "day": b.day,
        "detect_input": s1["detect_input"],
        "llm_input_ids": [str(a["article_id"]) for a in s1["llm_input"]],
        "ml_scores": s1["ml_scores"],
        "ml_kinds": s1["ml_kinds"],
        "entities": s1["entities"],
        "situations": [
            situation_dict(r, claim_types.get(r.situation_id, "")) for r in s1["situations"]
        ],
        "stats": s1["stats"],
        **s2,
    }


FREEZE_ATTESTATION_NAME = "freeze_attestation.json"


def deviations() -> list[str]:
    """本番との差 (manifest に残す)。"""
    base = [
        "assign gate not applied",
        "seed centroid evidence read from current ledger",
        "kinds/axes from stored values only",
    ]
    if not dc.PROFILE.as_of_strict:
        return base
    return [
        *base,
        "situation title = latest revision claim at window start (rejudge 2.3)",
        "dormancy from last evidence before window start (rejudge 2.3)",
        "already-assigned exclusion not applied",
        "reopen / later evidence edits not reconstructed",
    ]


def pir_context() -> list[dict[str, str]]:
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        return build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 - 本番と同じく PIR 不在でも detect は動く
        return []


def plan(repo: Any, store: Any) -> tuple[list[tuple[dc.Bin, dict[str, Any]]], dict[str, int]]:
    """全 32 区間の段 1 と適格の判定。除外の件数を数える。"""
    excluded = {
        "candidates_lt5": 0,
        "no_active_situations": 0,
        "as_of_title_unrecoverable": 0,
        "missing_kinds": 0,
    }
    bins: list[tuple[dc.Bin, dict[str, Any]]] = []
    for b in dc.build_bins():
        s1 = stage1(repo, store, b)
        reason = eligibility(s1["stats"])
        if reason:
            excluded[reason] += 1
            continue
        bins.append((b, s1))
    return bins, excluded


def run(args: argparse.Namespace) -> int:
    dc.verify_detect_model()
    repo, store = open_repo()
    eligible, excluded = plan(repo, store)
    picked = dc.sample_windows([b for b, _ in eligible])
    if args.max_windows:
        picked = sorted(picked, key=lambda x: x.start)[: args.max_windows]  # 試験用
    by_key = {b.key: s1 for b, s1 in eligible}
    print(f"[detect-snapshot] 区間 {len(dc.build_bins())} / 適格 {len(eligible)} / 除外 {excluded}")
    for b in sorted(picked, key=lambda x: x.start):
        st = by_key[b.key]["stats"]
        print(
            f"  {b.key} プール {st['pool']} 候補 {st['candidates']} 情勢 {st['situations_as_of']}"
            f" (active {st['active_as_of']}) 種別欠け {st['missing_kinds']}/{st['detect_input']}"
            f" 軸の付け直し {st['axes_reannotated']} 題名の戻し {st.get('as_of_retitled', '-')}"
        )
    print(f"  抽出 {len(picked)} 窓 (乱数種 {dc.SEED}、同じ日は最大 {dc.MAX_PER_DAY})")
    if args.dry_run:
        print("[detect-snapshot] dry-run: 書き出しなし")
        return 0
    cert = detect_attest.check_attestation(Path(args.attestation))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 凍結の時点の証明書を残す (実行の直前に取り直すため、prereg §9)
    (out_dir / FREEZE_ATTESTATION_NAME).write_text(
        json.dumps(cert, indent=2, sort_keys=True), encoding="utf-8"
    )
    windows = []
    unreadable = 0
    for i, b in enumerate(sorted(picked, key=lambda x: x.start)):
        s1 = by_key[b.key]
        s2 = asyncio.run(stage2(repo, store, b, s1))
        if s2["missing_articles"]:
            unreadable += 1
            print(f"  {b.key}: 記事が DB から読めない {len(s2['missing_articles'])} 件 -> 除外 (3)")
            continue
        windows.append(build_window(b, i, s1, s2))
    payload = {
        "meta": {
            "prereg_git_sha": cert["prereg_git_sha"],
            "seed": dc.SEED,
            "detect_model_sha256": dc.DETECT_MODEL_SHA256,
            "frozen_at": datetime.now(UTC).isoformat(),
            "excluded": {**excluded, "unreadable": unreadable},
            "profile": dc.PROFILE.name,
            "range": [dc.RANGE_START.isoformat(), dc.RANGE_END.isoformat()],
            "deviations": deviations(),
        },
        "pir_context": pir_context(),
        "windows": windows,
    }
    hashes = dc.write_json_with_sha(Path(args.out_dir), dc.INPUTS_NAME, dc.INPUTS_SHA_NAME, payload)
    print(
        f"[detect-snapshot] {len(windows)} 窓 -> {args.out_dir}"
        f" sha256={hashes['inputs_sha256'][:12]}"
    )
    if len(windows) < 18:
        print("[detect-snapshot] 窓が 18 未満: 判定不能の条件 (§6)", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out-dir", default=str(dc.OUT_DIR))
    ap.add_argument("--attestation", default=str(dc.OUT_DIR / detect_attest.ATTESTATION_NAME))
    ap.add_argument(
        "--attest", action="store_true", help="ホストで prereg の証明書を書く (git のみ)"
    )
    ap.add_argument("--max-windows", type=int, default=0, help="試験用: 先頭 N 窓だけ凍結")
    ap.add_argument(
        "--dry-run", action="store_true", help="窓の計画だけ (書き出さない・埋込を使わない)"
    )
    args = ap.parse_args(argv)
    if args.attest:
        detect_attest.attest(Path.cwd(), Path(args.attestation))
        print(f"[detect-snapshot] 証明書 -> {args.attestation}")
        return 0
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
