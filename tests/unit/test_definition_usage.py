"""定義の参照関係 — 「この定義を誰が使っているか」(S2)。

設計: docs/settings_consolidation_plan.md §4。

**なぜ移設より先か**: 旧配置基準の根拠は「対象を見ながら直せる」だったのに、
マッチリスト編集画面は**配信ルールを表示していなかった** — 理屈だけで実装が伴って
いなかった。参照関係を持たないまま設定へ移すと、同じ「理屈だけ」の状態を設定側に
再現することになる。

⚠ **未参照を「削除してよい」と読ませない**。参照ゼロは「いま使われていない」だけで、
これから使う下書きかもしれない。事実だけを返す。
"""

from __future__ import annotations

from typing import Any

from src.cti.definition_usage import match_list_usage


def _rule(rule_id: str, when: dict[str, Any]) -> dict[str, Any]:
    return {"id": rule_id, "when": when, "then": {"channel": "daily"}}


class TestMatchListUsage:
    def test_finds_a_rule_that_references_the_list(self) -> None:
        rules = [_rule("R1", {"property": "keyword_list", "op": "in", "value": ["early_warning"]})]

        assert match_list_usage(["early_warning"], rules) == {"early_warning": ["R1"]}

    def test_finds_references_nested_in_combinators(self) -> None:
        """入れ子の奥の参照を見落とさない (見落とすと「未参照」と誤表示する)。"""
        rules = [
            _rule(
                "R2",
                {
                    "all": [
                        {"property": "importance", "op": "eq", "value": "high"},
                        {"any": [{"property": "keyword_list", "op": "in", "value": ["emerg"]}]},
                    ]
                },
            )
        ]

        assert match_list_usage(["emerg"], rules) == {"emerg": ["R2"]}

    def test_finds_references_under_not(self) -> None:
        """否定の中の参照も参照である (消したらルールの意味が変わる)。"""
        rules = [
            _rule("R3", {"not": {"property": "keyword_list", "op": "in", "value": ["noise"]}})
        ]

        assert match_list_usage(["noise"], rules) == {"noise": ["R3"]}

    def test_unreferenced_list_maps_to_empty_not_missing(self) -> None:
        """未参照も**キーとして返す** — 欠落と区別できないと画面が何も出せない。"""
        rules = [_rule("R1", {"property": "importance", "op": "eq", "value": "high"})]

        assert match_list_usage(["unused"], rules) == {"unused": []}

    def test_one_rule_referencing_several_lists(self) -> None:
        rules = [
            _rule("R1", {"property": "keyword_list", "op": "in", "value": ["a", "b"]}),
        ]

        assert match_list_usage(["a", "b"], rules) == {"a": ["R1"], "b": ["R1"]}

    def test_rule_ids_are_deduplicated_and_ordered(self) -> None:
        """同じルールが 2 度参照しても 1 回。順序は決定論 (差分を読めるように)。"""
        rules = [
            _rule(
                "R2",
                {
                    "any": [
                        {"property": "keyword_list", "op": "in", "value": ["x"]},
                        {"property": "keyword_list", "op": "not_in", "value": ["x"]},
                    ]
                },
            ),
            _rule("R1", {"property": "keyword_list", "op": "in", "value": ["x"]}),
        ]

        assert match_list_usage(["x"], rules) == {"x": ["R1", "R2"]}

    def test_malformed_rules_do_not_raise(self) -> None:
        """壊れたルールで編集画面を落とさない (保存前の下書きが来る経路がある)。"""
        assert match_list_usage(["a"], [{"id": "R1"}, {"when": None}, "junk"]) == {"a": []}
