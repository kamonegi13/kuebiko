#!/usr/bin/env python3
"""プロンプト切替 (.j2 直読み → DB からの判定基準合成) の合否を実データで判定する CLI。

等価性の**静的**検証は ``scripts/verify_summarizer_rubric.py`` (行 diff の分類) が担う。
本 script はその先 — **切替後に実際の LLM 出力が劣化していないか**を、切替時刻の前後で
同じ幅の窓を取って比較する。「1 か月様子を見る」という曖昧な運用を、1 コマンドで
PASS / FAIL / INCONCLUSIVE が出る形にするためのもの。

Usage::

    uv run python scripts/verify_prompt_cutover.py --cutover 2026-08-16T07:01:26Z
    uv run python scripts/verify_prompt_cutover.py --cutover 2026-08-16T07:01:26Z --days 14
    uv run python scripts/verify_prompt_cutover.py --cutover 2026-08-16T07:01:26Z --json

exit code: 0=PASS / 1=FAIL / 2=INCONCLUSIVE (標本不足 / 窓が混成で判定不能)。

**窓の純度も検査する**。切替後の窓の中で判定基準がさらに版を重ねていたら「切替後」は
単一の状態ではなく、その数字は名指しした切替に帰属できない。版履歴 (config_store) を
読んで混成を検出し、判定を出さない (--allow-mixed で承知の上の続行)。

**summarizer 専用ではない**。今後 12 本の他プロンプトを同じ方式に切り替えるたび、
``--cutover`` と ``--days`` を変えて同じ道具を使う。集計は
``src/ui/services/rubric_field_stats.build_field_stats`` を 2 回呼ぶだけで、
判定基準の分母定義・充足条件をここに複製しない (複製したら SSoT が割れる)。

出力に記事本文・タイトル・プロンプト本文は含めない (フィールド ID と語彙 enum のみ)。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.storage.config_store import ConfigVersion, list_history  # noqa: E402
from src.ui.services.rubric_field_stats import (  # noqa: E402
    MAX_DAYS,
    MIN_DAYS,
    build_field_stats,
)

# ---------- 判定ゲート (閾値はすべてここに定数として置き、根拠をコメントに残す) ----------

# 切替後の窓の配信済み記事数がこれ未満なら PASS でも FAIL でもなく **INCONCLUSIVE**。
# 定常運用の配信量は 1 日あたり数十件で、300 件は「複数日ぶんの全量が揃っている」水準。
# これ未満の標本で出した差は日次のゆらぎと区別できない。「まだ様子見」を曖昧な
# 口約束ではなく **exit 2 という明示的な状態**にするためのゲート。
MIN_POST_ARTICLES = 300

# 切替で削除した 2 行 (「JSON のみ出力してください」/ title_ja の強調文) が直接狙って
# いたフィールド。影響が出るならまずここに出る。
#
# 閾値は**実測で較正**した (切替前の履歴で「本来 PASS すべき」比較を 4 週分実行):
#   summary 充足率 : 4 週すべて ±0.00 pt  → 1.0 pt で十分な余裕
#   title_ja 充足率: 通常 ±0.15 pt、ただし 08-02 の週だけ -1.23 pt の自然変動
# title_ja だけ 1.0 pt では誤 FAIL するため 2.0 pt に広げる。実際の遵守率低下
# (LLM が出力を落とす) なら数十ポイント動くので、2.0 pt でも検出力は落ちない。
EXPLICIT_COVERAGE_DROP_PT: dict[str, float] = {"title_ja": 2.0, "summary": 1.0}
EXPLICIT_COVERAGE_GATES: tuple[str, ...] = ("title_ja", "summary")

# 明示ゲート以外の availability=full フィールドの一括ゲート。
#
# ⚠ **緩やかなドリフトの監視はこのスクリプトの仕事ではない**。
# entity 系・日付系 (compromise_date / event_date / victim_orgs / victim_country /
# remediation) は「その週に何のニュースがあったか」で週次 7〜13 pt 揺れる。実測で
# 4 週中 3 週が no-op 比較で FAIL した = 5.0 pt はノイズより狭い。狼少年になる
# ゲートは無いより悪い (無視する習慣がつく)。
#
# 遅いドリフトには **既に専用の道具がある** — weekly-fill-rate-audit が前週 vs
# 過去 4 週中央値で判定しており、単発の 2 窓比較より遥かに適切な基準線を持つ。
# こちらを劣化コピーで再実装しない。本ゲートは「フィールドが丸ごと死んだ」級の
# 破局だけを捕まえる網として 15 pt に置く。
GENERIC_COVERAGE_DROP_PT = 15.0

# 判定傾向そのものの変質を見るゲート。各値の share の絶対差 (ポイント)。
#
# ⚠ **分布はプロンプト以外の理由でも動く**。実測 (切替前の履歴、7 日窓):
#   category geopolitical: 51.7% → 45.7% → 33.2% → 29.2% と週を追って下降。
#   これは収集ソース構成の変化による構造的トレンドで、判定基準とは無関係。
#   週次の変化幅は最大 13.5 pt に達する。
# 一方、本物の破局は桁が違う: article_type「未設定」が分類器の投入で **-94.9 pt**。
# 20 pt に置けば自然変動 (最大 13.5 pt) を通し、破局を確実に捕まえる。
# 実測 4 週の no-op 比較で誤検出 0 / 真の検出 1 (08-02 の分類器投入) を確認済み。
DISTRIBUTION_SHIFT_PT = 20.0
DISTRIBUTION_FIELDS: tuple[str, ...] = ("importance", "category", "article_type")

# 平均要約長の変化率。字数指定は判定基準側に残っているので、15% を超える変化は
# 契約散文の削除が文章量に効いたことを意味する (増減どちらも異常)。
AVG_LENGTH_CHANGE_RATIO = 0.15
AVG_LENGTH_FIELDS: tuple[str, ...] = ("summary", "remediation")
AVG_LENGTH_GATE_FIELDS: tuple[str, ...] = ("summary",)

# coverage の scope 分母がこれ未満のフィールドは 1 件の増減で数十ポイント動くため
# 判定から外して参考表示にする (誤 FAIL 防止)。scope 付きフィールド (vulnerability
# 限定の remediation など) は短い窓だと簡単にこの水準を割る。
MIN_SCOPE_ARTICLES = 30

# ---------- 窓の純度 (窓の中で判定基準がさらに動いていないか) ----------

# 切替後の窓の中で判定基準が版を重ねていたら、「切替後」は **単一の状態ではない**。
# その窓の数字は複数の版の混成で、名指しした切替には帰属できない。
#
# 実例 (2026-08-19 に判明): 08-16 の層分け (v1) を既定の 7 日窓で判定しようとすると、
# 窓の中に v3 (08-17) と v4 (08-18、判定基準 30% 削減) が入る。運用メモに「窓が
# 汚れているので注意」と書いても、コマンドは平然と PASS を返す。**禁止は指示では
# 止まらない (2026-08-19)** ので、判定を出さない関門としてコードに置く。
#
# ⚠ 検出できるのは **DB に版が残る判定基準の変更だけ**。コード側の変更 (schema の
# required 化・後処理 normalizer・モデル切替) はここからは見えない。「混成なし」は
# 「判定基準が動いていない」以上を意味しない。
DEFAULT_CONFIG_KEY = "summarizer_rubric"

# 版履歴の取得件数。1 プロンプトの版が 200 を超えることは運用上ありえず、
# 超えたとしても古い側は窓の外にある (窓は最大 MAX_DAYS=90 日)。
VERSION_HISTORY_LIMIT = 200

NOTE_VERSION_UNCHECKED = (
    "判定基準の版履歴を確認していない — 窓の中で基準が動いていないことを保証できない。"
)


# ---------- 判定結果の語彙 ----------

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_INCONCLUSIVE = "INCONCLUSIVE"
_EXIT_CODES = {STATUS_PASS: 0, STATUS_FAIL: 1, STATUS_INCONCLUSIVE: 2}

# C7 無人 rollback の作動条件 (破局判定) の説明。週次ガバナンスがこの行頭語を
# パースして auto_rollback に渡す — 行頭語 "破局判定:" は契約であり変更しない。
CATASTROPHIC_LINE_PREFIX = "破局判定:"
_CATASTROPHIC_NOTE = "無人 rollback の作動条件 — 分布シフト >20pt / 全滅級低下 >15pt のみ"

ROLE_GATE = "判定"
ROLE_REFERENCE = "参考"
ROLE_EXCLUDED = "対象外"
_NO_VERDICT = "—"
# ゲート行なのに verdict が無い = 標本不足で判定を保留した行。「判定」とだけ出すと
# 判定済みに見えるので、保留であることを列に明示する。
_SUSPENDED = "保留"

NOTE_AVAILABILITY_NONE = (
    "統計なし (DB に列が無い)。0% ではなく『測れない』ため比較対象から外します。"
)
NOTE_PARTIAL = (
    "availability=partial (代理指標)。代理指標の変動は基準の変化と切り分けられないため"
    "判定に使いません。"
)
NOTE_TITLE_JA = (
    "代理指標 (partial) だが、削除した強調文の直接の標的なので明示ゲートとして判定します。"
)
NOTE_SUMMARY = "削除した「JSON のみ出力してください」行の影響が最初に出るフィールド。"
_EXPLICIT_NOTES = {"title_ja": NOTE_TITLE_JA, "summary": NOTE_SUMMARY}


# ---------- 結果モデル ----------


@dataclass(frozen=True)
class MetricRow:
    """比較表の 1 行。``role`` が判定に使ったかどうかを表す (対象外/参考は verdict なし)。"""

    field_id: str
    metric: str  # coverage / share / average
    label: str
    pre: float | None
    post: float | None
    delta: float | None
    unit: str  # % / 字
    role: str
    verdict: str
    note: str = ""


@dataclass(frozen=True)
class CutoverVerdict:
    """1 回の比較の総合結果。``rows`` は人が読む表と JSON の両方の素材。"""

    status: str
    exit_code: int
    pre_articles: int
    post_articles: int
    min_articles: int
    rows: tuple[MetricRow, ...]
    failures: tuple[str, ...]
    reasons: tuple[str, ...]
    excluded_fields: tuple[str, ...]
    partial_fields: tuple[str, ...]
    # 窓の純度。``version_checked=False`` は「検査していない/できなかった」で、
    # 「混成なし」とは別物 (0 件と『測れない』を区別する — 本 script 全体の作法)。
    version_checked: bool = True
    config_key: str = ""
    pre_changes: tuple[ConfigVersion, ...] = field(default_factory=tuple)
    post_changes: tuple[ConfigVersion, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------- stats dict の読み取り (欠損に強く) ----------


def _stat_fields(stats: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = stats.get("fields") or {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def _posted_count(stats: dict[str, Any]) -> int:
    denominator = stats.get("denominator") or {}
    return int(denominator.get("count") or 0)


def _availability(stat: dict[str, Any]) -> str:
    return str(stat.get("availability") or "none")


def _coverage(stat: dict[str, Any]) -> tuple[float, int] | None:
    """(充足率 [ポイント], scope 分母)。coverage を持たないフィールドは None。"""
    coverage = stat.get("coverage")
    if not isinstance(coverage, dict):
        return None
    return (float(coverage.get("rate") or 0.0) * 100.0, int(coverage.get("total") or 0))


def _shares(stat: dict[str, Any]) -> dict[str, float]:
    """値 → share [ポイント]。空文字は「未設定」バケット (これも判定対象)。"""
    out: dict[str, float] = {}
    for bucket in stat.get("distribution") or []:
        if isinstance(bucket, dict):
            out[str(bucket.get("value") or "")] = float(bucket.get("share") or 0.0) * 100.0
    return out


def _average_value(stat: dict[str, Any]) -> float | None:
    average = stat.get("average")
    if not isinstance(average, dict) or average.get("value") is None:
        return None
    return float(average["value"])


def _has_none_availability(pre_stat: dict[str, Any], post_stat: dict[str, Any]) -> bool:
    return "none" in (_availability(pre_stat), _availability(post_stat))


# ---------- 行の組み立て ----------


def _coverage_row(
    field_id: str,
    pre_stat: dict[str, Any],
    post_stat: dict[str, Any],
    *,
    judged: bool,
) -> MetricRow | None:
    """充足率 1 行。統計なしは「除外」行、代理指標と極小 scope は「参考」行にする。"""
    if _has_none_availability(pre_stat, post_stat):
        return MetricRow(
            field_id=field_id,
            metric="coverage",
            label="充足率",
            pre=None,
            post=None,
            delta=None,
            unit="%",
            role=ROLE_EXCLUDED,
            verdict=_NO_VERDICT,
            note=NOTE_AVAILABILITY_NONE,
        )
    pre_cov = _coverage(pre_stat)
    post_cov = _coverage(post_stat)
    if pre_cov is None or post_cov is None:
        return None  # coverage を持たない (分布のみの) フィールド
    pre_rate, pre_total = pre_cov
    post_rate, post_total = post_cov
    delta = round(post_rate - pre_rate, 2)

    role = ROLE_GATE
    note = ""
    threshold: float | None = None
    if field_id in EXPLICIT_COVERAGE_GATES:
        threshold = EXPLICIT_COVERAGE_DROP_PT[field_id]
        note = _EXPLICIT_NOTES.get(field_id, "")
    elif _availability(pre_stat) == "full" and _availability(post_stat) == "full":
        threshold = GENERIC_COVERAGE_DROP_PT
    else:
        role, note = ROLE_REFERENCE, NOTE_PARTIAL
    if threshold is not None and min(pre_total, post_total) < MIN_SCOPE_ARTICLES:
        role = ROLE_REFERENCE
        threshold = None
        note = f"scope 分母が {MIN_SCOPE_ARTICLES} 件未満のため判定に使いません。"

    verdict = _NO_VERDICT
    if role == ROLE_GATE and judged and threshold is not None:
        verdict = STATUS_FAIL if delta < -threshold else STATUS_PASS
    return MetricRow(
        field_id=field_id,
        metric="coverage",
        label="充足率",
        pre=round(pre_rate, 2),
        post=round(post_rate, 2),
        delta=delta,
        unit="%",
        role=role,
        verdict=verdict,
        note=note,
    )


def _distribution_rows(
    field_id: str,
    pre_stat: dict[str, Any],
    post_stat: dict[str, Any],
    *,
    judged: bool,
) -> tuple[MetricRow, ...]:
    """分布 (各値の share) の行。値の増減どちらも「判定傾向の変質」なので絶対差で見る。"""
    if _has_none_availability(pre_stat, post_stat):
        return ()
    pre_shares = _shares(pre_stat)
    post_shares = _shares(post_stat)
    if not pre_shares and not post_shares:
        return ()
    is_full = _availability(pre_stat) == "full" and _availability(post_stat) == "full"
    rows: list[MetricRow] = []
    for value in sorted(set(pre_shares) | set(post_shares)):
        pre_share = pre_shares.get(value, 0.0)
        post_share = post_shares.get(value, 0.0)
        delta = round(post_share - pre_share, 2)
        verdict = _NO_VERDICT
        if is_full and judged:
            verdict = STATUS_FAIL if abs(delta) > DISTRIBUTION_SHIFT_PT else STATUS_PASS
        rows.append(
            MetricRow(
                field_id=field_id,
                metric="share",
                label=f"分布 {value or '(未設定)'}",
                pre=round(pre_share, 2),
                post=round(post_share, 2),
                delta=delta,
                unit="%",
                role=ROLE_GATE if is_full else ROLE_REFERENCE,
                verdict=verdict,
                note="" if is_full else NOTE_PARTIAL,
            )
        )
    return tuple(rows)


def _average_row(
    field_id: str,
    pre_stat: dict[str, Any],
    post_stat: dict[str, Any],
    *,
    judged: bool,
) -> MetricRow | None:
    """平均文字数の行。判定に使うのは summary のみ (remediation は参考)。"""
    if field_id not in AVG_LENGTH_FIELDS or _has_none_availability(pre_stat, post_stat):
        return None
    pre_value = _average_value(pre_stat)
    post_value = _average_value(post_stat)
    if pre_value is None or post_value is None or pre_value <= 0:
        if pre_value is None and post_value is None:
            return None
        return MetricRow(
            field_id=field_id,
            metric="average",
            label="平均文字数",
            pre=pre_value,
            post=post_value,
            delta=None,
            unit="字",
            role=ROLE_REFERENCE,
            verdict=_NO_VERDICT,
            note="片側の平均が算出できないため判定しません。",
        )
    ratio = (post_value - pre_value) / pre_value
    is_gate = field_id in AVG_LENGTH_GATE_FIELDS
    note = f"変化率 {ratio * 100:+.1f}% (閾値 ±{AVG_LENGTH_CHANGE_RATIO * 100:.0f}%)"
    verdict = _NO_VERDICT
    if is_gate and judged:
        verdict = STATUS_FAIL if abs(ratio) > AVG_LENGTH_CHANGE_RATIO else STATUS_PASS
    return MetricRow(
        field_id=field_id,
        metric="average",
        label="平均文字数",
        pre=round(pre_value, 1),
        post=round(post_value, 1),
        delta=round(post_value - pre_value, 1),
        unit="字",
        role=ROLE_GATE if is_gate else ROLE_REFERENCE,
        verdict=verdict,
        note=note if is_gate else f"{note} — 参考のみ",
    )


def _failure_text(row: MetricRow) -> str:
    delta = "n/a" if row.delta is None else f"{row.delta:+.2f}"
    return f"{row.field_id} / {row.label}: {delta}{'pt' if row.unit == '%' else row.unit}"


def _availability_groups(
    pre_fields: dict[str, dict[str, Any]], post_fields: dict[str, dict[str, Any]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(統計なしフィールド, 代理指標フィールド)。0% と『測れない』を混同させないため明示する。"""
    excluded: list[str] = []
    partial: list[str] = []
    for field_id in sorted(set(pre_fields) | set(post_fields)):
        levels = {
            _availability(pre_fields.get(field_id, {})),
            _availability(post_fields.get(field_id, {})),
        }
        if "none" in levels:
            excluded.append(field_id)
        elif "partial" in levels:
            partial.append(field_id)
    return (tuple(excluded), tuple(partial))


