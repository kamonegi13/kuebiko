// Diamond Model (Caltagirone+ 2013) の actor 詳細向け可視化 (Phase Diamond-Axes)。
// ThreatConnect の canonical 図を「参考」にしつつ、本ツール (live / dark / interactive /
// 日本語) 向けに最適化した独自表現。
//
// 4 頂点 = Adversary(上) / Capabilities(左) / Infrastructure(右) / Victim(下)。
// 2 軸をひし形の対角線として内包:
//   - 縦 (Adversary↔Victim) = ① 意図軸 (socio-political)。対角線を intent 色で着色しデータ連動。
//   - 横 (Capabilities↔Infrastructure) = ② 技術軸 (technical / TTPs)。中立グレー。
// 装飾は控えめ (大きな塗り三角は廃し、小さな①②ドットを対角線上に)。
// 依存追加なしのインライン SVG。md+ 専用 (呼出側で制御)。

import type { ActorActivity } from "../api/types";
import { intentHex, intentLabel, nationMeta } from "../utils/diamond";
import { sectorLabel } from "./geo/sectorColors";

const TECH_GRAY = "#64748b"; // ② 技術軸 (theme 中立 slate)
const UNKNOWN_BLUE = "#3a4453"; // 意図未判定時の縦軸 (くすませる)

