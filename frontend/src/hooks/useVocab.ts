// 語彙 (value→ja_label) を backend から 1 回取得し解決する。
// 設計: docs/vocabulary_label_architecture.md §4.2-4.3。
// backend `/api/v1/vocabularies` が唯一の SSoT。frontend は静的な value→label マップを持たない。
// - render 内: useVocab / useVocabMap / useVocabOptions (React Query 経由)
// - render 外 (純ヘルパ): vocabLabel (共有 cache を同期読み)
// boot gate (components/VocabGate.tsx) が初回描画前に取得を解決するため、cache は常に暖まっている。
import { useQuery } from "@tanstack/react-query";

import { queryClient } from "../api/queryClient";

export interface VocabItem {
  value: string;
  label: string;
  order: number;
  description?: string;
  deprecated?: boolean;
}

export type Vocabularies = Record<string, VocabItem[]>;

export const VOCAB_QUERY_KEY = ["vocabularies"] as const;

async function fetchVocabularies(): Promise<Vocabularies> {
  // timeout 必須: boot gate はアプリ全体を待たせるため、ハング (解決も reject もしない
  // 接続) だと spinner が永久固定になる。timeout→reject→isError→gate は degradation で先へ。
  const r = await fetch("/api/v1/vocabularies", {
    credentials: "same-origin",
    signal: AbortSignal.timeout(8000),
  });
  if (!r.ok) throw new Error(`vocabularies ${r.status}`);
  return (await r.json()) as Vocabularies;
}

const VOCAB_QUERY_OPTIONS = {
  queryKey: VOCAB_QUERY_KEY,
  queryFn: fetchVocabularies,
  staleTime: Infinity, // 起動時 1 回。運用者編集時は該当箇所で invalidate する (docs §4.2)。
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const;

function itemsToMap(items: VocabItem[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const i of items ?? []) out[i.value] = i.label;
  return out;
}

/** value → label の resolver (1 vocab)。未知値は原値へ fallback (旧 label() と同挙動)。 */
export function useVocab(name: string): (value: string | null | undefined) => string {
  const { data } = useQuery(VOCAB_QUERY_OPTIONS);
  const map = itemsToMap(data?.[name]);
  return (value) => (!value ? "" : (map[value] ?? value));
}

/** vocab を Record<string,string> で返す (合成が必要な箇所用)。 */
export function useVocabMap(name: string): Record<string, string> {
  const { data } = useQuery(VOCAB_QUERY_OPTIONS);
  return itemsToMap(data?.[name]);
}

/** vocab の順序付き options (フィルタ選択肢用)。 */
export function useVocabOptions(name: string): VocabItem[] {
  const { data } = useQuery(VOCAB_QUERY_OPTIONS);
  return data?.[name] ?? [];
}

/** boot gate 用: 初回取得の解決状況。error でも degradation して先へ進む。 */
export function useVocabGate(): { ready: boolean; error: boolean } {
  const { isSuccess, isError } = useQuery(VOCAB_QUERY_OPTIONS);
  return { ready: isSuccess || isError, error: isError };
}

/**
 * render 外 (純ヘルパ関数) から value→label を解決する非フック アクセサ。
 * 共有 queryClient の cache を同期読み (boot gate 済みなので通常は解決済み)。
 * cache 未取得や未知値は原値へ fallback。
 */
export function vocabLabel(name: string, value: string | null | undefined): string {
  if (!value) return "";
  const data = queryClient.getQueryData<Vocabularies>(VOCAB_QUERY_KEY);
  const item = data?.[name]?.find((i) => i.value === value);
  return item?.label ?? value;
}

/** snake_case / slug → 可読 (未知値の humanizer、Phase 4 の D 用途)。生トークンを直出ししない。 */
export function humanize(value: string): string {
  if (!value) return "";
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
