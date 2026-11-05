"""標本から評価の集合を外す: guard_n20_v2 (id・見出し・URL の完全一致) + leak_roots_v2 (事象の根)。

guard_match.py (D4、10-28) と同じく完全一致で当てる (部分一致は定型の見出しで過剰に落ちる)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Guard:
    ids: frozenset[str]
    titles: frozenset[str]
    urls: frozenset[str]
    eval_events: frozenset[str]
    roots_by_article: dict[str, frozenset[str]]

    @classmethod
    def load(cls, guard_path: Path, leak_path: Path) -> Guard:
        g = json.loads(guard_path.read_text(encoding="utf-8"))
        lk = json.loads(leak_path.read_text(encoding="utf-8"))
        return cls(
            ids=frozenset(str(x) for x in g["ids"]),
            titles=frozenset(str(x).strip() for x in g["titles"]),
            urls=frozenset(str(x) for x in g.get("urls", [])),
            eval_events=frozenset(lk["eval_events"]),
            roots_by_article={k: frozenset(v) for k, v in lk["roots_by_article"].items()},
        )

    def reason(
        self,
        *,
        article_ids: Iterable[str] = (),
        titles: Iterable[str] = (),
        urls: Iterable[str] = (),
        roots: Iterable[str] = (),
    ) -> str | None:
        """当たった理由 (``id:…`` / ``title:…`` / ``url:…`` / ``root:…``)。当たらなければ None。"""
        aids = list(article_ids)
        for a in aids:
            if a in self.ids:
                return f"id:{a}"
        for t in titles:
            if t and t.strip() in self.titles:
                return f"title:{t.strip()[:40]}"
        for u in urls:
            if u and u in self.urls:
                return f"url:{u[:60]}"
        for r in roots:
            if r in self.eval_events:
                return f"root:{r}"
        for a in aids:
            hit = self.roots_by_article.get(a, frozenset()) & self.eval_events
            if hit:
                return f"root:{sorted(hit)[0]}"
        return None
