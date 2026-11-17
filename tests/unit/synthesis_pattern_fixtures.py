"""状況総括パターンのテスト用の固定入力 (架空の組織名のみ)。"""

from __future__ import annotations

from dataclasses import replace

from src.storage.repo_judgment_attrs import JudgmentAttrs
from src.synthesis.grounded.estimate import KeyJudgment
from tests.unit.n20_t5_fixtures import judgment

JP = frozenset({"JP"})
BREACH = frozenset({"breach"})
EXPLOIT = frozenset({"exploitation"})


def j(
    jid: str, claim: str, *, delta: str = "opened", conf: str = "moderate", japan: bool = False
) -> KeyJudgment:
    return replace(judgment(jid, confidence=conf, delta=delta), claim=claim, japan_related=japan)


def attrs(
    *,
    countries: frozenset[str] = frozenset(),
    kinds: frozenset[str] = frozenset(),
    s3: bool = False,
    roots: tuple[str, ...] = (),
) -> JudgmentAttrs:
    return JudgmentAttrs(
        countries=countries,
        kinds=kinds,
        severities=frozenset({"S3"}) if s3 else frozenset(),
        event_roots=frozenset(roots),
    )
