"""LLM-assisted PIR compiler: description (Level 1) → structured (Level 2).

user が自然文で書いた PIR description を、structured fields (keywords / actors /
sectors / countries / feed_titles / routing / spotlight) に変換する。

context として既知 actor / sector / country / channel list を prompt に注入し、
LLM が hallucination せず既知 vocabulary から選択するよう制約する。

返却は CompileResult (Pir + confidence + rationale)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.pir.models import (
    DerivedSignal,
    LlmJudgeConfig,
    Pir,
    PirMetadata,
    RoutingImportance,
    SpotlightConfig,
    SpotlightWindow,
    StrongSignals,
)
from src.pir.signal_match import LEAF_OPS, tree_uses_only_keywords, validate_match_tree
from src.tools.llm_client import LLMClient

_CONFIG_DIR = Path("config")

# B2: PIR が条件化できる派生 signal の既知語彙 (DerivedSignal Literal から導出)。
_KNOWN_SIGNALS: frozenset[str] = frozenset(get_args(DerivedSignal))

ConfidenceLevel = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class CompileResult:
    """LLM compile 結果。pir は structured fields 込みの Pir、confidence と rationale 付き。"""

    pir: Pir
    confidence: dict[str, ConfidenceLevel]
    rationale: str
    warnings: list[str]


# ----- LLM 出力 schema (Pydantic、generate_structured 用) -----


class _CompiledSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    # アクター国籍 (加害側)。countries (被害/標的国) と意味を分離 (2026-07-05)
    actor_nations: list[str] = Field(default_factory=list)
    feed_titles: list[str] = Field(default_factory=list)
    # B2: 派生 signal 条件 (既知語彙のみ。未知は compile_pir で除去)
    signals: list[str] = Field(default_factory=list)


class _CompiledRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_importance: RoutingImportance = "auto"


class _CompiledSpotlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    title: str = ""
    window: SpotlightWindow = "weekly"


class _CompiledConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: ConfidenceLevel = "low"
    actors: ConfidenceLevel = "low"
    sectors: ConfidenceLevel = "low"
    countries: ConfidenceLevel = "low"
    actor_nations: ConfidenceLevel = "low"
    feed_titles: ConfidenceLevel = "low"


class _MatchCondition(BaseModel):
    """DNF の 1 条件 (AND 項)。negate=true で NOT。

    LLM に再帰ツリーを書かせない (authoring 統一 §2-4): flat な条件列を出させ、
    ``dnf_to_tree`` が決定論で樹形化する。
    """

    model_config = ConfigDict(extra="forbid")

    property: str = ""
    op: str = ""
    value: list[str] = Field(default_factory=list)
    negate: bool = False


class _MatchBranch(BaseModel):
    """DNF の 1 枝 (OR 項) = 条件の AND 集合。"""

    model_config = ConfigDict(extra="forbid")

    conditions: list[_MatchCondition] = Field(default_factory=list)


class _CompilerOutput(BaseModel):
    """LLM 出力の JSON schema。"""

    model_config = ConfigDict(extra="forbid")

    strong_signals: _CompiledSignals = Field(default_factory=_CompiledSignals)
    # 照合条件 (DNF: branches=OR、conditions=AND)。空なら match ツリーなし。
    match_branches: list[_MatchBranch] = Field(default_factory=list)
    # 概念的 intent (言及≠主題リスク) のとき true → llm_judge を有効化する提案。
    needs_subject_judge: bool = False
    judge_question: str = ""
    routing: _CompiledRouting = Field(default_factory=_CompiledRouting)
    spotlight: _CompiledSpotlight = Field(default_factory=_CompiledSpotlight)
    confidence: _CompiledConfidence = Field(default_factory=_CompiledConfidence)
    rationale: str = ""


# ----- 既知 vocabulary 取得 -----


def _load_known_actors(config_dir: Path = _CONFIG_DIR) -> list[str]:
    """actor_aliases.yaml から canonical actor name list を抽出。

    schema は ``actors:`` の下に {id: {canonical: "...", aliases: [...], ...}} 形式。
    """
    path = config_dir / "cti/actor_aliases.yaml"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    actors: list[str] = []
    actor_block = data.get("actors") if isinstance(data, dict) else None
    if isinstance(actor_block, list):
        # 通常 schema: list of {id, canonical, aliases, ...}
        for entry in actor_block:
            if isinstance(entry, dict):
                canonical = entry.get("canonical")
                if isinstance(canonical, str):
                    actors.append(canonical)
    elif isinstance(actor_block, dict):
        # legacy schema: {id: {canonical: "..."}}
        for entry in actor_block.values():
            if isinstance(entry, dict):
                canonical = entry.get("canonical")
                if isinstance(canonical, str):
                    actors.append(canonical)
    return actors[:200]


def _load_known_sectors(config_dir: Path = _CONFIG_DIR) -> list[str]:
    """victim_sectors.yaml から canonical sector list を抽出。

    schema は ``canonical:`` の下に {sector_id: {display, aliases, ...}}。
    """
    path = config_dir / "cti/victim_sectors.yaml"
    fallback = ["defense", "government", "finance", "energy", "telecom", "healthcare"]
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sectors: list[str] = []
    canon_block = data.get("canonical") if isinstance(data, dict) else None
    if isinstance(canon_block, dict):
        for sector_id in canon_block:
            if isinstance(sector_id, str):
                sectors.append(sector_id)
    return sectors or fallback


_KNOWN_IMPORTANCE: list[str] = ["high", "medium", "low", "auto"]
_KNOWN_SPOTLIGHT_WINDOWS: list[str] = ["daily", "weekly", "monthly"]

# eq を許す property で value が 1 件のときは eq + str に簡約する (deploy 済みツリーの流儀)。
_EQ_CAPABLE: frozenset[str] = frozenset({"category", "intent", "victim_country", "victim_sector"})


# ----- DNF → 条件ツリー builder (決定論) -----


def _condition_to_leaf(cond: _MatchCondition) -> tuple[dict[str, object] | None, str | None]:
    """1 条件を leaf ノードに変換。返り値 = (leaf or None, warning or None)。"""
    prop = cond.property.strip()
    op = cond.op.strip()
    allowed = LEAF_OPS.get(prop)
    if allowed is None:
        return None, f"未知の property を除去: {prop!r}"
    if op not in allowed:
        # LLM が op を誤っても property から一意に回復できることが多い (許可 op が 1 種の場合)
        if len(allowed) == 1:
            op = next(iter(allowed))
        else:
            return None, f"property '{prop}' に使えない op を除去: {cond.op!r}"
    values = [v.strip() for v in cond.value if v and v.strip()]
    leaf: dict[str, object]
    if op == "is_true":
        leaf = {"property": prop, "op": "is_true"}
    elif not values:
        return None, f"value 欠落の条件を除去: {prop}"
    elif op in ("eq", "in") and prop in _EQ_CAPABLE and len(values) == 1:
        leaf = {"property": prop, "op": "eq", "value": values[0].lower()}
    elif op in ("eq", "in"):
        leaf = {"property": prop, "op": "in", "value": [v.lower() for v in values]}
    else:  # keyword_any / contains_any / any_of — 表記をそのまま保持 (照合側が正規化)
        leaf = {"property": prop, "op": op, "value": values}
    if cond.negate:
        return {"not": leaf}, None
    return leaf, None


def dnf_to_tree(
    branches: list[_MatchBranch],
) -> tuple[dict[str, object] | None, list[str]]:
    """DNF (OR of AND-groups) を条件ツリーに樹形化する (単一要素は簡約)。

    返り値 = (tree or None, warnings)。全枝が空になったら None (match なし)。
    """
    warnings: list[str] = []
    built: list[dict[str, object]] = []
    for i, branch in enumerate(branches):
        leaves: list[dict[str, object]] = []
        for cond in branch.conditions:
            leaf, warn = _condition_to_leaf(cond)
            if warn:
                warnings.append(f"枝{i + 1}: {warn}")
            if leaf is not None:
                leaves.append(leaf)
        if not leaves:
            warnings.append(f"枝{i + 1}: 有効な条件が無く枝ごと除去")
            continue
        built.append(leaves[0] if len(leaves) == 1 else {"all": leaves})
    if not built:
        return None, warnings
    tree: dict[str, object] = built[0] if len(built) == 1 else {"any": built}
    errors, val_warnings = validate_match_tree(tree)
    warnings.extend(val_warnings)
    if errors:
        # builder 経由で構造エラーは通常起きない (防御)。壊れたツリーは返さない。
        warnings.extend(f"照合条件を破棄 (構造エラー): {e}" for e in errors[:3])
        return None, warnings
    return tree, warnings


# ----- prompt 構築 -----


def _build_compile_prompt(
    *,
    title: str,
    description: str,
    known_actors: list[str],
    known_sectors: list[str],
    known_categories: list[str],
    known_intents: list[str],
) -> str:
    actors_str = ", ".join(known_actors[:60]) if known_actors else "(actor 辞書なし)"
    sectors_str = ", ".join(known_sectors) if known_sectors else "(sector 辞書なし)"
    return f"""\
