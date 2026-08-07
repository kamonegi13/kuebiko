"""記事書込 seam の構造的不変条件 (R1、2026-07-26)。

「分析 (主題判定・entity 付与) は全取込経路が通る単一の判定点で行う」を **慣習でなく
テストで強制**する。ransomware.live 追加 (2026-06-19) 時にこの不変条件が silent に破れ、
5 週間 subject 無音欠落したのが動機 — 新しい source が add_article を直接呼んで分析を
迂回した瞬間に CI が落ちるようにする。

add_article の呼出は 2 経路のみ許可する:
- src/pipeline/persistence.py : RSS / grok の主判定点 (determine_subject_actors 経由)
- src/sources/ransomware_ingest.py : source 断言経路 (subject_actor_source='feed' 直書き)

新しい writer を足すなら、この allowlist を更新した上で **主題判定を必ず経由させる**
(determine_subject_actors か source 断言のいずれか) こと。
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"

# add_article を呼んでよいファイル (repo-relative)。増やす前に docstring を読むこと。
_ALLOWED_WRITERS = {
    "pipeline/persistence.py",
    "sources/ransomware_ingest.py",
}

# 呼出検出: `.add_article(` だが `.add_article_entities(` は除外する
_CALL_RE = re.compile(r"\.add_article\((?!_)")


def _writers_in_src() -> set[str]:
    out: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _CALL_RE.search(text):
            out.add(path.relative_to(_SRC).as_posix())
    return out


def test_only_sanctioned_modules_write_articles() -> None:
    """add_article の呼出点が allowlist と完全一致する (迂回 writer の混入を検知)。"""
    found = _writers_in_src()
    unexpected = found - _ALLOWED_WRITERS
    assert not unexpected, (
        "add_article を直接呼ぶ新しい経路が検出されました。分析 (主題判定) を迂回して"
        f"いないか確認し、確認後 _ALLOWED_WRITERS を更新してください: {sorted(unexpected)}"
    )


def test_sanctioned_writers_route_through_subject_determination() -> None:
    """許可された各 writer が主題判定を経由している (determine_subject_actors か source 断言)。"""
    for rel in _ALLOWED_WRITERS:
        text = (_SRC / rel).read_text(encoding="utf-8")
        routes = (
            "determine_subject_actors" in text
            or "subject_actor_source" in text
            or "SOURCE_FEED" in text
        )
        assert routes, f"{rel} が主題判定を経由していません (分析迂回の疑い)"
