"""地政学の救済の語の関門 (``geo_rescue_cues.geo_cue_reasons``) のテスト。"""

from __future__ import annotations

import pytest

from src.pipeline.geo_rescue_cues import geo_cue_reasons, is_cyber_geo_cue


@pytest.mark.parametrize(
    ("title", "text", "expected"),
    [
        ("Chinese coast guard ships enter disputed waters", "", "nation:CN"),
        ("Ministry statement", "Officials in Moscow said the unit moved.", "nation:RU"),
        ("North Korean launch", "", "nation:KP"),
        ("IRGC navy seizes tanker", "", "nation:IR"),
        ("Houthis claim strike on vessel", "", "proxy:houthi"),
        ("Militia attack", "Kataib Hezbollah claimed it.", "proxy:iraq_militia"),
        ("Africa Corps convoy ambushed", "", "proxy:wagner"),
        ("Ukrainian air defence downs drones", "", "war:ukraine"),
        ("Navy patrols the Taiwan Strait", "", "theatre:taiwan_strait"),
        ("Survey vessel in the Baltic Sea", "", "theatre:baltic"),
        ("Air policing on NATO's eastern flank", "", "theatre:nato_east"),
        ("Mine clearance in the Black Sea", "", "theatre:black_sea"),
        ("Jets scrambled after airspace violation", "", "trigger:airspace violation"),
        ("Submarine rotation under AUKUS", "", "framework:aukus"),
        ("New battalion to strengthen deterrence", "", "framework:deterrence"),
        ("防衛省が南シナ海で共同訓練", "", "theatre:south_china_sea"),
        ("領空侵犯を受けて警戒を強化", "", "trigger:領空侵犯"),
        # 定義 D (注視国の名前が無くても関門を開ける)
        ("Government publishes national cyber strategy", "", "cyber_geo:strategy"),
        ("Platform removes influence operation network", "", "cyber_geo:influence"),
        ("EU warns of hybrid threats", "", "cyber_geo:hybrid"),
        ("Damaged undersea cables investigated", "", "cyber_geo:infrastructure_sabotage"),
        ("偽情報の拡散を確認", "", "cyber_geo:influence"),
        ("海底ケーブルの切断を調査", "", "cyber_geo:infrastructure_sabotage"),
        ("能動的サイバー防御の法案", "", "cyber_geo:strategy"),
        # 2026-10-30 改訂で足した語 (定義から)
        ("NATOの東側で防空を強化", "", "theatre:nato_east"),
        ("Eastern-flank allies boost air defence", "", "theatre:nato_east"),
        ("Gulf states raise alert", "", "theatre:persian_gulf"),
        ("湾岸諸国が警戒を強める", "", "theatre:persian_gulf"),
        ("露軍が東部で攻勢", "", "nation:RU"),
        ("露朝の軍事協力", "", "nation:KP"),
        ("露ウの停戦協議", "", "nation:RU"),
        ("中台の緊張が高まる", "", "nation:CN"),
        ("日朝の協議", "", "nation:KP"),
        ("CCP officials announce drills", "", "nation:CN"),
        ("PLA Navy carrier group transits", "", "nation:CN"),
        ("Airports close after drone sightings", "", "trigger:drone sightings"),
        ("ドローンの飛来で空港を閉鎖", "", "trigger:ドローンの飛来"),
        ("日米比の共同訓練", "", "framework:日米比"),
        ("米韓の連合演習", "", "framework:米韓"),
        ("Quad leaders agree maritime initiative", "", "framework:quad"),
        ("Wagner Group fighters redeploy", "", "proxy:wagner"),
        # 2026-10-30 改訂 2
        ("日米の共同訓練", "", "framework:日米"),
        ("US-Japan alliance drills", "", "framework:us-japan"),
        ("U.S.-ROK combined exercise", "", "framework:u.s.-rok"),
        ("US-Philippines patrol", "", "framework:us-philippines"),
        ("Trilateral defense ministers meet", "", "framework:trilateral_security"),
        ("親露派の武装勢力", "", "nation:RU"),
        ("対露制裁を強化", "", "nation:RU"),
        ("露外相が会見", "", "nation:RU"),
        ("露国防省が発表", "", "nation:RU"),
    ],
)
def test_cue_detected(title: str, text: str, expected: str) -> None:
    assert expected in geo_cue_reasons(title, text)


@pytest.mark.parametrize(
    ("title", "text"),
    [
        # 結び付きの書かれていない同盟国・同志国の一般の調達・試験・産業 (誤りの型 A)
        ("Coast guard to acquire unmanned surface vessels", "The agency signed a contract."),
        ("Army reorganises its artillery battalions", "The new structure takes effect next year."),
        ("Navy orders naval guns for frigates", "Deliveries begin in 2028."),
        ("Firm opens sonar factory", "The plant will employ 200 people."),
        # 地理だけ (台湾・韓国・欧州の国名) は手掛かりにしない
        ("Taiwan agency buys drones", "South Korean and European suppliers bid."),
        # 語境界: 部分一致しない
        ("Determined engineers test a new radar", "The plasma cutter was replaced."),
        # 広すぎる当たりを消した語 (2026-10-30)
        ("中国地方の電力会社", "中国電力が設備を更新した。"),
        ("不正侵入の被害を公表", ""),
        ("Printer review", "We tested PLA filament and PLA plastic samples."),
        ("Opera season opens", "A new staging of Wagner was praised."),
        ("Network intrusion detection product", "The vendor released an update."),
        # trilateral は安全保障の語が無ければ手掛かりにしない
        ("Trilateral trade talks resume", "Ministers discussed tariffs."),
        ("露出の多い衣装", ""),
        ("Persistent identifiers", "The library uses purl links."),
    ],
)
def test_no_cue_for_unlinked_military_news(title: str, text: str) -> None:
    assert geo_cue_reasons(title, text) == ()


def test_cyber_geo_cue_is_separated_from_geo_cues() -> None:
    assert is_cyber_geo_cue("cyber_geo:hybrid")
    assert not is_cyber_geo_cue("nation:RU")
    out = geo_cue_reasons("Russian disinformation campaign", "")
    assert "nation:RU" in out and "cyber_geo:influence" in out


def test_reasons_are_unique_and_ordered() -> None:
    out = geo_cue_reasons("Russian drones over the Baltic", "Russia said... Russian units...")
    assert out[0] == "nation:RU"
    assert len(out) == len(set(out))
