"""定義の参照関係 — 「この定義を誰が使っているか」。

設計: docs/settings_consolidation_plan.md §4。

配置基準の改訂 (CLAUDE.md §11、2026-09-16) で定義の変更を設定カテゴリへ寄せるにあたり、
編集画面には **①効果 (preview) ②参照関係 ③成績 (KPI)** を揃える要件を課した。
本モジュールは ② を担う。

⚠ **なぜ移設より先に要るか**: 旧配置基準の根拠は「対象を見ながら直せる」だったのに、
マッチリスト編集画面は**配信ルールを表示していなかった** — 理屈だけで実装が伴っていな
かった。参照関係を持たないまま設定へ移すと、同じ「理屈だけ」を設定側に再現する。

⚠ **未参照を「削除してよい」と読ませない**。参照ゼロは「いま使われていない」だけで、
これから使う下書きかもしれない。ここは事実だけを返し、判断は利用者に委ねる。
"""

from __future__ import annotations

from typing import Any

#: 参照を探す葉のプロパティ名 → 参照先の種別。
_KEYWORD_LIST_PROPERTY = "keyword_list"


def _referenced_values(node: Any, prop: str) -> set[str]:
    """条件木のどこかで ``prop`` が参照している値を集める (入れ子・否定も辿る)。

    ⚠ 入れ子や ``not`` の奥を見落とすと「未参照」と誤表示し、消してよいものだと
    読ませてしまう。
    """
    if not isinstance(node, dict):
        return set()
    if node.get("property") == prop:
        value = node.get("value")
        if isinstance(value, list | tuple):
            return {str(v) for v in value}
        return {str(value)} if value is not None else set()
    out: set[str] = set()
    for key in ("all", "any"):
        children = node.get(key)
        if isinstance(children, list | tuple):
            for child in children:
                out |= _referenced_values(child, prop)
    if "not" in node:
        out |= _referenced_values(node["not"], prop)
    return out


def match_list_usage(
    names: list[str], rules: list[Any]
) -> dict[str, list[str]]:
    """マッチリスト名 → それを参照する配信ルール id (昇順・重複なし)。

    Args:
        names: 一覧に出すマッチリスト名。**未参照のものもキーとして返す**
            (欠落と区別できないと画面が何も出せない)。
        rules: 配信ルール (保存前の下書きが来る経路があるため、壊れていても落とさない)。
    """
    usage: dict[str, set[str]] = {n: set() for n in names}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_id = str(rule.get("id") or "")
        if not rule_id:
            continue
        for value in _referenced_values(rule.get("when"), _KEYWORD_LIST_PROPERTY):
            if value in usage:
                usage[value].add(rule_id)
    return {name: sorted(ids) for name, ids in usage.items()}
