// 公開版の地図 (被害国のみ)。
//
// ⚠ 分析画面の脅威マップ (LeafletThreatMap) を流用しない。あちらはアクター帰属と
// 意図 (Diamond Model) の層を重ねており、**報道された事実ではなく kuebiko の分析
// 判断**を含む。公開面では根拠の連鎖を示せないので、被害国の 1 層だけを描く。
//
// ⚠ **地図は収集網の観測であって世界ではない**。国を特定できなかった件数を必ず
// 併記する (実測では公開記事の 56% に国が付いていない)。割合を隠すと「これが世界の
// 実態」と読まれる。
//
// 基図は同一オリジンの Natural Earth geojson を fetch する (外部タイル非依存)。
// 1.4MB あるので **地図ページを開いたときにだけ**取りに行く。

import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import countriesUrl from "../components/geo/ne_countries.geojson?url";
import type { PublicMapNode } from "../api/publicNews";

// 世界ズームでは 70 か国以上が同時に出る。最小半径を絞らないと欧州が塊になる。
const MIN_R = 3;
const MAX_R = 18;

function radiusFor(count: number, maxCount: number): number {
  if (count <= 0) return 0;
  // √スケールで面積を perceptual に (件数の比が面積の比に見えるように)
  const t = Math.sqrt(count) / Math.sqrt(Math.max(1, maxCount));
  return MIN_R + (MAX_R - MIN_R) * t;
}

export function PublicMap({
  nodes,
  onCountryClick,
}: {
  nodes: PublicMapNode[];
  onCountryClick: (iso: string) => void;
}) {
  const divRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);
  const clickRef = useRef(onCountryClick);
  clickRef.current = onCountryClick;

  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    const map = L.map(divRef.current, {
      center: [28, 12],
      zoom: 1,
      minZoom: 1,
      maxZoom: 6,
      worldCopyJump: true,
      attributionControl: false,
      preferCanvas: true,
      zoomControl: true,
      scrollWheelZoom: false, // 記事を読みながらの誤操作を防ぐ (ピンチ/ボタンは有効)
    });
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);

    fetch(countriesUrl)
      .then((r) => r.json())
      .then((geo) => {
        if (!mapRef.current) return;
        L.geoJSON(geo, {
          style: {
            fillColor: "#1a2030",
            fillOpacity: 1,
            color: "rgba(255,255,255,0.13)",
            weight: 0.5,
          },
          interactive: false,
        })
          .addTo(map)
          .bringToBack();
      })
      .catch(() => {
        /* 基図が出なくてもバブルは描ける (機能を落として止めない) */
      });

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const group = layerRef.current;
    if (!group) return;
    group.clearLayers();
    const max = Math.max(1, ...nodes.map((n) => n.count));
    for (const n of nodes) {
      L.circleMarker([n.lat, n.lon], {
        radius: radiusFor(n.count, max),
        color: "rgba(255,255,255,0.35)",
        weight: 1,
        fillColor: "#e0603a",
        fillOpacity: 0.55,
      })
        .bindTooltip(`${n.label} — ${n.count} 件`, { direction: "top" })
        .on("click", () => clickRef.current(n.iso))
        .addTo(group);
    }
  }, [nodes]);

  // ⚠ 2 点、どちらも落とすと表示が壊れる:
  //  1) **背景色**を当てる。Leaflet のコンテナ既定は明るいグレーなので、暗色テーマだと
  //     海が真っ白になる (分析画面の地図も同じ指定を持っている)
  //  2) **スタッキング文脈を作る** (relative + z-0)。Leaflet の内部 pane は z-index 400+ を
  //     使うため、文脈を作らないと **sticky ヘッダの上にバブルが描かれる**
  return (
    <div
      ref={divRef}
      className="relative z-0 w-full h-[52vh] min-h-[280px] rounded-lg overflow-hidden"
      style={{ background: "#0a0e16" }}
    />
  );
}
