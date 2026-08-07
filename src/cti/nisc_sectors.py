"""NISC 重要インフラ分野の派生タクソノミ (二層の下層)。

coarse な ``victim_sector`` (victim_sectors.yaml の 22 canonical) は global 用に維持し、
JP 重要インフラ board 専用に**派生**する NISC 分野 (〜15 + 防衛) をここで導出する。
新規抽出タグでなく **読み時導出 + curated mapping** (tag_registry の保存最小化規律)。

- ``nisc_sector_for(canonical, text)``: canonical 既定 + JP キーワード精緻化で NISC 分野を返す。
- ``operator_nisc_sector(org)``: 主要 JP CI 事業者名 → 分野 (観測事案の分野配置。curated 小表)。
- ``sectors_for_tech(product_or_vendor)``: 製品/ベンダ → 一般的な分野 (潜在露出のリンク・露出仮説)。

curated 表 (JP_CI_OPERATORS / SECTOR_TECH) は**資産インベントリではない** — 分野レベルの典型知識で
あり特定事業者の実導入を確定しない (docs/jp_critical_infra_board_design.md §8)。
"""

from __future__ import annotations

import re

# board の行 = NISC 重要インフラ 15 分野 + 防衛 (NISC 外だが防衛 CTI ツールとして追加)。
# 表示順は lifeline → 情報通信 → 金融 → 交通 → その他。id は安定な romaji、label は日本語。
NISC_SECTORS: dict[str, str] = {
    "electricity": "電力",
    "gas": "ガス",
    "oil": "石油",
    "water": "水道",
    "it_telecom": "情報通信",
    "finance": "金融",
    "credit": "クレジット",
    "railway": "鉄道",
    "aviation": "航空",
    "airport": "空港",
    "logistics": "物流",
    "port": "港湾",
    "medical": "医療",
    "chemical": "化学",
    "government": "政府・行政",
    "defense": "防衛",
}

# canonical (victim_sectors.yaml) → NISC 分野の既定写像。text キーワードが優先 (下記)。
_CANONICAL_TO_NISC: dict[str, str] = {
    "telecom": "it_telecom",
    "financial": "finance",
    "healthcare": "medical",
    "government": "government",
    "national_security": "defense",
    "defense": "defense",
    "energy": "electricity",  # 既定。text で ガス/石油 に精緻化
    "transportation": "railway",  # 既定。text で 航空/空港/物流/港湾 に精緻化
    # critical_infra は catch-all のため text 精緻化に委ねる (既定なし)
}

# 明確に非 CI の canonical。text にデータ種別 (「クレジットカード情報」等) や CI 語が
# 言及されても board 対象外 — **被害者の分野 ≠ 漏えいデータの種別** (誤配置の根治)。
_NON_CI_CANONICALS: frozenset[str] = frozenset(
    {
        "retail",
        "media",
        "education",
        "technology",
        "ngo",
        "research",
        "professional_services",
        "enterprise",
        "food_agriculture",
        "space",
    }
)

# canonical ごとの text 精緻化の許容先。LLM が付けた victim_sector を text 言及が
# 覆さないための制約 (例: financial 記事の「行政」言及で政府・行政に飛ばない)。
# 表にない canonical (critical_infra / multi_sector / uncategorized / None 等) は全分野可。
_CANONICAL_ALLOWED: dict[str, frozenset[str]] = {
    "financial": frozenset({"finance", "credit"}),
    "healthcare": frozenset({"medical"}),
    "telecom": frozenset({"it_telecom"}),
    "government": frozenset({"government", "defense"}),
    "defense": frozenset({"defense"}),
    "national_security": frozenset({"defense"}),
    "energy": frozenset({"electricity", "gas", "oil"}),
    "transportation": frozenset({"railway", "aviation", "airport", "logistics", "port"}),
    # 製造業は CI 事業者ではないが、化学プラント/防衛産業は text で精緻化可
    "manufacturing": frozenset({"chemical", "defense"}),
}

_ALL_NISC: frozenset[str] = frozenset(NISC_SECTORS)

