"""組織名の別名判定 (org_identity) の純粋部分。判定役 (LLM) と DB は差し替えて確かめる。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.config_loader import AppConfig
from src.eventnews import org_identity as oi
from src.eventnews.grouping import blocked_by_different_victims, names_of_type
from src.eventnews.models import ItemState, MemberArticle


class TestMode:
    def test_default_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(oi.ORG_IDENTITY_ENV, raising=False)
        assert oi.org_identity_mode() == "off"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0", "off"), ("", "off"), ("shadow", "shadow"), ("1", "on"), ("bogus", "off")],
    )
    def test_parses_env(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
        monkeypatch.setenv(oi.ORG_IDENTITY_ENV, raw)
        assert oi.org_identity_mode() == expected


class TestAliasMap:
    def test_pairs_become_one_canonical_key(self) -> None:
        alias = oi.build_alias_map([("timescar", "タイムズカー")])
        assert alias.canonical("timescar") == alias.canonical("タイムズカー")

    def test_is_transitive(self) -> None:
        alias = oi.build_alias_map([("a1", "b1"), ("b1", "c1")])
        assert len({alias.canonical(k) for k in ("a1", "b1", "c1")}) == 1

    def test_unknown_key_is_unchanged(self) -> None:
        assert oi.build_alias_map([("a1", "b1")]).canonical("zzz") == "zzz"

    def test_representative_is_deterministic(self) -> None:
        one = oi.build_alias_map([("bb", "aa"), ("cc", "bb")])
        two = oi.build_alias_map([("cc", "bb"), ("aa", "bb")])
        assert one.canonical("cc") == two.canonical("cc") == "aa"

    def test_same_key_pair_is_ignored(self) -> None:
        assert not oi.build_alias_map([("aa", "aa")])


class TestRemap:
    def test_only_victim_org_keys_are_replaced(self) -> None:
        alias = oi.build_alias_map([("bb", "aa")])
        entities = frozenset({("victim_org", "bb"), ("cve", "bb"), ("actor", "bb")})
        out = oi.remap_entities(entities, alias)
        assert out == frozenset({("victim_org", "aa"), ("cve", "bb"), ("actor", "bb")})

    def test_empty_alias_returns_same_object(self) -> None:
        entities = frozenset({("victim_org", "bb")})
        assert oi.remap_entities(entities, oi.build_alias_map([])) is entities


class TestCandidateNamePairs:
    def test_excludes_pairs_that_already_share_a_match_key(self) -> None:
        pairs = oi.candidate_name_pairs(["株式会社Foo"], ["Foo"], limit=5)
        assert pairs == []

    def test_orders_similar_names_first_and_caps(self) -> None:
        pairs = oi.candidate_name_pairs(
            ["Alpha Corp", "Zeta"], ["Alphabet Corp", "Qux", "Zeta Holdings2"], limit=2
        )
        assert len(pairs) == 2
        assert pairs[0] == ("Alpha Corp", "Alphabet Corp")

    def test_pair_key_is_order_free(self) -> None:
        assert oi.pair_key("Foo Inc", "ばー") == oi.pair_key("ばー", "Foo Inc")


class TestQuestionAndContext:
    def test_context_sentence_picks_sentence_with_name(self) -> None:
        text = "前置き。タイムズカーで漏えいが発生した。後続。"
        assert "タイムズカー" in oi.context_sentence("t", text, "", "タイムズカー")

    def test_context_sentence_falls_back_to_title(self) -> None:
        assert oi.context_sentence("見出し", "別の話。", "", "Nothing") == "見出し"

    def test_context_sentence_skips_name_only_stub_line(self) -> None:
        body = "GMOリサーチ&AI株式会社\nGMOリサーチ&AIは不正アクセスによる漏えいを公表した。"
        ctx = oi.context_sentence("見出し", body, "", "GMOリサーチ&AI")
        assert "不正アクセス" in ctx

    def test_context_sentence_uses_stub_when_nothing_better(self) -> None:
        assert oi.context_sentence("見出し", "Foo社", "", "Foo社") == "Foo社"

    @pytest.mark.parametrize("build", [oi.same_event_question, oi.same_org_question])
    def test_questions_warn_that_similar_context_is_not_identity(self, build: object) -> None:
        q = build("A社", "a", "B社", "b")  # type: ignore[operator]
        assert "別々の被害者" in q and "似た種類の被害" in q

    @pytest.mark.parametrize("build", [oi.same_event_question, oi.same_org_question])
    def test_questions_mention_both_names_and_contexts(self, build: object) -> None:
        q = build("A社", "A社が被害", "B社", "B社が被害")  # type: ignore[operator]
        assert all(s in q for s in ("A社", "A社が被害", "B社", "B社が被害"))

    def test_event_question_includes_parent_and_operator(self) -> None:
        assert "親会社" in oi.same_event_question("A", "a", "B", "b")

    def test_org_question_excludes_similar_but_different_entities(self) -> None:
        assert "別の組織" in oi.same_org_question("A", "a", "B", "b")


class _Store:
    def __init__(self, known: dict[tuple[str, str], float] | None = None) -> None:
        self.known = dict(known or {})
        self.puts: dict[tuple[str, str], float] = {}

    def get_many(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
        return {k: self.known[k] for k in keys if k in self.known}

    def put_many(self, rows: dict[tuple[str, str], float]) -> None:
        self.puts.update(rows)


def _task(a: str, b: str) -> oi.NamePairTask:
    return oi.NamePairTask(name_a=a, ctx_a=a, name_b=b, ctx_b=b)


class TestResolve:
    @pytest.mark.asyncio
    async def test_judges_only_uncached_pairs_and_stores_them(self) -> None:
        cached = oi.pair_key("Foo Inc", "ふー")
        store = _Store({cached: 14.0})
        calls: list[str] = []

        async def judge(task: oi.NamePairTask) -> float | None:
            calls.append(task.name_a)
            return -9.0

        same, stats = await oi.resolve_same_pairs(
            [_task("Foo Inc", "ふー"), _task("Bar Ltd", "ばー")], store, judge, max_judgments=10
        )
        assert calls == ["Bar Ltd"]
        assert same == [cached]
        assert store.puts == {oi.pair_key("Bar Ltd", "ばー"): -9.0}
        assert (stats.judged, stats.cache_hits, stats.deferred) == (1, 1, 0)

    @pytest.mark.asyncio
    async def test_judgment_above_threshold_is_same(self) -> None:
        async def judge(task: oi.NamePairTask) -> float | None:
            return oi.SAME_MARGIN

        same, _ = await oi.resolve_same_pairs(
            [_task("Foo", "ふー")], _Store(), judge, max_judgments=5
        )
        assert same == [oi.pair_key("Foo", "ふー")]

    @pytest.mark.asyncio
    async def test_failed_judgment_is_not_stored_nor_same(self) -> None:
        async def judge(task: oi.NamePairTask) -> float | None:
            return None

        store = _Store()
        same, stats = await oi.resolve_same_pairs(
            [_task("Foo", "ふー")], store, judge, max_judgments=5
        )
        assert same == [] and store.puts == {} and stats.failed == 1

    @pytest.mark.asyncio
    async def test_budget_defers_the_rest(self) -> None:
        async def judge(task: oi.NamePairTask) -> float | None:
            return -9.0

        tasks = [_task(f"Name{i}", f"ばー{i}") for i in range(5)]
        _, stats = await oi.resolve_same_pairs(tasks, _Store(), judge, max_judgments=2)
        assert (stats.judged, stats.deferred) == (2, 3)

    @pytest.mark.asyncio
    async def test_duplicate_tasks_judge_once(self) -> None:
        calls = 0

        async def judge(task: oi.NamePairTask) -> float | None:
            nonlocal calls
            calls += 1
            return -9.0

        await oi.resolve_same_pairs(
            [_task("Foo", "ふー"), _task("ふー", "Foo")], _Store(), judge, max_judgments=5
        )
        assert calls == 1


def _ctx_task(ctx: str) -> oi.NamePairTask:
    return oi.NamePairTask(name_a="Foo", ctx_a=ctx, name_b="ふー", ctx_b=ctx)


class TestMultiContext:
    """判定は文脈で振れるので、名前の組ごとに複数の文脈を見て過半数で決める。"""

    @staticmethod
    def _judge_by_ctx(values: dict[str, float | None], calls: list[str]) -> oi.Judge:
        async def judge(task: oi.NamePairTask) -> float | None:
            calls.append(task.ctx_a)
            return values[task.ctx_a]

        return judge

    @pytest.mark.asyncio
    async def test_first_context_alone_cannot_make_a_pair_same(self) -> None:
        calls: list[str] = []
        judge = self._judge_by_ctx({"c1": 3.0, "c2": -27.0, "c3": -27.0}, calls)
        store = _Store()
        same, _ = await oi.resolve_same_pairs(
            [_ctx_task("c1"), _ctx_task("c2"), _ctx_task("c3")], store, judge
        )
        assert same == [] and calls == ["c1", "c2", "c3"]
        assert next(iter(store.puts.values())) < oi.SAME_MARGIN

    @pytest.mark.asyncio
    async def test_majority_of_contexts_makes_pair_same(self) -> None:
        calls: list[str] = []
        judge = self._judge_by_ctx({"c1": -2.0, "c2": 3.0, "c3": 3.0}, calls)
        same, _ = await oi.resolve_same_pairs(
            [_ctx_task("c1"), _ctx_task("c2"), _ctx_task("c3")], _Store(), judge
        )
        assert same == [oi.pair_key("Foo", "ふー")]

    @pytest.mark.asyncio
    async def test_stops_early_when_outcome_is_decided(self) -> None:
        calls: list[str] = []
        judge = self._judge_by_ctx({"c1": -9.0, "c2": -9.0, "c3": 3.0}, calls)
        await oi.resolve_same_pairs(
            [_ctx_task("c1"), _ctx_task("c2"), _ctx_task("c3")], _Store(), judge
        )
        assert calls == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_two_contexts_need_both_to_pass(self) -> None:
        calls: list[str] = []
        judge = self._judge_by_ctx({"c1": 3.0, "c2": -1.0}, calls)
        same, _ = await oi.resolve_same_pairs([_ctx_task("c1"), _ctx_task("c2")], _Store(), judge)
        assert same == [] and calls == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_caps_contexts_per_pair(self) -> None:
        calls: list[str] = []
        ctxs = [f"c{i}" for i in range(6)]
        judge = self._judge_by_ctx(
            {c: 3.0 if i % 2 == 0 else -3.0 for i, c in enumerate(ctxs)}, calls
        )
        _, stats = await oi.resolve_same_pairs([_ctx_task(c) for c in ctxs], _Store(), judge)
        assert len(calls) <= oi.MAX_CONTEXTS_PER_NAME_PAIR and stats.judged == 1

    @pytest.mark.asyncio
    async def test_failed_context_is_skipped_not_fatal(self) -> None:
        calls: list[str] = []
        judge = self._judge_by_ctx({"c1": None, "c2": 3.0, "c3": 3.0}, calls)
        same, stats = await oi.resolve_same_pairs(
            [_ctx_task("c1"), _ctx_task("c2"), _ctx_task("c3")], _Store(), judge
        )
        assert same == [oi.pair_key("Foo", "ふー")] and stats.failed == 0

    @pytest.mark.asyncio
    async def test_all_contexts_failing_counts_as_failed(self) -> None:
        judge = self._judge_by_ctx({"c1": None, "c2": None}, [])
        store = _Store()
        _, stats = await oi.resolve_same_pairs([_ctx_task("c1"), _ctx_task("c2")], store, judge)
        assert stats.failed == 1 and store.puts == {}

    def test_lower_median_is_conservative_for_even_counts(self) -> None:
        assert oi.lower_median([5.0, -1.0]) == -1.0
        assert oi.lower_median([3.0, -27.0, 3.0]) == 3.0


def _article(
    aid: str,
    *,
    title: str = "t",
    body: str = "",
    entities: frozenset[tuple[str, str]] = frozenset(),
) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=title,
        url=f"https://x.example/{aid}",
        feed_title="f",
        feed_url="u",
        host="x.example",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=datetime(2026, 10, 1, tzinfo=UTC),
        summary="",
        body=body,
        entities=entities,
    )


_NEAR = [1.0, 0.0]
_FAR = [0.0, 1.0]


class TestBuildTasks:
    def test_builds_name_pairs_for_close_articles_with_disjoint_victims(self) -> None:
        cand = _article("c", body="Times Car で漏えい。")
        member = _article("m", body="タイムズカーで漏えい。")
        tasks = oi.build_tasks(
            [cand],
            [member],
            {"c": _NEAR, "m": _NEAR},
            {"c": ["Times Car"], "m": ["タイムズカー"]},
        )
        assert [(t.name_a, t.name_b) for t in tasks] == [("Times Car", "タイムズカー")]
        assert "Times Car" in tasks[0].ctx_a and "タイムズカー" in tasks[0].ctx_b

    def test_skips_articles_whose_body_is_far(self) -> None:
        tasks = oi.build_tasks(
            [_article("c")],
            [_article("m")],
            {"c": _NEAR, "m": _FAR},
            {"c": ["Foo"], "m": ["Bar"]},
        )
        assert tasks == []

    def test_skips_pairs_that_already_share_a_key(self) -> None:
        tasks = oi.build_tasks(
            [_article("c")],
            [_article("m")],
            {"c": _NEAR, "m": _NEAR},
            {"c": ["株式会社Foo", "Other"], "m": ["Foo"]},
        )
        assert tasks == []

    def test_skips_article_without_victim_names(self) -> None:
        tasks = oi.build_tasks(
            [_article("c")], [_article("m")], {"c": _NEAR, "m": _NEAR}, {"c": ["Foo"]}
        )
        assert tasks == []

    def test_each_article_pair_is_visited_once(self) -> None:
        a, b = _article("a"), _article("b")
        tasks = oi.build_tasks([a, b], [], {"a": _NEAR, "b": _NEAR}, {"a": ["Foo"], "b": ["Bar"]})
        assert len(tasks) == 1


class TestApplyAlias:
    def test_replaces_keys_in_candidates_and_members_without_mutating_inputs(self) -> None:
        alias = oi.build_alias_map([("timescar", "タイムズカー")])
        cand = _article("c", entities=frozenset({("victim_org", "timescar"), ("cve", "x")}))
        member = _article("m", entities=frozenset({("victim_org", "タイムズカー")}))
        state = ItemState(
            item_id="i",
            first_reported_at=datetime(2026, 10, 1, tzinfo=UTC),
            last_reported_at=datetime(2026, 10, 1, tzinfo=UTC),
            status="new",
            importance="high",
            current_version=0,
            member_ids=("m",),
        )

        new_cands, new_existing = oi.apply_alias([cand], [(state, [member])], alias)

        assert names_of_type(new_cands[0].entities, "victim_org") == names_of_type(
            new_existing[0][1][0].entities, "victim_org"
        )
        assert ("cve", "x") in new_cands[0].entities
        assert ("victim_org", "timescar") in cand.entities  # 入力は変えない

    def test_unblocks_the_grouping_gate(self) -> None:
        alias = oi.build_alias_map([("timescar", "タイムズカー")])
        a = frozenset({("victim_org", "timescar")})
        b = frozenset({("victim_org", "タイムズカー")})
        assert blocked_by_different_victims(a, b)
        assert not blocked_by_different_victims(
            oi.remap_entities(a, alias), oi.remap_entities(b, alias)
        )


class TestJudgeNamePair:
    @pytest.mark.asyncio
    async def test_refuses_sft_judge_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "kuebiko-sft:s23i")

        async def must_not_run(*_: object, **__: object) -> dict[str, float]:
            raise AssertionError("SFT モデルで判定してはならない")

        monkeypatch.setattr(oi, "probe_choice", must_not_run)
        assert await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig()) is None

    @staticmethod
    def _fake_probe(by_marker: dict[str, float]) -> object:
        """問いの文面から、返す P(Yes) を選ぶ差し替え。"""

        async def fake(
            step: object, question: str, choices: tuple[str, ...], **_: object
        ) -> dict[str, float]:
            key = "event" if "同じサイバー事案" in question else "org"
            p = by_marker[key]
            return {"Yes": p, "No": 1 - p}

        return fake

    @pytest.mark.asyncio
    async def test_margin_is_non_negative_when_both_questions_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "gemma4:26b")
        monkeypatch.setattr(oi, "probe_choice", self._fake_probe({"event": 0.99999999, "org": 0.9}))
        margin = await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig())
        assert margin is not None and margin >= oi.SAME_MARGIN

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "probs", [{"event": 0.99999999, "org": 0.001}, {"event": 0.9, "org": 0.99999999}]
    )
    async def test_margin_is_negative_when_either_question_fails(
        self, monkeypatch: pytest.MonkeyPatch, probs: dict[str, float]
    ) -> None:
        monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "gemma4:26b")
        monkeypatch.setattr(oi, "probe_choice", self._fake_probe(probs))
        margin = await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig())
        assert margin is not None and margin < oi.SAME_MARGIN

    @pytest.mark.asyncio
    async def test_probe_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: "gemma4:26b")

        async def fake(*_: object, **__: object) -> None:
            return None

        monkeypatch.setattr(oi, "probe_choice", fake)
        assert await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig()) is None

    def test_logit_is_clipped(self) -> None:
        assert oi.to_logit(1.0) == 15.0
        assert oi.to_logit(0.0) == -15.0
        assert oi.to_logit(0.5) == 0.0
