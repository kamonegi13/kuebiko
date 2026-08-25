"""単独報のプロンプト分岐 (solo)。

単独報は出典が 1 件しかないので、文単位の [N] は情報を持たない。粒度を
「1 文 = 1 事実」から「1 節 = 1 段落」へ変え、読み物として再構成させる。
**複数報道側は現状維持**が要件なので、solo=False の描画に単独報用の語が
一切混ざらないことを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.eventnews import generator as gen
from src.eventnews import runner
from src.eventnews.models import ItemState, MemberArticle

_SOLO_ONLY = "facts の粒度 (単独報)"
# 単独報の説明文にも「ソース間の相違は存在しない」と出るので、見出しの形で照合する
_MULTI_ONLY = (
    "1 文 = 1 出典",
    "1 媒体のみが報じている",
    "**discrepancies (ソース間の相違)**",
)


def _member(index: int, body: str = "本文") -> MemberArticle:
    return MemberArticle(
        article_id=f"a{index}",
        title=f"タイトル{index}",
        url=f"https://kuebiko.example/{index}",
        feed_title=f"媒体{index}",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="high",
        category="apt",
        status="posted",
        anchor_ts=datetime(2026, 8, 26, tzinfo=UTC),
        summary="要約",
        body=body,
        entities=frozenset(),
    )


def test_solo_prompt_switches_to_section_granularity() -> None:
    # Arrange / Act
    prompt = gen.build_prompt([_member(1)], "(なし)")

    # Assert
    assert _SOLO_ONLY in prompt
    for phrase in _MULTI_ONLY:
        assert phrase not in prompt


def test_multi_prompt_keeps_sentence_granularity() -> None:
    # Arrange / Act — 複数報道は現状維持 (単独報用の語が混ざらない)
    prompt = gen.build_prompt([_member(1), _member(2)], "(なし)")

    # Assert
    assert _SOLO_ONLY not in prompt
    for phrase in _MULTI_ONLY:
        assert phrase in prompt


def test_no_unrendered_template_tags() -> None:
    # Arrange / Act — 分岐の閉じ忘れは、タグがそのまま本文に混ざる形で出る
    for members in ([_member(1)], [_member(1), _member(2)]):
        prompt = gen.build_prompt(members, "(なし)")

        # Assert
        assert "{%" not in prompt
        assert "{{" not in prompt


def test_solo_skeleton_shows_one_fact_per_section() -> None:
    # Arrange / Act — 骨組みはモデルが写す (節が全部 what に潰れた前例がある)
    prompt = gen.build_prompt([_member(1)], "(なし)")

    # Assert — 例が全て source_index 1 / 節ごとに 1 件 / 段落は 1 文でない
    assert '"section": "scope"' in prompt
    assert '"source_index": 2' not in prompt
    assert "2-4 文の段落" in prompt


class TestSoloGenerationScope:
    """単独報を生成対象にする条件 (src/eventnews/runner.py)。

    公開面は high しか出さないので母集団を high に揃える。全件生成すると
    205 件/日 (実測 2026-08-26) で、複数報道 18.6 件/日 の 11 倍になる。
    """

    @staticmethod
    def _item(importance: str, members: list[MemberArticle]) -> runner._LiveItem:
        state = ItemState(
            item_id="ev-1",
            first_reported_at=datetime(2026, 8, 26, tzinfo=UTC),
            last_reported_at=datetime(2026, 8, 26, tzinfo=UTC),
            importance=importance,
            current_version=0,
            member_ids=tuple(m.article_id for m in members),
            status="active",
        )
        return runner._LiveItem(snapshot=state, members=list(members))

    def test_multi_source_generates_regardless_of_importance(self) -> None:
        members = [_member(1), _member(2)]
        item = self._item("low", members)

        assert runner._should_generate(item, members) is True

    def test_solo_high_with_long_body_generates(self) -> None:
        members = [_member(1, body="あ" * runner._SOLO_MIN_BODY_CHARS)]
        item = self._item("high", members)

        assert runner._should_generate(item, members) is True

    def test_solo_below_high_is_skipped(self) -> None:
        members = [_member(1, body="あ" * runner._SOLO_MIN_BODY_CHARS)]
        item = self._item("medium", members)

        assert runner._should_generate(item, members) is False

    def test_solo_with_short_body_is_skipped(self) -> None:
        # タイトル + 抜粋だけで生成させると内容を創作する
        members = [_member(1, body="あ" * (runner._SOLO_MIN_BODY_CHARS - 1))]
        item = self._item("high", members)

        assert runner._should_generate(item, members) is False

    def test_no_textual_member_is_skipped(self) -> None:
        item = self._item("high", [])

        assert runner._should_generate(item, []) is False
