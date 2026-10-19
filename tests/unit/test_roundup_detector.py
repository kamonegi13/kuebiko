"""まとめ記事の判定と重要度上限 (U2、2026-10-18)。"""

from __future__ import annotations

import pytest

from src.cti.roundup_detector import (
    BASIS_LLM,
    BASIS_TITLE,
    cap_mode,
    cap_roundup_importance,
    is_roundup,
    roundup_basis,
)


@pytest.mark.parametrize(
    "title",
    [
        "週刊サイバー脅威まとめ:Cisco 0-day",
        "AhnLab 週報:新ランサムウェアグループ",
        "【資安週報】1012~1016",
        "CERT-FR 週次ニュースレター 2026-10-05",
        "今週の気になるセキュリティニュース - Issue #293",
        "Weekly Report: Google Chromeに複数の脆弱性",
        "ThreatsDay: Adobe RCE + 他 23 件",
        "Security Affairs マルウェアニュースレター第118回",
    ],
)
def test_periodical_title_is_roundup_by_title(title: str) -> None:
    assert roundup_basis(title) == BASIS_TITLE


@pytest.mark.parametrize(
    "title",
    [
        "Yandexのデータセンター障害についてまとめてみた",
        "韓国の金融機関で相次いだ不正アクセスについてまとめてみた",
        "ゴルフダイジェスト・オンラインに不正アクセス",
        "Check Point の 2025 年 2 月報告と類似と指摘",
        "さくらインターネット、不正アクセス 第3報",
    ],
)
def test_single_incident_title_is_not_roundup_by_title(title: str) -> None:
    assert roundup_basis(title) is None


def test_llm_recap_without_other_signal_is_roundup_by_llm() -> None:
    assert (
        roundup_basis(
            "ウクライナ戦況アップデート", llm_article_type="recap", category="geopolitical"
        )
        == BASIS_LLM
    )


def test_llm_recap_in_breach_or_policy_is_not_trusted() -> None:
    assert roundup_basis("某社の情報漏えい", llm_article_type="recap", category="breach") is None
    assert roundup_basis("某法制の解説", llm_article_type="recap", category="policy") is None


def test_non_recap_without_periodical_title_is_not_roundup() -> None:
    assert not is_roundup("某社への不正アクセス", llm_article_type="breaking", category="breach")


def test_cap_defaults_to_shadow_and_does_not_change_importance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROUNDUP_IMPORTANCE_CAP", raising=False)
    assert cap_mode() == "shadow"
    r = cap_roundup_importance("high", "週刊まとめ", llm_article_type="recap", category="other")
    assert (r.importance, r.would_cap, r.applied, r.basis) == ("high", True, False, BASIS_TITLE)


def test_cap_title_mode_caps_only_title_basis() -> None:
    a = cap_roundup_importance("high", "週刊まとめ", mode="title")
    assert (a.importance, a.applied) == ("medium", True)
    b = cap_roundup_importance(
        "high",
        "ウクライナ戦況アップデート",
        llm_article_type="recap",
        category="geopolitical",
        mode="title",
    )
    assert (b.importance, b.would_cap, b.applied) == ("high", True, False)


def test_cap_on_mode_caps_llm_basis_too() -> None:
    r = cap_roundup_importance(
        "high",
        "ウクライナ戦況アップデート",
        llm_article_type="recap",
        category="geopolitical",
        mode="on",
    )
    assert (r.importance, r.applied) == ("medium", True)


def test_cap_off_and_non_high_are_untouched() -> None:
    assert not cap_roundup_importance("high", "週刊まとめ", mode="off").would_cap
    assert cap_roundup_importance("medium", "週刊まとめ", mode="on").importance == "medium"
    assert cap_roundup_importance("low", "週刊まとめ", mode="on").importance == "low"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", "off"), ("off", "off"), ("1", "on"), ("on", "on"), ("title", "title"), ("x", "shadow")],
)
def test_cap_mode_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("ROUNDUP_IMPORTANCE_CAP", raw)
    assert cap_mode() == expected
