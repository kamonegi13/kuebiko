"""NISC 分野派生 / ATT&CK ICS 判定 / システム軸導出のテスト (JP CI board Phase A)。"""

from __future__ import annotations

import pytest

from src.cti.attack_catalog import is_ics_technique, technique_matrix
from src.cti.nisc_sectors import (
    NISC_SECTORS,
    nisc_sector_for,
    operator_nisc_sector,
    sector_label,
    sectors_for_tech,
)
from src.cti.system_domain import derive_system_domain


class TestNiscSectorFor:
    def test_keyword_beats_canonical(self) -> None:
        # energy canonical だが本文が「ガス」→ gas に精緻化
        assert nisc_sector_for("energy", "都市ガス事業者に不正アクセス") == "gas"

    def test_canonical_default_when_no_keyword(self) -> None:
        assert nisc_sector_for("energy", "電力会社が被害") == "electricity"
        assert nisc_sector_for("telecom", "通信キャリアが標的") == "it_telecom"
        assert nisc_sector_for("financial", "某社が被害") == "finance"

    def test_transportation_refinement(self) -> None:
        assert nisc_sector_for("transportation", "成田空港のシステム障害") == "airport"
        assert nisc_sector_for("transportation", "鉄道会社が被害") == "railway"

    def test_non_ci_returns_none(self) -> None:
        assert nisc_sector_for("retail", "小売店で情報漏洩") is None
        assert nisc_sector_for(None, "一般的なニュース") is None

    def test_non_ci_canonical_gate_beats_keywords(self) -> None:
        # 非 CI 事業者 (小売/メディア/教育) は本文にデータ種別や CI 語があっても board 対象外。
        # 実例: 仏壇店・動画配信・趣味サイトが「クレジットカード情報」言及でクレジット分野に
        # 誤配置されていた (被害者の分野 ≠ 漏えいデータの種別)。
        assert nisc_sector_for("retail", "不正アクセスでクレジットカード情報が漏えい") is None
        assert nisc_sector_for("media", "動画配信サイト退会、決済代行にも影響") is None
        assert nisc_sector_for("education", "大学で医療情報を含む書類が流出") is None
        assert nisc_sector_for("technology", "SaaS事業者に不正アクセス、銀行の顧客も利用") is None

    def test_data_type_mention_is_not_credit_sector(self) -> None:
        # 「クレジットカード情報が漏えい」はデータ種別の言及であり被害者分野ではない
        assert (
            nisc_sector_for(None, "ECサイトに不正アクセス、クレジットカード情報が漏えいの可能性")
            is None
        )

    def test_credit_entity_terms_map_to_credit(self) -> None:
        assert nisc_sector_for(None, "決済代行会社に不正アクセス") == "credit"
        assert nisc_sector_for("financial", "大手カード会社でシステム障害") == "credit"

    def test_bank_account_data_mention_not_finance(self) -> None:
        # 「銀行口座(情報)」のデータ種別言及では金融に置かない。事業者言及は置く。
        assert nisc_sector_for(None, "通販サイトから銀行口座情報が流出") is None
        assert nisc_sector_for(None, "地方銀行のシステムに不正アクセス") == "finance"
        assert nisc_sector_for(None, "信用組合で顧客情報漏洩") == "finance"

    def test_canonical_restricts_refinement_targets(self) -> None:
        # financial 記事は金融庁/行政などの言及で政府・行政に飛ばない (精緻化先は金融系のみ)
        assert (
            nisc_sector_for("financial", "自治体向け行政サービスも提供する金融グループ")
            == "finance"
        )
        # government 記事の防衛省言及は defense へ (government の精緻化先に defense を含む)
        assert nisc_sector_for("government", "防衛省の調達システムで漏洩") == "defense"

    def test_manufacturing_refines_only_chemical_or_defense(self) -> None:
        assert nisc_sector_for("manufacturing", "化学メーカーの工場が停止") == "chemical"
        assert nisc_sector_for("manufacturing", "三菱重工を装うフィッシング") == "defense"
        # 電力向け機器メーカー = 製造業であって電力事業者ではない
        assert nisc_sector_for("manufacturing", "電力向け機器メーカーが被害") is None
        assert nisc_sector_for("manufacturing", "自動車部品の製造ライン") is None

    def test_gyousei_shobun_not_government(self) -> None:
        # 「行政処分」は規制動作の言及であり被害者が行政という意味ではない
        assert nisc_sector_for(None, "暗号資産交換業者に行政処分") is None

    def test_unrestricted_placement_ignores_summary(self) -> None:
        # canonical 不明の記事は title のみで判定 — summary の対処助言
        # (「カード会社への連絡を推奨」等) で誤配置しない (pixivFANBOX 実例)
        assert (
            nisc_sector_for(
                None,
                "pixivFANBOX、なりすましコメントで偽サイト誘導",
                "対策としてカード会社への連絡やパスワード変更が推奨されています。",
            )
            is None
        )

    def test_restricted_refinement_may_use_summary(self) -> None:
        # CI 系 canonical の精緻化は summary も使ってよい (被害者は energy と確定済)
        assert (
            nisc_sector_for("energy", "重要インフラ事業者で障害", "都市ガスの供給網に影響") == "gas"
        )

    def test_short_ascii_no_false_positive(self) -> None:
        # "ana" が "analysis" に、"jal" が "jalisco" に誤爆しない (境界一致)
        assert nisc_sector_for(None, "malware analysis report") is None
        assert nisc_sector_for(None, "APT campaign in Jalisco") is None
        # 日本語文脈の ANA は許容
        assert nisc_sector_for(None, "ANAホールディングスが被害") == "aviation"

    def test_all_ids_have_labels(self) -> None:
        for nisc_id in NISC_SECTORS:
            assert sector_label(nisc_id) != nisc_id  # 日本語ラベルがある


