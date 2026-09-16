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

import json
from typing import Any

#: 参照を探す葉のプロパティ名 → 参照先の種別。
_KEYWORD_LIST_PROPERTY = "keyword_list"


def _referenced_values(node: Any, prop: str) -> set[str]:
    """条件木のどこかで ``prop`` が参照している値を集める (入れ子・否定・**旧形**も辿る)。

    ⚠ 入れ子や ``not`` の奥を見落とすと「未参照」と誤表示し、消してよいものだと
    読ませてしまう。

    ⚠⚠ **旧形の葉を必ず正規化してから見る**。本番の配信ルールは旧形
    ``{"keyword_list": {"in": [...]}}`` で参照しており、新形だけを見ていたため実際には
    参照されている 2 リストが「未参照」と表示された (2026-09-16 実データで発覚)。
    評価器 ``_eval_condition`` は両形を扱う — **同じ正規化を通す** (判定が 2 箇所に
    分かれると必ずずれる)。
    """
    if not isinstance(node, dict):
        return set()
    if not node:
        return set()
    has_combinator = any(k in node for k in ("all", "any", "not", "always"))
    if "property" not in node and not has_combinator:
        from src.cti.routing_rules import _normalize_condition

        normalized = _normalize_condition(node)
        # 正規化で形が変わらなければ未知の葉 — 無限再帰を避けて打ち切る。
        return _referenced_values(normalized, prop) if normalized != node else set()
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


def match_list_usage(names: list[str], rules: list[Any]) -> dict[str, list[str]]:
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


def sir_usage(pir_ids: list[str], standing_rows: list[Any]) -> dict[str, list[str]]:
    """SIR id → それを参照している常設情報要求 (問い) の situation_id (昇順)。

    SIR を消すと、参照していた問いの SIR リンクが孤児になる。編集画面で
    「この SIR は N 件の問いから参照されている」が見えることが移設の前提条件。

    ⚠ **配信ルールは SIR を参照しない** (R0 撤去済、CLAUDE.md §13 設計原則 2)。
    参照元は常設情報要求であって routing ではない — ここを取り違えると
    「参照ゼロ」と誤表示する。

    ⚠ ``situations.pir_ids`` は **JSON 文字列**で保持される (text 列)。
    list が来る経路 (射影済み) と両方を受ける。
    """
    usage: dict[str, set[str]] = {p: set() for p in pir_ids}
    for row in standing_rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("situation_id") or "")
        if not sid:
            continue
        raw = row.get("pir_ids")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                continue
        if not isinstance(raw, list | tuple):
            continue
        for pid in raw:
            if str(pid) in usage:
                usage[str(pid)].add(sid)
    return {p: sorted(ids) for p, ids in usage.items()}


def actor_usage(actors: list[tuple[str, str, list[str]]], pirs: list[Any]) -> dict[str, list[str]]:
    """アクター id → そのアクターを名指ししている SIR id (昇順)。

    Args:
        actors: ``(actor_id, canonical, aliases)`` の並び。
        pirs: SIR 定義 (``strong_signals.actors`` に名前が列挙される)。

    実データ (2026-09-16): 20 SIR のうち 3 件が actors を列挙している。
    canonical や別名を変えるとこの名指しが外れ、**SIR が静かに該当しなくなる**。
    辞書の編集画面でこれが見えることが移設の前提条件。

    ⚠ 名指しは **canonical でも別名でも**書かれうる (実データに「Cozy Bear」形式がある)。
    canonical だけを見ると別名で書かれた参照を見落とす。照合は大小文字を無視する。

    ⚠ 参照ゼロを「消してよい」と読ませない。そもそも辞書は削除でなく merge + 墓標が
    原則 (identity 8 原則)。
    """
    named: list[tuple[str, set[str]]] = []
    for actor_id, canonical, aliases in actors:
        keys = {canonical.strip().lower()} | {a.strip().lower() for a in aliases if a}
        named.append((actor_id, {k for k in keys if k}))

    usage: dict[str, set[str]] = {actor_id: set() for actor_id, _keys in named}
    for pir in pirs:
        if not isinstance(pir, dict):
            continue
        pir_id = str(pir.get("id") or "")
        signals = pir.get("strong_signals")
        if not pir_id or not isinstance(signals, dict):
            continue
        raw = signals.get("actors")
        if not isinstance(raw, list | tuple):
            continue
        mentioned = {str(x).strip().lower() for x in raw if x}
        for actor_id, keys in named:
            if keys & mentioned:
                usage[actor_id].add(pir_id)
    return {a: sorted(ids) for a, ids in usage.items()}
