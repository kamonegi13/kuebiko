"""分析軸 (intent / technical / event_date) の **focused 単機能分類器** (2026-07-13)。

背景: socio_political_intent / technical_axis_summary / event_date は summarizer
(過負荷の 26B、多数フィールド同時出力) の **末尾フィールド** として枯死した
(実測: 取込側 fill-rate が intent 75%→2% / technical 16%→1% / event_date 45%→5%。
プロンプト文言の修復 0663350 だけでは取込側は回復しなかった)。editorial_stance が
同じ病理で focused 分類器 (editorial_stance_classifier.py) に切り出されて 100% 安定した
前例に従い、分析軸 3 種を 1 つの小プロンプトに切り出す。backfill_intent.py の focused
プロンプト (実測 61% yield) が土台。

pipeline (forward、briefing._summarize_and_build) と backfill (scripts/backfill_axes.py)
の両方がこの 1 実装を使う。防御パース (parse_diamond_axes / _normalize_temporal) は
呼び出し側の既存 seam で行う — 本モジュールは LLM 出力を素通しで返すだけにして、
正規化ロジックの二重化を避ける。

ローカル LLM のみ。障害時は None (呼び出し側は summarizer 由来の値 = 空を維持)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_BODY_MAX_CHARS = 8000  # backfill_intent.py と同じ (判定に十分、呼出 ~1-2s/件)
_MIN_BODY_CHARS = 200  # これ未満は summary で代替


class AnalysisAxesOut(BaseModel):
    """LLM 構造化出力 (防御は下流の parse_diamond_axes / _normalize_temporal で行う)。"""

    model_config = ConfigDict(extra="ignore")

    intent: str = "unknown"
    confidence: str = "low"
    rationale: str | None = None
    technical: str | None = None
    event_date: str | None = None
    event_date_basis: str | None = None
    compromise_date: str | None = None
    # PMESII I-infra 軸 (2026-07-16): summarizer 末尾フィールドとして枯死したため
    # 本 focused 分類器に移設 (決定論フロア nisc_sector_for との union は呼び出し側)。
    i_infra: bool = False

    def to_diamond_dict(self) -> dict[str, object]:
        """SummaryOutput.diamond と同形の dict (既存 parse_diamond_axes に流すため)。"""
        return {
            "socio_political": {
                "intent": self.intent,
                "confidence": self.confidence,
                "rationale": self.rationale,
            },
            "technical": self.technical,
        }


# intent ブロックは backfill_intent.py の実証済みプロンプト (「確度つき記録」レジーム)。
# event_date は「切り捨てでなく basis で正直さを表現」(監査 2026-07-12 H1 の恒久対処):
# 明示日付・相対表現に加えて「進行中/直近の具体的事象だが日付非明示」を basis=reported で
# 報道日に係留することを許可する。下流は basis でフィルタできる。
_PROMPT_TEMPLATE = """あなたは CTI アナリスト。以下の記事について 3 つの分析軸を判定し、\
JSON のみで出力する。

# 軸1: intent — 主体 (攻撃者・国家等) の戦略的 intent (動機)。いずれか 1 つ
- espionage: 諜報・情報窃取 (機密/知財/個人情報の標的的窃取)
- financial: 金銭目的 (ransomware / 窃取 / 詐欺 / 暗号資産。DPRK 外貨稼ぎ含む)
- prepositioning: 事前配置 (有事に備えた重要インフラへの足場確保・潜伏。破壊や窃取をまだ行わず\
 access を維持。Volt Typhoon 型)
- disruption: 破壊・妨害 (wiper / sabotage / OT 物理影響 / DDoS。機能を損なうこと自体が目的)
- influence: 影響工作・認知戦 (世論操作 / disinformation / hack-and-leak)
- hacktivism: 主義主張・抗議 (イデオロギー動機)
- coercion: 威圧・強要 (圧力/威嚇/制裁/示威で相手の行動・政策を変えさせる)
- deterrence: 抑止 (能力・決意を示し相手の行動を思いとどまらせる。防御的)
- territorial: 領土・主権 (領土/主権/係争海域の主張・奪取・越境)
- subversion: 体制動揺・転覆 (対象国の内部の安定・正統性を内側から崩す)
- diplomacy: 外交・同盟 (同盟・条約・正常化・連携の協調行動)
- unknown: 動機を推定する材料が本文に実質ない

