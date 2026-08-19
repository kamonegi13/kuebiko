// 脅威マップ (Leaflet・オフライン)。**外部タイル不要** — バンドルした世界 GeoJSON を
// L.geoJSON で基図にし、ネットワーク非依存 (エアギャップ)。Leaflet の旨味 (なめらかな
// pan/zoom・マーカークラスタリング=点の重なり解消・popup) を egress ゼロで得る。
// データ/絞り込み (window/layer/sector/flow/actor) は MapPage から props で受け、
// 動的レイヤーを props 変化時に作り直す。Leaflet は lat/lon を Mercator 投影する。

import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { CyberMapResponse, GeoEventNode, GeoNode, SubCountryPoint } from "../../api/geo";
import { intentHex, intentLabel } from "../../utils/diamond";
import { sectorColor } from "./sectorColors";
import { CITY_LABELS } from "./cityLabels";
import { vocabLabel } from "../../hooks/useVocab";
import countriesUrl from "./ne_countries.geojson?url";

// 地図の表示制御型 (旧 ThreatMap.tsx から移設、本コンポーネントが正の定義元)。
// focus = アクター中心ビュー: そのアクターの被害国 iso 集合を強調 (弧は廃止)。
export interface MapFocus {
  countries: Set<string>;
}

export type MapLayer = "cyber" | "geopolitical" | "both";
// マーカ塗りの次元。"intent"=両レイヤー動機で統一(一貫) / "sector"=cyber はセクター(地政学は動機のまま)。
export type ColorBy = "intent" | "sector";

// バブル半径は **窓内正規化**。絶対 √scale だと 24h は件数が小さく全部 MIN_R に潰れて
// 視認不能、90d は clamp で頭打ち、と非対称になっていた。窓内最大を MAX_R に張り付け、
// 小さい国も MIN_R 以上で必ず視認可能にする (地政学ダイヤの maxGeo 正規化と整合)。
// 絶対件数は tooltip / ランキングが持つので、地図は「位置 + 窓内の相対量」を表す。
const MIN_R = 5;
const MAX_R = 20;
// 比率パイの「上位3外 + 未判定」を束ねる中立スライス色 (構成を誤魔化さず残余として明示)。
const PIE_REST = "#586273";

interface LeafletThreatMapProps {
  data: CyberMapResponse;
  selectedSector: string | null;
  selectedIntent?: string | null;
  layer?: MapLayer;
  colorBy?: ColorBy;
  focus?: MapFocus | null;
  highlightIso?: string | null;
  onCountryClick?: (iso: string, domain: "cyber" | "geopolitical") => void;
  // 地域プリセット: [[south,west],[north,east]]。変化で flyToBounds。null=操作なし。
  region?: [[number, number], [number, number]] | null;
  persistViewKey?: string; // 指定 key で zoom/center を localStorage 保存・復元 (full/mini で別キー)。
  // 都市・地域ピン (Phase 1b 後半): 呼出側が region/toggle でフィルタ済みの一覧を渡す
  // (国バブルのレスポンスとは独立、渡さなければ非表示)。
  subCountryPoints?: SubCountryPoint[];
}

function radiusFor(count: number, maxCount: number): number {
  if (count <= 0) return 0;
  // √スケールで面積を perceptual に。窓内最大=MAX_R、それ以下を MIN_R..MAX_R に配分。
  const t = Math.sqrt(count) / Math.sqrt(Math.max(1, maxCount));
  return MIN_R + (MAX_R - MIN_R) * t;
}

function effCount(sectors: { sector: string; count: number }[], total: number, sel: string | null): number {
  if (!sel) return total;
  return sectors.find((s) => s.sector === sel)?.count ?? 0;
}

// {color,value} 群を conic-gradient (比率パイ) 文字列に。単一スライスは実質ソリッド。
// total<=0 は中立色。正確な内訳は tooltip / ドリルが持つ → ここは「構成の一目把握」専用。
function conicGradient(slices: { color: string; value: number }[]): string {
  const total = slices.reduce((s, x) => s + x.value, 0);
  if (total <= 0) return PIE_REST;
  let acc = 0;
  const stops = slices.map((s) => {
    const start = (acc / total) * 100;
    acc += s.value;
    return `${s.color} ${start}% ${(acc / total) * 100}%`;
  });
  return `conic-gradient(${stops.join(", ")})`;
}

// 被害国バブルのセクター比 (上位3 + その他)。未選択時の cyber 塗りに使う。
function cyberSlices(n: GeoNode): { color: string; value: number }[] {
  const top = n.sectors.slice(0, 3).map((s) => ({ color: sectorColor(s.sector), value: s.count }));
  const rest = n.count - top.reduce((s, x) => s + x.value, 0);
  if (rest > 0) top.push({ color: PIE_REST, value: rest });
  return top;
}