あなたは CTI 担当者の PIR (Priority Intelligence Requirement) を
machine-readable な structured filter rule に変換する compiler です。
既知 vocabulary から選択し、hallucination を避けてください。

## PIR title
{title}

## PIR description (canonical intent)
{description}

## 既知 actor (上位 60 件、既存 actor_aliases.yaml から)
{actors_str}

## 既知 sector taxonomy
{sectors_str}

## 照合条件 match_branches (最重要 — 実際の記事照合はこれで行われる)
記事の意味プロパティへの述語で「どの記事がこの PIR に該当するか」を表現する。
branches = OR (どれかの枝が成立すれば match)、branch.conditions = AND (枝内は全条件成立)。
condition = {{property, op, value: [文字列...], negate}} (negate=true で NOT)。

利用可能な property と op:
  - category (op: in) — 記事分類。値域: {", ".join(known_categories)}
  - intent (op: in) — 社会政治的意図。値域: {", ".join(known_intents)}
  - victim_country (op: in) — 被害/標的国 ISO alpha-2 小文字 (例: jp, us)
  - victim_sector (op: in) — 被害セクター (上の sector taxonomy から)
  - is_ransomware (op: is_true) — ランサムウェア事案フラグ
  - feed_title (op: contains_any) — フィード名の部分一致 (例: cisa, jpcert)
  - text (op: keyword_any) — 全文の語一致 (title+summary+body)
  - title (op: keyword_any) — タイトルのみの語一致 (主題の近似)
  - actor (op: any_of) — 既知 actor 名 (辞書照合 + 主題判定つき)
  - actor_nation (op: in) — 加害アクターの国籍 ISO alpha-2 小文字 (例: cn, kp, ru)

