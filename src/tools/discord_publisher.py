"""Discord Webhook 投稿ツール (Step 7)。

``discord-webhook`` ライブラリを使い、``BriefingMessage`` を Discord Embed
に整形して投稿する。

機能:
- 重要度別の Embed カラー (high=red, medium=yellow, low=gray)
- BLUF を太字で description 先頭に
- IOC をコードブロック化
- MITRE ATT&CK ID を attack.mitre.org へのリンク化
- 長い summary の自動分割 (Discord embed description 4096 字制限内)
- レート制限 (asyncio.Lock + 最小間隔)

``DiscordPublisher`` は webhook URL を 1 本受け取る単純なアダプタ。
チャンネル別の振り分けは ``src/main.py`` 側のルーティング (重要度判定の
結果による) で行うため、ここでは関心を持たない (CLAUDE.md §7)。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, ClassVar, Literal

from discord_webhook import DiscordEmbed, DiscordWebhook
from pydantic import BaseModel, ConfigDict, Field

from src.logging_config import get_logger

_log = get_logger(__name__)

# ---------- 定数 ----------

# Discord 制限値
EMBED_TITLE_MAX = 256
EMBED_DESC_MAX = 4096
EMBED_FIELD_VALUE_MAX = 1024

# 安全マージン込みの実用上限
DESC_CHUNK_SAFE = 3500
FIELD_VALUE_SAFE = 1000

# 重要度別カラー (10 進)
IMPORTANCE_COLORS: dict[str, int] = {
    "high": 0xE74C3C,  # red
    "medium": 0xF1C40F,  # yellow
    "low": 0x95A5A6,  # gray
}

DEFAULT_USERNAME = "CTI Briefing Bot"
DEFAULT_RATE_LIMIT_PER_SECOND = 4

# ---------- Retry 設定 (Phase 5P) ----------
# 1 回の試行 + N 回のリトライ。MAX_RETRY_ATTEMPTS=3 で合計 4 回試行。
MAX_RETRY_ATTEMPTS = 3
# 5xx exponential backoff のベース秒。1 → 2 → 4 (合計 7s)
BACKOFF_BASE_SECONDS = 1.0
# 429 retry_after 値の上限 (Discord が極端に大きな値を返した場合の安全弁)
MAX_RETRY_AFTER_SECONDS = 30.0
# JSONDecodeError リトライ用の固定 wait
JSON_DECODE_RETRY_WAIT_SECONDS = 1.0
# リトライ対象の 5xx
RETRYABLE_5XX_STATUS = frozenset({500, 502, 503, 504})

ATTACK_TECHNIQUE_PATTERN = re.compile(r"^T(\d+)(?:\.(\d+))?$")

Importance = Literal["high", "medium", "low"]

# カテゴリ → (絵文字, 表示名) のマッピング (T1)。
# 国旗で国別 APT を一瞥識別、横断 / 概要は中性的なシンボル。
_CATEGORY_PRESENTATION: dict[str, tuple[str, str]] = {
    # state_apt
    "summary": ("📋", "全体サマリー"),
    "china_apt": ("🇨🇳", "中国関連 (MSS / PLA直轄系)"),
    "north_korea_apt": ("🇰🇵", "北朝鮮関連 (RGB直轄系)"),
    "russia_apt": ("🇷🇺", "ロシア関連 (GRU / SVR直轄系)"),
    "japan_targeted": ("🇯🇵", "日本に対するサイバー攻撃"),
    "noted_events": ("🌐", "界隈注目事象"),
    "crossover": ("🌐", "クロスオーバー"),
    # x_early_signals (Phase 5C)
    "vuln_chatter": ("🐛", "脆弱性・ゼロデイ気づき"),
    "ransomware_watch": ("💀", "ランサム被害・新ファミリー"),
    "leak_breach": ("📂", "情報流出・リーク"),
    "threat_actor_chatter": ("🕳️", "アンダーグラウンド動向"),
    "disclosure_disputes": ("⚖️", "ベンダー vs 研究者"),
    "misc_signals": ("🌀", "その他注目シグナル"),
    # system 通知 (パイプライン稼働ステータス)
    "system": ("🛠️", "システム"),
    "unknown": ("🛡️", "その他"),
}

# 重要度 → content 先頭絵文字 (Phase 5D / G': 色ベース統一)
# 信号機モチーフ: red / yellow / white で「重要度」を直感的に伝える。
# embed の左帯色 (red/yellow/gray) とも視覚的に一致。
_IMPORTANCE_EMOJI: dict[str, str] = {
    "high": "🔴",
    "medium": "🟡",
    "low": "⚪",
}

# 信頼性ラベル → 宝石モチーフの色記号 (Phase 5D / 13)。
# 重要度 (信号色) と意味的に分けるため別系統 (宝石/水滴) を採用。
_RELIABILITY_EMOJI: dict[str, str] = {
    "high": "💎",
    "medium": "🔷",
    "low": "🔹",
}

# 信頼性の日本語表示 (M3: embed だけ生英語 "high" で Web「確度高」と方言化していた)。
# "high=2, medium=1" のような集計文字列はそのまま通す (.get fallback)。
_CONFIDENCE_JA: dict[str, str] = {
    "high": "高",
    "medium": "中",
    "low": "低",
}


def _confidence_ja(value: str) -> str:
    """confidence 値を日本語化する (既知の 3 値のみ、集計文字列は素通し)。"""
    return _CONFIDENCE_JA.get(value.strip().lower(), value)


# Discord timestamp 用のフォーマット指定子
_TS_RELATIVE = "R"  # "30 分前" 形式
_TS_FULL_SHORT = "f"  # "2026年5月2日 06:35" 形式


# ---------- Models ----------


class Source(BaseModel):
    """記事の出典情報。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str
    url: str
    language: str = "en"


