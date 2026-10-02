"""事象単位ニュースの群化規則 (docs/event_news_design.md §5)。

決定論の参加判定のみを扱う純ロジック層 (DB / LLM 非依存)。numpy は内部の
コサイン類似度計算にのみ使い、公開 API (models.py の型) は numpy 非依存を保つ。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

import numpy as np

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.models import (
    COS_THRESHOLD,
    DORMANT_AFTER_DAYS,
    DORMANT_REJOIN_COS,
    DORMANT_REJOIN_SHARED,
    ENTITY_FREQ_CAP,
    FOCAL_CVE_COS,
    FOCAL_CVE_MAX,
    FREQ_CAP_EXEMPT_TYPES,
    JOIN_ENTITY_TYPES,
    SHARED_NAMES_COS,
    SHARED_NAMES_MIN,
    WINDOW_HOURS,
    Assignment,
    ItemState,
    MemberArticle,
)

# 1 記事の中に共通 entity が無いエッジは検討しない (§5 の 2 信号要求の下限)。
_MIN_SHARED_FOR_EDGE = 1

_EdgeOutcome = tuple[float, tuple[tuple[str, str], ...]] | str | None


def join_entity_key(entity_type: str, value: str) -> tuple[str, str]:
    """結合信号 entity のキー。**分母 (頻出ガードの counts) と参照側で必ず共有する**。

    victim_org だけ自由記述なので ``normalize_for_match`` で表記揺れを畳む。
    2026-08-25 まで counts 側は SQL の ``LOWER(TRIM(value))`` でキーを作っていたため
    victim_org のキーが**永久に一致せず、cap が一度も効いていなかった**
    (実測: shell / nasa 等 3 値が cap 超のまま結合信号として通っていた)。
    キーの作り方が 2 箇所にあると必ずずれるので、ここを唯一の口にする。
    """
    if entity_type == "victim_org":
        return (entity_type, normalize_for_match(value))
    return (entity_type, value)


def build_join_entities(
    raw: Iterable[tuple[str, str, str]],
    counts: Mapping[tuple[str, str], int],
) -> dict[str, frozenset[tuple[str, str]]]:
    """(article_id, entity_type, value) から結合信号となる entity 集合を組み立てる。

    - ``JOIN_ENTITY_TYPES`` (cve/victim_org/actor/malware_family) のみ採用
      (``actor_provisional`` は集合に含まれないため自動的に除外される)
    - victim_org は free-form のため ``normalize_for_match`` で正規化
    - ``counts`` (正規化後の (entity_type, value) をキーとする窓内出現記事数) が
      ``ENTITY_FREQ_CAP`` を超える値は結合信号から除外する (頻出ガード)
    """
    grouped: dict[str, set[tuple[str, str]]] = {}
    for article_id, entity_type, value in raw:
        if entity_type not in JOIN_ENTITY_TYPES:
            continue
        key = join_entity_key(entity_type, value)
        if entity_type not in FREQ_CAP_EXEMPT_TYPES and counts.get(key, 0) > ENTITY_FREQ_CAP:
            continue
        grouped.setdefault(article_id, set()).add(key)

    return {article_id: frozenset(values) for article_id, values in grouped.items()}


def _unit_vector(vec: np.ndarray) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm == 0.0:
        return arr
    return arr / norm


def _is_dormant(item: ItemState, now: datetime) -> bool:
    return (now - item.last_reported_at) >= timedelta(days=DORMANT_AFTER_DAYS)


def _within_window(anchor_ts: datetime, last_reported_at: datetime) -> bool:
    return abs((anchor_ts - last_reported_at).total_seconds()) <= WINDOW_HOURS * 3600


def _cve_count(entities: frozenset[tuple[str, str]]) -> int:
    return sum(1 for entity_type, _ in entities if entity_type == "cve")


def distinct_shared_names(shared: Sequence[tuple[str, str]]) -> int:
    """共有している **名前の数**。entity の数ではない。

    同じ名前が複数の type で抽出されることがある (実例: ``kimsuky`` が actor と
    malware_family の両方)。entity の数で数えると 1 つの根拠が 2 つに見え、
    別キャンペーンの記事が繋がる。
    """
    return len({value.casefold() for _, value in shared})


def _shares_focal_cve(
    cand_entities: frozenset[tuple[str, str]],
    member_entities: frozenset[tuple[str, str]],
    shared: Sequence[tuple[str, str]],
) -> bool:
    """CVE を **主題として** 共有しているか (一括アドバイザリを除く)。"""
    if not any(entity_type == "cve" for entity_type, _ in shared):
        return False
    return not (
        _cve_count(cand_entities) > FOCAL_CVE_MAX or _cve_count(member_entities) > FOCAL_CVE_MAX
    )


def required_cos(
    cand_entities: frozenset[tuple[str, str]],
    member_entities: frozenset[tuple[str, str]],
    shared: Sequence[tuple[str, str]],
) -> float:
    """このペアに要求する cos。緩めるのは 2 つの場合だけ。

    1. **CVE を主題として共有する** — 主題の定義は「両方の記事が CVE を
       ``FOCAL_CVE_MAX`` 個以下しか持たない」= 一括アドバイザリではないこと。
       列挙側 (1 記事で数十〜278 個) を緩めると無関係な事案が接着する。
    2. **特異な名前を ``SHARED_NAMES_MIN`` 個以上共有する** (2026-08-31 追加) —
       1 の発想を CVE 以外へ広げたもの。⚠ **名前の数で数える** (同じ名前が
       actor と malware_family に重複して出ると 1 つの根拠が 2 つに見える)。
       ⚠ **1 名では緩めない** — ランサム流出サイトの「同じグループ・別の被害者」
       が接着する。

    どちらにも当たらなければ ``COS_THRESHOLD``。
    """
    if _shares_focal_cve(cand_entities, member_entities, shared):
        return FOCAL_CVE_COS
    if distinct_shared_names(shared) >= SHARED_NAMES_MIN:
        return SHARED_NAMES_COS
    return COS_THRESHOLD


def names_of_type(entities: frozenset[tuple[str, str]], entity_type: str) -> set[str]:
    """その型の値だけを取り出す。"""
    return {value for etype, value in entities if etype == entity_type}


def shared_is_actor_name_only(shared: Sequence[tuple[str, str]]) -> bool:
    """共有が実質「アクター名 1 つ」だけか。だとしたら辺にしない。

    ⭐ **アクターは「誰が」であって「何が起きたか」ではない。** 同じ攻撃者の別々の
    作戦は必ずアクター名を共有するので、これを単独で辺の根拠にすると、活動が活発な
    攻撃者ほど全部 1 つに潰れる (実測 2026-08-31: Kimsuky の 6 記事が 1 事象になり、
    うち 3 件は Chrome 拡張 / 予備軍標的 / 漏洩問い合わせ詐称の**別作戦**だった)。

    CVE・被害者名・マルウェア名・ツール名は「何が起きたか」を指すので 1 つでも
    根拠になる。アクター名だけが例外。実測でこの条件に依存する辺は全体の 3%。

    ⚠ **型ではなく名前で見る。** ``kimsuky`` は actor と malware_family の両方に
    抽出されることがあり、型で判定すると「2 種類あるから可」と誤って通る。
    """
    names = {value.casefold() for _, value in shared}
    if len(names) != 1:
        return False
    only = next(iter(names))
    return any(etype == "actor" and value.casefold() == only for etype, value in shared)


def blocked_by_different_victims(
    cand_entities: frozenset[tuple[str, str]],
    member_entities: frozenset[tuple[str, str]],
) -> bool:
    """**名指しの被害者が食い違うなら別の事象**。cos がいくら高くても繋がない。

    ⭐ 動機: 身代金リークサイトの投稿は「アクター: 被害者 (国)」という**ほぼ同一の
    書式**なので、被害者が違っても cos が 0.75-0.78 に来る (実測 2026-08-31:
    ``Xpl0itrs: Gruppo Spaggiari Parma`` ⇔ ``xpl0itrs: BMW Group`` が 0.774)。
    actor 名を共有するだけで既定の 0.70 を超えるため、**アクター名を辞書へ入れた
    瞬間に、そのグループの被害者が全部 1 事象へ潰れる**。
    2026-07-26 に Play/Chaos/Deadlock 等を一括承認して言及層の 59% が誤検出化した
    のと同じ経路で、辞書の充実がそのまま群化の破壊になる。

    ⚠ **片方にしか被害者が無いときは塞がない。** 技術解説や続報は被害者名を持たない
    ことがあり、塞ぐと正しい合流まで落ちる。両方が名指ししていて、かつ 1 つも
    重ならないときだけ「別の事象」と判断する。
    """
    cand = names_of_type(cand_entities, "victim_org")
    member = names_of_type(member_entities, "victim_org")
    if not cand or not member:
        return False
    return not (cand & member)


def edge_is_allowed(
    cand_entities: frozenset[tuple[str, str]],
    member_entities: frozenset[tuple[str, str]],
    shared: Sequence[tuple[str, str]],
    cos: float,
) -> bool:
    """このペアを辺にしてよいか。**参加判定と遡及統合は必ずこれを共有する。**

    判定を 2 箇所に持つと必ずずれる — 2026-08-31 に遡及統合スクリプトが自前の
    条件を持っていたため、本番へ入れたガードが遡及側で効かず、別作戦の
    Kimsuky 記事が統合対象のまま残った。
    """
    if len(shared) < _MIN_SHARED_FOR_EDGE:
        return False
    if blocked_by_different_victims(cand_entities, member_entities):
        return False
    if shared_is_actor_name_only(shared):
        return False
    return cos >= required_cos(cand_entities, member_entities, shared)


#: ML 参加の quorum — 候補×既存メンバーの判定平均がこれ未満なら参加させない。
#: 較正 (2026-09-02・監査 106 群): 平均確率 0.5 で 記事 P 95% / R 84%。実例検証
#: (2026-09-03): SonicWall 事象へ混入した KEV 一括記事は最大辺 0.75 で通ったが
#: 平均 0.49 — この規則なら止まっていた。残る過剰統合 11.7% はハブ (一括勧告) が
#: 1 本の強い辺で入る形なので、辺 1 本でなく分布で判定する。
ML_JOIN_QUORUM_MEAN = 0.5
#: 判定済みペアがこれ未満なら quorum を適用しない (1 対だけでは分布にならない)。
_QUORUM_MIN_JUDGED = 2


def _quorum_blocks(
    cand_id: str,
    members: Sequence[MemberArticle],
    pair_proba: Mapping[frozenset[str], float] | None,
) -> bool:
    """候補とアイテムの判定済みペアの平均が quorum を割っているか。"""
    if not pair_proba:
        return False
    judged = [
        pair_proba[key]
        for member in members
        if (key := frozenset((cand_id, member.article_id))) in pair_proba
    ]
    if len(judged) < _QUORUM_MIN_JUDGED:
        return False
    return sum(judged) / len(judged) < ML_JOIN_QUORUM_MEAN


def _member_edges(
    cand_entities: frozenset[tuple[str, str]],
    cand_unit: np.ndarray,
    members: Sequence[MemberArticle],
    member_vecs: Mapping[str, np.ndarray],
    *,
    cand_id: str = "",
    pair_decision: Mapping[frozenset[str], bool] | None = None,
) -> list[tuple[float, tuple[tuple[str, str], ...], float]]:
    """各メンバーとの (cos, 共有 entity, 要求 cos) を、共有 entity があるものだけ集める。"""
    edges: list[tuple[float, tuple[tuple[str, str], ...], float]] = []
    for member in members:
        if member.is_roundup:
            continue  # まとめ記事は辺の相手にしない (まとめ記事経由の混入を防ぐ)
        shared = tuple(sorted(cand_entities & member.entities))
        vec = member_vecs.get(member.article_id)
        if vec is None:
            continue
        cos = float(np.dot(cand_unit, _unit_vector(vec)))
        if pair_decision is not None:
            # ⭐ 判定を外へ委ねる (ML)。**cos は辺の順位付けにだけ使う** —
            #    どのアイテムを選ぶかは従来どおり最高 cos で決める。
            if not pair_decision.get(frozenset((cand_id, member.article_id)), False):
                continue
            # ⚠ 委ねた以上、要求 cos を下流で掛け直さない。2026-09-01 の切替直後、
            #    _item_candidate の `cos >= need` が残っていたため ML の承認 13 組の
            #    うち通ったのは 2 組だけで、ML は辺を**減らせても増やせない**状態
            #    だった (承認された候補 6 件に対し実際の合流は 1 件)。
            #    dormant への再参加だけは別の定数で厳しいまま (掘り起こしは緩めない)。
            edges.append((cos, shared, 0.0))
            continue
        if not edge_is_allowed(cand_entities, member.entities, shared, cos):
            continue
        edges.append((cos, shared, required_cos(cand_entities, member.entities, shared)))
    return edges


def _item_candidate(
    item: ItemState,
    candidate: MemberArticle,
    cand_unit: np.ndarray,
    members: Sequence[MemberArticle],
    member_vecs: Mapping[str, np.ndarray],
    now: datetime,
    pair_decision: Mapping[frozenset[str], bool] | None = None,
    pair_proba: Mapping[frozenset[str], float] | None = None,
) -> _EdgeOutcome:
    """1 アイテムに対する参加可否を判定する。

    戻り値: 参加不可 (エッジ自体が無い) は None、参加不可だが理由がある場合は
    その理由文字列 (例: 'dormant_strict')、参加可能なら (cos, 共有 entity)。
    """
    edges = _member_edges(
        candidate.entities,
        cand_unit,
        members,
        member_vecs,
        cand_id=candidate.article_id,
        pair_decision=pair_decision,
    )
    if not edges:
        return None
    if pair_decision is not None and _quorum_blocks(candidate.article_id, members, pair_proba):
        # ⭐ 辺 1 本で入れない (2026-09-03)。ハブ (一括勧告) は 1 メンバーとだけ強く
        #    繋がり、群全体とは繋がらない。判定済みペアの**平均**で参加を決める。
        return "ml_quorum"

    if _is_dormant(item, now):
        # dormant への再参加は緩めない (古いアイテムを掘り起こす条件は厳しいまま)
        strict = [
            (cos, shared)
            for cos, shared, _ in edges
            if cos >= DORMANT_REJOIN_COS and len(shared) >= DORMANT_REJOIN_SHARED
        ]
        if strict:
            return max(strict, key=lambda pair: pair[0])
        normal = [(cos, shared) for cos, shared, need in edges if cos >= need]
        return "dormant_strict" if normal else None

    if not _within_window(candidate.anchor_ts, item.last_reported_at):
        return None

    normal = [(cos, shared) for cos, shared, need in edges if cos >= need]
    if not normal:
        return None
    return max(normal, key=lambda pair: pair[0])


def _invariant_holds(candidate: MemberArticle, members: Sequence[MemberArticle]) -> bool:
    """参加後、全メンバー + candidate が共有する entity が 1 つ以上残るか。"""
    common = candidate.entities
    for member in members:
        common = common & member.entities
        if not common:
            return False
    return True


def assign_article(
    candidate: MemberArticle,
    cand_vec: np.ndarray,
    items: Sequence[ItemState],
    item_members: Mapping[str, tuple[MemberArticle, ...]],
    member_vecs: Mapping[str, np.ndarray],
    now: datetime,
    pair_decision: Mapping[frozenset[str], bool] | None = None,
    pair_proba: Mapping[frozenset[str], float] | None = None,
) -> Assignment:
    """1 記事をどのアイテムに参加させるか判定する (§5)。

    2 信号 (cos ≥ COS_THRESHOLD かつ entity 共有 ≥ 1) を満たすエッジを持つ
    アイテムのうち、アイテム不変条件を満たすものの中から
    最高 cos (同点は first_reported_at の古い方) を選ぶ。どこにも入らなければ
    ``target_item_id=None`` (呼出側が新アイテムを起こす)。

    ⭐ ``pair_decision`` を渡すと、辺の可否の判定を**そこへ委ねる** (ML 判定)。
    渡さなければ従来どおり ``edge_is_allowed`` (決定論) が決める。**どのアイテムを
    選ぶかは常に最高 cos** — 判定を差し替えても選び方は変えない。
    """
    if candidate.is_roundup:
        # まとめ記事は既存の事象に入らない (単独の事象として立つ、2026-10-02)
        return Assignment(
            article_id=candidate.article_id,
            target_item_id=None,
            max_cos=0.0,
            shared_entities=(),
            rejected=("roundup",),
        )
    cand_unit = _unit_vector(cand_vec)
    rejected: list[str] = []
    best_item: ItemState | None = None
    best_cos = 0.0
    best_shared: tuple[tuple[str, str], ...] = ()

    for item in items:
        members = item_members.get(item.item_id, ())
        if not members:
            continue

        outcome = _item_candidate(
            item, candidate, cand_unit, members, member_vecs, now, pair_decision, pair_proba
        )
        if outcome is None:
            continue
        if isinstance(outcome, str):
            rejected.append(outcome)
            continue

        if not _invariant_holds(candidate, members):
            rejected.append("invariant")
            continue

        cos, shared = outcome
        is_better = best_item is None or cos > best_cos
        is_tie_older = (
            not is_better
            and best_item is not None
            and cos == best_cos
            and item.first_reported_at < best_item.first_reported_at
        )
        if is_better or is_tie_older:
            best_item, best_cos, best_shared = item, cos, shared

    return Assignment(
        article_id=candidate.article_id,
        target_item_id=best_item.item_id if best_item is not None else None,
        max_cos=best_cos,
        shared_entities=best_shared,
        rejected=tuple(rejected),
    )
