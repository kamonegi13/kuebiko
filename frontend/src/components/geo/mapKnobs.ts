// 脅威マップの表示フィルタ (localStorage 永続) の共有定義。
// MapPage (読み書き) と dashboard の geo widget (読み取り = ページの見え方を鏡写し) が
// 同一の key・既定値を使う。二重定義による drift (widget とページで既定が食い違い
// 「同じ 24h なのに表示が違う」) を防ぐため、key/既定値の定義はこのファイルに一元化する。

import type { Dispatch, SetStateAction } from "react";
import { usePersistedState } from "../../utils/usePersistedState";
import type { ColorBy, MapLayer } from "./LeafletThreatMap";
import type {
  MinImportance,
  PmesiiAxis,
  SourceStatus,
  ThreatClass,
  TimeBasis,
} from "../../api/geo";

// setter は usePersistedState (useState 互換) のまま = 関数型更新 (prev => next) も通す。
type Set<T> = Dispatch<SetStateAction<T>>;

export interface MapKnobs {
  layer: MapLayer;
  setLayer: Set<MapLayer>;
  selectedSector: string | null;
  setSelectedSector: Set<string | null>;
  selectedIntent: string | null;
  setSelectedIntent: Set<string | null>;
  threatClass: ThreatClass;
  setThreatClass: Set<ThreatClass>;
  sourceStatus: SourceStatus;
  setSourceStatus: Set<SourceStatus>;
  minImportance: MinImportance;
  setMinImportance: Set<MinImportance>;
  pmesii: PmesiiAxis;
  setPmesii: Set<PmesiiAxis>;
  colorBy: ColorBy;
  setColorBy: Set<ColorBy>;
  timeBasis: TimeBasis;
  setTimeBasis: Set<TimeBasis>;
}

export function useMapKnobs(): MapKnobs {
  const [layer, setLayer] = usePersistedState<MapLayer>("cti.map.layer", "cyber");
  const [selectedSector, setSelectedSector] = usePersistedState<string | null>(
    "cti.map.sector",
    null,
  );
  // 動機ファセット (色分け「動機」モード): 選択動機で両レイヤーを地理的に絞る (セクターと対称)。
  const [selectedIntent, setSelectedIntent] = usePersistedState<string | null>(
    "cti.map.intent",
    null,
  );
  const [threatClass, setThreatClass] = usePersistedState<ThreatClass>("cti.map.threat", "all");
  const [sourceStatus, setSourceStatus] = usePersistedState<SourceStatus>(
    "cti.map.sourceStatus",
    "all",
  );
  // 意義の地図 (A): 既定は「中+」= 低重要度 (台帳ランサム等) の量バイアスを既定で剥がす。
  const [minImportance, setMinImportance] = usePersistedState<MinImportance>(
    "cti.map.minImportance",
    "medium_up",
  );
  // PMESII 直交フィルタ (地政学レイヤーの領域絞り込み、色=動機 と直交)。
  const [pmesii, setPmesii] = usePersistedState<PmesiiAxis>("cti.map.pmesii", "all");
  // 塗りの色分け次元 (既定=動機で両レイヤー統一)。
  const [colorBy, setColorBy] = usePersistedState<ColorBy>("cti.map.colorBy", "intent");
  // 時間軸トグル: 報道時刻(collection が今出している) / 発生時刻(実際に起きた、dated のみ)。
  const [timeBasis, setTimeBasis] = usePersistedState<TimeBasis>("cti.map.timeBasis", "report");
  return {
    layer, setLayer,
    selectedSector, setSelectedSector,
    selectedIntent, setSelectedIntent,
    threatClass, setThreatClass,
    sourceStatus, setSourceStatus,
    minImportance, setMinImportance,
    pmesii, setPmesii,
    colorBy, setColorBy,
    timeBasis, setTimeBasis,
  };
}
