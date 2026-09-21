"""深掘りの構造化出力と描画 (2026-09-20 再設計)。

旧構成は「セクション見出し + 記事 1 件 300 字」の列挙で、本文は元要約と 91% 一致 =
転記だった。さらに 12 件選んで 3-5 件しか載らない回が直近 5 回中 3 回あった。
新構成はセクション単位の横断散文にし、**網羅を指示でなく構造で守る**。
"""

from __future__ import annotations

from src.digest.recap_render import (
    BODY_MIN_CHARS,
    SECTIONS_MAX,
    RecapOutput,
    RecapSection,
    mismatched_citations,
    render_fallback,
    render_markdown,
    thin_sections,
    uncited_articles,
)

_SRC = {
    "a1": ("The Hacker News", "https://kuebiko.example/1"),
    "a2": ("Rapid7", "https://kuebiko.example/2"),
    "a3": ("JPCERT/CC", "https://kuebiko.example/3"),
}


def _section(heading: str, ids: list[str], *, body_len: int = 600) -> RecapSection:
    return RecapSection(emoji="🔬", heading=heading, body="あ" * body_len, article_ids=ids)


def test_uncited_names_every_selected_article_no_section_touched() -> None:
    out = RecapOutput(sections=[_section("AI 悪用", ["a1"])])

    assert uncited_articles(out, selected_ids=["a1", "a2", "a3"]) == ["a2", "a3"]
    assert uncited_articles(out, selected_ids=["a1"]) == []


def test_thin_sections_flags_bodies_that_only_bundle() -> None:
    out = RecapOutput(
        sections=[_section("厚い", ["a1"]), _section("薄い", ["a2"], body_len=BODY_MIN_CHARS - 1)]
    )

    assert thin_sections(out) == ["薄い"]


def test_render_puts_structure_in_code_not_in_the_model() -> None:
    out = RecapOutput(
        sections=[_section("第一", ["a1", "a2"]), _section("第二", ["a3"])],
    )

    md = render_markdown(out, period_label="2026-09-14 - 2026-09-21", sources=_SRC)

    assert md.startswith("📰 Weekly Watch Recap (2026-09-14 - 2026-09-21)")
    assert "## 🔬 第一" in md and "## 🔬 第二" in md
    assert md.count("━" * 15) == 1  # 節間だけ。末尾に罫線を残さない
    assert "- The Hacker News → https://kuebiko.example/1" in md
    assert "- Rapid7 → https://kuebiko.example/2" in md


def test_render_drops_unknown_ids_and_duplicates_in_sources() -> None:
    out = RecapOutput(sections=[_section("節", ["a1", "a1", "存在しない"])])

    md = render_markdown(out, period_label="P", sources=_SRC)

    assert md.count("https://kuebiko.example/1") == 1  # 重複は 1 度だけ
    assert "存在しない" not in md  # 入力に無い id は出典に出さない


def test_render_skips_sections_with_no_heading_or_no_body() -> None:
    out = RecapOutput(
        sections=[
            RecapSection(emoji="🔬", heading="", body="あ" * 600, article_ids=["a1"]),
            RecapSection(emoji="🔬", heading="見出しだけ", body="  ", article_ids=["a2"]),
            _section("正常", ["a3"]),
        ]
    )

    md = render_markdown(out, period_label="P", sources=_SRC)

    assert "## 🔬 正常" in md
    assert "見出しだけ" not in md
    assert md.count("━" * 15) == 0  # 有効な節が 1 つなら罫線は出ない


def test_schema_caps_arrays_so_the_grammar_can_close() -> None:
    """detect と同じ Gemma 4 の暴走対策 (ollama#15502)。"""
    schema = RecapOutput.model_json_schema()

    assert schema["properties"]["sections"]["maxItems"] == SECTIONS_MAX
    inner = schema["$defs"]["RecapSection"]["properties"]["article_ids"]
    assert inner["maxItems"] > 0


class TestCitationGate:
    """出典が別記事を指すのは信頼を損なう (2026-09-20、dry-run で 16 件中 3 件)。"""

    @staticmethod
    def _out(body: str, ids: list[str]) -> RecapOutput:
        return RecapOutput(
            sections=[RecapSection(emoji="🔬", heading="節", body=body, article_ids=ids)]
        )

    def test_drops_a_citation_whose_product_is_never_mentioned(self) -> None:
        out = self._out("LiteLLM の認証バイパスが確認された。" + "あ" * 500, ["a1", "a2"])
        titles = {
            "a1": "LiteLLM の認証バイパスからクラウド侵害に至る脆弱性を公開",
            "a2": "N-able、N-central の重大なゼロデイ RCE 脆弱性を修正",
        }

        assert mismatched_citations(out, titles=titles) == {"節": ["a2"]}

    def test_generic_katakana_does_not_count_as_a_match(self) -> None:
        """「パッチ」「リリース」で通してしまい、無関係な節が生き残った実例。"""
        out = self._out("大規模パッチがリリースされた。" + "あ" * 500, ["a1"])
        titles = {"a1": "N-able、N-central の重大な RCE 脆弱性を修正し緊急パッチをリリース"}

        assert mismatched_citations(out, titles=titles) == {"節": ["a1"]}

    def test_title_without_identifiers_is_let_through(self) -> None:
        """判定材料が無いものを落とすと、日本語だけのタイトルが一律に消える。"""
        out = self._out("あ" * 600, ["a1"])

        # 英数字の識別子が無いタイトルは判定材料が無い → 落とさない
        assert mismatched_citations(out, titles={"a1": "国内企業で不正アクセスが発生"}) == {}

    def test_render_omits_the_dropped_citation(self) -> None:
        out = self._out("あ" * 600, ["a1", "a2"])

        md = render_markdown(out, period_label="P", sources=_SRC, drop={"節": ["a2"]})

        assert "https://kuebiko.example/1" in md
        assert "https://kuebiko.example/2" not in md


class TestFallback:
    """空を投稿するくらいなら一覧を出す (2026-09-21 の本番が見出し 1 行だけになった)。"""

    def test_lists_every_selected_article_with_its_source(self) -> None:
        md = render_fallback(
            period_label="P",
            items=[
                ("題 1", "要約 1", "Feed A", "https://kuebiko.example/1"),
                ("題 2", "", "Feed B", "https://kuebiko.example/2"),
            ],
        )

        assert "題 1" in md and "題 2" in md
        assert "要約 1" in md
        assert "- 出典: Feed B → https://kuebiko.example/2" in md
        assert "生成できませんでした" in md  # 縮退したことを読者に伝える
