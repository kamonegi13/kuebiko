"""事象ニュース生成 (メンバー選抜 + prompt 組立 + structured 生成) のテスト。

docs/event_news_design.md §9。識別子関門はここではテストしない (別 worktree 分担)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from src.eventnews.generator import build_prompt, generate_draft, select_members
from src.eventnews.models import PROMPT_MEMBER_CAP, EventNewsDraft, FactItem, MemberArticle
from src.tools.llm_client import LLMClient

_BASE_TS = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)


def _member(
    article_id: str,
    *,
    feed_title: str = "BleepingComputer",
    feed_url: str = "https://bleepingcomputer.com/feed",
    host: str = "bleepingcomputer.com",
    anchor_offset_hours: int = 0,
    summary: str = "s",
    body: str = "",
) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=f"title-{article_id}",
        url=f"https://e/{article_id}",
        feed_title=feed_title,
        feed_url=feed_url,
        host=host,
        importance="medium",
        category="apt",
        status="posted",
        anchor_ts=_BASE_TS + timedelta(hours=anchor_offset_hours),
        summary=summary,
        body=body,
        entities=frozenset(),
    )


class TestSelectMembers:
    def test_official_tier_ranks_before_other_tiers(self) -> None:
        # official (cisa.gov) は anchor_ts が後でも tier 優先で先頭に来る
        members = [
            _member(
                "a1",
                feed_title="BleepingComputer",
                feed_url="https://bleepingcomputer.com/feed",
                host="bleepingcomputer.com",
                anchor_offset_hours=0,
            ),
            _member(
                "a2",
                feed_title="CISA",
                feed_url="https://www.cisa.gov/x",
                host="cisa.gov",
                anchor_offset_hours=5,
            ),
        ]

        selected, omitted = select_members(members)

        assert [m.article_id for m in selected] == ["a2", "a1"]
        assert omitted == 0

    def test_within_same_tier_sorts_by_anchor_ts_ascending(self) -> None:
        members = [
            _member("a1", anchor_offset_hours=5),
            _member("a2", anchor_offset_hours=1),
        ]

        selected, _omitted = select_members(members)

        assert [m.article_id for m in selected] == ["a2", "a1"]

    def test_members_over_cap_are_omitted_and_counted(self) -> None:
        members = [_member(f"a{i}", anchor_offset_hours=i) for i in range(PROMPT_MEMBER_CAP + 3)]

        selected, omitted = select_members(members)

        assert len(selected) == PROMPT_MEMBER_CAP
        assert omitted == 3
        # 同格 tier なので anchor_ts 昇順で最も古い PROMPT_MEMBER_CAP 件が残る
        assert [m.article_id for m in selected] == [f"a{i}" for i in range(PROMPT_MEMBER_CAP)]


class TestBuildPrompt:
    def test_prompt_contains_numbered_members_and_allowed_identifiers(self) -> None:
        members = [_member("a1", summary="Actor abused CVE-2024-1234")]

        prompt = build_prompt(members, "CVE-2024-1234, APT41")

        assert "[1]" in prompt
        assert "CVE-2024-1234" in prompt
        assert "APT41" in prompt
        assert "title-a1" in prompt

    def test_prompt_reports_omitted_member_count(self) -> None:
        members = [_member(f"a{i}", anchor_offset_hours=i) for i in range(PROMPT_MEMBER_CAP + 2)]

        prompt = build_prompt(members, "")

        assert "他 2 件は省略" in prompt

    def test_prompt_carries_no_salience_injection(self) -> None:
        # CLAUDE.md §7: 収集量を重要性の代理にしない — eventnews prompt も対象 (test_burst_boundary)
        members = [_member("a1")]

        prompt = build_prompt(members, "")

        assert "forecast_indicators" not in prompt
        assert "nation_correlation" not in prompt


class _FakeLLM:
    """generate_structured のみ実装する fake (model property 必須の掟、過去の教訓)。"""

    model = "fake-eventnews"

    def __init__(self, draft: EventNewsDraft) -> None:
        self._draft = draft
        self.calls: list[str] = []

    async def generate_structured(
        self, prompt: str, schema: type, **_kwargs: Any
    ) -> EventNewsDraft:
        self.calls.append(prompt)
        assert schema is EventNewsDraft
        return self._draft


class TestGenerateDraft:
    def test_generate_draft_returns_llm_structured_output(self) -> None:
        members = [_member("a1", summary="Actor abused CVE-2024-1234")]
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="f1", source_index=1)],
        )
        fake = _FakeLLM(draft)

        result = asyncio.run(generate_draft(members, "CVE-2024-1234", cast(LLMClient, fake)))

        assert result is draft
        assert len(fake.calls) == 1
        assert "[1]" in fake.calls[0]
        assert "CVE-2024-1234" in fake.calls[0]


# ---------- 本文入力 + 無テキスト member の除外 (2026-08-23) ----------


class TestTextualMemberSelection:
    def test_members_without_text_are_not_selected(self) -> None:
        """本文も要約も無いメンバーは [N] 枠を得ない (捏造の温床を構造的に断つ)。"""
        withtext = _member("a", summary="本文あり")
        notext = _member("b", summary="", body="")

        selected, omitted = select_members([withtext, notext])

        assert [m.article_id for m in selected] == ["a"]
        assert omitted == 1

    def test_prompt_uses_body_over_summary(self) -> None:
        """入力は本文優先 — 要約は本文の 1/12.8 まで圧縮済みで情報を回復できない。"""
        m = _member("a", summary="みじかい要約", body="本文にしかない詳細な記述")

        prompt = build_prompt([m], "(識別子なし)")

        assert "本文にしかない詳細な記述" in prompt
        assert "みじかい要約" not in prompt


class TestBoilerplateStripping:
    def test_media_boilerplate_is_removed_from_prompt(self) -> None:
        """媒体側の節見出しを入力から断つ (指示では止まらない、の規約)。

        実測: The Register の 'MORE CONTEXT' が本文へ「CONTEXT の文脈として」と写った。
        """
        m = _member("a", summary="", body="侵害が判明した。MORE CONTEXT 同社はロシア由来である。")

        prompt = build_prompt([m], "(識別子なし)")

        assert "MORE CONTEXT" not in prompt
        assert "同社はロシア由来である" in prompt
