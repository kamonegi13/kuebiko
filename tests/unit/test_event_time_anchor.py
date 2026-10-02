"""事象時刻の錨 (不変条件): 言及の時刻は「DB へ書いた時刻」ではなく「事象の時刻」。

2026-08-22 の根本原因: entity_event_times が article_entities.created_at
(= DB 書込時刻) を事象時刻として返していた。バックフィル (再抽出 / 別名昇格 /
intent・axes backfill) は過去記事へ当日の日付で行を書くため、この近似は破れる。
実測で言及の 44.3% が 1 日超・34.4% が 7 日超ずれており、週次 FC3 spike の 44% が
偽陽性、日次バーストは単日最大 42 件の幻を出していた。

週次 8 週窓では均されて見えにくいが、日次窓では支配的になる。時間尺度を変えて
データ源を再利用するときは、必ず時刻意味論を再検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "anchor.db")


def _article(
    repo: RunHistoryRepository,
    article_id: str,
    *,
    created: datetime,
    published: datetime | None = None,
    sector: str | None = None,
    country: str | None = None,
    duplicate_of: str | None = None,
) -> None:
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=article_id,
            title="t",
            url=f"https://kuebiko.example/{article_id}",
            status="posted",
            category="incident",
            created_at=created,
            published_at=published,
            victim_sector_canonical=sector,
            victim_country_iso=country,
            duplicate_of=duplicate_of,
        )
    )


class TestEntityEventTimeAnchor:
    def test_backfilled_entity_reports_article_time_not_write_time(
        self, repo: RunHistoryRepository
    ) -> None:
        """核心の不変条件: 当日書いた entity でも、記事が古ければ古い時刻を返す。"""
        # Arrange — 2 か月前の記事に、今日 entity を書く (別名昇格 backfill と同じ形)
        old = _NOW - timedelta(days=60)
        _article(repo, "a1", created=old, published=old)
        repo.add_article_entities("a1", [("actor", "unc1151")], when=_NOW)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=90))

        # Assert — 今日の急増として数えてはならない
        assert len(times["unc1151"]) == 1
        assert abs((times["unc1151"][0] - old).total_seconds()) < 3600

    def test_falls_back_to_ingest_time_when_published_is_null(
        self, repo: RunHistoryRepository
    ) -> None:
        # Arrange
        ingested = _NOW - timedelta(days=3)
        _article(repo, "a1", created=ingested, published=None)
        repo.add_article_entities("a1", [("actor", "lazarus")], when=ingested)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert
        assert abs((times["lazarus"][0] - ingested).total_seconds()) < 3600

    def test_published_after_ingest_is_clamped_to_ingest(self, repo: RunHistoryRepository) -> None:
        """取込より未来の published_at は不正データ (実測 0.5%)。取込時刻へ丸める。"""
        # Arrange
        ingested = _NOW - timedelta(days=3)
        _article(repo, "a1", created=ingested, published=_NOW + timedelta(days=30))
        repo.add_article_entities("a1", [("actor", "lazarus")], when=ingested)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert — 未来へ飛ばさない
        assert abs((times["lazarus"][0] - ingested).total_seconds()) < 3600

    def test_duplicate_article_rows_do_not_inflate_mentions(
        self, repo: RunHistoryRepository
    ) -> None:
        """articles は同一 article_id が複数行ありうる (実測 3,593 行)。join で水増ししない。"""
        # Arrange — 同じ article_id を 2 回 add (取込リトライ / skipped_duplicate 相当)
        t = _NOW - timedelta(days=2)
        _article(repo, "a1", created=t, published=t)
        _article(repo, "a1", created=t + timedelta(minutes=5), published=t)
        repo.add_article_entities("a1", [("actor", "qilin")], when=t)

        # Act
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert
        assert len(times["qilin"]) == 1

    def test_since_filters_on_event_time(self, repo: RunHistoryRepository) -> None:
        """窓の判定も事象時刻で行う (書込時刻で切ると古い記事が窓に入り込む)。"""
        # Arrange — 記事は 60 日前、entity は今日書かれた
        old = _NOW - timedelta(days=60)
        _article(repo, "a1", created=old, published=old)
        repo.add_article_entities("a1", [("actor", "apt29")], when=_NOW)

        # Act — 直近 30 日窓
        times = repo.entity_event_times("actor", since=_NOW - timedelta(days=30))

        # Assert — 事象は窓外なので入らない
        assert "apt29" not in times


class TestVictimStreamAnchor:
    def test_sector_stream_uses_event_time(self, repo: RunHistoryRepository) -> None:
        # Arrange
        old = _NOW - timedelta(days=40)
        _article(repo, "a1", created=old, published=old, sector="healthcare")

        # Act
        times = repo.victim_sector_event_times(since=_NOW - timedelta(days=90))

        # Assert
        assert "healthcare" in times
        assert abs((times["healthcare"][0] - old).total_seconds()) < 3600

    def test_sector_stream_dedupes_article_rows(self, repo: RunHistoryRepository) -> None:
        # Arrange
        t = _NOW - timedelta(days=2)
        _article(repo, "a1", created=t, published=t, sector="finance")
        _article(repo, "a1", created=t + timedelta(minutes=5), published=t, sector="finance")

        # Act
        times = repo.victim_sector_event_times(since=_NOW - timedelta(days=30))

        # Assert
        assert len(times["finance"]) == 1

    def test_kept_duplicates_are_not_counted(self, repo: RunHistoryRepository) -> None:
        """重複として分析に残した記事 (2026-10-02) は二重計上になるので数えない。"""
        # Arrange
        t = _NOW - timedelta(days=1)
        _article(repo, "orig", created=t, published=t, sector="energy", country="US")
        _article(
            repo, "dup", created=t, published=t, sector="energy", country="US", duplicate_of="orig"
        )

        # Act
        sectors = repo.victim_sector_event_times(since=_NOW - timedelta(days=30))
        countries = repo.victim_country_event_times(since=_NOW - timedelta(days=30))

        # Assert
        assert len(sectors["energy"]) == 1
        assert len(countries["US"]) == 1


class TestBurstCloseOutWindow:
    """close-out の検証窓はバースト当日だけを除く (境界を固定する)。"""

    def test_same_day_excluded_next_day_included(self) -> None:
        # Arrange
        from datetime import timedelta as _td

        from src.forecast import burst

        start = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
        window_start = start + _td(days=1)
        window_end = start + _td(days=burst.VERIFY_AFTER_DAYS)
        same_day = start + _td(hours=13)  # バースト当日 → 除外
        next_day_00 = window_start  # 翌日 00:00 ちょうど → 含む
        last = window_end  # 窓末 → 含む
        after = window_end + _td(seconds=1)  # 窓外

        # Act
        cands = (same_day, next_day_00, last, after)
        counted = [t for t in cands if window_start <= t <= window_end]

        # Assert
        assert counted == [next_day_00, last]


class TestAnchorLiteralSingleSource:
    """錨式のリテラルは event_time.py にのみ存在する (複製 = 移植漏れの芽)。"""

    def test_anchor_literal_exists_only_in_event_time_module(self) -> None:
        marker = "published_at IS NOT NULL AND {a}.published_at"
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders = [
            path
            for path in src_root.rglob("*.py")
            if marker in path.read_text(encoding="utf-8") and path.name != "event_time.py"
        ]
        assert offenders == [], f"錨式の複製を検出: {offenders} (event_time.py を import すること)"

    def test_dedup_articles_literal_exists_only_in_event_time_module(self) -> None:
        marker = "MIN(published_at) AS published_at FROM articles GROUP BY article_id"
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders = [
            path
            for path in src_root.rglob("*.py")
            if marker in path.read_text(encoding="utf-8") and path.name != "event_time.py"
        ]
        assert offenders == [], (
            f"畳み込み式の複製を検出: {offenders} (event_time.py を import すること)"
        )


def test_article_list_orders_by_event_time_but_filters_by_ingest(tmp_path: object) -> None:
    """一覧は **事象時刻で並び**、絞り込みは **取得時刻** で行うこと。

    2026-08-24 利用者指摘: 一覧が created_at (取得時刻) 順なのに published_at (公開時刻)
    を表示していたため、時刻が前後して見えた。ANSSI のようにその日の advisory を
    まとめて後から配信する媒体で顕著 (実測: 公開と取得の差は 6h 超が 10%)。

    並びだけを事象時刻にする。絞り込みまで事象時刻にすると「公開は古いが取得は今」の
    記事が直近窓から消えて **見落とし** になる。
    """
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "order.db")  # type: ignore[operator]
    now = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (id, started_at, pipeline, dry_run, status)"
            " VALUES (1, ?, 'p', 0, 'done')",
            (now.isoformat(),),
        )
        rows = [
            # (id, 公開, 取得) — 後追い配信: 公開は古いが取得は最新
            ("late", now - timedelta(hours=13), now),
            ("fresh", now - timedelta(hours=1), now - timedelta(minutes=30)),
        ]
        for aid, pub, created in rows:
            conn.execute(
                "INSERT INTO articles (run_id, article_id, url, title, summary, body,"
                " importance, category, status, feed_title, feed_url, published_at, created_at)"
                " VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    f"https://kuebiko.example/{aid}",
                    aid,
                    "s",
                    "b",
                    "high",
                    "vuln",
                    "posted",
                    "F",
                    "https://kuebiko.example/f",
                    pub.isoformat(),
                    created.isoformat(),
                ),
            )

    # 並び = 事象時刻の降順 (公開が新しい fresh が先)
    got = repo.list_articles(limit=10)
    assert [a.article_id for a in got] == ["fresh", "late"]

    # 絞り込みは取得時刻。直近 2h で切っても後追い配信は残る (見落とさない)
    recent = repo.list_articles(since=now - timedelta(hours=2), limit=10)
    assert {a.article_id for a in recent} == {"fresh", "late"}