class BriefingIncident(BaseModel):
    """1 つの事象 (multi-embed モードで 1 embed に対応)。

    multi-embed モード (``BriefingMessage.incidents`` が空でない) のとき、
    各 incident は独立した Discord embed として表示される。

    Phase 5C: x_early_signals タスク用に追加された optional フィールド
    (signal_type 等) を持つ。state_apt 経路では None のまま。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    # 表示用見出し (例: "事象 1", "Incident 1: Volt Typhoon")
    heading: str = ""
    # 事象の本文 (description に入る)
    body: str = ""
    related_actor: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    sources: list[Source] = Field(default_factory=list)
    iocs: list[str] = Field(default_factory=list)
    ttps: list[str] = Field(default_factory=list)
    is_new: bool | None = None
    # x_early_signals 専用フィールド (state_apt では未使用)
    signal_type: Literal["observation", "claim", "speculation", "confirmation"] | None = None
    corroboration: Literal["multi_source", "single_source", "unverified"] | None = None
    account_class: (
        Literal[
            "vendor_official",
            "analyst_known",
            "analyst_unknown",
            "aggregator",
            "affected_party",
        ]
        | None
    ) = None
    engagement_signal: Literal["high", "medium", "low"] | None = None
    suggested_followup: str | None = None


class BriefingMessage(BaseModel):
    """1 件の CTI ブリーフィング (Discord 投稿単位)。

    ``incidents`` が空 (既定): 単 embed モード。summary が 1 つの embed に入る。
    ``incidents`` が非空: multi-embed モード。content にセクション概観、
        各 incident が個別 embed、最後に集約 embed が並ぶ。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str
    # Phase 5T-O: Inoreader 経路は廃止 (title と重複)、Grok 経路は機械的に生成して使用。
    # 空文字なら BLUF 部分はレンダリングされない (renderer 側で skip)。
    bluf: str = ""
    importance: Importance
    category: str
    summary: str
    iocs: list[str] = Field(default_factory=list)
    mitre_techniques: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    analyst_note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # multi-embed モード用 (空なら従来の単 embed 動作)
    incidents: list[BriefingIncident] = Field(default_factory=list)


class PostResult(BaseModel):
    """``post_batch`` の集約結果。"""

    model_config = ConfigDict(frozen=True)

    total: int
    succeeded: int
    failed: int
    failures: list[str] = Field(default_factory=list)


class DiscordPostMeta(BaseModel):
    """Phase 5T-K: Discord webhook 投稿後に得られる Discord 内 ID。

    digest pipeline で「digest 項目 → 元 watch ch 投稿への 1-click 遷移」
    を実現するため、最初の (主) webhook の message_id / channel_id を持ち回る。
    分割投稿の 2 件目以降は捨てる (digest link は 1 本でよい)。

    取得失敗 (wait=true でも response が欠ける、429 etc) 時は両 field None。
    """

    model_config = ConfigDict(frozen=True)

    message_id: str | None = None
    channel_id: str | None = None


class DiscordPostError(RuntimeError):
    """Discord Webhook 投稿が 4xx/5xx で失敗。"""


def _extract_post_meta(response: object) -> DiscordPostMeta:
    """Phase 5T-K: webhook response (wait=true) から message_id / channel_id を抽出。

    取れない場合は両 field None の DiscordPostMeta を返す (link 生成側で fallback)。
    """
    try:
        json_method = getattr(response, "json", None)
        if json_method is None:
            return DiscordPostMeta()
        data = json_method()
    except Exception:  # noqa: BLE001
        return DiscordPostMeta()
    if not isinstance(data, dict):
        return DiscordPostMeta()
    mid = data.get("id")
    cid = data.get("channel_id")
    return DiscordPostMeta(
        message_id=str(mid) if mid is not None else None,
        channel_id=str(cid) if cid is not None else None,
    )


# ---------- DiscordPublisher ----------


