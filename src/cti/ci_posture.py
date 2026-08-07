"""JP 重要インフラ board の posture 純粋ロジック (近接度ラダー)。

board の本質 = 「脅威がどの段階まで来ているか」を分野ごとに示す I&W 型の評価。
段階 (ラダー) は下から:

- ``quiet`` 静穏: シグナルなし (被覆明示、静か≠安全)
- ``indication`` 予兆: 同盟国同分野での活動 (read-across) / アクターの分野関心 / 分野技術の脆弱性
- ``targeting`` 標的化: アクターが日本の当分野を標的 / 実悪用中 (KEV) の脆弱性が分野技術に該当
- ``intrusion_it`` 侵害(IT系): 日本の当分野で敵対事象 (情報系)
- ``ot_approach`` 制御系接近: 敵対事象が OT/境界に接近、または事前潜伏 (prepositioning) 意図

system_domain / intent は**過大評価の減衰機構** (設計 doc §6): CI 事業者のただの IT 被害を
国家 CI 脅威に水増ししない。逆に prepositioning は IT 経由でも最上段 (Volt Typhoon 型)。

ここは DB 非依存の純粋関数のみ (集約側 = src/ui/services/jp_ci_board.py)。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# ---------- 近接度ラダー ----------

STAGE_RANKS: dict[str, int] = {
    "quiet": 0,
    "indication": 1,
    "targeting": 2,
    "intrusion_it": 3,
    "ot_approach": 4,
}

STAGE_LABELS: dict[str, str] = {
    "quiet": "静穏",
    "indication": "予兆",
    "targeting": "標的化",
    "intrusion_it": "侵害(IT系)",
    "ot_approach": "制御系接近",
}

# 段階バケット (quiet を除く上位 4 段。sector_stage / 表示列で降順に使う)
ACTIVE_STAGES: tuple[str, ...] = ("ot_approach", "intrusion_it", "targeting", "indication")


def observed_stage(
    system_domain: str, intent: str | None, intent_confidence: str | None = None
) -> str:
    """観測 (敵対) 事象の段階。OT/境界 or 事前配置 → 制御系接近、それ以外は侵害(IT系)。

    H3 (intent_confidence 配線): confidence=low の prepositioning は仮説なので
    最上段 (制御系接近) へ昇段させない。NULL は旧レジームの確定 tag として昇段を許す。
    """
    prepos_confirmed = intent == "prepositioning" and (intent_confidence or "") != "low"
    if system_domain in ("ot", "boundary") or prepos_confirmed:
        return "ot_approach"
    return "intrusion_it"


def sector_stage(stages: dict[str, list[dict[str, Any]]]) -> str:
    """分野の到達段階 = 空でない最上位バケット (全て空なら quiet)。"""
    for stage in ACTIVE_STAGES:
        if stages.get(stage):
            return stage
    return "quiet"


# ---------- 敵対性ゲート ----------

# アクター不在の事故 (誤送信/紛失/設定ミス等)。I&W の脅威圧力ではないため board の
# 脅威計上から隔離する (件数は nonthreat として正直に表示)。title の決定論パターン。
_NONTHREAT_PATTERN = re.compile(
    "誤送信|誤送付|誤交付|誤公開|誤掲載|誤提供|誤廃棄|誤配布|誤入力|誤表示|誤設定|"
    "誤解釈|誤登録|誤発送|誤配信|誤って|紛失|置き忘れ|所在不明|設定ミス|設定不備|誤操作"
)


def is_nonthreat_mishap(title: str) -> bool:
    """title が非敵対事象 (事務事故) を示すか。敵対語が併記される場合は脅威扱いを維持する。"""
    if not _NONTHREAT_PATTERN.search(title):
        return False
    # 敵対の明示があれば脅威側に倒す (例: 「不正アクセス後に誤って公開」)
    return not re.search("不正アクセス|ランサム|マルウェア|攻撃|標的|フィッシング|侵入", title)


# 統計・調査・回顧記事 (事象でない)。「原因調査」等の実事案表現に誤爆しないよう複合語のみ。
_SURVEY_PATTERN = re.compile(
    "実態調査|意識調査|調査結果|調査レポート|アンケート|統計|白書|漏えい率|"
    r"\d+[%％]が|\d+割が|が語った|インタビュー"
)


def is_survey_or_statistics(title: str) -> bool:
    """title が統計・調査・回顧記事 (個別の新規事象でない) を示すか。board の観測対象外。"""
    return _SURVEY_PATTERN.search(title) is not None


# ブランド詐称 (事業者を装う顧客標的の詐欺)。事業者システムへの侵入ではないため
# 観測レーンから除外する (phishing category 除外と同じ理由)。ただし詐称は初期潜入の
# 手口でもあるため、侵害の実示唆 (侵入/感染/流出等) が併記される場合は事象として維持。
_IMPERSONATION_PATTERN = re.compile("を装っ|を装う|をかたる|を騙る|なりすまし|偽サイト|偽SMS")
_INTRUSION_EVIDENCE_PATTERN = re.compile(
    "侵入|感染|ランサム|マルウェア|漏えい|漏洩|流出|不正アクセス被害|システム障害"
)


def is_brand_impersonation(title: str) -> bool:
    """title が事業者ブランド詐称 (顧客標的・侵入なし) を示すか。"""
    if not _IMPERSONATION_PATTERN.search(title):
        return False
    return not _INTRUSION_EVIDENCE_PATTERN.search(title)


# ---------- 報道クラスタ集約 ----------

_DOMAIN_SEVERITY: dict[str, int] = {"ot": 3, "boundary": 2, "it": 1, "unknown": 0}
_IMPORTANCE_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}
_CONFIDENCE_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}
_ORG_NOISE = re.compile(r"株式会社|\(株\)|（株）|\s+")


def _org_key(org: str) -> str:
    # NFKC で全角英数を半角に揃える (公的文書由来の「ＫＤＤＩ」等と半角表記を同一視)
    return _ORG_NOISE.sub("", unicodedata.normalize("NFKC", org)).lower()


def _title_key(title: str) -> str:
    return re.sub(r"\s+", "", title)


def merge_observed_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一事案の多重報道を 1 事象に畳む (KDDI 型: 28 報道 = 1 事案)。

    連結規則: victim_org (正規化) を共有する記事は同一事象 (連結成分)。org の無い記事は
    title 完全一致のみで畳む (過剰連結しない保守側)。件数は ``report_count`` に保存し
    「報道数 ≠ 事案数」の取り違えを防ぐ。証拠は合併 (domain/確度/intent は最大севを採る)。

    入力 item 契約: id/title/orgs/system_domain/importance/url/event_date/confidence/intent。
    """
    # 1. org → 成分 id の union (経由連結: a-KDDI, KDDI-中部 → 1 成分)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        org_keys = [_org_key(o) for o in it.get("orgs") or [] if o]
        if org_keys:
            root = f"org:{org_keys[0]}"
            for k in org_keys[1:]:
                union(root, f"org:{k}")
        else:
            root = f"title:{_title_key(str(it.get('title', '')))}"
        groups.setdefault(root, []).append(it)

    # union 後に成分ルートで再グルーピング
    merged_groups: dict[str, list[dict[str, Any]]] = {}
    for key, members in groups.items():
        merged_groups.setdefault(find(key), []).extend(members)

    out: list[dict[str, Any]] = []
    for members in merged_groups.values():
        rep = max(
            members,
            key=lambda m: _IMPORTANCE_RANK.get(str(m.get("importance") or ""), 0),
        )
        domain = max(
            (str(m.get("system_domain") or "unknown") for m in members),
            key=lambda d: _DOMAIN_SEVERITY.get(d, 0),
        )
        confidence = max(
            (str(m.get("confidence") or "low") for m in members),
            key=lambda c: _CONFIDENCE_RANK.get(c, 0),
        )
        intent = next(
            (m.get("intent") for m in members if m.get("intent") == "prepositioning"),
            rep.get("intent"),
        )
        operators = [o for m in members for o in (m.get("orgs") or []) if o]
        # 重複除去 (順序維持)
        seen: set[str] = set()
        uniq_ops: list[str] = []
        for o in operators:
            key = _org_key(str(o))
            if key not in seen:
                seen.add(key)
                uniq_ops.append(str(o))
        out.append(
            {
                **rep,
                "system_domain": domain,
                "confidence": confidence,
                "intent": intent,
                "operators": uniq_ops[:3],
                "report_count": len(members),
            }
        )
    # 表示順: 段階の重い順 → importance → 報道数
    out.sort(
        key=lambda e: (
            _DOMAIN_SEVERITY.get(str(e["system_domain"]), 0),
            _IMPORTANCE_RANK.get(str(e.get("importance") or ""), 0),
            e["report_count"],
        ),
        reverse=True,
    )
    return out


