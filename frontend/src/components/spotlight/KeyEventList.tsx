// Spotlight の主要事象 — 直近 24 時間を先に、それ以前 (7 日窓の残り) を後に (段C)。
import type { KeyEvent } from "../../api/spotlight";
import { ConfidenceBadge } from "../ConfidenceBadge";
import { RECENT_HOURS, splitByRecency } from "./keyEvents";

const LABEL = "text-[12px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5";

function importanceTone(importance: string): string {
  if (importance === "high") return "bg-critical-soft text-critical";
  if (importance === "medium") return "bg-warning-soft text-warning";
  return "bg-surface-3 text-fg-subtle";
}

function importanceLabel(importance: string): string {
  if (importance === "high") return "高";
  return importance === "medium" ? "中" : "低";
}

function Rows({ events }: { events: KeyEvent[] }) {
  return (
    <ul className="m-0 p-0 list-none space-y-1">
      {events.map((ke) => (
        <li key={ke.article_id} className="flex items-baseline gap-2 text-sm">
          <span className={`text-[12px] px-1.5 py-0.5 rounded font-mono ${importanceTone(ke.importance)}`}>
            {importanceLabel(ke.importance)}
          </span>
          <a
            href={ke.url}
            target="_blank"
            rel="noreferrer"
            className="text-fg hover:text-accent-hover flex-1 min-w-0 truncate"
          >
            {ke.title}
          </a>
          {/* S1: 出典基盤の確度バッジ (source メタから決定的に算出、reason は hover) */}
          <ConfidenceBadge sb={ke.source_basis} />
          <span className="text-[12px] text-fg-subtle">{ke.feed_title}</span>
        </li>
      ))}
    </ul>
  );
}

export function KeyEventList({ events }: { events: KeyEvent[] }) {
  if (events.length === 0) return null;
  const { recent, earlier } = splitByRecency(events, Date.now());
  return (
    <div className="mb-3 space-y-2">
      <div>
        <div className={LABEL}>
          直近 {RECENT_HOURS} 時間の主要事象 ({recent.length})
        </div>
        {recent.length > 0 ? (
          <Rows events={recent} />
        ) : (
          <p className="m-0 text-[13px] text-fg-subtle">新しい事象はない</p>
        )}
      </div>
      {earlier.length > 0 && (
        <div>
          <div className={LABEL}>それ以前 (7 日窓) ({earlier.length})</div>
          <Rows events={earlier} />
        </div>
      )}
    </div>
  );
}
