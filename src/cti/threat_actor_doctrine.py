"""国家アクター → 標的 NISC 分野の doctrine (公的勧告由来の標的プロファイル)。

board の行動中心レーンの scaffold。国家アクターは被害者を名指さず活動するため観測
(victim_sector) は疎になる (Volt Typhoon の実測 top_sectors が 'defense' のみ等)。そこで
**公的勧告 (CISA / 各国当局 / ベンダ脅威報告) が明示する既知の標的分野**を doctrine として
コード所有し、観測 (top_sectors) と union して分野を確定する。

- **doctrine = 静的な標的プロファイル** (どの分野を狙うアクターか、公知)。
- **コーパス集約 = 動的な活動性** (いま何が活発か: intent / 件数 / spike / TTP)。
- 両者の積 = 分野ごとの「国家アクター行動」警告 (I&W)。

⚠ これは資産インベントリではなく**アクターの標的傾向の公知知識**。各エントリは公的勧告に
接地する (コメント参照)。攻撃者情報であって被害者の脆弱性情報ではない。
"""

from __future__ import annotations

from collections.abc import Iterable

# 敵性国家 (この tool の CTI mission = 中朝露イランを主敵とする)。同盟国は本レンズ対象外。
STATE_NATIONS: frozenset[str] = frozenset({"cn", "ru", "kp", "ir"})

# 同盟・同志国 (ミッション脅威評価の被害国重み付け用)。ミッション接地: 日米同盟 +
# Five Eyes + 準同盟/地域パートナー (韓台比)。網羅リストではない — 欧州の政府/防衛
# 被害は各アクターの標的分野 doctrine (STATE_ACTOR_SECTORS) 経由で捕捉される。
# ⚠ situation.py の _nation_role("allied") は「辞書にその国のアクターが居る」という
# 別物のプロキシであり、同盟の意味ではここを参照すること。
ALLIED_NATIONS: frozenset[str] = frozenset({"us", "gb", "au", "ca", "nz", "kr", "tw", "ph"})

# 戦略 intent (socio_political_intent のうち国家アクター行動を示すもの)。優先順 = I&W 上の
# 重要度 (事前潜伏 = 最上位)。金融犯罪 (financial) や外交 (diplomacy) 等は除外。
STRATEGIC_INTENTS: tuple[str, ...] = (
    "prepositioning",
    "disruption",
    "espionage",
    "subversion",
    "coercion",
)

# アクター名 (lower) → 標的 NISC 分野。canonical / alias いずれでも引けるよう主要別名も key 化。
# 接地: 各アクターの CISA 勧告 / MITRE ATT&CK / 主要ベンダ報告の標的分野記述。
STATE_ACTOR_SECTORS: dict[str, tuple[str, ...]] = {
    # ---- 中国 (事前潜伏 / 諜報) ----
    # Volt Typhoon: CISA AA24-038A = 通信/電力/水道/交通の重要インフラに事前潜伏
    "volt typhoon": (
        "it_telecom",
        "electricity",
        "gas",
        "oil",
        "water",
        "railway",
        "aviation",
        "port",
        "government",
    ),
    "bronze silhouette": ("it_telecom", "electricity", "water", "government"),
    "vanguard panda": ("it_telecom", "electricity", "water", "government"),
    # Salt Typhoon: 通信キャリア (ISP/テレコム) 侵入・傍受
    "salt typhoon": ("it_telecom", "government", "defense"),
    # Flax Typhoon: IoT ボットネット・エッジ機器
    "flax typhoon": ("it_telecom", "government"),
    # APT40 (Leviathan): 海事・港湾・政府・防衛
    "apt40": ("port", "government", "defense", "aviation"),
    "leviathan": ("port", "government", "defense", "aviation"),
    # APT10 (menuPass): MSP/通信/製造/政府 (日本を重点標的)
    "apt10": ("it_telecom", "government", "defense"),
    "menupass": ("it_telecom", "government", "defense"),
    # MirrorFace / Earth Kasha: 日本の政府・防衛・シンクタンク・メディア
    "mirrorface": ("government", "defense", "it_telecom"),
    "earth kasha": ("government", "defense", "it_telecom"),
    # Tick (Bronze Butler): 日本の防衛・製造
    "tick": ("defense",),
    "bronze butler": ("defense",),
    # Gallium: 通信キャリア
    "gallium": ("it_telecom",),
    # Earth Lusca / Mustang Panda: 政府・諜報
    "earth lusca": ("government",),
    "mustang panda": ("government",),
    # HAFNIUM/Silk Typhoon: Exchange 経由の広域諜報 (通信/政府/医療)
    "silk typhoon": ("it_telecom", "government", "medical"),
    "hafnium": ("it_telecom", "government", "medical"),
    # BlackTech: ルーター (エッジ機器) 侵害で日米の政府・産業・防衛関連を標的
    # (CISA/NSA/FBI + JPCERT/NISC 共同勧告 AA23-270A、2023-09)
    "blacktech": ("it_telecom", "government", "defense"),
    "palmerworm": ("it_telecom", "government", "defense"),
    # ---- ロシア (破壊 / 諜報) ----
    # Sandworm: 電力破壊 (ウクライナ変電所) / 政府
    "sandworm": ("electricity", "government"),
    # APT28 (Fancy Bear, GRU): 政府/防衛/航空/エネルギー
    "apt28": ("government", "defense", "aviation", "electricity"),
    "fancy bear": ("government", "defense", "aviation", "electricity"),
    # Turla (FSB): 政府/防衛の諜報
    "turla": ("government", "defense"),
    # Ember Bear: 政府/通信 (ウクライナ)
    "ember bear": ("government", "it_telecom"),
    # ---- 北朝鮮 (諜報 / 金融) ----
    # Lazarus: 金融/クレジット/防衛
    "lazarus": ("finance", "credit", "defense"),
    # Kimsuky: 政府/防衛/シンクタンク
    "kimsuky": ("government", "defense"),
    # Andariel: 防衛/医療/エネルギー
    "andariel": ("defense", "medical", "electricity"),
    # ---- イラン (諜報 / 破壊) ----
    # MuddyWater: 政府/通信/石油
    "muddywater": ("government", "it_telecom", "oil"),
    # APT33: 石油/航空/エネルギー
    "apt33": ("oil", "aviation", "electricity"),
    # APT34 (OilRig): 石油/金融/政府
    "apt34": ("oil", "finance", "government"),
    "oilrig": ("oil", "finance", "government"),
}


