"""ci_posture (JP CI board の純粋 posture ロジック) のテスト。

核心: (1) 敵対性ゲートが誤送信/紛失等の非敵対事象を脅威計上から隔離する、
(2) 報道クラスタ集約が同一事案の多重報道 (KDDI 型) を 1 事象に畳む、
(3) 近接度ラダーが system_domain / intent を減衰機構として段階を決める。
"""

from __future__ import annotations

from typing import Any

from src.cti.ci_posture import (
    STAGE_LABELS,
    STAGE_RANKS,
    compose_headline,
    is_brand_impersonation,
    is_nonthreat_mishap,
    is_survey_or_statistics,
    merge_observed_events,
    observed_stage,
    sector_stage,
)

# ---------- 敵対性ゲート ----------


class TestNonthreatGate:
    def test_mishap_titles_are_nonthreat(self) -> None:
        # 自治体系の事故 (アクター不在) — 脅威 board から隔離すべき典型
        for title in (
            "講演会参加希望者向けのメールで誤送信 - 妙高市",
            "学生情報含む書類を宿泊施設に置き忘れて紛失 - 新潟県",
            "配付名簿に不同意者の個人情報、法令を誤解釈 - 阿南市",
            "関係者リストを誤送信、ファイル名で取り違え - 堺市",
            "個人情報を関係団体と委託先へ誤提供し個人情報漏洩",
            "クラウドの設定ミスで会員情報が公開状態に",
            "顧客情報を含む書類を誤廃棄",
            "ウェブサイトに個人情報を誤掲載",
            "顧客情報を保存した外部記憶媒体が所在不明に",
        ):
            assert is_nonthreat_mishap(title), title

    def test_adversarial_titles_pass_gate(self) -> None:
        for title in (
            "電力会社のSCADAに不正アクセス",
            "ランサムウェア被害で電子カルテが停止",
            "KDDIのISP向けメールシステム、ゼロデイ脆弱性が標的に",
            "マルウェアを仕込んだ USB ドライブが防衛ネットワークに侵入",
            "DDoS攻撃で行政サービスが一時停止",
        ):
            assert not is_nonthreat_mishap(title), title

    def test_survey_titles_are_not_events(self) -> None:
        # 統計・調査・回顧記事は「事象」ではない (敵対でも事務事故でもなく board 対象外)
        for title in (
            "日経225企業の96%が情報漏えいを経験 最も漏えい率の高い業界は",
            "ランサムウェア被害の実態調査レポートを公開",
            "セキュリティ意識調査: 企業の7割が対策不足",
            "サイバー攻撃に関する統計データを公表",
            "狙われたのは忘れられたシステム ~ サイバー攻撃被害企業が語ったインシデント対応の現実",
            "CISO インタビュー: ランサム被害からの復旧の舞台裏",
        ):
            assert is_survey_or_statistics(title), title

    def test_incident_investigation_is_still_event(self) -> None:
        # 「原因調査中」等の実事案は調査記事扱いしない
        for title in (
            "関連7サイトで障害、原因調査や復旧急ぐ - 名鉄協商",
            "不正アクセスの被害範囲を調査中",
        ):
            assert not is_survey_or_statistics(title), title

    def test_brand_impersonation_is_not_operator_intrusion(self) -> None:
        # ブランド詐称 (顧客標的) は事業者システムへの侵入ではない (phishing 除外と同じ理由)
        for title in (
            "滋賀銀行を装ったボイスフィッシングで4名逮捕-計3億5,000万円を詐取",
            "宅配業者をかたる偽SMSに注意喚起",
            "事務局を装うなりすましコメントで偽サイト誘導 フィッシング詐欺に注意喚起",
        ):
            assert is_brand_impersonation(title), title

    def test_impersonation_arrival_of_real_intrusion_is_event(self) -> None:
        # 詐称は初期潜入の手口でもある — 侵害の実示唆があれば事象として維持
        for title in (
            "取引先を装うメールから社内ネットワークに侵入",
            "なりすましメール経由でマルウェア感染、顧客情報が流出",
            "銀行の偽サイトに入力された認証情報で不正アクセス被害",
        ):
            assert not is_brand_impersonation(title), title


# ---------- 観測事象の段階 (system_domain / intent 減衰機構) ----------


class TestObservedStage:
    def test_ot_domain_is_ot_approach(self) -> None:
        assert observed_stage("ot", None) == "ot_approach"
        assert observed_stage("boundary", None) == "ot_approach"

    def test_prepositioning_intent_is_ot_approach(self) -> None:
        # 事前潜伏 = CI 脅威の本質 (Volt Typhoon 型)。IT 経由でも最上段。
        assert observed_stage("it", "prepositioning") == "ot_approach"

    def test_it_incident_attenuated(self) -> None:
        # 減衰機構: CI 事業者のただの IT 被害を国家 CI 脅威に水増ししない
        assert observed_stage("it", None) == "intrusion_it"
        assert observed_stage("unknown", None) == "intrusion_it"
        assert observed_stage("it", "financial") == "intrusion_it"


# ---------- 報道クラスタ集約 ----------


