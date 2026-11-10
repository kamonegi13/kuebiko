"""DETECT-RECALL の 4 つの旗 (既定 0 = 従来と同一、1 で入る)。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from src.assessment import detect_decompose
from src.assessment.claim_grounding import GroundingSources
from src.assessment.detect_decompose import (
    WrittenClaim,
    decompose_candidates,
    pick_merge,
    to_records,
    write_cluster_claim,
    writer_articles_v2,
    writer_call_limit,
)
from src.assessment.detect_merge_gate import MergeConflictGate
from src.assessment.detect_recall_flags import (
    RECALL_FLAG_KEYS,
    claim_salvage_enabled,
    merge_conflict_gate_enabled,
    writer_cap_refill_enabled,
    writer_input_v2_enabled,
)
from src.synthesis.grounded.passes import _render

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts" / "gpu_window"))
import detect_common as dc  # noqa: E402

_TEMPLATE = "synthesis/detect_cluster_claim.j2"
_ENABLED = (
    claim_salvage_enabled,
    writer_input_v2_enabled,
    writer_cap_refill_enabled,
    merge_conflict_gate_enabled,
)


class TestFlags:
    @pytest.mark.parametrize("raw", [None, "0", "", "shadow", "on", "true", "bogus"])
    def test_everything_but_one_is_off(
        self, monkeypatch: pytest.MonkeyPatch, raw: str | None
    ) -> None:
        for key in RECALL_FLAG_KEYS:
            if raw is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, raw)

        assert [f() for f in _ENABLED] == [False] * 4

    def test_one_turns_each_flag_on_independently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for i, key in enumerate(RECALL_FLAG_KEYS):
            for other in RECALL_FLAG_KEYS:
                monkeypatch.delenv(other, raising=False)
            monkeypatch.setenv(key, "1")

            assert [f() for f in _ENABLED] == [j == i for j in range(4)]


class TestWriterInput:
    _ARTS: list[dict[str, object]] = [
        {
            "article_id": "a1",
            "feed_title": "SomeOutlet",
            "title": "見出し",
            "importance": "high",
            "chronology": "報道 10-02 / 事象 2026-10-02(detected・再浮上)",
        },
        {"article_id": "a2", "feed_title": "", "title": "別の見出し", "importance": "medium"},
    ]

    def _ctx(self) -> dict[str, object]:
        return {
            "period_label": "P",
            "active_titles": ["追跡中A"],
            "pir_context": [{"title": "SIR1"}],
        }

    def test_flag_off_render_is_unchanged_with_the_variable_passed_explicitly(self) -> None:
        old = _render(_TEMPLATE, articles=self._ARTS, strict_rules=False, **self._ctx())
        new = _render(
            _TEMPLATE, articles=self._ARTS, strict_rules=False, input_v2=False, **self._ctx()
        )

        assert new.encode() == old.encode()

    def test_v2_drops_outlet_and_resurfaced_mark_and_adds_the_summary(self) -> None:
        arts = writer_articles_v2(self._ARTS, {"a1": " 要約の本文です。 "})

        out = _render(_TEMPLATE, articles=arts, strict_rules=True, input_v2=True, **self._ctx())

        assert "SomeOutlet" not in out
        assert "[?]" not in out
        assert "再浮上" not in out.split("【塊の記事】")[1]
        assert "要約: 要約の本文です。" in out
        assert "事象 2026-10-02(detected)" in out

    def test_v2_does_not_mutate_the_inputs_nor_truncate_the_summary(self) -> None:
        summary = "あ" * 1500

        arts = writer_articles_v2(self._ARTS, {"a1": summary})

        assert "feed_title" in self._ARTS[0]
        assert "summary" not in self._ARTS[0]
        assert arts[0]["summary"] == summary
        assert arts[1]["summary"] == ""

    @pytest.mark.asyncio
    async def test_writer_passes_the_flag_to_the_template(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class _LLM:
            model = "fake"

            async def generate_structured(self, prompt: str, schema: type, **kw: Any) -> Any:
                seen.append(prompt)
                return schema(claim="c", domain="tech")

        arts = writer_articles_v2(self._ARTS, {"a1": "要約X"})
        for value in ("0", "1"):
            monkeypatch.setenv("DETECT_WRITER_INPUT_V2", value)
            await write_cluster_claim(
                llm=_LLM(),  # type: ignore[arg-type]
                articles=arts if value == "1" else list(self._ARTS),
                active_titles=[],
                pir_context=[],
                period_label="P",
            )

        assert "SomeOutlet" in seen[0]
        assert "要約:" not in seen[0]
        assert "SomeOutlet" not in seen[1]
        assert "要約: 要約X" in seen[1]


def _cluster_args(ids: list[str]) -> dict[str, Any]:
    return {
        "candidate_ids": ids,
        "ml_scores": {a: 1.0 - i / 100 for i, a in enumerate(ids)},
        "strong_by_aid": {},
        "items_by_aid": {},
        "vec_by_aid": None,
        "merge_for": lambda c: None,
    }


class TestWriterCapRefill:
    def test_limit_is_unchanged_when_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DETECT_WRITER_CAP_REFILL", raising=False)

        assert writer_call_limit(8, 5) == 8

    @pytest.mark.parametrize(("wasted", "want"), [(0, 8), (1, 9), (3, 11), (7, 11)])
    def test_limit_grows_by_the_wasted_calls_up_to_three(
        self, monkeypatch: pytest.MonkeyPatch, wasted: int, want: int
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_CAP_REFILL", "1")

        assert writer_call_limit(8, wasted) == want

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("flag", "calls", "opened"), [("0", 2, 1), ("1", 3, 2)])
    async def test_a_blocked_writer_call_is_refilled_by_the_next_cluster(
        self, monkeypatch: pytest.MonkeyPatch, flag: str, calls: int, opened: int
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_CAP_REFILL", flag)
        called: list[str] = []

        async def write(cluster: Any) -> WrittenClaim | None:
            called.append(cluster[0])
            if cluster[0] == "a":
                return WrittenClaim("", "", "追跡中と同一")  # 無駄になる回
            return WrittenClaim(f"claim {cluster[0]}", "cyber_incident", "")

        res = await decompose_candidates(
            **_cluster_args(["a", "b", "c", "d"]), write_claim=write, max_claims=2
        )

        assert len(called) == calls
        assert len(res.opened()) == opened
        assert len(res.opened()) <= 2  # 開設の上限は変えない

    @pytest.mark.asyncio
    async def test_open_cap_holds_even_when_every_call_opens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_CAP_REFILL", "1")

        async def write(cluster: Any) -> WrittenClaim | None:
            return WrittenClaim(f"claim {cluster[0]}", "cyber_incident", "")

        res = await decompose_candidates(
            **_cluster_args(list("abcdef")), write_claim=write, max_claims=2
        )

        assert len(res.opened()) == 2
        assert res.overflow == 4

    @pytest.mark.asyncio
    async def test_refill_is_capped_at_three_extra_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_CAP_REFILL", "1")
        called: list[str] = []

        async def write(cluster: Any) -> WrittenClaim | None:
            called.append(cluster[0])
            return WrittenClaim("", "", "評価対象なし")

        res = await decompose_candidates(
            **_cluster_args(list("abcdefghij")), write_claim=write, max_claims=2
        )

        assert len(called) == 2 + 3
        assert res.overflow == 5


_SOURCES = GroundingSources(
    text="ホテルが不正アクセスを受け顧客情報が流出したと報じられている",
    members=(("ホテルが不正アクセスを受け顧客情報が流出したと報じられている", ""),),
)
_TWO_SENTENCES = (
    "ホテルが不正アクセスを受け顧客情報が流出したと報じられている。事象は2026年9月9日に検知された。"
)


async def _decompose_two_sentence_claim(*, enforce: bool) -> Any:
    async def write(cluster: Any) -> WrittenClaim | None:
        return WrittenClaim(_TWO_SENTENCES, "cyber_incident", "")

    return await decompose_candidates(
        **_cluster_args(["a"]),
        write_claim=write,
        grounding_text=lambda c: _SOURCES,
        enforce_grounding=enforce,
    )


class TestSalvageWiring:
    @pytest.mark.asyncio
    async def test_flag_off_still_blocks_the_unfaithful_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        monkeypatch.delenv("DETECT_CLAIM_SALVAGE", raising=False)

        res = await _decompose_two_sentence_claim(enforce=True)

        assert res.outcomes[0].action == "skip"
        assert res.outcomes[0].reason.startswith("claim が根拠どおりでない")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enforce", [True, False])
    async def test_flag_on_opens_the_salvaged_claim_and_records_the_cut(
        self, monkeypatch: pytest.MonkeyPatch, enforce: bool
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        monkeypatch.setenv("DETECT_CLAIM_SALVAGE", "1")

        res = await _decompose_two_sentence_claim(enforce=enforce)

        out = res.outcomes[0]
        assert out.action == "open"
        assert out.claim == "ホテルが不正アクセスを受け顧客情報が流出したと報じられている。"
        assert out.salvaged == ("second_sentence",)
        assert out.fidelity == ("multi_sentence", "date:9月9日")
        variant = "decompose" if enforce else "decompose_shadow"
        rec = to_records(res, run_at="2026-11-10T00:00:00+00:00", variant=variant)[0]  # type: ignore[arg-type]
        assert "salvaged: second_sentence" in rec.reason

    @pytest.mark.asyncio
    async def test_gate_off_or_shadow_never_salvages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECT_CLAIM_SALVAGE", "1")
        for gates in ("0", "shadow"):
            monkeypatch.setenv("DETECT_WRITER_GATES", gates)

            res = await _decompose_two_sentence_claim(enforce=True)

            assert res.outcomes[0].salvaged == ()
            assert res.outcomes[0].action == "open"
            assert res.outcomes[0].claim == _TWO_SENTENCES

    @pytest.mark.asyncio
    async def test_unsalvageable_claim_is_still_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DETECT_WRITER_GATES", "1")
        monkeypatch.setenv("DETECT_CLAIM_SALVAGE", "1")

        async def write(cluster: Any) -> WrittenClaim | None:
            return WrittenClaim("決済に不正アクセスがあった事案が再浮上した。", "tech", "")

        res = await decompose_candidates(
            **_cluster_args(["a"]),
            write_claim=write,
            grounding_text=lambda c: GroundingSources(text="決済に不正アクセスがあった"),
            enforce_grounding=True,
        )

        assert res.outcomes[0].action == "skip"


class TestMergeConflictGate:
    def _gate(
        self,
        entities: dict[str, set[str]],
        anchors: frozenset[str],
        title: str = "",
        alias: dict[str, str] | None = None,
    ) -> MergeConflictGate:
        return MergeConflictGate(
            entities_by_id=entities,
            anchors_by_sid={"s1": anchors},
            titles_by_sid={"s1": title},
            org_alias=alias or {},
        )

    def test_disjoint_cves_on_both_sides_conflict(self) -> None:
        gate = self._gate({"a": {"cve:CVE-2026-0001"}}, frozenset({"cve:CVE-2026-0002"}))

        assert gate.conflict(["a"], "s1") == "cve"

    def test_overlapping_cves_do_not_conflict(self) -> None:
        gate = self._gate(
            {"a": {"cve:CVE-2026-0001"}, "b": {"cve:CVE-2026-0003"}},
            frozenset({"cve:CVE-2026-0003", "cve:CVE-2026-0009"}),
        )

        assert gate.conflict(["a", "b"], "s1") is None

    def test_cve_in_the_situation_title_counts(self) -> None:
        gate = self._gate({"a": {"cve:CVE-2026-0001"}}, frozenset(), "製品Xの CVE-2026-7777 を悪用")

        assert gate.conflict(["a"], "s1") == "cve"

    def test_cve_on_one_side_only_does_not_conflict(self) -> None:
        assert self._gate({"a": {"cve:CVE-2026-0001"}}, frozenset()).conflict(["a"], "s1") is None
        assert (
            self._gate({"a": set()}, frozenset({"cve:CVE-2026-0002"})).conflict(["a"], "s1") is None
        )

    def test_different_named_victims_conflict(self) -> None:
        gate = self._gate({"a": {"victim_org:Foo Corp"}}, frozenset({"victim_org:Bar Inc"}))

        assert gate.conflict(["a"], "s1") == "victim"

    def test_victim_spelling_variants_are_not_a_conflict(self) -> None:
        gate = self._gate({"a": {"victim_org:Foo"}}, frozenset({"victim_org:Foo株式会社"}))

        assert gate.conflict(["a"], "s1") is None

    def test_org_alias_same_pair_is_not_a_conflict(self) -> None:
        from src.cti.org_normalizer import org_match_key

        short = org_match_key("Foo")
        long = org_match_key("Foo Holdings Japan")
        gate = self._gate(
            {"a": {"victim_org:Foo"}},
            frozenset({"victim_org:Foo Holdings Japan"}),
            alias={short: min(short, long), long: min(short, long)},
        )

        assert gate.conflict(["a"], "s1") is None

    def test_victim_on_one_side_only_does_not_conflict(self) -> None:
        gate = self._gate({"a": {"victim_org:Foo"}}, frozenset({"actor:APT29"}))

        assert gate.conflict(["a"], "s1") is None

    def test_allowed_filters_the_conflicting_situations_in_order(self) -> None:
        gate = MergeConflictGate(
            entities_by_id={"a": {"cve:CVE-2026-0001"}},
            anchors_by_sid={
                "s1": frozenset({"cve:CVE-2026-0002"}),
                "s2": frozenset({"cve:CVE-2026-0001"}),
            },
            titles_by_sid={},
        )

        assert gate.allowed(["a"], ["s1", "s2"]) == ["s2"]

    def test_blocked_best_falls_back_to_the_next_allowed_situation(self) -> None:
        scores = {("s1", "a"): (True, 0.9), ("s2", "a"): (True, 0.6)}
        gate = MergeConflictGate(
            entities_by_id={"a": {"cve:CVE-2026-0001"}},
            anchors_by_sid={"s1": frozenset({"cve:CVE-2026-0002"}), "s2": frozenset()},
            titles_by_sid={},
        )

        best = pick_merge(["a"], scores, ["s1", "s2"])
        fallback = pick_merge(["a"], scores, gate.allowed(["a"], ["s1", "s2"]))

        assert best is not None and best.situation_id == "s1"
        assert fallback is not None and fallback.situation_id == "s2"


class TestArmFlags:
    _ARMS = ["current", "ship"]

    def test_parse_accepts_a_per_arm_assignment(self) -> None:
        got = dc.parse_arm_flags(
            "ship:DETECT_CLAIM_SALVAGE=1, ship:DETECT_MERGE_CONFLICT_GATE=1", self._ARMS
        )

        assert got == {"ship": {"DETECT_CLAIM_SALVAGE": "1", "DETECT_MERGE_CONFLICT_GATE": "1"}}

    @pytest.mark.parametrize(
        "raw",
        [
            "ship:UNKNOWN=1",
            "other:DETECT_CLAIM_SALVAGE=1",
            "ship:DETECT_CLAIM_SALVAGE=2",
            "ship",
            "DETECT_CLAIM_SALVAGE=1",
        ],
    )
    def test_parse_rejects_unknown_arm_flag_or_value(self, raw: str) -> None:
        with pytest.raises(SystemExit):
            dc.parse_arm_flags(raw, self._ARMS)

    def test_empty_means_no_flags_are_set_by_the_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DETECT_ARM_FLAGS", raising=False)

        for arm in dc.ARMS:
            assert not set(dc.arm_env(arm)) & set(dc.ARM_FLAG_KEYS)

    def test_every_arm_gets_every_flag_explicitly_so_nothing_leaks_between_arms(self) -> None:
        flags = {"ship": {"DETECT_CLAIM_SALVAGE": "1"}}

        by_arm = {arm.name: dc.arm_env(arm, arm_flags=flags) for arm in dc.ARMS}

        assert by_arm["ship"]["DETECT_CLAIM_SALVAGE"] == "1"
        assert by_arm["ship"]["DETECT_MERGE_CONFLICT_GATE"] == "0"
        assert all(by_arm["current"][k] == "0" for k in dc.ARM_FLAG_KEYS)

    def test_flag_names_match_the_source_of_truth(self) -> None:
        assert set(dc.ARM_FLAG_KEYS) == set(RECALL_FLAG_KEYS)

    def test_the_recall_flags_are_exempt_from_the_prod_flag_comparison(self) -> None:
        prod = {"DETECT_CLAIM_SALVAGE": ""}
        local = {"DETECT_CLAIM_SALVAGE": "1"}

        assert dc.flag_mismatches(prod, local) == []


def test_detect_decompose_exposes_no_new_default_on_behaviour() -> None:
    assert not any(
        getattr(detect_decompose, name, False) is True
        for name in ("SALVAGE_DEFAULT", "REFILL_DEFAULT")
    )
