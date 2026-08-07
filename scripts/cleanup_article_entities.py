#!/usr/bin/env python3
"""Phase Diamond verify-fix: article_entities の false positive を一括削除。

削除対象:
1. benign domain (163.com, qq.com, vendor advisory, 政府 etc.) — ioc_extractor の filter 強化と整合
2. benign URL (vendor.com/advisories, cert.pl/cvd など)
3. 不適切 tool entries (軍事兵器名 / AI モデル / installer ファイル名)
4. Kali365 を tool → malware_family へ reclass

Usage:
    docker exec kuebiko /app/.venv/bin/python3 /app/scripts/cleanup_article_entities.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# 削除すべき tool entries (実データ検証で false positive 確認済)
INVALID_TOOLS = (
    # 軍事兵器 / 防空 / ドローン
    "BARAK MX", "Blue Spear", "Harop", "Harpy", "Mini-Harpy", "Neros Archer", "UAV",
    # AI モデル / 学習関連
    "LoRA", "LORA", "Mythos AI", "AI-generated nudes",
    # installer / DLL ファイル名 (IoC として別途扱う)
    "InitInstall.dll",
    # その他 generic 名詞 / 中国 IT
    "LOGINK",
)

# tool → malware_family に reclass すべき名前
RECLASS_TOOL_TO_MALWARE = ("Kali365",)


def main() -> int:
    con = sqlite3.connect("/app/data/run_history.db")
    cur = con.cursor()

    deleted = {}

    # ① benign domain 削除 (163.com 105 件等)
    BENIGN_DOMAINS = (
        "163.com", "126.com", "qq.com", "yeah.net", "sina.com", "sina.com.cn",
        "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.co.jp",
        "icloud.com", "protonmail.com",
        "weibo.com", "wechat.com", "baidu.com",
        "siemens.com", "abb.com", "hitachienergy.com", "hitachi.com",
        "schneider-electric.com", "cisco.com", "microsoft.com",
        "fortinet.com", "paloaltonetworks.com",
        "stealthmole.com", "recordedfuture.com", "mandiant.com",
        "cert.pl", "cert.org", "first.org",
        "cisa.gov", "us-cert.cisa.gov", "www.cisa.gov",
        "12339.gov.cn", "ASP.NET", "System.Net", "asp.net", "system.net",
        "git-tanstack.com",
    )
    # case-insensitive 削除
    placeholders = ",".join("?" * len(BENIGN_DOMAINS))
    cur.execute(
        f"DELETE FROM article_entities WHERE entity_type='ioc_domain' "
        f"AND LOWER(value) IN ({placeholders})",
        tuple(d.lower() for d in BENIGN_DOMAINS),
    )
    deleted["ioc_domain (benign)"] = cur.rowcount

    # ② benign URL 削除 (vendor advisory / cert / 公式 contact)
    BENIGN_URL_PATTERNS = (
        "%siemens.com%", "%abb.com/global%", "%hitachienergy.com/contact%",
        "%cert.pl/cvd%", "%cisa.gov%", "%first.org%",
    )
    n_url = 0
    for pattern in BENIGN_URL_PATTERNS:
        cur.execute(
            "DELETE FROM article_entities WHERE entity_type='ioc_url' AND LOWER(value) LIKE ?",
            (pattern,),
        )
        n_url += cur.rowcount
    deleted["ioc_url (benign)"] = n_url

    # ③ tool false positive 削除
    placeholders = ",".join("?" * len(INVALID_TOOLS))
    cur.execute(
        f"DELETE FROM article_entities WHERE entity_type='tool' "
        f"AND value IN ({placeholders})",
        INVALID_TOOLS,
    )
    deleted["tool (military / AI / installer)"] = cur.rowcount

    # ④ Kali365 を tool → malware_family へ reclass
    cur.execute(
        "UPDATE article_entities SET entity_type='malware_family' "
        "WHERE entity_type='tool' AND value IN ({})".format(
            ",".join("?" * len(RECLASS_TOOL_TO_MALWARE))
        ),
        RECLASS_TOOL_TO_MALWARE,
    )
    deleted["tool→malware reclass"] = cur.rowcount

    # ⑤ 異常 IPv4 削除 (network base address, broadcast)
    cur.execute(
        "DELETE FROM article_entities WHERE entity_type='ioc_ip' "
        "AND value IN ('0.0.0.0','146.0.0.0','255.255.255.255')",
    )
    deleted["ioc_ip (invalid base)"] = cur.rowcount

    con.commit()
    print("=== cleanup_article_entities done ===")
    total = 0
    for k, n in deleted.items():
        print(f"  {k:<35} deleted: {n}")
        total += n
    print(f"  TOTAL deleted/modified: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
