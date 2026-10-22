"""s24c の課題の印 (2026-10-22)。

s24b 専用だった新しい課題の印 (judgment / ioc_verify / ach_aux / dedup_judge) を s24c 系にも付ける。
**s23i と s24 (無印) の挙動は不変**であることをここで固定する (切替前の本番を動かさない)。
"""

from __future__ import annotations

import pytest

from src.tools.model_tiers import Step
from src.tools.task_prefix import prefix_for, untrained_steps_for, with_task_prefix

_NEW_MARKERS = {
    Step.JUDGMENT_CLASSIFY: "[task: judgment]\n",
    Step.IOC_LLM_VERIFY: "[task: ioc_verify]\n",
    Step.LEDGER_ACH_AUX: "[task: ach_aux]\n",
    Step.DEDUP_CLUSTER_JUDGE: "[task: dedup_judge]\n",
}
_NEW_FAMILY_TAGS = (
    "kuebiko-sft:s24b",
    "kuebiko-sft:s24c",
    "kuebiko-sft:s24bi50",
    "kuebiko-sft:s24ci50",
)
_OLD_TAGS = ("kuebiko-sft:s23i", "kuebiko-sft:s24", "kuebiko-sft:s24i25", "kuebiko-sft:i58")


@pytest.mark.parametrize("tag", _NEW_FAMILY_TAGS)
def test_new_markers_apply_to_s24b_and_s24c_families(tag: str) -> None:
    for step, mark in _NEW_MARKERS.items():
        assert prefix_for(step, tag) == mark
        assert with_task_prefix("本文", step, tag) == mark + "本文"


@pytest.mark.parametrize("tag", _NEW_FAMILY_TAGS)
def test_untrained_steps_stay_unmarked_for_new_family(tag: str) -> None:
    for step in (Step.SYNTHESIS_DETECT, Step.DETECT_CLAIM_WRITE, Step.EVENT_NEWS):
        assert with_task_prefix("本文", step, tag) == "本文"
    assert untrained_steps_for(tag).isdisjoint(_NEW_MARKERS)


@pytest.mark.parametrize("tag", ("kuebiko-sft:s24", "kuebiko-sft:s24i25"))
def test_plain_s24_keeps_borrowed_markers_and_no_new_ones(tag: str) -> None:
    for step in _NEW_MARKERS:
        assert "[task: judgment]" not in prefix_for(step, tag)
        assert prefix_for(step, tag) not in {"[task: ioc_verify]\n", "[task: ach_aux]\n"}
    assert with_task_prefix("本文", Step.DEDUP_CLUSTER_JUDGE, tag) == "本文"
    assert with_task_prefix("本文", Step.LEDGER_ACH_AUX, tag) == "本文"
    assert with_task_prefix("本文", Step.IOC_LLM_VERIFY, tag) == "本文"


def test_s23i_behavior_is_frozen() -> None:
    """本番 (s23i) は借用の印のまま — s24c の登録で何も変わらない。"""
    tag = "kuebiko-sft:s23i"
    assert with_task_prefix("本文", Step.JUDGMENT_CLASSIFY, tag) == "[task: summary]\n本文"
    assert with_task_prefix("本文", Step.IOC_LLM_VERIFY, tag) == "[task: summary]\n本文"
    assert with_task_prefix("本文", Step.LEDGER_ACH_AUX, tag) == "[task: ach]\n本文"
    assert with_task_prefix("本文", Step.DEDUP_CLUSTER_JUDGE, tag) == "本文"
    assert with_task_prefix("本文", Step.TRIAGE, tag) == "[task: triage]\n本文"
    assert untrained_steps_for(tag) == frozenset()


def test_s24c_unmarked_steps_not_marked_for_translation() -> None:
    """翻訳・和訳の印は n20 だけ (N20C) — s24c にも付かない。"""
    for step in (Step.ARTICLE_TRANSLATE, Step.ACTOR_SYNC):
        assert with_task_prefix("本文", step, "kuebiko-sft:s24c") == "本文"
