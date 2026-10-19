"""まとめ記事 (複数の独立した事案を束ねた記事) の確定的な判定 (2026-10-18, U2)。

背景: 保存の article_type は要約 LLM の出力そのもので、'recap' の適合率が約 5 割
(単一事案の続報・年次レポートに多く付く)。一方 detect_article_type の正規表現は
「まとめ」単独・Top N で単一事案を拾い (「…についてまとめてみた」)、週刊/週報/週次を拾わない。
ここでは **見出しの定期刊行の語** と **LLM の recap 判定 + 単一事案の手がかり** で判定する。

- 見出しに定期刊行の語 (週刊/週報/Weekly/Newsletter/Digest/Issue # 等) → まとめ記事
- 単一事案の手がかり (まとめてみた / 第N報 / 続報 / 最終報 / 調査結果 等) → 見出しの語が
  無い限りまとめ記事にしない
- LLM が recap と判定し、単一事案の手がかりも侵害カテゴリでもない → まとめ記事

評価は data/eval/U2_roundup_fix_report.md。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from src.tools.discord_publisher import Importance

#: 見出しの定期刊行・束ねを示す語。単独で「複数の独立した話題の束」と読める。
_PERIODICAL_TITLE = re.compile(
    r"(週刊|週報|週次|月報(?!告)|日次まとめ|(?<!ゴルフ)ダイジェスト|ニュースレター|先週注目|今週の|今月の|週間.*まとめ|"
    r"回顧|"
    r"\b(weekly|monthly|roundup|round-up|recap|wrap[- ]?up|newsletter|(?<!golf\s)digest|"
    r"week\s+in\s+review|this\s+week|bulletins?|threatsday)\b|"
    r"\bissue\s*#?\s*\d+|第\s*\d+\s*回)",
    re.IGNORECASE,
)

#: 単一事案の経緯・続報を示す手がかり (「まとめ」の語に引きずられない)。
_SINGLE_INCIDENT_CUE = re.compile(
    r"(まとめてみた|第\s*[0-9一二三四五六七八九十]+\s*報|続報|最終報|調査結果|調査完了|"
    r"お詫び|について.{0,10}まとめ)",
)

#: LLM の recap 判定を単独では信用しないカテゴリ (単一の侵害事案の続報・単一の制度解説に多く付く)。
_LLM_UNTRUSTED_CATEGORIES = frozenset({"breach", "policy"})


BASIS_TITLE = "title"
BASIS_LLM = "llm"


def roundup_basis(
    title: str,
    *,
    llm_article_type: str | None = None,
    category: str | None = None,
) -> str | None:
    """まとめ記事と判定した根拠を返す (判定しなければ None)。

    - ``"title"``: 見出しの定期刊行の語 (200 件の正解で適合率 96%)
    - ``"llm"``: LLM の recap 判定 + 単一事案の手がかり・侵害/政策カテゴリでない
      (適合率は 4〜5 割。単一事案の年次レポート・続報を拾う)
    """
    text = (title or "").strip()
    if _PERIODICAL_TITLE.search(text):
        return BASIS_TITLE
    if _SINGLE_INCIDENT_CUE.search(text):
        return None
    if (llm_article_type or "").strip().lower() != "recap":
        return None
    if (category or "") in _LLM_UNTRUSTED_CATEGORIES:
        return None
    return BASIS_LLM


def is_roundup(
    title: str,
    *,
    llm_article_type: str | None = None,
    category: str | None = None,
) -> bool:
    """見出しと LLM の article_type・category からまとめ記事かを判定する。"""
    return roundup_basis(title, llm_article_type=llm_article_type, category=category) is not None


# ---------- 重要度の上限 (まとめ記事は記事全体として high にしない) ----------

CAP_ENV = "ROUNDUP_IMPORTANCE_CAP"
CAP_MODE_OFF = "off"
CAP_MODE_SHADOW = "shadow"
CAP_MODE_TITLE = "title"
CAP_MODE_ON = "on"


def cap_mode() -> str:
    """``ROUNDUP_IMPORTANCE_CAP`` を解釈する (既定 shadow = ログのみ)。

    - ``off`` / ``0``: 何もしない
    - ``shadow``: 落とす件数と記事をログに残すだけ
    - ``title``: 見出しの定期刊行の語で判定した記事だけ上限 medium (適合率 96%)
    - ``on`` / ``1``: LLM recap 層も含めて上限 medium (適合率 4〜5 割。単一事案も落とす)
    """
    raw = os.environ.get(CAP_ENV, CAP_MODE_SHADOW).strip().lower()
    if raw in ("0", CAP_MODE_OFF):
        return CAP_MODE_OFF
    if raw in ("1", CAP_MODE_ON):
        return CAP_MODE_ON
    if raw == CAP_MODE_TITLE:
        return CAP_MODE_TITLE
    return CAP_MODE_SHADOW


@dataclass(frozen=True)
class RoundupCapResult:
    importance: Importance
    basis: str | None  # 判定の根拠 (まとめ記事でなければ None)
    would_cap: bool  # high → medium に該当したか (shadow でも True)
    applied: bool  # 実際に importance を変えたか


def cap_roundup_importance(
    importance: Importance,
    title: str,
    *,
    llm_article_type: str | None = None,
    category: str | None = None,
    mode: str | None = None,
) -> RoundupCapResult:
    """まとめ記事の high を medium に下げる (要約の後・保存の前)。入力は変えない。"""
    effective = cap_mode() if mode is None else mode
    if effective == CAP_MODE_OFF or importance != "high":
        return RoundupCapResult(importance, None, False, False)
    basis = roundup_basis(title, llm_article_type=llm_article_type, category=category)
    if basis is None:
        return RoundupCapResult(importance, None, False, False)
    applicable = effective == CAP_MODE_ON or (effective == CAP_MODE_TITLE and basis == BASIS_TITLE)
    return RoundupCapResult("medium" if applicable else importance, basis, True, applicable)
