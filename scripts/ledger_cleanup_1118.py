#!/usr/bin/env python3
"""台帳の既存の汚れた根拠の掃除 (2026-11-18、docs/ledger_upstream_fixes_2026_11_17.md §11)。

対象 = 当て直し (scripts/ledger_assign_fixes_replay.py、round3) で SEED_STRICT / ASSIGN_STRONG_ANCHOR
が「外れる / 落ちる」とした (situation_id, article_id)。Opus 盲検で無関係 100% / 92.5%。

やること:
1. `situation_evidence.weak_at` に印 (行は消さない = 評価から外れる既存の仕組み)。
2. 状況の anchors から、**外した記事だけが持つ** entity のキーを取り除く (残る根拠・判定文に在るキーは残す)。
3. 変更前の値を退避表 `_backup_ledger_cleanup_20261118` / `_backup_ledger_anchors_20261118` へ。

戻し方:
  UPDATE situation_evidence se SET weak_at = b.prev_weak_at FROM _backup_ledger_cleanup_20261118 b
   WHERE se.situation_id = b.situation_id AND se.article_id = b.article_id;
  UPDATE situations s SET anchors = b.anchors FROM _backup_ledger_anchors_20261118 b
   WHERE s.situation_id = b.situation_id;

使い方 (ホストで。DB は docker exec の psql):
  python3 scripts/ledger_cleanup_1118.py            # dry-run (件数と除くキーの一覧のみ)
  python3 scripts/ledger_cleanup_1118.py --apply    # 実行 (1 トランザクション)
"""

from __future__ import annotations

import argparse
import sys as _sys
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_sys.path.insert(0, str(ROOT))
SOURCES = (
    ("seed_strict", ROOT / "data/eval/ledger_fix_1117/round3/seed_strict.jsonl"),
    ("assign_strong_anchor", ROOT / "data/eval/ledger_fix_1117/round3/assign_strong_anchor.jsonl"),
)
BACKUP_EVIDENCE = "_backup_ledger_cleanup_20261118"
BACKUP_ANCHORS = "_backup_ledger_anchors_20261118"
PSQL = ["docker", "exec", "-i", "postgres", "psql", "-U", "kuebiko", "-d", "kuebiko", "-tA"]


def psql(sql: str) -> str:
    out = subprocess.run([*PSQL, "-v", "ON_ERROR_STOP=1"], input=sql, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr.strip()}")
    return out.stdout


def q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def load_pairs() -> dict[tuple[str, str], str]:
    pairs: dict[tuple[str, str], str] = {}
    for reason, path in SOURCES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                pairs.setdefault((r["situation_id"], r["article_id"]), reason)
    return pairs


