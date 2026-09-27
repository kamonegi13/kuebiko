"""事象 1 件の STIX 2.1 bundle (2026-09-27) — 事象の report が構成記事の report を指す。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import src.cti.stix.event as event_mod
from src.cti.actor_normalizer import ActorAliasRegistry
from src.cti.stix.article import ArticleFacts
from src.cti.stix.core import EXTENSION_ID
from src.cti.stix.event import build_event_bundle
from tests.unit.stix_validation import assert_extensions_match_schema, assert_valid_stix

_T0 = datetime(2026, 9, 20, tzinfo=UTC)
_T1 = datetime(2026, 9, 22, tzinfo=UTC)


class _Repo:
    def __init__(self, merged: bool = False) -> None:
        self.merged = merged

    def get_event_item(self, item_id: str) -> Any:
        state = SimpleNamespace(
            first_reported_at=_T0, last_reported_at=_T1, status="updated", importance="high"
        )
        return SimpleNamespace(
            state=state,
            merged_into="ev-other" if self.merged else None,
            change_kind="new_quantity",
            independent_sources=2,
        )

    def list_event_versions(self, item_id: str) -> list[Any]:
        body = {
            "bluf": "ある CMS に脆弱性が公表された。",
            "key_points": ["5 件の脆弱性"],
            "facts": [{"text": "5 件の脆弱性が公表された", "source_index": 2}],
            "discrepancies": [],
            "caveats": [{"text": "影響は脆弱性ごとに異なる", "source_index": 9}],
            "unknowns": ["悪用の有無"],
        }
        return [SimpleNamespace(version=3, headline="CMS に脆弱性", body_json=json.dumps(body))]

    def list_event_members(self, item_id: str) -> list[Any]:
        return [SimpleNamespace(article_id="a1"), SimpleNamespace(article_id="a2")]


@pytest.fixture(autouse=True)
def _facts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        event_mod,
        "facts_from_db",
        lambda repo, aid: ArticleFacts(
            article_id=aid,
            title=f"記事 {aid}",
            url=f"https://kuebiko.example/{aid}",
            cves=("CVE-2026-1234",),
        ),
    )


def test_event_bundle_is_valid_and_points_to_member_reports() -> None:
    b = build_event_bundle(_Repo(), ActorAliasRegistry(actors=()), "ev-1")  # type: ignore[arg-type]

    assert b is not None
    assert_valid_stix(b)
    assert_extensions_match_schema(b)
    reports = {x["name"]: x for x in b["objects"] if x["type"] == "report"}
    event = reports["CMS に脆弱性"]
    assert {reports["記事 a1"]["id"], reports["記事 a2"]["id"]} <= set(event["object_refs"])


def test_facts_keep_their_source_article() -> None:
    b = build_event_bundle(_Repo(), ActorAliasRegistry(actors=()), "ev-1")  # type: ignore[arg-type]

    assert b is not None
    event = next(x for x in b["objects"] if x.get("name") == "CMS に脆弱性")
    ext = event["extensions"][EXTENSION_ID]
    assert ext["facts"] == [{"text": "5 件の脆弱性が公表された", "article_id": "a2"}]
    assert ext["caveats"][0]["article_id"] == ""  # 範囲外の番号は出典なし
    assert ext["independent_sources"] == 2


def test_merged_event_is_none() -> None:
    assert build_event_bundle(_Repo(merged=True), ActorAliasRegistry(actors=()), "ev-1") is None  # type: ignore[arg-type]
