#!/usr/bin/env python3
"""Phase Diamond L2-4: articles.body から IoC/CVE/TTP を抽出して article_entities へ。

設計方針:
- 既存 articles.body (backfill_article_bodies で蓄積) を全件スキャン
- ioc_extractor で IoC / CVE / mitre_techniques を抽出
- malware / tool dictionary を title+summary+body に grep (Phase Diamond L4)
- article_entities へ INSERT OR IGNORE (冪等)
- 進捗 / 統計ログ

Usage:
    docker exec kuebiko /app/.venv/bin/python3 -m scripts.backfill_article_entities \\
        [--since-days 90] [--batch-size 200]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.actor_normalizer import load_actor_aliases  # noqa: E402
from src.cti.ioc_extractor import extract_iocs  # noqa: E402
from src.logging_config import get_logger  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

_log = get_logger(__name__)

# malware / tool / actor の代表名 (dictionary lookup)。
# yaml 化前の暫定実装: ここに直接 list で持つ (200 件目安)。後で config に外出し可能。
KNOWN_MALWARE_FAMILIES: tuple[str, ...] = (
    # Ransomware
    "LockBit",
    "BlackCat",
    "ALPHV",
    "Conti",
    "Akira",
    "RansomHub",
    "Cl0p",
    "Clop",
    "Royal",
    "BlackSuit",
    "Play",
    "Rhysida",
    "8Base",
    "Medusa",
    "Cactus",
    "INC",
    "Trinity",
    "BlackByte",
    "Hunters",
    "Qilin",
    "Avos",
    "BianLian",
    "Snatch",
    "Hive",
    "ALPHV",
    "Ryuk",
    "DarkSide",
    "REvil",
    "Maze",
    "Egregor",
    "DoppelPaymer",
    # APT-grade
    "TeamPCP",
    "Megalodon",
    "PlugX",
    "ShadowPad",
    "Snake",
    "Turla",
    "Dridex",
    "Emotet",
    "Qakbot",
    "TrickBot",
    "IcedID",
    "Bumblebee",
    "PikaBot",
    "DanaBot",
    "GootLoader",
    "Latrodectus",
    "AsyncRAT",
    "njRAT",
    "DarkComet",
    "Remcos",
    "AgentTesla",
    "Formbook",
    "Lokibot",
    "NanoCore",
    "RedLine",
    "Vidar",
    "RaccoonStealer",
    "Raccoon",
    "LummaC2",
    "Lumma",
    "StealC",
    "MetaStealer",
    "RisePro",
    "Atomic",
    "ChromeLoader",
    "SocGholish",
    # State-sponsored
    "WinDealer",
    "DustyCave",
    "DarkPink",
    "InvisibleFerret",
    "OtterCookie",
    "BeaverTail",
    "FlyingYeti",
    "GhostEmperor",
    "SugarGh0st",
    "Gh0st",
    "PoisonIvy",
    "Cobalt Strike",
    "Brute Ratel",
    "Sliver",
    "Mythic",
    "Havoc",
    "Manjusaka",
    "Empire",
    "Metasploit",
)

KNOWN_TOOLS: tuple[str, ...] = (
    # 検証で false positive 確認済の generic 名 (4 文字以上の word boundary でも
    # 兵器名 / 一般語と衝突する) を除外。CTI 文脈で確実に "tool" として使われるもののみ。
    # 攻撃側 / red-team / dual-use offensive tool
    "Mimikatz",
    "PsExec",
    "BloodHound",
    "SharpHound",
    "ROADtools",
    "Rubeus",
    "Impacket",
    "Responder",
    "CrackMapExec",
    "NetExec",
    "Certipy",
    "PowerView",
    "PowerSploit",
    "Nishang",
    "PowerUpSQL",
    "ADRecon",
    "PingCastle",
    "GMER",
    "ProcDump",
    "AdFind",
    "fscan",
    "Chisel",
    "Ngrok",
    # RMM (Remote Monitoring & Management) tools — ransomware affiliate が好んで悪用
    "AnyDesk",
    "TeamViewer",
    "ScreenConnect",
    "ConnectWise",
    "Atera",
    "Splashtop",
    "RustDesk",
    "Action1",
    "NetSupport",
    # File transfer / utility (ransomware exfiltration で観測)
    "WinSCP",
    "FileZilla",
    # Recon / scanning
    "Nmap",
    "Masscan",
    "ZGrab",
    # 注: 除外したもの ----
    # "frp"   — 3 文字、一般語と衝突するため少々 (HTTP frp / proxy frp 等)
    # "Certify" — TLS 文脈で多発の一般語
    # "WinRAR" / "7-Zip" — 一般的 archiver、攻撃 tool としては個別判定必須
    # "WinSecPolicy" / "Sysinternals" / "PsTools" — generic suite name
)

# malware/tool 名は単語単位 (case-insensitive)、word boundary 風の前後文字制限
# (短い名前の false positive を抑制)。
_NAME_BOUNDARY = r"(?:^|[\s\(\[\.,:;\"'<>「（）]|/|\b)"
_NAME_END = r"(?:$|[\s\(\)\[\]\.,:;\"'<>」（）]|/|\b)"


def _build_name_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    # 長い順にソート (Cobalt Strike が Cobalt より先に match)
    sorted_names = sorted(set(names), key=len, reverse=True)
    escaped = "|".join(re.escape(n) for n in sorted_names if len(n) >= 4)
    return re.compile(
        rf"{_NAME_BOUNDARY}({escaped}){_NAME_END}",
        re.IGNORECASE,
    )


_MALWARE_PATTERN = _build_name_pattern(KNOWN_MALWARE_FAMILIES)
_TOOL_PATTERN = _build_name_pattern(KNOWN_TOOLS)


def _extract_dictionary_matches(
    text: str,
    pattern: re.Pattern[str],
    canonical: dict[str, str],
) -> set[str]:
    """text から pattern hit を canonical 名集合で返す。case-insensitive match"""
    hits: set[str] = set()
    for m in pattern.finditer(text):
        matched = m.group(1)
        cano = canonical.get(matched.lower(), matched)
        hits.add(cano)
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="article_entities backfill")
    parser.add_argument(
        "--since-days",
        type=int,
        default=90,
        help="N 日以内の article のみ対象",
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-articles", type=int, default=10_000)
    args = parser.parse_args()

    repo = RunHistoryRepository()
    since_iso = (datetime.now(UTC) - timedelta(days=args.since_days)).isoformat()

    # canonical map (lower → canonical)
    mal_canon = {n.lower(): n for n in KNOWN_MALWARE_FAMILIES}
    tool_canon = {n.lower(): n for n in KNOWN_TOOLS}
    # Phase 0 (F2): Adversary backfill 用 actor registry。
    actor_registry = load_actor_aliases()

    counters: Counter[str] = Counter()
    processed = 0
    articles_with_entity = 0

    for article_id, title, summary, body in repo.iter_articles_with_body(
        batch_size=args.batch_size,
        since_iso=since_iso,
    ):
        if processed >= args.max_articles:
            break
        processed += 1
        full_text = " ".join(filter(None, [title, summary or "", body or ""]))
        if not full_text.strip():
            continue
        entities: list[tuple[str, str]] = []

        # 1. IoC / CVE / mitre_techniques を ioc_extractor で
        try:
            extracted = extract_iocs(body or "", source_url=None)
        except Exception as e:  # noqa: BLE001
            _log.debug("ioc_extract_fail", article_id=article_id, error=str(e))
            extracted = None
        if extracted is not None:
            for ip in extracted.ipv4:
                entities.append(("ioc_ip", ip))
            for dom in extracted.domains:
                entities.append(("ioc_domain", dom))
            for h in extracted.sha256:
                entities.append(("ioc_sha256", h))
            for h in extracted.sha1:
                entities.append(("ioc_sha1", h))
            for h in extracted.md5:
                entities.append(("ioc_md5", h))
            for u in extracted.urls:
                entities.append(("ioc_url", u))
            for c in extracted.cves:
                entities.append(("cve", c.upper()))
            for t in extracted.mitre_techniques:
                entities.append(("ttp", t.upper()))

        # 2. malware / tool dictionary lookup (title+summary+body)
        for name in _extract_dictionary_matches(full_text, _MALWARE_PATTERN, mal_canon):
            entities.append(("malware_family", name))
        for name in _extract_dictionary_matches(full_text, _TOOL_PATTERN, tool_canon):
            entities.append(("tool", name))

        # 3. Adversary: actor_aliases.yaml で canonical id を検出 (Phase 0 F2)。
        for actor in actor_registry.find_all(full_text):
            entities.append(("actor", actor.id))

        # 5. dedup + persist
        unique_entities = list(dict.fromkeys(entities).keys())
        if not unique_entities:
            continue
        try:
            inserted = repo.add_article_entities(article_id, unique_entities)
        except Exception as e:  # noqa: BLE001
            _log.warning("entities_persist_fail", article_id=article_id, error=str(e))
            continue
        if inserted > 0:
            articles_with_entity += 1
        for t, _v in unique_entities:
            counters[t] += 1

        if processed % 200 == 0:
            _log.info(
                "entity_backfill_progress",
                processed=processed,
                with_entity=articles_with_entity,
                by_type=dict(counters),
            )

    _log.info(
        "entity_backfill_done",
        processed=processed,
        articles_with_entity=articles_with_entity,
        by_type=dict(counters),
    )
    print("=== Entity backfill summary ===")
    print(f"  processed articles:        {processed}")
    print(f"  articles with any entity:  {articles_with_entity}")
    print("  entity counts by type:")
    for t, n in counters.most_common():
        print(f"    {t:<20} {n:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
