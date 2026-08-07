"""LLM 出力の routing 用構造化フラグ (Phase 5L-3)。

router が LLM の自由文を regex でパースしていた弱さ (Phase 5K の japan_watch
誤分類) を解消するため、prompts/summarizer.j2 で LLM に直接構造化フラグを
出させ、router はこれを primary シグナルとして利用する。

regex は ``confidence == "low"`` のときのみ補助 (二重防御) として残す。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]
EditorialStance = Literal[
    "factual_report",  # 事象報道 (中立)
    "analytical",  # 分析・解説 (中立、シンクタンク報告など)
    "opinion",  # 主観意見だが妥当な議論
    "propaganda",  # 特定国家・組織の framing narrative (R5b 降格対象)
    "unknown",  # 判定不能
]


class LLMRoutingFlags(BaseModel):
    """LLM が summarizer prompt で生成する routing 補助フラグの構造化スキーマ。

    フィールド:
        japan_targeted: 日本の組織 (政府/企業/重要インフラ等) が
            **明示的に標的・被害として記載されている** 場合のみ True。
            「日本のセキュリティ研究者が解説」「日本周辺諸国も対象」程度では False。

        is_breaking_critical: 24h 以内に対応・分析が必要な進行中事案。
            KEV 級脆弱性、active exploitation、進行中インシデント等。
            recap / 解説 / 政策動向は False。

        primary_actor_id: 主要 APT アクター名 (小文字スラッグ、例: "salt typhoon",
            "lazarus")。複数アクターが同等に主要なら最も活動が新しい方。
            判定不能なら空文字。

        dedup_key: 同事象識別キー。可能なら ``cve-2026-12345`` 形式の CVE-ID、
            それが無ければ ``{actor}-{verb}-{target}`` 形式 (例:
            "salt-typhoon-supplychain-ibm-italy") の slug。

        confidence: 上記フラグの自己評価。"low" のときは regex/heuristic フォールバック
            に降格させる用途。

        editorial_stance: 記事の編集スタンス (Phase B-R5b 改訂)。
            ``propaganda`` 判定時は R5b で watch 強制降格。
            判定不能なら ``unknown``。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    japan_targeted: bool = False
    is_breaking_critical: bool = False
    primary_actor_id: str = Field(default="", max_length=80)
    dedup_key: str = Field(default="", max_length=120)
    confidence: Confidence = "low"
    editorial_stance: EditorialStance = "unknown"


def parse_routing_flags(raw: object) -> LLMRoutingFlags:
    """LLM 出力 dict (もしくは欠落/不正値) から ``LLMRoutingFlags`` を安全に生成する。

    LLM が ``routing_flags`` を欠落させた / 型不一致を返したケースでも
    クラッシュさせず安全側 (全 False / 空 / "low") のデフォルトを返す。
    """
    if not isinstance(raw, dict):
        return LLMRoutingFlags()
    # 個別フィールドを型検証し default に倒す
    japan_targeted = (
        raw.get("japan_targeted") if isinstance(raw.get("japan_targeted"), bool) else False
    )
    is_breaking_critical = (
        raw.get("is_breaking_critical")
        if isinstance(raw.get("is_breaking_critical"), bool)
        else False
    )
    actor_raw = raw.get("primary_actor_id")
    primary_actor_id = (
        _normalize_slug(actor_raw, max_length=80) if isinstance(actor_raw, str) else ""
    )
    key_raw = raw.get("dedup_key")
    dedup_key = _normalize_slug(key_raw, max_length=120) if isinstance(key_raw, str) else ""
    conf_raw = raw.get("confidence")
    confidence: Confidence = conf_raw if conf_raw in ("high", "medium", "low") else "low"
    stance_raw = raw.get("editorial_stance")
    editorial_stance: EditorialStance = (
        stance_raw
        if stance_raw in ("factual_report", "analytical", "opinion", "propaganda", "unknown")
        else "unknown"
    )
    try:
        return LLMRoutingFlags(
            japan_targeted=bool(japan_targeted),
            is_breaking_critical=bool(is_breaking_critical),
            primary_actor_id=primary_actor_id,
            dedup_key=dedup_key,
            confidence=confidence,
            editorial_stance=editorial_stance,
        )
    except Exception:  # noqa: BLE001
        return LLMRoutingFlags()


def _normalize_slug(value: str, *, max_length: int) -> str:
    """文字列を小文字 + ASCII 安全な slug 風に整える (空白 → ハイフン)。

    LLM が大文字交じり / 全角空白 / 末尾制御文字等を出してもキー一致できるよう
    保守的に正規化。日本語等の非 ASCII はそのまま保持 (CVE-ID は ASCII)。
    """
    # 1. 制御文字 / 不可視文字を先に除去 (この後の split / join が安定)
    cleaned = "".join(c for c in value if c.isprintable())
    # 2. trim + lowercase
    cleaned = cleaned.strip().lower()
    # 3. 連続空白 → 単一ハイフン
    cleaned = "-".join(cleaned.split())
    # 4. 末尾ハイフンの掃除 (制御文字混入時に発生しうる)
    cleaned = cleaned.strip("-")
    return cleaned[:max_length]
