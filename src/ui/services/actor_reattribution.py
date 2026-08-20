"""承認時再帰属 (アクター辞書 Phase1 → D3 三パス化 2026-07-26)。

新アクター承認・alias 追加の時点で、過去記事へ主題/言及を帰属し直す。判定入力の
永続性に応じて 3 パスに分かれる (docs/actor_observed_history_design.md):

- **パス 1 (title 全期間)**: title は永久メタ — 決定論の title 層判定を全史に適用。
  主題遡及の無制限化の本体。
- **パス 2 (LLM 出力全期間)**: D1 で永続化した summarizer 生出力 (llm_primary_actor_raw)
  を現在の辞書で再解決。confidence high/medium のみ。本文現存なら言及 word-boundary
  検証、**本文 purge 済みなら raw と名前の完全一致時のみ付与** (LLM が本文から名前を
  転記した事実自体を言及の証拠とみなす保守則 — fuzzy 解決だけでは付与しない)。
  ※生出力の保存開始 (2026-07-26) 以前の記事にはこのパスは効かない (真に失われている)。
- **パス 3 (body 窓)**: 本文現存 (≤90 日 + D4 免除域) の記事に言及 entity を付与。

原則: 既存の subject 判定は上書きしない (id の追加のみ)。失敗しても承認フローは
殺さない (呼び出し側が warning ログで続行)。影響月は再蒸留する (窓内再蒸留は正当)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry, load_actor_aliases
from src.cti.actor_observed_history import month_label
from src.cti.subject_actor import (
    _LLM_USABLE_CONFIDENCE,
    _relevant_actors,
    _suppress_sponsor_orgs,
    determine_subject_actors,
    resolve_actor_by_name,
)
from src.logging_config import get_logger
from src.storage.row_mappers import _from_iso
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

# body 現存を前提とするパス 3 の窓 (retention 90 日 + 余裕なし。D4 免除域の古い記事は
# search_recent_articles_by_names の窓外だが、それらは既に言及 entity を持つ root 記事)
_BODY_WINDOW_DAYS = 90


def _split_ids(raw: object) -> list[str]:
    # split_subject_ids と挙動差 (list で順序を保持 — 呼出側が ",".join([*existing, new_id])
    # で追記順を維持する必要がある + strip() の空白耐性) のため据置 (2026-08-21 監査)。
    return [s.strip() for s in str(raw or "").split(",") if s.strip()]


def _slug_to_name(raw: str) -> str:
    return raw.replace("-", " ").replace("_", " ").strip()


class _Reattributor:
    """1 アクター分の再帰属の状態 (パス間で共有する集計・重複排除)。"""

    def __init__(
        self, repo: RunHistoryRepository, actor: ActorAlias, registry: ActorAliasRegistry
    ) -> None:
        self.repo = repo
        self.actor = actor
        self.registry = registry
        self.subject_done: set[str] = set()  # subject 追加済み/確認済みの article_id
        self.touched_months: set[str] = set()
        self.stats = {
            "title_candidates": 0,
            "llm_candidates": 0,
            "body_candidates": 0,
            "subjects_added": 0,
            "mentions_added": 0,
            "months_redistilled": 0,
        }

    def _touch_month(self, row: Mapping[str, Any]) -> None:
        created = _from_iso(row.get("created_at"))
        if created is None:
            return
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        self.touched_months.add(month_label(created))

    def try_add_subject(
        self, row: Mapping[str, Any], *, source: str, confidence: str | None
    ) -> bool:
        """actor を記事の subject に追加 (既存判定は保持、id 追加のみ)。"""
        aid = str(row["article_id"])
        if aid in self.subject_done:
            return False
        existing = _split_ids(row.get("subject_actor_ids"))
        if self.actor.id in existing:
            self.subject_done.add(aid)
            return False
        if existing:
            self.repo.update_subject_actor_fields(
                aid,
                ids_csv=",".join([*existing, self.actor.id]),
                source=str(row.get("subject_actor_source") or source),
                confidence=(
                    str(row["subject_actor_confidence"])
                    if row.get("subject_actor_confidence")
                    else confidence
                ),
            )
        else:
            self.repo.update_subject_actor_fields(
                aid, ids_csv=self.actor.id, source=source, confidence=confidence
            )
        self.subject_done.add(aid)
        self.stats["subjects_added"] += 1
        self._touch_month(row)
        return True

    # ---- パス 1: title 全期間 (決定論。title は永久メタ = retention 非依存) ----
    def pass_title_all_time(self) -> None:
        seen: set[str] = set()
        for row in self.repo.search_titles_by_names(list(self.actor.all_names)):
            aid = str(row["article_id"])
            if aid in seen:
                continue
            seen.add(aid)
            self.stats["title_candidates"] += 1
            subj = determine_subject_actors(
                titles=(str(row.get("title") or ""),),
                detected_actor_ids=(),
                llm_primary_actor_id="",
                llm_confidence="",
                category=str(row.get("category") or "") or None,
                registry=self.registry,
            )
            if self.actor.id in subj.ids:
                self.try_add_subject(row, source="title", confidence=None)

    # ---- パス 2: LLM 出力全期間 (D1 で保存した生入力の再解決) ----
    def pass_llm_all_time(self) -> None:
        names_folded = {n.casefold() for n in self.actor.all_names}
        seen: set[str] = set()
        for row in self.repo.search_llm_primary_by_names(list(self.actor.all_names)):
            aid = str(row["article_id"])
            if aid in seen or aid in self.subject_done:
                continue
            seen.add(aid)
            self.stats["llm_candidates"] += 1
            conf = str(row.get("llm_primary_confidence") or "")
            if conf not in _LLM_USABLE_CONFIDENCE:
                continue
            raw = str(row.get("llm_primary_actor_raw") or "")
            resolved = resolve_actor_by_name(_slug_to_name(raw), self.registry)
            if resolved is None or self.registry.resolve_actor_id(resolved.id) != self.actor.id:
                continue
            body = str(row.get("body") or "")
            if body:
                # 本文現存: 取込時と同じ word-boundary 言及検証 (二重ゲートの再現)
                matched = self.registry.find_all(f"{row.get('title')}\n{body}")
                filtered = _suppress_sponsor_orgs(
                    _relevant_actors(matched, str(row.get("category") or "") or None)
                )
                if self.actor.id not in {a.id for a in filtered}:
                    continue
            else:
                # 本文 purge 済み: raw が名前の完全一致 (casefold) のときのみ付与 —
                # LLM が本文から名前を転記した事実を言及の証拠とみなす (保守則)
                if _slug_to_name(raw).casefold() not in names_folded:
                    continue
            self.try_add_subject(row, source="llm", confidence=conf)

    # ---- パス 3: body 窓 (言及 entity — News facet / 共起の網羅) ----
    def pass_body_window(self, now: datetime) -> None:
        since = now - timedelta(days=_BODY_WINDOW_DAYS)
        seen: set[str] = set()
        for row in self.repo.search_recent_articles_by_names(list(self.actor.all_names), since):
            aid = str(row["article_id"])
            if aid in seen:
                continue
            seen.add(aid)
            self.stats["body_candidates"] += 1
            title = str(row["title"] or "")
            body = str(row["body"] or "")
            category = row["category"]
            matched = self.registry.find_all(f"{title}\n{body}")
            filtered = _suppress_sponsor_orgs(
                _relevant_actors(matched, str(category) if category else None)
            )
            if self.actor.id not in {a.id for a in filtered}:
                continue
            self.repo.add_article_entities(aid, [("actor", self.actor.id)])
            self.stats["mentions_added"] += 1


def reattribute_actor(
    repo: RunHistoryRepository,
    actor_id: str,
    *,
    registry: ActorAliasRegistry | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """actor_id の名称群で過去記事を再帰属し、影響月を再蒸留する (D3 三パス)。"""
    from src.ui.services.actor_history_distill import distill_and_store

    registry = registry or load_actor_aliases()
    actor = registry.by_id(actor_id)
    if actor is None or actor.is_merged:
        return {
            "title_candidates": 0,
            "llm_candidates": 0,
            "body_candidates": 0,
            "subjects_added": 0,
            "mentions_added": 0,
            "months_redistilled": 0,
        }
    worker = _Reattributor(repo, actor, registry)
    worker.pass_title_all_time()
    worker.pass_llm_all_time()
    worker.pass_body_window(now or datetime.now(UTC))
    if worker.touched_months:
        distill_and_store(repo, sorted(worker.touched_months))
        worker.stats["months_redistilled"] = len(worker.touched_months)
    _log.info("actor_reattribution_done", actor_id=actor_id, **worker.stats)
    return worker.stats
