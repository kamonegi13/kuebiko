"""source_quality の構造化保存 (render → roundtrip) のテスト (ブリーフ設定 UI 用)。"""

from __future__ import annotations

from pathlib import Path

from src.config_loader import (
    KNOWN_ARTICLE_CATEGORIES,
    SourceQualityConfig,
    load_source_quality,
    render_source_quality_yaml,
)


def _render_and_load(tmp_path: Path, cfg: SourceQualityConfig) -> SourceQualityConfig:
    p = tmp_path / "sq.yaml"
    p.write_text(render_source_quality_yaml(cfg), encoding="utf-8")
    return load_source_quality(p)


def test_render_roundtrips(tmp_path: Path) -> None:
    cfg = SourceQualityConfig(
        brief_cap_24h=20, high_threat_brief_categories=("apt", "vulnerability", "malware")
    )
    text = render_source_quality_yaml(cfg)
    # doc コメント (header) が保持される (Phase B-cal の経緯を失わない)
    assert text.lstrip().startswith("#")
    assert "Phase B-cal" in text
    back = _render_and_load(tmp_path, cfg)
    assert back.brief_cap_24h == 20
    assert back.high_threat_brief_categories == ("apt", "vulnerability", "malware")


def test_known_categories_cover_defaults() -> None:
    # 既定の high_threat カテゴリは全て既知語彙に含まれる (checklist で表現可能)
    for c in SourceQualityConfig().high_threat_brief_categories:
        assert c in KNOWN_ARTICLE_CATEGORIES


def test_render_empty_categories(tmp_path: Path) -> None:
    # 空リストは null でなく [] で出力され、roundtrip できる
    cfg = SourceQualityConfig(brief_cap_24h=0, high_threat_brief_categories=())
    back = _render_and_load(tmp_path, cfg)
    assert back.brief_cap_24h == 0
    assert back.high_threat_brief_categories == ()
