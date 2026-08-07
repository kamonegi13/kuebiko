"""派生タグの backfill (malware_type / affected_vendor / affected_product、決定論・LLM 不使用)。

- malware_type: 既存 malware_family entity → malware_aliases.yaml type 列の辞書導出 (P2)。
- affected_vendor / affected_product: 既存 cve entity → NVD CPE cache 逆引き (P1)。
  cache 未収録 CVE は skip (nvd warming が進んだ後に再実行すれば追補される = 冪等)。

使い方 (本番はコンテナ内):
    docker exec kuebiko python scripts/backfill_derived_tags.py --dry-run
    docker exec kuebiko python scripts/backfill_derived_tags.py --apply
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from src.cti.malware_normalizer import load_malware_normalizer
from src.storage.run_history import RunHistoryRepository
from src.tools.nvd_client import get_affected

_MAX_AFFECTED = 6  # main.py _MAX_AFFECTED_PER_ARTICLE と同値 (1 記事あたり上限)


def _entities_by_article(repo: RunHistoryRepository, entity_type: str) -> dict[str, list[str]]:
    with repo._connect() as conn:  # noqa: SLF001 — 接続 seam の意図的共有
        rows = conn.execute(
            "SELECT article_id, value FROM article_entities WHERE entity_type=?",
            (entity_type,),
        ).fetchall()
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        out[str(r["article_id"])].append(str(r["value"]))
    return out


def run(*, apply: bool) -> None:
    repo = RunHistoryRepository()
    mnorm = load_malware_normalizer()

    # P2: malware_family → malware_type
    fam_by_article = _entities_by_article(repo, "malware_family")
    mt_articles = mt_tags = 0
    for aid, fams in fam_by_article.items():
        types = sorted({t for f in fams if (t := mnorm.type_of(f))})
        if not types:
            continue
        mt_articles += 1
        mt_tags += len(types)
        if apply:
            repo.add_article_entities(aid, [("malware_type", t) for t in types])

    # P1: cve → affected_vendor / affected_product (NVD CPE cache)
    cve_by_article = _entities_by_article(repo, "cve")
    av_articles = av_tags = cache_miss = 0
    for aid, cves in cve_by_article.items():
        vendors: list[str] = []
        products: list[str] = []
        for c in sorted(set(cves)):
            vs, ps = get_affected(c)
            if not vs and not ps:
                cache_miss += 1
            vendors.extend(vs)
            products.extend(ps)
        ents: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for t, vals in (("affected_vendor", vendors), ("affected_product", products)):
            for v in vals:
                k = (t, v.lower())
                if v and k not in seen and sum(1 for e in ents if e[0] == t) < _MAX_AFFECTED:
                    seen.add(k)
                    ents.append((t, v))
        if not ents:
            continue
        av_articles += 1
        av_tags += len(ents)
        if apply:
            repo.add_article_entities(aid, ents)

    mode = "APPLIED" if apply else "DRY-RUN"
    print(
        f"{mode}: malware_type articles={mt_articles} tags={mt_tags} / "
        f"affected articles={av_articles} tags={av_tags} "
        f"(cve cache-miss={cache_miss} — nvd warming 後の再実行で追補)"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="malware_type / affected_* の backfill")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run(apply=bool(args.apply and not args.dry_run))


if __name__ == "__main__":
    main()
