"""逐語一致の関門 (src/eventnews/verbatim.py)。

事実に著作権は無いが表現にはある。要約を名乗る以上、原文の文をなぞった行は
出せない。実測 (2026-08-26) では原文が日本語の記事だけが 38-62% に達し、
翻訳を挟む記事は最大 9.8% だった。
"""

from __future__ import annotations

from src.eventnews import verbatim
from src.eventnews.models import FactItem

_SOURCE = (
    "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
    "一時的に侵入者から閲覧可能な状態にあったことが判明しました。"
    "対象は氏名、住所、電話番号を含むデータ七万件です。"
)


def _fact(text: str, source_index: int = 1) -> FactItem:
    return FactItem(text=text, source_index=source_index, paragraph=1, section="what")


def test_transcription_with_changed_ending_is_detected() -> None:
    # Arrange — 実際に本番で出た形 (語尾だけ変えた転記)
    fact = _fact(
        "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
        "一時的に侵入者から閲覧可能な状態にあったことが判明したと報じられている。"
    )

    # Act / Assert
    assert verbatim.needs_rewrite([fact], {1: _SOURCE}) is True
    assert verbatim.transcribed_lines([fact], {1: _SOURCE}) == (0,)


def test_paraphrase_passes() -> None:
    # Arrange — 同じ事実を自分の言葉で書き直した場合
    fact = _fact(
        "同社によれば、侵入を受けたサーバー上で取引先や利用者の登録情報が"
        "第三者の目に触れうる状況が一定期間続いていたという。"
    )

    # Act / Assert
    assert verbatim.needs_rewrite([fact], {1: _SOURCE}) is False


def test_short_lines_are_not_judged_per_line() -> None:
    # Arrange — 固有名詞や数値の並びは短くても一致する。行単位では見ない
    fact = _fact("対象は氏名、住所、電話番号を含むデータ七万件です。")

    # Act / Assert
    assert verbatim.transcribed_lines([fact], {1: _SOURCE}) == ()


def test_one_transcribed_line_is_caught_even_when_the_article_is_mostly_original() -> None:
    # Arrange — 全体比では埋もれる 1 行を隠さない
    original = [
        _fact("この事案は同業他社にも同種の設定不備がある可能性を示している。" * 3)
        for _ in range(6)
    ]
    copied = _fact(
        "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
        "一時的に侵入者から閲覧可能な状態にあったことが判明しました。"
    )
    facts = [*original, copied]

    # Act
    ratio = verbatim.article_ratio(facts, {1: _SOURCE})

    # Assert — 全体比は閾値未満だが、行単位で捕まる
    assert ratio <= verbatim.ARTICLE_MAX_RATIO
    assert verbatim.needs_rewrite(facts, {1: _SOURCE}) is True


def test_missing_body_is_not_treated_as_match() -> None:
    # Arrange — 原文が取れない出典番号 (検証不能を「一致」と読まない)
    fact = _fact("何らかの記述", source_index=9)

    # Act / Assert
    assert verbatim.needs_rewrite([fact], {1: _SOURCE}) is False


def test_character_width_differences_do_not_hide_a_match() -> None:
    # Arrange — 全角/半角や引用符の差で関門が素通りしないこと
    fact = _fact(
        "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
        "一時的に侵入者から閲覧可能な状態にあったことが判明しました。".replace("、", "，")
    )

    # Act / Assert
    assert verbatim.needs_rewrite([fact], {1: _SOURCE}) is True


def test_empty_input_passes() -> None:
    assert verbatim.needs_rewrite([], {1: _SOURCE}) is False
    assert verbatim.article_ratio([], {}) == 0.0


def test_block_only_on_transcribed_lines_not_on_fact_density() -> None:
    # Arrange — 数値の羅列は句読点を落とすと長い「一致」になるが、これは事実であって
    # 表現ではない (2026-08-26 実測: 書き直し後に残る一致はすべてこの形だった)
    dense = _fact(
        "対象は氏名、住所、電話番号を含むデータ七万件です。これは事案の規模を示す数字である。"
    )

    # Act / Assert — 書き直しは促すが、公開は止めない
    assert verbatim.needs_rewrite([dense], {1: _SOURCE}) is True
    assert verbatim.must_block([dense], {1: _SOURCE}) is False


def test_block_when_a_whole_sentence_is_copied() -> None:
    # Arrange
    copied = _fact(
        "調査の結果、不正アクセスを受けたサーバーに保存されていた顧客の個人情報が、"
        "一時的に侵入者から閲覧可能な状態にあったことが判明しました。"
    )

    # Act / Assert
    assert verbatim.must_block([copied], {1: _SOURCE}) is True
