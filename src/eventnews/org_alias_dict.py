"""被害組織名の別名辞書の組み立て (ORG-D-IMPL、純関数)。

辞書の中身 = ``org_identity_pairs`` の「同一」判定を ``build_alias_map`` で連結した写像
(照合キー → 代表キー)。照合キーは決定論の ``org_match_key`` (NFKC・大小・全半角・法人格・括弧・
記号・空白を畳む) で、別名 (言語違い・略称・旧社名) だけが判定役の結果に由来する。

伏字 (``*``) を含む名前は辞書に入れない。``org_match_key`` は記号を落とすので、キーだけでは
伏字かどうか分からない — 呼び手が伏字の**名前**を渡し、そのキーを含む組を辞書の材料から外す。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.cti.org_normalizer import org_match_key
from src.eventnews.grouping import is_masked_victim
from src.eventnews.org_identity import build_alias_map

DICT_SOURCE_PAIRS = "pairs"


def masked_keys(names: Iterable[str]) -> frozenset[str]:
    """伏字を含む名前の照合キー (空キーは除く)。"""
    return frozenset(k for n in names if is_masked_victim(n) and (k := org_match_key(n)))


def build_dict_mapping(
    same_pairs: Iterable[tuple[str, str]], masked: frozenset[str] = frozenset()
) -> dict[str, str]:
    """同一の組 → 辞書の写像。伏字のキーを含む組は外す (橋渡しにもさせない)。"""
    kept = [(a, b) for a, b in same_pairs if a not in masked and b not in masked]
    return dict(build_alias_map(kept).mapping)


def diff_mapping(
    current: Mapping[str, str], new: Mapping[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """(追加, 変更, 削除) のキー一覧 (ソート済み)。"""
    added = sorted(k for k in new if k not in current)
    changed = sorted(k for k in new if k in current and current[k] != new[k])
    removed = sorted(k for k in current if k not in new)
    return added, changed, removed
