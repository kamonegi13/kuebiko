"""geo_cyber_map のヘルパ + カバレッジ信頼層 (Phase 1) の unit テスト。

純粋ヘルパ (threat_class / source_status 句、confidence tier、source label) と、
build_cyber_map / country_articles の node 信頼層フィールド・収集経路フィルタを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.api.geo import _parse_isos
from src.ui.services.geo_cyber_map import (
    _class_clause,
    _confidence_tier,
    _importance_clause,
    _short_source_label,
    _status_filter,
    build_cyber_map,
    country_articles,
    sub_country_points,
    trend,
)


# ───────── threat_class 句 ─────────
def test_class_clause_ransomware() -> None:
    assert _class_clause("ransomware") == " AND is_ransomware=1"


def test_class_clause_other_includes_null() -> None:
    # その他 = ランサム以外。旧データ (is_ransomware IS NULL) も「その他」に含める。
    assert _class_clause("other") == " AND (is_ransomware=0 OR is_ransomware IS NULL)"


def test_class_clause_all_is_empty() -> None:
    assert _class_clause("all") == ""


def test_class_clause_respects_column_prefix() -> None:
    assert _class_clause("ransomware", "a.is_ransomware") == " AND a.is_ransomware=1"


# ───────── source_status (報道/台帳) 句 ─────────
def test_status_filter_all_has_no_params() -> None:
    clause, params = _status_filter("all")
    assert clause == "status IN ('posted', 'collected')"
    assert params == []


def test_status_filter_posted() -> None:
    assert _status_filter("posted") == ("status = ?", ["posted"])


def test_status_filter_collected() -> None:
    assert _status_filter("collected") == ("status = ?", ["collected"])


def test_status_filter_respects_column_prefix() -> None:
    clause, params = _status_filter("posted", "a.status")
    assert clause == "a.status = ?"
    assert params == ["posted"]


# ───────── confidence tier (実データ確定の閾値) ─────────
def test_confidence_thin_by_few_sources() -> None:
    # ソース ≤2 は単一/少数 → 薄い (シェアに関わらず)。
    assert _confidence_tier(1, 1.0) == "thin"
    assert _confidence_tier(2, 0.3) == "thin"


def test_confidence_thin_by_dominance_even_with_many_sources() -> None:
    # ソースは多いが 1 つが 70% 以上 → monoculture で薄い (明るいが脆い)。
    assert _confidence_tier(6, 0.70) == "thin"
    assert _confidence_tier(10, 0.85) == "thin"


def test_confidence_rich_requires_many_sources_and_no_dominance() -> None:
    assert _confidence_tier(5, 0.4) == "rich"
    assert _confidence_tier(8, 0.2) == "rich"


def test_confidence_medium_is_the_middle() -> None:
    assert _confidence_tier(3, 0.5) == "medium"
    assert _confidence_tier(4, 0.69) == "medium"


# ───────── source label の導出 ─────────
def test_short_source_label_prefers_feed_title() -> None:
    label = _short_source_label("https://api.ransomware.live/v2", "ransomware.live")
    assert label == "ransomware.live"


def test_short_source_label_falls_back_to_host() -> None:
    assert _short_source_label("https://api.ransomware.live/v2", None) == "api.ransomware.live"


def test_short_source_label_unknown_when_empty() -> None:
    assert _short_source_label(None, None) == "(不明ソース)"


# ───────── build_cyber_map / country_articles の統合 ─────────
def _seed_repo(tmp_path) -> str:  # type: ignore[no-untyped-def]
    """JP(多源=rich) / US(単一・台帳のみ=thin) / AU(支配ソース=thin) を投入した DB を返す。"""
    db = tmp_path / "geo.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="test", dry_run=True))

    def art(aid: str, iso: str, feed: str, status: str) -> ArticleRecord:
        return ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title=f"{iso} ransomware attack {aid}",
            url=f"https://x.example/{aid}",
            feed_title=feed.split("//")[-1],
            feed_url=feed,
            category="breach",
            status=status,  # type: ignore[arg-type]
            summary="incident summary",
            victim_country_iso=iso,
            victim_sector_canonical="saas",
            published_at=datetime.now(UTC),
        )

    # JP: 5 distinct sources × 1 = 厚い (source_count=5, share=0.2)
    for i in range(5):
        repo.add_article(art(f"jp{i}", "JP", f"https://feed{i}.jp/rss", "posted"))
    # US: 1 source × 3、すべて collected (台帳のみ) = 薄い
    for i in range(3):
        repo.add_article(art(f"us{i}", "US", "https://only.us/rss", "collected"))
    # AU: feedA×7 + B,C,D×1 = 10 (source_count=4, share=0.7) → 支配で薄い
    for i in range(7):
        repo.add_article(art(f"auA{i}", "AU", "https://feedA.au/rss", "posted"))
    for tag in ("B", "C", "D"):
        repo.add_article(art(f"au{tag}", "AU", f"https://feed{tag}.au/rss", "posted"))
    return str(db)


def test_geo_events_aggregate_intent_and_pmesii(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """地政学レイヤー: 国別の支配動機 (top_intent) + 内訳、PMESII 直交フィルタ、drill の intent。"""
    db = tmp_path / "geo_intent.db"
    repo = RunHistoryRepository(db_path=db)
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))

    def geo(
        aid: str, iso: str, intent: str, *, mil: bool = False, pol: bool = False
    ) -> ArticleRecord:
        return ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"{iso} geopolitical {aid}",
            url=f"https://x.example/{aid}",
            category="geopolitical",
            status="posted",
            victim_country_iso=iso,
            socio_political_intent=intent,
            pmesii_m=mil,
            pmesii_p=pol,
            published_at=datetime.now(UTC),
        )

    # CN: coercion×2 (軍事) + deterrence×1 (政治のみ)、JP: territorial×1 (軍事)
    repo.add_article(geo("c1", "CN", "coercion", mil=True))
    repo.add_article(geo("c2", "CN", "coercion", mil=True))
    repo.add_article(geo("c3", "CN", "deterrence", pol=True))
    repo.add_article(geo("j1", "JP", "territorial", mil=True))

    data = build_cyber_map(window_days=None, db_path=db)
    ge = {g["iso"]: g for g in data["geo_events"]}
    assert ge["CN"]["count"] == 3
    assert ge["CN"]["top_intent"] == "coercion"  # 2 vs 1 で支配
    assert {"intent": "coercion", "count": 2} in ge["CN"]["intents"]
    assert ge["JP"]["top_intent"] == "territorial"

    # PMESII=m (軍事) 直交フィルタ → 軍事軸の事象のみ (CN deterrence は政治のみで落ちる)
    mil = build_cyber_map(window_days=None, pmesii="m", db_path=db)
    gem = {g["iso"]: g for g in mil["geo_events"]}
    assert gem["CN"]["count"] == 2  # coercion×2 (軍事) のみ
    assert gem["JP"]["count"] == 1

    # drill (domain=geopolitical) は記事に動機を載せる
    drill = country_articles("CN", window_days=None, domain="geopolitical", db_path=db)
    assert any(a["socio_political_intent"] == "coercion" for a in drill["articles"])


def test_build_cyber_map_confidence_and_ledger(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed_repo(tmp_path)
    data = build_cyber_map(window_days=None, db_path=db)  # type: ignore[arg-type]
    by_iso = {n["iso"]: n for n in data["nodes"]}

    jp = by_iso["JP"]
    assert jp["count"] == 5
    assert jp["source_count"] == 5
    assert jp["confidence"] == "rich"
    assert jp["collected_count"] == 0 and jp["posted_count"] == 5

    us = by_iso["US"]
    assert us["count"] == 3
    assert us["source_count"] == 1
    assert us["confidence"] == "thin"
    # 報道が無く台帳のみ → 「台帳のみ」タグの条件 (posted=0, collected>0)。
    assert us["posted_count"] == 0 and us["collected_count"] == 3

    au = by_iso["AU"]
    assert au["confidence"] == "thin"  # source_count=4 だが単一ソース 70% で薄い
    assert au["top_source_share"] == 0.7


def test_build_cyber_map_source_status_filter(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed_repo(tmp_path)
    # 報道のみ: US (collected のみ) は消え、JP/AU は残る。
    posted = build_cyber_map(window_days=None, source_status="posted", db_path=db)  # type: ignore[arg-type]
    assert {n["iso"] for n in posted["nodes"]} == {"JP", "AU"}
    # 台帳のみ: US だけ。
    collected = build_cyber_map(window_days=None, source_status="collected", db_path=db)  # type: ignore[arg-type]
    assert {n["iso"] for n in collected["nodes"]} == {"US"}


def test_country_articles_exposes_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed_repo(tmp_path)
    res = country_articles("US", window_days=None, db_path=db)  # type: ignore[arg-type]
    assert res["count"] == 3
    assert all(a["status"] == "collected" for a in res["articles"])
    # 収集経路フィルタ: 報道のみ → US は 0 件。
    posted = country_articles("US", window_days=None, source_status="posted", db_path=db)  # type: ignore[arg-type]
    assert posted["count"] == 0


# ───────── P2: 地域フィルタ (trend isos) ─────────
def test_trend_region_filter_by_isos(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed_repo(tmp_path)
    full = trend(window_days=None, group_by="country", db_path=db)  # type: ignore[arg-type]
    assert {"JP", "US", "AU"} <= {s["key"] for s in full["series"]}
    # JP + AU の地域に絞る → US は系列から消える。
    region = trend(window_days=None, group_by="country", isos=["JP", "AU"], db_path=db)  # type: ignore[arg-type]
    keys = {s["key"] for s in region["series"]}
    assert "US" not in keys
    assert keys <= {"JP", "AU", "_other"}


def test_parse_isos_validates_alpha2() -> None:
    assert _parse_isos("jp, us , au") == ["JP", "US", "AU"]
    # 非 alpha2 (空 / 数字 / 3桁) は捨てる。
    assert _parse_isos("JP,1,USA,,KR") == ["JP", "KR"]


# ───────── 意義の地図 (A): importance しきい値 ─────────
def test_importance_clause_levels() -> None:
    assert _importance_clause("all") == ""
    assert _importance_clause("high") == " AND importance = 'high'"
    assert _importance_clause("medium_up") == " AND importance IN ('high', 'medium')"
    assert _importance_clause("high", "a.importance") == " AND a.importance = 'high'"


def test_build_cyber_map_importance_filter(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "imp.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))

    def art(aid: str, importance: str) -> ArticleRecord:
        return ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title=f"JP attack {aid}",
            url=f"https://x.example/{aid}",
            feed_url="https://f.jp/rss",
            category="breach",
            status="posted",
            importance=importance,
            summary="incident",
            victim_country_iso="JP",
            victim_sector_canonical="saas",
            published_at=datetime.now(UTC),
        )

    # JP: high×2 + medium×3 + low×4 = 全件9
    for i in range(2):
        repo.add_article(art(f"h{i}", "high"))
    for i in range(3):
        repo.add_article(art(f"m{i}", "medium"))
    for i in range(4):
        repo.add_article(art(f"l{i}", "low"))

    def jp_count(min_importance: str) -> int:
        data = build_cyber_map(window_days=None, min_importance=min_importance, db_path=db)
        node = next((n for n in data["nodes"] if n["iso"] == "JP"), None)
        return node["count"] if node else 0

    assert jp_count("all") == 9
    assert jp_count("medium_up") == 5  # high2 + medium3
    assert jp_count("high") == 2


# ───────── アクター中心ビュー (flow): 主題起点への反転 (2026-08-21) ─────────
# 国家情勢ボードの攻撃者面 (75c7dee, 2026-08-20) と同じ欠陥 (article_entities の言及
# (mention) actor を数えると記事の主題でないアクターが混入する) が地図の flow にもあった
# ため、articles.subject_actor_ids 起点に反転した。以下は反転の回帰防止。


def test_flow_actors_excludes_mention_only_article(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """言及のみ (評価済みだが主題なし) の記事は actors (flow ペア) に出ない。"""
    db = tmp_path / "flow_mention_only.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="mo1",
            title="JP breach mo1",
            url="https://x.example/mo1",
            category="breach",
            status="posted",
            victim_country_iso="JP",
            published_at=datetime.now(UTC),
            subject_actor_ids="",
            subject_actor_source="none",
        )
    )
    repo.add_article_entities("mo1", [("actor", "volt_typhoon")])

    data = build_cyber_map(window_days=None, db_path=db)
    actor_ids = {a["actor_id"] for a in data["actors"]}
    assert "volt_typhoon" not in actor_ids
    assert data["actors"] == []


def test_flow_actors_includes_subject_match_in_scoped_category(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """subject 一致 + 対象 (cyber-attack) カテゴリの記事は actors (flow ペア) に出る。"""
    db = tmp_path / "flow_subject_match.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="s1",
            title="JP breach s1",
            url="https://x.example/s1",
            category="breach",
            status="posted",
            victim_country_iso="JP",
            published_at=datetime.now(UTC),
            subject_actor_ids="lazarus",
            subject_actor_source="llm",
        )
    )

    data = build_cyber_map(window_days=None, db_path=db)
    lazarus = next((a for a in data["actors"] if a["actor_id"] == "lazarus"), None)
    assert lazarus is not None
    assert lazarus["total"] == 1
    assert {v["iso"] for v in lazarus["victims"]} == {"JP"}


# ───────── Phase 1b 後半: 都市・地域ピン (sub_country_points) ─────────
# 国バブル (nodes) とは別レイヤー。victim_city entity を CITY tier (geo_cities) →
# ADMIN1 tier (geo_admin1) の順で解決し、同一座標に集約したピンを返す。


def _seed_geo_gazetteers(db_path: str) -> None:
    """geo_cities / geo_admin1 を最小構成で seed する (test 専用オフライン gazetteer)。"""
    import sqlite3

    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS geo_cities (country_code TEXT, name_lower TEXT, "
        "lat DOUBLE PRECISION, lon DOUBLE PRECISION, population BIGINT, "
        "PRIMARY KEY(country_code, name_lower))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS geo_admin1 (country_code TEXT, name_lower TEXT, "
        "admin1_code TEXT, lat DOUBLE PRECISION, lon DOUBLE PRECISION, population BIGINT, "
        "PRIMARY KEY(country_code, name_lower))"
    )
    con.execute("INSERT INTO geo_cities VALUES (?,?,?,?,?)", ("JP", "osaka", 34.69, 135.5, 2753862))
    con.execute(
        "INSERT INTO geo_admin1 VALUES (?,?,?,?,?,?)",
        ("US", "minnesota", "US.MN", 44.98, -93.27, 429954),
    )
    con.commit()
    con.close()


def _seed_victim_city(repo: RunHistoryRepository, article_id: str, city: str) -> None:
    """article_entities に victim_city entity を直接 seed する (test 専用)。"""
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
            " VALUES (?, 'victim_city', ?, ?)",
            (article_id, city, datetime.now(UTC).isoformat()),
        )


def _sub_geo_article(run_id: int, aid: str, iso: str, **overrides: object) -> ArticleRecord:
    base: dict[str, object] = {
        "run_id": run_id,
        "article_id": aid,
        "title": f"{iso} breach {aid}",
        "url": f"https://x.example/{aid}",
        "category": "breach",
        "status": "posted",
        "summary": "incident",
        "victim_country_iso": iso,
        "published_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ArticleRecord(**base)  # type: ignore[arg-type]


def test_sub_country_points_resolves_city_and_admin1(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """都市名は CITY tier、州名は ADMIN1 tier で解決され、別ピンとして返る。"""
    # Arrange
    db = tmp_path / "subgeo_city_admin1.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(_sub_geo_article(run_id, "jp1", "JP"))
    _seed_victim_city(repo, "jp1", "Osaka")
    repo.add_article(_sub_geo_article(run_id, "us1", "US"))
    _seed_victim_city(repo, "us1", "Minnesota")
    _seed_geo_gazetteers(str(db))

    # Act
    data = sub_country_points(window_days=None, db_path=db)

    # Assert
    by_precision = {p["precision"]: p for p in data["points"]}
    assert by_precision["city"]["count"] == 1
    assert round(by_precision["city"]["lat"]) == 35  # Osaka 近傍
    assert by_precision["admin1"]["count"] == 1
    assert round(by_precision["admin1"]["lat"]) == 45  # Minnesota 代表点近傍
    assert data["total_points"] == 2
    assert data["total_articles"] == 2
    assert data["unresolved_count"] == 0


def test_sub_country_points_aggregates_same_coordinate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """同一都市の複数記事は 1 ピンに集約され、タイトルは上限 5 件に切り詰められる。"""
    # Arrange
    db = tmp_path / "subgeo_aggregate.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    for i in range(7):
        repo.add_article(_sub_geo_article(run_id, f"jp{i}", "JP", title=f"Osaka breach {i}"))
        _seed_victim_city(repo, f"jp{i}", "Osaka")
    _seed_geo_gazetteers(str(db))

    # Act
    data = sub_country_points(window_days=None, db_path=db)

    # Assert
    assert len(data["points"]) == 1
    pin = data["points"][0]
    assert pin["count"] == 7
    assert len(pin["titles"]) == 5  # ポップアップ上限で切り詰め
    assert len(pin["article_ids"]) == 5


def test_sub_country_points_unresolved_city_excluded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """gazetteer に無い地名は plot されず、unresolved_count に計上される (誠実性)。"""
    # Arrange
    db = tmp_path / "subgeo_unresolved.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(_sub_geo_article(run_id, "jp1", "JP", title="架空都市 breach"))
    _seed_victim_city(repo, "jp1", "架空都市")
    _seed_geo_gazetteers(str(db))

    # Act
    data = sub_country_points(window_days=None, db_path=db)

    # Assert
    assert data["points"] == []
    assert data["unresolved_count"] == 1


def test_sub_country_points_importance_filter(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """国バブルと同じ importance しきい値が sub_country_points にも効く。"""
    # Arrange
    db = tmp_path / "subgeo_importance.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(
        _sub_geo_article(run_id, "h1", "JP", title="Osaka breach h1", importance="high")
    )
    _seed_victim_city(repo, "h1", "Osaka")
    repo.add_article(
        _sub_geo_article(run_id, "l1", "JP", title="Osaka breach l1", importance="low")
    )
    _seed_victim_city(repo, "l1", "Osaka")
    _seed_geo_gazetteers(str(db))

    # Act
    all_data = sub_country_points(window_days=None, db_path=db)
    high_only = sub_country_points(window_days=None, min_importance="high", db_path=db)

    # Assert
    assert all_data["points"][0]["count"] == 2
    assert high_only["points"][0]["count"] == 1


def test_sub_country_points_excludes_accidental_leak(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """運用ミス (誤送信等) の記事は国バブルと同様にピンからも除外される。"""
    # Arrange
    db = tmp_path / "subgeo_accidental.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(
        _sub_geo_article(
            run_id,
            "jp1",
            "JP",
            title="Osaka 市役所が個人情報を誤送信",
            summary="運用ミスによる情報漏洩",
        )
    )
    _seed_victim_city(repo, "jp1", "Osaka")
    _seed_geo_gazetteers(str(db))

    # Act
    data = sub_country_points(window_days=None, db_path=db)

    # Assert
    assert data["points"] == []


def test_sub_country_points_does_not_affect_country_nodes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """sub_country_points は国バブル (build_cyber_map の nodes) の件数に影響しない。"""
    # Arrange
    db = tmp_path / "subgeo_country_unchanged.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    repo.add_article(_sub_geo_article(run_id, "jp1", "JP"))
    _seed_victim_city(repo, "jp1", "Osaka")
    _seed_geo_gazetteers(str(db))

    # Act
    country_data = build_cyber_map(window_days=None, db_path=db)

    # Assert: 国集計のレスポンス形は不変 (件数・キー構成とも既存のまま)。
    jp = next(n for n in country_data["nodes"] if n["iso"] == "JP")
    assert jp["count"] == 1
    assert jp["precision"] == "country"
