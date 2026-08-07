// チャンネル表示の共通モジュール。チャンネルは「色 + 名前」で識別する
// (per-channel アイコンは色+名前で十分・custom で無意味に退化するため不採用、2026-06-13)。
// id しか持たない呼び出し側 (routing rules 等) は useChannelMeta でラベルを解決する。

import { useQuery } from "@tanstack/react-query";
import { channelsApi, type ChannelDef } from "../api/channels";

// built-in id の固定カラー (tailwind.config.ts のパレットと同期)。
const CHANNEL_COLOR: Record<string, string> = {
  alert: "#ff6b6b",
  brief: "#6b88ff",
  watch: "#6b7280",
  japan_watch: "#f5a623",
  ops: "#9aa4b2",
};
// custom channel に id から決定的に割り当てるパレット (built-in 色とは別系統)。
const CUSTOM_PALETTE = [
  "#2dd4bf", // teal
  "#a78bfa", // violet
  "#f472b6", // pink
  "#fb923c", // orange
  "#38bdf8", // sky
  "#a3e635", // lime
];

/** id → 表示色。built-in は固定、custom は id ハッシュでパレットから自動割当 (設定不要)。 */
export function channelColor(id: string): string {
  const builtin = CHANNEL_COLOR[id];
  if (builtin) return builtin;
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return CUSTOM_PALETTE[h % CUSTOM_PALETTE.length];
}

export interface ChannelMeta {
  id: string;
  label: string;
}

/** 生きているチャンネル一覧を返すフック。/api/v1/channels を共有キャッシュで読む。 */
export function useChannels(): ChannelDef[] {
  const { data } = useQuery({
    queryKey: ["channels"],
    queryFn: channelsApi.get,
    staleTime: 60_000,
  });
  return data?.channels ?? [];
}

/** id → {label} を解決するフック。/api/v1/channels を共有キャッシュで読む。 */
export function useChannelMeta(): (id: string) => ChannelMeta {
  const { data } = useQuery({
    queryKey: ["channels"],
    queryFn: channelsApi.get,
    staleTime: 60_000,
  });
  const byId = new Map<string, ChannelDef>((data?.channels ?? []).map((c) => [c.id, c]));
  return (id: string): ChannelMeta => ({ id, label: byId.get(id)?.label || id });
}

/** チャンネル色のチップ (色ドット + 名前)。バッジ用途。 */
export function ChannelChip({ meta }: { meta: ChannelMeta }) {
  const color = channelColor(meta.id);
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-semibold"
      style={{ color, backgroundColor: `${color}1f` }}
    >
      <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: color }} />
      {meta.label}
    </span>
  );
}
