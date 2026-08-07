"""Phase B-content-dedup: Cross-source 同 advisory dedup を content-based で実装。

同一 incident / advisory が複数 source から流入した際、source の優劣判断 (hierarchy)
を一切せず **公平に first-come-first-served** で 2 件目以降を skip する。

判定ロジック:
    1. 各 article から ``title_signature`` を抽出 (CVE / 英語トークン / 日本語トークン)
    2. 24h 内の posted article で signature の Jaccard 類似度 >= 0.4 のものを検索
    3. 該当があれば status='skipped_duplicate' で skip

注意:
    - 24h 超え (48h-168h) は 続報として ``followup_info`` で通る (既存設計維持)
    - signature token が 3 個未満なら判定 skip (誤検知回避)
    - source の hierarchy なし、同一 reliability 前提
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from src.logging_config import get_logger
from src.storage.run_history import ArticleRecord, RunHistoryRepository

_log = get_logger(__name__)

# Cross-source content dedup の判定パラメータ
DEFAULT_LOOKBACK_HOURS = 24
# 同 advisory の cross-source 翻訳/言い回し差で Jaccard が低く出る (0.3-0.4) ため
# 0.4 を採用。CVE-ID 完全一致は別経路で forced match。
DEFAULT_JACCARD_THRESHOLD = 0.4
MIN_TOKENS_REQUIRED = 3

# 同一性確実な信号 (advisory id 共有 / ほぼ同一タイトル) にのみ適用する延長窓
# (監査 2026-08-01)。JVN → 翌日 JVNDB (iPedia) 再掲が 24h 窓の外で二重投稿された
# 実事例への対処。0.4 帯の「24h 超は続報として通す」設計は不変 — 延長窓で
# 効くのは「同一の advisory とほぼ断定できる」信号だけに限定する。
IDENTITY_LOOKBACK_HOURS = 72
IDENTITY_JACCARD_THRESHOLD = 0.85
# 72h 窓の候補取得上限 (24h 窓比で流量 3 倍を見込む)
_IDENTITY_CANDIDATE_LIMIT = 900

# CVE-ID 抽出 (highest-priority signature element) — DATE 除去前に raw text に対して適用
_CVE_RE = re.compile(r"cve[- ]?(\d{4})[- ]?(\d{4,7})", re.IGNORECASE)
# JVN advisory id: JVNVU#98759887 / JVNTA#... (タイトル表記) と
# jvn.jp/vu/JVNVU98759887/ (URL 表記)、JVNDB-2026-000103 (iPedia URL) を正規化。
# 同一 advisory が JVN 日本語版 / English 版 / JPCERT 統合 RSS で別 URL パス・
# 日英別タイトルになるため、URL 中のこの id が唯一の決定論的同一性キー。
_JVN_VU_RE = re.compile(r"jvn(vu|ta)[#＃]?(\d{8})", re.IGNORECASE)
_JVNDB_RE = re.compile(r"jvndb-(\d{4})-(\d{6})", re.IGNORECASE)
# 日付パターン (signature から除外、年/月/日 Japanese 表記限定で false-positive 回避)
_DATE_RE = re.compile(r"\(?\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?\)?")
# 句読点・記号
_PUNCT_RE = re.compile(r"[、。:：;；,，.\-_「」『』()()【】\[\]\"'`!?！？／/]")
# 日本語文字 (漢字 / カタカナ / ひらがな)
_JA_CHAR_RE = re.compile(r"[一-龯ぁ-んァ-ヶー]+")
# 英単語 (3 文字以上 lowercase / alphanumeric)
_EN_TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")

# Stop words: 一般語・形式語・category 名で signature にしない方が良いトークン
_STOP_WORDS: frozenset[str] = frozenset(
    {
        # 英語: 一般語 + CTI 文脈の頻出語
        "and",
        "the",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "this",
        "that",
        "these",
        "those",
        "have",
        "has",
        "had",
        "been",
        "being",
        "their",
        "there",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "via",
        "attack",
        "attacks",
        "attacker",
        "attackers",
        "threat",
        "threats",
        "security",
        "vulnerability",
        "vulnerabilities",
        "exploit",
        "exploits",
        "exploitation",
        "malware",
        "ransomware",
        "advisory",
        "advisories",
        "critical",
        "severe",
        "severity",
        "update",
        "updates",
        "patch",
        "patches",
        "warning",
        "warnings",
        "cyber",
        "cybersecurity",
        "flaws",
        "flaw",
        "multiple",
        "issue",
        "issues",
        "vendor",
        "vendors",
        "product",
        "products",
        "system",
        "systems",
        "service",
        "services",
        "users",
        "user",
        "data",
        "code",
        "remote",
        "execution",
        "access",
        "through",
        "report",
        "reports",
        "alert",
        "alerts",
        "new",
        "old",
        "latest",
        "now",
        # 日本語: 形式語
        "について",
        "における",
        "に関する",
        "および",
        "など",
        "もの",
        "こと",
        "ため",
        "とき",
        "場合",
        "ので",
        "から",
        "まで",
        "とした",
        "製品",
        "対象",
        "確認",
        "可能性",
        "可能",
        "影響",
        "結果",
        "原因",
        "脆弱性",
        "問題",
        "事象",
        "事件",
        "事例",
        "情報",
        "内容",
        "注意喚起",
        "注意",
        "喚起",
        "公開",
        "公表",
        "報告",
        "発表",
        "対応",
        "対策",
        "措置",
        "実施",
        "施行",
        "発行",
        "複数",
        "全て",
        "全部",
        "一部",
        "一連",
        "今回",
        "今後",
        "直近",
        "高度",
        "重大",
        "深刻",
        "緊急",
        "重要",
        "セキュリティ",
        "サイバー",
        "システム",
        "ソフトウェア",
        "アプリケーション",
        "ベンダー",
        "プロダクト",
        "アップデート",
        "パッチ",
        "アタック",
    },
)

# Japanese phrase-level stop substrings (delimiter として消す → token 境界になる)
_JA_STOP_WORDS_PHRASES: tuple[str, ...] = (
    "について",
    "における",
    "に関する",
    "において",
    "における",
    "および",
    "ならびに",
    "もしくは",
    "または",
    "製品",
    "脆弱性",
    "注意喚起",
    "セキュリティ",
    "サイバー",
    "情報",
    "事象",
    "事件",
    "事例",
    "対応",
    "対策",
    "措置",
    "ベンダー",
    "プロダクト",
    "システム",
    "ソフトウェア",
    "アップデート",
    "パッチ",
    "アタック",
    "アラート",
    "公開",
    "公表",
    "報告",
    "発表",
    "影響",
    "結果",
    "原因",
    "可能性",
    "問題",
    "確認",
    "施行",
    "発行",
    "重大",
    "深刻",
    "緊急",
    "重要",
    "高度",
    "複数",
)


def extract_title_signature(title: str, summary: str = "") -> frozenset[str]:
    """タイトル (+ 要約 head) から signature token 集合を抽出。

    Jaccard 比較に使う「正規化済の意味トークン」集合を返す。

    日本語は **stop word 除去 + 部分文字列 (trigram + 完全フレーズ)** で
    形態素解析なしでも reasonable な token 集合を作る。

    Args:
        title: BriefingMessage.title
        summary: 補助として要約の先頭 (token 数増加用、オプション)

    Returns:
        signature 用 frozenset。空集合 = 判定不能。
    """
    if not title:
        return frozenset()
    raw = f"{title} {summary}".lower()

    tokens: set[str] = set()
    # 1. CVE-ID (canonical form) — raw text に対して先に抽出 (date/punct cleanup の前)
    for m in _CVE_RE.finditer(raw):
        tokens.add(f"cve-{m.group(1)}-{m.group(2)}")

    # cleanup: 日付 (年月日 限定) と句読点を空白に
    text = _DATE_RE.sub(" ", raw)
    text = _PUNCT_RE.sub(" ", text)

    # 2. 英単語 (3+ chars)
    for m in _EN_TOKEN_RE.finditer(text):
        tokens.add(m.group(0))

    # 3. 日本語: 連続日本語文字列を抽出 → stop word substring を除去 →
    #    残った文字列を {2..6} 文字の重複 ngram に分解。
    #    形態素解析なしで Trend Micro / Shai-Hulud / ICS / Microsoft 等の
    #    product/actor 単語にマッチさせる効果を持つ。
    for ja_match in _JA_CHAR_RE.finditer(text):
        ja_segment = ja_match.group(0)
        # stop word の完全マッチを抜く
        for sw in _JA_STOP_WORDS_PHRASES:
            if sw in ja_segment:
                ja_segment = ja_segment.replace(sw, " ")
        # 残った日本語サブセグメントから 2-6 ngram を抽出
        for part in ja_segment.split():
            if len(part) >= 2:
                # 完全形 (短い token は意味単位として保持)
                if 2 <= len(part) <= 8:
                    tokens.add(part)
                # 長いセグメントは overlapping ngram (size 3, 4) で分解
                if len(part) > 4:
                    for n in (3, 4):
                        for i in range(len(part) - n + 1):
                            tokens.add(part[i : i + n])

    tokens -= _STOP_WORDS
    return frozenset(tokens)


def extract_advisory_ids(url: str, title: str, summary: str = "") -> frozenset[str]:
    """URL / タイトル / 要約から JVN 系 advisory id を正規化トークンで抽出する。

    返り値例: {"jvnvu-98759887"} / {"jvndb-2026-000103"}。空集合 = advisory id なし。
    """
    raw = f"{url} {title} {summary}".lower()
    tokens: set[str] = set()
    for m in _JVN_VU_RE.finditer(raw):
        tokens.add(f"jvn{m.group(1).lower()}-{m.group(2)}")
    for m in _JVNDB_RE.finditer(raw):
        tokens.add(f"jvndb-{m.group(1)}-{m.group(2)}")
    return frozenset(tokens)


def _hours_ago(created_at: datetime | None, now: datetime) -> float:
    """record の経過時間 (時間)。tz 欠落は UTC とみなす。不明は 0 (最新扱い)。"""
    if created_at is None:
        return 0.0
    dt = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


def find_recent_content_duplicate(
    *,
    repo: RunHistoryRepository,
    title: str,
    summary: str = "",
    url: str = "",
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    jaccard_threshold: float = DEFAULT_JACCARD_THRESHOLD,
    candidate_article_id: str | None = None,
) -> ArticleRecord | None:
    """直近 ``lookback_hours`` の posted article から content-similar な記事を探す。

    Args:
        repo: RunHistoryRepository
        title: 新 article の title
        summary: 新 article の summary (オプション、token 拡張用)
        url: 新 article の URL (advisory id 抽出用、オプション)
        lookback_hours: 比較対象 window (default 24h)
        jaccard_threshold: 類似と判定する Jaccard 下限 (default 0.4、DEFAULT_JACCARD_THRESHOLD)
        candidate_article_id: 自分自身を除外するための article_id (再判定用)

    Returns:
        マッチした 先行 article record (公平な first-come-first-served なので、
        最古でなく **最新** の被マッチ record を返す) or None。

    窓の設計 (監査 2026-08-01):
        - 通常信号 (Jaccard 0.4 帯 / 共有 CVE) は ``lookback_hours`` (24h) 内のみ —
          24h 超は続報として通す既存設計を維持する。
        - 同一性確実な信号 (共有 advisory id / Jaccard >= 0.85 のほぼ同一タイトル)
          のみ ``IDENTITY_LOOKBACK_HOURS`` (72h) まで判定する (JVNDB 翌日再掲型)。
    """
    sig_new = extract_title_signature(title, summary)
    ids_new = extract_advisory_ids(url, title, summary)
    if len(sig_new) < MIN_TOKENS_REQUIRED and not ids_new:
        # signature が貧弱な場合は誤判定を避けて dedup スキップ
        return None
    sig_new_cves = {t for t in sig_new if t.startswith("cve-")}

    # 候補は同一性延長窓 (72h) まで取得し、通常信号は 24h 内に限定して適用する
    candidates = repo.list_recent_posted_articles(
        lookback_hours=IDENTITY_LOOKBACK_HOURS, limit=_IDENTITY_CANDIDATE_LIMIT
    )
    now = datetime.now(UTC)
    best: ArticleRecord | None = None
    best_score = 0.0
    best_reason = ""
    for art in candidates:
        if candidate_article_id and art.article_id == candidate_article_id:
            continue
        # 強制 dedup (延長窓): 同 advisory id は URL パス・日英タイトル差に関係なく同一
        if ids_new:
            shared_ids = ids_new & extract_advisory_ids(art.url or "", art.title or "")
            if shared_ids:
                _log.info(
                    "content_dedup_match",
                    reason="shared_advisory_id",
                    shared_ids=sorted(shared_ids),
                    prior_article_id=art.article_id,
                    prior_feed=art.feed_title,
                )
                return art
        sig_old = extract_title_signature(art.title or "", art.summary or "")
        if len(sig_old) < MIN_TOKENS_REQUIRED:
            continue
        within_normal_window = _hours_ago(art.created_at, now) <= lookback_hours
        # 強制 dedup: 同 CVE-ID は確度高で同 advisory と判定 (通常窓のみ —
        # 24h 超の同 CVE 記事は続報でありうるため延長窓では判定しない)
        shared_cves = sig_new_cves & {t for t in sig_old if t.startswith("cve-")}
        if shared_cves and within_normal_window:
            _log.info(
                "content_dedup_match",
                reason="shared_cve",
                shared_cves=sorted(shared_cves),
                prior_article_id=art.article_id,
                prior_feed=art.feed_title,
            )
            return art
        # Jaccard 類似度: 通常窓は 0.4 帯、延長窓は「ほぼ同一タイトル」のみ
        score = _jaccard(sig_new, sig_old)
        effective_threshold = (
            jaccard_threshold if within_normal_window else IDENTITY_JACCARD_THRESHOLD
        )
        if score >= effective_threshold and score > best_score:
            best_score = score
            best = art
            best_reason = "jaccard" if within_normal_window else "jaccard_identity"
    if best is not None:
        _log.info(
            "content_dedup_match",
            reason=best_reason,
            jaccard=round(best_score, 3),
            threshold=jaccard_threshold,
            prior_article_id=best.article_id,
            prior_feed=best.feed_title,
            prior_title=(best.title or "")[:60],
        )
    return best
