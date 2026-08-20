"""ファイルカタログ (区分/説明 SSoT) のテスト。

核心は網羅性の強制: prompts/*.j2 / config/*.yaml を追加したら
カタログにも 1 行足す、という運用規約をテストで固定する。
"""

from __future__ import annotations

from pathlib import Path

from src.ui.services.file_catalog import (
    CONFIG_CATALOG,
    CONFIG_CATEGORY_ORDER,
    PROMPT_CATALOG,
    PROMPT_CATEGORY_ORDER,
    annotate_files,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestCatalogCompleteness:
    def test_every_prompt_file_is_cataloged(self) -> None:
        # rglob 格上げ (2026-08-16): 区分サブディレクトリ (briefing/ 等) 配下も網羅する。
        # glob("*.j2") はトップレベルしか見ないため、区分サブディレクトリのファイルが
        # 未登録でも検出できなかった (F7)。
        actual = {
            f"prompts/{p.relative_to(_REPO_ROOT / 'prompts').as_posix()}"
            for p in (_REPO_ROOT / "prompts").rglob("*.j2")
        }
        # +status_synthesis_skeleton (層分け 2 本目 2026-08-20)
        # +weekly_recap_skeleton / pir_daily_focus_skeleton / deep_dive_rubric_skeleton /
        # pir_spotlight_skeleton (層分け 3 本目 2026-08-20、digest/spotlight 群 4 本)
        # +ground_ach_skeleton / ground_incremental_skeleton / nominate_skeleton /
        # detect_new_skeleton / adversarial_skeleton / render_skeleton
        # (層分け 7〜12 本目 2026-08-20、grounded ACH 群 6 本)
        assert len(actual) == 25
        missing = actual - set(PROMPT_CATALOG)
        assert not missing, f"カタログ未登録のプロンプト: {sorted(missing)}"

    def test_every_config_file_is_cataloged(self) -> None:
        actual = {
            f"config/{p.relative_to(_REPO_ROOT / 'config').as_posix()}"
            for p in (_REPO_ROOT / "config").rglob("*.yaml")
        }
        # +status_synthesis_rubric seed (層分け 2 本目 2026-08-20)
        # +weekly_recap_rubric / pir_daily_focus_rubric / deep_dive_rubric /
        # pir_spotlight_rubric seed (層分け 3 本目 2026-08-20)
        # +ground_ach_rubric / ground_incremental_rubric / nominate_rubric /
        # detect_new_rubric / adversarial_rubric / synthesis_render_rubric seed
        # (層分け 7〜12 本目 2026-08-20)
        # +ioc_llm_verifier_rubric seed (group 4 の 1 本目、python_block kind、2026-08-21)
        assert len(actual) == 29
        missing = actual - set(CONFIG_CATALOG)
        assert not missing, f"カタログ未登録の設定ファイル: {sorted(missing)}"

    def test_catalog_has_no_ghost_entries(self) -> None:
        # 削除済みファイルのカタログ残骸を検出 (_persona_local は gitignore のため任意)
        optional = {"prompts/_persona_local.j2"}
        for key in list(PROMPT_CATALOG) + list(CONFIG_CATALOG):
            if key in optional:
                continue
            assert (_REPO_ROOT / key).exists(), f"実体の無いカタログエントリ: {key}"

    def test_categories_are_known(self) -> None:
        for info in PROMPT_CATALOG.values():
            assert info.category in PROMPT_CATEGORY_ORDER
        for info in CONFIG_CATALOG.values():
            assert info.category in CONFIG_CATEGORY_ORDER

    def test_prompt_category_includes_new_group(self) -> None:
        # summarizer_rubric.yaml の追加に伴う新区分 (2026-08-16)
        assert "プロンプト" in CONFIG_CATEGORY_ORDER


class TestAnnotateFiles:
    def test_groups_follow_category_order(self) -> None:
        files = ["prompts/_persona.j2", "prompts/briefing/summarizer.j2"]
        groups = annotate_files(files, PROMPT_CATALOG, PROMPT_CATEGORY_ORDER)
        assert [g["category"] for g in groups] == ["ブリーフィング", "共有パーツ"]

    def test_unknown_file_falls_back_to_misc(self) -> None:
        groups = annotate_files(["prompts/brand_new.j2"], PROMPT_CATALOG, PROMPT_CATEGORY_ORDER)
        assert groups == [
            {
                "category": "その他",
                "files": [
                    {"path": "prompts/brand_new.j2", "label": "brand_new.j2", "description": ""}
                ],
            }
        ]

    def test_entry_carries_label_and_description(self) -> None:
        groups = annotate_files(
            ["config/sources/feeds.yaml"], CONFIG_CATALOG, CONFIG_CATEGORY_ORDER
        )
        entry = groups[0]["files"][0]
        assert entry["label"] == "RSS feed 購読"
        assert "seed" in entry["description"]
