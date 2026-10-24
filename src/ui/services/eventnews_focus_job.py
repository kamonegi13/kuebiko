"""事象の焦点の記録 (2026-10-24 F0、docs/eventnews_focus_redesign.md §2・§7)。

毎時の群化と統合の段の後に、複数記事の生きた事象の焦点を計算して ``event_item_focus`` に記録する。
**記録だけで、群化・統合の判断には使わない** (本番の挙動は変えない)。あわせて、統合の判断記録に
焦点の照合 (support / weak / conflict / lineage) と両側の焦点の写しを残す (F1 の準備)。

- 停止: ``EVENTNEWS_FOCUS_RECORD=0`` (記録が止まるだけ)。
- 焦点は ``compute_focus`` の純粋関数で作り直せるキャッシュ。事象が動いた (``updated_at`` が
  焦点より新しい) ものだけを作り直し、変わった名前は ``event_item_focus_log`` に残す。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from src.eventnews.event_kind import KIND_LABELS_JA
from src.eventnews.identity.focus import (
    CLASS_CONFLICT,
    CLASS_LINEAGE,
    DEFAULT_RULE,
    Focus,
    FocusArticle,
    compute_focus,
    focus_from_json,
    focus_to_dict,
    focus_to_json,
    match_focus,
    verdict_to_dict,
)
from src.eventnews.merge import LinkVerdict
from src.eventnews.models import ENTITY_FREQ_WINDOW_HOURS, MemberArticle
from src.eventnews.org_identity import build_alias_map
from src.logging_config import get_logger
from src.storage.repo_event_focus import StoredFocus
from src.storage.repo_merge_decisions import MergeDecisionRow
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

FOCUS_FLAG = "EVENTNEWS_FOCUS_RECORD"
#: F2: 統合の判断で焦点の照合が conflict の組を結ばない (on / shadow / off、既定 on)
GATE_FLAG = "EVENTNEWS_MERGE_FOCUS_GATE"
GATE_ON = "on"
GATE_SHADOW = "shadow"
GATE_OFF = "off"
#: 1 回に作り直す事象の上限 (初回の遡及は数回に分けて進む)
MAX_ITEMS_PER_RUN = 3000
#: 読み込みの 1 まとまり (記事を引く SQL のプレースホルダ数を抑える)
BATCH_ITEMS = 400
#: 焦点の変化の記録の保持日数
LOG_RETENTION_DAYS = 180

CAUSE_SEED = "seed"
CAUSE_JOIN = "join"
CAUSE_MERGE = "merge"
CAUSE_UNDO = "undo"
CAUSE_REBUILD = "rebuild"

MemberLoader = Callable[
    [Any, list[str], Mapping[tuple[str, str], int]], Mapping[str, MemberArticle]
]


def focus_record_enabled() -> bool:
    return os.environ.get(FOCUS_FLAG, "1") != "0"


def focus_gate_mode() -> str:
    """``EVENTNEWS_MERGE_FOCUS_GATE`` の解釈。未設定 = on。読めない値は shadow (止めない側)。"""
    raw = os.environ.get(GATE_FLAG, GATE_ON).strip().lower()
    if raw in ("on", "1", "true"):
        return GATE_ON
    if raw in ("off", "0", "false"):
        return GATE_OFF
    return GATE_SHADOW


def load_org_alias(repo: RunHistoryRepository) -> dict[str, str]:
    """被害組織の別名判定 (shadow の「同一」) の写像。conflict を消す方向だけに使う。

    読めなくても照合は動く (別名なし = conflict が少し多めに出る側) ので、失敗は警告で続行する。
    """
    try:
        return dict(build_alias_map(repo.list_org_identity_same_pairs()).mapping)
    except Exception as e:  # noqa: BLE001 — 補助。別名が無くても照合は成り立つ
        _log.warning("eventnews_focus_org_alias_failed", error=str(e)[:160])
        return {}


def build_link_veto(
    members_of: Callable[[str], Sequence[MemberArticle]],
    org_alias: Mapping[str, str],
) -> Callable[[str, str], LinkVerdict | None]:
    """統合の判断に渡す照合 (F2)。conflict / lineage だけ ``LinkVerdict`` を返し、他は None。

    事象ごとの焦点は 1 回の計画の中で使い回す。照合に失敗した組は止めない (None) —
    統合を止める根拠にできるのは、照合が成功して conflict と出たときだけ。
    """
    cache: dict[str, Focus] = {}

    def focus_of(item_id: str) -> Focus:
        if item_id not in cache:
            cache[item_id] = focus_of_members(members_of(item_id))
        return cache[item_id]

    def veto(item_a: str, item_b: str) -> LinkVerdict | None:
        try:
            verdict = match_focus(focus_of(item_a), focus_of(item_b), org_alias=org_alias)
        except Exception as e:  # noqa: BLE001 — 照合の失敗で統合を止めない
            _log.warning("eventnews_focus_veto_failed", a=item_a, b=item_b, error=str(e)[:160])
            return None
        if verdict.klass == CLASS_CONFLICT:
            return LinkVerdict(CLASS_CONFLICT, ",".join(verdict.conflicts))
        if verdict.klass == CLASS_LINEAGE:
            return LinkVerdict(CLASS_LINEAGE, verdict.reason)
        return None

    return veto


def to_focus_article(member: MemberArticle) -> FocusArticle:
    return FocusArticle(
        article_id=member.article_id,
        names=member.entities,
        kind=member.kind,
        is_roundup=member.is_roundup,
    )


def focus_of_members(members: Sequence[MemberArticle]) -> Focus:
    """参加順に並んだメンバーから焦点を作る。"""
    return compute_focus([to_focus_article(m) for m in members], DEFAULT_RULE)


def order_members(
    rows: Sequence[tuple[str, str]], by_id: Mapping[str, MemberArticle]
) -> list[MemberArticle]:
    """(article_id, joined_at) を参加順に並べて MemberArticle にする。

    統合で足された行は同時刻なので、同時刻の中は事象時刻 (``anchor_ts``)、最後に article_id で決める
    (再現を決定論にする)。記事が読めなかった行は飛ばす。
    """
    pairs = [(joined, by_id[aid]) for aid, joined in rows if aid in by_id]
    pairs.sort(key=lambda p: (p[0], p[1].anchor_ts, p[1].article_id))
    return [m for _, m in pairs]


# ---------- 統合の判断記録への焦点 ----------


def focus_fields_for_decision(
    target_members: Sequence[MemberArticle],
    absorbed_members: Sequence[MemberArticle],
    org_alias: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """統合前の両側の焦点を照合し、(focus_class, focus_json) を返す。"""
    target = focus_of_members(target_members)
    absorbed = focus_of_members(absorbed_members)
    verdict = match_focus(target, absorbed, org_alias=org_alias)
    payload = {
        "rule_version": DEFAULT_RULE.version,
        "target": focus_to_dict(target),
        "absorbed": focus_to_dict(absorbed),
        "verdict": verdict_to_dict(verdict),
    }
    return verdict.klass, json.dumps(payload, ensure_ascii=False, sort_keys=True)


def enrich_decision_rows(
    rows: Sequence[MergeDecisionRow],
    members_of: Callable[[str], Sequence[MemberArticle]],
    org_alias: Mapping[str, str] | None = None,
) -> list[MergeDecisionRow]:
    """判断記録の行に焦点の照合を足す (flag off・失敗は元の行のまま)。

    ``org_alias`` は統合の判断 (``build_link_veto``) と同じ別名の写像。渡さないと記録の照合が
    判断より conflict を多く出し、止めた件数の記録と食い違う。

    ⚠ 焦点の失敗で統合の記録そのものを落とさない。落ちた行は focus_class が空 (= 未計算)。
    """
    if not focus_record_enabled():
        return list(rows)
    out: list[MergeDecisionRow] = []
    for row in rows:
        try:
            klass, text = focus_fields_for_decision(
                members_of(row.target_id), members_of(row.absorbed_id), org_alias
            )
        except Exception as e:  # noqa: BLE001 — 記録は補助。統合の記録を落とさない
            _log.warning(
                "eventnews_focus_decision_failed", absorbed=row.absorbed_id, error=str(e)[:160]
            )
            out.append(row)
            continue
        out.append(replace(row, focus_class=klass, focus_json=text))
    return out


# ---------- 焦点の記録 ----------


@dataclass(frozen=True)
class FocusRecordStats:
    candidates: int = 0
    recorded: int = 0
    changed: int = 0
    empty: int = 0
    dropped_merged: int = 0
    skipped: str = ""

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "candidates": self.candidates,
            "recorded": self.recorded,
            "changed": self.changed,
            "empty": self.empty,
            "dropped_merged": self.dropped_merged,
        }
        if self.skipped:
            out["skipped"] = self.skipped
        return out


def _name_keys(focus_json: str) -> set[tuple[str, str]]:
    if not focus_json:
        return set()
    return set(focus_from_json(focus_json).name_set)


def _stored_from(item_id: str, focus: Focus) -> StoredFocus:
    return StoredFocus(
        item_id=item_id,
        rule_version=DEFAULT_RULE.version,
        anchor_article=focus.anchor_article,
        names_json=focus_to_json(focus),
        kind=focus.kind,
        supporters=focus.supporters,
        outsiders=focus.outsiders,
        updated_at=None,
    )


def _cause_for(
    repo: RunHistoryRepository, item_id: str, prev: StoredFocus | None
) -> tuple[str, int | None]:
    if prev is None:
        return CAUSE_SEED, None
    if prev.rule_version != DEFAULT_RULE.version:
        return CAUSE_REBUILD, None
    since = prev.updated_at
    if since is not None:
        decision_id, undone = repo.latest_merge_decision_id(item_id, since=since)
        if decision_id is not None:
            return (CAUSE_UNDO if undone else CAUSE_MERGE), decision_id
    return CAUSE_JOIN, None


def _record_one(
    repo: RunHistoryRepository,
    item_id: str,
    focus: Focus,
    prev: StoredFocus | None,
    now: datetime,
) -> bool:
    """1 事象の焦点を書く。名前が変わった (または初回の) とき True (ログも書く)。"""
    stored = _stored_from(item_id, focus)
    repo.upsert_item_focus(stored, updated_at=now)
    old_names = _name_keys(prev.names_json) if prev else set()
    new_names = set(focus.name_set)
    added, removed = sorted(new_names - old_names), sorted(old_names - new_names)
    if prev is not None and not added and not removed:
        return False
    cause, decision_id = _cause_for(repo, item_id, prev)
    repo.append_focus_log(
        item_id=item_id,
        changed_at=now,
        cause=cause,
        decision_id=decision_id,
        added_json=json.dumps([list(n) for n in added], ensure_ascii=False),
        removed_json=json.dumps([list(n) for n in removed], ensure_ascii=False),
    )
    return True


def record_focuses(
    repo: RunHistoryRepository,
    *,
    now: datetime | None = None,
    limit: int = MAX_ITEMS_PER_RUN,
    load_members: MemberLoader | None = None,
    counts: Mapping[tuple[str, str], int] | None = None,
) -> FocusRecordStats:
    """動いた (または未記録の) 複数記事の生きた事象の焦点を作り直して記録する。

    flag off は何もしない。吸収された事象の焦点は消す。失敗は呼び手 (毎時の段) で握りつぶさず
    ログに残す方針なので、ここでは例外を伝える。
    """
    if not focus_record_enabled():
        return FocusRecordStats(skipped="flag_off")
    when = now or datetime.now(UTC)
    dropped = repo.delete_focus_of_merged()
    repo.purge_focus_log(before=when - timedelta(days=LOG_RETENTION_DAYS))
    item_ids = repo.list_focus_candidates(rule_version=DEFAULT_RULE.version, limit=limit)
    if not item_ids:
        return FocusRecordStats(dropped_merged=dropped)
    if load_members is None:
        from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members

        load_members = _load_members
        if counts is None:
            counts = _entity_counts(repo, when - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS))
    recorded = changed = empty = 0
    for i in range(0, len(item_ids), BATCH_ITEMS):
        batch = item_ids[i : i + BATCH_ITEMS]
        rows = repo.list_members_in_join_order(batch)
        article_ids = sorted({aid for rs in rows.values() for aid, _ in rs})
        by_id = load_members(repo, article_ids, counts or {})
        prevs = repo.get_item_focuses(batch)
        for item_id in batch:
            focus = focus_of_members(order_members(rows.get(item_id, ()), by_id))
            changed += int(_record_one(repo, item_id, focus, prevs.get(item_id), when))
            empty += int(focus.is_empty)
            recorded += 1
    stats = FocusRecordStats(len(item_ids), recorded, changed, empty, dropped)
    _log.info("eventnews_focus_recorded", **stats.as_dict())
    return stats


def run_focus_record(repo: RunHistoryRepository | None = None) -> dict[str, object]:
    """毎時の段 (群化・統合) の末尾から呼ぶ入口。失敗は大きく残して段を止めない。"""
    if not focus_record_enabled():
        return {"skipped": "flag_off"}
    try:
        return record_focuses(repo or RunHistoryRepository()).as_dict()
    except Exception as e:  # noqa: BLE001 — 記録は補助。群化・統合の成否を巻き込まない
        _log.error("eventnews_focus_record_failed", error=str(e)[:200])
        return {"error": str(e)[:200]}


# ---------- 検算 (再生と DB の値の一致) ----------


def verify_recorded_focuses(
    repo: RunHistoryRepository,
    item_ids: Sequence[str],
    *,
    load_members: MemberLoader | None = None,
    counts: Mapping[tuple[str, str], int] | None = None,
) -> list[str]:
    """記録済みの焦点を再生して突き合わせ、食い違う事象の id を返す (F0 の日次の検算の本体)。"""
    if load_members is None:
        from src.ui.services.eventnews_hourly_job import _entity_counts, _load_members

        load_members = _load_members
        if counts is None:
            counts = _entity_counts(
                repo, datetime.now(UTC) - timedelta(hours=ENTITY_FREQ_WINDOW_HOURS)
            )
    rows = repo.list_members_in_join_order(item_ids)
    by_id = load_members(repo, sorted({a for rs in rows.values() for a, _ in rs}), counts or {})
    stored = repo.get_item_focuses(item_ids)
    bad: list[str] = []
    for item_id in item_ids:
        row = stored.get(item_id)
        if row is None:
            continue
        replay = focus_of_members(order_members(rows.get(item_id, ()), by_id))
        if focus_to_json(replay) != row.names_json:
            bad.append(item_id)
    return bad


# ---------- 詳細 API の消費者 ----------


def focus_payload(repo: RunHistoryRepository, item_id: str) -> dict[str, object] | None:
    """事象の詳細 API に載せる焦点。記録が無ければ None (単独記事の事象・記録前)。"""
    row = repo.get_item_focuses([item_id]).get(item_id)
    if row is None:
        return None
    focus = focus_from_json(row.names_json)
    return {
        **focus_to_dict(focus),
        "kind_label": KIND_LABELS_JA.get(focus.kind, ""),
        "rule_version": row.rule_version,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