# ---------- 版の窓内検出 (純関数) ----------


def _parse_created_at(value: str) -> datetime | None:
    """``ConfigVersion.created_at`` (ISO 8601 文字列) を UTC datetime に。解釈できなければ None。

    **秒未満を落とす**。``--cutover`` は人が秒精度で書くのに版の created_at は
    マイクロ秒まで持つため、切り捨てないと「切替そのものの版」が切替の 0.7 ミリ秒後に
    見えて汚染として数えられる (実際に踏んだ)。比較は入力側の精度に合わせる。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.replace(microsecond=0)


def changes_within(
    history: Sequence[ConfigVersion],
    *,
    since: datetime,
    until: datetime,
    include_since: bool,
) -> tuple[ConfigVersion, ...]:
    """窓 ``[since, until)`` に入った版を古い順で返す。

    ``include_since=False`` は **切替後の窓**用 — 切替時刻ちょうどに保存された版は
    「切替そのもの」であって汚染ではないため境界から外す。切替前の窓は
    ``include_since=True`` (窓の左端に版が乗っていれば基準線は混成)。

    created_at を解釈できない行は **落とさず含める** (判定できないものを「無い」に
    倒すと fail-open になる)。
    """
    picked: list[tuple[datetime, ConfigVersion]] = []
    for version in history:
        parsed = _parse_created_at(version.created_at)
        if parsed is None:
            picked.append((since, version))
            continue
        after_start = parsed >= since if include_since else parsed > since
        if after_start and parsed < until:
            picked.append((parsed, version))
    picked.sort(key=lambda item: (item[0], item[1].version))
    return tuple(version for _, version in picked)


def _versions_text(versions: Sequence[ConfigVersion]) -> str:
    """``v3 2026-08-17T21:44Z / v4 2026-08-18T11:07Z``。note は含めない (表に出すのは別枠)。"""
    parts = []
    for version in versions:
        parsed = _parse_created_at(version.created_at)
        stamp = parsed.strftime("%Y-%m-%dT%H:%MZ") if parsed is not None else version.created_at
        parts.append(f"v{version.version} {stamp}")
    return " / ".join(parts)


# ---------- 純粋な判定関数 (DB に触れない = 本 script の検証可能な核) ----------


def evaluate_cutover(
    pre: dict[str, Any],
    post: dict[str, Any],
    *,
    min_articles: int = MIN_POST_ARTICLES,
    config_key: str = "",
    version_checked: bool = False,
    pre_changes: Sequence[ConfigVersion] = (),
    post_changes: Sequence[ConfigVersion] = (),
    allow_mixed: bool = False,
) -> CutoverVerdict:
    """切替前後の field-stats 2 つを突き合わせて PASS / FAIL / INCONCLUSIVE を出す。

    DB にも時刻にも依存しない純粋関数。``pre`` / ``post`` は
    ``build_field_stats`` の戻り値と同じ形の dict。

    窓の純度は呼び手が調べて渡す (``changes_within`` の結果)。**切替後の窓に版が
    入っていれば判定を出さない** — 混成した状態に PASS / FAIL を付けても意味が無い。
    ``version_checked=False`` (検査していない/できなかった) も同じく判定不能とする。
    どちらも ``allow_mixed=True`` で警告に落とせる。
    """
    pre_articles = _posted_count(pre)
    post_articles = _posted_count(post)
    reasons: list[str] = []
    if post_articles < min_articles:
        reasons.append(
            f"切替後の窓の配信済み記事が {post_articles} 件 (判定に必要: {min_articles} 件)"
        )
    if pre_articles < min_articles:
        reasons.append(
            f"切替前の窓の配信済み記事が {pre_articles} 件 (判定に必要: {min_articles} 件)"
        )
    warnings: list[str] = []
    if post_changes:
        text = (
            f"切替後の窓に判定基準の版が {len(post_changes)} 件入っている "
            f"({_versions_text(post_changes)}) — 「切替後」が単一の状態でない"
        )
        (warnings if allow_mixed else reasons).append(text)
    elif not version_checked:
        (warnings if allow_mixed else reasons).append(NOTE_VERSION_UNCHECKED)
    if pre_changes:
        # 基準線側の混成は致命ではない (比較の相手が blend になるだけ) が、差の帰属は
        # その分弱まる。黙って PASS を出さず、必ず本文に残す。
        warnings.append(
            f"切替前の窓に判定基準の版が {len(pre_changes)} 件入っている "
            f"({_versions_text(pre_changes)}) — 基準線が混成のため差の帰属は限定的"
        )
    judged = not reasons

    pre_fields = _stat_fields(pre)
    post_fields = _stat_fields(post)
    shared = sorted(set(pre_fields) & set(post_fields))

    rows: list[MetricRow] = []
    for field_id in shared:
        row = _coverage_row(field_id, pre_fields[field_id], post_fields[field_id], judged=judged)
        if row is not None:
            rows.append(row)
    for field_id in DISTRIBUTION_FIELDS:
        if field_id in pre_fields and field_id in post_fields:
            rows.extend(
                _distribution_rows(
                    field_id, pre_fields[field_id], post_fields[field_id], judged=judged
                )
            )
    for field_id in shared:
        row = _average_row(field_id, pre_fields[field_id], post_fields[field_id], judged=judged)
        if row is not None:
            rows.append(row)

    failures = tuple(_failure_text(row) for row in rows if row.verdict == STATUS_FAIL)
    if not judged:
        status = STATUS_INCONCLUSIVE
    elif failures:
        status = STATUS_FAIL
    else:
        status = STATUS_PASS
    excluded, partial = _availability_groups(pre_fields, post_fields)
    return CutoverVerdict(
        status=status,
        exit_code=_EXIT_CODES[status],
        pre_articles=pre_articles,
        post_articles=post_articles,
        min_articles=min_articles,
        rows=tuple(rows),
        failures=failures,
        reasons=tuple(reasons),
        excluded_fields=excluded,
        partial_fields=partial,
        version_checked=version_checked,
        config_key=config_key,
        pre_changes=tuple(pre_changes),
        post_changes=tuple(post_changes),
        warnings=tuple(warnings),
    )


# ---------- 表示 ----------


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _ljust(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _rjust(text: str, width: int) -> str:
    return " " * max(0, width - _width(text)) + text


def _fmt_value(row: MetricRow, value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}%" if row.unit == "%" else f"{value:.1f} 字"


def _fmt_delta(row: MetricRow) -> str:
    if row.delta is None:
        return "—"
    return f"{row.delta:+.2f} pt" if row.unit == "%" else f"{row.delta:+.1f} 字"


def _fmt_role(row: MetricRow) -> str:
    if row.verdict != _NO_VERDICT:
        return f"{row.role} {row.verdict}"
    return f"{row.role} {_SUSPENDED}" if row.role == ROLE_GATE else row.role


def _render_table(rows: tuple[MetricRow, ...]) -> list[str]:
    header = ("フィールド", "切替前", "切替後", "差", "判定")
    body = [
        (
            f"{row.field_id} / {row.label}",
            _fmt_value(row, row.pre),
            _fmt_value(row, row.post),
            _fmt_delta(row),
            _fmt_role(row),
        )
        for row in rows
    ]
    widths = [max(_width(c[i]) for c in [header, *body]) for i in range(len(header))]
    lines = [
        "  ".join(
            [_ljust(header[0], widths[0])]
            + [_rjust(header[i], widths[i]) for i in (1, 2, 3)]
            + [header[4]]
        ),
        "-" * (sum(widths) + 2 * (len(widths) - 1)),
    ]
    for cells in body:
        lines.append(
            "  ".join(
                [_ljust(cells[0], widths[0])]
                + [_rjust(cells[i], widths[i]) for i in (1, 2, 3)]
                + [cells[4]]
            )
        )
    return lines


def _render_versions(verdict: CutoverVerdict, windows: dict[str, Any]) -> list[str]:
    """窓の中で判定基準が動いたかを必ず 1 ブロック出す (混成していなくても『なし』と書く)。"""
    if not verdict.version_checked:
        return [f"  判定基準の版 : 未検査 — {NOTE_VERSION_UNCHECKED}"]
    key = verdict.config_key or DEFAULT_CONFIG_KEY
    lines = [
        f"  判定基準の版 (config_key={key})",
        f"    切替前の窓 : {_versions_text(verdict.pre_changes) or 'なし'}",
        f"    切替後の窓 : {_versions_text(verdict.post_changes) or 'なし'}",
    ]
    if verdict.post_changes:
        cutover = _parse_cutover(windows["cutover"])
        first = _parse_created_at(verdict.post_changes[0].created_at)
        if first is not None:
            hours = (first - cutover).total_seconds() / 3600.0
            clean_days = int(hours // 24)
            if clean_days >= MIN_DAYS:
                lines.append(
                    f"    → この切替を単独で測れるのは次の版までの {hours:.1f} 時間。"
                    f"--days {clean_days} 以下なら混成しない。"
                )
            else:
                lines.append(
                    f"    → 次の版まで {hours:.1f} 時間しかなく、--days の最小 ({MIN_DAYS} 日) "
                    "でも混成する。この切替を単独で判定することはできない。"
                )
    return lines


def _render(verdict: CutoverVerdict, windows: dict[str, Any]) -> str:
    pre_window = windows["pre"]
    post_window = windows["post"]
    lines = [
        "プロンプト切替の前後比較",
        f"  切替時刻   : {windows['cutover']}",
        f"  切替前の窓 : {pre_window['since']} 〜 {pre_window['until']}"
        f" (配信済み {verdict.pre_articles} 件)",
        f"  切替後の窓 : {post_window['since']} 〜 {post_window['until']}"
        f" (実データ終端 {post_window['data_end']}, 配信済み {verdict.post_articles} 件)",
        *_render_versions(verdict, windows),
        "",
    ]
    lines.extend(_render_table(verdict.rows))
    lines.append("")
    if verdict.excluded_fields:
        lines.append(f"比較対象外 (統計なし): {', '.join(verdict.excluded_fields)}")
        lines.append(f"  {NOTE_AVAILABILITY_NONE}")
    if verdict.partial_fields:
        lines.append(f"代理指標 (availability=partial): {', '.join(verdict.partial_fields)}")
        lines.append(f"  {NOTE_PARTIAL}")
        lines.append(f"  例外: {', '.join(EXPLICIT_COVERAGE_GATES)} は明示ゲート。{NOTE_TITLE_JA}")
    if verdict.failures:
        lines.append("閾値超過:")
        lines.extend(f"  {failure}" for failure in verdict.failures)
    if verdict.warnings:
        lines.append("注意 (判定は続行):")
        lines.extend(f"  {warning}" for warning in verdict.warnings)
    lines.append("")
    reason = f" — {' / '.join(verdict.reasons)}" if verdict.reasons else ""
    lines.append(f"総合判定: {verdict.status}{reason}")
    # C7 破局ゲート (2026-08-22 独立レビュー H6): 総合 FAIL のうち無人 rollback を
    # 作動させてよいのは「判定傾向の変質 (分布)」「フィールド全滅級の低下」のみ。
    # 充足率の小幅ゲート (1-2pt) は片側 Goodhart ラチェット、要約長 ±15% は
    # ブレビティ改善の自動巻き戻しになるため、人間向け報告に残すが無人適用には繋がない。
    if verdict.status == STATUS_FAIL:
        flag = STATUS_FAIL if any(is_catastrophic_row(r) for r in verdict.rows) else STATUS_PASS
        lines.append(f"{CATASTROPHIC_LINE_PREFIX} {flag} ({_CATASTROPHIC_NOTE})")
    return "\n".join(lines)


def is_catastrophic_row(row: MetricRow) -> bool:
    """C7 無人 rollback の作動対象 (破局ゲート) の FAIL 行か。

    - 分布シフト (metric="share"、閾値 ±20pt) — 判定傾向の変質
    - 汎用充足率 (metric="coverage" かつ明示ゲート外、閾値 -15pt) — フィールド全滅級

    明示ゲート (title_ja 2pt / summary 1pt) と平均要約長 (±15%) の FAIL は
    破局ではない — 人間の判断対象。
    """
    if row.verdict != STATUS_FAIL:
        return False
    if row.metric == "share":
        return True
    return row.metric == "coverage" and row.field_id not in EXPLICIT_COVERAGE_GATES


# ---------- CLI ----------


def _parse_cutover(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _logs_to_stderr() -> None:
    """service 層の構造化ログを stderr へ逃がす。

    ``build_field_stats`` は集計ログを stdout に書く (12-Factor)。本 script の stdout は
    表 / JSON そのものなので、混ざると ``--json`` の出力が壊れる。ログは捨てずに移すだけ。
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            handler.setStream(sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="プロンプト切替の前後で LLM 出力の遵守率が落ちていないかを判定する",
    )
    parser.add_argument(
        "--cutover",
        required=True,
        help="切替時刻 (ISO 8601、例: 2026-08-16T07:01:26Z)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help=f"前後それぞれの窓の幅 (日、{MIN_DAYS}〜{MAX_DAYS}、既定 7)",
    )
    parser.add_argument(
        "--min-articles",
        type=int,
        default=MIN_POST_ARTICLES,
        help=f"判定に必要な窓あたりの配信済み記事数 (既定 {MIN_POST_ARTICLES})",
    )
    parser.add_argument(
        "--config-key",
        default=DEFAULT_CONFIG_KEY,
        help=(
            "窓の純度を検査するための判定基準の config_store key "
            f"(既定 {DEFAULT_CONFIG_KEY}、空文字で検査を省略)"
        ),
    )
    parser.add_argument(
        "--allow-mixed",
        action="store_true",
        help="窓の中で判定基準が動いていても判定を出す (混成は警告として表示)",
    )
    parser.add_argument("--json", action="store_true", help="機械可読な JSON で出力する")
    return parser


