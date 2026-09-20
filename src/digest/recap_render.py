"""深掘り (weekly-recap) の構造化出力とその描画 (2026-09-20 再設計)。

⭐ **旧構成は「記事の列挙」だった**。セクション見出しの下に 1 記事 300 字の紹介を
並べる形で、実測すると本文は元記事の保存済み要約と **91% 一致 = 転記**だった
(文字 6-gram、直近の回で 80-93%)。LLM が実際に足していたのはテーマ分けと見出しだけ。

⚠ さらに「入力記事は基本的にすべて含めよ (見逃し防止が最優先)」という指示が
守られていなかった。直近 5 回のうち 3 回で **12 件選んで 3-5 件しか載っていない**。

新構成は **セクション単位の横断散文**にする:

- 本文は複数記事をまたいで書くので、元要約 1 本の転記では成立しない
- 網羅は**指示でなく構造**で守る — 選定した全記事がどこかのセクションで引用されて
  いることをコードが検証し、漏れは書き直しヒントで戻す (禁止は指示では止まらない)
- 配列に上限を宣言して Gemma 4 の文法暴走を防ぐ (detect で同じ穴を塞いだ直後)

状況総括との棲み分け: 状況総括は**台帳の事象**を固定 4 節 (重心・連鎖・波及・PIR) で
判断する。深掘りは**その週の watch 記事**を動的な主題へ束ねて解説する。入力も単位も
問いも違う。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, Field

# 暴走止め。Ollama は maxItems を文法へコンパイルするので、続けたくても閉じる。
SECTIONS_MAX = 7  # 3-5 節を狙い、余裕を 2 節分
SECTION_ARTICLES_MAX = 12  # 1 節が全件を抱え込む事故を防ぐ
BODY_MIN_CHARS = 400  # これ未満は「束ねただけ」= 解説になっていない
BODY_MAX_CHARS = 1_200

_RULE = "━" * 15

#: 出典が本文と噛み合っているかの判定に使う、タイトル中の識別語の最小長。
_CITE_TOKEN_MIN = 3
#: 識別語がこの数以上一致すれば「その節で扱っている」とみなす。
_CITE_MIN_HITS = 1


class RecapSection(BaseModel):
    """1 つの主題。本文は**その主題に属する記事を横断した解説**。"""

    emoji: str = Field(default="📌", description="主題を表す絵文字 1 つ")
    heading: str = Field(default="", description="その週の内容を反映した見出し (40 字以内)")
    body: str = Field(
        default="",
        description=(
            "この主題の解説 (600-900 字)。属する記事を横断し、"
            "何が新しいか / 個別事例が共通して示すものは何か / 防御側に何を意味するか を書く"
        ),
    )
    article_ids: list[str] = Field(
        default_factory=list,
        description="この主題で扱った記事の article_id (入力に挙がったものだけ)",
        json_schema_extra={"maxItems": SECTION_ARTICLES_MAX},
    )


class RecapOutput(BaseModel):
    model_config = {"extra": "ignore"}
    sections: list[RecapSection] = Field(
        default_factory=list, json_schema_extra={"maxItems": SECTIONS_MAX}
    )


def uncited_articles(out: RecapOutput, *, selected_ids: list[str]) -> list[str]:
    """選定したのにどの節でも引用されなかった記事 (純粋関数)。

    ⭐ 見逃し防止が最優先の機能なので、**落ちたことを検出できる形**にしておく。
    """
    cited = {i for s in out.sections for i in s.article_ids}
    return [i for i in selected_ids if i not in cited]


def thin_sections(out: RecapOutput) -> list[str]:
    """本文が短すぎる節の見出し (純粋関数)。束ねただけで解説になっていない節を拾う。"""
    return [
        s.heading or "(見出しなし)" for s in out.sections if len(s.body.strip()) < BODY_MIN_CHARS
    ]


def render_markdown(
    out: RecapOutput,
    *,
    period_label: str,
    sources: dict[str, tuple[str, str]],
    drop: Mapping[str, list[str]] | None = None,
) -> str:
    """構造化出力を Discord Markdown へ (純粋関数)。

    体裁 (罫線・見出し記法・出典行) は**コードが持つ**。旧構成は体裁まで LLM に
    書かせており、崩れても検出できなかった。

    Args:
        out: LLM の構造化出力
        period_label: 期間ラベル
        sources: article_id → (feed_title, url)
        drop: 節ごとに出典から外す article_id (``mismatched_citations`` の結果)
    """
    parts = [f"📰 Weekly Watch Recap ({period_label})", ""]
    blocks: list[str] = []
    for s in out.sections:
        if not s.heading.strip() or not s.body.strip():
            continue
        lines = [f"## {s.emoji.strip() or '📌'} {s.heading.strip()}", "", s.body.strip()]
        excluded = set((drop or {}).get(s.heading.strip() or "(見出しなし)", []))
        seen: list[str] = []
        for aid in s.article_ids:
            if aid in sources and aid not in seen and aid not in excluded:
                seen.append(aid)
        if seen:
            lines.append("")
            lines.append("出典:")
            lines.extend(f"- {sources[a][0]} → {sources[a][1]}" for a in seen)
        blocks.append("\n".join(lines))
    parts.append(f"\n\n{_RULE}\n\n".join(blocks))
    return "\n".join(parts).strip() + "\n"


def _identifying_tokens(title: str) -> list[str]:
    """タイトルから**英数字の識別子**を拾う (製品名・アクター名・CVE・数値)。

    ⚠ 片仮名は使わない。実測で「N-able、N-central の…緊急パッチをリリース」から
    「パッチ」「リリース」が拾われ、まったく別の話題の節を通した (2026-09-20)。
    片仮名は固有名 (ラザルス) と一般語 (アクセス) を分けられず、除外語を足し続ける
    ことになる。**判別できる語だけで判定し、判定できないものは通す** (fail-open) —
    出典を落とす関門なので、確信が持てないときは残す側に倒す。
    """
    return [
        t
        for t in re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}|\d{3,}", title)
        if t.lower() not in _CITE_STOPWORDS
    ]


#: どの記事にも出る語。これで一致させると全部通ってしまう。
_CITE_STOPWORDS = frozenset({"cve", "rce", "the", "and", "for", "with", "new", "api"})


def mismatched_citations(out: RecapOutput, *, titles: Mapping[str, str]) -> dict[str, list[str]]:
    """本文で扱っていない記事を引用している節 (純粋関数) → {見出し: [article_id]}。

    ⭐ **出典が別記事を指すのは信頼を損なう**。dry-run 実測で 16 件中 3 件が、
    その節で一言も触れていない記事を指していた (節 1 の「Microsoft 974 件」、
    節 3・4 の「N-able」)。08-22 の引用実在関門と同じ思想で、書き出す前に落とす。

    判定は**タイトル中の識別語** (製品名・アクター名・数値) が本文に出るか。
    識別語が拾えないタイトルは判定できないので**通す** (fail-open) — ここで
    落とすと、日本語だけのタイトルが一律に消える。
    """
    bad: dict[str, list[str]] = {}
    for sec in out.sections:
        body = sec.body
        for aid in sec.article_ids:
            probe = _identifying_tokens(titles.get(aid, ""))
            if not probe:
                continue  # 判定材料が無い → 通す (fail-open)
            if sum(1 for t in probe if t in body) < _CITE_MIN_HITS:
                bad.setdefault(sec.heading or "(見出しなし)", []).append(aid)
    return bad
