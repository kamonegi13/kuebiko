"""src.storage.run_history のテスト (Phase 1.5)。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import (
    ArticleRecord,
    F1SelectionRecord,
    RunHistoryRepository,
    RunRecord,
)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "test.db")


def _now() -> datetime:
    return datetime.now(UTC)


class TestSchemaInitialization:
    def test_schema_is_created_on_construct(self, tmp_path: Path) -> None:
        db = tmp_path / "fresh.db"
        assert not db.exists()
        RunHistoryRepository(db_path=db)
        assert db.exists()

    def test_idempotent_schema_creation(self, tmp_path: Path) -> None:
        db = tmp_path / "idempotent.db"
        RunHistoryRepository(db_path=db)
        # 二度目の構築でも例外を出さない
        RunHistoryRepository(db_path=db)


class TestRunLifecycle:
    def test_start_run_returns_id(self, repo: RunHistoryRepository) -> None:
        run = RunRecord(
            started_at=_now(),
            pipeline="daily-briefing",
            dry_run=False,
            triggered_by="manual",
        )
        run_id = repo.start_run(run)
        assert run_id >= 1

    def test_get_run_returns_started_record(self, repo: RunHistoryRepository) -> None:
        run = RunRecord(
            started_at=_now(),
            pipeline="daily-briefing",
            dry_run=True,
        )
        run_id = repo.start_run(run)
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.pipeline == "daily-briefing"
        assert loaded.dry_run is True
        assert loaded.status == "running"
        assert loaded.finished_at is None

    def test_finish_run_updates_status_and_metrics(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        finished = _now()
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=finished,
            total_fetched=10,
            summarized=10,
            posted=8,
            marked_read=8,
            error_count=2,
            note="2 articles failed extraction",
        )
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.finished_at is not None
        assert loaded.total_fetched == 10
        assert loaded.posted == 8
        assert loaded.error_count == 2

    def test_get_run_returns_none_for_missing_id(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        assert repo.get_run(9999) is None

    def test_list_runs_in_descending_order(self, repo: RunHistoryRepository) -> None:
        old = _now() - timedelta(hours=2)
        new = _now()
        id_old = repo.start_run(
            RunRecord(started_at=old, pipeline="daily", dry_run=False),
        )
        id_new = repo.start_run(
            RunRecord(started_at=new, pipeline="daily", dry_run=False),
        )
        runs = repo.list_runs(limit=10)
        assert [r.id for r in runs] == [id_new, id_old]


class TestArticleRecording:
    def test_add_article_returns_id(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        article_id = repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:foo",
                title="An article",
                url="https://example.com/1",
                feed_title="Feed",
                importance="high",
                category="apt",
                status="posted",
                posted_channel="priority",
                duration_seconds=12.5,
            ),
        )
        assert article_id >= 1

    def test_feed_url_roundtrip(self, repo: RunHistoryRepository) -> None:
        """安定 source キー feed_url が表示名 (feed_title) と別に永続化・復元される。"""
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="daily", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="fu1",
                title="t",
                url="https://e/a",
                feed_title="Display Name",
                feed_url="https://e/feed.xml",
                status="posted",
            ),
        )
        got = repo.list_articles(run_id=run_id)[0]
        assert got.feed_url == "https://e/feed.xml"  # 安定キー
        assert got.feed_title == "Display Name"  # 表示名は別保持

    def test_feed_url_defaults_none_when_absent(self, repo: RunHistoryRepository) -> None:
        """feed_url 未指定 (旧経路) は NULL → クエリ側 fallback で挙動不変。"""
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="daily", dry_run=False))
        repo.add_article(
            ArticleRecord(run_id=run_id, article_id="fu2", title="t", url="u", status="posted"),
        )
        got = repo.list_articles(run_id=run_id)[0]
        assert got.feed_url is None

    def test_backfill_feed_url_by_title(self, repo: RunHistoryRepository) -> None:
        """Stage 2: NULL 行のみ feed_title 一致で feed_url を後付け (既存値は不変・冪等)。"""
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="daily", dry_run=False))
        for i in range(2):  # feed_url 無し ×2
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"b{i}",
                    title="t",
                    url=f"u{i}",
                    feed_title="Acme",
                    status="posted",
                ),
            )
        # feed_url 既設定 ×1 (上書きされないこと)
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="b2",
                title="t",
                url="u2",
                feed_title="Acme",
                feed_url="https://existing",
                status="posted",
            ),
        )
        assert repo.count_articles_missing_feed_url() == (2, 3)

        n = repo.backfill_feed_url_by_title(feed_title="Acme", feed_url="https://acme/feed")
        assert n == 2  # NULL の 2 件のみ
        assert repo.count_articles_missing_feed_url() == (0, 3)
        existing = next(a for a in repo.list_articles(run_id=run_id) if a.article_id == "b2")
        assert existing.feed_url == "https://existing"  # 既存値は不変
        # 冪等: 再実行で 0 件
        assert repo.backfill_feed_url_by_title(feed_title="Acme", feed_url="https://acme/feed") == 0

    def test_published_at_roundtrip(self, repo: RunHistoryRepository) -> None:
        """published_at (公開時刻) が created_at(取得時刻) と別に永続化・復元される。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        pub = datetime(2026, 5, 20, 9, 0, tzinfo=UTC)
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="p1",
                title="t",
                url="u",
                status="posted",
                published_at=pub,
            ),
        )
        got = repo.list_articles(run_id=run_id)[0]
        assert got.published_at == pub
        # created_at は別物 (デフォルト=処理時刻 now)
        assert got.created_at != pub

    def test_article_type_roundtrip(self, repo: RunHistoryRepository) -> None:
        """article_type が add_article で永続化・復元される。

        2026-07-27 d90126d がモデル/persistence 側だけ追加し INSERT 列を漏らした断線
        (07-30 から fill 完全ゼロ) の再発防止。
        """
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="daily", dry_run=False))
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="at1",
                title="t",
                url="u",
                status="posted",
                article_type="advisory",
            ),
        )
        got = repo.list_articles(run_id=run_id)[0]
        assert got.article_type == "advisory"

    def test_add_article_persists_all_record_fields(self, repo: RunHistoryRepository) -> None:
        """ArticleRecord の全永続化対象フィールドが add_article で書かれる (guard)。

        「モデルにフィールドを足したが INSERT 列リストを更新し忘れる」クラスの
        バグ (article_type 断線の真因) を構造的に遮断する。別 seam で書く列
        (body_source / extraction_failure_reason = update_article_body、
        refetch_attempts = 再取得機構) と自動採番系 (id / created_at) は対象外。
        """
        via_other_seams = {
            "id",
            "created_at",
            "body_source",
            "extraction_failure_reason",
            "refetch_attempts",
        }
        run_id = repo.start_run(RunRecord(started_at=_now(), pipeline="daily", dry_run=False))
        sentinel: dict[str, object] = {
            "run_id": run_id,
            "article_id": "guard1",
            "title": "guard title",
            "url": "https://e/guard",
            "feed_title": "Guard Feed",
            "feed_url": "https://e/guard.xml",
            "importance": "high",
            "category": "apt",
            "status": "posted",
            "failure_reason": "fr",
            "posted_channel": "alert",
            "duration_seconds": 1.5,
            "dedup_key": "dk",
            "discord_message_id": "dm1",
            "discord_channel_id": "dc1",
            "summary": "s",
            "pmesii_p": True,
            "pmesii_m": True,
            "pmesii_e": True,
            "pmesii_s": True,
            "pmesii_i_infra": True,
            "pmesii_i_cyber": True,
            "pmesii_p_env": True,
            "pmesii_t": True,
            "victim_sector_canonical": "energy",
            "victim_sector_raw": "電力",
            "victim_country_iso": "JP",
            "victim_country_raw": "日本",
            "victim_country_scope": "multi",
            "is_ransomware": True,
            "socio_political_intent": "espionage",
            "intent_confidence": "high",
            "subject_actor_ids": "qilin",
            "subject_actor_source": "llm",
            "subject_actor_confidence": "high",
            "llm_primary_actor_raw": "Qilin",
            "llm_primary_confidence": "high",
            "subject_actor_rationale": "候補は背景言及のため主題なし",
            "remediation": "patch",
            "socio_political_rationale": "r",
            "technical_axis_summary": "tech",
            "editorial_stance": "factual_report",
            "routing_rule_id": "rule1",
            "routing_reason": "reason",
            "published_at": datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
            "event_date": "2026-05-19",
            "event_date_basis": "reported",
            "compromise_date": "2026-05-01",
            "article_type": "advisory",
        }
        model_fields = set(ArticleRecord.model_fields) - via_other_seams
        missing_sentinels = model_fields - set(sentinel)
        assert not missing_sentinels, (
            f"ArticleRecord に新フィールド {missing_sentinels} が追加されています。"
            " add_article の INSERT 列と本テストの sentinel の両方に追加してください"
            " (別 seam で書く列なら via_other_seams に理由付きで登録)。"
        )
        repo.add_article(ArticleRecord(**sentinel))  # type: ignore[arg-type]
        got = next(a for a in repo.list_articles(run_id=run_id) if a.article_id == "guard1")
        for field in model_fields:
            assert getattr(got, field) == sentinel[field], (
                f"フィールド {field} が add_article で永続化されていません"
                " (INSERT 列リストの更新漏れ)。"
            )

    def test_filter_articles_by_run(self, repo: RunHistoryRepository) -> None:
        run_a = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        run_b = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for run_id, n in [(run_a, 2), (run_b, 3)]:
            for i in range(n):
                repo.add_article(
                    ArticleRecord(
                        run_id=run_id,
                        article_id=f"id-{run_id}-{i}",
                        title="t",
                        url="u",
                        status="posted",
                    ),
                )
        assert len(repo.list_articles(run_id=run_a)) == 2
        assert len(repo.list_articles(run_id=run_b)) == 3

    def test_filter_articles_by_importance(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for imp in ["high", "high", "medium", "low"]:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"id-{imp}",
                    title="t",
                    url="u",
                    importance=imp,
                    status="posted",
                ),
            )
        high_only = repo.list_articles(importance="high")
        assert len(high_only) == 2
        assert all(a.importance == "high" for a in high_only)
        # importance_in: medium+high (記事フィードの「medium 以上」セマンティクス)
        med_plus = repo.list_articles(importance_in=["medium", "high"])
        assert len(med_plus) == 3  # high×2 + medium×1
        assert all(a.importance in ("medium", "high") for a in med_plus)

    def test_filter_articles_by_new_facets(self, repo: RunHistoryRepository) -> None:
        """記事フィード widget 用フィルタ (category/feed/channel/search/since)。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        rows = [
            ("CVE-2026-1234 critical RCE", "vulnerability", "NVD", "alert"),
            ("Advisory: patch now", "advisory", "NVD", "brief"),
            ("APT actor campaign", "apt", "Mandiant", "watch"),
            ("Ransomware breach report", "breach", "Mandiant", "watch"),
        ]
        for i, (title, cat, feed, chan) in enumerate(rows):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"id-{i}",
                    title=title,
                    url=f"https://example.com/{i}",
                    feed_title=feed,
                    category=cat,
                    posted_channel=chan,
                    status="posted",
                ),
            )

        # 単一 category
        assert len(repo.list_articles(category="apt")) == 1
        # 合成グループ (vuln = vulnerability + advisory)
        vuln = repo.list_articles(category_in=["vulnerability", "advisory"])
        assert len(vuln) == 2
        # feed フィルタ
        assert len(repo.list_articles(feed_title="Mandiant")) == 2
        # channel フィルタ
        assert len(repo.list_articles(posted_channel="alert")) == 1
        # 大小無視の部分一致検索
        assert len(repo.list_articles(search="cve-2026")) == 1
        assert len(repo.list_articles(search="REPORT")) == 1
        # since 窓 (未来からは 0 件)
        assert repo.list_articles(since=_now() + timedelta(hours=1)) == []
        assert len(repo.list_articles(since=_now() - timedelta(hours=1))) == 4

    def test_daily_post_counts_buckets_by_jst(self, repo: RunHistoryRepository) -> None:
        """M-1: 日次集計は JST 暦境界。UTC 16:00 (=JST 翌01:00) は JST の翌日に計上。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        # UTC 2026-06-04T16:00 = JST 2026-06-05T01:00 → JST 日付は 06-05
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="jst1",
                title="t",
                url="https://e.com/1",
                status="posted",
                created_at=datetime(2026, 6, 4, 16, 0, tzinfo=UTC),
            ),
        )
        counts = dict(repo.daily_post_counts(days=3650))
        assert counts.get("2026-06-05") == 1
        assert "2026-06-04" not in counts

    def test_list_articles_summary_search_and_entity_filter(
        self, repo: RunHistoryRepository
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a1",
                title="LockBit affiliate arrested",
                url="https://example.com/a1",
                feed_title="X",
                category="breach",
                summary="法執行機関が LockBit のアフィリエイトを逮捕した。",
                status="posted",
            ),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a2",
                title="Patch Tuesday roundup",
                url="https://example.com/a2",
                feed_title="X",
                category="vulnerability",
                summary="今月の更新まとめ。",
                status="posted",
            ),
        )
        repo.add_article_entities("a1", [("malware_family", "LockBit"), ("cve", "CVE-2026-9")])

        # summary 検索: title に無くても summary にヒット
        hit = repo.list_articles(search="逮捕")
        assert len(hit) == 1 and hit[0].article_id == "a1"
        # entity filter (malware_family、大小無視)
        mw = repo.list_articles(entity_type="malware_family", entity_value="lockbit")
        assert len(mw) == 1 and mw[0].article_id == "a1"
        # entity filter (cve)
        assert len(repo.list_articles(entity_type="cve", entity_value="CVE-2026-9")) == 1
        # 無関係な値は 0 件
        assert repo.list_articles(entity_type="malware_family", entity_value="emotet") == []
        # batch entity 取得
        emap = repo.entity_values_by_article(["a1", "a2"], "malware_family")
        assert emap.get("a1") == ["LockBit"] and "a2" not in emap

    def test_importance_breakdown_cyber_only(self, repo: RunHistoryRepository) -> None:
        """cyber_only=True は geopolitical 等の非サイバーを重要度分布から除外。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        rows = [
            ("apt", "high"),
            ("vulnerability", "high"),
            ("breach", "medium"),
            ("geopolitical", "high"),
            ("geopolitical", "high"),  # 戦略 high (除外対象)
            ("research", "low"),
        ]
        for i, (cat, imp) in enumerate(rows):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"id-{i}",
                    title="t",
                    url=f"u{i}",
                    category=cat,
                    importance=imp,
                    status="posted",
                ),
            )
        all_b = repo.importance_breakdown(days=1)
        cyber_b = repo.importance_breakdown(days=1, cyber_only=True)
        assert all_b.get("high") == 4  # apt+vuln+geo×2
        assert cyber_b.get("high") == 2  # apt+vuln のみ (geopolitical 除外)
        assert "low" not in cyber_b  # research(low) も除外


class TestAggregations:
    def test_daily_post_counts(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for _ in range(3):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="x",
                    title="t",
                    url="u",
                    importance="medium",
                    status="posted",
                ),
            )
        counts = repo.daily_post_counts(days=7)
        assert sum(c for _, c in counts) == 3

    def test_importance_breakdown(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for imp in ["high", "high", "medium", "low", "low", "low"]:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="x",
                    title="t",
                    url="u",
                    importance=imp,
                    status="posted",
                ),
            )
        breakdown = repo.importance_breakdown(days=7)
        assert breakdown == {"high": 2, "medium": 1, "low": 3}

    def test_extract_failure_rate_zero_total(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        assert repo.extract_failure_rate() == 0.0

    def test_extract_failure_rate_calculates_correctly(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for status in ["posted", "posted", "extract_failed", "posted"]:
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="x",
                    title="t",
                    url="u",
                    status=status,  # type: ignore[arg-type]
                ),
            )
        assert repo.extract_failure_rate() == pytest.approx(0.25)


class TestDeleteRuns:
    def test_delete_run_cascades_articles_and_logs(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=_now(),
            total_fetched=1,
            summarized=1,
            posted=1,
            marked_read=1,
            error_count=0,
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a-1",
                title="t",
                url="https://x.com/",
                status="posted",
            ),
        )
        repo.append_log_line(run_id, "hello world")

        deleted = repo.delete_run(run_id)
        assert deleted is True
        assert repo.get_run(run_id) is None
        assert repo.list_articles(run_id=run_id) == []
        assert repo.get_log_lines(run_id) == []

    def test_delete_run_returns_false_for_missing(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        assert repo.delete_run(99999) is False

    def test_delete_run_refuses_running_run(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        # finish_run を呼ばないので status='running' のまま
        assert repo.delete_run(run_id) is False
        assert repo.get_run(run_id) is not None

    def test_delete_runs_older_than_purges_old_runs_only(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # 古い run (40 日前) と新しい run (今日) を作る
        old_run = repo.start_run(
            RunRecord(
                started_at=_now() - timedelta(days=40),
                pipeline="daily",
                dry_run=False,
            ),
        )
        repo.finish_run(
            old_run,
            status="succeeded",
            finished_at=_now() - timedelta(days=40),
            total_fetched=0,
            summarized=0,
            posted=0,
            marked_read=0,
            error_count=0,
        )
        new_run = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.finish_run(
            new_run,
            status="succeeded",
            finished_at=_now(),
            total_fetched=0,
            summarized=0,
            posted=0,
            marked_read=0,
            error_count=0,
        )

        deleted = repo.delete_runs_older_than(days=30)
        assert deleted == 1
        assert repo.get_run(old_run) is None
        assert repo.get_run(new_run) is not None

    def test_delete_runs_older_than_keeps_running_runs(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # 40 日前から running のまま (dangling)
        run_id = repo.start_run(
            RunRecord(
                started_at=_now() - timedelta(days=40),
                pipeline="daily",
                dry_run=False,
            ),
        )
        deleted = repo.delete_runs_older_than(days=30)
        assert deleted == 0
        assert repo.get_run(run_id) is not None

    def test_delete_runs_older_than_rejects_negative_days(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        with pytest.raises(ValueError, match="days"):
            repo.delete_runs_older_than(days=-1)

    def test_vacuum_runs_without_error(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        # 何も書き込んでいない空 DB でも VACUUM は通る
        repo.vacuum()
        # 適当に書き込んで再度 VACUUM
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="x",
                title="t",
                url="u",
                status="posted",
            ),
        )
        repo.vacuum()

    def test_db_stats_returns_row_counts_and_size(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for _ in range(3):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id="x",
                    title="t",
                    url="u",
                    importance="medium",
                    status="posted",
                ),
            )
        repo.append_log_line(run_id, "hello")
        repo.mark_url_seen(url_hash="h1", url="https://x.com/")

        stats = repo.db_stats()
        assert stats["runs"] == 1
        assert stats["articles"] == 3
        assert stats["run_logs"] == 1
        assert stats["dedup_seen_urls"] == 1
        assert stats["article_embeddings"] == 0
        assert stats["file_size_bytes"] > 0

    def test_delete_run_preserves_dedup_entries(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """run 削除では dedup_seen_urls / article_embeddings は残す。"""
        from src.tools.url_normalizer import url_hash

        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=_now(),
            total_fetched=0,
            summarized=0,
            posted=0,
            marked_read=0,
            error_count=0,
        )

        url = "https://example.com/preserved"
        h = url_hash(url)
        repo.mark_url_seen(url_hash=h, url=url, title="kept")
        repo.add_article_embedding(
            url_hash=h,
            url=url,
            vector=[0.1, 0.2],
            model="m",
            title="kept",
        )

        repo.delete_run(run_id)

        # dedup 側は残っているはず
        assert repo.is_url_seen(h) is True
        assert repo.get_embedding(h) is not None


# ---------- Phase 5P: triage_error_count / partial_fetch カラム ----------


class TestRunRecordObservabilityFields:
    def test_run_record_has_triage_error_count_default_zero(self) -> None:
        run = RunRecord(started_at=_now(), pipeline="daily", dry_run=False)
        assert run.triage_error_count == 0

    def test_run_record_has_partial_fetch_default_false(self) -> None:
        run = RunRecord(started_at=_now(), pipeline="daily", dry_run=False)
        assert run.partial_fetch is False
        assert run.partial_fetch_count == 0

    def test_finish_run_persists_triage_error_count(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=_now(),
            total_fetched=5,
            summarized=5,
            posted=5,
            marked_read=5,
            error_count=0,
            triage_error_count=2,
        )
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.triage_error_count == 2

    def test_finish_run_persists_partial_fetch_flag_and_count(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.finish_run(
            run_id,
            status="failed",
            finished_at=_now(),
            total_fetched=12,
            summarized=0,
            posted=0,
            marked_read=0,
            error_count=1,
            partial_fetch=True,
            partial_fetch_count=12,
            note="partial_fetch=12/100",
        )
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.partial_fetch is True
        assert loaded.partial_fetch_count == 12
        assert loaded.note == "partial_fetch=12/100"

    def test_apply_migrations_idempotent_for_new_columns(
        self,
        tmp_path: Path,
    ) -> None:
        """二度の構築で ALTER TABLE が冪等に動くこと。"""
        db = tmp_path / "migrate.db"
        RunHistoryRepository(db_path=db)
        # 既に新カラムを含む schema だが、再オープンでもエラーにならない
        repo2 = RunHistoryRepository(db_path=db)
        run_id = repo2.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        loaded = repo2.get_run(run_id)
        assert loaded is not None
        assert loaded.triage_error_count == 0
        assert loaded.partial_fetch is False

    def test_apply_migrations_adds_columns_to_legacy_db(
        self,
        tmp_path: Path,
    ) -> None:
        """旧 schema (新カラム無し) の DB をオープンすると ALTER で追加される。"""
        import sqlite3

        db = tmp_path / "legacy.db"
        # 新カラムを含まない最小 runs テーブルを手動で作る
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at      TEXT    NOT NULL,
                finished_at     TEXT,
                pipeline        TEXT    NOT NULL,
                dry_run         INTEGER NOT NULL DEFAULT 0,
                triggered_by    TEXT    NOT NULL DEFAULT 'scheduler',
                status          TEXT    NOT NULL DEFAULT 'running',
                total_fetched   INTEGER NOT NULL DEFAULT 0,
                summarized      INTEGER NOT NULL DEFAULT 0,
                posted          INTEGER NOT NULL DEFAULT 0,
                marked_read     INTEGER NOT NULL DEFAULT 0,
                error_count     INTEGER NOT NULL DEFAULT 0,
                note            TEXT
            )
            """,
        )
        conn.commit()
        conn.close()

        RunHistoryRepository(db_path=db)
        # 直接 sqlite で table_info を確認 (リポジトリの内部 connection は使わない)
        check = sqlite3.connect(db)
        try:
            cols = {row[1] for row in check.execute("PRAGMA table_info(runs)")}
        finally:
            check.close()
        assert "triage_error_count" in cols
        assert "partial_fetch" in cols


