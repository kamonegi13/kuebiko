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
        rules = [_rule("R3", {"not": {"property": "keyword_list", "op": "in", "value": ["noise"]}})]

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


class TestLegacyConditionForm:
    """⚠ 実データで発覚 (2026-09-16): 本番ルールは**旧形**で参照していた。

        {"keyword_list": {"in": ["early_warning"]}}     ← 旧形 (本番の実体)
        {"property": "keyword_list", "op": "in", ...}    ← 新形

    新形だけを見ていたため、実際には参照されている 2 リストが「未参照」と表示された。
    **評価器 (`_eval_condition`) は両形を扱う** — 参照検出だけ別実装にしたのが誤りで、
    同じ正規化を通すこと (判定が 2 箇所に分かれると必ずずれる)。
    """

    def test_legacy_leaf_is_detected(self) -> None:
        rules = [_rule("R2f", {"keyword_list": {"in": ["early_warning"]}})]

        assert match_list_usage(["early_warning"], rules) == {"early_warning": ["R2f"]}

    def test_legacy_leaf_nested_in_all(self) -> None:
        """本番の実体そのままの形。"""
        rules = [
            _rule(
                "R2g",
                {
                    "all": [
                        {"article_type": {"not_in": ["recap", "tutorial"]}},
                        {"keyword_list": {"in": ["emergency_directives"]}},
                    ]
                },
            )
        ]

        assert match_list_usage(["emergency_directives"], rules) == {
            "emergency_directives": ["R2g"]
        }

    def test_both_forms_coexist(self) -> None:
        rules = [
            _rule("R_old", {"keyword_list": {"in": ["x"]}}),
            _rule("R_new", {"property": "keyword_list", "op": "in", "value": ["x"]}),
        ]

        assert match_list_usage(["x"], rules) == {"x": ["R_new", "R_old"]}


class TestSirUsage:
    """SIR の参照関係 (S4) — **常設情報要求 (PIR) がどの SIR を参照しているか**。

    SIR を消すと、参照していた問いの SIR リンクが孤児になる。編集画面で
    「この SIR は N 件の問いから参照されている」が見えることが移設の前提条件
    (docs/settings_consolidation_plan.md §4)。

    ⚠ 配信ルールは SIR を参照しない (R0 撤去済、CLAUDE.md §13 設計原則 2)。
    参照元は**常設情報要求**であって routing ではない。
    """

    def _row(self, sid: str, pir_ids: list[str]) -> dict[str, Any]:
        return {"situation_id": sid, "title": f"問い {sid}", "pir_ids": pir_ids}

    def test_maps_sir_to_referencing_questions(self) -> None:
        from src.cti.definition_usage import sir_usage

        rows = [
            self._row("s-a", ["pir_critical_infra", "pir_russia_apt"]),
            self._row("s-b", ["pir_critical_infra"]),
        ]

        usage = sir_usage(["pir_critical_infra", "pir_russia_apt"], rows)

        assert usage["pir_critical_infra"] == ["s-a", "s-b"]
        assert usage["pir_russia_apt"] == ["s-a"]

    def test_unreferenced_sir_maps_to_empty(self) -> None:
        from src.cti.definition_usage import sir_usage

        assert sir_usage(["pir_unused"], [self._row("s-a", ["pir_other"])]) == {"pir_unused": []}

    def test_question_without_links_is_ignored(self) -> None:
        """SIR リンクを持たない問い (閾値型など) は参照元にならない。"""
        from src.cti.definition_usage import sir_usage

        assert sir_usage(["pir_x"], [self._row("s-a", [])]) == {"pir_x": []}

    def test_json_encoded_pir_ids_are_accepted(self) -> None:
        """DB は pir_ids を JSON 文字列で持つ (situations.pir_ids は text)。"""
        from src.cti.definition_usage import sir_usage

        rows = [{"situation_id": "s-a", "pir_ids": '["pir_x"]'}]

        assert sir_usage(["pir_x"], rows) == {"pir_x": ["s-a"]}

    def test_malformed_rows_do_not_raise(self) -> None:
        from src.cti.definition_usage import sir_usage

        rows = [{"situation_id": "s-a", "pir_ids": "not json"}, {"pir_ids": ["pir_x"]}, "junk"]

        assert sir_usage(["pir_x"], rows) == {"pir_x": []}


class TestActorUsage:
    """アクター辞書の参照関係 (S7) — **SIR がアクターを名指ししている**。

    実データ (2026-09-16): 20 SIR のうち 3 件が actors を列挙している
    (pir_china_apt → Volt Typhoon / Salt Typhoon …)。canonical や別名を変えると
    この名指しが外れ、**SIR が静かに該当しなくなる**。

    ⚠ 名指しは **canonical でも別名でも**書かれうる。canonical だけを見ると
    別名で書かれた参照を見落とす。
    """

    def _pir(self, pid: str, actors: list[str]) -> dict[str, Any]:
        return {"id": pid, "strong_signals": {"actors": actors}}

    def test_matches_by_canonical_name(self) -> None:
        from src.cti.definition_usage import actor_usage

        pirs = [self._pir("pir_cn", ["Volt Typhoon"])]

        usage = actor_usage([("volt_typhoon", "Volt Typhoon", [])], pirs)

        assert usage == {"volt_typhoon": ["pir_cn"]}

    def test_matches_by_alias(self) -> None:
        """別名で名指しされていても参照である (見落とすと「未参照」と誤表示)。"""
        from src.cti.definition_usage import actor_usage

        pirs = [self._pir("pir_ru", ["Cozy Bear"])]

        usage = actor_usage([("apt29", "APT29", ["Cozy Bear", "Nobelium"])], pirs)

        assert usage == {"apt29": ["pir_ru"]}

    def test_match_is_case_insensitive(self) -> None:
        from src.cti.definition_usage import actor_usage

        pirs = [self._pir("pir_cn", ["volt typhoon"])]

        assert actor_usage([("volt_typhoon", "Volt Typhoon", [])], pirs) == {
            "volt_typhoon": ["pir_cn"]
        }

    def test_unreferenced_actor_maps_to_empty(self) -> None:
        from src.cti.definition_usage import actor_usage

        assert actor_usage([("x", "X", [])], [self._pir("pir_a", ["Y"])]) == {"x": []}

    def test_several_pirs_are_sorted(self) -> None:
        from src.cti.definition_usage import actor_usage

        pirs = [self._pir("pir_b", ["Lazarus"]), self._pir("pir_a", ["Lazarus"])]

        assert actor_usage([("lazarus", "Lazarus", [])], pirs) == {"lazarus": ["pir_a", "pir_b"]}

    def test_malformed_pirs_do_not_raise(self) -> None:
        from src.cti.definition_usage import actor_usage

        pirs = [{"id": "p1"}, {"strong_signals": None}, "junk", {"id": "p2", "strong_signals": {}}]

        assert actor_usage([("x", "X", [])], pirs) == {"x": []}

    def test_empty_actor_list_returns_empty_mapping(self) -> None:
        """⚠ 空入力で dict 以外を返さない (set と dict を取り違える書き方をしていた)。"""
        from src.cti.definition_usage import actor_usage

        result = actor_usage([], [{"id": "p1", "strong_signals": {"actors": ["X"]}}])

        assert result == {}
        assert isinstance(result, dict)