# NISC 分野ごとの JP キーワード (title+summary の部分一致、上から順に最初の一致が勝つ)。
# 曖昧が起きやすい対 (空港/航空, 電力/ガス/石油) は具体側を上に置く。
_NISC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("airport", ("空港", "airport", "成田空港", "羽田空港", "関西空港")),
    ("aviation", ("航空", "airline", "ana", "全日空", "jal", "日本航空")),
    ("gas", ("都市ガス", "ガス事業", "天然ガス", "lng", "東京ガス", "大阪ガス")),
    ("oil", ("石油", "製油", "精製", "oil refin", "eneos", "出光", "コスモ石油")),
    (
        "electricity",
        (
            "電力",
            "送電",
            "配電",
            "発電",
            "power grid",
            "grid operator",
            "tepco",
            "東京電力",
            "関西電力",
            "中部電力",
            "電力会社",
            "系統",
        ),
    ),
    ("water", ("水道", "上下水道", "浄水", "下水", "water utility", "water treatment")),
    ("railway", ("鉄道", "railway", "新幹線", "私鉄", "地下鉄", "metro", "jr東", "jr西", "jr東海")),
    ("logistics", ("物流", "logistics", "倉庫", "宅配", "運送", "freight", "3pl", "サプライ物流")),
    ("port", ("港湾", "港運", "maritime", "海運", "shipping line", "コンテナ港", "名古屋港")),
    (
        "it_telecom",
        (
            "情報通信",
            "電気通信",
            "通信事業",
            "telecom",
            "carrier",
            "isp",
            "5g",
            "ntt",
            "kddi",
            "ソフトバンク",
            "楽天モバイル",
            "携帯電話網",
        ),
    ),
    # credit は**事業者語のみ** — 「クレジットカード情報が漏えい」等のデータ種別言及で
    # EC/小売の被害をクレジット分野に誤配置しない (bare クレジット/credit card は使わない)
    (
        "credit",
        (
            "クレジットカード会社",
            "カード会社",
            "信販",
            "決済代行",
            "決済ネットワーク",
            "決済事業者",
            "jcb",
        ),
    ),
    (
        "finance",
        (
            "銀行",
            "金融機関",
            "bank",
            "証券",
            "securities",
            "メガバンク",
            "信用金庫",
            "信用組合",
            "みずほ",
        ),
    ),
    ("medical", ("医療", "病院", "hospital", "healthcare", "診療", "電子カルテ", "医療機関")),
    ("chemical", ("化学", "chemical plant", "石化", "化学工場", "化学メーカー", "petrochemical")),
    (
        "government",
        (
            "政府機関",
            "行政",
            "省庁",
            "自治体",
            "地方公共団体",
            "官公庁",
            "ministry",
            "government agency",
        ),
    ),
    (
        "defense",
        (
            "防衛",
            "自衛隊",
            "jsdf",
            "防衛産業",
            "防衛装備",
            "military contractor",
            "三菱重工",
            "川崎重工",
        ),
    ),
)

# 主要 JP CI 事業者 → 分野 (観測事案で victim_org が名指しされた時の分野配置。curated 小表)。
JP_CI_OPERATORS: dict[str, str] = {
    "東京電力": "electricity",
    "tepco": "electricity",
    "関西電力": "electricity",
    "中部電力": "electricity",
    "九州電力": "electricity",
    "東北電力": "electricity",
    "東京ガス": "gas",
    "大阪ガス": "gas",
    "東邦ガス": "gas",
    "eneos": "oil",
    "出光": "oil",
    "コスモ石油": "oil",
    "ntt": "it_telecom",
    "kddi": "it_telecom",
    "ソフトバンク": "it_telecom",
    "softbank": "it_telecom",
    "楽天モバイル": "it_telecom",
    "jr東日本": "railway",
    "jr東海": "railway",
    "jr西日本": "railway",
    "東京メトロ": "railway",
    "近鉄": "railway",
    "ana": "aviation",
    "全日空": "aviation",
    "jal": "aviation",
    "日本航空": "aviation",
    "三菱ufj": "finance",
    "三井住友": "finance",
    "みずほ": "finance",
    "ゆうちょ": "finance",
    "野村": "finance",
    "jcb": "credit",
    "楽天カード": "credit",
    "三井住友カード": "credit",
    "日本郵便": "logistics",
    "ヤマト": "logistics",
    "佐川": "logistics",
    "日本通運": "logistics",
    "三菱重工": "defense",
    "川崎重工": "defense",
    "ihi": "defense",
    "富士通防衛": "defense",
}

# 分野ごとの典型製品/ベンダ (潜在露出の分野リンク。露出仮説であって実導入の確定ではない)。
SECTOR_TECH: dict[str, tuple[str, ...]] = {
    "electricity": (
        "scada",
        "siemens",
        "schneider",
        "hitachi energy",
        "mitsubishi electric",
        "abb",
        "ge grid",
        "iec 61850",
        "rtu",
        "plc",
    ),
    "gas": ("scada", "yokogawa", "emerson", "honeywell", "dcs"),
    "oil": ("dcs", "honeywell", "emerson", "yokogawa", "aveva", "pi system", "osisoft"),
    "water": ("scada", "clearscada", "wonderware", "modbus", "plc"),
    "it_telecom": ("cisco", "juniper", "ericsson", "nokia", "fortinet", "palo alto", "5g core"),
    "railway": ("scada", "siemens mobility", "hitachi rail", "cbtc", "interlocking"),
    "aviation": ("sita", "amadeus", "sabre"),
    "medical": ("dicom", "hl7", "philips", "ge healthcare", "epic", "cerner", "pacs"),
    "chemical": ("dcs", "triconex", "safety instrumented", "honeywell", "yokogawa"),
}


