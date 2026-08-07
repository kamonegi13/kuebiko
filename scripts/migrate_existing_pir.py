"""既存 hardcoded PIR (article_triage.py の 13 high criteria) を PIR yaml に migration。

戦略: behavior-preserving migration。
  - 各 hardcoded item を 1 PIR として抽出
  - target_channel="auto" / target_importance="high" (or medium) で挿入
  - spotlight.enabled=false (Spotlight 自動生成しない、user が後で enable)
  - approved_by_user=false (UI で「承認待ち」banner に表示)
  - migrated_from にトレース情報 ("article_triage.py:high:1" 等)

Usage:
    uv run python scripts/migrate_existing_pir.py             # 標準出力に preview
    uv run python scripts/migrate_existing_pir.py --apply    # config/pir.yaml に書込み

注意: --apply 時、既に config/pir.yaml に PIR がある場合は merge せず追加する。
重複 id があれば追加せずスキップ。
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# project root を path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pir.loader import load_pir_config, save_pir_config
from src.pir.models import (
    Pir,
    PirMetadata,
    SpotlightConfig,
    StrongSignals,
)

# article_triage.py の high 13 criteria を構造化抽出
# (実コードからの転記、actor / sector hints も補足)
_HIGH_CRITERIA: list[dict] = [
    {
        "id": "pir_china_apt",
        "title": "中国系 APT 動向 (Volt Typhoon, APT41 系)",
        "description": (
            "中国系 APT (Volt Typhoon, Salt Typhoon, Silk Typhoon, "
            "APT41, APT10, APT40, MSS/PLA 直轄系) の動向を最優先で検知。"
            "PLA/MSS 系の事前配置 (pre-positioning)、台湾標的、サプライチェーン汚染も含む。"
        ),
        "actors": [
            "Volt Typhoon", "Salt Typhoon", "Silk Typhoon", "Flax Typhoon",
            "Storm-0558", "APT41", "APT10", "APT40", "Mustang Panda",
        ],
        "countries": ["CN"],
        "spotlight_title": "🇨🇳 中国 APT 動向",
    },
    {
        "id": "pir_dprk_apt",
        "title": "北朝鮮系 APT 動向 (Lazarus, Kimsuky 等)",
        "description": (
            "北朝鮮系 APT (Lazarus, Kimsuky, Andariel) の動向。"
            "金融・暗号資産窃取、政策諜報、海外 IT 労働者偽装も含む。"
            "特に日本対象なら最優先。"
        ),
        "actors": ["Lazarus", "Kimsuky", "Andariel", "APT38"],
        "countries": ["KP"],
        "spotlight_title": "🇰🇵 北朝鮮 APT 動向",
    },
    {
        "id": "pir_russia_apt",
        "title": "ロシア系 APT 動向 (Sandworm, APT28, APT29 等)",
        "description": (
            "ロシア系 APT (Sandworm, APT28, APT29, GRU/SVR 直轄系) の動向。"
            "destructive operations / 諜報 / 影響力工作。特に日本対象なら最優先。"
        ),
        "actors": ["Sandworm", "APT28", "APT29", "Cozy Bear", "Fancy Bear", "Gamaredon"],
        "countries": ["RU"],
        "spotlight_title": "🇷🇺 ロシア APT 動向",
    },
    {
        "id": "pir_jp_targeted",
        "title": "日本標的の攻撃 (国家系 APT + その他アクター)",
        "description": (
            "イラン系・犯罪集団等を含むあらゆるアクターによる日本標的の攻撃事案。"
            "日本企業・日本政府・日本国内重要インフラを標的とする active threat 全般。"
        ),
        "keywords": ["日本標的", "日本企業", "国内重要インフラ", "日系大手"],
        "countries": ["JP"],
        "spotlight_title": "🇯🇵 JP-targeted threats",
    },
    {
        "id": "pir_critical_infra",
        "title": "防衛・政府・重要インフラ標的",
        "description": (
            "OT/ICS/SCADA、発電・送電、上下水道、鉄道、通信/5G、防衛産業基盤、"
            "AI/宇宙・電磁波アセット、衛星・宇宙資産。広く重要インフラへの脅威。"
        ),
        "keywords": ["OT", "ICS", "SCADA", "重要インフラ", "防衛産業"],
        "sectors": ["defense", "government", "energy", "telecom", "transportation", "critical_infra"],
    },
    {
        "id": "pir_state_ransomware",
        "title": "国家・政府レベルに影響のランサムウェア",
        "description": (
            "金銭目的でも医療・電力・自治体等の停止を伴うランサムウェア。"
            "国家機能や社会インフラに影響する大規模事案。"
        ),
        "keywords": ["ransomware", "ランサムウェア"],
        "sectors": ["healthcare", "energy", "government"],
    },
    {
        "id": "pir_apt_leak",
        "title": "APT 内部情報漏洩 (i-Soon / Vulkan 級)",
        "description": (
            "APT 関連企業や国家系サイバー組織の内部情報漏洩。"
            "i-Soon リーク、Vulkan files、Conti chat leaks 級の APT 実態暴露。"
        ),
        "keywords": ["内部漏洩", "leak", "i-Soon", "Vulkan", "Conti chat"],
    },
    {
        "id": "pir_supply_chain_broad",
        "title": "広域影響セキュリティ企業 / MSSP / 認証基盤侵害",
        "description": (
            "SolarWinds 級の連鎖侵害、Okta/Microsoft Entra/Cisco 等の "
            "認証基盤 / MSSP / セキュリティ企業侵害で広域に波及するもの。"
        ),
        "keywords": ["supply chain", "サプライチェーン", "認証基盤", "MSSP"],
        "feed_titles": ["Okta", "Microsoft Security", "Cisco"],
    },
    {
        "id": "pir_sw_supply_chain",
        "title": "広域影響ソフトウェアサプライチェーン汚染 (log4j/xz 級)",
        "description": (
            "log4j 級、xz utils 級のソフトウェアサプライチェーン汚染。"
            "1 component の侵害が広域に波及するもの。"
        ),
        "keywords": ["log4j", "xz", "npm package", "PyPI", "supply chain attack"],
    },
    {
        "id": "pir_integrated_cyber_ops",
        "title": "統合サイバー作戦 (AI攻撃/宇宙電磁波)",
        "description": (
            "AI を活用した攻撃/防御、宇宙・電磁波サイバー、"
            "サイバー物理融合、軍事先端技術関連の統合作戦。"
        ),
        "keywords": ["AI攻撃", "サイバー物理", "宇宙サイバー", "電磁波"],
    },
    {
        "id": "pir_apt_attribution",
        "title": "中国・北朝鮮・ロシア APT への帰属公表",
        "description": (
            "五眼同盟合同 attribution、警察庁・防衛省・NSC 発表、"
            "米財務省 OFAC 制裁、DOJ 起訴等の APT 帰属公表。"
        ),
        "keywords": ["attribution", "帰属", "OFAC", "indictment", "起訴", "制裁"],
        "feed_titles": ["DOJ", "OFAC", "警察庁", "NSC"],
    },
    {
        "id": "pir_emergency_alerts",
        "title": "緊急度の高い国家機関アラート",
        "description": (
            "CISA Emergency Directive、JPCERT 緊急、ED-XX 級の国家機関緊急アラート。"
            "緊急度低い更新は除く。"
        ),
        "keywords": ["Emergency Directive", "JPCERT 緊急", "ED-", "緊急"],
        "feed_titles": ["CISA", "JPCERT"],
        "spotlight_title": "⚡ Emergency Alerts",
    },
    {
        "id": "pir_geopolitical_cyber",
        "title": "国家戦略 / 地政学的サイバー分析",
        "description": (
            "日本・同盟国 (米英豪韓)・東アジア敵対国 (中朝露) の "
            "国家戦略 / 地政学的サイバー分析。"
        ),
        "keywords": ["geopolitical", "cyber strategy", "国家戦略", "doctrine"],
        "countries": ["JP", "US", "GB", "AU", "KR", "CN", "KP", "RU"],
    },
    {
        "id": "pir_disinfo",
        "title": "中露の影響力工作・偽情報作戦 (DISINFO/IO)",
        "description": (
            "中露の影響力工作・偽情報作戦 (DISINFO/IO)。"
            "サイバー作戦とは別軸で重要、選挙干渉や認知戦も含む。"
        ),
        "keywords": ["DISINFO", "IO", "influence operation", "偽情報", "影響力工作"],
        "countries": ["CN", "RU"],
    },
]

# medium criteria (Phase 5T-V + JP fix + 5T-Z 4 件追加で完全互換化)
_MEDIUM_CRITERIA: list[dict] = [
    {
        "id": "pir_jp_company_breach",
        "title": "日本企業 / 日本国内組織への ransomware / breach / 不正アクセス",
        "description": (
            "日本企業・日本国内組織への ransomware / breach / 不正アクセス / 情報漏洩。"
            "規模・業種を問わず最低 medium。CTI mission の本筋。"
            "例: ホクヨーへのランサムウェア、エネサンスHD 漏洩、CAMPFIRE 不正アクセス等。"
        ),
        "keywords": ["不正アクセス", "情報漏洩", "ランサムウェア攻撃", "breach", "ransomware"],
        "countries": ["JP"],
        "feed_titles": [
            "ScanNetSecurity", "Security NEXT", "セキュリティニュース",
            "ITmedia エンタープライズ", "@IT セキュリティ",
        ],
        "spotlight_title": "🇯🇵 JP company breach",
    },
    {
        "id": "pir_known_threat_followup",
        "title": "既知脅威の継続フォローアップ",
        "description": "既存報告された threat の続報・展開・新詳細。",
        "target_importance": "medium",
    },
    {
        "id": "pir_new_poc_vuln",
        "title": "新 PoC 公開 / 業界注目脆弱性",
        "description": "新規 PoC 公開や業界が注目する脆弱性 (KEV 未掲載でも)。",
        "keywords": ["PoC", "proof of concept", "脆弱性"],
        "target_importance": "medium",
    },
    # ↓ Phase 5T-Z で追加 (A/B verify 結果で medium→low flip 27% を検出、4 件欠落が原因)
    {
        "id": "pir_ally_cyber_event",
        "title": "同盟国・東アジアでの一般的サイバー出来事 (日本標的でない)",
        "description": (
            "米英豪韓・台湾・東アジア諸国における一般的なサイバー出来事 / advisory / 脅威分析。"
            "日本標的ではないが地政学的に関連がある事案 (米中緊張下のサイバー作戦、台湾標的、韓国 NK 関連等)。"
        ),
        "keywords": ["advisory", "cyber operation", "サイバー作戦"],
        "countries": ["US", "GB", "AU", "KR", "TW"],
        "target_importance": "medium",
    },
    {
        "id": "pir_minor_security_vendor_breach",
        "title": "影響範囲が限定的なセキュリティ企業侵害",
        "description": (
            "SolarWinds / Okta 級でない、影響範囲が限定的なセキュリティ企業侵害 / "
            "MSSP 侵害 / 一社限定の不正アクセス。広域連鎖侵害ではないもの。"
        ),
        "keywords": ["security vendor", "MSSP", "EDR", "セキュリティ企業"],
        "target_importance": "medium",
    },
    {
        "id": "pir_general_agency_alert",
        "title": "一般的な国家機関アラート (緊急度低い更新等)",
        "description": (
            "CISA / JPCERT / ENISA / NCSC 等の国家機関による一般的なアラート・更新。"
            "Emergency Directive 級ではなく、advisory の routine update / awareness 系。"
        ),
        "keywords": ["advisory", "alert", "guidance", "注意喚起", "公開"],
        "feed_titles": ["CISA", "JPCERT", "ENISA", "NCSC"],
        "target_importance": "medium",
    },
    {
        "id": "pir_minor_supply_chain",
        "title": "一般的なサプライチェーン攻撃事例 (広域影響でない)",
        "description": (
            "log4j / xz utils 級ではない、特定 sw / library / npm package 等の "
            "限定的サプライチェーン侵害事例。広域連鎖を伴わないもの。"
        ),
        "keywords": ["npm", "PyPI", "supply chain", "dependency", "サプライチェーン"],
        "target_importance": "medium",
    },
]


def _build_pir(spec: dict, default_importance: str) -> Pir:
    """1 spec から Pir object を生成。"""
    spotlight_title = spec.get("spotlight_title", "")
    spotlight = SpotlightConfig(
        enabled=False,  # 全 migrated PIR は spotlight 無効で開始
        title=spotlight_title,
        window="weekly",
    )
    strong = StrongSignals(
        keywords=spec.get("keywords", []),
        actors=spec.get("actors", []),
        sectors=spec.get("sectors", []),
        countries=spec.get("countries", []),
        feed_titles=spec.get("feed_titles", []),
    )
    target_importance = spec.get("target_importance", default_importance)
    return Pir(
        id=spec["id"],
        title=spec["title"],
        description=spec["description"],
        enabled=True,
        strong_signals=strong,
        target_importance=target_importance,
        target_channel="auto",  # routing 互換性: 既存 R1-R5b に委ねる
        spotlight=spotlight,
        metadata=PirMetadata(
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            migrated_from=f"article_triage.py:hardcoded:{spec['id']}",
            approved_by_user=False,  # draft 状態 (UI で承認待ち banner)
            rationale="自動 migration: hardcoded triage criteria から PIR 化。"
            "user が UI で内容を確認・修正・承認してください。",
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="config/pir.yaml に実際に書き込む (default は preview のみ)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("config/pir.yaml"),
        help="書き込み先 yaml (--apply 時)",
    )
    args = parser.parse_args()

    high_pirs = [_build_pir(spec, "high") for spec in _HIGH_CRITERIA]
    medium_pirs = [_build_pir(spec, "medium") for spec in _MEDIUM_CRITERIA]
    new_pirs = high_pirs + medium_pirs

    print(f"=== Migration preview ({len(new_pirs)} PIRs) ===\n")
    for p in new_pirs:
        print(f"  [{p.target_importance:6s}] {p.id:35s} {p.title}")
        signals_summary = []
        if p.strong_signals.actors:
            signals_summary.append(f"actors={len(p.strong_signals.actors)}")
        if p.strong_signals.keywords:
            signals_summary.append(f"keywords={len(p.strong_signals.keywords)}")
        if p.strong_signals.sectors:
            signals_summary.append(f"sectors={len(p.strong_signals.sectors)}")
        if p.strong_signals.countries:
            signals_summary.append(f"countries={len(p.strong_signals.countries)}")
        if p.strong_signals.feed_titles:
            signals_summary.append(f"feeds={len(p.strong_signals.feed_titles)}")
        print(f"           signals: {', '.join(signals_summary) or '(none, LLM only)'}")
    print()

    if not args.apply:
        print("--apply なしのため書き込みません。")
        print(f"書き込むには: uv run python {sys.argv[0]} --apply")
        return

    # Existing PIRs と merge (id 重複は skip)
    existing = load_pir_config(args.output)
    existing_ids = {p.id for p in existing.priorities}
    added = 0
    skipped = 0
    for new in new_pirs:
        if new.id in existing_ids:
            skipped += 1
            continue
        existing.priorities.append(new)
        added += 1

    save_pir_config(existing, args.output)
    print(f"完了: {added} 件追加、{skipped} 件 skip (既存)")
    print(f"出力: {args.output}")
    print()
    print("次の手:")
    print("  1. UI (/app/pir) で draft PIR を確認")
    print("  2. 各 PIR を [承認] click で active 化 (or 内容修正)")
    print("  3. scripts/verify_pir_migration.py で A/B 互換性検証")


if __name__ == "__main__":
    main()
