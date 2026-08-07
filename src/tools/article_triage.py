"""記事の重要度を軽量 LLM で先に判定する triage 層 (Phase 3.1)。

設計:
    Inoreader は 1 日 100 件規模の記事を生成するが、Discord への通知は
    CTI 観点で重要なものに絞りたい。フル要約 (gemma4:31b ~15s/記事) を
    全件に適用すると 25 分かかるので、軽量 LLM で **タイトル + 概要のみ**
    から重要度を 1 単語で判定し、フィルタしてから本要約に進める。

    LLM 用途は「分類器」(Phase 3.0r と同じ思想):
      - 入力: タイトル + 概要 1500 字程度
      - 出力: importance ∈ {high, medium, low} + 短い理由
      - think=False、軽量モデル (gemma4:26b など) を想定

失敗時:
    triage 失敗は medium にフォールバック (誤って重要記事を捨てない安全側)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.llm_client import LLMClient

# Phase 5I: _strip_html を src/tools/text_utils.py に集約 (main.py と統合)
from src.tools.text_utils import strip_html as _strip_html

_log = get_logger(__name__)


class TriageDecision(BaseModel):
    """1 記事の triage 結果。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    importance: Literal["high", "medium", "low"] = "medium"
    reason: str = Field(default="", max_length=200)
    # Phase 5P: LLM 失敗による medium フォールバックかを表す。
    # True なら呼び出し側でカウントし run_history.triage_error_count に集計、
    # ops チャンネルへ即時通知する (silent 失敗対策)。
    error: bool = False


