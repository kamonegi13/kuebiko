"""victim_org のベンダ混入を決定論で遮断する SSoT (2026-08-19)。

判定基準は「**含めないもの**: ベンダ / ソフトメーカー (脆弱性・製品の提供元)」と
明記しているが、30 日実測で Google 19 / Microsoft 17 / Cisco 6 等 **49 件**が混入し、
地図の組織本社プロットと被害国 KPI に偽の点を出していた。中身はバグバウンティの
報奨金記事・製品対応記事で、いずれも被害組織ではない。

⭐ **指示では止まらない** — 同日 summarizer (判定基準の強化で逆に悪化) / judgment
(禁止事項を明記しても政党・個人・防御側を収穫) と同じ性質。決定論の関門で止める。

⚠ **保守則: そのベンダ自身が侵害された記事は残す**。「OpenAI 社内 Slack 侵害」の
ような実被害を巻き込まないため、category が breach / incident の行は遮断しない
(2026-08-01 の掃除 script が確立した規約をそのまま引き継ぐ)。

取込 filter (persistence) / 掃除 script (purge_vendor_victim_org) が共有する。
"""

from __future__ import annotations

# 実測で victim_org に混入した提供元。⚠ 列挙は攻めてよい — 実侵害は下の
# category 条件が保護するため、過剰に弾いても実害記事は残る。
VENDOR_DENYLIST: frozenset[str] = frozenset(
    {
        # AI プラットフォーム (2026-08-01 の掃除 script から継承)
        "openai",
        "anthropic",
        "hugging face",
        "huggingface",
        "google deepmind",
        "deepmind",
        "meta ai",
        "mistral",
        "mistral ai",
        "cohere",
        "stability ai",
        "xai",
        "perplexity",
        "perplexity ai",
        # 一般 IT ベンダ (2026-08-19 実測。旧 denylist は AI 限定で素通りしていた)
        "microsoft",
        "google",
        "apple",
        "cisco",
        "oracle",
        "adobe",
        "sap",
        "vmware",
        "citrix",
        "fortinet",
        "ivanti",
        "atlassian",
        "gitlab",
        "github",
        "amazon",
        "aws",
        "meta",
        "ibm",
        "intel",
        "nvidia",
        "salesforce",
        "zoom",
        "slack",
        "mozilla",
        "docker",
        "red hat",
        "canonical",
        "broadcom",
        "palo alto networks",
        "crowdstrike",
        "sophos",
        "trend micro",
        "kaspersky",
        "eset",
        "checkpoint",
        "check point",
    }
)

# 実被害の可能性があるため遮断しない category (そのベンダ自身が侵害された記事)。
PROTECTED_CATEGORIES: frozenset[str] = frozenset({"breach", "incident"})


def is_vendor_noise(org: str, category: str | None) -> bool:
    """``org`` を victim_org として保存すべきでないか (純粋関数)。

    ⚠ category が breach / incident なら常に False — ベンダ自身の侵害記事を守る。
    """
    if category is not None and category.strip().lower() in PROTECTED_CATEGORIES:
        return False
    return org.strip().lower() in VENDOR_DENYLIST