# 日本標的 doctrine: **公的一次ソース (警察庁/NISC/JPCERT/CISA 共同勧告等) が
# 「日本を標的」と明示・帰属公表したアクター**のみを載せる (ベンダ報告単独では追加
# しない — curation を絞ることで評価インフレを防ぐ)。値 = 接地 (駆動要因として UI に
# そのまま併記される)。key は STATE_ACTOR_SECTORS と同様 canonical/主要 alias の lower。
# ⚠ これは「対日脅威をあぶり出す下駄」ではない — 公知事実の機械可読化であり、
# 載っていないアクターの対日関連度は観測 (victim=JP) と他 doctrine から独立に立つ。
JP_TARGETING_ACTORS: dict[str, str] = {
    # 警察庁・NISC 2025-01-08 公表: MirrorFace による日本の政府/防衛/宇宙/先端技術への
    # 長期攻撃キャンペーン (2019-2024) を国家関与と特定
    "mirrorface": "警察庁・NISC 2025-01 帰属公表 (政府/防衛/宇宙/先端技術を長期標的)",
    "earth kasha": "警察庁・NISC 2025-01 帰属公表 (政府/防衛/宇宙/先端技術を長期標的)",
    # 外務省 2018-12 談話 + 米英 2018-12 共同帰属 (Cloud Hopper): 日本含む MSP 経由の
    # 長期諜報、日本組織の被害を明示
    "apt10": "外務省 2018-12 談話 / 米司法省起訴 (Cloud Hopper、日本の MSP・企業を標的)",
    "menupass": "外務省 2018-12 談話 / 米司法省起訴 (Cloud Hopper、日本の MSP・企業を標的)",
    "stone panda": "外務省 2018-12 談話 / 米司法省起訴 (Cloud Hopper、日本の MSP・企業を標的)",
    # 警視庁公安部 2021-04 公表: JAXA 等 ~200 組織への攻撃を Tick (61419部隊関与) と特定
    "tick": "警視庁公安部 2021-04 公表 (JAXA 等 ~200 組織、61419部隊関与)",
    "bronze butler": "警視庁公安部 2021-04 公表 (JAXA 等 ~200 組織、61419部隊関与)",
    # 警察庁・NISC 2023-09 注意喚起 + CISA/NSA/FBI/JPCERT 共同勧告: 日本を含む東アジアの
    # 政府/産業/防衛関連をルーター侵害で標的
    "blacktech": "警察庁・NISC 2023-09 注意喚起 / 日米共同勧告 AA23-270A (政府/産業/防衛)",
    "palmerworm": "警察庁・NISC 2023-09 注意喚起 / 日米共同勧告 AA23-270A (政府/産業/防衛)",
    # 警察庁・金融庁・NISC 2022-10 注意喚起 (国内暗号資産事業者標的) + 警察庁・FBI 2024-12
    # 共同声明 (TraderTraitor による DMM Bitcoin 4,502 BTC 窃取)
    "lazarus": "警察庁 2022-10 注意喚起 (暗号資産事業者) / 2024-12 日米共同声明 (DMM Bitcoin)",
    # JPCERT/CC 2024-07 注意喚起: Kimsuky による日本の組織を標的とした攻撃活動
    "kimsuky": "JPCERT/CC 2024-07 注意喚起 (日本の組織を標的とした攻撃活動)",
    # 警察庁・NISC が co-seal した 2024-07 国際共同勧告 AA24-207A: 防衛/宇宙/エンジニア
    # リング分野を標的 (日本を含む防衛産業基盤)
    "andariel": "国際共同勧告 AA24-207A 2024-07 (警察庁/NISC co-seal、防衛/宇宙標的)",
}