def fetch_json(sql: str) -> list[dict[str, object]]:
    raw = psql(f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t;").strip()
    return json.loads(raw or "[]")


def plan(pairs: dict[tuple[str, str], str]) -> tuple[list[dict[str, object]], dict[str, set[str]]]:
    values = ",".join(f"({q(s)},{q(a)})" for s, a in pairs)
    pair_cte = f"WITH p(situation_id, article_id) AS (VALUES {values})"
    rows = fetch_json(
        f"{pair_cte} SELECT se.situation_id, se.article_id, se.weak_at FROM situation_evidence se"
        " JOIN p USING (situation_id, article_id)"
    )
    target = {(str(r["situation_id"]), str(r["article_id"])) for r in rows if r["weak_at"] is None}
    sids = sorted({s for s, _ in target})
    if not sids:
        return [], {}
    sid_list = ",".join(q(s) for s in sids)
    anchors = {
        str(r["situation_id"]): set(json.loads(str(r["anchors"] or "[]")))
        for r in fetch_json(f"SELECT situation_id, anchors FROM situations WHERE situation_id IN ({sid_list})")
    }
    ev = fetch_json(
        "SELECT se.situation_id, se.article_id, se.weak_at, ae.entity_type, ae.value"
        " FROM situation_evidence se JOIN article_entities ae ON ae.article_id = se.article_id"
        f" WHERE se.situation_id IN ({sid_list})"
    )
    removed_keys: dict[str, set[str]] = defaultdict(set)
    kept_keys: dict[str, set[str]] = defaultdict(set)
    for r in ev:
        sid, aid = str(r["situation_id"]), str(r["article_id"])
        key = f"{r['entity_type']}:{r['value']}"
        if (sid, aid) in target:
            removed_keys[sid].add(key)
        elif r["weak_at"] is None:
            kept_keys[sid].add(key)
    texts = {
        str(r["situation_id"]): f"{r['title'] or ''} {r['claim'] or ''}"
        for r in fetch_json(
            "SELECT s.situation_id, s.title, (SELECT claim FROM situation_revisions r"
            " WHERE r.situation_id = s.situation_id ORDER BY rev DESC LIMIT 1) AS claim"
            f" FROM situations s WHERE s.situation_id IN ({sid_list})"
        )
    }
    prune: dict[str, set[str]] = {}
    for sid in sids:
        keys = {
            k
            for k in (anchors.get(sid, set()) & removed_keys[sid]) - kept_keys[sid]
            if not _named_in(k, texts.get(sid, ""))
        }
        if keys:
            prune[sid] = keys
    return [{"situation_id": s, "article_id": a, "reason": pairs[(s, a)]} for s, a in sorted(target)], prune


def _named_in(key: str, text: str) -> bool:
    """anchor のキーが状況の題・最新の判定文に出るなら残す (国は gazetteer、他は名前の部分一致)。"""
    kind, _, value = key.partition(":")
    if kind == "involved_country":
        from src.cti.nation_gazetteer import nations_in_text

        return value.upper() in {n.upper() for n in nations_in_text(text)}
    norm = value.replace("株式会社", "").replace("(株)", "").strip()
    return bool(norm) and norm.casefold() in text.casefold()


def apply(target: list[dict[str, object]], prune: dict[str, set[str]]) -> None:
    now = datetime.now(UTC).isoformat()
    values = ",".join(f"({q(str(t['situation_id']))},{q(str(t['article_id']))},{q(str(t['reason']))})" for t in target)
    stmts = [
        "BEGIN;",
        f"CREATE TABLE {BACKUP_EVIDENCE} (situation_id text, article_id text, prev_weak_at text, reason text, cleaned_at text);",
        f"CREATE TABLE {BACKUP_ANCHORS} (situation_id text PRIMARY KEY, anchors text, removed text);",
        f"INSERT INTO {BACKUP_EVIDENCE} SELECT se.situation_id, se.article_id, se.weak_at, p.reason, {q(now)}"
        f" FROM situation_evidence se JOIN (VALUES {values}) AS p(situation_id, article_id, reason)"
        " USING (situation_id, article_id);",
        f"UPDATE situation_evidence se SET weak_at = {q(now)} FROM {BACKUP_EVIDENCE} b"
        " WHERE se.situation_id = b.situation_id AND se.article_id = b.article_id AND se.weak_at IS NULL;",
    ]
    for sid, keys in prune.items():
        stmts.append(
            f"INSERT INTO {BACKUP_ANCHORS} SELECT situation_id, anchors, {q(json.dumps(sorted(keys), ensure_ascii=False))}"
            f" FROM situations WHERE situation_id = {q(sid)};"
        )
        stmts.append(
            "UPDATE situations SET anchors = (SELECT coalesce(json_agg(k ORDER BY k), '[]')::text"
            f" FROM json_array_elements_text(anchors::json) k WHERE k NOT IN ({','.join(q(k) for k in keys)}))"
            f" WHERE situation_id = {q(sid)};"
        )
    stmts.append("COMMIT;")
    psql("\n".join(stmts))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    pairs = load_pairs()
    target, prune = plan(pairs)
    by_reason: dict[str, int] = defaultdict(int)
    for t in target:
        by_reason[str(t["reason"])] += 1
    print(f"候補 {len(pairs)} 組 → 印を付ける (weak_at が空の行) {len(target)} 行 {dict(by_reason)}")
    print(f"状況 {len({t['situation_id'] for t in target})} 件 / anchors からキーを除く状況 {len(prune)} 件・キー {sum(map(len, prune.values()))} 個")
    for sid, keys in list(prune.items())[:15]:
        print(f"  {sid}: {sorted(keys)[:6]}")
    if not args.apply:
        print("dry-run (--apply で実行)")
        return 0
    apply(target, prune)
    print("適用した")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
