"""群の中の「同じ出来事でないメンバー」を、ペア判定から決定論で見つける。

⭐ ペア単位 88% と群単位 83% の差の一因は、群の形成が「最良辺 1 本」の貪欲な
参加 + 推移閉包であること (2026-08-31 設計メモ)。辺のモデルがどれだけ良くても、
連鎖 (A-B, B-C は承認 / A-C は否認) とハブ (一括勧告が多数の群に薄く繋がる) は
分割ステップでしか直せない。ここはラベル不要の組合せ処理で、判定は常に
``pair_shadow.judge_pairs`` の出力 (確率) を使う — 判定を 2 か所に持たない。

規則の較正は 2026-09-02 の監査済み 106 群 (過剰統合 43・1,250 ペア) に対して実施:
連結成分 t=0.70 で群 P 100% / R 70%、記事 P 99% / R 76%。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence


def split_components(
    member_ids: Sequence[str],
    pair_probas: Mapping[frozenset[str], float | None],
    *,
    edge_threshold: float,
) -> tuple[list[str], list[list[str]]]:
    """承認辺の連結成分に割り、(本体, 分離する成分の列) を返す。

    - 本体 = 最大成分。既存の id と URL を保持する側。同数のときは
      ``member_ids`` の並びで先に現れるメンバーを含む成分 (決定論)。
    - 分離側は **成分ごとに一塊** — 正しく繋がっている組 (例: 同一被害者の
      公開記事と掲載記録) を単独事象へバラさない (2026-09-03 dry-run で発覚)。
    - ⚠ 判定できなかったペア (None) は **繋がっている扱い** — 証拠の欠如を
      分割の根拠にしない。中立値を閾値と比べる方式は、閾値 > 中立値のとき
      「判定に失敗したペアほど切れやすい」逆転を起こす (同 dry-run で発覚)。
    """
    parent: dict[str, str] = {m: m for m in member_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key, proba in pair_probas.items():
        if proba is not None and proba < edge_threshold:
            continue  # 否認された辺だけが「繋がっていない」
        pair = [m for m in key if m in parent]
        if len(pair) != 2:
            continue
        ra, rb = find(pair[0]), find(pair[1])
        if ra != rb:
            parent[ra] = rb

    groups: dict[str, list[str]] = defaultdict(list)
    for m in member_ids:  # member_ids の並びを保つ
        groups[find(m)].append(m)
    ordered = sorted(groups.values(), key=lambda g: (-len(g), min(member_ids.index(m) for m in g)))
    return ordered[0], ordered[1:]