判定規則:
- confidence は証拠の強さの正直な申告: high=標的・動機が本文で実証 / medium=強い状況証拠・\
推定混じり / low=弱いシグナルからの仮説 (ツール類似のみ・コモディティ malware・帰属未確定)。
- 過剰確信の禁止: 禁じるのは確度の水増しであって、仮説の記録ではない。弱いシグナルでも方向が\
読み取れるなら最有力の intent + confidence=low で記録し、rationale に根拠の弱さを明示する。\
low 相当の根拠を medium/high と申告しない。
- unknown は「材料が実質ない」場合のみ (製品リリース / 一般調査・統計レポート / 主体の意図が\
論点にならない記事)。弱い証拠を unknown に倒さない。
- rationale: 判定根拠の日本語 1 行 (80 字以内)。unknown なら null。

# 軸2: technical — Capability ⇄ Infrastructure の技術的結線 (string|null)
「どの capability (TTP/malware/tool/脆弱性) を、どの infrastructure (配信元/C2/staging) を\
用いて運用するか」の手口の繋がりを 1-2 文 (120 字以内、日本語) で。\
例: 「ScreenConnect 経由で初期侵入し、Cobalt Strike beacon を Cloudflare fronted C2 で運用」。\
手口が部分的にしか書かれていなくても、初期侵入経路・悪用機能・配布手段など**読み取れた結線を\
書く** (完全な kill-chain は不要)。純粋な政策/地政学記事など技術的手口が皆無なら null。

# 軸3: 時間軸 — event_date / event_date_basis / compromise_date
- event_date (YYYY-MM-DD|null): 記事が報じる主要事象の日付。優先順:
  1. 本文に明示された日付 (「June 28」等。月のみ明示は当月 1 日)。basis は事象の性質で\
 occurred (発生) / detected (検知) / disclosed (公表・開示)。
  2. 相対表現を【報道日】基準で解決 (「昨日」=報道日-1、「先週」「今月初め」=代表日)。\
 basis=relative (粒度の粗さを正直に示す)。
  3. 進行中/直近の**具体的事象**の報道だが日付が本文に無い → event_date=報道日、\
 basis=reported (報道時点しか分からないことを正直に示す)。
  4. 総説・チュートリアル・統計レポート・製品発表など「特定の事象」が無い記事 → null。
- 過去事案の参照日 (関連記事・出典の昔の事件) を本件の event_date にしない。
- compromise_date (YYYY-MM-DD|null): 初期侵害の開始日が本文に明示されていれば\
 (「3 月から侵入していた」等)。必ず event_date 以前。明示が無ければ null。

# 軸4: i_infra — 重要インフラ関連か (true|false)
物理サービスを提供する重要インフラ (医療/電力/ガス/水道/通信/交通/物流/金融基盤/\
政府サービス/防衛産業/半導体製造/OT・ICS・SCADA) への攻撃・脅威・防護・政策に**実質的に**\
関わるなら true。一般企業の IT breach・個別 CVE 解説・SaaS 脆弱性 (重要インフラでの運用が\
明示されない限り)・セキュリティ製品リリースは false。

# 出力 (JSON のみ、他のテキスト禁止)
{{"intent": "...", "confidence": "high|medium|low", "rationale": "...", "technical": "...",\
 "event_date": "YYYY-MM-DD", "event_date_basis": "occurred|detected|disclosed|reported|relative",\
 "compromise_date": null, "i_infra": false}}

# 記事
カテゴリ: {category}
報道日: {published}
タイトル: {title}
本文:
{text}
"""


def build_axes_prompt(
    title: str,
    category: str | None,
    body: str | None,
    summary_text: str | None,
    published: str | None,
) -> str | None:
    """分析軸プロンプトを組み立てる (body 優先、なければ summary。実質空なら None)。"""
    text = (body or "").strip()
    if len(text) < _MIN_BODY_CHARS:
        text = (summary_text or "").strip()
    if not text:
        return None
    return _PROMPT_TEMPLATE.format(
        category=category or "unknown",
        published=published or "不明",
        title=(title or "").strip(),
        text=text[:_BODY_MAX_CHARS],
    )


async def classify_analysis_axes(
    llm: LLMClient,
    *,
    title: str,
    category: str | None,
    body: str | None,
    summary_text: str | None,
    published: str | None,
) -> AnalysisAxesOut | None:
    """記事 1 本の分析軸 3 種を focused に判定する。障害時は None (安全側 = 空を維持)。"""
    prompt = build_axes_prompt(title, category, body, summary_text, published)
    if prompt is None:
        return None
    try:
        out = await llm.generate_structured(prompt, schema=AnalysisAxesOut, think=False)
    except Exception as e:  # noqa: BLE001 — 分類失敗で記事処理を止めない
        _log.warning("analysis_axes_classify_failed", error=str(e)[:80])
        return None
    # 型防御: schema 非対応の client/fake が別型を返しても記事処理を壊さない
    if not isinstance(out, AnalysisAxesOut):
        _log.warning("analysis_axes_unexpected_type", got=type(out).__name__)
        return None
    return out