設計ガイド (重要):
  a. **意味プロパティ (category/intent/victim_*/is_ransomware/actor) を優先**する。
     生キーワードは (1) log4j や i-Soon のような固有名詞、(2) 意味プロパティと AND する
     弱い補強、のどちらかに限る。多義語 (脆弱性/制裁/leak 等) を text 単独で使わない。
     **キーワードは日本語と英語の両方を併記する** (コーパスは日英混在。例:
     ["内部漏洩", "リーク", "leak"]。英語だけのキーワードは日本語記事に当たらない)。
  b. サイバー PIR で地政学 (軍事/外交) 記事の混入を避けたいときは
     {{property: category, op: in, value: [geopolitical], negate: true}} を AND する。
  c. 「タイトルに載る語 = 主題」を使いたいときは text でなく title スコープ。
  d. アクター名は text キーワードでなく actor property (主題判定つき) を使う。
  e. 「〜が主題か」の概念判定 (統合作戦/帰属の公表/実態の暴露 など、語で表現できない
     関心事) が必要なら needs_subject_judge=true とし judge_question に判定基準を書く。
     このとき match_branches は「候補を広めに拾うゲート」でよい。

## 利用可能な target_importance
{", ".join(_KNOWN_IMPORTANCE)}
  - "auto": triage LLM 判定に委ねる (default、互換性維持)

## 利用可能な派生 signal (strong_signals.signals、任意)
keyword では表現できない高精度な判定済みフラグ。該当する意図が description にあるときのみ選択:
  - "kev": CISA KEV 掲載 / 実環境で悪用中
  - "zero_day": 0day
  - "japan_critical": 日本の重要インフラ標的
  - "japan_relevant": 日本関連の active threat
  - "known_apt": 既知 APT への言及
  - "apt_leak": APT 組織の内部リーク
  - "security_relevant": 広義のセキュリティ関連
  これら以外の値は出力禁止。description に明示的な意図が無ければ空 list。

## 出力ルール (重要)
0. match_branches が主出力。description の関心事を上の property 述語で表現する
   (設計ガイド a-e 遵守)。strong_signals は補助メタデータ (脅威ページ連携・旧方式の予備)。
1. strong_signals は description から「具体的に何にマッチさせたいか」を抽出。
   - keywords: description で重要視されている語彙 (例: "ransomware", "0day", "supply chain")
   - actors: 既知 actor 辞書から該当を選択。**辞書にない名前は出力禁止** (hallucination 防止)
   - sectors: 既知 sector taxonomy から該当を選択
   - countries: **被害/標的国** の ISO 3166-1 alpha-2 (例: 「日本標的の攻撃」→ ["JP"])。
     被害国の意味でのみ使う。
   - actor_nations: **加害側アクターの国籍** の ISO 3166-1 alpha-2 (例: 「中国系 APT の
     動向」→ ["CN"])。国家系アクターの帰属国で拾いたい場合はこちら (countries と混同禁止:
     「中国系 APT」の CN を countries に入れると中国=被害者の記事が match してしまう)。
   - feed_titles: substring match される文字列 (例: "JPCERT", "CISA")
   - signals: 上記「利用可能な派生 signal」から、description が示す高精度判定のみ選択 (例:
     "0day を即応" → ["kev", "zero_day"])
2. routing:
   - target_importance="auto" を default。description で「最優先」「critical」を強く示す
     場合のみ "high"。
   - 配信チャンネルは PIR では指定しない (チャンネル決定は routing rules が担う)。