def kw_hit(keyword: str, text_lower: str) -> bool:
    """keyword が text (小文字化済) に含まれるか。

    短い純 ASCII 英字語 (ana/jal/abb/ntt 等) は **ASCII 英字に挟まれない境界一致**にする
    (英文 "analysis" の "ana"・"rabbit" の "abb" 等の誤爆を防ぐ。一方で日本語文脈
    "nttドコモ" は前後が非 ASCII 英字なので許容)。スペース/数字を含む語は素の部分一致。
    system_domain 等の他モジュールの日本語/短英語マッチもこれを使う (length-aware 教訓)。
    """
    kw = keyword.lower()
    if kw.isascii() and kw.isalpha():
        return re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", text_lower) is not None
    return kw in text_lower


_kw_hit = kw_hit  # 後方互換 (module 内の旧名利用)

# keyword に続くと**被害者言及でなくなる**接尾辞 (データ種別/規制動作)。
# 例: 「銀行口座情報が流出」= 口座データの言及 ≠ 銀行の被害。「行政処分」= 規制 ≠ 行政の被害。
_KEYWORD_EXCLUDE_SUFFIX: dict[str, tuple[str, ...]] = {
    "銀行": ("口座",),
    "bank": (" account",),
    "行政": ("処分",),
}


def _sector_kw_hit(keyword: str, text_lower: str) -> bool:
    """分野キーワードの一致 (kw_hit + 除外接尾辞ガード)。"""
    excl = _KEYWORD_EXCLUDE_SUFFIX.get(keyword)
    if excl is None:
        return kw_hit(keyword, text_lower)
    kw = keyword.lower()
    neg = "".join(f"(?!{re.escape(e)})" for e in excl)
    pattern = re.escape(kw) + neg
    if kw.isascii() and kw.isalpha():
        pattern = rf"(?<![a-z]){re.escape(kw)}(?![a-z]){neg}"
    return re.search(pattern, text_lower) is not None


def nisc_sector_for(canonical: str | None, title: str, summary: str = "") -> str | None:
    """NISC 分野 id を返す (非該当は None)。

    配置規律 (board の信頼性の土台):
    1. 非 CI canonical (retail/media/education...) は text に何が書かれていても None。
    2. text 精緻化は canonical の許容先 (_CANONICAL_ALLOWED) 内のみ — LLM の victim 判定を
       データ種別言及が覆さない。
    3. **制約なし配置 (canonical 不明等) は title のみで判定** — summary の対処助言
       (「カード会社への連絡を推奨」等) や周辺言及での誤配置を防ぐ。被害者は title に現れる。
       制約付き精緻化 (energy→ガス等) は被害者が CI 系と確定済のため summary も使える。
    4. キーワード無しは canonical 既定 (許容先内のみ)。
    """
    canon = (canonical or "").strip().lower()
    if canon in _NON_CI_CANONICALS:
        return None
    restricted = canon in _CANONICAL_ALLOWED
    allowed = _CANONICAL_ALLOWED.get(canon, _ALL_NISC)
    scan_text = f"{title} {summary}" if restricted else title
    lowered = (scan_text or "").lower()
    for nisc_id, keywords in _NISC_KEYWORDS:
        if nisc_id not in allowed:
            continue
        if any(_sector_kw_hit(kw, lowered) for kw in keywords):
            return nisc_id
    default = _CANONICAL_TO_NISC.get(canon)
    return default if default in allowed else None


def operator_nisc_sector(org: str | None) -> str | None:
    """事業者名 → NISC 分野 (curated 小表の部分一致)。未知は None。"""
    if not org:
        return None
    lowered = org.lower()
    for alias, nisc_id in JP_CI_OPERATORS.items():
        if _kw_hit(alias, lowered):
            return nisc_id
    return None


def sectors_for_tech(product_or_vendor: str | None) -> list[str]:
    """製品/ベンダ名 → その製品が一般的な NISC 分野の list (露出仮説の分野リンク)。"""
    if not product_or_vendor:
        return []
    lowered = product_or_vendor.lower()
    return [nisc for nisc, kws in SECTOR_TECH.items() if any(_kw_hit(kw, lowered) for kw in kws)]


def sector_label(nisc_id: str) -> str:
    """NISC 分野 id → 日本語表示 (未知は id をそのまま)。"""
    return NISC_SECTORS.get(nisc_id, nisc_id)