def _load_history(config_key: str) -> tuple[tuple[ConfigVersion, ...], bool]:
    """判定基準の版履歴と「検査できたか」を返す。

    読めない / 0 件は **「混成なし」ではなく「未検査」** に倒す (fail-closed)。
    key の打ち間違いが「窓は綺麗でした」と読める出力になるのを避ける。
    """
    if not config_key:
        return ((), False)
    try:
        history = tuple(list_history(config_key, limit=VERSION_HISTORY_LIMIT))
    except Exception as exc:  # DB 障害・table 不在: 判定は続けるが未検査として扱う
        print(
            f"判定基準の版履歴を読めませんでした ({type(exc).__name__}) — 窓の純度は未検査",
            file=sys.stderr,
        )
        return ((), False)
    if not history:
        print(
            f"config_key={config_key!r} の版履歴が 0 件 — 窓の純度は未検査",
            file=sys.stderr,
        )
        return ((), False)
    return (history, True)


def main(argv: list[str] | None = None) -> int:
    _logs_to_stderr()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.days < MIN_DAYS or args.days > MAX_DAYS:
        parser.error(f"--days は {MIN_DAYS}〜{MAX_DAYS} で指定してください")
    try:
        cutover = _parse_cutover(args.cutover)
    except ValueError:
        parser.error(f"--cutover を ISO 8601 として解釈できません: {args.cutover!r}")
    now = datetime.now(UTC)
    if cutover > now:
        parser.error(f"--cutover が未来です ({_iso_z(cutover)} > {_iso_z(now)})")

    # 切替後の窓は [cutover, cutover + days)。上限を cutover + days に置くことで
    # build_field_stats の since が cutover ちょうどに揃う (切替前のデータが混ざらない)。
    # created_at は取込時刻なので未来行は存在せず、実データの終端は now で頭打ちになる。
    post_until = cutover + timedelta(days=args.days)
    pre = build_field_stats(args.days, until=cutover)
    post = build_field_stats(args.days, until=post_until)

    history, version_checked = _load_history(args.config_key)
    verdict = evaluate_cutover(
        pre,
        post,
        min_articles=args.min_articles,
        config_key=args.config_key,
        version_checked=version_checked,
        pre_changes=changes_within(
            history,
            since=cutover - timedelta(days=args.days),
            until=cutover,
            include_since=True,
        ),
        post_changes=changes_within(history, since=cutover, until=post_until, include_since=False),
        allow_mixed=args.allow_mixed,
    )

    windows = {
        "cutover": _iso_z(cutover),
        "days": args.days,
        "pre": {"since": pre["window"]["since"], "until": pre["window"]["until"]},
        "post": {
            "since": post["window"]["since"],
            "until": post["window"]["until"],
            "data_end": _iso_z(min(now, post_until)),
        },
    }
    if args.json:
        print(
            json.dumps(
                {"windows": windows, **asdict(verdict)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_render(verdict, windows))
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
