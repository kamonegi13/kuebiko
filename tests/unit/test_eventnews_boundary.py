"""事象ニュース層の import 境界関門 (allowlist / default-deny)。

設計 SSoT: docs/event_news_design.md §2。生成物のパイプライン還流
(triage / routing / synthesis 証拠 / 検索 / digest / spotlight / forecast) を
denylist の数え漏れごと防ぐため、src/ 内で src/eventnews を import してよい場所を
allowlist で固定する。新しい消費者 (UI API 等) を作るときはここに 1 行足すこと —
それが「公開面に露出する変更である」ことを明示する儀式になる。
"""

from __future__ import annotations

import re
from pathlib import Path

# src/ 内で eventnews を import してよいモジュール (repo-relative)。
# v1 の許可: storage の Mixin (ItemState 型と VERSION_CAP を読む infra — 生成物の
# 消費者ではない)。scripts/ は src 外なので対象外。
_ALLOWLIST: frozenset[str] = frozenset({"src/storage/repo_eventnews.py"})

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+src\.eventnews\b", re.MULTILINE)


class TestEventNewsImportBoundary:
    def test_only_allowlisted_modules_import_eventnews(self) -> None:
        src_root = Path(__file__).resolve().parents[2] / "src"
        offenders: list[str] = []
        for path in src_root.rglob("*.py"):
            rel = path.relative_to(src_root.parent).as_posix()
            if rel.startswith("src/eventnews/"):
                continue
            if _IMPORT_RE.search(path.read_text(encoding="utf-8")) and rel not in _ALLOWLIST:
                offenders.append(rel)
        assert offenders == [], (
            f"eventnews を import する未許可モジュール: {offenders} — "
            "消費者を増やすなら test_eventnews_boundary._ALLOWLIST に明示追加すること"
        )

    def test_eventnews_does_not_import_judgment_layers(self) -> None:
        """逆向きも固定: eventnews が triage/routing/synthesis を読まない (§2 一方向)。"""
        pkg = Path(__file__).resolve().parents[2] / "src" / "eventnews"
        banned = re.compile(
            r"^\s*(?:from|import)\s+src\.(?:tools\.article_triage|synthesis|pir\.|forecast)",
            re.MULTILINE,
        )
        offenders = [
            p.name for p in pkg.rglob("*.py") if banned.search(p.read_text(encoding="utf-8"))
        ]
        assert offenders == [], f"eventnews から判断層への依存を検出: {offenders}"
