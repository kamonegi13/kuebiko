"""統合判断分類器 (2026-07-26)。

過負荷 summarizer の「判断フィールド枯死」の抜本対策。stance / intent / diamond /
event_date / i_infra / article_type / subject_actor を **1 つの focused 呼び出し**に集約する。

背景と実証:
- summarizer は 18 フィールド + 2-4 段落の長い summary を 1 生成に詰め込むため、末尾の
  **判断系** (推論を要するフィールド) が枯死する — editorial_stance / intent / event_date /
  primary_actor が実測で 0-5% に崩壊 (診断 2026-06〜07)。抽出系 (本文をなぞる) は健全。
- 対策として個別 focused 分類器を都度切り出してきたが (stance / analysis_axes / subject の
  3 呼び出しに増殖)、A/B 検証 (2026-07-26) で **1 呼び出しに統合しても各フィールドの充足・
  正確性が保たれる** ことを実証。さらに article_type は summarizer で 100% "breaking" に
  暗黙枯死しており、統合分類器が advisory/recap を正しく分類 (実データ目視で確認)。

本 module は現行 3 分類器 (editorial_stance_classifier / analysis_axes_classifier /
subject_actor_classifier) を置換する。importance / category / victim は **抽出寄りで枯死
していない** ため summarizer に据え置き (この分類器は扱わない)。ローカル LLM のみ。
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from src.cti.actor_normalizer import ActorAlias
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_BODY_MAX_CHARS = 8000

# 出力候補 (下流の parse/normalize が最終防御なので、ここは緩く受ける)
_STANCES = ("factual_report", "analytical", "opinion", "propaganda", "unknown")
_ARTICLE_TYPES = ("breaking", "advisory", "recap", "tutorial", "research", "press", "opinion")
# 本文がこれ未満なら summary_text を代替入力にする (旧 analysis_axes 分類器と同値)
_SHORT_BODY_CHARS = 200
_CONF = ("high", "medium", "low")
# subject_rationale の表示用上限 (プロンプトは 80 字指示、暴走出力の防御は倍余裕)
_RATIONALE_MAX_CHARS = 200


# LLM に **キーの出力そのものを義務づける** フィールド (2026-08-21、summarizer 08-18 と同機構)。
# 全フィールドに既定値があるため required がゼロで、モデルは JSON をいつでも閉じられた。
# schema 順で最後の subject_rationale が最初の犠牲者になり、「主題なし時は根拠必須」の
# プロンプト指示 (指示では止まらない) にかかわらず 59% が空だった (2026-08-21 実測 144/245)。
# ⚠ Python 側の既定値は残す (内部構築・テストに寛容、LLM にだけ厳格の非対称は意図)。
# ⚠ 全フィールドが "" / null / bool を返せる型のため、捏造の強制にはならない。
_LLM_REQUIRED_FIELDS = frozenset(
    {
        "editorial_stance",
        "article_type",
        "intent",
        "confidence",
        "rationale",
        "technical",
        "event_date",
        "event_date_basis",
        "compromise_date",
        "i_infra",
        "subject_actor_id",
        "subject_confidence",
        "named_primary_actor",
        "subject_rationale",
    }
)


class JudgmentOut(BaseModel):
    """統合判断分類器の LLM 構造化出力 (防御は呼び出し側 / 下流 normalize)。"""

    model_config = ConfigDict(extra="ignore")

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """LLM へ渡す schema でだけ ``required`` を広げる (上の定数の説明を参照)。"""
        schema = handler(core_schema)
        required = set(schema.get("required", ())) | _LLM_REQUIRED_FIELDS
        schema["required"] = sorted(required)
        return schema

    editorial_stance: str = "unknown"
    article_type: str = "breaking"
    intent: str = "unknown"
    confidence: str = "low"
    rationale: str | None = None
    technical: str | None = None
    event_date: str | None = None
    event_date_basis: str | None = None
    compromise_date: str | None = None
    i_infra: bool = False
    subject_actor_id: str = ""
    subject_confidence: str = "low"
    # 新規アクター発見用の ungated 生フィールド (辞書内外を問わず攻撃主体名をそのまま)。
    # subject_actor_id (候補ゲート = 辞書内のみ) と別。辞書未収録のブランド名アクター
    # (CyberAv3ngers 等) を harvest → provisional → 人承認 → 辞書 の発見経路に載せる。
    named_primary_actor: str = ""
    # 主題判定の根拠 (2026-08-13 可視化)。特に主題なし時に「候補アクターは背景言及」等の
    # 理由を残す — 正しい判定でも根拠が見えないと利用者は取りこぼしと区別できない
    # (ConsentFix v3 事案)。記事詳細 UI に表示する。
    subject_rationale: str = ""

    def to_diamond_dict(self) -> dict[str, object]:
        """SummaryOutput.diamond と同形 (既存 parse_diamond_axes に流すため — 下流互換)。"""
        return {
            "socio_political": {
                "intent": self.intent,
                "confidence": self.confidence,
                "rationale": self.rationale,
            },
            "technical": self.technical,
        }


# 各軸の基準は置換元の focused プロンプト (editorial_stance_classifier / analysis_axes_classifier
# / subject_actor_classifier) を忠実に集約。A/B + 実データ目視で正確性を確認済み (2026-07-26)。
#
# ⚠ 編集禁止: DB 編集層 (blocks) の内容と独立させるため、この文字列は他のどこからも import
# されない自己完結コピーとして維持する (rollback 用の frozen 実装、``JUDGMENT_COMPOSER=0`` の実体。
# block 方式の legacy .j2 / ioc_llm_verifier の ``_build_prompt_legacy`` と同じ役割)。
_PROMPT_TEMPLATE_LEGACY = """\
あなたは CTI アナリスト。以下の記事について複数の判断軸を判定し JSON のみで出力する。
**source 名でなく本文の内容で判定する。**

