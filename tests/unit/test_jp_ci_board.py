"""日本重要インフラ board 集約サービスの統合テスト (近接度ラダー版)。

核心: (1) 近接度ラダー (静穏→予兆→標的化→侵害IT→制御系接近) が system_domain / intent を
減衰機構として段階を決める、(2) 敵対性ゲート・canonical ゲート・報道クラスタ集約がノイズを
隔離する、(3) 前期間比 delta が変化を検知する、(4) 疎でも全分野表示 + 静か≠安全。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.cti.nisc_sectors import NISC_SECTORS
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services.jp_ci_board import build_jp_ci_board


@pytest.fixture()
def seeded_db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # KEV: pot1 の CVE を実悪用中とする (標的化段階の検証)
    monkeypatch.setattr(
        "src.tools.kev_client.get_kev_cve_set", lambda: frozenset({"CVE-2025-0001"})
    )
    db = tmp_path / "jpci.db"
    repo = RunHistoryRepository(db_path=db)
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="test", dry_run=True))

    def art(aid: str, **kw: Any) -> ArticleRecord:
        base: dict[str, Any] = {
            "run_id": run_id,
            "article_id": aid,
            "title": kw.pop("title", "x"),
            "url": f"https://x.example/{aid}",
            "feed_title": kw.pop("feed_title", "src"),
            "feed_url": f"https://feed/{aid}",
            "status": kw.pop("status", "posted"),
            "published_at": kw.pop("published_at", datetime.now(UTC)),
        }
        base.update(kw)
        return ArticleRecord(**base)

    # 電力: OT 敵対事象 (ICS TTP + 事業者名) → 制御系接近
    repo.add_article(
        art(
            "obs1",
            title="電力会社のSCADAに不正アクセス",
            category="apt",
            importance="high",
            summary="制御系に侵入",
            victim_country_iso="JP",
            victim_sector_canonical="energy",
        )
    )
    repo.add_article_entities("obs1", [("ttp", "T0883"), ("victim_org", "東京電力")])

    # 潜在: SCADA 製品の KEV 脆弱性 → SCADA 分野群の「標的化」
    repo.add_article(
        art(
            "pot1",
            title="Schneider SCADA の脆弱性 RCE",
            category="vulnerability",
            importance="high",
            summary="遠隔コード実行",
        )
    )
    repo.add_article_entities(
        "pot1", [("cve", "CVE-2025-0001"), ("affected_product", "Schneider SCADA")]
    )

    # 潜在: 製品リンクの無い汎用 KEV 告知 → board に置かない (text fallback 廃止)
    repo.add_article(
        art(
            "pot2",
            title="CISA、悪用が確認された脆弱性をKEVカタログに追加 官公庁向け注意喚起",
            category="advisory",
            importance="high",
            summary="複数製品",
        )
    )

    # 医療: IT 敵対事象 → 侵害(IT系) (減衰機構)
    repo.add_article(
        art(
            "obs2",
            title="病院で顧客情報が情報漏洩",
            category="breach",
            importance="medium",
            summary="個人情報流出",
            victim_country_iso="JP",
            victim_sector_canonical="healthcare",
        )
    )

    # 政府: 誤送信 (非敵対) → 脅威計上せず nonthreat に隔離
    repo.add_article(
        art(
            "obs3",
            title="住民向けメールで誤送信 - 某市",
            category="incident",
            importance="low",
            summary="事務ミス",
            victim_country_iso="JP",
            victim_sector_canonical="government",
        )
    )

    # 情報通信: 同一事案の多重報道 (KDDI 型) → 1 事象に集約
    for i, t in enumerate(
        ["KDDIのメールシステムに不正アクセス", "KDDI侵害の続報: ゼロデイが標的に"]
    ):
        aid = f"obs4-{i}"
        repo.add_article(
            art(
                aid,
                title=t,
                category="breach",
                importance="high" if i else "medium",
                summary="ISP向けメールシステム",
                victim_country_iso="JP",
                victim_sector_canonical="telecom",
            )
        )
        repo.add_article_entities(aid, [("victim_org", "KDDI")])

    # 非CI (retail) + データ種別言及 → board 対象外 (canonical ゲート)
    repo.add_article(
        art(
            "obs5",
            title="ECサイトに不正アクセス、クレジットカード情報が漏えいの可能性",
            category="breach",
            importance="medium",
            summary="通販サイト",
            victim_country_iso="JP",
            victim_sector_canonical="retail",
        )
    )

    # 鉄道: 前窓のみの敵対事象 (40日前) → delta 後退の検証
    repo.add_article(
        art(
            "obs6",
            title="鉄道会社の予約システムに不正アクセス",
            category="breach",
            importance="medium",
            summary="窓外の事案",
            victim_country_iso="JP",
            victim_sector_canonical="transportation",
            published_at=datetime.now(UTC) - timedelta(days=40),
        )
    )

    # 金融: 指定事業者の和名/英名の別報道 → 名簿 alias で 1 事象に merge (Aflac 実例)
    for i, (t, org) in enumerate(
        [
            ("アフラック生命保険への不正アクセス", "アフラック生命保険株式会社"),
            ("Aflac Japan でデータ漏洩、438万人の個人情報が流出", "Aflac Japan"),
        ]
    ):
        aid = f"obs7-{i}"
        repo.add_article(
            art(
                aid,
                title=t,
                category="breach",
                importance="high",
                summary="保険会社への不正アクセス",
                victim_country_iso="JP",
                victim_sector_canonical="financial",
            )
        )
        repo.add_article_entities(aid, [("victim_org", org)])

    # 金融: 集中ノード (全銀ネット) の侵害 → systemic フラグ
    repo.add_article(
        art(
            "obs_sys",
            title="全国銀行資金決済ネットワークで障害、銀行間送金に影響",
            category="incident",
            importance="high",
            summary="資金清算システムの不具合",
            victim_country_iso="JP",
            victim_sector_canonical="financial",
        )
    )
    repo.add_article_entities("obs_sys", [("victim_org", "全国銀行資金決済ネットワーク")])

    # 金融: org が特定できていて CI 事業者でない (ポイントサービス) → 周辺観測に降格
    repo.add_article(
        art(
            "obs8",
            title="ポイントサービスにサイバー攻撃 - サービスが一時停止",
            category="breach",
            importance="medium",
            summary="ポイント交換サービス",
            victim_country_iso="JP",
            victim_sector_canonical="financial",
        )
    )
    repo.add_article_entities("obs8", [("victim_org", "サイバーエージェント")])

    # 金融: 同一非 CI 事案の org 抽出漏れ記事 → 周辺 org と title 突合で道連れ降格
    repo.add_article(
        art(
            "obs9",
            title="フィンパワードが不正アクセス被害の最終報告を公開",
            category="breach",
            importance="medium",
            summary="開発者端末侵害",
            victim_country_iso="JP",
            victim_sector_canonical="financial",
        )
    )
    repo.add_article(
        art(
            "obs9b",
            title="フィンパワードホールディングスへの不正アクセス、情報流出の可能性",
            category="breach",
            importance="medium",
            summary="家計簿SaaS",
            victim_country_iso="JP",
            victim_sector_canonical="financial",
        )
    )
    repo.add_article_entities("obs9b", [("victim_org", "フィンパワードホールディングス")])

    # 医療: SaaS 侵害の多数 victim (非CI 3 + CI 1)。判定根拠 org が cap で落ちない検証
    repo.add_article(
        art(
            "obs10",
            title="メール配信システムに不正アクセス、複数の利用組織で情報漏洩の恐れ",
            category="breach",
            importance="medium",
            summary="配信SaaSの侵害",
            victim_country_iso="JP",
        )
    )
    # 非 CI org は ASCII 名 (値ソートで eクリニック より先) — merge の operators cap (3件)
    # で判定根拠が落ちるケースを決定論的に再現する
    repo.add_article_entities(
        "obs10",
        [
            ("victim_org", "AAA Store"),
            ("victim_org", "BBB Theatre"),
            ("victim_org", "CCC Delivery"),
            ("victim_org", "eクリニック"),
        ],
    )

    # 行動レーン: Volt Typhoon (CN) の事前潜伏 — JP victim を名指さない世界コーパス記事。
    # doctrine で 通信/電力/水道 等に射影されるべき (victim 中心では不可視な先行指標)。
    for i in range(4):
        aid = f"vt-{i}"
        repo.add_article(
            art(
                aid,
                title=f"Volt Typhoon が重要インフラに潜伏 第{i}報",
                category="apt",
                importance="high",
                summary="living off the land による事前潜伏",
                victim_country_iso="US",
                socio_political_intent="prepositioning",
            )
        )
        repo.add_article_entities(aid, [("actor", "Volt Typhoon"), ("ttp", "T1190")])
    return db


def _by_sector(data: dict[str, Any]) -> dict[str, Any]:
    return {s["nisc_sector"]: s for s in data["sectors"]}


class TestLadder:
    def test_all_nisc_sectors_present(self, seeded_db: Any) -> None:
        data = build_jp_ci_board(window_days=30, db_path=seeded_db)
        assert set(_by_sector(data)) == set(NISC_SECTORS)  # 疎でも全分野表示

    def test_ot_adversarial_is_ot_approach(self, seeded_db: Any) -> None:
        elec = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["electricity"]
        assert elec["stage"] == "ot_approach"
        items = elec["stages"]["ot_approach"]
        assert len(items) == 1
        # 観測は事業者名指し可。指定事業者は名簿 canonical で表示 (表記ゆれの統一)
        assert items[0]["operator"] == "東京電力ホールディングス"
        assert items[0]["system_domain"] == "ot"
        assert "敵対事象" in elec["headline"]

    def test_it_adversarial_attenuated_to_intrusion_it(self, seeded_db: Any) -> None:
        med = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["medical"]
        assert med["stage"] == "intrusion_it"
        assert med["stages"]["ot_approach"] == []

    def test_kev_sector_tech_is_targeting(self, seeded_db: Any) -> None:
        # 水道は観測ゼロだが SCADA KEV → 標的化 (実悪用中の攻撃経路が分野技術に開いている)
        water = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["water"]
        assert water["stage"] == "targeting"
        kev_items = water["stages"]["targeting"]
        assert kev_items and kev_items[0]["kev"] is True
        assert kev_items[0]["operator"] is None  # 潜在は露出クラス (名指し不可)

    def test_generic_advisory_not_placed_without_product(self, seeded_db: Any) -> None:
        # pot2 (製品リンク無し) はどの分野にも置かれない (text fallback 廃止)
        data = build_jp_ci_board(window_days=30, db_path=seeded_db)
        for s in data["sectors"]:
            for stage_items in s["stages"].values():
                assert all("pot2" not in i["id"] for i in stage_items)


class TestNoiseGates:
    def test_mishap_isolated_to_nonthreat(self, seeded_db: Any) -> None:
        gov = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["government"]
        assert gov["counts"]["nonthreat"] == 1
        assert gov["stages"]["intrusion_it"] == []  # 誤送信は脅威計上しない

    def test_non_ci_canonical_not_on_board(self, seeded_db: Any) -> None:
        # retail EC 事案 (クレジットカード情報言及) がクレジット分野に混入しない
        credit = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["credit"]
        assert all(not items for items in credit["stages"].values())

    def test_report_cluster_merged(self, seeded_db: Any) -> None:
        tel = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["it_telecom"]
        events = tel["stages"]["intrusion_it"]
        assert len(events) == 1  # 2 報道 = 1 事象
        assert events[0]["report_count"] == 2
        assert events[0]["importance"] == "high"  # 代表は最重要報道


class TestOperatorGate:
    def test_designated_operator_tier_and_alias_merge(self, seeded_db: Any) -> None:
        # 和名/英名の別報道が名簿 alias で 1 事象に merge され、指定事業者 tier が付く
        fin = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["finance"]
        events = fin["stages"]["intrusion_it"]
        aflac = [e for e in events if e.get("operator") == "アフラック生命保険"]
        assert len(aflac) == 1
        assert aflac[0]["report_count"] == 2
        assert aflac[0]["operator_tier"] == "designated"

    def test_non_ci_org_demoted_to_peripheral(self, seeded_db: Any) -> None:
        # org 特定済みで名簿にも型にも合致しない → 周辺観測 (段階を駆動しない)
        fin = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["finance"]
        assert all(
            e.get("operator") != "サイバーエージェント"
            for items in fin["stages"].values()
            for e in items
        )
        assert fin["counts"]["peripheral"] >= 1
        assert any("ポイントサービス" in p["title"] for p in fin["peripheral"])

    def test_org_less_ci_canonical_stays_on_ladder(self, seeded_db: Any) -> None:
        # org 抽出の無い記事は検証不能 → canonical (healthcare) を信頼して段階維持 (recall)
        med = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["medical"]
        assert med["stage"] == "intrusion_it"

    def test_org_less_report_of_peripheral_incident_follows_demotion(self, seeded_db: Any) -> None:
        # 同一非 CI 事案の org 抽出漏れ記事は、周辺 org の中核名が title に現れる場合
        # 道連れで周辺観測へ (org 抽出の揺らぎで CI レーンに漏れない)
        fin = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["finance"]
        assert all(
            "フィンパワード" not in e["title"] for items in fin["stages"].values() for e in items
        )
        assert sum(1 for p in fin["peripheral"] if "フィンパワード" in p["title"]) >= 1

    def test_type_tier_for_operator_suffix(self, seeded_db: Any) -> None:
        # 東京電力 = 名簿 designated (型でなく名簿が勝つ)
        elec = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["electricity"]
        assert elec["stages"]["ot_approach"][0]["operator_tier"] == "designated"

    def test_coverage_denominator_quantifies_dark_space(self, seeded_db: Any) -> None:
        # 名簿が「暗域の分母」になる: 指定M事業者中、観測はK者 (残りは無音)
        elec = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["electricity"]
        assert elec["designated_total"] >= 10  # 電力の指定事業者数
        assert elec["designated_observed"] == 1  # 東京電力のみ観測
        # 静穏分野でも分母は非ゼロ (無音が定量化される)
        port = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["port"]
        assert port["designated_total"] >= 10
        assert port["designated_observed"] == 0

    def test_systemic_node_flagged_on_event(self, seeded_db: Any) -> None:
        # 集中ノード (全銀ネット) の侵害は systemic フラグが立つ
        fin = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["finance"]
        zengin = [
            e
            for items in fin["stages"].values()
            for e in items
            if e.get("operator") == "全国銀行資金決済ネットワーク"
        ]
        assert len(zengin) == 1 and zengin[0]["systemic"] is True
        # 代替可能な大手 (アフラック) は systemic でない
        aflac = [
            e for e in fin["stages"]["intrusion_it"] if e.get("operator") == "アフラック生命保険"
        ]
        assert aflac and aflac[0]["systemic"] is False

    def test_multi_victim_saas_breach_keeps_tier_of_matched_org(self, seeded_db: Any) -> None:
        # SaaS 侵害で victim_org が多数 (非CI 多め + CI 1件) でも、判定に使った org が
        # merge の operators cap (3件) で落ちず tier が表示される (める配くん実例)
        med = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["medical"]
        saas = [e for e in med["stages"]["intrusion_it"] if "配信システム" in e["title"]]
        assert len(saas) == 1
        assert saas[0]["operator_tier"] == "type"
        assert saas[0]["operator"] == "eクリニック"


class TestBehaviorLane:
    def test_state_actor_behavior_projected_via_doctrine(self, seeded_db: Any) -> None:
        # Volt Typhoon の事前潜伏 (JP victim なし) が doctrine で lifeline 分野に射影される
        sectors = _by_sector(build_jp_ci_board(window_days=90, db_path=seeded_db))
        tel = sectors["it_telecom"]
        vt = [b for b in tel["threat_behavior"] if b["actor"] == "Volt Typhoon"]
        assert len(vt) == 1
        assert vt[0]["intent"] == "prepositioning"
        assert vt[0]["nation"] == "cn"
        assert tel["world_active"] is True
        # doctrine は電力・水道にも射影
        assert any(b["actor"] == "Volt Typhoon" for b in sectors["electricity"]["threat_behavior"])
        assert any(b["actor"] == "Volt Typhoon" for b in sectors["water"]["threat_behavior"])

    def test_quiet_jp_but_active_world(self, seeded_db: Any) -> None:
        # 航空は JP 観測静穏 (KEV も無い) だが世界行動あり = 「静穏の再定義」
        aviation = _by_sector(build_jp_ci_board(window_days=90, db_path=seeded_db))["aviation"]
        assert aviation["stage"] == "quiet"  # JP 近接度は静穏
        assert aviation["world_active"] is True  # だが世界行動あり (Volt Typhoon doctrine)
        assert aviation["counts"]["behavior"] >= 1

    def test_cross_sector_campaign_detected(self, seeded_db: Any) -> None:
        # Volt Typhoon が 3+ 分野で事前潜伏 → 横断キャンペーンとして board 頭部に
        data = build_jp_ci_board(window_days=90, db_path=seeded_db)
        vt = [c for c in data["campaigns"] if c["actor"] == "Volt Typhoon"]
        assert len(vt) == 1
        assert vt[0]["intent"] == "prepositioning"
        assert len(vt[0]["sector_ids"]) >= 3
        assert "it_telecom" in vt[0]["sector_ids"]

    def test_behavior_lane_absent_without_snapshot(self, seeded_db: Any, monkeypatch: Any) -> None:
        # snapshot 取得失敗でも board は落ちない (行動レーンは空)
        monkeypatch.setattr("src.ui.services.jp_ci_board._fetch_snapshot", lambda *a, **k: None)
        data = build_jp_ci_board(window_days=90, db_path=seeded_db)
        assert data["campaigns"] == []
        assert all(not s["world_active"] for s in data["sectors"])


class TestDelta:
    def test_receded_sector_has_negative_delta(self, seeded_db: Any) -> None:
        # 前窓 = 侵害(IT系) (obs6)、現窓 = SCADA KEV の標的化のみ → 後退
        rail = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))["railway"]
        assert rail["stage"] == "targeting"
        assert rail["prev_stage"] == "intrusion_it"
        assert rail["delta"] == -1

    def test_escalated_sector_in_changes(self, seeded_db: Any) -> None:
        data = build_jp_ci_board(window_days=30, db_path=seeded_db)
        changed = {c["nisc_sector"]: c for c in data["changes"]}
        assert "electricity" in changed  # 前窓 quiet → 制御系接近
        assert changed["electricity"]["to_stage"] == "ot_approach"
        assert "railway" in changed  # 後退も変化として報告

    def test_no_delta_for_all_time_window(self, seeded_db: Any) -> None:
        data = build_jp_ci_board(window_days=None, db_path=seeded_db)
        assert data["changes"] == []
        assert all(s["delta"] is None for s in data["sectors"])


class TestHonesty:
    def test_quiet_not_safe_flag(self, seeded_db: Any) -> None:
        sectors = _by_sector(build_jp_ci_board(window_days=30, db_path=seeded_db))
        assert sectors["port"]["stage"] == "quiet"
        assert sectors["port"]["quiet_not_safe"] is True
        assert "静か" in sectors["port"]["headline"]

    def test_note_and_labels(self, seeded_db: Any) -> None:
        data = build_jp_ci_board(window_days=30, db_path=seeded_db)
        assert "静か" in data["note"]
        assert set(data["stage_labels"]) == {
            "quiet",
            "indication",
            "targeting",
            "intrusion_it",
            "ot_approach",
        }
