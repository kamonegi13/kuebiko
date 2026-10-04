"""運用画面の写し (旧 Tier1、2026-10-04 に匿名公開へ移行) を静的ファイルへ書き出す。

目的は **Mac に到達できないときの継続**。ライブの代わりではなく、
「その時点の写し」を別経路で読めるようにする (2026-08-29 利用者提案)。

⭐ **2026-10-04 利用者決定: 写しは常時匿名公開**。Cloudflare Access の認証を外し、
誰でも読める面にする。これにより書き出せるものの線引きが変わった — 旧 Tier1
(限られた要員のみ認証済みで読む) では記事本文を含めていたが、匿名公開ではもう
その前提が成り立たない。以下は **無条件** に書き出さない:
    - 記事・事象ニュースの本文 (body / body_ja)。再配布の禁止は Tier0 (匿名公開サイト)
      だけの話ではなくなった。事象ニュース詳細は構成記事を入れ子で持つため、
      再帰的に剥がす (`_strip_forbidden`)
    - メモ・ブックマーク (`/api/v1/notes`) — 個人の作業メモ
    - 購読ソース一覧 (`/api/v1/subscriptions`) — フィード URL の列挙
    - Grok 関連 (`/api/v1/grok/tasks` `/api/v1/grok/session` `/api/v1/grok-mail/health`)
    - アクター別名などの承認待ち提案 (`/api/v1/actors/sync`) — レビューキュー
    - チャンネル設定 (`/api/v1/channels`) / `/api/v1/runtime-flags` — webhook の
      環境変数名・認証状態等の運用情報。画面の起動関門には要るので**同形の
      空スタブ**を書く (稼働中の値は取得しない)
    - ジョブ計画 / 実行履歴 (`dashboard/summary` の `recent_runs` / `next_run_at`
      のような「いま」を知る情報)・レビューキュー・モデルティア・鍵 — §12 の
      ままローカル専用
    - 設定 / プロンプト — DB が SSoT。古い写しは編集判断を誤らせる
    - 分析チャット / 翻訳 — LLM 依存 (外へ出せない)

⭐ **稼働中の API を HTTP で叩いて写す**。同じ関数を import する方式だと、
書き出し側の環境が違ったときに静かに別物を出す (2026-08-26 に公開サイトで、
DATABASE_URL の無いホストで実行して空の SQLite にフォールバックし、
0 件のサイトを「成功」として配信した)。HTTP なら **利用者と同じ経路**を通るので、
環境差がそもそも生まれない。アプリが落ちていれば失敗する = 静かに壊れない。

出力:
    meta.json            生成時刻・件数・with_bodies=false 固定 (画面が写しの注記を出すのに使う)
    articles.json        記事一覧 (直近 N 日、本文なし)
    articles/<id>.json   記事の詳細 (本文なし)
    eventnews.json       事象ニュース一覧
    eventnews/<id>.json  事象ニュースの詳細 (構成記事も本文なし)
    api/<hash>.json      その他の画面 API (ダッシュボード・週次深掘り含む)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

#: 記事の窓。Pages の 1 デプロイ 20,000 ファイル制限に対する余裕を保つ
#: (30 日で約 8,300 件。90 日にすると 28,000 件で超える)。
DEFAULT_DAYS = 30
#: 一覧 API の 1 ページ上限 (API 側の制約と同じ値)
_PAGE = 200
#: これを下回ったら書き出しを失敗させる。**空を配信しないための最後の関門**。
#: 環境を揃えるだけでは再発する (公開サイトで実証済み) ので件数でも守る。
#: 取りすぎの歯止め。Pages は 1 デプロイ 20,000 ファイルまで。
MAX_ARTICLES = 9000
MAX_EVENTS = 4000
DEFAULT_MIN_ARTICLES = 100
DEFAULT_MIN_EVENTS = 50

#: 画面が起動時・描画時に必ず引く小さな参照データ。
#:
#: ⭐ これを写さないと、写しは記事があっても**表示できない**。語彙 (value→日本語ラベル)
#: は起動関門になっていて、解決するまで本体を描かないため、取得が失敗し続けると
#: 読み込み中のまま固まる (2026-08-29 の実障害)。ラベルが無いと生の enum が出るので、
#: 「関門を外して先へ進む」は解にならない。
#:
#: いずれも数十 KB 以下で、絞り込み条件を持たない全体一覧。
#:
#: ⚠ **記事本文は常に書き出さない** (2026-10-04、写しの匿名公開化に合わせて確定)。
#: 旧 Tier1 (限られた要員のみ認証済みで読む) では「本文が無いと単独媒体の事象は
#: 要約しか読めない」を理由に本文を含めていたが、匿名公開では再配布の禁止が
#: 公開サイト (旧 Tier0) と同じ扱いになる。本文除去は `_strip_forbidden` が
#: 再帰的に行う (事象ニュース詳細の入れ子な構成記事も含む)。
#: 公開サイト側の禁止は export_public_site.py の _FORBIDDEN_KEYS が別に守っている
#: (こちらを緩めても向こうは緩まない — 関門は面ごとに独立している)。
#: 記事一覧の絞り込み。**列挙できる facet だけ** を、既定の組み合わせ 1 段で写す。
#:
#: ⭐ 絞り込みの意味をブラウザ側で書き直さない。サーバは本文まで含めて検索し、
#: entity (CVE/マルウェア/アクター) は別表を引く。写し側で真似ると **黙って
#: 少なく返す** 実装になり、利用者からは違いが見えない。写せる組み合わせだけを
#: そのまま持ち、外れたら 501 = 「写しに含まれていません」と出す。
#: ⚠ 語彙 (vocabularies) の値ではなく、**画面の選択肢** に合わせる。
#: 画面はカテゴリ群 (vuln / threat / incident_breach) も同じ引数で送るため、
#: 語彙だけ見て並べると実際に押される値と噛み合わない (実測でずれた)。
#: SSoT は frontend/src/components/news/facets.tsx の useFacetOptions。
_ARTICLE_CATEGORIES = (
    "vuln",
    "threat",
    "incident_breach",
    "vulnerability",
    "breach",
    "malware",
    "apt",
    "geopolitical",
    "policy",
    "research",
    "advisory",
)
_ARTICLE_IMPORTANCE = ("high", "medium", "low")

#: 期間の選択肢 (frontend/src/state/filters.ts の FilterState と対)。
_TIMES = ("7", "30", "90", "365")

#: ダッシュボードの対象期間 (frontend/src/pages/dashboard/overviewWindow.ts の
#: WINDOW_CHOICES と対)。14 は日次投稿推移 widget の既定 (DAYS_OPTION、7/14/30)。
_DASHBOARD_DAYS = ("1", "7", "14", "30", "90")

#: ダッシュボードの国家情勢・脅威マップ widget は共有窓 (1/7/30/90) に連動するが、
#: メインの News/脅威マップ画面は "1" (24h) を選択肢に持たない (frontend/src/state/
#: filters.ts)。_TIMES を汚さずダッシュボード側だけ "1" を足す。
_DASHBOARD_TIMES = ("1", *_TIMES)

#: 週次深掘りの取得本数。DeepDivePage の初期表示 (8) + 「さらに前の週を表示」
#: (8 刻み) + backend 上限 (deep_dives_api._MAX_WEEKS=52)。
_DEEPDIVE_WEEKS = ("8", "16", "26", "52")

#: 画面ごとの取得。**稼働中の運用画面を実際に開いて記録した** ものに基づく
#: (推測で並べると、足りない 1 本が「読み込み中で固まる」形で表に出る)。
#:
#: 絞り込みは既定の組み合わせだけを写す。地図の脅威種別・出典状態のような facet は
#: 掛け合わせると爆発するので持たない — 写しで動かすと 501 になり、画面に
#: 「写しに含まれていません」と出る (黙って固まるよりよい)。
SCREEN_ENDPOINTS: tuple[str, ...] = (
    # 現況 (総括)
    *(f"/api/v1/intel-graph/synthesis?period_type={p}" for p in ("daily", "weekly", "monthly")),
    *(f"/api/v1/intel-graph/snapshot?time={t}" for t in _TIMES),
    # 脅威アクター
    *(f"/api/v1/intel-graph/threats?time={t}" for t in _TIMES),
    # 国家情勢 (国家情勢タブは _TIMES、ダッシュボード widget は共有窓の "1" も要る)
    *(f"/api/v1/intel-graph/situation?time={t}" for t in _TIMES),
    *(f"/api/v1/intel-graph/situation/nations?time={t}" for t in _DASHBOARD_TIMES),
    # 将来予測
    *(f"/api/v1/intel-graph/forecast?weeks={w}" for w in ("4", "8", "12")),
    # 重要インフラ脅威 ("0"=JPCI_DAYS_OPTION の全期間、"1"=共有窓の 24h 固定)
    *(f"/api/v1/jp-ci-board?days={d}" for d in (*_DASHBOARD_TIMES, "0")),
    # 脅威マップ (facet は既定のみ。ダッシュボードの mini_map/geo_ranking widget が
    # 共有窓の "1" も叩くため _DASHBOARD_TIMES を使う)
    *(
        f"/api/v1/geo/cyber-map?days={d}&threat_class=all&source_status=all"
        "&min_importance=medium_up&pmesii=all&time_basis=report"
        for d in _DASHBOARD_TIMES
    ),
    *(
        f"/api/v1/geo/sub-country-points?days={d}&threat_class=all&source_status=all"
        "&min_importance=medium_up&time_basis=report"
        for d in _DASHBOARD_TIMES
    ),
    *(
        f"/api/v1/geo/trend?days={d}&threat_class=all&group_by=country&domain=cyber"
        "&source_status=all&min_importance=medium_up&pmesii=all"
        for d in _TIMES
    ),
    # 重要インフラ (事業者一覧)
    "/api/v1/jp-ci-operators",
    # コンテンツ: アクター辞書
    # ⚠ ブックマーク・メモ (/notes) / 購読ソース一覧 (/subscriptions) / Grok 関連
    # (/grok/tasks, /grok/session, /grok-mail/health) / アクター承認待ち提案
    # (/actors/sync、レビューキュー) は匿名公開のため書き出さない (2026-10-04)。
    "/api/v1/actors",
    "/api/v1/actors/observed-summary",
    # ニュース検索 (既定 + 列挙できる facet 1 段)
    "/api/v1/articles?status=posted&limit=30",
    *(f"/api/v1/articles?category={c}&status=posted&limit=30" for c in _ARTICLE_CATEGORIES),
    *(f"/api/v1/articles?importance={i}&status=posted&limit=30" for i in _ARTICLE_IMPORTANCE),
    # ダッシュボードの「記事フィード」widget (news_feed) の既定設定 (defaultConfig:
    # {mode: "summary", per: 5}、絞り込みは未指定) が実際に送る query。他の facet
    # 組み合わせとは limit/include_summary だけが違うため別行で持つ
    # (frontend/src/pages/dashboard/widgets/articles.tsx の articlesApi.list 呼び出しと
    # 一字一句同じ順序・値でなければハッシュが合わない)。
    "/api/v1/articles?status=posted&limit=5&include_summary=1",
    # PIR (常設の問い) — 画面「PIR (問い)」が読む一覧 (2026-10-04、写しに無く読み込みに失敗していた)
    "/api/v1/questions",
    # PIR / Spotlight
    "/api/v1/pir",
    # frontend/src/api/spotlight.ts の spotlightApi.list() 既定 (period_type=rolling7)。
    # bare "/api/v1/spotlight" は実際には送られないクエリなので別に持つ。
    "/api/v1/spotlight?period_type=rolling7",
    # ブリーフ・振り返り (一覧。本体は最新から数本を別途たどる)
    "/api/v1/intel-graph/daily-briefs?limit=60&meta_only=1",
    "/api/v1/intel-graph/brief-context",
    # ダッシュボード (概観 + KPI)。dashboard/summary の recent_runs / next_run_at
    # はジョブ実行という「いま」の状態なので書き出し時に落とす (main() 側で処理)。
    *(f"/api/v1/dashboard/overview?days={d}" for d in _DASHBOARD_DAYS),
    *(f"/api/v1/dashboard/summary?days={d}" for d in _DASHBOARD_DAYS),
    "/api/v1/intel-graph/pmesii?time=30",
    "/api/v1/pir/dashboard/overview",
    # 週次深掘り (読み取り専用。本文を伴わない記事選定の根拠 + narrative)
    *(f"/api/v1/deep-dives?weeks={w}" for w in _DEEPDIVE_WEEKS),
)

#: 日次ブリーフの本体を何本たどるか。1 本 ~77KB。
BRIEF_DETAILS = 30

#: ⚠ `/api/v1/runtime-flags` と `/api/v1/channels` はここから外している。
#: どちらも画面の起動関門が引くが、稼働中の値 (webhook env key・認証状態) を
#: そのまま写すと運用情報が匿名公開に乗る。main() が同形の空スタブを別途書く。
REFERENCE_ENDPOINTS = (
    "/api/v1/vocabularies",
    "/api/v1/feed-options",
    "/api/v1/actor-options",
    "/api/v1/affected-vendors",
    "/api/v1/pir/options",
)

#: runtime-flags の空スタブ。匿名公開では認証状態そのものが無意味 (常時未認証)
#: なので、画面の ANONYMOUS 既定 (frontend/src/hooks/useRuntimeFlags.ts) と同じ形を
#: 固定値で書く。稼働中の値を取得しない。
_RUNTIME_FLAGS_STUB: dict[str, Any] = {
    "read_only": False,
    "authenticated": False,
    "auth_available": False,
    "remote_write": False,
}

#: channels の空スタブ。webhook 設定・ルーティング既定は運用情報なので持たない。
#: 画面は label 解決に失敗すると id をそのまま出す (frontend/src/components/channel.tsx
#: の useChannelMeta フォールバック) ので、空でも描画は壊れない。
_CHANNELS_STUB: dict[str, Any] = {
    "channels": [],
    "builtin_ids": [],
    "rule_refs": {},
    "webhook_set": {},
    "webhook_masked": {},
}

#: チャンネルを匿名の写しに出すときに残す項目 (2026-10-04)。絞り込み (「日本関連」等) の
#: 選択肢と表示名にだけ使う。webhook の環境変数名・設定状態・ルーティングの参照は
#: 運用情報なので落とす
_CHANNEL_PUBLIC_KEYS = ("id", "label", "enabled", "routable", "order")


def _public_channels(payload: Any) -> dict[str, Any]:
    """`/api/v1/channels` の応答から、表示名と並びだけを残した新しい構造を返す。"""
    channels = payload.get("channels", []) if isinstance(payload, dict) else []
    return {
        **_CHANNELS_STUB,
        "channels": [
            {k: c[k] for k in _CHANNEL_PUBLIC_KEYS if k in c}
            for c in channels
            if isinstance(c, dict)
        ],
        "builtin_ids": list(payload.get("builtin_ids", [])) if isinstance(payload, dict) else [],
    }


#: 匿名公開では書き出さないエンドポイント (レビューキュー・個人メモ・購読ソース・
#: Grok 関連)。最終関門 (`_final_gate`) がファイルとして存在しないことを確認する。
_EXCLUDED_ENDPOINTS = (
    "/api/v1/notes",
    "/api/v1/subscriptions",
    "/api/v1/grok/tasks",
    "/api/v1/grok/session",
    "/api/v1/grok-mail/health",
    "/api/v1/actors/sync",
)

#: 値に関わらず弾く **項目名**。本文 (body/body_ja) は再配布禁止、notes は個人の
#: 作業メモ。credential 判定 (`_CREDENTIAL_KEY`) とは別の関門として持つ
#: (本文判定は値の形で判定できないため、項目名で守るしかない)。
_FORBIDDEN_BODY_KEYS = ("body", "body_ja", "notes")


#: 資格情報らしい **フィールド名**。語を含むだけでは弾かない — アクター名に
#: "secret" を含むものがあり (global_secret_group)、部分一致では誤検知する。
#: フィールド名の**末尾**で判定する。
_CREDENTIAL_KEY = re.compile(
    r"(?i)(api_?key|_key|token|password|passwd|secret|webhook|authorization|credential)$"
)

#: 環境変数名 (鍵の置き場を指す値)。値そのものではないので通す。
_ENV_VAR_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")

#: 鍵そのものの形。**キー名に関わらず** 弾く。フィールド名を頼りにすると、
#: 名前を変えた経路が素通りする。
_SECRET_VALUE = re.compile(
    # ⚠ 語の途中に当てない。記事 URL の "task-host-flaw" が "sk-host-flaw…" に
    # 見えて止まった (2026-08-29 実測)。前が英数・ハイフンなら鍵の始まりではない。
    r"(?i)(?<![A-Za-z0-9_-])("
    r"https://\S*(discord|slack)\.com/api/webhooks/\S+"
    r"|sk-(ant|proj|live)-[A-Za-z0-9_-]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|bearer\s+[A-Za-z0-9._-]{20,}"
    r")"
)


def _assert_no_credentials(payload: Any, where: str) -> None:
    """資格情報らしい値が混じっていたら**書き出しを止める**。

    写しは限定公開とはいえエッジに置くので、鍵は載せない。公開サイト側の禁止
    (本文) とは対象が違う — 面ごとに守るものが違うため、関門も別に持つ
    (片方を緩めても他方は緩まない)。

    通すもの: 短い値 (件数・真偽・空) と、**鍵の置き場を指す値**
    (`webhook_env_key: "DISCORD_WEBHOOK_ALERT"` のような環境変数名)。
    弾きすぎると関門を外したくなり、結局守られなくなる。
    """
    if isinstance(payload, str):
        if _SECRET_VALUE.search(payload):
            raise SystemExit(f"鍵そのものを書き出そうとした: {where}")
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if (
                _CREDENTIAL_KEY.search(str(key))
                and isinstance(value, str)
                and len(value) >= 12
                and not _ENV_VAR_NAME.fullmatch(value)
            ):
                raise SystemExit(f"資格情報らしい値を書き出そうとした: {where}.{key}")
            _assert_no_credentials(value, f"{where}.{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            _assert_no_credentials(value, f"{where}[{i}]")


def _strip_forbidden(payload: Any) -> Any:
    """`_FORBIDDEN_BODY_KEYS` を**再帰的に**取り除いた新しい構造を返す (immutable)。

    事象ニュースの詳細は構成記事を `members` に入れ子で持つため、トップレベルの
    記事 1 件分だけ処理しても足りない。どれだけ深く入れ子になっていても辿って落とす。
    """
    if isinstance(payload, dict):
        return {
            key: _strip_forbidden(value)
            for key, value in payload.items()
            if key not in _FORBIDDEN_BODY_KEYS
        }
    if isinstance(payload, list):
        return [_strip_forbidden(value) for value in payload]
    return payload


def _assert_no_forbidden_keys(payload: Any, where: str) -> None:
    """`_FORBIDDEN_BODY_KEYS` が残っていたら**書き出しを止める**。

    `_write` は先に `_strip_forbidden` で取り除いているので、ここで引っかかるのは
    本来起きない取り残し (呼び出し忘れ・新しい書き出し経路の追加漏れ) を捕まえる
    ための安全網。黙って通さない。
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _FORBIDDEN_BODY_KEYS:
                raise SystemExit(f"書き出してはならない項目が残っていた: {where}.{key}")
            _assert_no_forbidden_keys(value, f"{where}.{key}")
    elif isinstance(payload, list):
        for i, value in enumerate(payload):
            _assert_no_forbidden_keys(value, f"{where}[{i}]")