# editorial_stance — 編集スタンス (rhetorical 性質)
factual_report(客観的事実・事象報道。サイバー advisory/CVE/breach + 地政学/政策の客観報道も。
既定) / analytical(中立な分析・解説・technical writeup) / opinion(主観的論評) /
propaganda(一方的 framing・検証なき断定。国営でも客観報道なら factual_report) /
unknown(本文が無い時のみ。**迷ったら factual_report**)。

# article_type — 記事のタイプ (文体・時制・列挙構造で判定)
breaking(進行中インシデント・速報・「先週/昨日確認」) / advisory(ベンダ Advisory・CVE 警告・
KEV・脆弱性詳細解説/PoC) / recap(Weekly/Monthly/Top N/振り返り/期間まとめ・列挙系) /
tutorial(How to・入門・解説ガイド) / research(論文・分析レポート・アカデミック解析) /
press(製品発表・企業ニュース・リリース) / opinion(論説・コラム・考察)。難しければ breaking。

# intent — 主体 (攻撃者・国家等) の戦略的動機 (1 つ)
espionage(諜報・情報窃取。機密/知財/個人情報の標的的窃取) /
financial(金銭。ransomware/窃取/詐欺/暗号資産。DPRK 外貨稼ぎ含む) /
prepositioning(事前配置。有事に備えた重要インフラへの足場確保・潜伏。破壊や窃取をまだ行わず
access を維持。Volt Typhoon 型) /
disruption(破壊・妨害。wiper/sabotage/OT 物理影響/DDoS。機能を損なうこと自体が目的) /
influence(影響工作・認知戦。世論操作/disinformation/hack-and-leak) /
hacktivism(主義主張・抗議) / coercion(威圧・強要。圧力/威嚇/制裁/示威で相手の行動を変えさせる) /
deterrence(抑止。能力・決意を示し思いとどまらせる。防御的) / territorial(領土・主権・係争海域) /
subversion(体制動揺・転覆。対象国の安定・正統性を内側から崩す) / diplomacy(外交・同盟・協調) /
unknown(動機を推定する材料が本文に実質ない)。
confidence = high(標的・動機が本文で実証) / medium(強い状況証拠・推定混じり) /
low(弱いシグナルからの仮説。ツール類似のみ・コモディティ malware・帰属未確定)。
弱い証拠でも方向が読めれば最有力 intent + confidence=low で記録し rationale に根拠の弱さを明示
(過剰確信のみ禁止。弱い証拠を unknown に倒さない)。unknown は材料が実質ない時のみ
(製品リリース / 一般調査・統計レポート / 主体の意図が論点にならない記事)。
rationale = 判定根拠の日本語 1 行 (80 字以内)。unknown なら null。

# technical — Capability⇄Infrastructure の技術的結線 (1 行 string、無ければ null)

# 時間軸 — event_date (YYYY-MM-DD|null) / event_date_basis / compromise_date
event_date = 記事が報じる主要事象の日付。明示日付優先。進行中/直近の具体的事象で日付が本文に
無ければ報道日を係留し basis=reported。過去事案の参照日は使わない。
event_date_basis = occurred|detected|disclosed|reported|relative。
compromise_date = 初期侵害開始日が本文に明示されていれば (YYYY-MM-DD)、無ければ null。