class ArticleTriage:
    """記事の重要度を判定する軽量 triage クラス。"""

    def __init__(
        self,
        llm: LLMClient,
        *,
        body_preview_chars: int = 1500,
        think: bool = False,
    ) -> None:
        self._llm = llm
        self._body_preview_chars = body_preview_chars
        self._think = think

    async def triage(self, article: Article) -> TriageDecision:
        """1 記事を重要度判定する。失敗時は medium で graceful degradation。"""
        prompt = self._build_prompt(article)
        try:
            return await self._llm.generate_structured(
                prompt,
                schema=TriageDecision,
                think=self._think,
                max_tokens=400,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "triage_failed",
                article_id=article.id,
                error=str(e),
            )
            # 安全側: 重要記事を誤って捨てないため medium にフォールバック。
            # ただし error=True で呼び出し側に「LLM 障害で fail-open した」ことを知らせる。
            return TriageDecision(
                importance="medium",
                reason=f"triage error: {type(e).__name__}",
                error=True,
            )

    def _triage_content(self, article: Article) -> str:
        """triage 判定に使う本文プレビューを返す。

        Phase 3: thin feed 対策で先行抽出した ``body_text`` があればそれを優先
        (薄い RSS snippet ではなく実本文で判定)。無ければ従来通り summary_html。
        """
        raw = article.body_text or article.summary_html or ""
        return _strip_html(raw)[: self._body_preview_chars]

    def _build_prompt(self, article: Article) -> str:
        """Triage prompt を構築。PIR yaml が active なら PIR-driven、不在なら legacy。

        PIR-driven path:
            config/pir.yaml の enabled PIR の title + description を、判定基準
            (high / medium) として動的注入。low は exclusion 性質のため hardcoded のまま。
            既存挙動との互換は migration 時の PIR yaml 内容で担保 (verify_pir_migration.py)。

        legacy path:
            env var PIR_DRIVEN_TRIAGE=0 or PIR yaml が空のとき、Phase 5T 時点の
            hardcoded 13 high criteria を使用 (緊急 rollback 経路)。
        """
        # Lazy import で循環依存 + テスト時の副作用を回避
        from src.pir.integration import (
            build_triage_high_criteria,
            build_triage_medium_criteria,
            get_pir_config,
            is_pir_driven_triage_enabled,
        )

        if is_pir_driven_triage_enabled():
            cfg = get_pir_config()
            high_criteria = build_triage_high_criteria(cfg.priorities)
            medium_criteria = build_triage_medium_criteria(cfg.priorities)
            if high_criteria:
                return self._build_prompt_pir_driven(article, high_criteria, medium_criteria)
        # legacy fallback (Phase 5T 時点の hardcoded、互換性確保のため保持)
        return self._build_prompt_legacy_hardcoded(article)

    def _build_prompt_pir_driven(
        self,
        article: Article,
        high_criteria: str,
        medium_criteria: str,
    ) -> str:
        """PIR yaml から動的生成された prompt。

        各 PIR の title + description が judging criteria として注入される。
        user が PIR を編集すると、ここに反映される。
        """
        body_preview = self._triage_content(article)
        title = (article.title or "").strip()
        feed = (article.feed_title or "").strip()

        medium_section = ""
        if medium_criteria:
            medium_section = "## 判定基準 (medium)\n" + medium_criteria + "\n\n"

        return (
            "あなたは日本の CTI アナリストです。"
            "以下の記事を **タイトルと概要のみ** から重要度判定してください。"
            "本文を要約する必要はありません。判定だけ返します。\n\n"
            f"フィード: {feed}\n"
            f"タイトル: {title}\n"
            f"概要: {body_preview}\n\n"
            "## 判定基準 (high)\n"
            "次のいずれかに該当すれば **high** (即時通知):\n"
            f"{high_criteria}\n\n"
            f"{medium_section}"
            "## 判定基準 (low)\n"
            "- 単発の金銭目的犯罪で **日本国外** の一般企業のデータ侵害 (海外の小規模事案)\n"
            "- 製品マーケティング / イベント告知 / RSA や Black Hat の宣伝\n"
            "- 一般的なセキュリティ Tips / ベストプラクティス記事\n"
            "- 古い事例の振り返り (新情報なし)\n"
            "- 完全に無関係な話題 (フィード混入のノイズ)\n\n"
            "## 出力ルール\n"
            "- importance: {high, medium, low} のいずれかを 1 単語で\n"
            "- reason: 30 字以内で判定理由を簡潔に "
            "(例: 「中国APTのSolarWinds型サプライチェーン侵害」「金銭目的犯罪、影響限定的」)\n"
        )

    def _build_prompt_legacy_hardcoded(self, article: Article) -> str:
        """Phase 5T 時点の hardcoded prompt (rollback 用、本番では PIR-driven を使用)。

        PIR yaml が空 / 不在 / env で disable された場合の fallback。
        """
        body_preview = self._triage_content(article)
        title = (article.title or "").strip()
        feed = (article.feed_title or "").strip()
        return (
            "あなたは日本の CTI アナリストです。"
            "以下の記事を **タイトルと概要のみ** から重要度判定してください。"
            "本文を要約する必要はありません。判定だけ返します。\n\n"
            f"フィード: {feed}\n"
            f"タイトル: {title}\n"
            f"概要: {body_preview}\n\n"
            "## 判定基準 (high)\n"
            "次のいずれかに該当すれば **high** (即時通知):\n"
            "1. 中国系 APT (Volt Typhoon, Salt Typhoon, Silk Typhoon, "
            "APT41, APT10, APT40, MSS/PLA 直轄系) の動向\n"
            "2. 北朝鮮系 APT (Lazarus, Kimsuky, Andariel) / "
            "ロシア系 APT (Sandworm, APT28, APT29, GRU/SVR 直轄系) の動向 "
            "(特に日本対象なら最優先)\n"
            "3. その他アクター (イラン系、犯罪集団等) による **日本標的** の攻撃\n"
            "4. 防衛・政府・重要インフラ標的 "
            "(OT/ICS/SCADA、発電・送電、上下水道、鉄道、通信/5G、防衛産業基盤、"
            "AI/宇宙・電磁波アセット、衛星・宇宙資産)\n"
            "5. 国・政府レベルに影響のランサムウェア "
            "(金銭目的でも、医療・電力・自治体等の停止を伴うもの)\n"
            "6. APT 関連企業や国家系サイバー組織の **内部情報漏洩** "
            "(i-Soon リーク, Vulkan files, Conti chat leaks 級 — APT 実態が暴露されるもの)\n"
            "7. **広域影響** のセキュリティ企業 / MSSP / 認証基盤侵害 "
            "(SolarWinds 級の連鎖侵害、Okta/Microsoft Entra/Cisco 等)\n"
            "8. 広域影響のソフトウェアサプライチェーン汚染 (log4j 級、xz utils 級)\n"
            "9. 統合サイバー作戦 (AI を活用した攻撃/防御、宇宙・電磁波サイバー、"
            "サイバー物理融合、軍事先端技術関連)\n"
            "10. 中国・北朝鮮・ロシア APT への帰属公表 "
            "(五眼同盟合同 attribution、警察庁・防衛省・NSC 発表、米財務省 OFAC 制裁、DOJ 起訴)\n"
            "11. 緊急度の高い国家機関アラート "
            "(CISA Emergency Directive、JPCERT 緊急、ED-25-XX 級 — 緊急度低い更新は除く)\n"
            "12. 日本・同盟国 (米英豪韓) ・東アジア敵対国 (中朝露) の "
            "**国家戦略 / 地政学的サイバー分析**\n"
            "13. 中露の **影響力工作・偽情報作戦** (DISINFO/IO、サイバー作戦とは別軸で重要)\n\n"
            "## 判定基準 (medium)\n"
            "- 既知脅威の継続フォローアップ\n"
            "- 新 PoC 公開 / 業界注目脆弱性\n"
            "- 同盟国・東アジアでの一般的サイバー出来事 (日本標的でないもの)\n"
            "- 影響範囲が限定的なセキュリティ企業侵害\n"
            "- 一般的な国家機関アラート (緊急度低い更新等)\n"
            "- 一般的なサプライチェーン攻撃事例 (広域影響でない)\n"
            "- **日本企業 / 日本国内組織への ransomware / breach / 不正アクセス / 情報漏洩 — "
            "規模・業種を問わず最低 medium**。CTI mission の本筋であり、"
            "「一般企業の事案」を理由に low へ落とすことは禁止。\n"
            "  例: ホクヨーへのランサムウェア、エネサンスHD 漏洩、CAMPFIRE 不正アクセス、"
            "横浜DeNA 個人情報漏洩、第一工業ランサムウェア → すべて medium 以上\n\n"
            "## 判定基準 (low)\n"
            "- 単発の金銭目的犯罪で **日本国外** の一般企業のデータ侵害 (海外の小規模事案)\n"
            "- 製品マーケティング / イベント告知 / RSA や Black Hat の宣伝\n"
            "- 一般的なセキュリティ Tips / ベストプラクティス記事\n"
            "- 古い事例の振り返り (新情報なし)\n"
            "- 完全に無関係な話題 (フィード混入のノイズ)\n\n"
            "## 出力ルール\n"
            "- importance: {high, medium, low} のいずれかを 1 単語で\n"
            "- reason: 30 字以内で判定理由を簡潔に "
            "(例: 「中国APTのSolarWinds型サプライチェーン侵害」「金銭目的犯罪、影響限定的」)\n"
        )
