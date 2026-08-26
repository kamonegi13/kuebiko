"""カタログ駆動の書き直し (重要識別子の取りこぼしを埋める)。

31B はカタログを見せられた上で重要識別子の半分しか使わない。散文の一般指示は
3 度無効だったが、**具体的な番号の指摘**による書き直しは不足 5 → 0 に埋めた
(2026-08-27 実測、Rust クレート記事)。発動は 5 記事中 1 件のみ = 平均コストは小さい。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from src.eventnews import identifier_gate, runner
from src.eventnews.models import EventNewsDraft, FactItem, MemberArticle


def _member(body: str) -> MemberArticle:
    return MemberArticle(
        article_id="a1",
        title="タイトル",
        url="https://kuebiko.example/1",
        feed_title="媒体",
        feed_url="https://kuebiko.example/feed",
        host="kuebiko.example",
        importance="high",
        category="vuln",
        status="posted",
        anchor_ts=datetime(2026, 8, 27, tzinfo=UTC),
        summary="",
        body=body,
        entities=frozenset(),
    )


_BODY = "CVE-2026-11111 は バージョン 2.10.5 以前に影響する。修正は version 2.10.6 で提供。"


class TestMissingImportantIdentifiers:
    def test_detects_a_dropped_version(self) -> None:
        members = [_member(_BODY)]
        catalog = identifier_gate.build_member_catalog(members)
        draft = EventNewsDraft(
            headline="h",
            bluf="CVE-2026-11111 の脆弱性が修正された",
            facts=[FactItem(text="脆弱性が報告された", source_index=1)],
        )

        missing = identifier_gate.missing_important_identifiers(draft, catalog)

        kinds = {kind for _, _, kind in missing}
        raws = {raw for _, raw, _ in missing}
        assert "version" in kinds
        assert "2.10.5" in raws or "2.10.6" in raws
        # CVE は BLUF に書かれているので不足に数えない
        assert not any(kind == "cve" for _, _, kind in missing)

    def test_caveats_and_unknowns_count_as_coverage(self) -> None:
        # unknowns で触れていれば「落とした」ではない
        members = [_member("CVE-2026-22222 の詳細は明らかでない。")]
        catalog = identifier_gate.build_member_catalog(members)
        draft = EventNewsDraft(
            headline="h",
            bluf="b",
            facts=[FactItem(text="脆弱性が報告された", source_index=1)],
            unknowns=["CVE-2026-22222 の悪用状況は不明"],
        )

        assert identifier_gate.missing_important_identifiers(draft, catalog) == ()

    def test_bulk_kinds_are_not_demanded(self) -> None:
        # ドメイン・ハッシュ・IP は一括列挙の記事で数百個になる。要求しない
        assert frozenset({"cve", "version", "cvss"}) == identifier_gate.IMPORTANT_KINDS


class TestRunnerWiring:
    def test_rewrite_hint_names_specific_tokens(self) -> None:
        source = inspect.getsource(runner._rewrite_hints)

        assert "missing_important_identifiers" in source
        assert "eventnews_coverage_rewrite" in source
        # 逃げ道を残す — 全部を強制すると一括列挙で記事が壊れる
        assert "関連しない値" in source

    def test_hint_is_capped(self) -> None:
        assert runner._COVERAGE_HINT_MAX <= 10

    def test_still_missing_is_observed_not_forced(self) -> None:
        source = inspect.getsource(runner._generate_version)

        assert "eventnews_coverage_missing" in source