# 事前配置 doctrine: 公的勧告が「重要インフラへの pre-positioning (事前潜伏)」を明示した
# アクター。観測 victim が疎でも (被害者を名指さず潜伏するアクターの構造的特性)、勧告
# 自体が I&W 最上位の根拠になる。値 = 接地。
PREPOSITIONING_DOCTRINE_ACTORS: dict[str, str] = {
    # CISA AA24-038A (2024-02): 米重要インフラ (通信/電力/水道/交通) への事前潜伏を確認、
    # 台湾有事等の危機時の破壊活動準備と評価
    "volt typhoon": "CISA AA24-038A (重要インフラ事前潜伏、危機時破壊の準備と評価)",
    "bronze silhouette": "CISA AA24-038A (重要インフラ事前潜伏、危機時破壊の準備と評価)",
    "vanguard panda": "CISA AA24-038A (重要インフラ事前潜伏、危機時破壊の準備と評価)",
}


# 非国家系 family (犯罪ランサム / eCrime / ハクティビスト)。nation 帰属 (Qilin=ru 等) を
# 持っていても「国家系アクター」としては扱わない (2026-07-18 ユーザー判断: Qilin は APT
# ではない)。nation 自体は事実帰属として辞書に残す — 除外するのは「国家系」レンズだけ。
# 消費: PIR actor_nations 照合 / ミッション脅威評価の敵性国家ベースライン / CI board 行動レーン。
NON_STATE_FAMILIES: frozenset[str] = frozenset({"ransom_group", "spider", "hacktivist"})


def is_state_nation(nation: str | None) -> bool:
    """nation が敵性国家 (中朝露イラン) か。"""
    return bool(nation) and str(nation).lower() in STATE_NATIONS


def is_state_actor(nation: str | None, family: str | None) -> bool:
    """敵性国家の**国家系**アクターか (nation ∈ 中朝露イラン ∧ 非国家系 family でない)。

    Qilin (ransom_group, nation=ru) や NoName057(16) (hacktivist, nation=ru) は
    False — 国籍帰属はあっても国家指揮系のアクターではない。
    """
    return is_state_nation(nation) and (family or "") not in NON_STATE_FAMILIES


def _doctrine_grounds(names: Iterable[str], doctrine: dict[str, str]) -> tuple[str, ...]:
    """names のいずれかが doctrine に載っていれば接地 (citation) を返す (重複排除)。"""
    out: list[str] = []
    for name in names:
        ground = doctrine.get(str(name).strip().lower())
        if ground and ground not in out:
            out.append(ground)
    return tuple(out)


def jp_targeting_grounds(names: Iterable[str]) -> tuple[str, ...]:
    """アクターが日本標的 doctrine に該当する場合、その公的接地を返す (非該当は空)。"""
    return _doctrine_grounds(names, JP_TARGETING_ACTORS)


def prepositioning_grounds(names: Iterable[str]) -> tuple[str, ...]:
    """アクターが事前配置 doctrine に該当する場合、その公的接地を返す (非該当は空)。"""
    return _doctrine_grounds(names, PREPOSITIONING_DOCTRINE_ACTORS)


def actor_target_niscs(names: Iterable[str], observed_niscs: Iterable[str]) -> frozenset[str]:
    """アクターの標的 NISC 分野 = doctrine (任意の別名一致) ∪ 観測分野。"""
    out: set[str] = {n for n in observed_niscs if n}
    for name in names:
        key = str(name).strip().lower()
        if key in STATE_ACTOR_SECTORS:
            out.update(STATE_ACTOR_SECTORS[key])
    return frozenset(out)


def dominant_strategic_intent(top_intents: Iterable[tuple[str, int]]) -> str | None:
    """intent 分布から支配的な戦略 intent を返す。金融犯罪等は除外、事前潜伏を最優先。

    STRATEGIC_INTENTS の優先順 (事前潜伏 > 破壊 > 諜報 > ...) で、存在する最上位を返す。
    """
    present = {str(i) for i, _ in top_intents}
    for intent in STRATEGIC_INTENTS:
        if intent in present:
            return intent
    return None