# i_infra (true|false)
重要インフラ (医療/電力/ガス/水道/通信/交通/物流/金融基盤/政府サービス/防衛産業/半導体製造/
OT・ICS・SCADA) への攻撃・脅威・防護・政策に **実質的に** 関わるなら true。
**一般企業の IT breach・個別 CVE 解説・SaaS 脆弱性 (重要インフラでの運用が明示されない限り)・
セキュリティ製品リリースは false。**

# subject_actor_id — 記事が **主題** とするアクター
主題 = 記事の主語 (その集団の活動・作戦・攻撃を報じている)。単なる言及 (比較・背景・
「〜に類似」「過去に〜も」) は主題ではない。**必ず下の候補 id から 1 つ選ぶ。候補外は返さない。**
主題が候補にない/曖昧なら空文字。subject_confidence = high|medium|low。
候補 (記事に登場した既知アクター): {candidates}

# named_primary_actor — 記事が主題とする**攻撃実行主体**の名前 (辞書内外を問わず生の名前)
記事の主題である攻撃者 (APT グループ名・自称 crew 名・ベンダ命名 UNCxxxx/Storm-xxxx 等) を
**本文表記のまま** 1 つ書く。上の候補になくてよい (新顔アクターの発見に使う)。
**絶対に書かないもの: 防御側・報告機関 (CISA/FBI/NSA/US Cyber Command/CERT 等)・被害を受けた
組織/企業・セキュリティベンダ・研究者・**マルウェア/ツール/攻撃キット/技法名 (ClickFix 等)・
脆弱性名・製品/サービス名・一般企業/防衛産業/AI 企業・AI モデル名・個人名 (政治家/経営者/
研究者)・国家/政党/軍組織そのもの・「Russian hackers」等の形容詞的総称**。
「集団としての攻撃主体の固有名」のみ。攻撃者が明確でなければ空文字。

# subject_rationale — 主題判定の根拠 (日本語 1〜2 文、80 字以内)
なぜその主題判定なのかを簡潔に。**主題なし (subject_actor_id が空) の場合は必須**:
候補アクターが本文でどういう位置付けか (出自・比較・過去事例などの背景言及なのか) と、
記事の実際の主語が何かを書く。

# 出力 (JSON のみ、前置き禁止)
{{"editorial_stance":"...","article_type":"...","intent":"...","confidence":"low",
"rationale":null,"technical":null,"event_date":null,"event_date_basis":null,
"compromise_date":null,"i_infra":false,"subject_actor_id":"","subject_confidence":"low",
"named_primary_actor":"","subject_rationale":""}}

