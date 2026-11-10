"""分けた形の「塊 → 追跡中の情勢」の合流で、別の事象を合流させない関門 (DETECT-RECALL、既定 off)。

11-10 の再判定の合流 31 塊のうち 10 が別の事象だった: 同じ製品の別の CVE 4・名前の似た別組織や
同業の別事案 5・分析記事を事象に合流 1。割当 ML は話題の近さを見るので、**名指しの食い違い**を
見落とす。ここは決定論で 2 つを止める (どちらも「両側が名指ししていて、重ならない」ときだけ。
片側にしか無いときは止めない — 続報や解説は名指しを持たないことがある):

- ``cve``: 塊の記事の CVE と、情勢の CVE (anchors + 題名) が交わらない
- ``victim``: 被害組織名が食い違う。群化の ``blocked_by_different_victims`` を再利用し、
  組織名は ``org_match_key`` (別名判定の照合キー) で畳み、別名判定 (``org_alias``) の「同一」と
  表記ゆれ・略称 (``org_names_compatible``、焦点の関門と同じ) は食い違いに数えない

部品は複製しない。合流を止めた塊は書き手に回る (開設できるかは通常の関門が決める)。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from src.cti.ioc_extractor import extract_iocs
from src.cti.org_normalizer import org_match_key, org_names_compatible
from src.eventnews.grouping import blocked_by_different_victims
from src.eventnews.org_identity import AliasMap, remap_entities

ConflictKind = Literal["cve", "victim"]
_CVE = "cve"
_VICTIM = "victim_org"


def _typed(keys: Iterable[str], entity_type: str) -> list[str]:
    prefix = f"{entity_type}:"
    return [k[len(prefix) :] for k in keys if k.startswith(prefix)]


def _cves(keys: Iterable[str], title: str = "") -> frozenset[str]:
    found = {v.upper() for v in _typed(keys, _CVE)}
    found.update(c.upper() for c in extract_iocs(title).cves)
    return frozenset(found)


def _victims(keys: Iterable[str], alias: AliasMap) -> frozenset[tuple[str, str]]:
    entities = frozenset((_VICTIM, org_match_key(v)) for v in _typed(keys, _VICTIM))
    return remap_entities(entities, alias)


def _any_compatible(a: frozenset[tuple[str, str]], b: frozenset[tuple[str, str]]) -> bool:
    """どれか 1 組が同じ組織に読める (表記ゆれ・略称 ⇔ 正式名の接頭辞)。食い違いを消す方向だけ。"""
    return any(org_names_compatible(x, y) for _, x in a for _, y in b)


@dataclass(frozen=True)
class MergeConflictGate:
    """塊と情勢の名指しの食い違いを調べる (不変)。"""

    entities_by_id: Mapping[str, set[str]]
    anchors_by_sid: Mapping[str, frozenset[str]]
    titles_by_sid: Mapping[str, str]
    org_alias: Mapping[str, str] = field(default_factory=dict)

    def conflict(self, cluster: Sequence[str], situation_id: str) -> ConflictKind | None:
        cluster_keys = [k for a in cluster for k in self.entities_by_id.get(a, ())]
        anchors = self.anchors_by_sid.get(situation_id, frozenset())
        cluster_cves = _cves(cluster_keys)
        sit_cves = _cves(anchors, self.titles_by_sid.get(situation_id, ""))
        if cluster_cves and sit_cves and not (cluster_cves & sit_cves):
            return "cve"
        alias = AliasMap(dict(self.org_alias))
        cluster_victims, sit_victims = _victims(cluster_keys, alias), _victims(anchors, alias)
        if blocked_by_different_victims(cluster_victims, sit_victims) and not _any_compatible(
            cluster_victims, sit_victims
        ):
            return "victim"
        return None

    def allowed(self, cluster: Sequence[str], situation_ids: Sequence[str]) -> list[str]:
        """合流してよい情勢 (食い違いのないもの) だけを元の順で返す。"""
        return [s for s in situation_ids if self.conflict(cluster, s) is None]