3. spotlight.enabled=false を default。description で明示的に「Spotlight にしたい」
   「常時 dashboard 表示」と書かれている場合のみ true。
4. confidence:
   - 各 field について high/medium/low で確信度を出す。
   - 既知 vocabulary から直接選択できたら high、推測なら medium、不明なら low。
5. rationale: なぜこの structured を選んだか、簡潔に (50-150 字)。
"""


# ----- 主 compile entry point -----


async def compile_pir(
    *,
    pir_id: str,
    title: str,
    description: str,
    enabled: bool = True,
    llm: LLMClient,
    config_dir: Path = _CONFIG_DIR,
) -> CompileResult:
    """description を構造化 PIR に compile する。

    Args:
        pir_id: PIR の ID (新規作成時は user 指定 or auto-generate)
        title: PIR title
        description: 自然文 description (LLM への入力)
        enabled: 初期 enabled 状態
        llm: Ollama LLM クライアント
        config_dir: actor_aliases.yaml / victim_sectors.yaml の探索先

    Returns:
        CompileResult (Pir + confidence + rationale + warnings)
    """
    known_actors = _load_known_actors(config_dir)
    known_sectors = _load_known_sectors(config_dir)
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS

    prompt = _build_compile_prompt(
        title=title,
        description=description,
        known_actors=known_actors,
        known_sectors=known_sectors,
        known_categories=sorted(KNOWN_ARTICLE_CATEGORIES),
        known_intents=sorted(SOCIO_POLITICAL_INTENTS - {"unknown"}),
    )

    output = await llm.generate_structured(
        prompt=prompt,
        schema=_CompilerOutput,
        temperature=0.2,
        max_tokens=2048,
        think=False,
    )

    # warnings: LLM が hallucination した actor 等を flag
    warnings: list[str] = []
    known_actors_lower = {a.lower() for a in known_actors}
    unknown_actors = [
        a for a in output.strong_signals.actors if a.lower() not in known_actors_lower
    ]
    if unknown_actors:
        warnings.append(
            f"既知 actor 辞書にない名前: {unknown_actors[:3]}"
            f"{'...' if len(unknown_actors) > 3 else ''}",
        )

    known_sectors_lower = {s.lower() for s in known_sectors}
    unknown_sectors = [
        s for s in output.strong_signals.sectors if s.lower() not in known_sectors_lower
    ]
    if unknown_sectors:
        warnings.append(
            f"既知 sector taxonomy にない名前: {unknown_sectors[:3]}",
        )

    # B2: 派生 signal は既知語彙のみ採用 (LLM hallucination を除去し ValidationError を防ぐ)。
    ss_dump = output.strong_signals.model_dump()
    raw_signals = ss_dump.get("signals", []) or []
    ss_dump["signals"] = [s for s in raw_signals if s in _KNOWN_SIGNALS]
    dropped_signals = [s for s in raw_signals if s not in _KNOWN_SIGNALS]
    if dropped_signals:
        warnings.append(f"未知の派生 signal を除去: {dropped_signals[:3]}")

    # 照合条件 (DNF → ツリー、authoring 統一 §3.3)。ツリーを作れなかったら match=None
    # (フロントは既存 PIR の match を保全するため、silent 退行しない)。
    match_tree, tree_warnings = dnf_to_tree(output.match_branches)
    warnings.extend(tree_warnings)
    if match_tree is None:
        warnings.append("照合条件を生成できませんでした (旧 strong_signals 照合のまま)")
    elif tree_uses_only_keywords(match_tree) and not output.needs_subject_judge:
        warnings.append(
            "照合条件がキーワードのみです — 固有名詞以外は意味プロパティとの AND か"
            "主題判定 (needs_subject_judge) を推奨"
        )
    llm_judge = LlmJudgeConfig(
        enabled=bool(output.needs_subject_judge and match_tree is not None),
        question=(output.judge_question or "").strip() if output.needs_subject_judge else "",
    )
    if llm_judge.enabled:
        warnings.append(
            "LLM 主題判定を有効化しました — 判定は夜間バッチ後に反映されます "
            "(preview は候補ゲートのみ)"
        )

    pir = Pir(
        id=pir_id,
        title=title,
        description=description,
        enabled=enabled,
        strong_signals=StrongSignals(**ss_dump),
        match=match_tree,
        llm_judge=llm_judge,
        target_importance=output.routing.target_importance,
        spotlight=SpotlightConfig(**output.spotlight.model_dump()),
        metadata=PirMetadata(
            rationale=output.rationale,
        ),
    )

    return CompileResult(
        pir=pir,
        confidence=output.confidence.model_dump(),
        rationale=output.rationale,
        warnings=warnings,
    )
