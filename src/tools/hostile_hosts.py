"""敵対的 host の分類 (純粋関数、2026-10-30)。

取得ポリシー (``hostile_fetch``) が「この host は厳しめに取るか」を決めるための分類器。
副作用なし (設定ファイルの読み込みだけキャッシュする)。判定は次の OR で、**迷ったら
hostile 側に倒す** (解釈できない host・IDNA 変換に失敗する host も hostile):

- (a) ソース設定の ``fetch_profile: hostile`` 印 (呼出側が ``source_profile`` で渡す)
- (b) ccTLD が ``config/fetch/hostile_domains.yaml`` (汎用、公開リポ) の ``cctlds`` に含まれる
- (c) ``domains`` に suffix 一致 (登録可能ドメイン単位)。**実際の一覧は運用者ローカルの
  ``config/local/hostile_domains.yaml`` (gitignore) に置き**、汎用ファイルに和集合で重ねる。
  ローカルが無ければ汎用の規則 (ccTLD と placeholder の例) だけで動く (docs/local_overlay.md)
- (d) 解決済み IP の国が ``HOSTILE_GEOIP_COUNTRIES`` に含まれる (``ip_countries`` を渡した場合のみ。
  GeoIP DB は ``hostile_fetch`` 側が ``HOSTILE_GEOIP_DB`` で設定されたときだけ引く。依存は足さない)

理由 (``reasons``) は必ず返す。ログと shadow 計測の根拠になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from src.local_overlay import local_path
from src.logging_config import get_logger

_log = get_logger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config/fetch/hostile_domains.yaml"

#: 運用者ローカルの一覧 (gitignore)。名前は config/local/ からの相対。
LOCAL_FILE_NAME = "hostile_domains.yaml"

PROFILE_HOSTILE = "hostile"

# 設定が読めないときの最後の砦 (ファイル欠落で hostile 判定が全滅しないように)。
_FALLBACK_CCTLDS: frozenset[str] = frozenset({"cn", "ru", "su", "xn--p1ai", "by", "kp", "ir"})

# GeoIP で hostile とみなす国 (ISO 3166-1 alpha-2)。ccTLD の国と同じ顔ぶれ。
GEOIP_HOSTILE_COUNTRIES: frozenset[str] = frozenset({"CN", "RU", "BY", "KP", "IR"})

REASON_SOURCE_TAG = "source_tag"
REASON_CCTLD = "cctld"
REASON_DOMAIN = "domain"
REASON_GEOIP = "geoip"
REASON_UNPARSEABLE = "unparseable_host"
REASON_CLASSIFIER_ERROR = "classifier_error"


@dataclass(frozen=True)
class HostVerdict:
    """1 host の分類結果。"""

    host: str
    hostile: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HostileLists:
    """読み込み済みの設定 (frozen、キャッシュ可能)。"""

    cctlds: frozenset[str]
    domains: frozenset[str]


def _clean(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v).strip().lower().strip(".") for v in values if str(v).strip()]


_warned_fallback = False


def _fallback(reason: str) -> HostileLists:
    """設定が使えないとき ccTLD のみの組み込み一覧で動く (警告は 1 プロセスに 1 回)。"""
    global _warned_fallback  # noqa: PLW0603
    if not _warned_fallback:
        _warned_fallback = True
        _log.warning(
            "hostile_domains_config_invalid", reason=reason, fallback="builtin_cctlds_only"
        )
    return HostileLists(cctlds=_FALLBACK_CCTLDS, domains=frozenset())


def _parse(raw: object) -> HostileLists | str:
    """yaml の中身を一覧にする。構造が不正なら理由の文字列を返す。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        return "top_level_not_mapping"
    cctlds_raw, entries = raw.get("cctlds"), raw.get("domains")
    if (cctlds_raw is not None and not isinstance(cctlds_raw, list)) or (
        entries is not None and not isinstance(entries, list)
    ):
        return "cctlds_or_domains_not_list"
    domains: set[str] = set()
    for entry in entries or []:
        name = entry.get("domain") if isinstance(entry, dict) else entry
        if isinstance(name, str):
            domains.update(_clean([name]))
    return HostileLists(cctlds=frozenset(_clean(cctlds_raw)), domains=frozenset(domains))


