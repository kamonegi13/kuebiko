"""本文/ソース取得の User-Agent 単一ソース (2026-07-27)。

docs/body_extraction_and_entity_integrity_redesign.md §2.2。

陳腐化した UA そのものが WAF のボット署名になり全文取得が 403 で無音失敗していた
(GBHackers 953/953=100% 切り株)。UA 定義が fetch 経路ごとに乱立していると、片方だけ
修正しても購読ソース追加のプレビューや別 watcher が古い UA (Chrome/120 等) のまま 403 で
弾かれる。**ブラウザ相当の UA が要る経路はすべてこの単一関数を参照**し、UA 自己修復ジョブ
(ua_health) が更新する ``.env`` の ``CONTENT_EXTRACTOR_USER_AGENT`` を一点で共有する。

- ``browser_user_agent()`` = 構築時/呼出時に env を解決 (module load 時 1 回束縛だと自己修復の
  更新が再起動まで効かないため)。ブラウザに見せる必要のある経路 (本文抽出 / ソース追加
  プレビュー / JS レンダ watcher) が使う。
- ボット UA は RSS/sitemap 取得の**意図的な**礼儀正しい役割名だけの識別
  (feed/sitemap は通常ボットを許可する)。WAF がボットを弾く feed はソース追加の
  ブラウザ fallback で救済する。

**ボット UA の SSoT は ``bot_user_agent()`` 1 箇所** (2026-10-30)。プロジェクト名・URL を
含まない汎用の feed fetcher 語 (``NEUTRAL_BOT_UA``) を送る。旧 ``kuebiko/1.0 (+役割)`` は
公開リポ名を相手のログに残すため既定で廃止。``BOT_UA_NEUTRAL=0`` で旧形式に戻る。
"""

from __future__ import annotations

import os

ENV_USER_AGENT = "CONTENT_EXTRACTOR_USER_AGENT"
ENV_BOT_UA_NEUTRAL = "BOT_UA_NEUTRAL"

# プロジェクト名・URL・運用者情報を含まない汎用のボット識別 (全 fetch 経路で共通)。
# 既知のクローラ (Googlebot / Feedfetcher 等) に似せない、一般的な RSS クライアント風の文字列。
NEUTRAL_BOT_UA = "RSS-Client/1.0"
# 旧識別の接頭辞 (BOT_UA_NEUTRAL=0 の戻し先、および呼出側が旧文字列を渡した場合の検出用)。
_LEGACY_BOT_UA_PREFIX = "kuebiko/1.0"

# 既定の Chrome メジャーバージョン (env / 自己修復ジョブ未設定時の floor)。
DEFAULT_CHROME_MAJOR = 133
_DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/{DEFAULT_CHROME_MAJOR}.0.0.0 Safari/537.36"
)

# 現実的なブラウザ相当のリクエストヘッダ (UA 単独より WAF 通過率が上がる)。
BROWSER_HEADERS: dict[str, str] = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
}


def browser_user_agent() -> str:
    """ブラウザ相当 UA を解決 (.env の CONTENT_EXTRACTOR_USER_AGENT があれば優先)。

    **呼出時**に env を読む — 自己修復ジョブ (ua_health) が .env + os.environ を更新すると、
    以後の fetch がすべて新 UA を拾う (再起動不要)。
    """
    override = (os.environ.get(ENV_USER_AGENT) or "").strip()
    return override or _DEFAULT_CHROME_UA


def bot_ua_neutral_enabled() -> bool:
    """中立ボット UA を使うか (``BOT_UA_NEUTRAL``、既定 1)。呼出時に env を読む。"""
    return (os.environ.get(ENV_BOT_UA_NEUTRAL) or "1").strip() != "0"


def bot_user_agent(role: str = "rss-fetcher") -> str:
    """ボット UA を返す (SSoT)。既定は役割名も載せない中立文字列。

    ``BOT_UA_NEUTRAL=0`` のときだけ旧 ``kuebiko/1.0 (+role)`` を返す (戻し用)。
    """
    if bot_ua_neutral_enabled():
        return NEUTRAL_BOT_UA
    return f"{_LEGACY_BOT_UA_PREFIX} (+{role})"


def resolve_bot_identity(identity: str) -> str:
    """呼出側が持つ identity を現行方針に合わせる。

    中立モードで旧形式 (``kuebiko/1.0 …``) が渡されたら中立 UA に置き換える
    (永続設定や古い呼出が旧文字列を持っていても外へ出さない)。
    """
    if bot_ua_neutral_enabled() and identity.lower().startswith(_LEGACY_BOT_UA_PREFIX):
        return NEUTRAL_BOT_UA
    return identity
