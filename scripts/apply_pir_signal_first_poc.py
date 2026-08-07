"""signal-first PoC の match ツリーを DB (config_store) の pir config に注入する。

raw dict 操作 (Pir model 非依存) なので旧/新どちらの container でも動く。再実行冪等
(既存 match を上書き)。ツリーの根拠は docs/pir_signal_first_matching_design.md §5。

    docker exec -e PYTHONPATH=/app kuebiko python scripts/apply_pir_signal_first_poc.py
"""

from __future__ import annotations

from src.storage.config_store import get_config, save_config

# signal-first で有効化済みの match ツリー (すべて永続化済みフィールドのみ = スキーマ移行ゼロ)。
# Phase 1 (2026-07-22): disinfo / new_poc_vuln / state_ransomware。
# Phase 2 zero-migration (2026-07-22): jp_company_breach / ally_cyber_event。
# 再実行冪等 (既存 match を上書き)。docs/pir_signal_first_matching_design.md §5/§7.5。
TREES: dict[str, dict[str, object]] = {
    "pir_disinfo": {"property": "intent", "op": "eq", "value": "influence"},
    "pir_new_poc_vuln": {"property": "category", "op": "eq", "value": "vulnerability"},
    # 国家・政府レベルのランサム: LLM 主題判定の候補ゲートに転換 (recall 監査 2026-07-23)。
    # 旧 sector AND は ransomware の 62% が sector 欠損のため実 FN (米郡政府/米政府機関/
    # 英水道会社) を落としていた。ゲート = is_ransomware AND 非地政学 (is_ransomware flag の
    # 地政学 FP も遮断)、公共機能への影響判定は LLM が確定する。
    "pir_state_ransomware": {
        "all": [
            {"property": "is_ransomware", "op": "is_true"},
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
        ]
    },
    # 日本企業/組織への breach/ransomware = victim_country=jp AND (ransomware OR breach/incident)。
    # 英語 ransomware/breach keyword + JP-feed OR が拾っていた非日本事案 (独/米 breach) を除去。
    "pir_jp_company_breach": {
        "all": [
            {"property": "victim_country", "op": "eq", "value": "jp"},
            {
                "any": [
                    {"property": "is_ransomware", "op": "is_true"},
                    {"property": "category", "op": "in", "value": ["breach", "incident"]},
                ]
            },
        ]
    },
    # 同盟国での一般サイバー事案 = victim_country∈{米英豪韓台} AND 非 geopolitical。
    # 軍事/地政学記事 (advisory keyword で漏れていた) を category で除外。国別 victim の無い
    # グローバル脆弱性は new_poc_vuln が拾うため意図的に対象外。
    "pir_ally_cyber_event": {
        "all": [
            {"property": "victim_country", "op": "in", "value": ["us", "gb", "au", "kr", "tw"]},
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
        ]
    },
    # 国家機関の一般アラート = 4 機関フィード AND category∈{advisory, vulnerability}。
    # keyword 公開/alert が地政学/軍事まで拾っていた (1751件) のを機関発信のみに精密化。
    # vulnerability を含めるのは recall 監査 2026-07-23 — JVN/ICS 系の機関 advisory 68 件が
    # category=vulnerability に分類され落ちていた (全件正当な機関アラート)。
    # feed 名は完全名 ("CISA Cybersecurity Advisories" 等) のため contains_any (substring)。
    "pir_general_agency_alert": {
        "all": [
            {
                "property": "feed_title",
                "op": "contains_any",
                "value": ["cisa", "jpcert", "enisa", "ncsc"],
            },
            {"property": "category", "op": "in", "value": ["advisory", "vulnerability"]},
        ]
    },
    # --- Phase 2 keyword_any (2026-07-23): 具体名キーワード PIR の多義語コンタミ除去 ---
    # APT 内部リーク: LLM 主題判定の**候補ゲート**に転換 (Phase B PoC)。leak 系を広く
    # 拾い直し (recall 回復)、主題適合は LLM が確定する — 具体名 (i-Soon) でも
    # 「例示言及だけ」の FP が残ることが実測で確定したため (30d 残 2 件が両方 FP)。
    "pir_apt_leak": {
        "property": "text",
        "op": "keyword_any",
        "value": [
            "内部漏洩",
            "リーク",
            "leak",
            "i-Soon",
            "Vulkan",
            "Conti chat",
            "leaked documents",
            "data dump",
        ],
    },
    # 限定的セキュリティ企業侵害。EDR (製品名ノイズ) を排す (195→95)。
    "pir_minor_security_vendor_breach": {
        "property": "text",
        "op": "keyword_any",
        "value": ["security vendor", "MSSP", "セキュリティ企業"],
    },
    # SW SC 汚染 (log4j/xz 級)。not geopolitical で物理 SC 漏れを防ぐ (64→64、健全維持)。
    "pir_sw_supply_chain": {
        "all": [
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["log4j", "xz", "npm package", "supply chain attack", "PyPI"],
            },
        ]
    },
    # 広域 SC 侵害。物理/経済 SC (海上封鎖/潜水艦調達) を not geopolitical で除外 (655→289)。
    "pir_supply_chain_broad": {
        "all": [
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["認証基盤", "MSSP", "サプライチェーン", "supply chain"],
            },
        ]
    },
    # 限定的 SC 事例。同上 (728→334)。
    "pir_minor_supply_chain": {
        "all": [
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["npm", "PyPI", "dependency", "サプライチェーン", "supply chain"],
            },
        ]
    },
    # APT 帰属公表の候補ゲート。多義語は LLM judge が精度を守るため、recall 側に寄せる:
    # title スコープの 制裁/sanction を追加 (recall 監査 2026-07-23 — EU 制裁記事 2 件が
    # 帰属/起訴語なし+category=geopolitical でゲート外だった。body スコープだと +1,384 の
    # 爆発になるため title 限定)。
    "pir_apt_attribution": {
        "any": [
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["attribution", "帰属", "OFAC", "indictment", "起訴"],
            },
            {
                "property": "title",
                "op": "keyword_any",
                "value": ["制裁", "sanction"],
            },
            {
                "property": "feed_title",
                "op": "contains_any",
                "value": ["doj", "ofac", "警察庁", "nsc"],
            },
        ]
    },
    # 防衛/政府/重要インフラ標的 = victim_sector ∪ OT語(ICS/OT/SCADA)。多義語 重要インフラ/
    # 防衛産業 (月面防衛・欧州再軍備等の地政学を誤爆) を排す。A/B 821→431。
    # japan_critical boolean は intent に不要 (A/B 2026-07-23、Phase 2b 移行不要と判明)。
    "pir_critical_infra": {
        "any": [
            {
                "property": "victim_sector",
                "op": "in",
                "value": [
                    "defense",
                    "government",
                    "energy",
                    "telecom",
                    "transportation",
                    "critical_infra",
                ],
            },
            {"property": "text", "op": "keyword_any", "value": ["ICS", "OT", "SCADA"]},
        ]
    },
    # 緊急度の高い国家機関アラート = CISA Emergency Directive / JPCERT 緊急。真の緊急指令は
    # 本来レア (A/B 30d=1)。多義語 緊急 (氾濫) を排す。ルーティン KEV/ICS advisory は
    # agency_alert が拾う (kev boolean は intent 不一致=Phase 2b 移行不要)。A/B 225→1。
    "pir_emergency_alerts": {
        "property": "text",
        "op": "keyword_any",
        "value": ["Emergency Directive", "JPCERT 緊急"],
    },
    # --- Phase A (2026-07-23、docs/pir_concept_llm_judge_design.md §6) ---
    # 統合サイバー作戦。学術サイドチャネル論文 (category=research) を除外 (15→8)。
    # NOT geopolitical は真陽性 (軍事 AI/電子攻撃 = この PIR の本質) を殺すため不採用。
    # 残余の言及系は Phase C で LLM 主題判定。
    "pir_integrated_cyber_ops": {
        "all": [
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["AI攻撃", "サイバー物理", "宇宙サイバー", "電磁波"],
            },
            {"not": {"property": "category", "op": "in", "value": ["research"]}},
        ]
    },
    # 日本標的の攻撃事案 = victim_country=jp AND 非 geopolitical (391→338)、
    # + title{日本, 日系} × 攻撃系 category の救済枝 (recall 監査 2026-07-23 —
    # victim_country 欠損 (breach/incident の 50%) で新日本検定協会/PoisonX 日本標的
    # キャンペーン等の実 FN があった。body スコープは 「国内」⊂「米国内」等の substring
    # 誤爆で +213 の大半がノイズになるため title 限定)。
    "pir_jp_targeted": {
        "any": [
            {
                "all": [
                    {"property": "victim_country", "op": "eq", "value": "jp"},
                    {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
                ]
            },
            {
                "all": [
                    {
                        "property": "category",
                        "op": "in",
                        "value": ["breach", "incident", "malware"],
                    },
                    {"property": "title", "op": "keyword_any", "value": ["日本", "日系"]},
                ]
            },
        ]
    },
    # --- Phase D (2026-07-23、docs/pir_concept_llm_judge_design.md §7): 再定義 +
    # 候補ゲート。旧 8ヶ国 victim OR (via countries=956/1558 が主犯) を撤去し、
    # 「地政学/政策 × サイバー語」の genre 候補に絞る (30d 560)。主題適合は LLM が確定。
    "pir_geopolitical_cyber": {
        "all": [
            {"property": "category", "op": "in", "value": ["geopolitical", "policy"]},
            {
                "property": "text",
                "op": "keyword_any",
                "value": ["サイバー", "cyber", "ハッキング", "hacking"],
            },
        ]
    },
}