# ---------- 見出し (決定論合成) ----------


def _stage_fragment(stage: str, items: list[dict[str, Any]]) -> str:
    """段階バケットの一行要約 (見出しの部品)。"""
    top = items[0]
    if stage in ("ot_approach", "intrusion_it"):
        prefix = "制御系・境界に接近する敵対事象" if stage == "ot_approach" else "敵対事象"
        return f"{prefix} {len(items)}件 — {str(top.get('title', ''))[:60]}"
    if stage == "targeting":
        actor_item = next((i for i in items if i.get("kind") == "actor"), None)
        if actor_item is not None:
            return f"{actor_item.get('actor')} が日本の当分野を標的"
        cves = top.get("cves") or []
        cve_note = f" ({cves[0]})" if cves else ""
        return f"実悪用中の脆弱性が分野技術に該当{cve_note}"
    # indication
    rax = next((i for i in items if i.get("kind") == "readacross"), None)
    if rax is not None:
        allies = rax.get("allies") or ""
        where = f"同盟国 ({allies})" if allies else "同盟国"
        return f"{rax.get('actor')} が{where}の同分野で活動"
    if any(i.get("kind") == "actor" for i in items):
        actor = next(i for i in items if i.get("kind") == "actor")
        return f"{actor.get('actor')} が当分野に関心"
    return f"分野技術の脆弱性 {len(items)}件"


_COVERAGE_LABELS: dict[str, str] = {"thin": "薄", "medium": "中", "rich": "厚"}


def compose_headline(
    stage: str, stages: dict[str, list[dict[str, Any]]], coverage_tier: str
) -> str:
    """分野の一行見出し。主段階の要約 + 次に高い非空段階の短い文脈。"""
    if stage == "quiet":
        tier = _COVERAGE_LABELS.get(coverage_tier, coverage_tier)
        return f"観測なし — 静かは安全ではない (被覆: {tier})"
    parts = [_stage_fragment(stage, stages[stage])]
    for lower in ACTIVE_STAGES:
        if STAGE_RANKS[lower] < STAGE_RANKS[stage] and stages.get(lower):
            parts.append(_stage_fragment(lower, stages[lower]))
            break
    return " ｜ ".join(parts)
