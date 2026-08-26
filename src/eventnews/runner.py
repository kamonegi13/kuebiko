"""事象単位ニュースの実行器 (v1 shadow — スケジューラ非接続、scripts から呼ぶ)。

記事を錨時刻順に 1 件ずつ処理する逐次適用 (リプレイと本番で同一コード)。
状態は in-memory registry に持ち、書込は repo へ委譲する。

生成の対象は **メンバー 2 件以上のアイテムのみ** (singleton の「ニュース」は
記事要約そのものなので LLM を呼ばない — 表示層が要約を出す)。§7 の
「version 0 は無条件再生成」もメンバー 2 件以上に限る。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import numpy as np

from src.eventnews import coverage, grouping, identifier_gate, quantities, state, verbatim
from src.eventnews import generator as gen
from src.eventnews.models import (
    UPDATE_DRIVER_TYPES,
    Assignment,
    EventNewsDraft,
    GateResult,
    ItemState,
    MemberArticle,
    SourceBreakdown,
)
from src.logging_config import get_logger
from src.storage.repo_eventnews import EventNewsMixin
from src.tools.llm_client import LLMClient, LLMError

_log = get_logger(__name__)

_IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}
# v2 (2026-08-24): 「[N] のみが報じる」と「text に [N] を書かない」が矛盾していた
# v3 (2026-08-24): 掲載場所/URL を事実として書かせない + 裏取り状態を単独行にしない
# v4: 本文を節 (what/scope/how/response/context/action) に分ける (2026-08-25)
_PROMPT_VERSION = "eventnews-v4"
# これ以上の事実行があるのに節が 1 種類なら、割り当てが効いていないとみなす
_SECTION_SPREAD_MIN_FACTS = 6

# 単独報 (本文を持つメンバーが 1 件) を生成する条件。
# 公開面は high しか出さないので、母集団を high に揃える — 全件生成すると
# 205 件/日 (実測 2026-08-26) になり、複数報道 18.6 件/日 の 11 倍になる。
_SOLO_MIN_IMPORTANCE = "high"
# タイトルだけで生成させると内容を創作する (2026-08-23 実測)。抜粋しか無い記事も
# 同じなので、本文の長さで足切りする。
_SOLO_MIN_BODY_CHARS = 1500


@dataclass
class _LiveItem:
    """処理中のアイテムの in-memory 状態 (書込済みと同期)。"""

    snapshot: ItemState
    members: list[MemberArticle] = field(default_factory=list)
    breakdown: SourceBreakdown | None = None


@dataclass(frozen=True)
class ProcessStats:
    created: int
    updated: int
    reinforced: int
    generated: int
    generation_failures: int
    generation_skipped: int
    dropped_lines: int
    repaired_ids: int
    substituted_ids: int
    # updated を駆動した理由の内訳 (E4' の診断用 — 判定器が恒真化していないかを見る)
    reason_counts: tuple[tuple[str, int], ...] = ()
    # E4' の判別力測定: 既に 2 媒体以上あるアイテムへの合流 (= 構造的に必然の
    # first_corroboration を除いた母集団) とそのうちの reinforced 数
    later_joins: int = 0
    later_reinforced: int = 0


def _item_id_for(article_id: str) -> str:
    return "ev-" + hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:16]


def _driver_entities(members: list[MemberArticle]) -> dict[str, frozenset[str]]:
    out: dict[str, set[str]] = {t: set() for t in UPDATE_DRIVER_TYPES}
    for m in members:
        for etype, value in m.entities:
            if etype in out:
                out[etype].add(value)
    return {k: frozenset(v) for k, v in out.items()}


def _max_importance(a: str, b: str) -> str:
    return a if _IMPORTANCE_RANK.get(a, 0) >= _IMPORTANCE_RANK.get(b, 0) else b


def _allowed_identifiers_text(members: list[MemberArticle]) -> str:
    """プロンプトへ載せる識別子カタログ (実値はここにだけ現れ、本文には {In} で参照させる)。"""
    return identifier_gate.render_allowed_identifiers(members)


def _should_generate(item: _LiveItem, selected: Sequence[MemberArticle]) -> bool:
    """このアイテムを生成対象にするか。

    本文を持つメンバーが 0 件なら対象外 — タイトルだけで生成させると内容を創作する
    (2026-08-23 実測)。1 件のみ (単独報) は、公開面と同じ ``high`` かつ本文が
    ``_SOLO_MIN_BODY_CHARS`` 以上のものに限る。2 件以上は従来どおり無条件。
    """
    if not selected:
        return False
    if len(selected) >= 2:
        return True
    if item.snapshot.importance != _SOLO_MIN_IMPORTANCE:
        return False
    return len(selected[0].body or "") >= _SOLO_MIN_BODY_CHARS


_VERBATIM_HINT = """前回の出力には、原文の文をほぼそのまま写した箇所があった。**要約は原文の表現を
借りずに書く**。原文の文をつなぎ替えたり語尾だけ変えたりせず、一度読んで理解した
内容を**自分の語彙と語順で**書き直すこと (複数文をまとめる / 順序を変える /
抽象度を上げる)。ただし**数値・日付・固有名詞は原文どおり**に保つ
(これらは事実であって表現ではない)。"""


def _rewrite_hints(
    gate: GateResult,
    bodies: Mapping[int, str],
    texts: Mapping[int, str],
    item_id: str,
) -> list[str]:
    """書き直しをさせる理由。空なら書き直さない。"""
    hints: list[str] = []
    if verbatim.needs_rewrite(gate.draft.facts, bodies):
        _log.warning(
            "eventnews_verbatim_rewrite",
            item_id=item_id,
            ratio=round(verbatim.article_ratio(gate.draft.facts, bodies), 3),
            transcribed=len(verbatim.transcribed_lines(gate.draft.facts, bodies)),
        )
        hints.append(_VERBATIM_HINT)
    missing = quantities.unsupported_lines(gate.draft.facts, texts)
    if missing:
        values = sorted({v for _, vs in missing for v in vs})
        _log.warning(
            "eventnews_unsupported_values_rewrite",
            item_id=item_id,
            lines=len(missing),
            values=values[:8],
        )
        hints.append(
            "次の数値・日付は提示した記事のどこにも書かれていない: "
            + " / ".join(values)
            + "。**記事に書かれていない値を書かない**。足し合わせた合計、曜日から"
            "割り出した日付、年の補完はいずれも禁止。書けないなら、その値に触れずに述べること。"
        )
    return hints


def _drop_unsupported_lines(gate: GateResult, texts: Mapping[int, str], item_id: str) -> GateResult:
    """書き直し後も原文に無い数値・日付が残る行を落とす。"""
    missing = quantities.unsupported_lines(gate.draft.facts, texts)
    if not missing:
        return gate
    drop = {index for index, _ in missing}
    _log.warning(
        "eventnews_unsupported_values_dropped",
        item_id=item_id,
        lines=len(drop),
        values=sorted({v for _, vs in missing for v in vs})[:8],
    )
    kept = [f for i, f in enumerate(gate.draft.facts) if i not in drop]
    return replace(
        gate,
        draft=gate.draft.model_copy(update={"facts": kept}),
        dropped_lines=gate.dropped_lines + len(drop),
    )


async def _generate_version(
    repo: EventNewsMixin,
    item: _LiveItem,
    llm: LLMClient,
    now: datetime,
    new_facts_json: str,
) -> tuple[GateResult | None, str | None]:
    """生成 + 関門 + 版記録。失敗は fail-open (None を返す)。

    関門の照合対象は build_prompt が番号を振ったのと同一の選抜列 (select_members は
    決定論 + 安定 sort なので、selected を渡せば番号と照合対象が一致する)。
    """
    members = item.members
    try:
        selected, _omitted = gen.select_members(members)
        if not _should_generate(item, selected):
            _log.info(
                "eventnews_generation_skipped_no_text",
                item_id=item.snapshot.item_id,
                members=len(members),
                textual=len(selected),
                importance=item.snapshot.importance,
            )
            return None, None
        allowed = _allowed_identifiers_text(selected)
        bodies = {index: m.body for index, m in enumerate(selected, start=1)}
        draft: EventNewsDraft = await gen.generate_draft(selected, allowed, llm)
        gate: GateResult = identifier_gate.verify_draft(draft, selected)
        # 表現と数値の関門。問題があれば **1 回だけ** 理由付きで書き直させる。
        # 書き直し後も残る場合の扱いは種類で違う:
        #   - 丸写しの行が残る → 版を書かない (転記を出すより元記事の要約が正しい)
        #   - 原文に無い数値・日付が残る → **その行だけ落とす** (1 つの値のために
        #     記事全体を捨てない。識別子関門の dropped_lines と同じ粒度)
        texts = dict(enumerate(quantities.supporting_texts(selected), start=1))
        hints = _rewrite_hints(gate, bodies, texts, item.snapshot.item_id)
        if hints:
            draft = await gen.generate_draft(selected, allowed, llm, rewrite_hint="\n".join(hints))
            gate = identifier_gate.verify_draft(draft, selected)
            if verbatim.must_block(gate.draft.facts, bodies):
                _log.warning(
                    "eventnews_verbatim_blocked",
                    item_id=item.snapshot.item_id,
                    ratio=round(verbatim.article_ratio(gate.draft.facts, bodies), 3),
                    transcribed=len(verbatim.transcribed_lines(gate.draft.facts, bodies)),
                )
                return None, None
            gate = _drop_unsupported_lines(gate, texts, item.snapshot.item_id)
    except LLMError as exc:
        _log.warning("eventnews_generation_failed", item_id=item.snapshot.item_id, error=str(exc))
        return None, None
    version = item.snapshot.current_version + 1
    body_json = gate.draft.model_dump_json()
    # 節が 1 種類だけ = 割り当てが効いていない疑い。実測 (2026-08-25): JSON 例が
    # `"section": "what"` の 1 行だけだったとき、モデルが写して全文 what になった。
    # 自動で振り直さない (創作になる) — **観測できるようにして気付けるようにする**。
    sections = {f.section for f in gate.draft.facts}
    if len(gate.draft.facts) >= _SECTION_SPREAD_MIN_FACTS and len(sections) <= 1:
        _log.warning(
            "eventnews_sections_not_spread",
            item_id=item.snapshot.item_id,
            facts=len(gate.draft.facts),
            section=next(iter(sections), ""),
        )
    # 原文の前半で打ち切っていないか (2026-08-26 実測: 単独報が 27% 地点で止まり、
    # 後半の被害規模を落とした上で unknowns に「不明」と書いていた)。ここも
    # **自動で書き足さない** — 観測して、プロンプト側で直すための計測。
    reach = coverage.deepest_coverage(
        gate.draft.facts, {index: m.body for index, m in enumerate(selected, start=1)}
    )
    if reach is not None and reach < coverage.WARN_BELOW:
        _log.warning(
            "eventnews_coverage_truncated",
            item_id=item.snapshot.item_id,
            coverage=round(reach, 2),
            sources=len(selected),
            facts=len(gate.draft.facts),
        )
    repo.record_event_version(
        item_id=item.snapshot.item_id,
        version=version,
        generated_at=now,
        model=llm.model,
        prompt_version=_PROMPT_VERSION,
        headline=gate.draft.headline,
        body_json=body_json,
        new_facts_json=new_facts_json,
        verified_at=now if gate.verified else None,
        dropped_lines=gate.dropped_lines,
        repaired_ids=gate.repaired_ids,
    )
    return gate, body_json


async def process_candidates(
    repo: EventNewsMixin,
    candidates: list[MemberArticle],
    vectors: Mapping[str, np.ndarray],
    origin: str,
    llm_factory: Callable[[], LLMClient] | None,
    *,
    generate: bool = True,
    existing: Sequence[tuple[ItemState, Sequence[MemberArticle]]] = (),
) -> ProcessStats:
    """錨時刻順の逐次適用。candidates は anchor_ts 昇順であること。

    ``existing`` に窓内の既存アイテム (状態 + メンバー) を渡すと、その続きから処理する。
    毎時運用ではこれを repo から復元して渡し、リプレイでは空で開始する — **同じ関数で
    本番とリプレイを走らせる**ことで、評価と本番の挙動差を構造的に無くす。
    """
    items: dict[str, _LiveItem] = {
        state.item_id: _LiveItem(snapshot=state, members=list(members))
        for state, members in existing
    }
    stats = dict.fromkeys(
        (
            "created",
            "updated",
            "reinforced",
            "generated",
            "generation_failures",
            "generation_skipped",
            "dropped_lines",
            "repaired_ids",
            "substituted_ids",
        ),
        0,
    )
    llm = llm_factory() if (generate and llm_factory) else None
    reason_counter: dict[str, int] = {}
    later_joins = later_reinforced = 0

    for cand in candidates:
        vec = vectors.get(cand.article_id)
        if vec is None:
            continue
        now = cand.anchor_ts
        snapshot_list = [it.snapshot for it in items.values()]
        member_map = {it.snapshot.item_id: tuple(it.members) for it in items.values()}
        assignment: Assignment = grouping.assign_article(
            cand, vec, snapshot_list, member_map, vectors, now
        )

        if assignment.target_item_id is None:
            item_id = _item_id_for(cand.article_id)
            breakdown = state.compute_source_breakdown([cand])
            snap = ItemState(
                item_id=item_id,
                first_reported_at=cand.anchor_ts,
                last_reported_at=cand.anchor_ts,
                status="new",
                importance=cand.importance,
                current_version=0,
                member_ids=(cand.article_id,),
            )
            items[item_id] = _LiveItem(snapshot=snap, members=[cand], breakdown=breakdown)
            repo.create_event_item(
                item_id=item_id,
                origin=origin,
                first_reported_at=cand.anchor_ts,
                last_reported_at=cand.anchor_ts,
                importance=cand.importance,
                best_source_tier=breakdown.best_tier,
                independent_sources=breakdown.independent,
                state_media_count=breakdown.state_media,
                unclassified_sources=breakdown.unclassified,
                when=now,
            )
            repo.add_event_member(
                item_id=item_id,
                article_id=cand.article_id,
                joined_at=now,
                contributed_new_facts=1,
                join_signal="seed",
            )
            stats["created"] += 1
            continue

        item = items[assignment.target_item_id]
        breakdown_before = item.breakdown or state.compute_source_breakdown(item.members)
        drivers_before = _driver_entities(item.members)
        importance_before = item.snapshot.importance
        item.members.append(cand)
        breakdown_after = state.compute_source_breakdown(item.members)
        decision = state.decide_arrival(
            drivers_before, cand, breakdown_before, breakdown_after, importance_before
        )
        new_importance = _max_importance(importance_before, cand.importance)
        version = item.snapshot.current_version
        item.snapshot = ItemState(
            item_id=item.snapshot.item_id,
            first_reported_at=min(item.snapshot.first_reported_at, cand.anchor_ts),
            last_reported_at=max(item.snapshot.last_reported_at, cand.anchor_ts),
            status=decision.kind,
            importance=new_importance,
            current_version=version,
            member_ids=(*item.snapshot.member_ids, cand.article_id),
        )
        item.breakdown = breakdown_after
        repo.add_event_member(
            item_id=item.snapshot.item_id,
            article_id=cand.article_id,
            joined_at=now,
            contributed_new_facts=1 if decision.kind == "updated" else 0,
            join_signal=",".join(f"{t}:{v}" for t, v in assignment.shared_entities[:4]),
        )
        stats[decision.kind] += 1
        for r in decision.reasons:
            reason_counter[r] = reason_counter.get(r, 0) + 1
        if breakdown_before.independent >= 2:
            later_joins += 1
            if decision.kind == "reinforced":
                later_reinforced += 1

        needs_generation = (
            generate
            and llm is not None
            and len(item.members) >= 2
            and (decision.kind == "updated" or item.snapshot.current_version == 0)
        )
        gate = None
        if needs_generation:
            assert llm is not None  # needs_generation が保証
            new_facts_json = json.dumps(decision.new_facts, ensure_ascii=False, default=str)
            gate, _ = await _generate_version(repo, item, llm, now, new_facts_json)
            if gate is None:
                textual, _ = gen.select_members(item.members)
                key = "generation_skipped" if len(textual) < 2 else "generation_failures"
                stats[key] += 1
            else:
                stats["generated"] += 1
                stats["dropped_lines"] += gate.dropped_lines
                stats["repaired_ids"] += gate.repaired_ids
                stats["substituted_ids"] += gate.substituted_ids
                item.snapshot = ItemState(
                    **{**item.snapshot.__dict__, "current_version": version + 1}
                )
        repo.update_event_item(
            item.snapshot.item_id,
            {
                "status": decision.kind,
                "change_kind": decision.change_kind,
                "current_version": item.snapshot.current_version,
                "importance": new_importance,
                "best_source_tier": breakdown_after.best_tier,
                "independent_sources": breakdown_after.independent,
                "state_media_count": breakdown_after.state_media,
                "unclassified_sources": breakdown_after.unclassified,
                "last_reported_at": item.snapshot.last_reported_at,
                "updated_at": now,
            },
        )

    return ProcessStats(
        **stats,
        reason_counts=tuple(sorted(reason_counter.items())),
        later_joins=later_joins,
        later_reinforced=later_reinforced,
    )


@dataclass(frozen=True)
class BackfillStats:
    attempted: int
    generated: int
    skipped: int
    failed: int


async def generate_pending(
    repo: EventNewsMixin,
    pending: Sequence[tuple[ItemState, Sequence[MemberArticle]]],
    llm_factory: Callable[[], LLMClient],
    *,
    limit: int | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> BackfillStats:
    """まだ版を持たないアイテムに対し、**最終状態で 1 回だけ**生成する。

    過去分の遡及生成 (バックフィル) 用。逐次適用をそのまま再生すると状態遷移ごとに
    生成が走り、読み手には見えない中間版に LLM 時間を費やすことになる。過去の
    「更新の履歴」は事後には価値が無い — 必要なのは今読める最終形なので、
    群化 (``process_candidates(generate=False)``) と生成をこの関数で分ける。
    """
    llm = llm_factory()
    targets = list(pending)[: limit if limit is not None else len(pending)]
    generated = skipped = failed = 0
    for i, (snapshot, members) in enumerate(targets, start=1):
        item = _LiveItem(snapshot=snapshot, members=list(members))
        if on_progress:
            on_progress(i, len(targets), snapshot.item_id)
        # ⚠ **1 件ごとに時刻を取る**。ループの外で 1 回だけ取ると、2.5 時間かけた
        # 253 件が全部同じ generated_at になる (2026-08-25 の遡及で実際にそうなった)。
        # 「いつ書いたか」を全件同じ値にすると、生成の進み方が事後に追えない。
        now = datetime.now(UTC)
        gate, _ = await _generate_version(repo, item, llm, now, "[]")
        if gate is None:
            # 対象外 (skipped) と生成失敗 (failed) の区別は **_should_generate 一箇所**で
            # 決める。ここに条件を複製すると、対象の定義を変えたときに集計だけずれる。
            textual, _ = gen.select_members(item.members)
            if _should_generate(item, textual):
                failed += 1
            else:
                skipped += 1
            continue
        generated += 1
        repo.update_event_item(
            snapshot.item_id,
            {"current_version": snapshot.current_version + 1, "updated_at": now},
        )
    return BackfillStats(
        attempted=len(targets), generated=generated, skipped=skipped, failed=failed
    )