# --- authoring 統一 (2026-07-23、docs/pir_authoring_unification_design.md §3.1): Tier 1
# (APT 3 件) を actor/actor_nation leaf でツリー化。A/B = legacy と完全一致 (109/109/125)。
# 主題アクターゲート意味論は leaf 内で bug-for-bug 温存。これで全 enabled PIR が同一言語。
APT_TREES: dict[str, dict[str, object]] = {
    "pir_china_apt": {
        "any": [
            {
                "property": "actor",
                "op": "any_of",
                "value": [
                    "Volt Typhoon",
                    "Salt Typhoon",
                    "Silk Typhoon",
                    "Flax Typhoon",
                    "Storm-0558",
                    "APT41",
                    "APT10",
                    "APT40",
                    "Mustang Panda",
                ],
            },
            {"property": "actor_nation", "op": "in", "value": ["cn"]},
        ]
    },
    "pir_dprk_apt": {
        "any": [
            {
                "property": "actor",
                "op": "any_of",
                "value": ["Lazarus", "Kimsuky", "Andariel", "APT38"],
            },
            {"property": "actor_nation", "op": "in", "value": ["kp"]},
        ]
    },
    "pir_russia_apt": {
        "any": [
            {
                "property": "actor",
                "op": "any_of",
                "value": ["Sandworm", "APT28", "APT29", "Cozy Bear", "Fancy Bear", "Gamaredon"],
            },
            {"property": "actor_nation", "op": "in", "value": ["ru"]},
        ]
    },
}
TREES.update(APT_TREES)