class TestOperator:
    def test_known_operators(self) -> None:
        assert operator_nisc_sector("東京電力ホールディングス") == "electricity"
        assert operator_nisc_sector("JR東日本") == "railway"
        assert operator_nisc_sector("NTTドコモ") == "it_telecom"

    def test_unknown_operator(self) -> None:
        assert operator_nisc_sector("架空商事株式会社") is None
        assert operator_nisc_sector(None) is None

    def test_short_alias_no_false_positive(self) -> None:
        # "ana" が "financial analysis firm" に、"ihi" が "vehicle" 等に誤爆しない
        assert operator_nisc_sector("financial analysis firm") is None
        assert operator_nisc_sector("banana republic corp") is None


class TestSectorsForTech:
    def test_scada_maps_to_ot_sectors(self) -> None:
        got = sectors_for_tech("Schneider Electric SCADA")
        assert "electricity" in got

    def test_medical_product(self) -> None:
        assert "medical" in sectors_for_tech("Philips DICOM viewer")

    def test_unknown_tech(self) -> None:
        assert sectors_for_tech("random office suite") == []
        assert sectors_for_tech(None) == []

    def test_short_tech_no_false_positive(self) -> None:
        # "abb" が "rabbit" に、"plc"/"dcs" が無関係語に誤爆しない (境界一致)
        assert sectors_for_tech("rabbit hole scanner") == []


class TestIcsTechnique:
    @pytest.mark.parametrize("tid", ["T0883", "T0889", "T0800", "t0836"])
    def test_ics_detected(self, tid: str) -> None:
        assert is_ics_technique(tid) is True
        assert technique_matrix(tid) == "ics"

    @pytest.mark.parametrize("tid", ["T1566", "T1190", "T1059.001"])
    def test_enterprise_not_ics(self, tid: str) -> None:
        assert is_ics_technique(tid) is False
        assert technique_matrix(tid) == "enterprise"

    def test_invalid(self) -> None:
        assert is_ics_technique("not a tid") is False
        assert is_ics_technique(None) is False
        assert technique_matrix(None) == "unknown"


class TestSystemDomain:
    def test_ics_ttp_is_ot(self) -> None:
        assert derive_system_domain(text="侵入", ttps=["T0883"], affected_products=[]) == "ot"

    def test_ics_product_is_ot(self) -> None:
        assert (
            derive_system_domain(text="脆弱性", ttps=[], affected_products=["Triconex controller"])
            == "ot"
        )

    def test_ot_keyword_is_ot(self) -> None:
        assert (
            derive_system_domain(text="SCADA システムに侵入", ttps=[], affected_products=[]) == "ot"
        )

    def test_boundary_strong_keyword_standalone(self) -> None:
        # HMI/historian 等は本質的に OT 文脈語 → 単独で境界
        assert (
            derive_system_domain(text="HMIサーバに不審なアクセス", ttps=[], affected_products=[])
            == "boundary"
        )

    def test_boundary_weak_keyword_needs_ot_context(self) -> None:
        # リモート保守/踏み台は IT 侵害でも頻出する一般語 → OT 文脈の共起があるときのみ境界
        assert (
            derive_system_domain(
                text="工場のリモート保守回線経由で侵入", ttps=[], affected_products=[]
            )
            == "boundary"
        )
        # OT 文脈なしの リモート保守/踏み台 は境界にしない (保守的既定 = unknown)
        assert (
            derive_system_domain(
                text="リモート保守用の踏み台経由で侵入", ttps=[], affected_products=[]
            )
            == "unknown"
        )

    def test_perimeter_defense_not_boundary(self) -> None:
        # 「境界型防御」(ネットワーク境界) は OT/IT 境界ではない
        assert (
            derive_system_domain(text="境界型防御を突破された", ttps=[], affected_products=[])
            == "unknown"
        )

    def test_english_substring_not_ot(self) -> None:
        # "ics" ⊂ "forensics"/"graphics" の部分一致で OT にしない (境界一致)
        assert (
            derive_system_domain(
                text="digital forensics and graphics benchmark", ttps=[], affected_products=[]
            )
            == "unknown"
        )

    def test_it(self) -> None:
        assert (
            derive_system_domain(text="顧客情報が情報漏洩", ttps=[], affected_products=[]) == "it"
        )

    def test_conservative_unknown(self) -> None:
        # 憶測で OT にしない: 手掛かり無しは unknown
        assert (
            derive_system_domain(text="サイバー攻撃を受けた", ttps=[], affected_products=[])
            == "unknown"
        )

    def test_hr_ransomware_not_ot(self) -> None:
        # 電力会社の人事系ランサム = OT でない (過大評価の減衰)
        got = derive_system_domain(
            text="電力会社の業務システムがランサムウェア被害、顧客情報が流出",
            ttps=[],
            affected_products=[],
        )
        assert got == "it"