function scrollToSection(id: string): void {
  const el = document.getElementById(id);
  if (el instanceof HTMLDetailsElement) el.open = true;
  el?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function Chip({ children, mono }: { children: React.ReactNode; mono?: boolean }) {
  return (
    <span
      className={`inline-block max-w-full truncate bg-surface-3 border border-border-subtle text-fg px-1.5 py-0.5 rounded text-[10px] leading-tight ${mono ? "font-mono" : ""}`}
    >
      {children}
    </span>
  );
}

function VertexCard({
  posClass, kicker, accentHex, onClick, title, children,
}: {
  posClass: string;
  kicker: string;
  accentHex?: string;
  onClick?: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  const Comp = onClick ? "button" : "div";
  return (
    <Comp
      onClick={onClick}
      title={title}
      style={accentHex ? { borderColor: accentHex } : undefined}
      className={`absolute z-10 w-[176px] px-2.5 py-2 rounded-lg bg-surface-2 border text-left shadow-sm ${
        accentHex ? "" : "border-border-default"
      } ${onClick ? "cursor-pointer hover:bg-surface-3 hover:border-accent-soft transition-colors" : ""} ${posClass}`}
    >
      <div className="text-[9px] uppercase tracking-wider font-semibold mb-1" style={accentHex ? { color: accentHex } : undefined}>
        {kicker}
      </div>
      {/* チップ領域は 2 行に固定 (max-h + overflow) してカード高さの暴れを防ぐ。
          溢れた分は kicker の件数 + 頂点 click 先の詳細セクションで確認できる。 */}
      <div className="flex flex-wrap gap-1 max-h-[40px] overflow-hidden">{children}</div>
    </Comp>
  );
}

// 三角形に重ねる番号 badge (リファレンス風の小さな四角)。
function NumBadge({ n, hex, posClass }: { n: number; hex: string; posClass: string }) {
  return (
    <span
      className={`absolute z-20 w-[18px] h-[18px] rounded-[3px] flex items-center justify-center text-[10px] font-bold text-white ring-1 ring-surface-1/60 ${posClass}`}
      style={{ backgroundColor: hex }}
    >
      {n}
    </span>
  );
}

export function DiamondDiagram({ activity: a }: { activity: ActorActivity }) {
  const nation = nationMeta(a.nation);

  const capItems = [
    ...a.top_malware_families.map(([m]) => m),
    ...a.top_tools.map(([t]) => t),
  ].slice(0, 3);
  const capCount = a.top_malware_families.length + a.top_tools.length + a.top_ttps.length;

  const infraItems = [
    ...a.top_iocs_domain.map(([v]) => v),
    ...a.top_iocs_ip.map(([v]) => v),
    ...a.top_iocs_hash.map(([v]) => `${v.slice(0, 10)}…`),
  ].slice(0, 3);
  const iocCount =
    a.top_iocs_ip.length + a.top_iocs_domain.length + a.top_iocs_hash.length + a.top_iocs_url.length;

  const vicItems = [
    ...a.top_sectors.filter(([s]) => s !== "uncategorized").map(([s]) => sectorLabel(s)),
    ...a.top_countries.map(([c]) => c),
  ].slice(0, 3);

  // ① 意図軸 = intent (縦対角線の色)、② 技術軸 = TTPs
  const intent = a.top_intents[0]?.[0] ?? null;
  const intentHexVal = intent ? intentHex(intent) : UNKNOWN_BLUE;
  const ttps = a.top_ttps.slice(0, 4);

  return (
    <div>
      <div className="relative w-full flex justify-center py-24">
        <div className="relative w-[280px] h-[280px]">
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 w-full h-full">
            {/* ① 意図軸の三角 (上右内側、intent 色) = socio-political */}
            <polygon points="50,50 50,10 88,50" fill={intentHexVal} opacity={intent ? "0.5" : "0.16"} />
            {/* ② 技術軸の三角 (下左内側、中立グレー) = technical */}
            <polygon points="50,50 50,90 12,50" fill={TECH_GRAY} opacity="0.45" />
            {/* ひし形 outline */}
            <polygon points="50,4 96,50 50,96 4,50" fill="none" stroke="rgba(148,163,184,0.45)" strokeWidth="1.2" />
            {/* 横対角線 = ② 技術軸 (中立グレー) */}
            <line x1="4" y1="50" x2="96" y2="50" stroke={TECH_GRAY} strokeWidth="1" opacity="0.85" />
            {/* 縦対角線 = ① 意図軸 (intent 色で着色 → データ連動) */}
            <line x1="50" y1="4" x2="50" y2="96" stroke={intentHexVal} strokeWidth="1.3" opacity={intent ? "0.9" : "0.5"} />
          </svg>

          {/* 三角形に重ねる番号 badge (リファレンス風) */}
          <NumBadge n={1} hex={intentHexVal} posClass="left-[60%] top-[34%]" />
          <NumBadge n={2} hex={TECH_GRAY} posClass="left-[35%] top-[60%]" />

          {/* 上頂点: Adversary (nation 色) */}
          <VertexCard
            posClass="bottom-full mb-3 left-1/2 -translate-x-1/2"
            kicker={`◆ ADVERSARY${nation ? ` · ${nation.label}` : ""}`}
            accentHex={nation?.hex}
            title={nation ? `${nation.label} (CrowdStrike/Microsoft: ${nation.vendor})` : "Adversary (帰属不明)"}
          >
            <div className="w-full">
              <span className="text-sm font-bold text-fg leading-tight">{a.canonical}</span>
              {a.family && <span className="ml-1.5 text-[10px] text-fg-muted font-mono">系統: {a.family}</span>}
            </div>
          </VertexCard>

          {/* 左頂点: Capabilities */}
          <VertexCard
            posClass="right-full mr-3 top-1/2 -translate-y-1/2"
            kicker={`CAPABILITIES${capCount > capItems.length ? ` · +${capCount - capItems.length}` : ""}`}
            onClick={() => scrollToSection("dm-capability")}
            title="クリックで Capability セクションへ"
          >
            {capItems.length ? capItems.map((v, i) => <Chip key={i}>{v}</Chip>) : <Chip>—</Chip>}
          </VertexCard>

          {/* 右頂点: Infrastructure */}
          <VertexCard
            posClass="left-full ml-3 top-1/2 -translate-y-1/2"
            kicker={`INFRASTRUCTURE${iocCount ? ` · ${iocCount} IoC` : ""}`}
            onClick={() => scrollToSection("dm-infrastructure")}
            title="クリックで Infrastructure セクションへ"
          >
            {infraItems.length ? infraItems.map((v, i) => <Chip key={i} mono>{v}</Chip>) : <Chip>—</Chip>}
          </VertexCard>

          {/* 下頂点: Victim */}
          <VertexCard
            posClass="top-full mt-3 left-1/2 -translate-x-1/2"
            kicker={`VICTIM${a.japan_targeted_count ? ` · JP ${a.japan_targeted_count}` : ""}`}
            onClick={() => scrollToSection("dm-victim")}
            title="クリックで Victim セクションへ"
          >
            {vicItems.length ? vicItems.map((v, i) => <Chip key={i}>{v}</Chip>) : <Chip>—</Chip>}
          </VertexCard>
        </div>
      </div>

      {/* 軸の凡例 (日本語主体)。① 意図軸 = intent、② 技術軸 = TTPs。 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <button
          onClick={() => scrollToSection("dm-socio")}
          className="flex items-start gap-2 p-2.5 rounded-lg bg-surface-1 border border-border-subtle text-left hover:bg-surface-2 transition-colors"
        >
          <span className="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold text-white mt-0.5" style={{ backgroundColor: intentHexVal }}>1</span>
          <div className="min-w-0">
            <div className="text-xs font-semibold text-fg">意図軸 <span className="text-fg-subtle font-normal">socio-political</span></div>
            <div className="text-[10px] text-fg-subtle mb-1">攻撃者 → 被害者 の動機</div>
            {intent ? (
              <a
                href={`/app/news?intent=${encodeURIComponent(intent)}&search=${encodeURIComponent(a.canonical)}`}
                onClick={(e) => e.stopPropagation()}
                style={{ color: intentHexVal, borderColor: intentHexVal }}
                className="inline-block px-2 py-0.5 rounded-full bg-surface-2 border text-xs font-semibold hover:bg-surface-3"
                title={`News を「${intentLabel(intent)} × ${a.canonical}」で絞り込み`}
              >
                {intentLabel(intent)} →
              </a>
            ) : (
              <span className="text-fg-subtle text-xs italic">意図 未判定</span>
            )}
          </div>
        </button>

        <button
          onClick={() => scrollToSection("dm-capability")}
          className="flex items-start gap-2 p-2.5 rounded-lg bg-surface-1 border border-border-subtle text-left hover:bg-surface-2 transition-colors"
        >
          <span className="shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-[11px] font-bold text-white mt-0.5" style={{ backgroundColor: TECH_GRAY }}>2</span>
          <div className="min-w-0">
            <div className="text-xs font-semibold text-fg">技術軸 <span className="text-fg-subtle font-normal">technical · TTPs</span></div>
            <div className="text-[10px] text-fg-subtle mb-1">capability ⇄ infrastructure</div>
            <div className="flex flex-wrap gap-1">
              {ttps.length ? ttps.map((t) => <Chip key={t} mono>{t}</Chip>) : <span className="text-fg-subtle text-xs italic">TTP 未抽出</span>}
            </div>
          </div>
        </button>
      </div>
    </div>
  );
}
