"""組織名の照合キー (src/cti/org_normalizer.py)。fixture はすべて架空の組織名。"""

from __future__ import annotations

import pytest

from src.cti.org_normalizer import org_match_key, org_names_compatible


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("株式会社サンプル商事", "サンプル商事"),
        ("サンプル商事株式会社", "(株)サンプル商事"),
        ("Example Widgets, Inc.", "Example Widgets"),
        ("Example Widgets Corporation", "example widgets"),
        ("Sample Logistics GmbH", "Sample Logistics"),
        ("Sample Law LLP", "Sample Law, PLLC"),
        ("Example Holdings Group", "Example"),
        ("サンプルホールディングス", "サンプル"),
        ("sampledomain.com", "SampleDomain"),
        ("Sample Register (SR)", "Sample Register"),
        ("学校法人 架空学園", "架空学園"),
        ("国立大学法人架空大学", "架空大学"),
        ("ＡＢＣ商事", "ABC商事"),  # 全角英字は NFKC で半角化
    ],
)
def test_same_org_surface_variants_share_a_key(a: str, b: str) -> None:
    assert org_match_key(a) == org_match_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Example Widgets", "Example Gadgets"),
        ("サンプル商事", "サンプル物産"),
        ("Co-op", "Op"),  # "co" をハイフン付き語から剥がさない
        ("Sample Bank", "Sample Bank Securities"),
    ],
)
def test_different_orgs_keep_different_keys(a: str, b: str) -> None:
    assert org_match_key(a) != org_match_key(b)


def test_generic_only_name_is_not_collapsed_to_empty() -> None:
    assert org_match_key("Group") == "group"
    assert org_match_key("株式会社") != ""


def test_empty_input_is_safe() -> None:
    assert org_match_key("") == ""


class TestOrgLooseCompatible:
    """焦点の食い違い判定用の緩い一致 (conflict を消す方向だけに使う)。架空の名前。"""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("さんぷる", "サンプル"),  # ひらがな ⇔ カタカナ
            ("サンプル", "サンプルー"),  # 長音符の有無
            ("サンプル", "サンプル運輸"),  # 略称は正式名の接頭辞
            ("sample", "samplelighting"),
            ("orgx", "orgx"),
        ],
    )
    def test_compatible(self, a: str, b: str) -> None:
        assert org_names_compatible(a, b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("サンプル運輸", "サンプル商事"),  # 共通の接頭辞だけでは同一にしない
            ("ab", "abc"),  # 短すぎる接頭辞は使わない
            ("allstate", "newcastle"),
            ("", "サンプル"),
        ],
    )
    def test_incompatible(self, a: str, b: str) -> None:
        assert not org_names_compatible(a, b)