// 地政学ダイヤの動機比 (上位3 + 残り動機/未判定)。
function geoSlices(g: GeoEventNode): { color: string; value: number }[] {
  const top = g.intents.slice(0, 3).map((i) => ({ color: intentHex(i.intent), value: i.count }));
  const rest = g.count - top.reduce((s, x) => s + x.value, 0);
  if (rest > 0) top.push({ color: PIE_REST, value: rest });
  return top;
}

// cyber バブルの動機比 (色分け「動機」モード用、上位3 + 残り/未判定)。geoSlices の cyber 版。
function nodeIntentSlices(n: GeoNode): { color: string; value: number }[] {
  const top = n.intents.slice(0, 3).map((i) => ({ color: intentHex(i.intent), value: i.count }));
  const rest = n.count - top.reduce((s, x) => s + x.value, 0);
  if (rest > 0) top.push({ color: PIE_REST, value: rest });
  return top;
}

// shiftRight>0 で右・<0 で左に画素ずらし (iconAnchor、zoom 非依存)。background は solid color
// か conic-gradient(比率パイ)。opacity は要素全体に掛けて減光。輪郭は細い白で構成を読みやすく。
function diamondIcon(
  background: string,
  size: number,
  opacity: number,
  ring = false,
  shiftRight = 0,
): L.DivIcon {
  const border = ring ? "2px solid #e8ebf0" : "1px solid rgba(255,255,255,0.32)";
  return L.divIcon({
    className: "",
    html: `<div style="width:${size}px;height:${size}px;background:${background};opacity:${opacity};transform:rotate(45deg);border:${border}"></div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2 - shiftRight, size / 2],
  });
}

// 比率パイ円マーカ (background=conic or solid)。circleMarker(canvas) と違い画素オフセット可。
function pieCircleIcon(
  background: string,
  diameter: number,
  opacity: number,
  ring: boolean,
  shiftRight: number,
): L.DivIcon {
  const d = Math.max(2, Math.round(diameter));
  const border = ring ? "2px solid #e8ebf0" : "1px solid rgba(255,255,255,0.3)";
  return L.divIcon({
    className: "",
    html: `<div style="width:${d}px;height:${d}px;border-radius:50%;background:${background};opacity:${opacity};border:${border}"></div>`,
    iconSize: [d, d],
    iconAnchor: [d / 2 - shiftRight, d / 2],
  });
}

// 都市・地域ピン (地図 Phase 1b 後半): victim_city を CITY/ADMIN1 tier で解決した精密点。
// 精度を honest に見分けさせる — CITY=塗りつぶし小円 / ADMIN1=中抜きリング (代表点=州内
// 最大人口都市、都市より粗い)。どちらも国バブルの最小半径 (MIN_R=5) より明確に小さくする。
const SUB_COUNTRY_COLOR = "#5eead4"; // teal-300。国バブル(セクター/動機パイ)・地政学ダイヤと
// 衝突しない中立アクセントで「別レイヤーの精密点」と一目で分かる配色にする。
const SUB_COUNTRY_MIN_R = 2.5;
const SUB_COUNTRY_MAX_R = 4.5;

function subCountryRadius(count: number, maxCount: number): number {
  if (count <= 0) return 0;
  const t = Math.sqrt(count) / Math.sqrt(Math.max(1, maxCount));
  return SUB_COUNTRY_MIN_R + (SUB_COUNTRY_MAX_R - SUB_COUNTRY_MIN_R) * t;
}

// CITY=塗りつぶし円 (filled) / ADMIN1=中抜きリング (transparent fill + 着色枠)。
function subCountryIcon(diameter: number, filled: boolean): L.DivIcon {
  const d = Math.max(2, Math.round(diameter));
  const style = filled
    ? `background:${SUB_COUNTRY_COLOR};border:1px solid rgba(255,255,255,0.55)`
    : `background:transparent;border:1.5px solid ${SUB_COUNTRY_COLOR}`;
  return L.divIcon({
    className: "",
    html: `<div style="width:${d}px;height:${d}px;border-radius:50%;${style}"></div>`,
    iconSize: [d, d],
    iconAnchor: [d / 2, d / 2],
  });
}

// ◯と◇のペアを国重心を中点に左右対称配置する片側ずらし量 (px)。両マーカが同じ値で
// 逆向きに動くので中点=重心。45°回転◇の水平半幅 ≈0.7·s。広がり過ぎ防止に上限 18px。
function pairShift(radius: number, diamondSize: number): number {
  return Math.min(18, (radius + 0.7 * diamondSize + 4) / 2);
}

function labelIcon(name: string): L.DivIcon {
  const safe = name.replace(/[<>&]/g, "");
  return L.divIcon({
    className: "geo-city-label",
    html: `<span>${safe}</span>`,
    iconSize: [0, 0],
  });
}

// ズーム別の都市ラベル表示閾値 (scalerank。低いほど大都市)。ズームするほど多く出す。
function labelRankThreshold(zoom: number): number {
  if (zoom <= 3) return 0;
  if (zoom <= 4) return 1;
  if (zoom <= 5) return 2;
  if (zoom <= 6) return 3;
  return 4;
}

// 地図 view (zoom/center) の永続化 (画面更新で復元)。key 指定で full/mini を別保存。
function readSavedView(key: string): { center: [number, number]; zoom: number } | null {
  try {
    const raw = typeof localStorage !== "undefined" ? localStorage.getItem(key) : null;
    if (!raw) return null;
    const v = JSON.parse(raw) as { lat?: number; lng?: number; zoom?: number };
    if (typeof v.lat === "number" && typeof v.lng === "number" && typeof v.zoom === "number") {
      return { center: [v.lat, v.lng], zoom: v.zoom };
    }
  } catch {
    /* localStorage 不可 / 壊れた値は無視 */
  }
  return null;
}
function saveMapView(key: string, center: L.LatLng, zoom: number): void {
  try {
    localStorage.setItem(key, JSON.stringify({ lat: center.lat, lng: center.lng, zoom }));
  } catch {
    /* ignore */
  }
}

export function LeafletThreatMap({
  data,
  selectedSector,
  selectedIntent = null,
  layer = "cyber",
  colorBy = "intent",
  focus = null,
  highlightIso = null,
  onCountryClick,
  region = null,
  persistViewKey,
  subCountryPoints = [],
}: LeafletThreatMapProps) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const dynRef = useRef<L.LayerGroup | null>(null);
  const clickRef = useRef(onCountryClick);
  clickRef.current = onCountryClick;

  // mount: 地図 + 高精細基図 (Natural Earth 50m、同一オリジン asset を fetch) + 都市ラベル。
  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    const saved = persistViewKey ? readSavedView(persistViewKey) : null;
    const map = L.map(divRef.current, {
      center: saved?.center ?? [28, 12],
      zoom: saved?.zoom ?? 2,
      minZoom: 1, // 世界全体まで縮小可 (旧 2 では小コンテナで世界が出せなかった)
      maxZoom: 9,
      worldCopyJump: true,
      attributionControl: false,
      preferCanvas: true,
      zoomControl: true,
    });
    mapRef.current = map;
    // zoom/pan を localStorage に保存し、画面更新で復元 (full / mini それぞれ別キー)。
    if (persistViewKey) {
      map.on("moveend zoomend", () => saveMapView(persistViewKey, map.getCenter(), map.getZoom()));
    }
    dynRef.current = L.layerGroup().addTo(map);

    // 基図 (国境線つき detailed countries)。外部でなく自サーバの static asset を fetch。
    fetch(countriesUrl)
      .then((r) => r.json())
      .then((geo) => {
        if (!mapRef.current) return;
        const layer = L.geoJSON(geo, {
          style: {
            fillColor: "#1a2030",
            fillOpacity: 1,
            color: "rgba(255,255,255,0.13)",
            weight: 0.5,
          },
          interactive: false,
        }).addTo(map);
        layer.bringToBack();
      })
      .catch(() => {});

    // 都市ラベル: ズーム/表示域でゲート (大都市から、ズームで増やす、画面内のみ)。
    const labelLayer = L.layerGroup().addTo(map);
    const updateLabels = () => {
      labelLayer.clearLayers();
      const thr = labelRankThreshold(map.getZoom());
      const b = map.getBounds();
      let shown = 0;
      for (const c of CITY_LABELS) {
        if (c.rank > thr || shown > 120) continue;
        if (!b.contains([c.lat, c.lon])) continue;
        L.marker([c.lat, c.lon], { icon: labelIcon(c.name), interactive: false }).addTo(labelLayer);
        shown += 1;
      }
    };
    map.on("zoomend moveend", updateLabels);
    updateLabels();

    // flex レイアウト内で初期サイズが 0 になる事故を防ぐ (リサイズ追従)。
    const ro = new ResizeObserver(() => map.invalidateSize());
    ro.observe(map.getContainer());
    setTimeout(() => map.invalidateSize(), 60);

    return () => {
      ro.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // props 変化: 動的レイヤー (国バブル / 地政学ダイヤ) を作り直す。
  useEffect(() => {
    const map = mapRef.current;
    const dyn = dynRef.current;
    if (!map || !dyn) return;
    dyn.clearLayers();
    const showCyber = layer !== "geopolitical";
    const showGeo = layer !== "cyber";

    // cyber バブルの半径 / 地政学ダイヤのサイズを先に算出 (両方を持つ国を both で左右対称に
    // 配置するため、片側ずらし量 dx を ◯/◇ 双方が同じ値で算出できるようにする)。
    const both = layer === "both";
    // ファセット (件数を 1 カテゴリに絞る) は **その色分けモードのみ** 有効:
    // セクターモード→sectorFacet / 動機モード→intentFacet。色分け次元と一致させる。
    const sectorFacet = colorBy === "sector" ? selectedSector : null;
    const intentFacet = colorBy === "intent" ? selectedIntent : null;
    const intentCount = (arr: { intent: string; count: number }[], v: string): number =>
      arr.find((i) => i.intent === v)?.count ?? 0;
    // cyber バブルの実効件数: 動機モード=動機ファセット(or 全件) / セクターモード=セクターファセット。
    const cyberEff = (n: GeoNode): number =>
      colorBy === "intent"
        ? intentFacet
          ? intentCount(n.intents, intentFacet)
          : n.count
        : effCount(n.sectors, n.count, sectorFacet);
    // geo ダイヤの実効件数: 動機ファセット(動機モードのみ) or 全件。
    const geoEff = (g: GeoEventNode): number =>
      intentFacet ? intentCount(g.intents, intentFacet) : g.count;

    let maxEff = 0;
    for (const n of data.nodes) {
      const e = cyberEff(n);
      if (e > maxEff) maxEff = e;
    }
    const cyberRadiusByIso = new Map<string, number>();
    if (showCyber) {
      for (const n of data.nodes) {
        const eff = cyberEff(n);
        if (eff > 0) cyberRadiusByIso.set(n.iso, radiusFor(eff, maxEff));
      }
    }
    let maxGeo = 1;
    for (const g of data.geo_events) {
      const e = geoEff(g);
      if (e > maxGeo) maxGeo = e;
    }
    const geoSizeByIso = new Map<string, number>();
    if (showGeo) {
      for (const g of data.geo_events) {
        const eff = geoEff(g);
        if (eff > 0) geoSizeByIso.set(g.iso, 8 + Math.sqrt(eff / maxGeo) * 16);
      }
    }

    // 地政学イベント (動機比パイ ◆)。**動機の構成比を conic-gradient で塗る** = 単一支配色でなく
    // 混在 (上位3動機 + 残り/未判定灰) を一目で示す。click で国ドリル (domain=geopolitical)。
    // both で同一国に◯もある場合は ◇ を国重心から **右** に dx ずらす (◯は左に dx)。
    if (showGeo) {
      for (const g of data.geo_events) {
        const s = geoSizeByIso.get(g.iso);
        if (s == null) continue; // 動機ファセットで該当が無い国は非表示 (geoEff<=0)
        const dimmed = focus != null && !focus.countries.has(g.iso);
        const r = cyberRadiusByIso.get(g.iso);
        const dx = both && r != null ? pairShift(r, s) : 0;
        const mix = g.intents
          .slice(0, 3)
          .map((i) => `${intentLabel(i.intent)} ${Math.round((i.count / g.count) * 100)}%`)
          .join(" / ");
        const motiveLine = g.top_intent
          ? `<br><span style="opacity:.75">動機: ${mix} (全${g.count}件中${g.intent_count}件で判定)</span>`
          : '<br><span style="opacity:.5">動機: 未判定</span>';
        // 動機ファセット時はその動機のソリッド、無選択時は動機比パイ。
        const bg = intentFacet ? intentHex(intentFacet) : conicGradient(geoSlices(g));
        L.marker([g.lat, g.lon], {
          icon: diamondIcon(bg, s, dimmed ? 0.2 : 0.9, false, dx),
          interactive: true,
        })
          .bindTooltip(
            `<b>${g.label}</b> (${g.iso}) · 地政学イベント: ${g.count} 件${motiveLine}`,
            { sticky: true },
          )
          .on("click", () => clickRef.current?.(g.iso, "geopolitical"))
          .addTo(dyn);
      }
    }

    // 国別 被害バブル (cyber 層 ◯)。**セクター未選択時はセクター比パイ** で混在を示す
    // (単一の top_sector 色は誤解を招くため。正確値は tooltip/右パネル)。セクター選択時はその
    // セクターのソリッド色 + effCount で「その軸の地理分布」を見るファセットに切り替わる。
    // both で同一国に◇もある場合は ◯ を国重心から **左** に dx ずらす (◇は右)。
    if (showCyber) {
      for (const n of data.nodes) {
        const eff = cyberEff(n);
        if (eff <= 0) continue;
        const dimmed = focus != null && !focus.countries.has(n.iso);
        const ring = highlightIso === n.iso;
        const radius = radiusFor(eff, maxEff);
        const gs = geoSizeByIso.get(n.iso);
        const dx = both && gs != null ? -pairShift(radius, gs) : 0;
        // 塗り: 「動機」モード=動機ファセット時そのソリッド・無選択時は動機比パイ /
        // 「セクター」モード=セクター選択時そのソリッド・無選択時はセクター比パイ。
        const bg =
          colorBy === "intent"
            ? intentFacet
              ? intentHex(intentFacet)
              : conicGradient(nodeIntentSlices(n))
            : sectorFacet
              ? sectorColor(sectorFacet)
              : conicGradient(cyberSlices(n));
        const m = L.marker([n.lat, n.lon], {
          icon: pieCircleIcon(bg, radius * 2, dimmed ? 0.16 : 0.88, ring, dx),
          interactive: true,
        });
        const top = n.sectors.slice(0, 4).map((s) => `${s.label} ${s.count}`).join(" / ");
        // 動機モードは動機内訳も tooltip に (色が何かを説明)。
        const motiveLine =
          colorBy === "intent" && n.top_intent
            ? `<br><span style="opacity:.75">動機: ${n.intents
                .slice(0, 3)
                .map((i) => `${intentLabel(i.intent)} ${Math.round((i.count / n.count) * 100)}%`)
                .join(" / ")} (全${n.count}件中${n.intent_count}件で判定)</span>`
            : "";
        const shareP = Math.round(n.top_source_share * 100);
        const srcName = (n.top_source || "").replace(/[<>&]/g, "");
        const srcLine = `${n.source_count}ソース / 最大${shareP}%${srcName ? `（主に ${srcName}）` : ""}`;
        const ledger =
          n.collected_count > 0
            ? `<br><span style="opacity:.55">台帳 ${n.collected_count} / 報道 ${n.posted_count}</span>`
            : "";
        m.bindTooltip(
          `<b>${n.label}</b> (${n.iso})<br>被害報道: ${eff} 件` +
            `${top ? `<br><span style="opacity:.7">${top}</span>` : ""}${motiveLine}` +
            `<br><span style="opacity:.55">${srcLine}</span>${ledger}`,
          { sticky: true },
        );
        m.on("click", () => clickRef.current?.(n.iso, "cyber"));
        dyn.addLayer(m);
      }
    }

    // 都市・地域ピン (Phase 1b 後半): victim_city を CITY/ADMIN1 tier で解決した精密点。
    // cyber レイヤー非表示中 (地政学のみ) は victim_city と無関係なので出さない。
    if (showCyber && subCountryPoints.length > 0) {
      let maxPin = 1;
      for (const p of subCountryPoints) if (p.count > maxPin) maxPin = p.count;
      for (const p of subCountryPoints) {
        const r = subCountryRadius(p.count, maxPin);
        const filled = p.precision === "city";
        const precisionLabel = vocabLabel("geo_precision", p.precision);
        const titleLines = p.titles
          .map((t) => `<span style="opacity:.8">・${t.replace(/[<>&]/g, "")}</span>`)
          .join("<br>");
        L.marker([p.lat, p.lon], {
          icon: subCountryIcon(r * 2, filled),
          interactive: true,
          zIndexOffset: 500, // 国バブルの上に重ねて隠れないようにする
        })
          .bindTooltip(
            `<b>${precisionLabel}</b>・${p.count} 件` + (titleLines ? `<br>${titleLines}` : ""),
            { sticky: true },
          )
          .addTo(dyn);
      }
    }
  }, [data, selectedSector, selectedIntent, layer, colorBy, focus, highlightIso, subCountryPoints]);

  // 地域プリセット: region 変化で flyToBounds。
  useEffect(() => {
    const map = mapRef.current;
    if (map && region) {
      map.flyToBounds(region, { padding: [24, 24], maxZoom: 7, duration: 0.6 });
    }
  }, [region]);

  return <div ref={divRef} className="h-full w-full" style={{ background: "#0a0e16" }} />;
}
