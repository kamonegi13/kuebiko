"""国家情勢ボード (situation) の相関集計テスト。

サイバー(actor.nation 経由) と地政学(involved_country 経由) が **国家** で相関されることを、
実 registry の既知アクター (Volt Typhoon→cn) を使って検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.situation import list_nations, situation_by_nation

_NOW = datetime.now(UTC)


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "sit.db"
    repo = RunHistoryRepository(db_path=db)
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    now = datetime.now(UTC)

    def art(aid: str, cat: str, intent: str) -> ArticleRecord:
        return ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=f"{aid} {cat}",
            url=f"https://e/{aid}",
            category=cat,
            status="posted",
            socio_political_intent=intent,
            pmesii_m=True,
            created_at=now,
            published_at=now,
        )

    # サイバー: 中国系 APT (Volt Typhoon = registry で nation=cn) の侵入。
    # 2026-08-20 反転: 攻撃者面は主題 (subject) 起点のため、mention だけでなく
    # subject_actor_ids/source も評価済みとして設定する (言及のみでは面に出ない)。
    cy1 = art("cy1", "apt", "prepositioning").model_copy(
        update={"subject_actor_ids": "volt_typhoon", "subject_actor_source": "llm"}
    )
    repo.add_article(cy1)
    repo.add_article_entities("cy1", [("actor", "volt_typhoon")], when=now)
    # 地政学: 中国が当事者
    repo.add_article(art("gp1", "geopolitical", "coercion"))
    repo.add_article_entities("gp1", [("involved_country", "CN")], when=now)
    # 無関係 (ロシア地政学) — CN に混ざらないこと
    repo.add_article(art("gp2", "geopolitical", "territorial"))
    repo.add_article_entities("gp2", [("involved_country", "RU")], when=now)

    # 標的レンズ: 日本を victim とする cyber 攻撃 (lazarus が政府機関を侵害)
    tgt = art("tg1", "breach", "financial")
    tgt = tgt.model_copy(
        update={"victim_country_iso": "JP", "victim_sector_canonical": "government"}
    )
    repo.add_article(tgt)
    repo.add_article_entities("tg1", [("actor", "lazarus")], when=now)
    return db


def test_situation_correlates_cyber_and_geopolitical_by_nation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed(tmp_path)
    s = situation_by_nation("CN", window_days=None, db_path=db)

    assert s["nation"] == "CN"
    # サイバー面: Volt Typhoon (actor.nation=cn) が拾える
    assert s["cyber"]["total"] == 1
    assert any(a["actor_id"] == "volt_typhoon" for a in s["cyber"]["actors"])
    # 地政学面: 中国当事の事象 (RU は混ざらない)
    assert s["geopolitical"]["total"] == 1
    assert s["geopolitical"]["intents"][0]["intent"] == "coercion"
    # テンポ: 両系列が立つ
    assert sum(s["tempo"]["cyber"]) == 1
    assert sum(s["tempo"]["geopol"]) == 1


def test_situation_by_nation_subject_gate_excludes_mention_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """評価済み記事で主題でない CN APT 言及は攻撃者面 (actors) に計上しない。

    2026-07-29: 元々は subject_gate_clause による fallback ゲートとして実装されていた。
    2026-08-20 の母集団反転で、攻撃者面は articles.subject_actor_ids 起点の構成的な
    集計になったため、主題不一致の記事はそもそも母集団に入らない (同じ結論を別機構で保証)。
    """
    db = tmp_path / "sit_gate.db"
    repo = RunHistoryRepository(db_path=db)
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    now = datetime.now(UTC)
    # volt_typhoon (CN APT) を言及するが主題は別 (lazarus) の評価済み記事
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="m1",
            title="m1 apt",
            url="https://e/m1",
            category="apt",
            status="posted",
            created_at=now,
            published_at=now,
            subject_actor_ids="lazarus",
            subject_actor_source="llm",
        )
    )
    repo.add_article_entities("m1", [("actor", "volt_typhoon")], when=now)
    s = situation_by_nation("CN", window_days=None, db_path=db)
    assert all(x["actor_id"] != "volt_typhoon" for x in s["cyber"]["actors"])


def test_situation_target_lens_surfaces_threats_facing_nation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """標的レンズ: その国が victim の cyber 攻撃 (攻撃元アクター + 分野) を出す。"""
    db = _seed(tmp_path)
    jp = situation_by_nation("JP", window_days=None, db_path=db)

    # JP は攻撃者面ゼロ (日本 APT は辞書に無い) だが標的面で脅威が見える
    assert jp["cyber"]["total"] == 0
    assert jp["cyber_target"]["total"] == 1
    assert any(a["actor_id"] == "lazarus" for a in jp["cyber_target"]["actors"])  # 攻撃元
    assert any(s["sector"] == "government" for s in jp["cyber_target"]["sectors"])  # 標的分野

    # 標的レンズは list_nations にも反映され JP が浮上する
    nations = list_nations(window_days=None, db_path=db)
    by_iso = {n["iso"]: n for n in nations}
    assert by_iso["JP"]["cyber_target"] == 1 and by_iso["JP"]["cyber"] == 0
    # JP = 自国 (防衛 CTI の主体) → home グループで最前面
    assert by_iso["JP"]["role"] == "home"


def test_situation_separates_attributed_cyber_from_mention(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """分離抽出: 地政学記事に埋もれた『帰属なしサイバー言及』(i_cyber × involved_country ×
    actor 無し) を帰属済みサイバー作戦 (actor.nation) と別レーンに分離する。"""
    db = _seed(tmp_path)
    repo = RunHistoryRepository(db_path=db)
    rid = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=True))
    # 中国が当事者のサイバー政策記事 (i_cyber 点灯・APT 名指し無し)
    cm = ArticleRecord(
        run_id=rid,
        article_id="cm1",
        title="cn cyber doctrine",
        url="https://e/cm1",
        category="geopolitical",
        status="posted",
        socio_political_intent="coercion",
        pmesii_i_cyber=True,
        created_at=_NOW,
        published_at=_NOW,
    )
    repo.add_article(cm)
    repo.add_article_entities("cm1", [("involved_country", "CN")], when=_NOW)

    s = situation_by_nation("CN", window_days=None, db_path=db)
    # 帰属レーンは Volt Typhoon のみ (言及記事は actor 無しなので入らない)
    assert s["cyber"]["total"] == 1
    # 言及レーンに分離して入る
    assert s["cyber_mention"]["total"] == 1
    # 帰属済み記事 (cy1=volt_typhoon、involved_country 無し) は言及レーンに混ざらない
    assert all(r["article_id"] != "cy1" for r in s["cyber_mention"]["recent"])


def test_situation_tempo_exposes_report_and_event_series_with_coverage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """時間軸トグル: テンポは報道時刻系列 + 発生時刻系列 + カバレッジを返す。
    event_date 付きは発生時刻系列に立ち、未付与 (速報) は dated に数えない。"""
    db = _seed(tmp_path)
    repo = RunHistoryRepository(db_path=db)
    rid = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=True))
    today = _NOW.date().isoformat()
    dated = ArticleRecord(
        run_id=rid,
        article_id="ev1",
        title="cn apt dated",
        url="https://e/ev1",
        category="apt",
        status="posted",
        socio_political_intent="prepositioning",
        created_at=_NOW,
        published_at=_NOW,
        event_date=today,
        event_date_basis="detected",
        # 攻撃者面は主題起点 (2026-08-20) のため、テンポ系列に乗せるには subject 評価も要る。
        subject_actor_ids="volt_typhoon",
        subject_actor_source="llm",
    )
    repo.add_article(dated)
    repo.add_article_entities("ev1", [("actor", "volt_typhoon")], when=_NOW)

    t = situation_by_nation("CN", window_days=None, db_path=db)["tempo"]
    # 両系列 + カバレッジが存在
    assert {"cyber", "geopol", "cyber_event", "geopol_event", "coverage"} <= set(t)
    # 報道時刻系列は既存 cy1 + 新 ev1 で >=2、発生時刻系列は dated の ev1 のみ >=1
    assert sum(t["cyber"]) >= 2
    assert sum(t["cyber_event"]) >= 1
    # カバレッジ: dated は ev1 のみ (cy1 は event_date 無し → 速報扱い)
    assert t["coverage"]["dated"] == 1
    assert t["coverage"]["total"] >= 2


def test_list_nations_includes_cyber_and_geopol(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _seed(tmp_path)
    nations = list_nations(window_days=None, db_path=db)
    by_iso = {n["iso"]: n for n in nations}
    assert by_iso["CN"]["cyber"] == 1 and by_iso["CN"]["geopol"] == 1
    assert by_iso["RU"]["geopol"] == 1 and by_iso["RU"]["cyber"] == 0
    # 役割グループ: 中露は敵対 (セレクタの最前面グループ)
    assert by_iso["CN"]["role"] == "adversary"
    assert by_iso["RU"]["role"] == "adversary"
