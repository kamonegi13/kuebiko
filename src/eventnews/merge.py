"""既にできた事象どうしを突き合わせて統合する (2026-09-21)。

毎時の群化は **新しい記事を既存の事象へ入れる**だけで、**既にできた事象どうし**を
突き合わせない。実測の参加信号は **seed (新規作成) 4,617 に対し既存への参加 146 (3%)**。

⭐ 比較の**単位**が違う:

| | 比較する単位 |
|---|---|
| 毎時の群化 | 1 記事 × 1 事象 |
| **これ** | **記事 × 記事** (事象の枠を外した総当たり) |

事象 A の 3 本目と事象 B の 2 本目が似ている、という関係に毎時は到達できない。
長期化する事案ほど割れる = **追跡価値が高いものほど不利**だった (実例: 国内クラウド
事業者の大規模漏えいの続報が 25-26 日差で別事象、Citrix の「修正」と「悪用確認」が
18 日差で分断、PaperCut の 48 記事が 19 事象)。

⚠ **2026-10-22 に時間の上限を戻した** (``plan_merges(max_gap=...)``、既定 60 日、
``EVENTNEWS_MERGE_MAX_GAP_DAYS``、0 で無制限)。下の「上限なし」は 09-21 の判断で、
判定が緩む方向 (ML のずれ・ハブへの吸収) の歯止めが無いことが全体見直しで問題になった。
30 日と無制限の差は 2% なので、60 日なら正しい統合をほぼ失わず被害の上限になる。

(09-21 当時の判断) **時間差の上限は設けない**。実測 (全期間・候補ペア 62,884):

| 時間差の上限 | 畳む事象 |
|---|---|
| 7 日 (旧 retro_merge) | 599 |
| 30 日 | 1,015 |
| **無制限** | **1,038** |

無制限でも 30 日と実質同じ = 時間が離れた記事はそもそもエッジ判定を通らない。
**判定自体が十分に厳しく、時間の上限は安全弁として機能していない**ので、恣意的な
定数を 1 つ減らす。計算は 3 秒 (ML のみ)。

⚠ 旧 `scripts/retro_merge_events.py` は **`len(member_ids)==1` の単独事象しか見ない**
ため、大群どうし (PaperCut の 11/8/7 件) は永久に統合されなかった。ここでは全事象を見る。

⚠ 一括勧告 (ハブ) の抑止は既存の仕組みに任せる — `edge_is_allowed` と ML の
`kind_advisory_vs_incident`、そして毎時側の定足数ガード。ここで自前の条件を書かない
(2026-08-31 に遡及側が自前の条件を持っていてガードが片方だけ効かなかった)。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from src.eventnews.grouping import blocked_by_different_victims, edge_is_allowed
from src.eventnews.models import FOCAL_CVE_MAX, FREQ_CAP_EXEMPT_TYPES, MemberArticle

#: 事象どうしを結ぶのに要る辺の本数。**1 本では結ばない**。
#: ⭐ 一括勧告 (ハブ) は「多数の無関係な事案と CVE を共有する」ため、相手ごとに
#:   1 本ずつ細い辺を張る。毎時側の `_quorum_blocks` (辺 1 本では参加させない、
#:   2026-09-03) と同じ思想を統合側へ移植する。
#: ⚠ 記事の 88% が未分類なので `event_kind` による除外は当てにならない
#:   (VMware の 38 記事のうち 28 件が未分類)。**構造で見る**。
MIN_EDGES_BETWEEN_ITEMS = 2

#: ハブ (複数 CVE を並記する大きな事象) とみなす記事数の下限。CVE の種類が ``FOCAL_CVE_MAX`` 超
#: のときだけ対象 (主題が明確な大きな事象は制限しない)。2026-10-22 の実例: NetScaler 61-76 記事
#: (CVE 18 種)・GitLab 9-15 記事 (CVE 22 種)
HUB_MIN_ARTICLES = 8
#: ハブの「主題の CVE」= 事象の記事のうち、この割合以上が持つ CVE。NetScaler の主題 (70%) に対し、
#: 吸収された別件の CVE は 4-25%。ハブの CVE の和集合で見ると全ての CVE が「事象の CVE」になる
HUB_CORE_SHARE = 0.30


@dataclass(frozen=True)
class MergeLink:
    """統合の根拠 1 本 = 結ばれた事象の組と、その組を支えた記事対 (辺)。

    ``rescued`` は辺の本数が足りないのに「記事 1 件の事象の救済」で結んだ組 (2026-10-22 追加、
    統合の判断を記録して後から戻せるようにするため)。
    """

    item_a: str
    item_b: str
    edges: tuple[tuple[str, str], ...]
    rescued: bool = False


#: 焦点の照合で「結ばない」値 (F2)。lineage は止めず関係 (系統) の候補として記録するだけ
VETO_BLOCKING_CLASS = "conflict"
#: 記録だけして結ぶ値 (関係の候補)
VETO_RELATION_CLASS = "lineage"


@dataclass(frozen=True)
class LinkVerdict:
    """事象の組を結ぶ前の照合の結果 (焦点の照合の 4 値のうち support 以外)。"""

    klass: str
    detail: str = ""


@dataclass(frozen=True)
class VetoedLink:
    """照合で止めた (または止める対象だった) 事象の組の記録。

    ``enforced`` が True のときだけ実際に結ばなかった。shadow の conflict と lineage
    (関係の候補) は結んだうえで記録だけする。
    """

    item_a: str
    item_b: str
    klass: str
    detail: str
    edge_count: int
    enforced: bool


LinkVeto = Callable[[str, str], LinkVerdict | None]


@dataclass(frozen=True)
class MergeGroup:
    """統合する事象の組。``target`` が統合先 (最初に立った事象)。

    ``links`` は群を結んだ根拠 (記録用。判定には使わない)。
    """

    target: str
    absorbed: tuple[str, ...]
    links: tuple[MergeLink, ...] = ()


def candidate_pairs_by_entity(
    entities: Mapping[str, frozenset[tuple[str, str]]],
    *,
    vectors: Mapping[str, np.ndarray],
    hub_cap: int = 400,
) -> set[tuple[str, str]]:
    """共有 entity で索引を張って候補対を作る (純粋関数)。

    ⚠ ``hub_cap`` を超える汎用 entity は飛ばす。「ランサムウェア」のような語は
    数千記事に付き、総当たりが爆発するうえ意味のある信号にならない。
    """
    by_ent: dict[tuple[str, str], list[str]] = {}
    for aid, es in entities.items():
        for e in es:
            by_ent.setdefault(e, []).append(aid)
    pairs: set[tuple[str, str]] = set()
    for aids in by_ent.values():
        if len(aids) > hub_cap:
            continue
        for i in range(len(aids)):
            for j in range(i + 1, len(aids)):
                a, b = sorted((aids[i], aids[j]))
                if a in vectors and b in vectors:
                    pairs.add((a, b))
    return pairs


def frequent_entity_pairs(
    *,
    uncapped: Mapping[str, set[tuple[str, str]] | frozenset[tuple[str, str]]],
    capped: Mapping[str, frozenset[tuple[str, str]]],
    item_of: Mapping[str, str],
    vectors: Mapping[str, np.ndarray],
    roundups: set[str],
    threshold: float,
    hub_cap: int = 400,
) -> set[tuple[str, str]]:
    """頻出ガードで外した名前を共有する、別々の事象の記事対 (純粋関数、2026-10-02)。

    本文の埋込 cos が ``threshold`` 以上の組だけ (頻出の名前は単独では弱い信号なので、
    本文の近さと組にする)。まとめ記事・被害組織が食い違う組は除く。判定は呼び手が ML に回す。
    """
    by_key: dict[tuple[str, str], list[str]] = {}
    for aid, keys in uncapped.items():
        if aid in roundups or aid not in item_of or aid not in vectors:
            continue
        kept = capped.get(aid, frozenset())
        for k in keys:
            if k in kept or k[0] in FREQ_CAP_EXEMPT_TYPES:
                continue
            by_key.setdefault(k, []).append(aid)
    pairs: set[tuple[str, str]] = set()
    for aids in by_key.values():
        if len(aids) > hub_cap:
            continue
        for i in range(len(aids)):
            for j in range(i + 1, len(aids)):
                a, b = sorted((aids[i], aids[j]))
                if item_of[a] == item_of[b]:
                    continue
                if float(np.dot(vectors[a], vectors[b])) < threshold:
                    continue
                if blocked_by_different_victims(
                    capped.get(a, frozenset()), capped.get(b, frozenset())
                ):
                    continue
                pairs.add((a, b))
    return pairs


def focal_cves_by_item(
    item_of: Mapping[str, str],
    entities: Mapping[str, frozenset[tuple[str, str]]],
) -> dict[str, frozenset[str]]:
    """事象ごとの「主題の CVE」。一括勧告の記事 (CVE が ``FOCAL_CVE_MAX`` 超) は数えない。"""
    out: dict[str, set[str]] = {}
    for aid, iid in item_of.items():
        cves = {v for t, v in entities.get(aid, frozenset()) if t == "cve"}
        if cves and len(cves) <= FOCAL_CVE_MAX:
            out.setdefault(iid, set()).update(cves)
    return {iid: frozenset(v) for iid, v in out.items()}


def _cve_count(entities: frozenset[tuple[str, str]]) -> int:
    return sum(1 for t, _ in entities if t == "cve")


def is_bulletin_cve_bridge(
    ents_a: frozenset[tuple[str, str]],
    ents_b: frozenset[tuple[str, str]],
    shared: Sequence[tuple[str, str]],
) -> bool:
    """辺が CVE だけを根拠にし、**片方だけ**が一括の勧告 (CVE が ``FOCAL_CVE_MAX`` 超) か。

    ⭐ 両方が一括の勧告の辺は止めない: 同じ勧告群を報じた記事どうし (Tomcat・Nessus・KEV 5 件の
    追加の続報) で、14 日の裁定では止めた 13 件のうち同一の 3 件は全てこの形だった。

    一括の勧告は先に事象に入るとハブになり、その CVE の 1 つだけを持つ別件を吸収する
    (別 CVE の勧告どうし、Patch Tuesday が 2 CVE の KEV 記事を橋に別事象へ入った例)。
    被害組織などの CVE 以外の共有がある辺は対象外 (変えない)。
    """
    if not any(t == "cve" for t, _ in shared) or any(t != "cve" for t, _ in shared):
        return False
    return (_cve_count(ents_a) > FOCAL_CVE_MAX) != (_cve_count(ents_b) > FOCAL_CVE_MAX)


def hub_core_cves(
    item_of: Mapping[str, str],
    entities: Mapping[str, frozenset[tuple[str, str]]],
) -> dict[str, frozenset[str]]:
    """ハブの事象ごとの主題の CVE (ハブでない事象は含めない、純粋関数)。

    ハブ = 記事 ``HUB_MIN_ARTICLES`` 件以上で、CVE の種類が ``FOCAL_CVE_MAX`` 超。
    主題 = 全記事のうち ``HUB_CORE_SHARE`` 以上が持つ CVE (一括勧告の記事も数える)。
    """
    members: dict[str, list[str]] = {}
    for aid, iid in item_of.items():
        members.setdefault(iid, []).append(aid)
    out: dict[str, frozenset[str]] = {}
    for iid, aids in members.items():
        if len(aids) < HUB_MIN_ARTICLES:
            continue
        counts: dict[str, int] = {}
        for aid in aids:
            for t, v in entities.get(aid, frozenset()):
                if t == "cve":
                    counts[v] = counts.get(v, 0) + 1
        if len(counts) <= FOCAL_CVE_MAX:
            continue
        floor = HUB_CORE_SHARE * len(aids)
        out[iid] = frozenset(v for v, n in counts.items() if n >= floor)
    return out


def is_hub_minor_cve_edge(
    shared: Sequence[tuple[str, str]],
    side_cores: Sequence[frozenset[str] | None],
) -> bool:
    """CVE だけを根拠にした辺が、ハブの主題でない CVE を橋にしているか。

    ``side_cores`` は辺の両端の事象の主題 CVE (ハブでなければ None)。ハブの側で共有 CVE が
    主題と 1 つも重ならなければ True。被害組織など CVE 以外の共有がある辺は対象外。
    """
    if not shared or any(t != "cve" for t, _ in shared):
        return False
    cves = {v for _, v in shared}
    return any(core is not None and not (cves & core) for core in side_cores)


def reports_of_different_periods(report_periods: Mapping[str, str], a: str, b: str) -> bool:
    """両端がともに期間つきレポートで、期間が違うか (月だけが違う同種のレポート)。"""
    pa, pb = report_periods.get(a), report_periods.get(b)
    return pa is not None and pb is not None and pa != pb


def cve_sets_disjoint(cves: Mapping[str, frozenset[str]], item_a: str, item_b: str) -> bool:
    """両方の事象が主題の CVE を持ち、1 つも重ならないか (= 別の脆弱性の話)。"""
    a, b = cves.get(item_a), cves.get(item_b)
    return bool(a) and bool(b) and not (a & b)  # type: ignore[operator]


def exceeds_time_gap(
    article_ts: Mapping[str, datetime],
    a: str,
    b: str,
    max_gap: timedelta | None,
) -> bool:
    """記事対の時刻差が上限を超えるか。上限なし・時刻不明は False (= 止めない)。"""
    if max_gap is None:
        return False
    ta, tb = article_ts.get(a), article_ts.get(b)
    if ta is None or tb is None:
        return False
    return abs(ta - tb) > max_gap


def _root(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def plan_merges(
    *,
    item_of: Mapping[str, str],
    first_seen: Mapping[str, object],
    entities: Mapping[str, frozenset[tuple[str, str]]],
    vectors: Mapping[str, np.ndarray],
    approved: Sequence[tuple[str, str]] | None = None,
    hub_cap: int = 400,
    min_edges: int = MIN_EDGES_BETWEEN_ITEMS,
    single_edge_ok: set[tuple[str, str]] | None = None,
    extra_edges: Sequence[tuple[str, str]] | None = None,
    cve_disjoint_gate: bool = False,
    bulletin_bridge_gate: bool = False,
    hub_gate: bool = False,
    report_periods: Mapping[str, str] | None = None,
    article_ts: Mapping[str, datetime] | None = None,
    max_gap: timedelta | None = None,
    link_veto: LinkVeto | None = None,
    veto_enforce: bool = True,
    vetoed_out: list[VetoedLink] | None = None,
) -> list[MergeGroup]:
    """統合する群を決める (純粋関数)。

    Args:
        item_of: article_id → item_id
        first_seen: item_id → 最初の報道時刻 (統合先の決定に使う)
        entities: article_id → 参加判定用 entity
        vectors: article_id → 正規化済み埋込
        approved: ML が承認した記事対。**None なら決定論のみ** (ML 不在時の縮退)
        min_edges: 2 つの事象を結ぶのに要る辺の本数。1 だとハブが多数を吸い込む
            (実測: 決定論のみで VMware の一括勧告が 82 事象・277 記事を吸収)
        single_edge_ok: 1 本でも結んでよい記事対 (ML 承認かつまとめ系の特徴が立たない)。
            **記事 1 件の事象** がちょうど 1 つの事象へ辺を持つときだけ使う (2026-09-27)。
            相手が 1 件の事象なら辺は構造上 1 本しか張れず、本数の規則では永久に取り残される
            (盲検: まとめ系を除くと 40/41 正しい)。2 つ以上の事象へ辺を持つ単独記事は救わない
            (2 群を橋渡しして連鎖させない)
        extra_edges: 頻出の名前の共有で ML が承認した記事対
            (2026-10-02、``frequent_entity_pairs``)。
            頻出ガード後の entity では共有が無いため ``edge_is_allowed`` を通らない。承認済みの
            辺として本数に数え、単独事象の救済にも使う (本数の規則そのものは変えない)
        bulletin_bridge_gate: CVE だけを根拠にする辺のうち、一括の勧告 (CVE が
            ``FOCAL_CVE_MAX`` 超) と焦点の記事を結ぶ辺を使わない (一括の勧告を橋にしない)
        hub_gate: CVE だけを根拠にする辺のうち、ハブ (複数 CVE を並記する大きな事象) の
            主題でない CVE が橋の辺を使わない (``hub_core_cves``、事象の CVE の和集合でなく
            焦点の CVE で照合する)
        report_periods: article_id → 期間 (``roundup_detector.report_period``)。両端が期間つき
            レポートで期間が違う辺を使わない (月次レポートどうしを統合しない)。None で無効
        article_ts / max_gap: 記事対の時刻差が ``max_gap`` を超える辺を使わない (2026-10-22)。
            判定が緩む方向 (ML のずれ・ハブ) の被害の上限。None で無制限 (09-21 の挙動)
        link_veto: 結ぶ条件 (辺の本数・救済) を満たした事象の組に当てる焦点の照合 (F2)。
            conflict は結ばない (``veto_enforce`` が False なら記録だけ)。lineage は止めず記録だけ。
            照合は組ごとに独立 (A-B と B-C が通れば A-C が食い違っても推移で結ばれる)
        vetoed_out: 照合が conflict / lineage を返した組の記録の出力先 (呼び手が空の list を渡す)
    """
    pairs = candidate_pairs_by_entity(entities, vectors=vectors, hub_cap=hub_cap)
    focal = focal_cves_by_item(item_of, entities) if cve_disjoint_gate else {}
    hub_cores = hub_core_cves(item_of, entities) if hub_gate else {}
    allow = set(approved) if approved is not None else None
    # ⭐ 事象の組ごとに辺を数える。**1 本では結ばない** (ハブ対策)。
    edge_count: dict[tuple[str, str], int] = {}
    edge_articles: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for a, b in pairs:
        ia, ib = item_of.get(a), item_of.get(b)
        if ia is None or ib is None or ia == ib:
            continue
        if focal and cve_sets_disjoint(focal, ia, ib):
            continue
        if allow is not None and (a, b) not in allow and (b, a) not in allow:
            continue
        shared = tuple(sorted(entities[a] & entities[b]))
        if bulletin_bridge_gate and is_bulletin_cve_bridge(entities[a], entities[b], shared):
            continue
        if hub_cores and is_hub_minor_cve_edge(shared, (hub_cores.get(ia), hub_cores.get(ib))):
            continue
        if report_periods and reports_of_different_periods(report_periods, a, b):
            continue
        if article_ts and exceeds_time_gap(article_ts, a, b, max_gap):
            continue
        cos = float(np.dot(vectors[a], vectors[b]))
        if not edge_is_allowed(entities[a], entities[b], shared, cos):
            continue
        key = (ia, ib) if ia < ib else (ib, ia)
        edge_count[key] = edge_count.get(key, 0) + 1
        edge_articles.setdefault(key, []).append((a, b))

    counted = {tuple(sorted(p)) for arts in edge_articles.values() for p in arts}
    for a, b in extra_edges or ():
        ia, ib = item_of.get(a), item_of.get(b)
        if ia is None or ib is None or ia == ib or tuple(sorted((a, b))) in counted:
            continue
        if focal and cve_sets_disjoint(focal, ia, ib):
            continue
        if report_periods and reports_of_different_periods(report_periods, a, b):
            continue
        if article_ts and exceeds_time_gap(article_ts, a, b, max_gap):
            continue
        key = (ia, ib) if ia < ib else (ib, ia)
        edge_count[key] = edge_count.get(key, 0) + 1
        edge_articles.setdefault(key, []).append((a, b))
        counted.add(tuple(sorted((a, b))))

    rescue_ok = (single_edge_ok or set()) | {tuple(sorted(p)) for p in extra_edges or ()}
    rescued = _singleton_rescues(item_of, edge_articles, rescue_ok)  # type: ignore[arg-type]
    parent: dict[str, str] = {}
    for iid in set(item_of.values()):
        parent[iid] = iid
    used: list[MergeLink] = []
    for (ia, ib), n in edge_count.items():
        is_rescued = (ia, ib) in rescued and n < min_edges
        if n < min_edges and not is_rescued:
            continue
        if link_veto is not None and _vetoed(
            link_veto, ia, ib, n, veto_enforce=veto_enforce, out=vetoed_out
        ):
            continue
        used.append(MergeLink(ia, ib, tuple(sorted(edge_articles[(ia, ib)])), is_rescued))
        ra, rb = _root(parent, ia), _root(parent, ib)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for iid in parent:
        groups.setdefault(_root(parent, iid), []).append(iid)
    links_of: dict[str, list[MergeLink]] = {}
    for link in used:
        links_of.setdefault(_root(parent, link.item_a), []).append(link)
    out: list[MergeGroup] = []
    for root, ids in groups.items():
        if len(ids) < 2:
            continue
        # ⭐ 統合先は **最初に立った事象** — URL がそこに残る
        ordered = sorted(ids, key=lambda i: str(first_seen.get(i, "")))
        out.append(
            MergeGroup(
                target=ordered[0],
                absorbed=tuple(ordered[1:]),
                links=tuple(sorted(links_of.get(root, ()), key=lambda lk: (lk.item_a, lk.item_b))),
            )
        )
    return out


def _vetoed(
    link_veto: LinkVeto,
    item_a: str,
    item_b: str,
    edge_count: int,
    *,
    veto_enforce: bool,
    out: list[VetoedLink] | None,
) -> bool:
    """組を結ばないなら True。止めた・止める対象だった組は ``out`` に残す。"""
    verdict = link_veto(item_a, item_b)
    if verdict is None or verdict.klass not in (VETO_BLOCKING_CLASS, VETO_RELATION_CLASS):
        return False
    blocking = verdict.klass == VETO_BLOCKING_CLASS
    enforced = blocking and veto_enforce
    if out is not None:
        out.append(VetoedLink(item_a, item_b, verdict.klass, verdict.detail, edge_count, enforced))
    return enforced


def _singleton_rescues(
    item_of: Mapping[str, str],
    edge_articles: Mapping[tuple[str, str], list[tuple[str, str]]],
    single_edge_ok: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """本数が足りなくても結ぶ事象の組 (記事 1 件の事象の救済、pure)。"""
    if not single_edge_ok:
        return set()
    size: dict[str, int] = {}
    for iid in item_of.values():
        size[iid] = size.get(iid, 0) + 1
    ok = {tuple(sorted(p)) for p in single_edge_ok}
    # 単独事象ごとに、強い辺で繋がる相手の事象を集める
    partners: dict[str, set[str]] = {}
    for (ia, ib), arts in edge_articles.items():
        if not any(tuple(sorted(p)) in ok for p in arts):
            continue
        for single, other in ((ia, ib), (ib, ia)):
            if size.get(single) == 1:
                partners.setdefault(single, set()).add(other)
    out: set[tuple[str, str]] = set()
    for single, others in partners.items():
        if len(others) != 1:
            continue  # 2 つ以上へ繋がる単独記事は橋渡しになるので救わない
        other = next(iter(others))
        out.add((single, other) if single < other else (other, single))
    return out


def edge_inputs(members: Mapping[str, MemberArticle]) -> list[tuple[str, str, str]]:
    """``build_join_entities`` へ渡す (article_id, entity_type, value) の並び。"""
    from src.eventnews.models import JOIN_ENTITY_TYPES

    return [
        (aid, et, val)
        for aid, m in members.items()
        for et, val in m.entities
        if et in JOIN_ENTITY_TYPES
    ]
