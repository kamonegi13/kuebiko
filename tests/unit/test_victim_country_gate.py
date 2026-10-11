"""victim_country_gate のテスト (2026-10-10)。

監査 (data/eval/victim_country_audit_v1.jsonl) で判明した「非インシデント記事の
被害国誤付与」を落とす関門の単体テスト。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.cti.victim_country_gate import (
    GATE_ENV,
    KEYWORD_GATE_ENV,
    NON_INCIDENT_ARTICLE_TYPES,
    should_drop_victim_country,
)
from src.pipeline.briefing import _build_briefing
from src.pipeline.summary import SummaryOutput
from src.tools.article_model import Article


class TestNonIncidentTypesWithoutOrgs:
    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_drops_when_no_victim_org_named(self, article_type: str) -> None:
        # Arrange
        victim_orgs: list[str] = []

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is True

    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_keeps_when_victim_org_named(
        self, article_type: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: 案 A (語の関門) は既定 on (2026-10-11) になったため、この base
        # org-check の単体テストは isolation のため keyword gate を明示的に無効化する
        # (keyword gate 自体の挙動は TestKeywordGate で別途検証する)。
        monkeypatch.setenv(KEYWORD_GATE_ENV, "0")
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is False

    @pytest.mark.parametrize("article_type", sorted(NON_INCIDENT_ARTICLE_TYPES))
    def test_whitespace_only_org_counts_as_none(self, article_type: str) -> None:
        # Arrange
        victim_orgs = ["   ", ""]

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is True


class TestIncidentTypesAlwaysKeep:
    @pytest.mark.parametrize("article_type", ["breaking", "recap"])
    def test_keeps_regardless_of_victim_orgs(self, article_type: str) -> None:
        # Arrange
        victim_orgs: list[str] = []

        # Act
        result = should_drop_victim_country(article_type, victim_orgs)

        # Assert
        assert result is False


class TestEnvFlagRollback:
    def test_flag_zero_disables_gate_even_without_org(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv(GATE_ENV, "0")

        # Act
        result = should_drop_victim_country("advisory", [])

        # Assert
        assert result is False

    def test_flag_unset_defaults_to_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        monkeypatch.delenv(GATE_ENV, raising=False)

        # Act
        result = should_drop_victim_country("press", [])

        # Assert
        assert result is True


class TestKeywordGate:
    """案 A (data/eval/c1_x_design.md §2.2): victim_org があっても、本文+要約に侵害・攻撃の
    語彙が無ければ victim_country を落とす。既定 on (VICTIM_COUNTRY_GATE_KEYWORD、
    2026-10-11 新標本での測り直し後。"0" で旧挙動 = victim_org があれば保持)。"""

    def test_flag_unset_drops_org_present_without_incident_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: flag 未設定は既定 on と同じ。侵害・攻撃の語彙が無ければ落とす
        monkeypatch.delenv(KEYWORD_GATE_ENV, raising=False)
        victim_orgs = ["Acme Corp"]
        body_text = "この会社は新しいセキュリティ対策ソフトウェアを発表した。"

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is True

    def test_flag_zero_keeps_org_present_regardless_of_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        monkeypatch.setenv(KEYWORD_GATE_ENV, "0")
        victim_orgs = ["Acme Corp"]
        body_text = "この記事には侵害や攻撃に関する記述は一切ありません。"

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    def test_flag_on_drops_org_present_without_incident_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: flag on + org あり + 侵害の語が本文に無い → 落とす
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]
        body_text = "この会社は新しいセキュリティ対策ソフトウェアを発表した。"

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is True

    def test_flag_on_keeps_org_present_with_japanese_incident_word(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: flag on + org あり + 本文に「不正アクセス」(日本語の侵害語) がある → 保持
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]
        body_text = "Acme Corp は不正アクセスを受け、顧客情報が流出した。"

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    @pytest.mark.parametrize(
        "body_text",
        [
            "The company confirmed a data breach affecting customers.",
            "Attackers compromised internal systems last week.",
            "The firm was hacked by an unknown group.",
            "A ransomware attack disrupted operations.",
            "Sensitive files were exfiltrated from the network.",
            "Customer records were stolen in the incident.",
            "Internal documents were leaked online.",
            "The victim organization notified regulators.",
            "Investigators found evidence of an intrusion.",
            "The breach involved unauthorized access to servers.",
            "Several endpoints were infected with malware.",
        ],
    )
    def test_flag_on_keeps_org_present_with_english_incident_words(
        self, monkeypatch: pytest.MonkeyPatch, body_text: str
    ) -> None:
        # Arrange
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    def test_flag_on_still_drops_when_no_org_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: org が無い記事は、本文に侵害の語があっても元からの条件 (org 無し) で落ちる
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs: list[str] = []
        body_text = "A major breach was reported but no victim was named."

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is True

    def test_flag_on_does_not_affect_incident_types(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange: breaking/recap は関門の対象外なので、キーワード条件も掛からない
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]
        body_text = "新製品発表のお知らせ。侵害や攻撃の記述は無い。"

        # Act
        result = should_drop_victim_country("breaking", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    def test_flag_on_defaults_empty_body_to_no_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: body_text 省略時は空文字扱いとなり、侵害の語が無いので落とす
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country("press", victim_orgs)

        # Assert
        assert result is True

    @pytest.mark.parametrize(
        "body_text",
        [
            "大阪府の個人情報書類が他の申請者へ誤送付された。",
            "担当課が住民の氏名を誤って交付した件、誤交付と判明。",
            "個人情報が記載された書類を誤って送付したと発表した。",
            "内部資料が誤公開されていたことが分かった。",
            "担当者が資料を誤って公開してしまった。",
            "対象外の情報を誤掲載していた。",
            "一覧に他人の情報を誤表示していた。",
            "個人情報が記載された USB を紛失したと発表した。",
            "本来保存すべき書類を誤破棄していた。",
        ],
    )
    def test_flag_on_keeps_org_present_with_japanese_accidental_leak_words(
        self, monkeypatch: pytest.MonkeyPatch, body_text: str
    ) -> None:
        # Arrange: 2026-10-11 追加 — 攻撃・侵害の語を使わない事故由来の漏えい (誤送付等) も
        # 侵害の事実として扱い、落とさない (P3 jp 層の見逃し 1 件への対処)
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    @pytest.mark.parametrize(
        "body_text",
        [
            "The documents were misdirected to the wrong recipient.",
            "An email was mistakenly sent to an unintended address.",
            "A laptop containing customer records was lost.",
            "Personal data was exposed due to a configuration error.",
            "The file was accidentally shared with the wrong group.",
        ],
    )
    def test_flag_on_keeps_org_present_with_english_accidental_leak_words(
        self, monkeypatch: pytest.MonkeyPatch, body_text: str
    ) -> None:
        # Arrange: 2026-10-11 追加 — 英語の事故由来の漏えい語彙
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]

        # Act
        result = should_drop_victim_country("press", victim_orgs, body_text=body_text)

        # Assert
        assert result is False

    def test_flag_on_jp_victim_country_skips_keyword_gate_without_incident_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: 2026-10-11 追加 (P3 jp 層の見逃し対処) — victim_country_iso が JP の行は
        # 語の関門 (案 A) を当てない。本文に侵害・事故の語が無くても落とさない
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["大阪府こころの健康総合センター"]
        body_text = "個人情報を含む書類を別の申請者に送ってしまった。"

        # Act
        result = should_drop_victim_country(
            "press", victim_orgs, body_text=body_text, victim_country_iso="JP"
        )

        # Assert
        assert result is False

    def test_flag_on_non_jp_victim_country_still_applies_keyword_gate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: JP 以外の国には JP 例外は掛からない (通常どおり語の関門が働く)
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs = ["Acme Corp"]
        body_text = "この会社は新しいセキュリティ対策ソフトウェアを発表した。"

        # Act
        result = should_drop_victim_country(
            "press", victim_orgs, body_text=body_text, victim_country_iso="US"
        )

        # Assert
        assert result is True

    def test_flag_on_jp_victim_country_without_org_still_drops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: JP 例外は「org があるが侵害の語が無い」行にのみ働く。org 自体が
        # 無い行は JP でも従来どおり落とす (母体の org-check 分岐が先に評価される)
        monkeypatch.setenv(KEYWORD_GATE_ENV, "1")
        victim_orgs: list[str] = []
        body_text = "個人情報を含む書類を別の申請者に送ってしまった。"

        # Act
        result = should_drop_victim_country(
            "press", victim_orgs, body_text=body_text, victim_country_iso="JP"
        )

        # Assert
        assert result is True


def _article() -> Article:
    return Article(
        id="rss:abc",
        title="t",
        url="https://example.com/a",
        summary_html="",
        published=datetime(2026, 10, 10, 9, 0, tzinfo=UTC),
        feed_title="f",
        feed_url="https://example.com/",
    )


def _summary(**kw: object) -> SummaryOutput:
    base: dict[str, object] = {
        "title_ja": "見出し",
        "importance": "medium",
        "category": "advisory",
        "summary": "正常な要約です。",
    }
    return SummaryOutput(**{**base, **kw})  # type: ignore[arg-type]


class TestBriefingWiring:
    """src.pipeline.briefing._build_briefing の正規化 seam が関門を実際に通すこと。"""

    def test_press_article_without_victim_org_drops_country_metadata(self) -> None:
        # Arrange
        summary = _summary(article_type="press", victim_country="United States", victim_orgs=[])

        # Act
        msg = _build_briefing(_article(), summary)

        # Assert
        assert "victim_country_iso" not in msg.metadata
        assert "victim_country_raw" not in msg.metadata

    def test_breaking_article_without_victim_org_keeps_country_metadata(self) -> None:
        # Arrange
        summary = _summary(article_type="breaking", victim_country="United States", victim_orgs=[])

        # Act
        msg = _build_briefing(_article(), summary)

        # Assert
        assert msg.metadata.get("victim_country_iso") == "US"
