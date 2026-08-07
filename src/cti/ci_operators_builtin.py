"""JP 重要インフラ 指定事業者名簿の BUILTIN 既定 (データ専用モジュール)。

**事業者名 → NISC 分野のみ**の公知情報名簿。運用 SSoT は DB (config_store key
"jp_ci_operators"、UI 編集・版履歴)。本モジュールは初回 seed + DB 不達時の fail-safe。

⚠ 統治境界 (設計 doc §8): この名簿に**技術・製品・露出情報の列を将来も足さない**。
事業者→資産のマッピングは「攻撃者の標的パッケージ」であり構築禁止 (確定原則)。

行形式: (id, canonical, aliases, nisc_sector)。
- alias は報道での別表記 (和名/英名/略称/主要子会社)。**短い 2 文字 CJK ブランド略称
  (東急/京王/近鉄 等) は入れない** — 部分一致でホテル・不動産等のグループ非 CI 子会社に
  誤爆する (length-aware 教訓)。正式社名は層2 型判定 (〜電鉄/〜鉄道) が拾う。
- 型判定 (層2) で拾える事業者も、系統重要な大手は「指定事業者」バッジのため収載する。
"""

from __future__ import annotations

# (id, canonical, aliases, sector)
BUILTIN_OPERATORS_DATA: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    # ---------- 電力 ----------
    (
        "tepco",
        "東京電力ホールディングス",
        ("東京電力", "TEPCO", "東京電力パワーグリッド", "東京電力エナジーパートナー"),
        "electricity",
    ),
    ("kepco_kansai", "関西電力", ("関西電力送配電",), "electricity"),
    ("chubu_electric", "中部電力", ("中部電力パワーグリッド", "中部電力ミライズ"), "electricity"),
    ("kyushu_electric", "九州電力", ("九州電力送配電",), "electricity"),
    ("tohoku_electric", "東北電力", ("東北電力ネットワーク",), "electricity"),
    ("chugoku_electric", "中国電力", ("中国電力ネットワーク",), "electricity"),
    ("shikoku_electric", "四国電力", ("四国電力送配電",), "electricity"),
    ("hokkaido_electric", "北海道電力", ("北海道電力ネットワーク",), "electricity"),
    ("hokuriku_electric", "北陸電力", ("北陸電力送配電",), "electricity"),
    ("okinawa_electric", "沖縄電力", (), "electricity"),
    ("jera", "JERA", ("ジェラ",), "electricity"),
    ("jpower", "電源開発", ("J-POWER", "Jパワー", "電源開発送変電ネットワーク"), "electricity"),
    ("genden", "日本原子力発電", ("原電",), "electricity"),
    # 発電・送電の特定社会基盤事業者 (経済安保指定 令和8年7月版。IPP/共同火力/洋上風力/送電)
    ("tepco_renewable", "東京電力リニューアブルパワー", (), "electricity"),
    ("kashima_power", "鹿島パワー", (), "electricity"),
    (
        "kobelco_power",
        "コベルコパワー神戸",
        ("コベルコパワー真岡", "コベルコパワー神戸第二"),
        "electricity",
    ),
    ("chiba_sodegaura_power", "千葉袖ケ浦パワー", (), "electricity"),
    ("joban_kyodo", "常磐共同火力", (), "electricity"),
    ("soma_kyodo", "相馬共同火力発電", (), "electricity"),
    ("tsugaru_offshore", "つがるオフショアエナジー", (), "electricity"),
    ("nakoso_igcc", "勿来IGCCパワー", (), "electricity"),
    ("hirono_igcc", "広野IGCCパワー", (), "electricity"),
    ("hibiki_power", "ひびき発電", (), "electricity"),
    ("himeji_gas_power", "姫路天然ガス発電", ("姫路天然ガス発電3号",), "electricity"),
    ("fukushima_gas_power", "福島ガス発電", (), "electricity"),
    ("akita_yurihonjo_wind", "秋田由利本荘オフショアウィンド", (), "electricity"),
    ("murakami_tainai_wind", "村上胎内洋上風力発電", (), "electricity"),
    ("fukushima_souden", "福島送電", (), "electricity"),
    ("hokkaido_hokubu_wind", "北海道北部風力送電", (), "electricity"),
    ("eflow", "E-Flow", (), "electricity"),
    ("enelx_japan", "エネルエックス・ジャパン", (), "electricity"),
    # 発電事業 (50万kW+) として指定された複合事業者。主業は製鉄だが指定役務に従い電力に収載
    ("nippon_steel", "日本製鉄", (), "electricity"),
    # ---------- ガス ----------
    ("tokyo_gas", "東京ガス", ("東京瓦斯",), "gas"),
    ("osaka_gas", "大阪ガス", ("大阪瓦斯", "Daigas"), "gas"),
    ("toho_gas", "東邦ガス", (), "gas"),
    ("saibu_gas", "西部ガス", (), "gas"),
    ("hokkaido_gas", "北海道ガス", (), "gas"),
    ("keiyo_gas", "京葉ガス", (), "gas"),
    ("shizuoka_gas", "静岡ガス", (), "gas"),
    ("nichigas", "日本瓦斯", ("ニチガス",), "gas"),
    ("hiroshima_gas", "広島ガス", (), "gas"),
    ("hokuriku_gas", "北陸ガス", (), "gas"),
    ("sendai_gas", "仙台市ガス局", (), "gas"),
    ("energy_sora", "エナジー宇宙", (), "gas"),
    ("ogishima_gas", "扇島都市ガス供給", (), "gas"),
    ("inpex", "INPEX JAPAN", ("INPEX", "国際石油開発帝石"), "gas"),
    ("japex", "石油資源開発", ("JAPEX",), "gas"),
    ("shimizu_lng", "清水エル・エヌ・ジー", (), "gas"),
    ("hibiki_lng", "ひびきエル・エヌ・ジー", (), "gas"),
    ("daigas_gps", "Daigasガスアンドパワーソリューション", (), "gas"),
    # ---------- 石油 ----------
    ("eneos", "ENEOS", ("エネオス", "ENEOSホールディングス", "JX金属"), "oil"),
    ("idemitsu", "出光興産", ("出光", "昭和シェル"), "oil"),
    ("cosmo", "コスモ石油", ("コスモエネルギーホールディングス", "コスモエネルギー"), "oil"),
    ("taiyo_oil", "太陽石油", (), "oil"),
    ("fuji_oil_co", "富士石油", (), "oil"),
    ("kygnus", "キグナス石油", (), "oil"),
    # 石油精製・LPG 輸入の特定社会基盤事業者 (経済安保指定)。
    # ⚠意図的除外: 全国農業協同組合連合会 (LPG輸入で指定) — 組織の主機能は農業流通であり
    # 石油分野への配置は誤誘導。型判定 (農業協同組合→finance) は JA バンク文脈で許容。
    ("osaka_intl_refining", "大阪国際石油精製", (), "oil"),
    ("kashima_aromatics", "鹿島アロマティックス", (), "oil"),
    ("kashima_oil", "鹿島石油", (), "oil"),
    ("showa_yokkaichi_oil", "昭和四日市石油", (), "oil"),
    ("toa_oil", "東亜石油", (), "oil"),
    ("astomos", "アストモスエネルギー", (), "oil"),
    ("iwatani", "岩谷産業", (), "oil"),
    ("eneos_globe", "ENEOSグローブ", (), "oil"),
    ("japan_gas_energy", "ジャパンガスエナジー", (), "oil"),
    ("gyxis", "ジクシス", (), "oil"),
    # ---------- 水道 (給水人口100万人超の水道事業 + 主要用水供給、経済安保指定) ----------
    ("sapporo_water", "札幌市水道局", (), "water"),
    ("sendai_water", "仙台市水道局", (), "water"),
    ("saitama_city_water", "さいたま市水道局", (), "water"),
    ("chiba_pref_water", "千葉県営水道", ("千葉県水道局",), "water"),
    ("tokyo_water", "東京都水道局", (), "water"),
    ("kanagawa_pref_water", "神奈川県営水道", ("神奈川県水道局",), "water"),
    ("yokohama_water", "横浜市水道局", (), "water"),
    ("kawasaki_water", "川崎市上下水道局", ("川崎市水道局",), "water"),
    ("nagoya_water", "名古屋市上下水道局", ("名古屋市水道局",), "water"),
    ("kyoto_water", "京都市上下水道局", ("京都市水道局",), "water"),
    ("osaka_city_water", "大阪市水道局", (), "water"),
    ("kobe_water", "神戸市水道局", (), "water"),
    ("hiroshima_water", "広島市水道局", (), "water"),
    ("kitakyushu_water", "北九州市上下水道局", ("北九州市水道局",), "water"),
    ("fukuoka_water", "福岡市水道局", (), "water"),
    ("kitachiba_water", "北千葉広域水道企業団", (), "water"),
    ("kanagawa_water_supply", "神奈川県内広域水道企業団", (), "water"),
    ("osaka_wide_water", "大阪広域水道企業団", (), "water"),
    ("hanshin_water", "阪神水道企業団", (), "water"),
    # ---------- 情報通信 ----------
    ("ntt", "日本電信電話", ("NTT", "NTTグループ"), "it_telecom"),
    ("ntt_east", "NTT東日本", ("東日本電信電話",), "it_telecom"),
    ("ntt_west", "NTT西日本", ("西日本電信電話",), "it_telecom"),
    ("ntt_docomo", "NTTドコモ", ("ドコモ", "docomo"), "it_telecom"),
    ("ntt_com", "NTTコミュニケーションズ", ("NTT Com", "NTTドコモビジネス"), "it_telecom"),
    ("ntt_ltd_japan", "NTTリミテッド・ジャパン", (), "it_telecom"),
    ("ntt_data", "NTTデータ", ("NTT DATA",), "it_telecom"),
    ("kddi", "KDDI", (), "it_telecom"),
    ("softbank", "ソフトバンク", ("SoftBank",), "it_telecom"),
    ("rakuten_mobile", "楽天モバイル", (), "it_telecom"),
    ("iij", "インターネットイニシアティブ", ("IIJ",), "it_telecom"),
    ("biglobe", "ビッグローブ", ("BIGLOBE",), "it_telecom"),
    ("nifty", "ニフティ", ("@nifty",), "it_telecom"),
    ("sonet", "ソニーネットワークコミュニケーションズ", ("So-net",), "it_telecom"),
    ("jcom", "J:COM", ("JCOM", "ジュピターテレコム"), "it_telecom"),
    ("optage", "オプテージ", ("OPTAGE", "ケイ・オプティコム"), "it_telecom"),
    ("ctc_chubu", "中部テレコミュニケーション", (), "it_telecom"),
    ("stnet", "STNet", (), "it_telecom"),
    ("qtnet", "QTnet", (), "it_telecom"),
    ("enecom", "エネコム", (), "it_telecom"),
    ("arteria", "アルテリア・ネットワークス", ("アルテリア",), "it_telecom"),
    ("sakura_internet", "さくらインターネット", (), "it_telecom"),
    ("okinawa_cellular", "沖縄セルラー電話", ("沖縄セルラー",), "it_telecom"),
    ("line_yahoo", "LINEヤフー", ("ヤフー",), "it_telecom"),
    # 主要 ISP 補完 + インターネット基盤 (ISP は被害者かつ攻撃インフラ化 = pre-positioning の主戦場)
    ("gmo_internet", "GMOインターネットグループ", ("GMOインターネット",), "it_telecom"),
    ("idc_frontier", "IDCフロンティア", (), "it_telecom"),
    ("jprs", "日本レジストリサービス", ("JPRS",), "it_telecom"),  # .jp DNS = 集中ノード
    ("jpnic", "日本ネットワークインフォメーションセンター", ("JPNIC",), "it_telecom"),
    ("skyperfect_jsat", "スカパーJSAT", ("スカパー", "SKY Perfect JSAT"), "it_telecom"),  # 衛星通信
    # 放送 (経済安保「放送」指定。NISC/NCO 情報通信分野は放送を含むため it_telecom に収載)
    ("nhk", "日本放送協会", ("NHK",), "it_telecom"),
    ("ntv", "日本テレビ放送網", ("日本テレビ", "日テレ"), "it_telecom"),
    ("tv_asahi", "テレビ朝日", (), "it_telecom"),
    ("tbs", "TBSテレビ", ("TBS",), "it_telecom"),
    ("fuji_tv", "フジテレビジョン", ("フジテレビ",), "it_telecom"),
    ("tv_tokyo", "テレビ東京", (), "it_telecom"),
    # ---------- 金融 ----------
    ("boj", "日本銀行", ("日銀",), "finance"),
    ("mufg_bank", "三菱UFJ銀行", ("三菱UFJフィナンシャル・グループ", "MUFG"), "finance"),
    ("smbc_bank", "三井住友銀行", ("三井住友フィナンシャルグループ", "SMBC"), "finance"),
    ("mizuho_bank", "みずほ銀行", ("みずほフィナンシャルグループ", "みずほFG"), "finance"),
    ("resona", "りそな銀行", ("りそなホールディングス",), "finance"),
    ("smtb", "三井住友信託銀行", (), "finance"),
    ("jp_bank", "ゆうちょ銀行", (), "finance"),
    ("nomura", "野村證券", ("野村ホールディングス", "野村証券"), "finance"),
    ("daiwa_sec", "大和証券", ("大和証券グループ本社",), "finance"),
    ("smbc_nikko", "SMBC日興証券", ("日興証券",), "finance"),
    ("sbi_sec", "SBI証券", (), "finance"),
    ("rakuten_bank", "楽天銀行", (), "finance"),
    ("rakuten_sec", "楽天証券", (), "finance"),
    ("zengin_net", "全国銀行資金決済ネットワーク", ("全銀ネット", "全銀システム"), "finance"),
    ("jpx", "日本取引所グループ", ("JPX", "東京証券取引所", "東証", "大阪取引所"), "finance"),
    ("aflac", "アフラック生命保険", ("アフラック", "Aflac"), "finance"),
    ("nippon_life", "日本生命", (), "finance"),
    ("dai_ichi_life", "第一生命", (), "finance"),
    ("meiji_yasuda", "明治安田生命", (), "finance"),
    ("sumitomo_life", "住友生命", (), "finance"),
    ("kampo", "かんぽ生命", (), "finance"),
    ("tokio_marine", "東京海上日動", ("東京海上",), "finance"),
    ("sompo_japan", "損保ジャパン", ("損害保険ジャパン", "SOMPO"), "finance"),
    ("ms_ad", "三井住友海上", (), "finance"),
    ("aioi_nissay", "あいおいニッセイ同和損害保険", ("あいおいニッセイ同和損保",), "finance"),
    # 銀行・系統中央・市場インフラ・資金移動の特定社会基盤事業者 (経済安保指定)
    ("mufg_trust", "三菱UFJ信託銀行", (), "finance"),
    ("seven_bank", "セブン銀行", (), "finance"),
    ("lawson_bank", "ローソン銀行", (), "finance"),
    ("joyo_bank", "常陽銀行", (), "finance"),
    ("chiba_bank", "千葉銀行", (), "finance"),
    ("yokohama_bank", "横浜銀行", ("コンコルディア・フィナンシャルグループ",), "finance"),
    ("shizuoka_bank", "静岡銀行", (), "finance"),
    ("fukuoka_bank", "福岡銀行", ("ふくおかフィナンシャルグループ",), "finance"),
    ("hokuyo_bank", "北洋銀行", (), "finance"),
    ("saitama_resona", "埼玉りそな銀行", (), "finance"),
    ("sbi_shinsei", "SBI新生銀行", (), "finance"),
    ("nishi_nippon_city", "西日本シティ銀行", (), "finance"),
    ("sumishin_sbi", "住信SBIネット銀行", (), "finance"),
    ("paypay_bank", "PayPay銀行", (), "finance"),
    ("aeon_bank", "イオン銀行", (), "finance"),
    ("shinkin_central", "信金中央金庫", (), "finance"),
    ("rokinren", "労働金庫連合会", (), "finance"),
    ("zenshinkumiren", "全国信用協同組合連合会", (), "finance"),
    ("norinchukin", "農林中央金庫", (), "finance"),
    ("merpay", "メルペイ", (), "finance"),
    ("paypay", "PayPay", (), "finance"),
    ("mizuho_sec", "みずほ証券", (), "finance"),
    ("mumss", "三菱UFJモルガン・スタンレー証券", ("三菱UFJモルガンスタンレー証券",), "finance"),
    ("tfx", "東京金融取引所", (), "finance"),
    ("jscc", "日本証券クリアリング機構", ("JSCC",), "finance"),
    ("hofuri_clearing", "ほふりクリアリング", (), "finance"),
    ("custody_bank", "日本カストディ銀行", (), "finance"),
    ("master_trust", "日本マスタートラスト信託銀行", (), "finance"),
    ("deposit_insurance", "預金保険機構", (), "finance"),
    ("agri_deposit_insurance", "農水産業協同組合貯金保険機構", (), "finance"),
    ("jasdec", "証券保管振替機構", ("ほふり",), "finance"),
    ("densai_jbank", "日本電子債権機構", (), "finance"),
    ("mizuho_densai", "みずほ電子債権記録", (), "finance"),
    ("zengin_densai", "全銀電子債権ネットワーク", (), "finance"),
    # ---------- クレジット ----------
    ("jcb", "ジェーシービー", ("JCB",), "credit"),
    ("smcc", "三井住友カード", (), "credit"),
    ("rakuten_card", "楽天カード", (), "credit"),
    ("mufg_nicos", "三菱UFJニコス", ("ニコス", "NICOS"), "credit"),
    ("credit_saison", "クレディセゾン", ("セゾンカード",), "credit"),
    ("orico", "オリエントコーポレーション", ("オリコ",), "credit"),
    ("jaccs", "ジャックス", (), "credit"),
    ("aeon_financial", "イオンフィナンシャルサービス", ("イオンクレジットサービス",), "credit"),
    ("toyota_finance", "トヨタファイナンス", (), "credit"),
    ("cardnet", "日本カードネットワーク", (), "credit"),
    # クレカ・前払式支払手段の特定社会基盤事業者 (経済安保指定)
    ("seven_card", "セブン・カードサービス", ("セブンカード",), "credit"),
    ("paypay_card", "PayPayカード", (), "credit"),
    ("au_financial", "auフィナンシャルサービス", (), "credit"),
    ("docomo_financial", "NTTドコモ・フィナンシャルグループ", (), "credit"),
    ("pasmo", "パスモ", ("PASMO",), "credit"),
    ("rakuten_edy", "楽天Edy", (), "credit"),
    # 信用情報機関・主要決済代行 (与信・決済の集中点。NCO クレジット分野の指定信用情報機関等)
    ("cic", "シー・アイ・シー", ("CIC",), "credit"),
    ("jicc", "日本信用情報機構", ("JICC",), "credit"),
    ("gmo_pg", "GMOペイメントゲートウェイ", (), "credit"),
    ("sb_payment", "SBペイメントサービス", (), "credit"),
    ("sony_payment", "ソニーペイメントサービス", (), "credit"),
    # ---------- 鉄道 ----------
    ("jr_east", "JR東日本", ("東日本旅客鉄道",), "railway"),
    ("jr_central", "JR東海", ("東海旅客鉄道",), "railway"),
    ("jr_west", "JR西日本", ("西日本旅客鉄道",), "railway"),
    ("jr_kyushu", "JR九州", ("九州旅客鉄道",), "railway"),
    ("jr_hokkaido", "JR北海道", ("北海道旅客鉄道",), "railway"),
    ("jr_shikoku", "JR四国", ("四国旅客鉄道",), "railway"),
    ("jr_freight", "JR貨物", ("日本貨物鉄道",), "railway"),
    ("tokyo_metro", "東京メトロ", ("東京地下鉄",), "railway"),
    ("toei", "東京都交通局", ("都営地下鉄",), "railway"),
    ("osaka_metro", "Osaka Metro", ("大阪メトロ", "大阪市高速電気軌道"), "railway"),
    # 大手私鉄の正式社名は層2 型判定 (〜電鉄/〜鉄道) が拾う
    # ---------- 航空 ----------
    ("ana", "ANAホールディングス", ("ANA", "全日本空輸", "全日空"), "aviation"),
    ("jal", "日本航空", ("JAL",), "aviation"),
    ("skymark", "スカイマーク", (), "aviation"),
    ("solaseed", "ソラシドエア", (), "aviation"),
    ("starflyer", "スターフライヤー", (), "aviation"),
    ("peach", "Peach Aviation", ("ピーチ・アビエーション",), "aviation"),
    ("jetstar_japan", "ジェットスター・ジャパン", (), "aviation"),
    ("airdo", "AIRDO", ("エア・ドゥ",), "aviation"),
    # ---------- 空港 ----------
    ("naa", "成田国際空港", ("NAA",), "airport"),
    ("jat", "日本空港ビルデング", (), "airport"),
    ("kansai_airports", "関西エアポート", (), "airport"),
    ("centrair", "中部国際空港", ("セントレア",), "airport"),
    ("hokkaido_airports", "北海道エアポート", (), "airport"),
    ("shin_kansai_airport", "新関西国際空港", (), "airport"),
    ("fukuoka_intl_airport", "福岡国際空港", (), "airport"),
    # ---------- 物流 ----------
    ("yamato", "ヤマト運輸", ("ヤマトホールディングス", "クロネコヤマト"), "logistics"),
    ("sagawa", "佐川急便", ("SGホールディングス",), "logistics"),
    ("nittsu", "日本通運", ("NXホールディングス", "NIPPON EXPRESS"), "logistics"),
    ("japan_post", "日本郵便", ("日本郵政",), "logistics"),
    ("seino", "西濃運輸", ("セイノーホールディングス",), "logistics"),
    ("fukuyama", "福山通運", (), "logistics"),
    ("logisteed", "ロジスティード", ("日立物流",), "logistics"),
    ("kwe", "近鉄エクスプレス", (), "logistics"),
    ("yusen_logistics", "郵船ロジスティクス", (), "logistics"),
    # ---------- 港湾 ----------
    ("nyk", "日本郵船", ("NYK",), "port"),
    ("mol", "商船三井", (), "port"),
    ("kline", "川崎汽船", (), "port"),
    ("one_shipping", "Ocean Network Express", (), "port"),
    ("kamigumi", "上組", (), "port"),
    ("mitsui_soko", "三井倉庫", ("三井倉庫ホールディングス",), "port"),
    ("sumitomo_soko", "住友倉庫", (), "port"),
    ("mitsubishi_soko", "三菱倉庫", (), "port"),
    ("nagoya_port", "名古屋港運協会", (), "port"),
    # 一般港湾運送の特定社会基盤事業者 (経済安保指定、主要コンテナターミナル荷役 32 者)
    ("asahi_unyu", "旭運輸", (), "port"),
    ("azuma_shipping", "東海運", (), "port"),
    ("isewan_kaiun", "伊勢湾海運", (), "port"),
    ("utoc", "株式会社宇徳", ("宇徳",), "port"),
    ("kinki_koun", "近畿港運", (), "port"),
    ("keihin_koun", "京濱港運", ("京浜港運",), "port"),
    ("sankyu", "山九", (), "port"),
    ("genec", "ジェネック", (), "port"),
    ("shosen_koun", "商船港運", (), "port"),
    ("suzue", "鈴江コーポレーション", (), "port"),
    ("sogo_unyu", "相互運輸", (), "port"),
    ("daiichi_koun", "第一港運", (), "port"),
    ("daito_corp", "ダイトーコーポレーション", (), "port"),
    ("tatsumi_shokai", "辰巳商会", (), "port"),
    ("tokai_kyowa", "東海協和", (), "port"),
    ("tokyo_intl_wharf", "東京国際埠頭", (), "port"),
    ("toyo_wharf", "東洋埠頭", (), "port"),
    ("nickel_lions", "ニッケル．エンド．ライオンス", ("ニッケルエンドライオンス",), "port"),
    # 「日新」は 2 文字中核名で他社 (日新電機等) に誤爆するため会社形態語込みで収載
    ("nissin_port", "株式会社日新", (), "port"),
    ("nitto_butsuryu", "日東物流", (), "port"),
    ("hakata_koun", "博多港運", (), "port"),
    ("fujitrans", "フジトランスコーポレーション", ("フジトランス",), "port"),
    ("maruzen_showa", "丸全昭和運輸", (), "port"),
    ("mitsui_soko_port", "三井倉庫港運", (), "port"),
    ("meiko_kaiun", "名港海運", (), "port"),
    ("yusen_koun", "郵船港運", (), "port"),
    ("uniex_nct", "ユニエツクスNCT", ("ユニエックスNCT",), "port"),
    # ---------- 医療 ----------
    ("nho", "国立病院機構", (), "medical"),
    ("ncc", "国立がん研究センター", (), "medical"),
    ("jrc_medical", "日本赤十字社", ("日赤",), "medical"),
    ("saiseikai", "済生会", (), "medical"),
    ("tokushukai", "徳洲会", (), "medical"),
    ("jcho", "地域医療機能推進機構", ("JCHO",), "medical"),
    # ---------- 化学 ----------
    ("mitsubishi_chemical", "三菱ケミカル", ("三菱ケミカルグループ",), "chemical"),
    ("sumitomo_chemical", "住友化学", (), "chemical"),
    ("asahi_kasei", "旭化成", (), "chemical"),
    ("shin_etsu", "信越化学工業", ("信越化学",), "chemical"),
    ("mitsui_chemicals", "三井化学", (), "chemical"),
    ("tosoh", "東ソー", (), "chemical"),
    ("resonac", "レゾナック", ("昭和電工",), "chemical"),
    ("dic_corp", "DIC", (), "chemical"),
    ("kaneka", "カネカ", (), "chemical"),
    ("kuraray", "クラレ", (), "chemical"),
    ("nippon_shokubai", "日本触媒", (), "chemical"),
    ("toray", "東レ", (), "chemical"),
    ("teijin", "帝人", (), "chemical"),
    # ---------- 政府・行政 (省庁・自治体は層2 型判定が拾う。ここは非サフィックス組織) ----------
    ("cabinet_secretariat", "内閣官房", (), "government"),
    ("nisc_gov", "内閣サイバーセキュリティセンター", ("NISC",), "government"),
    ("digital_agency", "デジタル庁", (), "government"),
    ("soumu", "総務省", (), "government"),
    ("mofa", "外務省", (), "government"),
    ("mof_japan", "財務省", (), "government"),
    ("meti", "経済産業省", ("経産省",), "government"),
    ("npa", "警察庁", (), "government"),
    ("fsa", "金融庁", (), "government"),
    ("mhlw", "厚生労働省", ("厚労省",), "government"),
    ("mlit", "国土交通省", ("国交省",), "government"),
    ("nenkin", "日本年金機構", (), "government"),
    ("jlis", "地方公共団体情報システム機構", ("J-LIS",), "government"),
    ("jaxa", "宇宙航空研究開発機構", ("JAXA",), "government"),
    ("ipa_gov", "情報処理推進機構", ("IPA",), "government"),
    # ---------- 防衛 ----------
    ("mod", "防衛省", (), "defense"),
    ("atla", "防衛装備庁", (), "defense"),
    ("jsdf", "自衛隊", ("陸上自衛隊", "海上自衛隊", "航空自衛隊", "統合幕僚監部"), "defense"),
    ("mhi", "三菱重工業", ("三菱重工", "MHI"), "defense"),
    ("khi", "川崎重工業", ("川崎重工",), "defense"),
    ("ihi", "IHI", (), "defense"),
    # 三菱電機/NEC/富士通 = 防衛電子の主要 3 プライムかつ 2020 年の実標的。多角化巨大企業
    # だが DIB identity が顕著なため defense に維持 (日立/東芝/ダイキン等の民生主体は非収載 =
    # 型/canonical で民生分野へ。過剰帰属再発を防ぐ、設計 doc §11)。
    ("melco", "三菱電機", (), "defense"),
    ("nec", "NEC", ("日本電気",), "defense"),
    ("fujitsu", "富士通", (), "defense"),
    ("jsw", "日本製鋼所", (), "defense"),
    # 防衛専業/準専業 (防衛が主 identity。装備庁の主要調達先、公知)
    ("shinmaywa", "新明和工業", (), "defense"),  # US-2 飛行艇
    ("howa", "豊和工業", (), "defense"),  # 小火器
    ("hosoya_pyro", "細谷火工", (), "defense"),  # 火工品
    ("nippon_koki", "日本工機", (), "defense"),  # 火薬・弾薬
    ("mitsubishi_precision", "三菱プレシジョン", (), "defense"),  # 誘導・シミュレータ
    ("meisei", "明星電気", (), "defense"),  # 防衛電子・気象
    ("pasco", "パスコ", (), "defense"),  # 空間情報・衛星 (2020 標的)
    ("nippon_avionics", "日本アビオニクス", ("アビオニクス",), "defense"),  # 赤外線・防衛電子
    ("tamagawa_seiki", "多摩川精機", (), "defense"),  # ジャイロ・航法
    # 造船 (護衛艦・潜水艦。JMU/三井E&S/MHI が主要建造。造船クラスは民生誤爆のため roster 明示)
    ("jmu", "ジャパンマリンユナイテッド", ("JMU", "ジャパン マリンユナイテッド"), "defense"),
    ("mitsui_es", "三井E&S", ("三井E&Sホールディングス", "三井造船"), "defense"),
    ("kanadevia", "カナデビア", ("日立造船",), "defense"),  # 艦艇・機雷
    # 防衛電子は**子会社名で収載** (親の民生侵害を defense に誤流入させない = 過剰帰属回避)
    ("toshiba_infra", "東芝インフラシステムズ", (), "defense"),  # ミサイル・レーダー
    ("hitachi_kokusai", "日立国際電気", (), "defense"),  # 防衛通信
    ("oki", "沖電気工業", ("OKI",), "defense"),  # ソナー・防衛電子
    ("jrc", "日本無線", ("JRC",), "defense"),  # 防衛レーダー・通信
    ("furuno", "古野電気", (), "defense"),  # ソナー・レーダー
    ("tokyo_keiki", "東京計器", (), "defense"),  # 航法・誘導
    ("nof_corp", "日油", (), "defense"),  # 推進薬・火薬
    ("sumitomo_heavy", "住友重機械工業", (), "defense"),  # 機関砲・装甲
    ("ishikawa_seisakusho", "石川製作所", (), "defense"),  # 機雷
    ("ihi_aerospace", "IHIエアロスペース", (), "defense"),  # ロケット・ミサイル推進
    ("mitsubishi_space_sw", "三菱スペース・ソフトウエア", (), "defense"),  # 防衛宇宙 SW
    ("mss_mitsubishi", "三菱防衛・宇宙", (), "defense"),  # 三菱電機系 防需
    ("nird", "防衛研究所", (), "defense"),  # シンクタンク (諜報標的)
    (
        "subaru_aero",
        "SUBARU航空宇宙カンパニー",
        (),
        "defense",
    ),  # UH-2/AH 生産 (親 SUBARU は民生主体で非収載)
)
