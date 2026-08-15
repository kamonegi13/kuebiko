"""src.cti.stix_from_briefing のテスト (Phase 4.5)。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from src.config_loader import AppConfig, StixAttachPolicy
from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.stix_from_briefing import (
    briefing_to_stix_bytes,
    make_attachment_filename,
)
from src.tools.discord_publisher import (
    BriefingIncident,
    BriefingMessage,
    Source,
)

# Phase 5F: 既存テストは lenient policy で旧挙動を維持 (新規テストで strict 既定を検証)
_LENIENT = StixAttachPolicy(mode="lenient")


def _schema_aware_gen(summary: Any) -> Any:
    """generate_structured の side_effect: summarizer は summary を、統合判断分類器
    (JudgmentOut) は既定値を返す (2026-07-26 に briefing が判断を JudgmentOut 1 呼び出しへ統合)。"""
    from src.cti.judgment_classifier import JudgmentOut

    async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
        if schema is JudgmentOut:
            return JudgmentOut(editorial_stance="factual_report")
        return summary

    return _gen


def _make_msg(**overrides: Any) -> BriefingMessage:
    base: dict[str, Any] = {
        "title": "Test",
        "bluf": "x",
        "importance": "medium",
        "category": "advisory",
        "summary": "y",
        "iocs": [],
        "mitre_techniques": [],
        "sources": [Source(title="src", url="https://example.com/", language="en")],
        "metadata": {},
    }
    base.update(overrides)
    return BriefingMessage(**base)


def _make_registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="volt-typhoon",
                canonical="Volt Typhoon",
                aliases=("Vanguard Panda",),
                mitre_group="G1017",
                nation="cn",
                sponsor="MSS",
            ),
        ),
    )


class TestBriefingToStixBytes:
    def test_returns_none_when_empty(self) -> None:
        msg = _make_msg()
        assert briefing_to_stix_bytes(msg) is None

    def test_emits_bundle_with_ipv4_indicator(self) -> None:
        msg = _make_msg(iocs=["45.55.66.77"])
        out = briefing_to_stix_bytes(msg)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        assert bundle["type"] == "bundle"
        types = [o["type"] for o in bundle["objects"]]
        assert "indicator" in types
        # IPv4 pattern が含まれる
        ipv4_indicators = [o for o in bundle["objects"] if o.get("type") == "indicator"]
        assert any("45.55.66.77" in o["pattern"] for o in ipv4_indicators)

    def test_emits_bundle_with_cve_vulnerability(self) -> None:
        msg = _make_msg(iocs=["CVE-2024-99999"])
        out = briefing_to_stix_bytes(msg)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        types = [o["type"] for o in bundle["objects"]]
        assert "vulnerability" in types

    def test_emits_bundle_with_mitre_technique(self) -> None:
        # Phase 5F: technique のみは strict 既定では skip。lenient で旧挙動を検証。
        msg = _make_msg(mitre_techniques=["T1566.001"])
        out = briefing_to_stix_bytes(msg, policy=_LENIENT)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        types = [o["type"] for o in bundle["objects"]]
        assert "attack-pattern" in types

    def test_emits_bundle_with_actor_via_metadata_id(self) -> None:
        # Phase 5F: actor のみは strict 既定では skip。lenient で旧挙動を検証。
        registry = _make_registry()
        msg = _make_msg(metadata={"detected_actor_ids": ["volt-typhoon"]})
        out = briefing_to_stix_bytes(msg, registry=registry, policy=_LENIENT)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        actors = [o for o in bundle["objects"] if o.get("type") == "threat-actor"]
        assert len(actors) == 1
        assert actors[0]["name"] == "Volt Typhoon"

    def test_subject_gate_restricts_to_primary_actor(self) -> None:
        """routing_flags.primary_actor_id があれば言及でなく主題 actor のみ threat-actor 化。

        外部連携 STIX への誤帰属流出防止 (2026-07-29)。言及 2 名でも主題 (volt) のみ出力。
        """
        registry = ActorAliasRegistry(
            actors=(
                ActorAlias(
                    id="volt-typhoon", canonical="Volt Typhoon", mitre_group="G1017", nation="cn"
                ),
                ActorAlias(
                    id="lazarus", canonical="Lazarus Group", mitre_group="G0032", nation="kp"
                ),
            ),
        )
        msg = _make_msg(
            metadata={
                "detected_actor_ids": ["volt-typhoon", "lazarus"],
                "routing_flags": {"primary_actor_id": "volt-typhoon"},
            }
        )
        out = briefing_to_stix_bytes(msg, registry=registry, policy=_LENIENT)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        actors = [o for o in bundle["objects"] if o.get("type") == "threat-actor"]
        assert {a["name"] for a in actors} == {"Volt Typhoon"}

    def test_emits_bundle_with_actor_via_text_fallback(self) -> None:
        """metadata に actor_ids が無い場合、msg のテキストから検出する (Grok 経路)。"""
        registry = _make_registry()
        msg = _make_msg(
            title="Volt Typhoon が日本のインフラを標的化",
            summary="Volt Typhoon は CRITICAL 認定された侵害キャンペーンを継続。",
        )
        # Phase 5F: lenient で actor のみでの bundle 生成を検証
        out = briefing_to_stix_bytes(msg, registry=registry, policy=_LENIENT)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        actors = [o for o in bundle["objects"] if o.get("type") == "threat-actor"]
        assert len(actors) == 1
        assert actors[0]["name"] == "Volt Typhoon"

    # Phase 5F: strict 既定の挙動検証
    def test_strict_default_skips_actor_only(self) -> None:
        """strict 既定: actor 1 名のみでは添付しない。"""
        registry = _make_registry()
        msg = _make_msg(metadata={"detected_actor_ids": ["volt-typhoon"]})
        out = briefing_to_stix_bytes(msg, registry=registry)
        assert out is None

    def test_strict_default_skips_technique_only(self) -> None:
        """strict 既定: technique 1 件のみでは添付しない。"""
        msg = _make_msg(mitre_techniques=["T1566.001"])
        out = briefing_to_stix_bytes(msg)
        assert out is None

    def test_strict_emits_with_concrete_ioc(self) -> None:
        """strict: 具体的 IOC (CVE/IP/domain/hash) ≥ 1 で添付。"""
        msg = _make_msg(iocs=["CVE-2024-99999"])
        out = briefing_to_stix_bytes(msg)
        assert out is not None

    def test_strict_emits_with_actor_and_technique(self) -> None:
        """strict: actor + technique 両方なら添付。"""
        registry = _make_registry()
        msg = _make_msg(
            metadata={"detected_actor_ids": ["volt-typhoon"]},
            mitre_techniques=["T1566.001"],
        )
        out = briefing_to_stix_bytes(msg, registry=registry)
        assert out is not None

    def test_strict_skips_empty_section_briefing(self) -> None:
        """strict: 「該当なし」briefing (grok_section_empty=True) は無条件 skip。"""
        registry = _make_registry()
        msg = _make_msg(
            metadata={
                "detected_actor_ids": ["volt-typhoon"],
                "grok_section_empty": True,
            },
            iocs=["CVE-2024-99999"],
        )
        out = briefing_to_stix_bytes(msg, registry=registry)
        assert out is None

    def test_off_mode_never_emits(self) -> None:
        msg = _make_msg(iocs=["45.55.66.77"], mitre_techniques=["T1566.001"])
        out = briefing_to_stix_bytes(msg, policy=StixAttachPolicy(mode="off"))
        assert out is None

    def test_aggregates_iocs_from_incidents(self) -> None:
        """multi-embed (incidents) も IOC を集約する。"""
        msg = _make_msg(
            incidents=[
                BriefingIncident(
                    heading="incident 1",
                    body="b",
                    iocs=["45.55.66.78"],
                    ttps=["T1059"],
                ),
                BriefingIncident(
                    heading="incident 2",
                    body="b",
                    iocs=["evil[.]malicious-test[.]io"],
                    ttps=["T1566.001"],
                ),
            ],
        )
        out = briefing_to_stix_bytes(msg)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        # IPv4 / domain indicator + 2 つの attack-pattern が出る
        types = [o["type"] for o in bundle["objects"]]
        assert types.count("indicator") >= 2
        assert types.count("attack-pattern") == 2
        # defang 解除されてドメインが正規表記で入る
        domain_indicators = [
            o
            for o in bundle["objects"]
            if o.get("type") == "indicator" and "domain-name" in o.get("pattern", "")
        ]
        assert any("evil.malicious-test.io" in o["pattern"] for o in domain_indicators)

    def test_includes_source_url_in_description(self) -> None:
        msg = _make_msg(
            iocs=["45.55.66.77"],
            sources=[Source(title="t", url="https://news.example.com/x", language="en")],
        )
        out = briefing_to_stix_bytes(msg)
        assert out is not None
        bundle = json.loads(out.decode("utf-8"))
        notes = [o for o in bundle["objects"] if o.get("type") == "note"]
        assert len(notes) == 1
        assert "https://news.example.com/x" in notes[0]["content"]

    # ---- Phase 5P: Discord 添付サイズ上限チェック ----

    def test_returns_none_when_payload_exceeds_size_limit(self) -> None:
        """巨大な IOC リストで 6MB を超えたら None を返し warning ログを出す。"""
        from unittest.mock import patch

        # 1 MB を超えるサイズのため、ユニーク IPv4 を 100k 件くらい
        many = [f"198.51.{(i // 256) % 256}.{i % 256}" for i in range(100_000)]
        msg = _make_msg(iocs=many)
        with patch("src.cti.stix_from_briefing._log") as mock_log:
            out = briefing_to_stix_bytes(msg)
        assert out is None
        # warning に stix_bundle_too_large が出る
        warn_calls = [
            c
            for c in mock_log.warning.call_args_list
            if c.args and c.args[0] == "stix_bundle_too_large"
        ]
        assert len(warn_calls) == 1
        assert "size" in warn_calls[0].kwargs

    def test_returns_payload_when_under_size_limit(self) -> None:
        """通常サイズ (< 1MB) は従来通り bytes を返す。"""
        msg = _make_msg(iocs=["45.55.66.77", "CVE-2026-99999"])
        out = briefing_to_stix_bytes(msg)
        assert out is not None
        assert len(out) < 6 * 1024 * 1024

    def test_size_limit_constant_is_under_discord_max(self) -> None:
        """安全マージン: Discord 上限 (8MB) 未満であること。"""
        from src.cti.stix_from_briefing import STIX_MAX_BYTES

        discord_max = 8 * 1024 * 1024
        assert discord_max > STIX_MAX_BYTES
        # マージン: 1 MB 以上の余裕を持たせる
        assert discord_max - STIX_MAX_BYTES >= 1 * 1024 * 1024


class TestMakeAttachmentFilename:
    def test_filename_has_importance_and_hash(self) -> None:
        f1 = make_attachment_filename(importance="high", article_id="tag:foo/1")
        assert f1.startswith("stix-high-")
        assert f1.endswith(".json")

    def test_same_article_same_filename(self) -> None:
        f1 = make_attachment_filename(importance="medium", article_id="tag:bar/2")
        f2 = make_attachment_filename(importance="medium", article_id="tag:bar/2")
        assert f1 == f2

    def test_different_article_different_filename(self) -> None:
        f1 = make_attachment_filename(importance="medium", article_id="a")
        f2 = make_attachment_filename(importance="medium", article_id="b")
        assert f1 != f2


# ---------- 統合: src/main.py 側で publisher.post に attachments が渡るか ----------


class TestPipelineAttachesStix:
    @pytest.mark.asyncio
    async def test_publish_loop_passes_attachments_when_iocs_present(
        self,
        tmp_path: Any,
    ) -> None:
        """run_pipeline で IOC を持つ briefing が STIX を添付して post される。"""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock

        import jinja2

        from src.config_loader import (
            PipelineConfig,
            ProcessorConfig,
            SourceConfig,
        )
        from src.main import SummaryOutput, run_pipeline
        from src.storage.run_history import RunHistoryRepository
        from src.tools.article_model import Article
        from src.tools.content_extractor import ExtractionResult
        from src.tools.discord_publisher import DiscordPublisher

        article = Article(
            id="a-1",
            title="Sample",
            url="https://example.com/1",
            summary_html=(
                "<p>45.55.66.77 was used in the attack. "
                + "Detailed analysis of the intrusion follows. " * 3
                + "</p>"
            ),
            author="A",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            feed_title="Feed",
            feed_url="https://example.com/feed",
        )

        env = jinja2.Environment(
            loader=jinja2.DictLoader({"briefing/summarizer.j2": "{{ body }}"}),
            autoescape=False,
        )
        template = env.get_template("briefing/summarizer.j2")

        pipeline = PipelineConfig(
            name="daily-briefing",
            source=SourceConfig(type="rss", max_articles=10),
            processor=ProcessorConfig(),
        )

        class _Cfg:
            ollama_main_model = "x"

        source = AsyncMock()
        source.fetch.return_value = [article]
        extractor = AsyncMock()
        extractor.extract.return_value = ExtractionResult(
            url=article.url,
            text=(
                "The attacker used 45.55.66.77 to reach the C2. "
                "Full incident analysis with additional context follows. " * 2
            ),
            success=True,
            language="en",
        )
        llm = AsyncMock()
        llm.generate_structured.side_effect = _schema_aware_gen(
            SummaryOutput(
                title_ja="サンプルタイトル",
                bluf="x",
                importance="medium",
                category="advisory",
                summary="y",
                iocs=["45.55.66.77"],
                mitre_techniques=[],
            )
        )
        publisher = AsyncMock(spec=DiscordPublisher)

        repo = RunHistoryRepository(db_path=tmp_path / "stix.db")

        await run_pipeline(
            config=cast(AppConfig, _Cfg()),
            pipeline=pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers={"brief": publisher},
            template=template,
            dry_run=False,
            dedup_repo=repo,
        )

        publisher.post.assert_called_once()
        call_kwargs = publisher.post.call_args.kwargs
        assert "attachments" in call_kwargs
        attachments = call_kwargs["attachments"]
        assert attachments is not None
        assert len(attachments) == 1
        filename, content = attachments[0]
        assert filename.startswith("stix-medium-")
        assert filename.endswith(".json")
        bundle = json.loads(content.decode("utf-8"))
        assert bundle["type"] == "bundle"

    @pytest.mark.asyncio
    async def test_publish_loop_skips_attachments_when_no_iocs(
        self,
        tmp_path: Any,
    ) -> None:
        """IOC / actor / technique がない briefing は添付なしで post される。"""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock

        import jinja2

        from src.config_loader import (
            PipelineConfig,
            ProcessorConfig,
            SourceConfig,
        )
        from src.main import SummaryOutput, run_pipeline
        from src.storage.run_history import RunHistoryRepository
        from src.tools.article_model import Article
        from src.tools.content_extractor import ExtractionResult
        from src.tools.discord_publisher import DiscordPublisher

        article = Article(
            id="a-2",
            title="No IOC article",
            url="https://example.com/2",
            summary_html=(
                "<p>just a thought piece. "
                + "Extended editorial commentary without indicators. " * 3
                + "</p>"
            ),
            author="A",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            feed_title="Feed",
            feed_url="https://example.com/feed",
        )

        env = jinja2.Environment(
            loader=jinja2.DictLoader({"briefing/summarizer.j2": "{{ body }}"}),
            autoescape=False,
        )
        template = env.get_template("briefing/summarizer.j2")

        pipeline = PipelineConfig(
            name="daily-briefing",
            source=SourceConfig(type="rss", max_articles=10),
            processor=ProcessorConfig(),
        )

        class _Cfg:
            ollama_main_model = "x"

        source = AsyncMock()
        source.fetch.return_value = [article]
        extractor = AsyncMock()
        extractor.extract.return_value = ExtractionResult(
            url=article.url,
            text=(
                "general security commentary, no specific indicators. "
                "Extended opinion piece body without any technical detail. " * 2
            ),
            success=True,
            language="en",
        )
        llm = AsyncMock()
        llm.generate_structured.side_effect = _schema_aware_gen(
            SummaryOutput(
                title_ja="サンプルタイトル",
                bluf="x",
                importance="medium",
                category="advisory",
                summary="y",
            )
        )
        publisher = AsyncMock(spec=DiscordPublisher)

        repo = RunHistoryRepository(db_path=tmp_path / "no-stix.db")

        await run_pipeline(
            config=cast(AppConfig, _Cfg()),
            pipeline=pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers={"brief": publisher},
            template=template,
            dry_run=False,
            dedup_repo=repo,
        )

        publisher.post.assert_called_once()
        call_kwargs = publisher.post.call_args.kwargs
        # attachments=None で post される (添付スキップ)
        assert call_kwargs.get("attachments") is None