class DiscordPublisher:
    """Discord Webhook 経由で ``BriefingMessage`` を投稿する。"""

    _MITRE_URL_FMT: ClassVar[str] = "https://attack.mitre.org/techniques/{base}{suffix}/"

    def __init__(
        self,
        webhook_url: str,
        username: str = DEFAULT_USERNAME,
        rate_limit_per_second: int = DEFAULT_RATE_LIMIT_PER_SECOND,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook_url が空です")
        if rate_limit_per_second <= 0:
            raise ValueError("rate_limit_per_second は正の整数が必要です")
        self._webhook_url = webhook_url
        self._username = username
        self._rate_limit_per_second = rate_limit_per_second
        self._min_interval = 1.0 / rate_limit_per_second
        self._last_send_at: float = 0.0
        self._send_lock = asyncio.Lock()

    @property
    def min_interval_seconds(self) -> float:
        return self._min_interval

    # ----- public API -----

    async def post(
        self,
        message: BriefingMessage,
        *,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> DiscordPostMeta:
        """1 件投稿する (長すぎる summary は複数 webhook に自動分割)。

        ``attachments``: ``[(filename, content_bytes), ...]`` のリスト。
        指定された場合は **最初の webhook** にのみ添付され、Discord 側で
        メッセージ本体と一緒に 1 つのアタッチメントとして表示される。
        分割された後続 webhook には添付しない (重複防止)。

        Returns:
            DiscordPostMeta: 最初の webhook の message_id / channel_id (Phase 5T-K)。
            取得失敗時は両 field None だが、本体投稿自体は成功している場合あり。
        """
        webhooks = self._build_webhooks(message)
        first_meta = DiscordPostMeta()
        for i, webhook in enumerate(webhooks):
            if i == 0 and attachments:
                for filename, content in attachments:
                    webhook.add_file(file=content, filename=filename)
            meta = await self._send_with_rate_limit(webhook)
            if i == 0:
                first_meta = meta
        return first_meta

    async def post_batch(self, messages: list[BriefingMessage]) -> PostResult:
        """複数件を直列に投稿する。1 件失敗しても続行し集約結果を返す。"""
        succeeded = 0
        failures: list[str] = []
        for msg in messages:
            try:
                await self.post(msg)
                succeeded += 1
            except Exception as e:  # noqa: BLE001
                failures.append(f"{msg.title!r}: {type(e).__name__}: {e}")
                _log.error(
                    "discord_post_failed",
                    title=msg.title,
                    error_type=type(e).__name__,
                    error=str(e),
                )
        return PostResult(
            total=len(messages),
            succeeded=succeeded,
            failed=len(failures),
            failures=failures,
        )

    # ----- internal: rate-limited send -----

    async def _send_with_rate_limit(self, webhook: DiscordWebhook) -> DiscordPostMeta:
        """webhook を送信する。429/5xx/JSONDecodeError は自動リトライ。

        Phase 5P: Discord 側の一時障害に対して堅牢化。
        - 429: ``Retry-After`` header または body の ``retry_after`` を尊重
          (上限 ``MAX_RETRY_AFTER_SECONDS``)
        - 5xx (500/502/503/504): exponential backoff (1s → 2s → 4s)
        - JSONDecodeError: 固定 1s wait で 1 回だけリトライ
        - その他 4xx: リトライしない (即 raise)

        Phase 5T-K: 投稿成功時 wait=true 応答から DiscordPostMeta を返す。
        最大試行回数 = ``1 + MAX_RETRY_ATTEMPTS``。それでも失敗したら最終応答で raise。
        """
        async with self._send_lock:
            await self._respect_min_interval()
            meta = await self._execute_with_retry(webhook)
            self._last_send_at = time.monotonic()
            return meta

    async def _respect_min_interval(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_send_at
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)

    async def _execute_with_retry(self, webhook: DiscordWebhook) -> DiscordPostMeta:
        attempt = 0
        last_status: int | None = None
        last_body: str = ""
        while True:
            attempt += 1
            try:
                response = await asyncio.to_thread(webhook.execute)
            except json.JSONDecodeError as e:
                # discord-webhook が応答 JSON を内部 parse して失敗する稀なケース
                if attempt > MAX_RETRY_ATTEMPTS:
                    raise DiscordPostError(
                        f"webhook JSON parse failed after {attempt} attempts: {e}",
                    ) from e
                _log.warning(
                    "discord_post_retry",
                    attempt=attempt,
                    wait_seconds=JSON_DECODE_RETRY_WAIT_SECONDS,
                    status=None,
                    reason="json_decode_error",
                )
                await asyncio.sleep(JSON_DECODE_RETRY_WAIT_SECONDS)
                continue

            status = getattr(response, "status_code", None)
            body = str(getattr(response, "text", ""))
            last_status = status
            last_body = body

            if status is not None and status < 400:
                _log.info("discord_post_sent", status=status)
                # Phase 5T-K: wait=true 応答から message_id / channel_id を抽出
                return _extract_post_meta(response)

            wait = self._compute_retry_wait(status, body, response, attempt)
            if wait is None or attempt > MAX_RETRY_ATTEMPTS:
                raise DiscordPostError(
                    f"webhook 投稿失敗 ({attempt} attempts): HTTP {last_status}: {last_body[:200]}",
                )
            _log.warning(
                "discord_post_retry",
                attempt=attempt,
                wait_seconds=wait,
                status=status,
                reason="rate_limit" if status == 429 else "server_error",
            )
            await asyncio.sleep(wait)

    @staticmethod
    def _compute_retry_wait(
        status: int | None,
        body: str,
        response: Any,
        attempt: int,
    ) -> float | None:
        """この応答がリトライ対象なら待機秒、そうでなければ None を返す。"""
        if status == 429:
            return DiscordPublisher._wait_from_429(response, body)
        if status in RETRYABLE_5XX_STATUS:
            # exponential backoff: 1s, 2s, 4s, ...
            return float(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        # その他 4xx (401/403/404 等) や None はリトライしない
        return None

    @staticmethod
    def _wait_from_429(response: Any, body: str) -> float:
        """429 応答から待機秒を抽出 (上限クランプ)。"""
        # 1. Retry-After header (HTTP 標準、整数秒 or HTTP-date)
        headers = getattr(response, "headers", None)
        if headers:
            raw = headers.get("Retry-After") or headers.get("retry-after")
            if raw:
                try:
                    return min(float(raw), MAX_RETRY_AFTER_SECONDS)
                except (TypeError, ValueError):
                    pass
        # 2. body の retry_after (Discord 仕様、float 秒)
        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if isinstance(payload, dict):
            ra = payload.get("retry_after")
            if isinstance(ra, (int, float)):
                return min(float(ra), MAX_RETRY_AFTER_SECONDS)
        # 3. fallback
        return BACKOFF_BASE_SECONDS

    # ----- internal: embed assembly -----

    def _build_webhooks(self, message: BriefingMessage) -> list[DiscordWebhook]:
        # Phase 5D / 7: system 通知 (パイプライン稼働状況) は embed 不要。
        # 1 行の状態テキストなので plain-text のみで投稿し、視覚ノイズを抑える。
        if message.category == "system":
            return self._build_system_notification_webhooks(message)
        # 事象を持たない構造化セクション (summary, 該当事象なし等) は
        # プレーンテキストのみで投稿。embed 化すると擬似的な「事象」を
        # 作り出してしまうため。
        # 判定: incidents が空 + Grok 経路 (metadata.grok_task_id あり)。
        is_grok_section_without_incidents = not message.incidents and bool(
            message.metadata.get("grok_task_id")
        )
        if is_grok_section_without_incidents:
            return self._build_plaintext_only_webhooks(message)

        # multi-embed モード: incidents が空でないとき、1 webhook 内に
        # content + 各 incident embed + 集約 embed を並べる。
        if message.incidents:
            return self._build_multi_embed_webhooks(message)

        description = self._compose_description(message)
        chunks = _chunk_text(description, max_chars=DESC_CHUNK_SAFE)
        webhooks: list[DiscordWebhook] = []
        total = len(chunks)
        # content (embed 外プレビュー) は最初の webhook にのみ設定
        content_preview = self._compose_content_preview(message)
        for i, chunk in enumerate(chunks):
            is_first = i == 0
            is_last = i == total - 1
            base_title = _apply_followup_prefix(message)
            title = base_title if is_first else f"{base_title} (続き {i + 1}/{total})"
            embed = DiscordEmbed(
                title=title[:EMBED_TITLE_MAX],
                description=chunk,
                color=IMPORTANCE_COLORS[message.importance],
            )
            if is_first:
                self._set_author(embed, message)
                self._add_metadata_fields(embed, message)
                self._set_timestamp(embed, message)
            if is_last and message.sources:
                self._add_sources_field(embed, message.sources)
            if is_last:
                self._set_footer(embed, message)

            webhook = DiscordWebhook(
                url=self._webhook_url,
                username=self._username,
                wait=True,
            )
            if is_first and content_preview:
                webhook.set_content(content_preview)
            webhook.add_embed(embed)
            webhooks.append(webhook)
        return webhooks

    def _build_system_notification_webhooks(
        self,
        message: BriefingMessage,
    ) -> list[DiscordWebhook]:
        """system チャンネル専用の plain-text 投稿を組み立てる (Phase 5D / 7)。

        embed を使わず、``title`` (1 行ヘッダ) + ``bluf`` (詳細) + ``analyst_note``
        (失敗時のみ、エラー先頭行) を改行区切りでそのまま content に流す。
        運用ステータスを最小ノイズで伝える。
        """
        webhook = DiscordWebhook(
            url=self._webhook_url,
            username=self._username,
            wait=True,
        )
        parts: list[str] = []
        title = (message.title or "").strip()
        if title:
            parts.append(title)
        bluf = (message.bluf or "").strip()
        # title と bluf が同一実体ならどちらか 1 つだけ表示
        if bluf and bluf != title:
            parts.append(bluf)
        note = (message.analyst_note or "").strip()
        if note:
            parts.append(f"```\n{note[:1500]}\n```")
        webhook.set_content("\n".join(parts))
        return [webhook]

    def _build_plaintext_only_webhooks(
        self,
        message: BriefingMessage,
    ) -> list[DiscordWebhook]:
        """事象を持たないセクション用 (embed なし、プレーンテキストのみ)。

        対象:
            - サマリー系 (overall summary)
            - 「該当事象なし」が明示されたセクション (例: 該当する新規事象は観測されず)

        Phase 5D 表示構造:
            🔴 📋 国家APT 全体サマリー · 🎯 8/10
            > [TL;DR 1 文 60 字]
            [本文 / digest]
            📅 観測期間
            🔗 詳細リンク
        """
        webhook = DiscordWebhook(
            url=self._webhook_url,
            username=self._username,
            wait=True,
        )
        title_line = self._build_section_title_line(message, with_count=False)
        parts: list[str] = [title_line]

        # TL;DR (L2): タイトル直下に引用形式で
        tldr = str(message.metadata.get("tldr", "") or "").strip()
        if tldr:
            parts.append(f"> {tldr}")

        # 本文: digest (bluf) + 元 summary を二重投入しないように、長い方を採用
        bluf = (message.bluf or "").strip()
        summary = (message.summary or "").strip()
        body = bluf if len(bluf) >= len(summary) else summary
        if body:
            parts.append(body)

        # 観測期間
        time_range = _format_observation_window(message)
        if time_range:
            parts.append(time_range)

        # CTA リンク
        cta_url = str(message.metadata.get("grok_chat_url", "") or "")
        if cta_url.startswith("http"):
            parts.append(f"🔗 [Grok レポートで詳細を見る →](<{cta_url}>)")

        webhook.set_content("\n".join(parts))
        return [webhook]

    def _build_multi_embed_webhooks(
        self,
        message: BriefingMessage,
    ) -> list[DiscordWebhook]:
        """multi-embed モード (BriefingMessage.incidents が非空) の組立。

        - content (plain text): セクション概観 (重要度 + カテゴリ + bluf)
        - embed 1..N: 各事象 (heading / body / 関連アクター / 信頼性 / 出典 / IOC)
        - embed N+1: 集約情報 (全出典 / 信頼性集計 / IOC まとめ / 観測期間 / CTA)

        Discord の制約: 1 webhook あたり embed 最大 10 個、合計 6000 字以内。
        多い incident は前から優先で詰め、超過分は集約 embed の備考に件数のみ残す。
        """
        webhook = DiscordWebhook(
            url=self._webhook_url,
            username=self._username,
            wait=True,
        )
        webhook.set_content(self._compose_multi_content(message))

        color = IMPORTANCE_COLORS[message.importance]
        # Discord は 1 message に embed 最大 10。先頭の事象から優先表示。
        max_incident_embeds = 10
        incidents = list(message.incidents)
        shown = incidents[:max_incident_embeds]
        truncated_count = max(0, len(incidents) - len(shown))

        last_idx = len(shown) - 1
        for idx, inc in enumerate(shown, start=1):
            embed = self._build_incident_embed(
                inc,
                idx=idx,
                color=color,
                message=message,
                # 省略件数の警告は最後の embed に付与する
                is_last=(idx - 1 == last_idx),
                truncated=truncated_count,
            )
            webhook.add_embed(embed)
        return [webhook]

    def _compose_multi_content(self, message: BriefingMessage) -> str:
        """multi-embed モードの plain text content (タイトル相当)。

        Phase 5D 構造 (プレーンテキスト、embed の上に表示):
            1. **タイトル行**: 色重要度 + カテゴリ表示名 (display_name) + 件数 + 関心度
            2. TL;DR (L2): 引用形式 1 文
            3. ダイジェスト要約 (LLM 由来の 2-3 文)
            4. 観測期間 (📅)
            5. 詳細リンク (🔗)
        """
        title_line = self._build_section_title_line(message, with_count=True)
        parts: list[str] = [title_line]

        # TL;DR (L2): タイトル直下に引用形式で 1 文
        tldr = str(message.metadata.get("tldr", "") or "").strip()
        if tldr:
            parts.append(f"> {tldr}")

        bluf = (message.bluf or "").strip()
        # bluf == tldr のケースで二重表示しないようにする
        if bluf and bluf != tldr:
            parts.append(bluf)

        # 観測期間
        time_range = _format_observation_window(message)
        if time_range:
            parts.append(time_range)

        # CTA リンク
        cta_url = str(message.metadata.get("grok_chat_url", "") or "")
        if cta_url.startswith("http"):
            parts.append(f"🔗 [Grok レポートで詳細を見る →](<{cta_url}>)")

        return "\n".join(parts)

    def _build_section_title_line(
        self,
        message: BriefingMessage,
        *,
        with_count: bool,
    ) -> str:
        """カテゴリタイトル行を組み立てる (Phase 5D 共通実装)。

        Phase 5D の改善:
        - 色重要度 (🔴/🟡/⚪) のみで重要度を伝え、`[HIGH]` のような文字タグは廃止
        - カテゴリ表示は ``metadata['category_display_name']`` (yaml SSoT) を最優先
          し、無ければ legacy ``_CATEGORY_PRESENTATION`` 辞書を使う
        - 関心度 (L3) があれば末尾に `· 🎯 N/10`
        """
        emoji = _IMPORTANCE_EMOJI.get(message.importance, "")
        # category 表示: metadata の display_name を最優先 (yaml SSoT)
        explicit = str(message.metadata.get("category_display_name", "") or "").strip()
        badge_meta = str(message.metadata.get("grok_category_badge", "") or "").strip()
        if explicit:
            cat_label = f"{badge_meta} {explicit}".strip() if badge_meta else explicit
        elif message.category in _CATEGORY_PRESENTATION:
            cat_emoji, cat_name = _CATEGORY_PRESENTATION[message.category]
            cat_label = f"{cat_emoji} {cat_name}"
        else:
            cat_label = ""

        suffix_parts: list[str] = []
        if with_count:
            n = len(message.incidents)
            if n > 0:
                suffix_parts.append(f"{n} 件の事象")
        # 関心度 (L3)
        relevance = message.metadata.get("relevance_score")
        if isinstance(relevance, int) and 0 <= relevance <= 10:
            suffix_parts.append(f"🎯 {relevance}/10")
        # 該当事象なし表示
        if message.metadata.get("grok_section_empty"):
            suffix_parts.append("該当事象なし")

        head_parts: list[str] = []
        if emoji:
            head_parts.append(emoji)
        if cat_label:
            head_parts.append(f"**{cat_label}**")
        title = " ".join(head_parts).strip()
        if suffix_parts:
            title = f"{title} · " + " · ".join(suffix_parts)
        return title.strip()

    def _build_incident_embed(
        self,
        incident: BriefingIncident,
        *,
        idx: int,
        color: int,
        message: BriefingMessage,
        is_last: bool = False,
        truncated: int = 0,
    ) -> DiscordEmbed:
        heading = (incident.heading or f"事象 {idx}").strip()
        body = (incident.body or "").strip()
        body_chunk = body[:DESC_CHUNK_SAFE]
        embed = DiscordEmbed(
            title=heading[:EMBED_TITLE_MAX],
            description=body_chunk,
            color=color,
        )
        # フィールド (inline で並べる)
        if incident.related_actor:
            embed.add_embed_field(
                name="🎯 関連アクター",
                value=incident.related_actor[:200],
                inline=True,
            )
        if incident.confidence:
            # Phase 5D / 13: 信頼性は宝石記号 (💎/🔷/🔹) で色分け
            gem = _RELIABILITY_EMOJI.get(incident.confidence, "📊")
            embed.add_embed_field(
                name=f"{gem} 信頼性",
                value=_confidence_ja(incident.confidence),
                inline=True,
            )
        # x_early_signals: 信号タイプ + 裏取り (Phase 5C)
        signal_label = _format_signal_label(incident)
        if signal_label:
            embed.add_embed_field(
                name="🛰️ シグナル",
                value=signal_label,
                inline=True,
            )
        if incident.account_class:
            embed.add_embed_field(
                name="🪪 ソース種別",
                value=_format_account_class(incident.account_class),
                inline=True,
            )
        if incident.engagement_signal:
            embed.add_embed_field(
                name="📣 反応",
                value=incident.engagement_signal,
                inline=True,
            )
        if incident.is_new is True:
            embed.add_embed_field(name="⚡ 状態", value="新規", inline=True)
        elif incident.is_new is False:
            embed.add_embed_field(name="🔄 状態", value="継続", inline=True)
        if incident.iocs:
            embed.add_embed_field(
                name="🔍 IOC",
                value=_format_iocs(list(incident.iocs)),
                inline=False,
            )
        if incident.ttps:
            embed.add_embed_field(
                name="🛡️ MITRE ATT&CK",
                value=_format_mitre_techniques(list(incident.ttps)),
                inline=False,
            )
        if incident.sources:
            embed.add_embed_field(
                name="📎 出典",
                value=_format_sources(list(incident.sources)),
                inline=False,
            )
        # x_early_signals: 推奨フォローアップ (Phase 5C)
        if incident.suggested_followup:
            embed.add_embed_field(
                name="🧭 次のアクション",
                value=incident.suggested_followup[:300],
                inline=False,
            )
        # 各事象 embed は本文 + 事象メタのみに集中。
        # author / 観測期間 / CTA / footer はすべてプレーンテキスト content に集約済
        # (Grok 元レポートに author 概念がないため擬似メタを embed に入れない)。
        # 省略件数があれば最後の embed にだけ警告
        if is_last and truncated > 0:
            embed.add_embed_field(
                name="⚠️ 省略",
                value=(f"残り {truncated} 件の事象は省略 (上限 {len(message.incidents)} 件中表示)"),
                inline=False,
            )
        return embed

    def _compose_content_preview(self, message: BriefingMessage) -> str:
        """通知プレビュー / モバイル用の 1 行ヘッダ (Phase 5D 改修)。

        色重要度絵文字 + カテゴリラベル + title + 相対時刻 を 1 行に。
        ``metadata['category_display_name']`` を優先 (Phase 5D / B)。
        ``[HIGH]`` のような文字タグは廃止 (色で表現)。
        """
        emoji = _IMPORTANCE_EMOJI.get(message.importance, "")
        explicit = str(message.metadata.get("category_display_name", "") or "").strip()
        badge_meta = str(message.metadata.get("grok_category_badge", "") or "").strip()

        if explicit:
            tag = f"[{badge_meta} {explicit}]" if badge_meta else f"[{explicit}]"
        elif message.category == "japan_targeted":
            tag = "[🇯🇵 日本標的]"
        elif message.category in _CATEGORY_PRESENTATION:
            cat_emoji, cat_name = _CATEGORY_PRESENTATION[message.category]
            tag = f"[{cat_emoji} {_short_category(cat_name)}]"
        else:
            # 不明カテゴリ: 色記号のみで重要度を伝える (タグなし)
            tag = ""

        title = _trim_one_line(_apply_followup_prefix(message), 180)
        relative_ts = _format_relative_timestamp(message)
        suffix = f" · {relative_ts}" if relative_ts else ""
        head = f"{emoji} {tag}".strip() if tag else emoji
        return f"{head} {title}{suffix}".strip()

    def _compose_description(self, message: BriefingMessage) -> str:
        """description (本文) を組み立てる。

        Phase 2.8a/2.8a2:
        - BLUF が summary に含まれる場合は重複表示を避ける
        - 「ラベル：値」形式の属性行を ``**ラベル**: 値`` に変換
        - 段落間に空行を挿入して可読性を上げる (Discord は ``\\n\\n`` を段落として描画)
        Phase 5D:
        - TL;DR が metadata にあれば description 冒頭に引用形式で 1 行
        """
        bluf = (message.bluf or "").strip()
        summary = (message.summary or "").strip()
        tldr = str(message.metadata.get("tldr", "") or "").strip()
        parts: list[str] = []
        if tldr and tldr != bluf:
            parts.append(f"> {tldr}")
            parts.append("")

        if bluf and summary and _is_bluf_redundant_with_summary(bluf, summary):
            # title (= 重要度絵文字 + 短縮 BLUF) と description 冒頭・各行の
            # 重複削減のため、raw summary 内で title 本文と一致する prefix を
            # 持つ行から重複を取り除いた上で整形する。
            trimmed_summary = _trim_overlap_with_title(summary, message.title)
            formatted_summary = _format_paragraphs(trimmed_summary)
            if formatted_summary:
                parts.append(formatted_summary)
        elif bluf and summary:
            formatted_summary = _format_paragraphs(summary)
            parts.append(f"**{bluf}**")
            if formatted_summary:
                parts.append("")
                parts.append(formatted_summary)
        elif bluf:
            parts.append(f"**{bluf}**")
        elif summary:
            formatted_summary = _format_paragraphs(summary)
            if formatted_summary:
                parts.append(formatted_summary)

        # アナリスト所見が field と完全に重複する集計情報のみ (例:
        # "アクター: X / 信頼性: high=2") なら description では省く。
        # それ以外 (人間の所感 / 推奨対応 等) は太字ラベルで表示。
        if message.analyst_note and not _is_field_only_note(message.analyst_note):
            parts.append("")
            parts.append(f"**アナリスト所見**: {message.analyst_note}")

        # Phase 2.8a: CTA リンク (Grok レポート / 元 URL に最短アクセス)
        cta_url = str(message.metadata.get("grok_chat_url", "") or "")
        if cta_url.startswith("http"):
            parts.append("")
            parts.append(f"🔗 [Grok レポートで詳細を見る →]({cta_url})")

        return "\n".join(parts)

    def _set_author(self, embed: DiscordEmbed, message: BriefingMessage) -> None:
        # author 表示は yaml SSoT の display_name (metadata) を最優先。
        # legacy 経路で metadata 未設定の場合のみ _CATEGORY_PRESENTATION にフォールバック。
        explicit = str(message.metadata.get("category_display_name", "") or "").strip()
        badge_meta = str(message.metadata.get("grok_category_badge", "") or "").strip()
        if explicit:
            cat_label = f"{badge_meta} {explicit}".strip() if badge_meta else explicit
        elif message.category in _CATEGORY_PRESENTATION:
            cat_emoji, cat_name = _CATEGORY_PRESENTATION[message.category]
            cat_label = f"{cat_emoji} {cat_name}"
        else:
            return  # 不明カテゴリは author を設定しない (中途半端表示の回避)

        # Phase 2.8a: incident 数 + Phase 5D: relevance score を author に併記
        incident_count = _safe_int(message.metadata.get("incident_count"))
        parts = [cat_label]
        if incident_count > 0:
            parts.append(f"{incident_count} incidents")
        relevance = message.metadata.get("relevance_score")
        if isinstance(relevance, int) and 0 <= relevance <= 10:
            parts.append(f"🎯 {relevance}/10")
        name = " · ".join(parts)

        chat_url = str(message.metadata.get("grok_chat_url", "") or "")
        kwargs: dict[str, Any] = {"name": name[:256]}
        if chat_url.startswith("http"):
            kwargs["url"] = chat_url
        embed.set_author(**kwargs)

    def _set_footer(self, embed: DiscordEmbed, message: BriefingMessage) -> None:
        section_label = str(message.metadata.get("grok_section", "") or "")
        run_id = message.metadata.get("grok_email_uid", "") or message.metadata.get("run_id", "")
        feed_title = "CTI Briefing"
        bits: list[str] = [feed_title]
        if section_label:
            bits.append(section_label)
        # Phase 2.8a: 観測時間レンジ (受信時刻 ± 24h) を追記
        time_range = _format_observation_window(message)
        if time_range:
            bits.append(time_range)
        # Phase B-续报: 過去 7 日内の同 dedup_key 投稿があれば「前報 N 件」付記
        fi = message.metadata.get("followup_info")
        if isinstance(fi, dict) and int(fi.get("prior_count") or 0) > 0:
            prior_at = str(fi.get("prior_at_iso") or "")[:10]
            n = int(fi["prior_count"])
            bits.append(f"🔁 続報 (前報 {n} 件 · 最終 {prior_at})")
        if run_id:
            bits.append(f"id: {str(run_id)[:32]}")
        embed.set_footer(text=" · ".join(bits))

    def _set_timestamp(self, embed: DiscordEmbed, message: BriefingMessage) -> None:
        ts = _resolve_timestamp(message)
        if ts is not None:
            embed.set_timestamp(ts.timestamp())

    def _add_metadata_fields(
        self,
        embed: DiscordEmbed,
        message: BriefingMessage,
    ) -> None:
        # アクター / 信頼性 を inline で出す。観測時刻は footer + embed.timestamp
        # で表現するため field では出さない (Phase 2.8a で重複削除)。
        actor = _extract_actor(message)
        confidence = _extract_confidence(message)

        if actor:
            embed.add_embed_field(name="🎯 アクター", value=actor[:200], inline=True)
        if confidence:
            embed.add_embed_field(
                name="📊 信頼性", value=_confidence_ja(confidence)[:200], inline=True
            )

        if message.mitre_techniques:
            embed.add_embed_field(
                name="🛡️ MITRE ATT&CK",
                value=_format_mitre_techniques(message.mitre_techniques),
                inline=False,
            )
        if message.iocs:
            embed.add_embed_field(
                name="🔍 IOC",
                value=_format_iocs(message.iocs),
                inline=False,
            )

    def _add_sources_field(
        self,
        embed: DiscordEmbed,
        sources: list[Source],
    ) -> None:
        embed.add_embed_field(
            name="📎 出典",
            value=_format_sources(sources),
            inline=False,
        )


# ---------- 整形ヘルパ ----------


# x_early_signals の signal_type / corroboration を 1 行で表示する補助 (Phase 5C)
_SIGNAL_TYPE_LABEL: dict[str, str] = {
    "observation": "観測",
    "claim": "主張",
    "speculation": "推測",
    "confirmation": "確認",
}
_CORROBORATION_LABEL: dict[str, str] = {
    "multi_source": "複数裏取り",
    "single_source": "単一ソース",
    "unverified": "未確認",
}
_ACCOUNT_CLASS_LABEL: dict[str, str] = {
    "vendor_official": "ベンダー公式",
    "analyst_known": "著名研究者",
    "analyst_unknown": "未確認研究者",
    "aggregator": "アグリゲータ",
    "affected_party": "当事者",
}


def _format_signal_label(incident: BriefingIncident) -> str:
    """signal_type / corroboration を Discord embed field 用 1 行に整形。"""
    parts: list[str] = []
    if incident.signal_type:
        parts.append(_SIGNAL_TYPE_LABEL.get(incident.signal_type, incident.signal_type))
    if incident.corroboration:
        parts.append(
            _CORROBORATION_LABEL.get(incident.corroboration, incident.corroboration),
        )
    return " · ".join(parts)


def _format_account_class(account_class: str) -> str:
    """account_class の英値を日本語ラベルに。未知値はそのまま表示。"""
    return _ACCOUNT_CLASS_LABEL.get(account_class, account_class)


# field で表現済みの「アクター:」「信頼性:」のみで構成された analyst_note を
# 検出して description から省くための判定パターン。
# 例:
#   "アクター: APT34 / 信頼性: high=2"  → True (冗長)
#   "信頼性: high=1"                    → True (冗長)
#   "セキュリティベンダーへの攻撃は注意"  → False (実質的な所見)
_REDUNDANT_NOTE_RE = re.compile(
    r"^\s*"
    r"(?:アクター\s*[:：][^/]*)?"
    r"\s*(?:/\s*)?"
    r"(?:信頼性\s*[:：][^/]*)?"
    r"\s*$",
)


def _is_field_only_note(note: str) -> bool:
    """analyst_note が field と重複する集計のみで構成されているかを判定。"""
    stripped = note.strip()
    if not stripped:
        return True
    # 完全マッチ + どちらかのキーワードを含む (空行マッチ防止)
    if "アクター" not in stripped and "信頼性" not in stripped:
        return False
    return bool(_REDUNDANT_NOTE_RE.fullmatch(stripped))


def _trim_overlap_with_title(summary: str, title: str) -> str:
    """summary 内で title (短縮 BLUF) と一致する prefix を持つ行から重複を削除する。

    title は ``🔴 ...内容... [末尾省略時は…]`` の形。比較時は重要度絵文字と
    末尾の省略記号を除去する。

    重複削除は段階的:
    - **完全一致行** は title 長に関わらず常に削除 (誤削除リスクなし)
    - **prefix 一致** (line が title で始まり、続きがある) は title が
      30 字以上のときのみ削除 (短い title では他の意味の文との偶発一致を避ける)

    なお ``事象N：`` を含む行は **触らない** (事象見出しの構造を残すため)。
    BLUF はセクション概観であって事象の内容そのものではないので、
    通常は事象本文と一致しないが、誤削除を避けるためここでも保護する。
    """
    if not summary or not title:
        return summary
    # 重要度絵文字の prefix を除去
    compare = re.sub(r"^[🔴🟡⚪]\s*", "", title).strip()
    # 末尾の省略記号 (… や ...) を除去
    compare = compare.rstrip("…").rstrip(".").strip()
    if not compare:
        return summary

    allow_prefix_trim = len(compare) >= 30

    lines = summary.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        # 事象見出しを含む行は構造保持のため触らない
        if _INCIDENT_HEAD_LINE_RE.match(stripped):
            out.append(line)
            continue
        # 完全一致 → 行ごと削除 (短い title でも安全)
        if stripped == compare:
            continue
        # prefix 一致 → prefix を削除して残りを保持 (誤削除を避けるため長め要求)
        if allow_prefix_trim and stripped.startswith(compare):
            remainder = stripped[len(compare) :].lstrip()
            if remainder:
                indent_len = len(line) - len(line.lstrip())
                indent = line[:indent_len]
                out.append(indent + remainder)
            continue
        out.append(line)
    # 末尾の空行を削除
    return "\n".join(out).strip("\n")


def _format_paragraphs(text: str) -> str:
    """Grok 本文を Discord で読みやすい段落構造に整形する (Phase 2.8a2)。

    1. 各行をトリムし空行は捨てる
    2. ``ラベル：値`` 形式の行は ``**ラベル**: 値`` に置換 (太字化)
    3. 行と行の間に空行を入れて Discord の段落区切りとして描画させる
    """
    if not text:
        return ""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(_format_attribute_line(line))
    return "\n\n".join(out)


# 属性ラベルの最大長 (短すぎ・長すぎを除外して誤検出を抑える)
_LABEL_MIN_CHARS = 2
_LABEL_MAX_CHARS = 40
# 行頭が箇条書き / 補足のときは属性行扱いしない (二重太字を防ぐ)
_LIST_PREFIXES: tuple[str, ...] = ("-", "・", "*", "▪", "▫", ">", "#")

# Grok のタスク 2 形式で section.body に含まれる「事象N:」見出しを検出する。
# これらは Discord の h3 (### ...) に変換して視覚的なセパレーターにする。
_INCIDENT_HEAD_LINE_RE = re.compile(r"^事象\s*(\d+)\s*[:：]\s*(.*)$")


def _format_attribute_line(line: str) -> str:
    """``ラベル：値`` を ``**ラベル**: 値`` に変換する。

    特殊ケース:
      - ``事象N:`` の行は Discord の h3 見出し (``### 事象 N``) に変換し、
        値部分は次行に分離 (タスク 2 の事象セパレーターとして機能)。

    マッチしない場合や、行頭がリスト記号で始まる場合はそのまま返す。
    """
    stripped = line.lstrip()
    if not stripped:
        return line
    if stripped[0] in _LIST_PREFIXES:
        return line
    # 「事象 N:」見出しを Discord の h3 に変換 (タスク 2 専用)
    m_incident = _INCIDENT_HEAD_LINE_RE.match(stripped)
    if m_incident:
        num = m_incident.group(1)
        body = m_incident.group(2).strip()
        return f"### 事象 {num}\n{body}" if body else f"### 事象 {num}"
    # 全角コロン or 半角コロンで分割
    for sep in ("：", ":"):
        idx = stripped.find(sep)
        if idx == -1:
            continue
        label = stripped[:idx].strip()
        value = stripped[idx + 1 :].strip()
        # value が URL スキーマ (//...) で始まる場合は url らしいので変換しない
        if value.startswith("//"):
            return line
        if (
            _LABEL_MIN_CHARS <= len(label) <= _LABEL_MAX_CHARS
            and value
            and "\n" not in label
            # ラベルに http が含まれているのは ほぼ確実に URL 行
            and "http" not in label.lower()
        ):
            return f"**{label}**: {value}"
        return line
    return line


def _is_bluf_redundant_with_summary(bluf: str, summary: str) -> bool:
    """BLUF と summary が「事実上同じ文章」かを判定する。

    Grok レポートの summary セクションのように 1 段落しかない場合、
    BLUF = summary 全文の冒頭 N 文字 になり、両方表示すると重複する。
    """
    if not bluf or not summary:
        return False
    bluf_n = _normalize_for_comparison(bluf)
    summary_n = _normalize_for_comparison(summary)
    if not bluf_n or not summary_n:
        return False
    # 1) BLUF が summary に丸ごと substring 含まれる
    if bluf_n in summary_n:
        return True
    # 2) summary が BLUF に丸ごと含まれる (短い summary)
    if summary_n in bluf_n:
        return True
    # 3) 先頭 60 文字が一致 → ほぼ同じ書き出し
    head_len = min(60, len(bluf_n), len(summary_n))
    return head_len >= 30 and bluf_n[:head_len] == summary_n[:head_len]


def _normalize_for_comparison(text: str) -> str:
    """空白・改行を畳んで比較用に正規化する。"""
    return re.sub(r"\s+", "", text)


def _short_category(full_name: str) -> str:
    """カテゴリ表示名を content プレビュー用に短くする。

    例: "中国関連 (MSS / PLA直轄系)" -> "中国関連"
    """
    # `(` または `（` 以降を切り捨て
    for sep in ("(", "（"):
        if sep in full_name:
            return full_name.split(sep, 1)[0].strip()
    return full_name


def _trim_one_line(text: str, max_chars: int) -> str:
    """改行を空白に置換 + 長すぎる場合は省略記号で切る。"""
    if not text:
        return ""
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "…"


def _apply_followup_prefix(message: BriefingMessage) -> str:
    """Phase B-续报: ``message.metadata["followup_info"]`` があれば ``🔁 [続報] `` を prefix。

    続報情報が無い場合は元の title をそのまま返す。
    """
    base = (message.title or "").strip()
    fi = message.metadata.get("followup_info")
    if isinstance(fi, dict) and int(fi.get("prior_count") or 0) > 0:
        return f"🔁 [続報] {base}"
    return base


def _resolve_timestamp(message: BriefingMessage) -> datetime | None:
    """metadata から ``received_at`` 等を取り出して datetime にする。"""
    raw = message.metadata.get("received_at") or message.metadata.get("grok_received_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _format_relative_timestamp(message: BriefingMessage) -> str:
    """Discord の `<t:UNIX:R>` (相対時刻表示) を返す。なければ空文字。"""
    ts = _resolve_timestamp(message)
    if ts is None:
        return ""
    return f"<t:{int(ts.timestamp())}:{_TS_RELATIVE}>"


def _format_observed_timestamp(message: BriefingMessage) -> str:
    """observed (受信) 時刻を Discord 自動変換 timestamp で返す。"""
    ts = _resolve_timestamp(message)
    if ts is None:
        return ""
    # `<t:UNIX:f>` でユーザの TZ で「2026年5月2日 06:35」表示
    return f"<t:{int(ts.timestamp())}:{_TS_FULL_SHORT}>"


def _format_observation_window(message: BriefingMessage) -> str:
    """Grok レポート対象の観測ウィンドウ (受信時刻 ± 24h) を JST 表記で返す。

    例: ``📅 04/30 06:00〜05/01 06:00 JST``
    """
    end = _resolve_timestamp(message)
    if end is None:
        return ""
    try:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        jst = ZoneInfo("Asia/Tokyo")
        end_jst = end.astimezone(jst)
        start_jst = end_jst - timedelta(hours=24)
    except Exception:  # noqa: BLE001
        return ""
    return f"📅 {start_jst:%m/%d %H:%M}〜{end_jst:%m/%d %H:%M} JST"


def _safe_int(value: object) -> int:
    """metadata の任意値を int に安全変換。失敗時は 0。"""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _extract_actor(message: BriefingMessage) -> str:
    """analyst_note や metadata から アクター情報を抽出する。"""
    explicit = message.metadata.get("actor")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    note = message.analyst_note or ""
    if "アクター:" in note:
        # "アクター: APT34, 各種犯罪集団 / 信頼性: high=2"
        head = note.split("/")[0]
        return head.split("アクター:", 1)[1].strip()
    return ""


def _extract_confidence(message: BriefingMessage) -> str:
    """analyst_note から信頼性集計を抽出する。"""
    explicit = message.metadata.get("confidence")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    note = message.analyst_note or ""
    if "信頼性:" in note:
        # "アクター: ... / 信頼性: high=2, medium=1"
        tail = note.rsplit("信頼性:", 1)[1]
        return tail.split("/", 1)[0].strip()
    return ""


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """改行境界を尊重して text を max_chars 以下のチャンクに分ける。"""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _mitre_url(technique_id: str) -> str | None:
    """``T1566.001`` -> ``https://attack.mitre.org/techniques/T1566/001/``"""
    match = ATTACK_TECHNIQUE_PATTERN.match(technique_id.strip())
    if not match:
        return None
    base = f"T{match.group(1)}"
    sub = match.group(2)
    suffix = f"/{sub}" if sub else ""
    return DiscordPublisher._MITRE_URL_FMT.format(base=base, suffix=suffix)


def _format_mitre_techniques(techniques: list[str]) -> str:
    """MITRE テクニック ID を [T1566.001](url) 形式のリストに。"""
    parts: list[str] = []
    used = 0
    for tid in techniques:
        url = _mitre_url(tid)
        chunk = f"[{tid}]({url})" if url else tid
        if used + len(chunk) + 2 > FIELD_VALUE_SAFE:
            parts.append(f"…他 {len(techniques) - len(parts)} 件")
            break
        parts.append(chunk)
        used += len(chunk) + 2  # ", "
    return ", ".join(parts) if parts else "-"


def _format_iocs(iocs: list[str]) -> str:
    """IOC をコードブロック形式に。1024 字制限内に収める。"""
    fence_overhead = len("```\n\n```")
    body_lines: list[str] = []
    used = fence_overhead
    for ioc in iocs:
        line_len = len(ioc) + 1
        if used + line_len > FIELD_VALUE_SAFE:
            body_lines.append(f"... 他 {len(iocs) - len(body_lines)} 件")
            break
        body_lines.append(ioc)
        used += line_len
    return "```\n" + "\n".join(body_lines) + "\n```"


def _format_sources(sources: list[Source]) -> str:
    """Sources を `- [title](url) (lang)` のリストに。"""
    lines: list[str] = []
    used = 0
    for src in sources:
        title = src.title[:80]
        line = f"- [{title}]({src.url}) ({src.language})"
        if used + len(line) + 1 > FIELD_VALUE_SAFE:
            lines.append(f"... 他 {len(sources) - len(lines)} 件")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
