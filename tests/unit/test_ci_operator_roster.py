"""ci_operator_roster (JP CI 指定事業者名簿 + 事業者型判定) のテスト。

核心: (1) BUILTIN 名簿の整合性 (id/sector/alias 衝突なし)、(2) 層1 名簿照合が別名
(Aflac Japan/アフラック) を同一事業者に解決する、(3) 層2 型判定が中小 CI 事業者
(〜病院/〜市/〜銀行) を回収しつつ非 CI (fintech SaaS/商社) を通さない、
(4) DB (config_store) が SSoT で BUILTIN は fail-safe。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.cti.ci_operator_roster import (
    SYSTEMIC_NODE_IDS,
    OperatorDef,
    classify_org,
    designated_counts_by_sector,
    invalidate_operators_cache,
    load_operators,
    validate_operators,
)
from src.cti.ci_operators_builtin import BUILTIN_OPERATORS_DATA
from src.cti.nisc_sectors import NISC_SECTORS


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    invalidate_operators_cache()
    yield
    invalidate_operators_cache()


class TestBuiltinIntegrity:
    def test_ids_unique(self) -> None:
        ids = [row[0] for row in BUILTIN_OPERATORS_DATA]
        assert len(ids) == len(set(ids))

    def test_sectors_valid(self) -> None:
        for row in BUILTIN_OPERATORS_DATA:
            assert row[3] in NISC_SECTORS, row[0]

    def test_no_alias_collision_across_operators(self) -> None:
        # canonical + aliases が別事業者間で重複しない (誤 merge / 誤帰属防止)
        seen: dict[str, str] = {}
        for op_id, canonical, aliases, _sector in BUILTIN_OPERATORS_DATA:
            for name in (canonical, *aliases):
                low = name.lower()
                assert low not in seen, f"alias 衝突: {name} ({seen.get(low)} vs {op_id})"
                seen[low] = op_id

    def test_validate_builtin_clean(self) -> None:
        ops = load_operators()
        assert validate_operators([o.as_dict() for o in ops]) == []


class TestDesignatedMatch:
    def test_alias_bridges_jp_en_names(self) -> None:
        # アフラック: 和名/英名の報道が同一事業者に解決される (Aflac 別名 merge の土台)
        a = classify_org("アフラック生命保険株式会社")
        b = classify_org("Aflac Japan")
        assert a is not None and b is not None
        assert a.tier == "designated" and b.tier == "designated"
        assert a.operator_id == b.operator_id
        assert a.sector == "finance"

    def test_subsidiary_and_suffix_normalization(self) -> None:
        got = classify_org("KDDI株式会社")
        assert got is not None and got.operator_id == "kddi" and got.sector == "it_telecom"

    def test_designated_beats_type(self) -> None:
        # 名簿該当は型より優先 (東京電力 → roster の electricity)
        got = classify_org("東京電力ホールディングス")
        assert got is not None and got.tier == "designated" and got.sector == "electricity"

    def test_short_ascii_alias_no_false_positive(self) -> None:
        # ANA/JAL 等の短 ASCII 別名が英単語に誤爆しない (length-aware 教訓)
        assert classify_org("banana republic corp") is None
        assert classify_org("financial analysis firm") is None

    def test_fullwidth_ascii_normalized(self) -> None:
        # 官報・公的文書は全角英字が多い (経済安保指定一覧の「ＮＴＴ東日本」で実証)
        got = classify_org("ＮＴＴ東日本株式会社")
        assert got is not None and got.tier == "designated" and got.sector == "it_telecom"
        got2 = classify_org("ＫＤＤＩ株式会社")
        assert got2 is not None and got2.operator_id == "kddi"

    def test_fullwidth_punctuation_normalized_on_both_sides(self) -> None:
        # 名簿側の名前にも NFKC を適用 (全角「．」入り社名が半角表記の org と一致)
        for org in ("ニッケル．エンド．ライオンス株式会社", "ニッケル.エンド.ライオンス"):
            got = classify_org(org)
            assert got is not None and got.tier == "designated" and got.sector == "port", org

    def test_designated_covers_economic_security_list_samples(self) -> None:
        # 経済安保推進法 特定社会基盤事業者 (令和8年7月1日版) の代表例が名簿で解決される
        cases = {
            "東京電力パワーグリッド株式会社": "electricity",
            "相馬共同火力発電株式会社": "electricity",
            "日本製鉄株式会社": "electricity",  # 発電事業 (50万kW+) としての指定
            "仙台市ガス局": "gas",
            "アストモスエネルギー株式会社": "oil",
            "札幌市水道局": "water",
            "名港海運株式会社": "port",
            "山九株式会社": "port",
            "LINEヤフー株式会社": "it_telecom",
            "日本放送協会": "it_telecom",  # 放送は NISC 情報通信分野の一部
            "株式会社TBSテレビ": "it_telecom",
            "信金中央金庫": "finance",
            "農林中央金庫": "finance",
            "株式会社メルペイ": "finance",
            "PayPay株式会社": "finance",
            "あいおいニッセイ同和損害保険株式会社": "finance",
            "株式会社パスモ": "credit",
            "PayPayカード株式会社": "credit",
            "沖縄セルラー電話株式会社": "it_telecom",
            "福岡国際空港株式会社": "airport",
        }
        for org, sector in cases.items():
            got = classify_org(org)
            assert got is not None, org
            assert got.tier == "designated", org
            assert got.sector == sector, org

    def test_nisshin_port_no_false_positive(self) -> None:
        # 港湾の「(株)日新」は 2 文字中核名 — 「日新電機」等に誤爆しない収載形
        got = classify_org("日新電機株式会社")
        assert got is None or got.sector != "port"

    def test_paypay_bank_not_merged_into_paypay(self) -> None:
        # PayPay銀行 (指定外・別法人) が資金移動 PayPay に誤 merge されない
        got = classify_org("PayPay銀行株式会社")
        assert got is not None and got.canonical == "PayPay銀行"


class TestTypeMatch:
    @pytest.mark.parametrize(
        ("org", "sector"),
        [
            ("佐渡市", "government"),
            ("和歌山県", "government"),
            ("山口地方検察庁岩国支部", "government"),
            ("警視庁", "government"),
            ("国立病院機構北海道医療センター", "medical"),
            ("半田病院", "medical"),
            ("西日本シティ銀行", "finance"),
            ("益田信用組合", "finance"),
            ("陸上自衛隊中部方面総監部", "defense"),
            ("九州電力送配電", "electricity"),
            ("北陸ガス", "gas"),
            ("大阪有機化学工業株式会社", "chemical"),
            ("東京都水道局", "water"),
            ("ビューカード", "credit"),
            ("福井鉄道", "railway"),
            ("トキエア", None),  # 航空は「航空」を含む org のみ (社名だけでは判定しない)
        ],
    )
    def test_type_patterns(self, org: str, sector: str | None) -> None:
        got = classify_org(org)
        if sector is None:
            assert got is None or got.tier == "designated"
        else:
            assert got is not None, org
            assert got.sector == sector, org
            assert got.tier in ("designated", "type")

    def test_air_self_defense_force_is_defense_not_aviation(self) -> None:
        got = classify_org("航空自衛隊美保基地")
        assert got is not None and got.sector == "defense"

    def test_non_ci_orgs_not_matched(self) -> None:
        # 層3 (fintech SaaS/商社/ポイント/クラウドPBX) は名簿にも型にも合致しない
        for org in (
            "株式会社マネーフォワード",
            "サイバーエージェント",
            "UPSIDERホールディングス",
            "株式会社ソフツー",
            "株式会社樋口商会",
            "株式会社マルタケ",
            "かわさきロケ情報",
            "九州大学",  # 大学 = education (非 CI)。大学病院なら medical
        ):
            assert classify_org(org) is None, org

    def test_kyodo_news_not_telecom(self) -> None:
        # 「〜通信」(報道機関) を情報通信に誤判定しない
        assert classify_org("共同通信社") is None
        assert classify_org("時事通信社") is None


class TestDbOverride:
    def test_db_value_wins_over_builtin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {
                "id": "test_op",
                "canonical": "テスト重要インフラ",
                "aliases": ["TestCI"],
                "sector": "electricity",
            }
        ]
        monkeypatch.setattr("src.storage.config_store.get_config", lambda key, **kw: rows)
        invalidate_operators_cache()
        ops = load_operators()
        assert [o.id for o in ops] == ["test_op"]
        got = classify_org("テスト重要インフラ株式会社")
        assert got is not None and got.operator_id == "test_op"

    def test_builtin_failsafe_when_db_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.storage.config_store.get_config", lambda key, **kw: None)
        invalidate_operators_cache()
        ops = load_operators()
        assert len(ops) > 100  # BUILTIN 名簿


class TestSystemicNodes:
    def test_systemic_ids_exist_in_roster(self) -> None:
        ids = {row[0] for row in BUILTIN_OPERATORS_DATA}
        for node_id in SYSTEMIC_NODE_IDS:
            assert node_id in ids, node_id

    def test_clearing_node_flagged_systemic(self) -> None:
        # 全銀ネット (資金清算) = 分野横断カスケードの単一障害点
        got = classify_org("全国銀行資金決済ネットワーク")
        assert got is not None and got.systemic is True

    def test_exchange_and_csd_flagged_systemic(self) -> None:
        for org in ("株式会社東京証券取引所", "証券保管振替機構"):
            got = classify_org(org)
            assert got is not None and got.systemic is True, org

    def test_ordinary_bank_not_systemic(self) -> None:
        # 代替可能な大手 (地銀・メガバンク) は集中ノードではない
        got = classify_org("株式会社千葉銀行")
        assert got is not None and got.tier == "designated" and got.systemic is False

    def test_type_match_never_systemic(self) -> None:
        got = classify_org("半田病院")
        assert got is not None and got.tier == "type" and got.systemic is False


class TestDesignatedCounts:
    def test_counts_cover_all_sectors(self) -> None:
        counts = designated_counts_by_sector()
        assert set(counts) <= set(NISC_SECTORS)
        # 主要分野は複数の指定事業者を持つ
        assert counts["finance"] >= 20
        assert counts["electricity"] >= 10
        assert counts["it_telecom"] >= 15

    def test_count_equals_roster_grouping(self) -> None:
        counts = designated_counts_by_sector()
        assert sum(counts.values()) == len(load_operators())


class TestExpandedCoverage:
    def test_broadcast_type_pattern(self) -> None:
        # 地方放送局は型判定 (放送$/テレビ$) で情報通信に回収 (キー局は名簿)
        for org in ("北海道放送株式会社", "RKB毎日放送", "中京テレビ放送株式会社"):
            got = classify_org(org)
            assert got is not None and got.sector == "it_telecom", org

    def test_hoso_daigaku_not_broadcast(self) -> None:
        # 「放送大学」は大学 (education) — 放送$ 末尾一致で誤爆しない
        got = classify_org("放送大学")
        assert got is None or got.sector != "it_telecom"

    def test_credit_information_agency_in_roster(self) -> None:
        # 指定信用情報機関は信用インフラの集中点
        got = classify_org("株式会社シー・アイ・シー")
        assert got is not None and got.sector == "credit"

    def test_defense_specialists_added(self) -> None:
        for org in ("新明和工業株式会社", "豊和工業株式会社"):
            got = classify_org(org)
            assert got is not None and got.sector == "defense", org

    def test_diversified_giant_not_force_routed_to_defense(self) -> None:
        # 日立・東芝・ダイキン等の多角化巨大企業は defense に強制配置しない
        # (民生事業の侵害が defense に誤流入 = 過剰帰属再発を防ぐ)
        for org in ("株式会社日立製作所", "ダイキン工業株式会社"):
            got = classify_org(org)
            assert got is None or got.sector != "defense", org


class TestValidation:
    def test_duplicate_id_rejected(self) -> None:
        rows = [
            {"id": "x", "canonical": "A社", "aliases": [], "sector": "electricity"},
            {"id": "x", "canonical": "B社", "aliases": [], "sector": "gas"},
        ]
        errs = validate_operators(rows)
        assert any("id" in e for e in errs)

    def test_unknown_sector_rejected(self) -> None:
        rows = [{"id": "x", "canonical": "A社", "aliases": [], "sector": "nuclear"}]
        errs = validate_operators(rows)
        assert any("sector" in e or "分野" in e for e in errs)

    def test_alias_collision_rejected(self) -> None:
        rows = [
            {"id": "a", "canonical": "A社", "aliases": ["共通名"], "sector": "electricity"},
            {"id": "b", "canonical": "B社", "aliases": ["共通名"], "sector": "gas"},
        ]
        errs = validate_operators(rows)
        assert any("共通名" in e for e in errs)

    def test_empty_canonical_rejected(self) -> None:
        rows = [{"id": "x", "canonical": "", "aliases": [], "sector": "electricity"}]
        assert validate_operators(rows) != []


def _op(op_id: str, canonical: str, sector: str, *aliases: str) -> OperatorDef:
    return OperatorDef(id=op_id, canonical=canonical, aliases=tuple(aliases), sector=sector)


class TestOperatorDef:
    def test_as_dict_roundtrip(self) -> None:
        op = _op("x", "X社", "electricity", "エックス")
        d = op.as_dict()
        assert d["id"] == "x" and d["aliases"] == ["エックス"]
