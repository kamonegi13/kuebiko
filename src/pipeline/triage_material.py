"""triage の材料から確認・拒否の画面の文を外す (2026-11-20、11-19 監査 D2 P1-8a)。

本文の先行取得 (``filters._prefetch_thin_bodies``) は取得に成功すれば画面の文でも
``body_text`` に入れ、triage はそれを優先して読む。feed の抜粋自体が画面の文の媒体もある。
LLM は「概要はボット検証画面の定型文のみ」と書いて low にしていた (14 日で FDD 14 件など)。

旗 ``TRIAGE_BLOCKED_MATERIAL=0|shadow|1`` (コードの既定 0、compose の既定 shadow)。
- shadow: 判定は変えず、ログ ``triage_material_blocked`` (記事 id・どの材料か) だけ。
- 1: 画面の文を外した写しで triage する (材料が無くなれば題だけの判定)。落選の理由に
  ``NO_MATERIAL_PREFIX`` を付けて、取得の失敗が媒体の詳細で見えるようにする。先行取得は
  画面の文を ``body_text`` に入れない (本処理で取り直す)。
プロンプトは変えない (凍結の sha を保つ)。
"""

from __future__ import annotations

import os
from typing import Literal

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.block_page import is_block_page
from src.tools.text_utils import strip_html

_log = get_logger(__name__)

FLAG = "TRIAGE_BLOCKED_MATERIAL"
#: 落選の理由の接頭辞 (列は足さない。媒体の詳細の「材料なし」の件数はこれで数える)
NO_MATERIAL_PREFIX = "[材料なし] "

Mode = Literal["off", "shadow", "on"]


def blocked_material_mode() -> Mode:
    raw = os.environ.get(FLAG, "0").strip().lower()
    if raw == "shadow":
        return "shadow"
    if raw in {"1", "on", "true"}:
        return "on"
    return "off"


def blocked_fields(article: Article) -> tuple[str, ...]:
    """画面の文になっている材料の名前 (``body_text`` / ``summary_html``)。"""
    found: list[str] = []
    if article.body_text and is_block_page(article.body_text):
        found.append("body_text")
    if article.summary_html and is_block_page(strip_html(article.summary_html)):
        found.append("summary_html")
    return tuple(found)


def material_for_triage(article: Article, mode: Mode) -> tuple[Article, bool]:
    """(triage に渡す記事, 材料が無くなったか)。off・該当なしは元の記事をそのまま返す。"""
    if mode == "off":
        return article, False
    fields = blocked_fields(article)
    if not fields:
        return article, False
    _log.info("triage_material_blocked", article_id=article.id, fields=list(fields), mode=mode)
    if mode != "on":
        return article, False
    update: dict[str, object] = {}
    if "body_text" in fields:
        update["body_text"] = None
    if "summary_html" in fields:
        update["summary_html"] = ""
    stripped = article.model_copy(update=update)
    no_material = not (stripped.body_text or strip_html(stripped.summary_html or "").strip())
    return stripped, no_material


def mark_no_material(reason: str) -> str:
    return reason if reason.startswith(NO_MATERIAL_PREFIX) else NO_MATERIAL_PREFIX + reason


__all__ = [
    "FLAG",
    "NO_MATERIAL_PREFIX",
    "blocked_fields",
    "blocked_material_mode",
    "mark_no_material",
    "material_for_triage",
]