class TestFindRecentPostByCve:
    """Phase 5T-V-2: 48h 以内同 CVE post 検索のテスト。"""

    def test_returns_none_when_no_match(self, repo: RunHistoryRepository) -> None:
        assert repo.find_recent_post_by_cve("cve-2026-9999") is None

    def test_exact_key_match(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:exact",
                title="Palo Alto RCE",
                url="https://example.com/p",
                importance="high",
                category="vulnerability",
                status="posted",
                posted_channel="brief",
                dedup_key="cve-2026-0300",
            ),
        )
        prior = repo.find_recent_post_by_cve("cve-2026-0300", within_hours=48)
        assert prior is not None
        assert prior.article_id == "tag:exact"

    def test_suffix_key_match(self, repo: RunHistoryRepository) -> None:
        """LLM 不安定問題: dedup_key='cve-2026-0300-palo-alto-rce' でもマッチ。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:suffix",
                title="Palo Alto RCE",
                url="https://example.com/s",
                importance="high",
                category="vulnerability",
                status="posted",
                posted_channel="brief",
                dedup_key="cve-2026-0300-palo-alto-rce",
            ),
        )
        prior = repo.find_recent_post_by_cve("cve-2026-0300", within_hours=48)
        assert prior is not None
        assert prior.dedup_key == "cve-2026-0300-palo-alto-rce"

    def test_outside_48h_window_not_matched(self, repo: RunHistoryRepository) -> None:
        """48h 超え再 post は続報として許容、検索ヒットしない。"""
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        old = _now() - timedelta(hours=72)
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:OLD",
                title="t",
                url="https://example.com/o",
                status="posted",
                posted_channel="brief",
                dedup_key="cve-2026-0300",
                created_at=old,
            ),
        )
        prior = repo.find_recent_post_by_cve("cve-2026-0300", within_hours=48, now=_now())
        assert prior is None

    def test_only_posted_status_matches(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:SK",
                title="t",
                url="https://example.com/sk",
                status="skipped_duplicate",
                posted_channel="brief",
                dedup_key="cve-2026-1111",
            ),
        )
        prior = repo.find_recent_post_by_cve("cve-2026-1111", within_hours=48)
        assert prior is None


class TestBriefCapCount:
    """Phase 5T-V: count_brief_in_window のテスト。"""

    def test_returns_zero_when_no_brief_posts(self, repo: RunHistoryRepository) -> None:
        assert repo.count_brief_in_window(hours=24) == 0

    def test_counts_only_brief_channel(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        for ch in ("brief", "brief", "watch", "alert", "japan_watch"):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"tag:{ch}-{_now().timestamp()}",
                    title="t",
                    url=f"https://example.com/{ch}",
                    status="posted",
                    posted_channel=ch,
                ),
            )
        # brief 2 件のみ
        assert repo.count_brief_in_window(hours=24) == 2

    def test_filters_by_window(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        now = _now()
        # 古い brief (30h 前) は cap=24h で除外される
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:OLD",
                title="t",
                url="https://example.com/old",
                status="posted",
                posted_channel="brief",
                created_at=now - timedelta(hours=30),
            ),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:NEW",
                title="t",
                url="https://example.com/new",
                status="posted",
                posted_channel="brief",
                created_at=now - timedelta(hours=1),
            ),
        )
        assert repo.count_brief_in_window(hours=24, now=now) == 1

    def test_excludes_non_posted_status(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="daily", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:SKIPPED",
                title="t",
                url="https://example.com/sk",
                status="skipped_duplicate",
                posted_channel="brief",
            ),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="tag:POSTED",
                title="t",
                url="https://example.com/p",
                status="posted",
                posted_channel="brief",
            ),
        )
        # status='posted' のみカウント
        assert repo.count_brief_in_window(hours=24) == 1


class TestF1SelectionRecording:
    """Phase 5T-T1: F1 (weekly deep dive) 選定履歴の永続化テスト。"""

    def _make_run(self, repo: RunHistoryRepository) -> int:
        return repo.start_run(
            RunRecord(started_at=_now(), pipeline="weekly-recap", dry_run=False),
        )

    def test_record_returns_inserted_count(self, repo: RunHistoryRepository) -> None:
        run_id = self._make_run(repo)
        selections = [
            F1SelectionRecord(
                run_id=run_id,
                article_id="tag:A1",
                dedup_key="apt41-japan-2026-05",
                composite_score=4.2,
                pir=5.0,
                roi=4.0,
                timeliness=4.0,
                novelty=3.0,
            ),
            F1SelectionRecord(
                run_id=run_id,
                article_id="tag:A2",
                dedup_key=None,
                composite_score=3.1,
                pir=3.0,
                roi=4.0,
                timeliness=2.0,
                novelty=3.0,
            ),
        ]
        n = repo.record_f1_selections(selections)
        assert n == 2

    def test_find_recent_dedup_keys_excludes_none(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = self._make_run(repo)
        repo.record_f1_selections(
            [
                F1SelectionRecord(
                    run_id=run_id,
                    article_id="tag:A1",
                    dedup_key="apt41-japan",
                    composite_score=4.0,
                    pir=5.0,
                    roi=4.0,
                    timeliness=3.0,
                    novelty=3.0,
                ),
                F1SelectionRecord(
                    run_id=run_id,
                    article_id="tag:A2",
                    dedup_key=None,
                    composite_score=3.0,
                    pir=3.0,
                    roi=3.0,
                    timeliness=3.0,
                    novelty=3.0,
                ),
            ],
        )
        keys = repo.find_recent_f1_dedup_keys(lookback_hours=24)
        assert keys == {"apt41-japan"}

    def test_find_recent_dedup_keys_filters_by_window(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = self._make_run(repo)
        now = _now()
        old_selected_at = now - timedelta(hours=200)  # 4 週 = 672h より新しいが 168h より古い
        repo.record_f1_selections(
            [
                F1SelectionRecord(
                    run_id=run_id,
                    article_id="tag:OLD",
                    dedup_key="old-incident",
                    composite_score=4.0,
                    pir=5.0,
                    roi=4.0,
                    timeliness=3.0,
                    novelty=3.0,
                    selected_at=old_selected_at,
                ),
                F1SelectionRecord(
                    run_id=run_id,
                    article_id="tag:NEW",
                    dedup_key="new-incident",
                    composite_score=4.0,
                    pir=5.0,
                    roi=4.0,
                    timeliness=3.0,
                    novelty=3.0,
                    selected_at=now - timedelta(hours=1),
                ),
            ],
        )
        # 168h window: NEW のみ
        keys_168 = repo.find_recent_f1_dedup_keys(lookback_hours=168, now=now)
        assert keys_168 == {"new-incident"}
        # 672h (4週) window: 両方
        keys_672 = repo.find_recent_f1_dedup_keys(lookback_hours=672, now=now)
        assert keys_672 == {"old-incident", "new-incident"}

    def test_find_recent_returns_empty_when_no_history(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        keys = repo.find_recent_f1_dedup_keys(lookback_hours=168)
        assert keys == set()

    def test_record_zero_selections_returns_zero(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        n = repo.record_f1_selections([])
        assert n == 0

    def test_cascade_delete_on_run_removal(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = self._make_run(repo)
        repo.record_f1_selections(
            [
                F1SelectionRecord(
                    run_id=run_id,
                    article_id="tag:A",
                    dedup_key="k1",
                    composite_score=4.0,
                    pir=5.0,
                    roi=4.0,
                    timeliness=3.0,
                    novelty=3.0,
                ),
            ],
        )
        assert repo.find_recent_f1_dedup_keys(lookback_hours=24) == {"k1"}
        # delete_run は running 状態を弾く仕様 → 先に finish させる
        repo.finish_run(
            run_id,
            status="succeeded",
            finished_at=_now(),
            total_fetched=1,
            summarized=1,
            posted=1,
            marked_read=0,
            error_count=0,
        )
        deleted = repo.delete_run(run_id)
        assert deleted is True
        assert repo.find_recent_f1_dedup_keys(lookback_hours=24) == set()


class TestJobRunHistory:
    """実行履歴 (2026-07-07 実行状況一覧性): runs 優先 / job_run_log fallback。"""

    def test_pipeline_history_from_runs_rich(self, repo: RunHistoryRepository) -> None:
        for _ in range(3):
            rid = repo.start_run(
                RunRecord(started_at=_now(), pipeline="weekly-recap", dry_run=False)
            )
            repo.finish_run(
                rid,
                status="succeeded",
                finished_at=_now(),
                total_fetched=5,
                summarized=5,
                posted=3,
                marked_read=3,
                error_count=0,
            )
        runs = repo.runs_for_job("weekly-recap", limit=10)
        assert len(runs) == 3
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["posted"] == 3  # runs テーブルのリッチ列
        assert runs[0]["finished_at"] is not None

    def test_bespoke_history_from_job_run_log(self, repo: RunHistoryRepository) -> None:
        repo.record_job_run("pir-entity-rebuild", status="succeeded", detail="ok1")
        repo.record_job_run("pir-entity-rebuild", status="failed", detail="boom")
        runs = repo.runs_for_job("pir-entity-rebuild", limit=10)
        assert len(runs) == 2
        # 新しい順 (最後に入れた failed が先頭)
        assert runs[0]["status"] == "failed"
        assert runs[0]["detail"] == "boom"
        assert runs[0]["posted"] is None  # bespoke はメトリクス無し

    def test_record_job_run_keeps_latest_upsert_and_appends_history(
        self, repo: RunHistoryRepository
    ) -> None:
        repo.record_job_run("daily-maintenance", status="succeeded")
        repo.record_job_run("daily-maintenance", status="succeeded")
        # job_last_run は 1 行 (upsert)、job_run_log は 2 行 (append)
        last = repo.get_job_last_runs()
        assert last["daily-maintenance"]["status"] == "succeeded"
        assert len(repo.runs_for_job("daily-maintenance", limit=10)) == 2

    def test_empty_history_for_unknown_job(self, repo: RunHistoryRepository) -> None:
        assert repo.runs_for_job("never-ran", limit=10) == []

    def test_falls_back_to_job_last_run_when_log_empty(self, repo: RunHistoryRepository) -> None:
        # job_run_log 導入前に走った bespoke を模す: job_last_run だけ手で入れる
        with repo._connect() as conn:
            conn.execute(
                "INSERT INTO job_last_run (job_id, last_run_at, status, detail) "
                "VALUES (?, ?, ?, ?)",
                ("legacy-bespoke", "2026-07-01T00:00:00+00:00", "succeeded", "old"),
            )
        runs = repo.runs_for_job("legacy-bespoke", limit=10)
        assert len(runs) == 1
        assert runs[0]["status"] == "succeeded"
        assert runs[0]["detail"] == "old"

    def test_purge_old_job_run_log(self, repo: RunHistoryRepository) -> None:
        old = _now() - timedelta(days=40)
        repo.record_job_run("daily-heartbeat", status="succeeded", when=old)
        repo.record_job_run("daily-heartbeat", status="succeeded")  # 今
        purged = repo.purge_old_job_run_log(days=30)
        assert purged == 1
        assert len(repo.runs_for_job("daily-heartbeat", limit=10)) == 1
