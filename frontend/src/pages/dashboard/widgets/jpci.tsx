// 重要インフラ脅威 widget: ダッシュボードの本務は「どの分野がどの程度危険で、
// 前期からどう動いたか (推移)」— 変化 (backend の同長前窓比 changes) と分野格子が主役。
// **認識論的地位を視覚階層に写像する**: JP 観測 (確定) = 前景・彩色 / 世界の国家アクター
// 行動 (日本への含意は示唆どまり・未確定) = 後景・中立色・輪郭ドット。示唆情報に警報級の
// 視覚重量を与えない (因果非主張の doctrine と同根)。例外: campaign の「日本標的」判定は
// JP 関連の確定情報なので警告色を維持。事前配置 posture (国家別常設評価) は別の問いへの
// 答えなので widget には載せない (board 頭部のカードに常設)。
// チップ列数は widget 幅に auto-fit で追従。フル board は /app/jpci。
import { useQuery } from "@tanstack/react-query";
import { Globe } from "lucide-react";
import { jpciApi, type StageId } from "../../../api/jpci";
import { useWidgetWindow } from "../overviewWindow";
import { WidgetCard, Loading, WidgetError, type WidgetProps } from "../shared";

// 段階ラダー色 (board ページと同一 = 画面間で色が entity に追従)。順序尺度なので
// 凡例 + 注意度ソートが二次符号化 (CVD 分離は検証済み: 最悪ペア ΔE16.5 deutan)。
const STAGE_COLOR: Record<StageId, string> = {
  quiet: "#6b7280",
  indication: "#60a5fa",
  targeting: "#fbbf24",
  intrusion_it: "#fb923c",
  ot_approach: "#f87171",
};
const STAGE_RANK: Record<StageId, number> = {
  quiet: 0,
  indication: 1,
  targeting: 2,
  intrusion_it: 3,
  ot_approach: 4,
};
// 凡例はラダー昇順 (静穏 → 制御系接近) で読ませる。
const STAGE_ORDER: StageId[] = ["quiet", "indication", "targeting", "intrusion_it", "ot_approach"];
const MAX_MOVERS_INLINE = 4;