# --- Phase B/C/D: 概念 PIR の LLM 主題判定 (docs/pir_concept_llm_judge_design.md §5) ---
# match (候補ゲート) 通過分にのみ夜間バッチが判定。question 空 = title+description が基準。
LLM_JUDGE: dict[str, dict[str, object]] = {
    # Phase B PoC: 言及≠主題の実証済み FP (FSB 制裁 / PLA 調達の i-Soon 例示) を落とす。
    "pir_apt_leak": {"enabled": True, "question": ""},
    # recall 監査 2026-07-23: sector 欠損 62% の取りこぼしを LLM が補う (公共機能への
    # 影響 = 概念判定)。ゲートは is_ransomware AND 非地政学。
    "pir_state_ransomware": {
        "enabled": True,
        "question": (
            "国・政府レベルに影響するランサムウェア事案 — 政府機関・自治体・医療・電力・"
            "水道・通信など公共機能/重要インフラの停止・侵害、またはそれに準ずる社会影響 — "
            "が記事の主題か。一般企業の単発被害、被害統計・週刊まとめ、犯人の逮捕・裁判"
            "だけの記事は不適合。"
        ),
    },
    # Phase C: 「国家による APT 帰属の公表」概念。起訴/attribution の多義 FP を落とす。
    "pir_apt_attribution": {
        "enabled": True,
        "question": (
            "国家機関・政府による、国家系 APT / 国家支援サイバーアクターへの攻撃帰属の公表 "
            "(合同 attribution、制裁、起訴、名指し公表) が記事の主題か。"
            "サイバーと無関係な起訴・制裁・輸出規制、研究論文の帰属手法は不適合。"
        ),
    },
    # Phase C: 「統合作戦」概念。市場予測・単なる技術言及を落とす。
    "pir_integrated_cyber_ops": {
        "enabled": True,
        "question": (
            "AI・宇宙・電磁波・サイバー物理融合など先端技術の、軍事・作戦レベルでの"
            "統合サイバー作戦への利用 (攻撃/防御/能力開発) が記事の主題か。"
            "市場規模予測・製品宣伝・語の言及だけの記事は不適合。"
        ),
    },
    # Phase D: 再定義した description が基準 (question 不要)。
    "pir_geopolitical_cyber": {"enabled": True, "question": ""},
}

# Phase D: pir_geopolitical_cyber の再定義 (利用者承認済 2026-07-23)。
# description は canonical intent — triage 基準と LLM 判定基準の双方に効く。
DESCRIPTIONS: dict[str, str] = {
    "pir_geopolitical_cyber": (
        "国家 (特に中朝露・米・日) のサイバー戦略・ドクトリン・能力・作戦・法制度に"
        "関する分析・政策動向。単なる被害事案や一般地政学 (軍事・外交・経済) は含まない。"
    ),
}


def main() -> None:
    raw = get_config("pir")
    if not isinstance(raw, dict) or "priorities" not in raw:
        raise SystemExit("pir config not found in DB (config_store key='pir')")

    updated: list[str] = []
    for pir in raw["priorities"]:
        if not isinstance(pir, dict):
            continue
        pid = str(pir.get("id"))
        touched = False
        if pid in TREES:
            pir["match"] = TREES[pid]
            touched = True
        if pid in LLM_JUDGE:
            pir["llm_judge"] = LLM_JUDGE[pid]
            touched = True
        if pid in DESCRIPTIONS:
            pir["description"] = DESCRIPTIONS[pid]
            touched = True
        if touched:
            updated.append(pid)

    version = save_config("pir", raw, note=f"signal-first + llm_judge: {len(updated)} PIR 更新")
    print(f"updated {len(updated)} PIRs {updated}, new version={version}")


if __name__ == "__main__":
    main()
