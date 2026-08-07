"""signal-first PIR 照合 — 意味プロパティ述語ツリーを ArticleFacts に対して評価する。

routing の述語エンジンを再利用する:
- 演算子意味論 = ``routing_rules._apply_op`` (純粋関数、in_config 以外は SourceQualityConfig 非依存)
- 条件ツリー文法 = ``all`` / ``any`` / ``not`` / ``always`` / leaf ``{property, op, value}``
  (routing_rules._eval_condition と同一)

PoC (Phase 1) は routing の PROPERTY_CATALOG / accessor を触らず、葉の解決だけを
ArticleFacts 上の PIR-local resolver (``_PROPS``) で行う。catalog 一元化は Phase 3 で
routing を巻き込んで実施する (docs/pir_signal_first_matching_design.md §3.1)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.config_loader import SourceQualityConfig
from src.cti.keyword_match import keyword_in_text
from src.cti.routing_rules import _apply_op
from src.pir.article_facts import ArticleFacts

# _apply_op は in_config 演算子でのみ sq を参照する。PIR 述語は in_config を使わないため
# 既定値の SourceQualityConfig で十分 (全フィールド default 有り)。
_SQ = SourceQualityConfig()

# PIR-local property resolver: prop_id -> (kind, ArticleFacts からの読取)。
# kind は routing の _apply_op と同一語彙 ("str" | "boolean" | "set" | "number")。
_PROPS: dict[str, tuple[str, Callable[[ArticleFacts], object]]] = {
    "category": ("str", lambda f: f.category),
    "victim_sector": ("str", lambda f: f.victim_sector),
    "victim_country": ("str", lambda f: f.victim_country),
    "intent": ("str", lambda f: f.intent),
    "is_ransomware": ("boolean", lambda f: f.is_ransomware),
    # フィード名 (完全名)。exact でなく contains_any 演算子で substring 照合する。
    "feed_title": ("str", lambda f: f.feed_title),
}

# property → 許可 op (validate_match_tree / compiler / UI ヒントの SSoT)。
# text/title = keyword_any スコープ、actor/actor_nation = 主題アクターゲート leaf
# (docs/pir_authoring_unification_design.md §3.1-3.2)。
LEAF_OPS: dict[str, frozenset[str]] = {
    "category": frozenset({"eq", "in"}),
    "intent": frozenset({"eq", "in"}),
    "victim_country": frozenset({"eq", "in"}),
    "victim_sector": frozenset({"eq", "in"}),
    "is_ransomware": frozenset({"is_true"}),
    "feed_title": frozenset({"contains_any"}),
    "text": frozenset({"keyword_any"}),
    "title": frozenset({"keyword_any"}),
    "actor": frozenset({"any_of"}),
    "actor_nation": frozenset({"in"}),
}

# PIR 述語で使える property id (検証・UI authoring の値域)。
KNOWN_PROPERTIES: frozenset[str] = frozenset(LEAF_OPS)


def _lower_value(kind: str, value: Any) -> Any:  # noqa: ANN401
    """str 述語の値を小文字化 (ArticleFacts 側も小文字正規化済みのため対称にする)。"""
    if kind != "str":
        return value
    if isinstance(value, list):
        return [str(v).strip().lower() for v in value]
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _contains_any(actual: object, value: object) -> bool:
    """actual (str) が value のいずれかを substring として含むか。

    完全名フィールド (feed_title = "CISA Advisories" 等) を短い識別子 ("cisa") で
    照合するための演算子。routing の _apply_op には無いため signal_match ローカルで持つ。
    """
    haystack = str(actual or "")
    needles = value if isinstance(value, list) else [value]
    return any(str(n) in haystack for n in needles if n)


def _keyword_any(text: str, value: object) -> bool:
    """text (小文字済) が keyword のいずれかを語境界照合で含むか (keyword_match.py 再利用)。

    ASCII は語境界 (OT→not/protocol 誤爆防止)、非 ASCII (日本語) は substring。
    keyword は意味 clause と AND した弱補強に限る (単独 sole-match は避ける)。
    """
    needles = value if isinstance(value, list) else [value]
    return any(keyword_in_text(str(n), text) for n in needles if n)


def _subject_gated(facts: ArticleFacts) -> bool:
    """主題アクターゲートが有効か (評価済み行 かつ SUBJECT_ACTOR_GATE 有効)。"""
    from src.pir.evaluator import _subject_gate_enabled

    return bool(facts.subject_source) and _subject_gate_enabled()


def _match_actor_leaf(facts: ArticleFacts, value: object) -> tuple[bool, frozenset[str]]:
    """actor any_of leaf — legacy `_row_match_signals` の actors 分岐と同一意味論。

    subject 評価済み行 = 主題 id 照合 (+辞書外名はタイトル語境界 fallback)、
    legacy 行 = text/entity 照合を bug-for-bug 温存
    (docs/subject_actor_attribution_design.md、authoring 統一 §3.1)。
    evaluator のヘルパを遅延 import で再利用 (patch-target を動かさない)。
    """
    names = tuple(str(n) for n in (value if isinstance(value, list) else [value]) if n)
    if not names:
        return False, frozenset()
    if _subject_gated(facts):
        from src.pir.evaluator import _resolve_pir_actor_names

        pir_ids, unresolved = _resolve_pir_actor_names(names)
        if facts.subject_ids & pir_ids:
            return True, frozenset({"actor", f"subject:{facts.subject_source}"})
        if unresolved:
            from src.cti.actor_normalizer import _alias_pattern

            hit = any(
                n in facts.title_text and _alias_pattern(n).search(facts.title_text)
                for n in unresolved
            )
            if hit:
                return True, frozenset({"actor", "subject:title"})
        return False, frozenset()
    names_lower = {n.lower() for n in names}
    hit = any(n in facts.text for n in names_lower) or bool(facts.actor_values & names_lower)
    return hit, (frozenset({"actor"}) if hit else frozenset())


def _match_actor_nation_leaf(facts: ArticleFacts, value: object) -> tuple[bool, frozenset[str]]:
    """actor_nation in leaf — legacy actor_nations 分岐と同一意味論 (国家系のみの map)。"""
    targets = {str(n).lower() for n in (value if isinstance(value, list) else [value]) if n}
    if not targets:
        return False, frozenset()
    if _subject_gated(facts):
        if facts.subject_ids:
            from src.pir.evaluator import _actor_nation_by_id

            nation_by_id = _actor_nation_by_id()
            if any(nation_by_id.get(s) in targets for s in facts.subject_ids):
                return True, frozenset({"actor_nation", f"subject:{facts.subject_source}"})
        return False, frozenset()
    if facts.actor_values:
        from src.pir.evaluator import _actor_nation_by_name

        nation_by_name = _actor_nation_by_name()
        if any(nation_by_name.get(v) in targets for v in facts.actor_values):
            return True, frozenset({"actor_nation"})
    return False, frozenset()


def _eval_leaf(node: dict[str, Any], facts: ArticleFacts) -> tuple[bool, frozenset[str]]:
    op = str(node.get("op", ""))
    prop_id = str(node.get("property", ""))
    # keyword_any の照合スコープは property で選ぶ: "title" = タイトルのみ
    # (タイトルに載る = 主題の決定論近似。body 言及の氾濫を避ける)、それ以外 = 全文。
    if op == "keyword_any":
        haystack = facts.title_text if prop_id == "title" else facts.text
        hit = _keyword_any(haystack, node.get("value"))
        return hit, (frozenset({"keyword"}) if hit else frozenset())
    # 主題アクターゲート leaf (Tier 1 の移植、authoring 統一 §3.1)
    if prop_id == "actor" and op == "any_of":
        return _match_actor_leaf(facts, node.get("value"))
    if prop_id == "actor_nation":
        return _match_actor_nation_leaf(facts, node.get("value"))
    spec = _PROPS.get(prop_id)
    if spec is None:
        return False, frozenset()
    kind, accessor = spec
    value = _lower_value(kind, node.get("value"))
    if op == "contains_any":
        result = _contains_any(accessor(facts), value)
    else:
        result = _apply_op(kind, accessor(facts), op, value, _SQ)
    return result, (frozenset({prop_id}) if result else frozenset())


def validate_match_tree(node: Any) -> tuple[list[str], list[str]]:  # noqa: ANN401
    """match ツリーの構造検証。返り値 = (errors, warnings)。

    errors = 保存を拒否すべき構造不正 (未知 combinator / 未知 property / 不許可 op /
    value 欠落)。warnings = 保存は許すが利用者に提示すべき懸念 (統制語彙外の category 値)。
    evaluate_match は不明構造を静かに False に倒す (fail-safe) ため、authoring 経路では
    この検証で「静かな no-match」を事前に露見させる (authoring 統一 §3.2)。
    """
    errors: list[str] = []
    warnings: list[str] = []
    _validate_node(node, errors, warnings, path="match")
    return errors, warnings


def _validate_node(node: Any, errors: list[str], warnings: list[str], *, path: str) -> None:  # noqa: ANN401
    if not isinstance(node, dict):
        errors.append(f"{path}: ノードは object である必要があります")
        return
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        children = node.get(key)
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{key}: 空でない配列が必要です")
            return
        for i, child in enumerate(children):
            _validate_node(child, errors, warnings, path=f"{path}.{key}[{i}]")
        return
    if "not" in node:
        _validate_node(node.get("not"), errors, warnings, path=f"{path}.not")
        return
    if "always" in node:
        if not isinstance(node.get("always"), bool):
            errors.append(f"{path}.always: bool が必要です")
        return
    if "property" not in node:
        errors.append(f"{path}: all/any/not/always/leaf のいずれでもありません")
        return
    prop_id = str(node.get("property", ""))
    op = str(node.get("op", ""))
    allowed = LEAF_OPS.get(prop_id)
    if allowed is None:
        errors.append(f"{path}: 未知の property '{prop_id}'")
        return
    if op not in allowed:
        ops = "/".join(sorted(allowed))
        errors.append(f"{path}: property '{prop_id}' に op '{op}' は使えません ({ops})")
        return
    value = node.get("value")
    if op != "is_true":
        values = value if isinstance(value, list) else [value]
        if value is None or not [v for v in values if v not in (None, "")]:
            errors.append(f"{path}: op '{op}' には value が必要です")
            return
        if prop_id == "category":
            try:
                from src.config_loader import KNOWN_ARTICLE_CATEGORIES

                unknown = [
                    str(v) for v in values if str(v).strip().lower() not in KNOWN_ARTICLE_CATEGORIES
                ]
                if unknown:
                    warnings.append(f"{path}: 統制語彙にない category 値 {unknown[:3]}")
            except Exception:  # noqa: BLE001,S110 — 語彙ロード失敗で検証全体を殺さない
                pass


def tree_uses_only_keywords(node: Any) -> bool:  # noqa: ANN401
    """ツリーの leaf が keyword_any だけか (生キーワード単独 PIR の警告判定用)。

    意味プロパティ clause を 1 つも持たない PIR は、llm_judge 無効なら
    「具体的固有名詞のみ推奨」の警告対象 (signal-first 設計原則 1)。
    """
    if not isinstance(node, dict):
        return False
    if "all" in node or "any" in node:
        children = node.get("all" if "all" in node else "any") or []
        return all(tree_uses_only_keywords(c) for c in children) if children else False
    if "not" in node:
        # NOT 単独は意味 clause とみなさない (除外だけでは対象を規定しない)
        return tree_uses_only_keywords(node.get("not"))
    if "always" in node:
        return False
    return str(node.get("op", "")) == "keyword_any"


def evaluate_match(node: Any, facts: ArticleFacts) -> tuple[bool, frozenset[str]]:  # noqa: ANN401
    """条件ツリーを評価。返り値 = (満たすか, 発火した property id 集合)。

    不明な構造は (False, {}) で fail-safe。発火集合は「なぜこの PIR に該当したか」の
    provenance (matched_via 表示) に使う。
    """
    if not isinstance(node, dict):
        return False, frozenset()
    if "all" in node:
        children = [evaluate_match(c, facts) for c in node.get("all", [])]
        ok = all(res for res, _ in children)
        fired = frozenset().union(*[f for _, f in children]) if children else frozenset()
        return ok, (fired if ok else frozenset())
    if "any" in node:
        children = [evaluate_match(c, facts) for c in node.get("any", [])]
        ok = any(res for res, _ in children)
        fired = frozenset().union(*[f for res, f in children if res]) if ok else frozenset()
        return ok, fired
    if "not" in node:
        res, _ = evaluate_match(node.get("not"), facts)
        return (not res), frozenset()
    if "always" in node:
        return bool(node.get("always")), frozenset()
    if "property" in node:
        return _eval_leaf(node, facts)
    return False, frozenset()