def _obs(aid: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": aid,
        "title": f"title {aid}",
        "orgs": [],
        "system_domain": "it",
        "importance": "medium",
        "url": f"https://x.example/{aid}",
        "event_date": None,
        "confidence": "medium",
        "intent": None,
    }
    base.update(kw)
    return base


class TestMergeObservedEvents:
    def test_shared_org_merges_multi_hop(self) -> None:
        # KDDI 型: 記事ごとに org が違っても共有 org を介して 1 事象に連結
        items = [
            _obs("a1", orgs=["KDDI"]),
            _obs("a2", orgs=["中部テレコミュニケーション", "KDDI"]),
            _obs("a3", orgs=["中部テレコミュニケーション"]),
        ]
        merged = merge_observed_events(items)
        assert len(merged) == 1
        assert merged[0]["report_count"] == 3

    def test_org_normalization_merges_corporate_suffix(self) -> None:
        items = [_obs("a1", orgs=["KDDI株式会社"]), _obs("a2", orgs=["KDDI"])]
        assert len(merge_observed_events(items)) == 1

    def test_distinct_orgs_stay_separate(self) -> None:
        items = [_obs("a1", orgs=["東京電力"]), _obs("a2", orgs=["大阪ガス"])]
        assert len(merge_observed_events(items)) == 2

    def test_orphans_merge_by_title(self) -> None:
        items = [
            _obs("a1", title="同一タイトルの転載記事", orgs=[]),
            _obs("a2", title="同一タイトルの転載記事", orgs=[]),
            _obs("a3", title="別の事案", orgs=[]),
        ]
        merged = merge_observed_events(items)
        assert len(merged) == 2

    def test_representative_is_highest_importance(self) -> None:
        items = [
            _obs("a1", orgs=["KDDI"], importance="medium", title="続報"),
            _obs("a2", orgs=["KDDI"], importance="high", title="第一報 詳細"),
        ]
        merged = merge_observed_events(items)
        assert merged[0]["title"] == "第一報 詳細"
        assert merged[0]["importance"] == "high"

    def test_domain_takes_max_severity(self) -> None:
        # 別報道が境界系の証拠を出したら事象全体は境界系 (証拠の合併)
        items = [
            _obs("a1", orgs=["KDDI"], system_domain="it"),
            _obs("a2", orgs=["KDDI"], system_domain="boundary"),
        ]
        assert merge_observed_events(items)[0]["system_domain"] == "boundary"

    def test_prepositioning_intent_survives_merge(self) -> None:
        items = [
            _obs("a1", orgs=["x社"], intent=None),
            _obs("a2", orgs=["x社"], intent="prepositioning"),
        ]
        assert merge_observed_events(items)[0]["intent"] == "prepositioning"


# ---------- 分野の段階と見出し ----------


def _stages(**kw: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    base: dict[str, list[dict[str, Any]]] = {
        "ot_approach": [],
        "intrusion_it": [],
        "targeting": [],
        "indication": [],
    }
    base.update(kw)
    return base


class TestSectorStage:
    def test_highest_nonempty_wins(self) -> None:
        st = _stages(intrusion_it=[{"title": "x"}], indication=[{"title": "y"}])
        assert sector_stage(st) == "intrusion_it"

    def test_all_empty_is_quiet(self) -> None:
        assert sector_stage(_stages()) == "quiet"

    def test_ranks_are_total_order(self) -> None:
        assert set(STAGE_RANKS) == set(STAGE_LABELS)
        assert sorted(STAGE_RANKS.values()) == list(range(len(STAGE_RANKS)))


class TestHeadline:
    def test_quiet_mentions_not_safe(self) -> None:
        h = compose_headline("quiet", _stages(), "thin")
        assert "静か" in h and "安全" in h

    def test_intrusion_counts_and_title(self) -> None:
        st = _stages(
            intrusion_it=[
                {"title": "アフラックで不正アクセス", "kind": "observed", "report_count": 3},
                {"title": "別件", "kind": "observed", "report_count": 1},
            ]
        )
        h = compose_headline("intrusion_it", st, "medium")
        assert "2件" in h
        assert "アフラック" in h

    def test_targeting_actor_named(self) -> None:
        st = _stages(targeting=[{"title": "t", "kind": "actor", "actor": "Volt Typhoon"}])
        h = compose_headline("targeting", st, "medium")
        assert "Volt Typhoon" in h

    def test_targeting_kev_shows_cve(self) -> None:
        st = _stages(
            targeting=[{"title": "t", "kind": "potential", "kev": True, "cves": ["CVE-2026-1"]}]
        )
        h = compose_headline("targeting", st, "medium")
        assert "CVE-2026-1" in h

    def test_secondary_context_appended(self) -> None:
        # 主段階 + 次段階の要約 (侵害があり、かつ read-across もある分野)
        st = _stages(
            intrusion_it=[{"title": "侵害事案", "kind": "observed", "report_count": 1}],
            indication=[
                {"title": "t", "kind": "readacross", "actor": "Volt Typhoon", "allies": "US"}
            ],
        )
        h = compose_headline("intrusion_it", st, "medium")
        assert "侵害事案" in h
        assert "Volt Typhoon" in h
