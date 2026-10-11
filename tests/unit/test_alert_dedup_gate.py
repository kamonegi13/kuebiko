"""src.cti.alert_dedup_gate のテスト (R1b v2)。

事象の群化 (src/eventnews/hourly.py) は配信より後に走る毎時バッチのため、配信時点で
使える代替手がかり (dedup_key/advisory id 完全一致 + embedding 候補検索 + 事象群化 ML
(pair_model)) だけで「同じ事象の重複 alert」を検出できるかを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.cti.alert_dedup_gate import (
    GATE_ENV,
    build_current_entities,
    find_prior_alert_duplicate,
    has_new_named_entities,
    is_alert_dedup_event_enabled,
)
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord

_EMBED_MODEL = "test-embed-model"


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "alert_dedup.db")


def _now() -> datetime:
    return datetime.now(UTC)


def _vector(seed: int, *, dim: int = 32) -> list[float]:
    """決定的な (再現可能な) ベクトル。正規分布なので 2 本の cos はおよそ 0 に近い。"""
    rng = np.random.default_rng(seed)
    return [float(v) for v in rng.normal(size=dim)]


def _seed(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    title: str,
    summary: str = "",
    url: str | None = None,
    category: str = "apt",
    posted_channel: str = "alert",
    dedup_key: str | None = None,
    hours_ago: float = 1.0,
    vector: list[float] | None = None,
    entities: list[tuple[str, str]] | None = None,
) -> str:
    """記事 (+ 任意で embedding・entity) を投稿済みとして登録する。登録した url を返す。"""
    art_url = url or f"https://example.com/{article_id}"
    run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title=title,
            summary=summary,
            url=art_url,
            category=category,
            feed_title="test-feed",
            status="posted",
            posted_channel=posted_channel,
            dedup_key=dedup_key,
            created_at=_now() - timedelta(hours=hours_ago),
        ),
    )
    if vector is not None:
        from src.tools.url_normalizer import url_hash

        h = url_hash(art_url)
        # article_embeddings.url_hash は dedup_seen_urls(url_hash) への FK
        repo.mark_url_seen(url_hash=h, url=art_url, article_id=article_id, title=title)
        repo.add_article_embedding(url_hash=h, url=art_url, vector=vector, model=_EMBED_MODEL)
    if entities:
        repo.add_article_entities(article_id, entities)
    return art_url


class TestIsAlertDedupEventEnabled:
    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(GATE_ENV, raising=False)
        assert is_alert_dedup_event_enabled() is False

    def test_enabled_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(GATE_ENV, "1")
        assert is_alert_dedup_event_enabled() is True


class TestBuildCurrentEntities:
    def test_collects_cve_from_iocs_and_mitre(self) -> None:
        entities = build_current_entities(
            iocs=["CVE-2026-1234", "1.2.3.4"],
            mitre_techniques=["CVE-2026-5678"],
            malware_families=["LockBit"],
            tools=["Cobalt Strike"],
            detected_actor_ids=["flax-typhoon"],
            victim_orgs=["Acme Corp"],
        )
        assert ("cve", "CVE-2026-1234") in entities
        assert ("cve", "CVE-2026-5678") in entities
        assert ("malware_family", "LockBit") in entities
        assert ("tool", "Cobalt Strike") in entities
        assert ("actor", "flax-typhoon") in entities
        assert ("victim_org", "Acme Corp") in entities

    def test_empty_inputs_give_empty_set(self) -> None:
        entities = build_current_entities(
            iocs=None,
            mitre_techniques=None,
            malware_families=None,
            tools=None,
            detected_actor_ids=None,
            victim_orgs=None,
        )
        assert entities == frozenset()


class TestHasNewNamedEntities:
    def test_true_when_current_has_new_actor(self) -> None:
        prior = frozenset({("actor", "flax-typhoon")})
        current = frozenset({("actor", "flax-typhoon"), ("victim_org", "Acme Corp")})
        assert has_new_named_entities(current, prior) is True

    def test_false_when_no_new_entities(self) -> None:
        prior = frozenset({("actor", "flax-typhoon"), ("cve", "CVE-2026-1")})
        current = frozenset({("actor", "Flax-Typhoon")})  # 大小文字違いのみ
        assert has_new_named_entities(current, prior) is False

    def test_ignores_non_tracked_types(self) -> None:
        prior: frozenset[tuple[str, str]] = frozenset()
        current = frozenset({("pir", "pir_china_apt"), ("ioc_ip", "1.2.3.4")})
        assert has_new_named_entities(current, prior) is False


class TestFindPriorAlertDuplicateExactMatch:
    def test_no_candidates_returns_none(self, repo: RunHistoryRepository) -> None:
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="新しい脅威レポート発表",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
        )
        assert result is None

    def test_matches_on_exact_dedup_key(self, repo: RunHistoryRepository) -> None:
        _seed(
            repo,
            article_id="a1",
            title="FBI、Flax Typhoon の基盤を無力化",
            dedup_key="flax-typhoon-takedown",
            posted_channel="alert",
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="flax-typhoon-takedown",
            title="FBI、Flax Typhoon の基盤を無力化 (続報)",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
            candidate_article_id="a2",
        )
        assert result is not None
        assert result.reason == "dedup_key"
        assert result.prior_article_id == "a1"

    def test_ignores_dedup_key_match_outside_alert_channel(
        self, repo: RunHistoryRepository
    ) -> None:
        _seed(
            repo,
            article_id="a1",
            title="日本関連の弱い言及記事",
            dedup_key="shared-key",
            posted_channel="japan_watch",
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="shared-key",
            title="別の記事",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
        )
        assert result is None

    def test_matches_on_shared_advisory_id(self, repo: RunHistoryRepository) -> None:
        _seed(
            repo,
            article_id="a1",
            title="JVN#12345678 の脆弱性に関する注意喚起",
            url="https://jvn.jp/jp/JVN12345678/",
            posted_channel="alert",
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="JVN#12345678 (JVNDB 再掲)",
            url="https://jvndb.jvn.jp/jvndb/JVN12345678/",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
        )
        assert result is not None
        assert result.reason == "advisory_id"

    def test_excludes_candidate_article_id_itself(self, repo: RunHistoryRepository) -> None:
        _seed(
            repo,
            article_id="a1",
            title="同一記事の再評価ケース",
            dedup_key="self-key",
            posted_channel="alert",
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="self-key",
            title="同一記事の再評価ケース",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
            candidate_article_id="a1",
        )
        assert result is None


class TestFindPriorAlertDuplicateEmbeddingPairModel:
    """embedding 候補検索 + 事象群化 ML (pair_model) の経路。"""

    def test_matches_when_cos_high_and_entities_shared(self, repo: RunHistoryRepository) -> None:
        vec = _vector(seed=0)
        shared_entities = [("actor", "flax-typhoon"), ("cve", "CVE-2026-1234")]
        _seed(
            repo,
            article_id="a1",
            title="FBI、Flax Typhoon の基盤を無力化",
            category="apt",
            posted_channel="alert",
            vector=vec,
            entities=shared_entities,
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="米司法省、Flax Typhoon 関連インフラを押収",
            category="apt",
            feed_title="feedB",
            published_at=_now(),
            vector=vec,  # 同一ベクトル → cos=1.0
            embedding_model=_EMBED_MODEL,
            entities=frozenset(shared_entities),
        )
        assert result is not None
        assert result.reason == "pair_model"
        assert result.prior_article_id == "a1"
        assert result.prior_entities == frozenset(shared_entities)

    def test_does_not_match_when_cos_low_and_no_shared_entities(
        self, repo: RunHistoryRepository
    ) -> None:
        _seed(
            repo,
            article_id="a1",
            title="Acme Widget の新しいランサムウェア感染",
            category="ransomware",
            posted_channel="alert",
            vector=_vector(seed=1),
            entities=[("malware_family", "lockbit")],
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="全く別の話題: 量子コンピュータの進展について",
            category="other",
            feed_title="feedC",
            published_at=_now() - timedelta(hours=50),
            vector=_vector(seed=2),
            embedding_model=_EMBED_MODEL,
            entities=frozenset({("tool", "nmap")}),
        )
        assert result is None

    def test_does_not_match_without_vector(self, repo: RunHistoryRepository) -> None:
        _seed(
            repo,
            article_id="a1",
            title="FBI、Flax Typhoon の基盤を無力化",
            posted_channel="alert",
            vector=_vector(seed=0),
            entities=[("actor", "flax-typhoon")],
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="無関係な別記事",
            published_at=_now(),
            vector=None,
            embedding_model="",
            entities=frozenset(),
        )
        assert result is None

    def test_ignores_candidate_outside_alert_channel(self, repo: RunHistoryRepository) -> None:
        vec = _vector(seed=0)
        _seed(
            repo,
            article_id="a1",
            title="FBI、Flax Typhoon の基盤を無力化",
            posted_channel="watch",
            vector=vec,
            entities=[("actor", "flax-typhoon")],
        )
        result = find_prior_alert_duplicate(
            repo=repo,
            dedup_key="",
            title="米司法省、Flax Typhoon 関連インフラを押収",
            published_at=_now(),
            vector=vec,
            embedding_model=_EMBED_MODEL,
            entities=frozenset({("actor", "flax-typhoon")}),
        )
        assert result is None
