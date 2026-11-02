"""深掘りの本文を「節ごとの要点の構造 → 文はコードが組む」形で作る。

n20 T5、旗 ``DEEP_DIVE_SLOT_RENDER`` (既定 off)。

契約 (data/eval/n20_contract_2026_11_02.md §1): RecapOutput の本文 (自由な散文) を
``{heading, points[{text, article_ids}]}`` に。要点ごとに根拠の記事 id を持たせるので、
「どの文がどの記事に拠るか」を検査できる (旧は節単位でしか分からない)。
本文はコードが要点を順に繋いで組み、以降 (網羅・出典の照合・Markdown の体裁) は
``recap_render`` の既存の関門をそのまま通す (``to_recap`` で旧の形へ写す)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.digest.recap_render import SECTION_ARTICLES_MAX, SECTIONS_MAX, RecapOutput, RecapSection
from src.tools.llm_schema import require_all_properties

#: 旗が効くのは深掘り (weekly_recap) だけ — 他のテンプレートが来たら旧の経路のまま
TEMPLATE_V2 = "digest/weekly_recap.j2"
TEMPLATE_V3 = "digest/weekly_recap_v3.j2"
POINTS_MAX = 8
HEADING_MAX_CHARS = 40  # 学習の関門 (DEEP_DIVE_HEADING_MAX) と同じ


class _AllRequired(BaseModel):
    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


class RecapPoint(_AllRequired):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    article_ids: list[str] = Field(
        default_factory=list, json_schema_extra={"maxItems": SECTION_ARTICLES_MAX}
    )


class RecapPointSection(_AllRequired):
    model_config = ConfigDict(extra="ignore")
    heading: str = ""
    points: list[RecapPoint] = Field(
        default_factory=list, json_schema_extra={"maxItems": POINTS_MAX}
    )


class RecapPointsOutput(_AllRequired):
    """weekly_recap v3 の LLM 出力 (n20 の学習の形と同じ)。"""

    model_config = ConfigDict(extra="ignore")
    sections: list[RecapPointSection] = Field(
        default_factory=list, json_schema_extra={"maxItems": SECTIONS_MAX}
    )


def _sentence(text: str) -> str:
    t = text.strip()
    if not t:
        return ""
    return t if t.endswith(("。", "！", "？", ".")) else t + "。"


def to_recap(out: RecapPointsOutput, *, known_ids: frozenset[str]) -> RecapOutput:
    """要点を繋いで旧の形 (節の本文 + 扱った記事 id) へ写す (純粋関数)。

    入力に無い記事 id は落とす (作った id を出典にしない)。根拠の id が 1 つも残らない要点は
    本文に入れない (要点ごとに根拠を持つのがこの形の意味)。見出しは 40 字で切る。
    """
    sections: list[RecapSection] = []
    for sec in out.sections:
        texts: list[str] = []
        ids: list[str] = []
        for p in sec.points:
            valid = [a for a in p.article_ids if a in known_ids]
            if not valid or not p.text.strip():
                continue
            texts.append(_sentence(p.text))
            ids.extend(a for a in valid if a not in ids)
        if texts:
            sections.append(
                RecapSection(
                    heading=sec.heading.strip()[:HEADING_MAX_CHARS],
                    body="".join(texts),
                    article_ids=ids[:SECTION_ARTICLES_MAX],
                )
            )
    return RecapOutput(sections=sections)


def dropped_points(out: RecapPointsOutput, *, known_ids: frozenset[str]) -> int:
    """根拠の記事 id が入力に 1 つも無いため本文に入れなかった要点の数 (log 用)。"""
    return sum(
        1
        for sec in out.sections
        for p in sec.points
        if not p.text.strip() or not any(a in known_ids for a in p.article_ids)
    )