export function JpCiThreatWidget({ config }: WidgetProps) {
  // 対象期間は概観の共有窓に連動が既定、config.days 数値で固定 (共通フック)。
  // 国家アクター行動は長窓ほど見えるため 90日/全期間の固定choiceを持つ。
  const days = useWidgetWindow(config);
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jpci-widget", days],
    queryFn: () => jpciApi.board(days),
    staleTime: 60_000,
  });

  if (isLoading)
    return (
      <WidgetCard title="重要インフラ脅威">
        <Loading />
      </WidgetCard>
    );
  if (isError || !data)
    return (
      <WidgetCard title="重要インフラ脅威">
        <WidgetError />
      </WidgetCard>
    );

  const total = data.sectors.length;
  const worldActive = data.sectors.filter((s) => s.world_active).length;
  const jpBreached = data.sectors.filter(
    (s) => s.stage === "intrusion_it" || s.stage === "ot_approach",
  ).length;
  const systemicHits = data.sectors.filter((s) => s.systemic_hit).length;

  // 前期比 (同長前窓との段階昇降、backend 計算)。悪化 (up) を先に。
  const movers = [...data.changes].sort(
    (a, b) => Number(b.direction === "up") - Number(a.direction === "up"),
  );
  const changeBySector = new Map(data.changes.map((c) => [c.nisc_sector, c]));

  // 格子は注意度順 (JP 段階 desc → 世界行動あり) — 一瞥で「熱い分野」が上に来る。
  const sorted = [...data.sectors].sort(
    (a, b) =>
      STAGE_RANK[b.stage] - STAGE_RANK[a.stage] ||
      Number(b.world_active) - Number(a.world_active),
  );

  return (
    <WidgetCard title="重要インフラ脅威" href="/app/jpci">
      {/* ── 主役1: 前期比の推移 (同長前窓との段階昇降) ── */}
      <div className="mb-1.5 flex items-center gap-x-2 gap-y-0.5 flex-wrap text-[11px]">
        <span className="text-fg-subtle shrink-0">前期比:</span>
        {movers.length === 0 ? (
          <span className="text-fg-muted">段階変化なし</span>
        ) : (
          <>
            {movers.slice(0, MAX_MOVERS_INLINE).map((c) => (
              <span
                key={c.nisc_sector}
                className={`font-semibold ${c.direction === "up" ? "text-critical" : "text-accent"}`}
                title={`${c.label}: ${data.stage_labels[c.from_stage]} → ${data.stage_labels[c.to_stage]}${c.driver ? ` · ${c.driver}` : ""}`}
              >
                {c.direction === "up" ? "▲" : "▼"}
                {c.label}
              </span>
            ))}
            {movers.length > MAX_MOVERS_INLINE && (
              <span className="text-fg-subtle">+{movers.length - MAX_MOVERS_INLINE}</span>
            )}
          </>
        )}
      </div>

      {/* ── 主役2: 分野格子 (左帯 = JP 観測段階 / ▲▼ = 前期比 / ドット = 世界行動) ── */}
      <div
        className="grid gap-1.5"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(104px, 1fr))" }}
      >
        {sorted.map((s) => {
          const beh = s.threat_behavior[0];
          const chg = changeBySector.get(s.nisc_sector);
          return (
            <div
              key={s.nisc_sector}
              className="relative rounded py-1.5 pl-2 pr-3.5 text-[11px] leading-tight truncate text-fg"
              style={{
                backgroundColor: `${STAGE_COLOR[s.stage]}1a`,
                borderLeft: `3px solid ${STAGE_COLOR[s.stage]}`,
              }}
              title={`${s.label}: JP観測 ${data.stage_labels[s.stage]}${
                chg
                  ? ` (前期 ${data.stage_labels[chg.from_stage]} から${chg.direction === "up" ? "悪化" : "改善"})`
                  : " (前期から変化なし)"
              }${s.world_active ? ` / 世界: ${beh?.actor} (${data.intent_labels[beh.intent]})` : ""}`}
            >
              {chg && (
                <span
                  className={`font-bold ${chg.direction === "up" ? "text-critical" : "text-accent"}`}
                >
                  {chg.direction === "up" ? "▲" : "▼"}
                </span>
              )}
              {s.label}
              {s.world_active && beh && (
                <span className="absolute top-1.5 right-1.5 h-2 w-2 rounded-full border-[1.5px] border-fg-subtle" />
              )}
            </div>
          );
        })}
      </div>

      {/* 一行サマリ (分母と窓を明示) */}
      <div className="mt-1.5 flex items-center gap-x-3 gap-y-0.5 flex-wrap text-[10px] text-fg-subtle">
        <span>
          JP侵害段階 <b className="text-fg">{jpBreached}</b>/{total}
        </span>
        <span title="世界で観測された国家アクターの分野行動。日本への含意は示唆であり確定情報ではない">
          世界行動 <b className="text-fg-muted">{worldActive}</b>/{total}
        </span>
        {systemicHits > 0 && (
          <span className="text-critical font-semibold">基幹ノード {systemicHits}</span>
        )}
        <span className="text-fg-subtle/70">({days}日 vs 前{days}日)</span>
      </div>

      {/* ── 文脈: 横断キャンペーン (協調作戦の兆候) ── */}
      {data.campaigns.slice(0, 2).map((c) => (
        <div
          key={c.actor}
          className="mt-1 text-[11px] flex items-center gap-1.5 flex-nowrap overflow-hidden text-fg-muted"
          title={`${c.actor} が世界の ${c.sector_ids.length} 分野で${data.intent_labels[c.intent]} = 協調作戦の兆候 (日本への含意は示唆)`}
        >
          <Globe className="h-3 w-3 text-fg-subtle shrink-0" />
          <span className="font-semibold shrink-0">{c.actor}</span>
          <span className="shrink-0">{data.intent_labels[c.intent]}</span>
          <span className="text-fg-subtle truncate">
            世界{c.sector_ids.length}分野
            {c.jp_targeted && <span className="text-critical font-semibold"> · 日本標的</span>}
          </span>
        </div>
      ))}

      {/* 段階ミニ凡例 (左帯色の読み方) */}
      <div className="mt-1.5 flex items-center gap-x-2 gap-y-0.5 flex-wrap text-[9px] text-fg-subtle">
        {STAGE_ORDER.map((st) => (
          <span key={st} className="flex items-center gap-0.5">
            <span
              className="inline-block h-2.5 w-[3px] rounded-sm"
              style={{ backgroundColor: STAGE_COLOR[st] }}
            />
            {data.stage_labels[st]}
          </span>
        ))}
        <span className="flex items-center gap-0.5">
          <span className="inline-block h-2 w-2 rounded-full border-[1.5px] border-fg-subtle" />
          =世界行動 (示唆)
        </span>
      </div>
    </WidgetCard>
  );
}