def _read(target: Path) -> HostileLists | str:
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError) as e:
        return f"unreadable: {type(e).__name__}"
    return _parse(raw)


def _load_generic(target: Path) -> HostileLists:
    parsed = _read(target)
    if isinstance(parsed, str):
        return _fallback(parsed)
    return HostileLists(cctlds=parsed.cctlds or _FALLBACK_CCTLDS, domains=parsed.domains)


def _load_local(target: Path) -> HostileLists | None:
    """ローカルの一覧。無ければ None、壊れていれば警告して None (汎用だけで動く)。"""
    if not target.exists():
        return None
    parsed = _read(target)
    if isinstance(parsed, str):
        # 中身 (ドメイン名) は出さない。理由だけ。
        _log.warning("hostile_domains_local_invalid", reason=parsed, fallback="generic_only")
        return None
    return parsed


def load_lists(path: Path | None = None, *, local: Path | None = None) -> HostileLists:
    """設定を読む (例外は投げない)。

    - ``path``: 汎用の一覧 (既定 ``config/fetch/hostile_domains.yaml``)。壊れていれば
      組み込みの ccTLD だけ。
    - ``local``: 運用者ローカルの一覧。和集合で重ねる。``None`` なら重ねない
      (``default_lists`` は ``config/local/hostile_domains.yaml`` を渡す)。
    """
    return _load_layers(path or _CONFIG_PATH, local)[0]


def _load_layers(path: Path, local: Path | None) -> tuple[HostileLists, bool]:
    """(汎用 ∪ ローカル, ローカルを使ったか)。"""
    generic = _load_generic(path)
    overlay = _load_local(local) if local is not None else None
    if overlay is None:
        return generic, False
    merged = HostileLists(
        cctlds=generic.cctlds | overlay.cctlds, domains=generic.domains | overlay.domains
    )
    return merged, True


def _log_loaded(lists: HostileLists, *, local_used: bool) -> None:
    """どちらの一覧で動いているかを件数だけ出す (ドメイン名は出さない)。"""
    _log.info(
        "hostile_domains_loaded",
        source="generic+local" if local_used else "generic",
        cctlds=len(lists.cctlds),
        domains=len(lists.domains),
    )


@lru_cache(maxsize=1)
def default_lists() -> HostileLists:
    lists, local_used = _load_layers(_CONFIG_PATH, local_path(LOCAL_FILE_NAME))
    _log_loaded(lists, local_used=local_used)
    return lists


def reset_cache() -> None:
    """設定の再読込 (テスト・設定編集後)。"""
    default_lists.cache_clear()


def normalize_host(host_or_url: str) -> str | None:
    """URL / host を小文字の ASCII (punycode) host にする。解釈できなければ None。"""
    text = (host_or_url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.strip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def _matches_domain(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_host(
    host_or_url: str,
    *,
    source_profile: str | None = None,
    ip_countries: frozenset[str] | None = None,
    lists: HostileLists | None = None,
) -> HostVerdict:
    """host が hostile かを理由つきで返す。"""
    cfg = lists or default_lists()
    reasons: list[str] = []
    if (source_profile or "").strip().lower() == PROFILE_HOSTILE:
        reasons.append(REASON_SOURCE_TAG)

    host = normalize_host(host_or_url)
    if host is None:
        reasons.append(REASON_UNPARSEABLE)
        return HostVerdict(host=(host_or_url or "")[:80], hostile=True, reasons=tuple(reasons))

    if host.rsplit(".", 1)[-1] in cfg.cctlds:
        reasons.append(REASON_CCTLD)
    if _matches_domain(host, cfg.domains):
        reasons.append(REASON_DOMAIN)
    if ip_countries and ip_countries & GEOIP_HOSTILE_COUNTRIES:
        reasons.append(REASON_GEOIP)
    return HostVerdict(host=host, hostile=bool(reasons), reasons=tuple(reasons))
