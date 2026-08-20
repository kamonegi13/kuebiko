"""攻撃者面 (国家情勢ボード) の母集団反転テスト (2026-08-20)。

`_attacker_face_rows` が articles.subject_actor_ids (主題評価済み) × 脅威スコープ
(_CYBER_CATS) 起点で母集団を作ることを検証する。旧実装は article_entities の言及
(mention) を数えており、実測 30日窓で全国家に水増しが確認された
(ru 348→実態119 / cn 101→31 / us 32→0)。本テストはその反転の回帰防止。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.situation import list_nations, situation_by_nation


def _start_run(repo: RunHistoryRepository) -> int:
    return repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))


def _article(article_id: str, *, category: str = "apt", **kw: object) -> ArticleRecord:
    """テスト記事の共通ビルダー (tests/unit/test_assessment.py の art(**kw) イディオムに倣う)。

    run_id / created_at / published_at / subject_actor_ids 等 テストごとに変わる値は
    呼び出し側が **kw で渡す。
    """
    return ArticleRecord(
        article_id=article_id,
        title=f"{article_id} {category}",
        url=f"https://e/{article_id}",
        category=category,
        status="posted",
        **kw,  # type: ignore[arg-type]
    )


def test_mention_only_article_excluded_from_attacker_face(tmp_path: Path) -> None:
    """言及のみ (評価済みだが主題なし) の記事は攻撃者面に出ない (今回のバグの fixture 化)。"""
    db = tmp_path / "sit_mention_only.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "mo1",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="",
            subject_actor_source="none",
        )
    )
    repo.add_article_entities("mo1", [("actor", "volt_typhoon")], when=now)

    s = situation_by_nation("CN", window_days=None, db_path=db)
    assert s["cyber"]["total"] == 0
    assert s["cyber"]["actors"] == []


def test_subject_actor_apt_article_counted_in_attacker_face(tmp_path: Path) -> None:
    """subject_actor_ids に該当 actor + 脅威スコープ内カテゴリなら攻撃者面に出て
    top actors に数えられる。
    """
    db = tmp_path / "sit_subject_apt.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "s1",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )

    s = situation_by_nation("CN", window_days=None, db_path=db)
    assert s["cyber"]["total"] == 1
    assert any(a["actor_id"] == "volt_typhoon" and a["count"] == 1 for a in s["cyber"]["actors"])


def test_subject_actor_policy_article_excluded_out_of_threat_scope(tmp_path: Path) -> None:
    """subject 一致でも category が脅威スコープ外 (policy = 国家機関の政策記事型) なら出ない。"""
    db = tmp_path / "sit_policy.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "p1",
            category="policy",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )

    s = situation_by_nation("CN", window_days=None, db_path=db)
    assert s["cyber"]["total"] == 0


def test_state_organ_actor_subject_breach_included(tmp_path: Path) -> None:
    """IRGC/GRU 型 (family=state_organ) の actor も除外しない — family によるフィルタは行わない。"""
    db = tmp_path / "sit_state_organ.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "so1",
            category="breach",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="russia_gru",
            subject_actor_source="llm",
        )
    )

    s = situation_by_nation("RU", window_days=None, db_path=db)
    assert s["cyber"]["total"] == 1
    assert any(a["actor_id"] == "russia_gru" for a in s["cyber"]["actors"])


def test_attacker_face_population_is_internally_consistent(tmp_path: Path) -> None:
    """一覧 (total) と内訳 (actors) が同一母集団 (cyber_rows) から構成的に導出される。"""
    db = tmp_path / "sit_consistency.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    for i in range(2):
        repo.add_article(
            _article(
                f"c{i}",
                run_id=rid,
                created_at=now,
                published_at=now,
                subject_actor_ids="volt_typhoon",
                subject_actor_source="llm",
            )
        )
    # 言及のみ (主題は無し) の記事は分母に混ざらない。
    repo.add_article(
        _article(
            "mo",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="",
            subject_actor_source="none",
        )
    )
    repo.add_article_entities("mo", [("actor", "volt_typhoon")], when=now)

    s = situation_by_nation("CN", window_days=None, db_path=db)
    total = s["cyber"]["total"]
    assert total == 2
    counts = {a["actor_id"]: a["count"] for a in s["cyber"]["actors"]}
    assert counts.get("volt_typhoon") == 2
    # この不等式は本 fixture 固有 (各記事の subject actor が volt_typhoon 1 件のみ) の下で
    # 成り立つ。同一国の複数 actor が主題の記事 (subject_actor_ids="a,b" 等) では 1 記事が
    # 複数 actor の count に加算されるため sum(counts) > total になり得るのが正しい仕様。
    assert sum(counts.values()) <= total
    assert all(r["article_id"] != "mo" for r in s["cyber"]["recent"])


def test_legacy_row_without_subject_evaluation_excluded_even_with_mention(tmp_path: Path) -> None:
    """subject_actor_source が NULL (legacy 未評価) の行は、言及があっても攻撃者面に出ない。

    旧実装の mention fallback は本反転で意図的に廃止 (実測で gate 後件数≒subject 起点件数の
    ため情報損失なしと判断)。
    """
    db = tmp_path / "sit_legacy.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "lg1",
            run_id=rid,
            created_at=now,
            published_at=now,
        )
    )
    repo.add_article_entities("lg1", [("actor", "volt_typhoon")], when=now)

    s = situation_by_nation("CN", window_days=None, db_path=db)
    assert s["cyber"]["total"] == 0


def test_window_since_excludes_older_subject_articles(tmp_path: Path) -> None:
    """窓 (since) が攻撃者面にも効く。"""
    db = tmp_path / "sit_window.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    repo.add_article(
        _article(
            "old1",
            run_id=rid,
            created_at=old,
            published_at=old,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )
    repo.add_article(
        _article(
            "new1",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )

    s = situation_by_nation("CN", window_days=7, db_path=db)
    assert s["cyber"]["total"] == 1
    assert any(r["article_id"] == "new1" for r in s["cyber"]["recent"])
    assert all(r["article_id"] != "old1" for r in s["cyber"]["recent"])


def test_list_nations_cyber_count_matches_subject_population(tmp_path: Path) -> None:
    """list_nations の cyber 件数も同じ母集団 (言及のみ記事を作っても増えない)。"""
    db = tmp_path / "sit_list_nations.db"
    repo = RunHistoryRepository(db_path=db)
    rid = _start_run(repo)
    now = datetime.now(UTC)
    repo.add_article(
        _article(
            "ln1",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="volt_typhoon",
            subject_actor_source="llm",
        )
    )
    nations_before = {n["iso"]: n["cyber"] for n in list_nations(window_days=None, db_path=db)}
    assert nations_before.get("CN", 0) == 1

    # 言及のみの記事を追加しても増えない。
    repo.add_article(
        _article(
            "ln2",
            run_id=rid,
            created_at=now,
            published_at=now,
            subject_actor_ids="",
            subject_actor_source="none",
        )
    )
    repo.add_article_entities("ln2", [("actor", "volt_typhoon")], when=now)
    nations_after = {n["iso"]: n["cyber"] for n in list_nations(window_days=None, db_path=db)}
    assert nations_after.get("CN", 0) == 1