def _get(client: httpx.Client, path: str, **params: Any) -> Any:
    r = client.get(path, params=params or None)
    r.raise_for_status()
    return r.json()


def _write(path: Path, payload: Any) -> int:
    payload = _strip_forbidden(payload)
    _assert_no_credentials(payload, path.name)
    _assert_no_forbidden_keys(payload, path.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(body)
    return len(body)


def _final_gate(out: Path) -> None:
    """全ファイルを最後に見直す **fail-closed な関門**。

    `_write` 側の関門を個別に抜けてしまう経路 (将来の書き出し追加漏れ等) があっても、
    ここで必ず捕まえる。除外対象のエンドポイントがファイルとして存在しないことも
    ここで確認する — 居なくなったことを信じるのではなく、居ないことを確かめる。
    """
    for ep in _EXCLUDED_ENDPOINTS:
        excluded_path = out / "api" / f"{_safe_name(ep)}.json"
        if excluded_path.exists():
            raise SystemExit(f"除外対象のエンドポイントが書き出されている: {ep}")
    for path in sorted(out.rglob("*.json")):
        payload = json.loads(path.read_bytes())
        where = str(path.relative_to(out))
        _assert_no_credentials(payload, where)
        _assert_no_forbidden_keys(payload, where)


def _safe_name(article_id: str) -> str:
    """id をファイル名にする。URL をそのまま id にしている経路があるため、
    ディレクトリ区切りや長さの問題を避けてハッシュに落とす。"""
    return hashlib.sha256(article_id.encode()).hexdigest()[:32]


#: 事象ニュース一覧の絞り込み語彙 (frontend/src/pages/EventNewsPage.tsx の
#: IMPORTANCE_OPTS / facets.tsx の選択肢) のうち、backend が値として保持する範囲を
#: 一覧アイテムへ持ち上げる。**これを足す理由**: 写しの一覧 (eventnews.json) は
#: 絞り込み前の全量を返すだけで、画面側 (mirrorStatic.ts) が先頭 N 件を切るだけだと
#: low importance の事象が high/medium の間に混ざって出る (2026-10-04 発見)。
#: 一覧 API 自体は category / channel / actor / cve 等の記事側条件を記事単位で
#: 評価してから事象へ持ち上げるため、一覧アイテムの時点ではこれらの値を持たない。
#: 詳細 (``/api/v1/eventnews/{id}``) は構成記事から集計した facet を既に持っている
#: ので、**詳細を書き出す際に同じ値を一覧アイテムへ複製**する (新しい API を
#: 増やさず、既存のレスポンスから拾う)。
def _eventnews_filter_tags(detail: dict[str, Any]) -> dict[str, Any]:
    """事象詳細から一覧の絞り込みに要る値を拾う (immutable: 新しい dict を返す)。

    ``entities`` は型 → 値一覧 (actor は canonical id、cve は大文字、pir は SIR id)。
    記事単位の AND 条件 (同じ記事が複数条件を同時に満たす) までは再現できない —
    事象単位の OR (いずれかの構成記事が持つ値) に近似する。単一条件での絞り込みは
    忠実、複数条件の組み合わせは近似であることをフロント側のコメントにも明記する。
    """
    metadata = detail.get("metadata") or {}
    judgement = metadata.get("judgement") or {}

    def _facet_values(key: str) -> list[str]:
        facet = judgement.get(key)
        if not facet:
            return []
        return [str(v.get("value")) for v in facet.get("values", []) if v.get("value")]

    entities: dict[str, list[str]] = {}
    vendors: set[str] = set()
    for group in metadata.get("entities") or []:
        etype = str(group.get("type") or "")
        values = [str(v.get("value")) for v in group.get("values", []) if v.get("value")]
        if etype and values:
            entities[etype] = values
        if etype == "cve":
            for info in (group.get("affected") or {}).values():
                vendors.update(str(v) for v in info.get("vendors", []))
                vendors.update(str(v) for v in info.get("products", []))

    feeds = sorted(
        {str(m.get("feed_title")) for m in detail.get("members", []) if m.get("feed_title")}
    )

    return {
        "categories": _facet_values("category"),
        "channels": _facet_values("channel"),
        "intents": _facet_values("intent"),
        "feeds": feeds,
        "entities": entities,
        "vendors": sorted(vendors),
    }


def _fetch_articles(client: httpx.Client, days: int, cap: int) -> list[dict[str, Any]]:
    """記事一覧を窓のぶん全件集める (offset で辿る)。

    ``cap`` は取りすぎの歯止め。Pages の 1 デプロイ 20,000 ファイル制限があるので、
    窓を広げすぎたときに黙って上限へ突っ込まないようにする。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < cap:
        page = _get(
            client,
            "/api/v1/articles",
            limit=_PAGE,
            offset=offset,
            since_hours=days * 24,
            include_summary=True,
        )
        batch = page.get("articles", [])
        if not batch:
            break
        for a in batch:
            aid = str(a.get("article_id") or "")
            if aid and aid not in seen:
                seen.add(aid)
                out.append(a)
        offset += _PAGE
    return out[:cap]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8001")
    ap.add_argument("--out", required=True, help="書き出し先ディレクトリ")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--max-articles", type=int, default=MAX_ARTICLES)
    ap.add_argument("--max-events", type=int, default=MAX_EVENTS)
    ap.add_argument("--min-articles", type=int, default=DEFAULT_MIN_ARTICLES)
    ap.add_argument("--min-events", type=int, default=DEFAULT_MIN_EVENTS)
    args = ap.parse_args()

    out = Path(args.out)
    generated_at = datetime.now(UTC)
    total_bytes = 0

    with httpx.Client(base_url=args.base_url, timeout=120.0) as client:
        # --- 画面が引く参照データ ---
        # 記事より先に取る。ここが欠けると画面が描けないので、
        # 記事だけ揃った「読めない写し」を作らない。
        for ep in REFERENCE_ENDPOINTS:
            payload = _get(client, ep)
            total_bytes += _write(out / "api" / f"{_safe_name(ep)}.json", payload)

        # runtime-flags は稼働中の値を取らず同形の空スタブを書く (認証状態は運用情報)。
        # channels は表示名と並びだけ残す (webhook の環境変数名・設定状態は落とす)。
        total_bytes += _write(
            out / "api" / f"{_safe_name('/api/v1/runtime-flags')}.json", _RUNTIME_FLAGS_STUB
        )
        total_bytes += _write(
            out / "api" / f"{_safe_name('/api/v1/channels')}.json",
            _public_channels(_get(client, "/api/v1/channels")),
        )

        # --- 画面ごとの取得 ---
        # 1 本の失敗で写し全体を止めない。落ちた画面は 501 になり、
        # 「写しに含まれていません」と出る (黙って固まるよりよい)。
        missing: list[str] = []
        for ep in SCREEN_ENDPOINTS:
            try:
                payload = _get(client, ep)
            except httpx.HTTPError as exc:
                missing.append(f"{ep} ({type(exc).__name__})")
                continue
            if ep.startswith("/api/v1/dashboard/summary") and isinstance(payload, dict):
                # recent_runs / next_run_at はジョブ実行という「いま」の状態。
                # KPI 本体 (summary/db_stats) は構成要素として残す。
                payload = {
                    k: v for k, v in payload.items() if k not in ("recent_runs", "next_run_at")
                }
            total_bytes += _write(out / "api" / f"{_safe_name(ep)}.json", payload)

        # --- 掘り下げ (アクター / 国) ---
        # 一覧から id を取り、個別ページの取得を写す。期間は既定 (30 日) のみ —
        # 4 通り持つとファイル数が 4 倍になり Pages の上限に近づく。
        try:
            actors = _get(client, "/api/v1/actors")
            ids = [
                str(a.get("id"))
                for a in (actors if isinstance(actors, list) else actors.get("actors", []))
                if a.get("id")
            ]
            for aid in ids:
                enc = urllib.parse.quote(aid, safe="")
                for ep in (
                    f"/api/v1/intel-graph/threats/actor/{enc}?time=30",
                    f"/api/v1/actors/{enc}/history",
                    f"/api/v1/actors/{enc}/situations",
                ):
                    try:
                        total_bytes += _write(
                            out / "api" / f"{_safe_name(ep)}.json", _get(client, ep)
                        )
                    except httpx.HTTPError:
                        continue
        except httpx.HTTPError as exc:
            missing.append(f"アクター個別 ({type(exc).__name__})")

        try:
            nations = _get(client, "/api/v1/intel-graph/situation/nations", time=30)
            codes = [
                # 国の鍵は iso (code / nation ではない)。
                str(n.get("iso") or "")
                for n in (nations if isinstance(nations, list) else nations.get("nations", []))
            ]
            for code in [c for c in codes if c]:
                for ep in (
                    f"/api/v1/intel-graph/snapshot?time=30&nation={code}",
                    f"/api/v1/intel-graph/situation?nation={code}&time=30",
                ):
                    try:
                        total_bytes += _write(
                            out / "api" / f"{_safe_name(ep)}.json", _get(client, ep)
                        )
                    except httpx.HTTPError:
                        continue
        except httpx.HTTPError as exc:
            missing.append(f"国別 ({type(exc).__name__})")

        # SIR (PIR) 個別 (詳細画面用)。一覧 (`/api/v1/pir`) は既に SCREEN_ENDPOINTS で
        # 書き出し済みだが、詳細画面 (PirDetailPage) は個別の get/kpi を別経路で叩くため
        # ここで ID ごとに辿る必要がある (旧実装は一覧のみでここが丸ごと欠けていた→
        # 詳細画面が常に「SIR が見つかりません」になっていた)。
        try:
            pir_list = _get(client, "/api/v1/pir")
            pir_items = pir_list.get("priorities", []) if isinstance(pir_list, dict) else []
            for item in pir_items:
                pid = str(item.get("id") or "")
                if not pid:
                    continue
                enc = urllib.parse.quote(pid, safe="")
                for ep in (f"/api/v1/pir/{enc}", f"/api/v1/pir/{enc}/kpi"):
                    try:
                        total_bytes += _write(
                            out / "api" / f"{_safe_name(ep)}.json", _get(client, ep)
                        )
                    except httpx.HTTPError:
                        continue
                # Spotlight はまだ生成されていない SIR もある (404 を通常として扱う)。
                # PirDetailPage は spotlightApi.get() の既定どおり period_type=rolling7 固定。
                if item.get("spotlight_enabled"):
                    ep = f"/api/v1/spotlight/{enc}?period_type=rolling7"
                    try:
                        total_bytes += _write(
                            out / "api" / f"{_safe_name(ep)}.json", _get(client, ep)
                        )
                    except httpx.HTTPError:
                        continue
        except httpx.HTTPError as exc:
            missing.append(f"SIR 個別 ({type(exc).__name__})")

        # 日次ブリーフの本体。一覧の新しい方から数本たどる。
        try:
            briefs = _get(
                client, "/api/v1/intel-graph/daily-briefs", limit=BRIEF_DETAILS, meta_only=1
            )
            for b in (briefs.get("briefs") or briefs.get("items") or [])[:BRIEF_DETAILS]:
                bid = b.get("id")
                if bid is None:
                    continue
                ep = f"/api/v1/intel-graph/daily-briefs/{bid}"
                try:
                    total_bytes += _write(out / "api" / f"{_safe_name(ep)}.json", _get(client, ep))
                except httpx.HTTPError:
                    continue
        except httpx.HTTPError as exc:
            missing.append(f"daily-briefs ({type(exc).__name__})")

        if missing:
            # 黙って欠けさせない。何が写せなかったかを毎回出す。
            print(f"写せなかった画面 {len(missing)} 本:", file=sys.stderr)
            for m in missing:
                print(f"  - {m}", file=sys.stderr)

        # --- 記事 ---
        articles = _fetch_articles(client, args.days, args.max_articles)
        if len(articles) < args.min_articles:
            print(
                f"記事が {len(articles)} 件しかない (下限 {args.min_articles})。"
                " 空の写しを配らないため中止する。",
                file=sys.stderr,
            )
            return 1
        total_bytes += _write(out / "articles.json", {"articles": articles, "count": len(articles)})

        for a in articles:
            aid = str(a.get("article_id") or "")
            if not aid:
                continue
            enc = urllib.parse.quote(aid, safe="")
            try:
                detail = _get(client, f"/api/v1/articles/{enc}")
            except httpx.HTTPStatusError:
                continue  # 1 件の欠落で写し全体を止めない
            # 本文 (body/body_ja) は _write → _strip_forbidden が再帰的に落とす。
            total_bytes += _write(out / "articles" / f"{_safe_name(aid)}.json", detail)

        # --- 事象ニュース (こちらも offset で全件辿る) ---
        events: list[dict[str, Any]] = []
        seen_ev: set[str] = set()
        ev_offset = 0
        while len(events) < args.max_events:
            batch = _get(client, "/api/v1/eventnews", limit=_PAGE, offset=ev_offset).get(
                "items", []
            )
            if not batch:
                break
            for e in batch:
                eid = str(e.get("id") or "")
                if eid and eid not in seen_ev:
                    seen_ev.add(eid)
                    events.append(e)
            ev_offset += _PAGE
        events = events[: args.max_events]
        if len(events) < args.min_events:
            print(
                f"事象ニュースが {len(events)} 件しかない (下限 {args.min_events})。中止する。",
                file=sys.stderr,
            )
            return 1

        # 詳細を先に辿って絞り込み用の値を一覧へ複製する (_eventnews_filter_tags)。
        # 一覧の書き出しは詳細取得の **後** に行う (immutable: 元の events は変えず
        # enriched という新しいリストを組み立てる)。
        enriched: list[dict[str, Any]] = []
        for e in events:
            eid = str(e.get("id") or "")
            if not eid:
                enriched.append(e)
                continue
            try:
                detail = _get(client, f"/api/v1/eventnews/{urllib.parse.quote(eid, safe='')}")
            except httpx.HTTPStatusError:
                enriched.append(e)
                continue
            total_bytes += _write(out / "eventnews" / f"{_safe_name(eid)}.json", detail)
            enriched.append({**e, **_eventnews_filter_tags(detail)})

        total_bytes += _write(out / "eventnews.json", {"items": enriched})

    # --- 写しであることの宣言 ---
    # 画面はこれを読んで「○○時点の写し」を常時出す。無いとライブと見分けが付かない。
    # with_bodies は匿名公開のため常に false (frontend の MirrorBanner が本文なしの
    # 注記を出すのに読む。2026-10-04 以前は選べたが、いまは無条件)。
    meta = {
        "generated_at": generated_at.isoformat(),
        "kind": "ops-mirror",
        "window_days": args.days,
        "with_bodies": False,
        "counts": {"articles": len(articles), "eventnews": len(events)},
    }
    _write(out / "meta.json", meta)

    # --- 最終関門 ---
    # ここまでの書き出しがすべて関門を抜けているはずだが、**信じずに確かめる**。
    _final_gate(out)

    print(
        f"書き出し完了: 記事 {len(articles)} 件 / 事象 {len(events)} 件 "
        f"/ {total_bytes / 1024 / 1024:.1f} MB / 匿名公開 (本文なし)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