# 記事
カテゴリ: {category}
報道日: {published}
タイトル: {title}
本文:
{body}
"""


# ---------- プロンプトの層分け (python_block kind、2026-08-21、group 4 の 2 本目) ----------
#
# ioc_llm_verifier と同じ設計判断: .j2 を持たず Python コードが構造を所有する動的プロンプト
# (subject_actor 候補は実行時に決まるため Jinja ループで表現しない)。編集可能な指示散文は
# セクション単位で 10 block に分離し DB (config_store) 所有にする
# (src/prompts/registry.py の PromptSpec、kind="python_block")。
#
# ioc_llm_verifier との違い: 候補は「値そのもの」でなく「表示済み文字列」として渡ってくる
# ため (subject_actor_id の候補リストは記事ごとに件数が変わる 1 行の文字列表現)、合成は
# **最終文字列を組んでから既存と同じ ``.format()`` を通す**設計にする (block に生の ``{``
# が混入すると ``.format()`` が ``ValueError``/``KeyError`` で落ちる — 保存前検証がこれを
# サンプル context での実合成まで通すことで拾う、既存の ``validate_python_block_rubric`` が
# そのまま使える)。code 所有: candidates 行 (データ注入)、JSON 出力形式行 (JudgmentOut
# スキーマと結合)、「# 記事」以下のデータ部。見出し行は block に含める (byte 一致が成立し、
# かつ UI 編集で見出しごと直せる方が自然 — ioc_llm_verifier と同じ判断)。
SLOT_IDS: tuple[str, ...] = (
    "intro",
    "editorial_stance",
    "article_type",
    "intent",
    "technical",
    "time_axes",
    "i_infra",
    "subject_actor",
    "named_primary_actor",
    "subject_rationale",
)

_PROMPT_ID = "judgment_classifier"

# 保存前検証・preview 用のサンプル context (各 placeholder を埋める非空ダミー)。実データを
# 使わず kuebiko.example 系のみ (§4: ログ・応答に本物の記事本文を持ち込まない)。
SAMPLE_CONTEXT: dict[str, str] = {
    "candidates": "qilin, apt29",
    "category": "apt",
    "published": "2026-01-01",
    "title": "kuebiko.example 系ネットワークへの不正アクセス (render 検証用)",
    "body": (
        "kuebiko.example 系の重要インフラ事業者を標的にしたランサムウェア攻撃が"
        "観測された (render 検証用サンプル本文)。"
    ),
}


def compose_from_blocks(blocks: Mapping[str, str], context: Mapping[str, str]) -> str:
    """blocks (DB 所有の指示散文) + context (code 所有のプレースホルダ値) からプロンプトを合成する。

    レイアウトは ``_PROMPT_TEMPLATE_LEGACY`` と同一。blocks が legacy 由来の verbatim テキスト
    なら結果は byte 一致する (golden 不変量、tests/unit で固定)。組み立てた文字列に
    ``.format(**context)`` を通す ── block に ``candidates``/``category`` 等以外の生の ``{``
    が混入していれば ``ValueError``/``KeyError`` で落ちる (呼び出し側の保存前検証 / fail-safe
    で拾う)。``blocks`` に ``SLOT_IDS`` の欠落があれば ``KeyError``。

    ⚠ 逆に **有効な context キー** (``{body}``/``{title}``/``{candidates}`` 等) を block の
    散文に書くと、エラーにならず**その位置に実データが黙って展開される** (format 名前空間を
    blocks と共有しているため)。単独運用者の編集面なので injection 脅威ではないが、
    「本文を参照」のつもりで ``{body}`` と書くと記事全文がそのセクションに挿入される —
    プレースホルダを散文で言及したいときは ``{{body}}`` とエスケープすること。
    """
    lines = [
        blocks["intro"],
        "",
        blocks["editorial_stance"],
        "",
        blocks["article_type"],
        "",
        blocks["intent"],
        "",
        blocks["technical"],
        "",
        blocks["time_axes"],
        "",
        blocks["i_infra"],
        "",
        blocks["subject_actor"],
        "候補 (記事に登場した既知アクター): {candidates}",
        "",
        blocks["named_primary_actor"],
        "",
        blocks["subject_rationale"],
        "",
        "# 出力 (JSON のみ、前置き禁止)",
        '{{"editorial_stance":"...","article_type":"...","intent":"...","confidence":"low",',
        '"rationale":null,"technical":null,"event_date":null,"event_date_basis":null,',
        '"compromise_date":null,"i_infra":false,"subject_actor_id":"","subject_confidence":"low",',
        '"named_primary_actor":"","subject_rationale":""}}',
        "",
        "# 記事",
        "カテゴリ: {category}",
        "報道日: {published}",
        "タイトル: {title}",
        "本文:",
        "{body}",
        "",
    ]
    template = "\n".join(lines)
    return template.format(**context)


def _composed_prompt(context: Mapping[str, str]) -> str | None:
    """DB 編集層があればそれで合成する。flag OFF / 編集層不在 / 合成失敗は None (= legacy へ)。"""
    from src.prompts.prompt_store import build_python_block_source
    from src.prompts.registry import get_spec

    spec = get_spec(_PROMPT_ID)
    if spec is None:
        return None
    return build_python_block_source(spec, context)


def _build_prompt(context: Mapping[str, str]) -> str:
    """判断プロンプトを組み立てる (DB 編集層 → legacy の順で fail-safe に解決)。

    graceful degradation (本 module の契約) を下層の例外処理に依存させない — 合成経路が
    どこで raise しても legacy (純 Python・I/O なし) に落ちる構造保証。per-article の
    hot path で prompt 組み立てを新しい crash 点にしないための関門 (ioc_llm_verifier と同型)。
    """
    try:
        composed = _composed_prompt(context)
    except Exception as e:  # noqa: BLE001 — 合成失敗は契約上 legacy へ
        _log.warning("judgment_prompt_compose_failed", error=str(e))
        composed = None
    return composed if composed is not None else _build_prompt_legacy(context)


def _build_prompt_legacy(context: Mapping[str, str]) -> str:
    """rollback 用の frozen 実装 (``JUDGMENT_COMPOSER=0`` の実体)。"""
    return _PROMPT_TEMPLATE_LEGACY.format(**context)


def build_judgment_prompt(
    *,
    title: str,
    category: str | None,
    body: str | None,
    published: str | None,
    candidates: list[ActorAlias],
) -> str:
    cand = ", ".join(c.id for c in candidates) or "(候補なし — subject_actor_id は空)"
    context: dict[str, str] = {
        "candidates": cand,
        "category": (category or "").strip() or "?",
        "published": (published or "?"),
        "title": (title or "").strip(),
        "body": (body or "").strip()[:_BODY_MAX_CHARS],
    }
    return _build_prompt(context)


async def classify_judgment(
    llm: LLMClient,
    *,
    title: str,
    category: str | None,
    body: str | None,
    published: str | None,
    candidates: list[ActorAlias],
    summary_text: str | None = None,
) -> JudgmentOut | None:
    """記事 1 本の判断軸を統合 focused で判定する。障害時は None (安全側=空を維持)。

    subject_actor_id は候補 (本文言及集合) に無い値なら空に矯正する — 構造ゲート
    (LLM は候補外を主題にできない = 誤帰属を構造的に防ぐ)。

    ``summary_text``: 本文が空/極短 (200 字未満) のときの代替入力。旧 analysis_axes
    分類器が持っていた fallback で、07-26 の統合時に喪失していた (監査 2026-08-01 ②:
    空 body で即 None → summarizer 由来の枯死 diamond を維持 = intent NULL)。
    """
    _body = (body or "").strip()
    _summary = (summary_text or "").strip()
    if len(_body) < _SHORT_BODY_CHARS and len(_summary) > len(_body):
        _body = _summary
    if not _body:
        return None
    prompt = build_judgment_prompt(
        title=title, category=category, body=_body, published=published, candidates=candidates
    )
    try:
        out = await llm.generate_structured(prompt, schema=JudgmentOut, think=False)
    except Exception as e:  # noqa: BLE001 — 分類失敗で記事処理を止めない
        _log.warning("judgment_classify_failed", error=str(e)[:80])
        return None
    # 防御正規化 (下流の parse も効くが、ここで最終値を確定)
    if out.editorial_stance not in _STANCES:
        out = out.model_copy(update={"editorial_stance": "unknown"})
    if out.article_type not in _ARTICLE_TYPES:
        out = out.model_copy(update={"article_type": "breaking"})
    if out.confidence not in _CONF:
        out = out.model_copy(update={"confidence": "low"})
    valid_ids = {c.id for c in candidates}
    if out.subject_actor_id and out.subject_actor_id not in valid_ids:
        out = out.model_copy(update={"subject_actor_id": "", "subject_confidence": "low"})
    # named_primary_actor は **ゲートしない** (辞書外の新顔を発見する信号)。ただし LLM の
    # 常套句 (unknown/N/A 等) と防御側機関の誤記入は除去して harvest 汚染を防ぐ。
    cleaned_named = _sanitize_named_actor(out.named_primary_actor)
    if cleaned_named != out.named_primary_actor:
        out = out.model_copy(update={"named_primary_actor": cleaned_named})
    # 根拠文は表示用の短文に制限 (暴走出力の防御。UI は 1〜2 文想定)
    rationale = " ".join(out.subject_rationale.split())[:_RATIONALE_MAX_CHARS]
    if rationale != out.subject_rationale:
        out = out.model_copy(update={"subject_rationale": rationale})
    return out


# LLM が「該当なし」に使う常套句 (アクター名ではない)。harvest 前に落とす。
_NAMED_ACTOR_NOISE: frozenset[str] = frozenset(
    {
        "unknown",
        "n/a",
        "na",
        "none",
        "null",
        "various",
        "multiple",
        "unattributed",
        "unidentified",
        "unclear",
        "several",
        "-",
    }
)
# 防御側・報告機関 (プロンプトで除外指示済だが二重防御)。攻撃主体でないため harvest しない。
_DEFENDER_ORG_MARKERS: tuple[str, ...] = (
    "cisa",
    "fbi",
    "nsa",
    "cyber command",
    "cybercom",
    "cert",
    "us-cert",
    "ncsc",
    "mandiant",
    "microsoft",
    "google",
    "researcher",
    "vendor",
)
_NAMED_ACTOR_MAX_LEN = 60


def _sanitize_named_actor(raw: str) -> str:
    """ungated named_primary_actor を整形 (常套句・防御側機関を除去、長さ上限)。"""
    name = (raw or "").strip().strip("\"'.,;:()[]{}").strip()
    if not name or len(name) > _NAMED_ACTOR_MAX_LEN:
        return ""
    low = name.lower()
    if low in _NAMED_ACTOR_NOISE:
        return ""
    if any(marker in low for marker in _DEFENDER_ORG_MARKERS):
        return ""
    return name
