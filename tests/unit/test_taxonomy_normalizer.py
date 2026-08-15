"""Phase H: taxonomy_normalizer のテスト。

raw -> canonical 変換、union merge、未知値の uncategorized fallback を検証。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.cti.taxonomy_normalizer import (
    PMESII_AXES,
    load_normalizer,
)


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    for _sub in ("sources", "cti", "delivery"):
        (config / _sub).mkdir(exist_ok=True)

    sectors_yaml = config / "cti/victim_sectors.yaml"
    sectors_yaml.write_text(
        yaml.safe_dump(
            {
                "canonical": {
                    "financial": {
                        "display": "金融",
                        "aliases": ["finance", "bank", "fintech", "金融"],
                    },
                    "healthcare": {
                        "display": "医療",
                        "aliases": ["healthcare", "hospital", "医療"],
                    },
                    "other": {"display": "その他", "aliases": []},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    countries_yaml = config / "cti/countries.yaml"
    countries_yaml.write_text(
        yaml.safe_dump(
            {
                "canonical": {
                    "JP": {"display": "日本", "aliases": ["日本", "Japan", "JP"]},
                    "US": {"display": "米国", "aliases": ["US", "USA", "アメリカ"]},
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    mapping_yaml = config / "cti/pmesii_default_mapping.yaml"
    mapping_yaml.write_text(
        yaml.safe_dump(
            {
                "feed_to_axes": {
                    "Mandiant": ["M", "I-cyber"],
                    "Dragos": ["I-infra", "M"],
                    "Sputnik Globe (Russia)": ["S", "P"],
                },
                "category_to_axes": {
                    "apt": ["M", "I-cyber"],
                    "policy": ["P", "S"],
                    "vulnerability": ["I-cyber", "P"],
                },
            },
        ),
        encoding="utf-8",
    )
    return config


class TestSectorNormalization:
    def test_canonical_match_exact(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, raw = n.normalize_sector("finance")
        assert canon == "financial"
        assert raw == "finance"

    def test_canonical_match_japanese(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, raw = n.normalize_sector("金融")
        assert canon == "financial"

    def test_unknown_value_returns_uncategorized(
        self,
        tmp_config_dir: Path,
    ) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, raw = n.normalize_sector("aerospace")
        assert canon == "uncategorized"
        assert raw == "aerospace"

    def test_empty_input(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, raw = n.normalize_sector(None)
        assert canon is None
        assert raw is None
        canon, raw = n.normalize_sector("")
        assert canon is None
        assert raw is None

    def test_case_insensitive(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, _ = n.normalize_sector("FINANCE")
        assert canon == "financial"

    def test_trim_whitespace(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        canon, _ = n.normalize_sector("  finance  ")
        assert canon == "financial"


class TestCountryNormalization:
    def test_iso_match(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        iso, raw = n.normalize_country("Japan")
        assert iso == "JP"
        assert raw == "Japan"

    def test_japanese_match(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        iso, _ = n.normalize_country("日本")
        assert iso == "JP"

    def test_iso_code_direct(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        iso, _ = n.normalize_country("JP")
        assert iso == "JP"

    def test_unknown_country(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        iso, raw = n.normalize_country("Kazakhstan")
        assert iso is None  # 未知国は ISO なし、raw のみ保持
        assert raw == "Kazakhstan"

    def test_strips_json_artifact_quotes_and_commas(self, tmp_config_dir: Path) -> None:
        # LLM 出力が JSON 風に漏れた "US'," / "[US]" / "Japan'," を救済 (raw は保持)
        n = load_normalizer(config_dir=tmp_config_dir)
        for noisy in ("US',", "'US'", "[US]", "Japan',", " US , "):
            iso, raw = n.normalize_country(noisy)
            assert iso in {"US", "JP"}, noisy
            assert raw == noisy.strip()

    def test_does_not_split_multi_country(self, tmp_config_dir: Path) -> None:
        # 内部カンマは触らない → 複数国はあくまで unresolved (誤って単一国に潰さない)
        n = load_normalizer(config_dir=tmp_config_dir)
        iso, _ = n.normalize_country("US, Japan")
        assert iso is None


class TestAxesMerge:
    def test_union_strategy(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        merged = n.merge_axes(
            llm_axes=["M"],
            feed_title="Mandiant",
            category="apt",
        )
        # LLM: M / feed: M+I-cyber / category: M+I-cyber → union = {M, I-cyber}
        assert set(merged) == {"M", "I-cyber"}

    def test_invalid_axes_filtered(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        merged = n.merge_axes(
            llm_axes=["M", "INVALID_AXIS", "I-cyber"],
            feed_title=None,
            category=None,
        )
        assert "INVALID_AXIS" not in merged
        assert "M" in merged
        assert "I-cyber" in merged

    def test_feed_only(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        merged = n.merge_axes(
            llm_axes=[],
            feed_title="Sputnik Globe (Russia)",
            category=None,
        )
        assert set(merged) == {"S", "P"}

    def test_category_only(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        merged = n.merge_axes(
            llm_axes=[],
            feed_title="Unknown Feed",
            category="vulnerability",
        )
        assert set(merged) == {"I-cyber", "P"}

    def test_all_empty(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        merged = n.merge_axes(llm_axes=[], feed_title=None, category=None)
        assert merged == []


class TestAxesValidationSet:
    def test_pmesii_axes_constant(self) -> None:
        assert (
            frozenset(
                {"P", "M", "E", "S", "I-infra", "I-cyber", "P-env", "T"},
            )
            == PMESII_AXES
        )


class TestNormalizerLoad:
    def test_missing_yaml_falls_back_to_empty(self, tmp_path: Path) -> None:
        """yaml 不在 → 空の dict で動作 (uncategorized になるだけで crash しない)。"""
        n = load_normalizer(config_dir=tmp_path)  # yaml なし
        canon, _ = n.normalize_sector("anything")
        assert canon == "uncategorized" or canon is None


class TestSectorCanonicalList:
    def test_list_canonical_returns_all(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        cans = n.list_sector_canonical()
        assert "financial" in cans
        assert "healthcare" in cans
        assert "other" in cans

    def test_list_country_canonical(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        cans = n.list_country_canonical()
        assert "JP" in cans
        assert "US" in cans


def test_real_config_new_sector_aliases() -> None:
    """config/cti/victim_sectors.yaml に追加した安全 alias が正しい canonical に写ること (回帰)。

    実 config を読む (fixture でなく)。辞書から alias が消えたら uncategorized に戻り検知できる。
    """
    n = load_normalizer()  # 既定 = 実 config/
    cases = {
        "telecommunication": "telecom",
        "network infrastructure": "telecom",
        "it services": "technology",
        "it managed service provider (msp)": "technology",
        "internet services": "technology",
        "non-profit": "ngo",
        "entertainment": "media",
        # 新カテゴリ (2026-06-20): food_agriculture / professional_services + 既存への写像。
        "agriculture and food production": "food_agriculture",
        "food & beverage": "food_agriculture",
        "professional services": "professional_services",
        "legal": "professional_services",
        "maritime": "critical_infra",
        "construction": "enterprise",
        "consumer services": "enterprise",
        "宿泊・観光": "enterprise",
    }
    for raw, expected in cases.items():
        canon, _ = n.normalize_sector(raw)
        assert canon == expected, f"{raw} -> {canon} (expected {expected})"


class TestNullSentinels:
    """抽出 LLM の「特定できなかった」番兵語は空入力と同義 (2026-07-12 根治)。"""

    def test_sector_sentinels_normalize_to_none(self) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        n = load_normalizer()
        for raw in ("Not Found", "unknown", "不明", "unspecified", "N/A", "なし"):
            assert n.normalize_sector(raw) == (None, None), raw

    def test_country_sentinels_normalize_to_none(self) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        n = load_normalizer()
        for raw in ("Not Found", "unknown", "不明"):
            assert n.normalize_country(raw) == (None, None), raw

    def test_real_values_still_normalize(self) -> None:
        from src.cti.taxonomy_normalizer import load_normalizer

        n = load_normalizer()
        assert n.normalize_sector("金融") == ("financial", "金融")
        assert n.normalize_sector("交通") == ("critical_infra", "交通")
        assert n.normalize_sector("化学") == ("manufacturing", "化学")
        assert n.normalize_sector("cybersecurity") == ("technology", "cybersecurity")


class TestNormalizeCountryScope:
    """victim_country スコープ判定 (監査 2026-08-01 ⑥)。

    briefing/summarizer.j2 は複数国攻撃に "global"/"EU"/"APAC" を指示しているのに正規化器に
    受け皿が無く、月 175 件が黙って iso=NULL に落ちていた断線の閉鎖。ISO2 の意味
    (単一国) は不変 — スコープは別値で表現する。
    """

    def test_global_tokens(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        assert n.normalize_country_scope("global") == ("global", ())
        assert n.normalize_country_scope("Worldwide") == ("global", ())
        assert n.normalize_country_scope("世界") == ("global", ())

    def test_regional_tokens(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        assert n.normalize_country_scope("EU") == ("regional", ())
        assert n.normalize_country_scope("APAC") == ("regional", ())
        assert n.normalize_country_scope("中東") == ("regional", ())

    def test_multi_country_resolves_parts(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        scope, isos = n.normalize_country_scope("US, Japan")
        assert scope == "multi"
        assert set(isos) == {"US", "JP"}

    def test_unknown_value_stays_none(self, tmp_config_dir: Path) -> None:
        n = load_normalizer(config_dir=tmp_config_dir)
        assert n.normalize_country_scope("Atlantis") == (None, ())
        assert n.normalize_country_scope("") == (None, ())
        assert n.normalize_country_scope(None) == (None, ())

    def test_single_country_is_not_scope(self, tmp_config_dir: Path) -> None:
        # 単一国は normalize_country の領分 (scope にしない)
        n = load_normalizer(config_dir=tmp_config_dir)
        assert n.normalize_country_scope("Japan") == (None, ())
