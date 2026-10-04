// 利用者保存ビューの読み書き。ops (full instance) = DB (news-views API, 版履歴あり)。
// 写し (VITE_MIRROR=1 ビルド) と公開 read-only instance (ops ビルドだが
// READ_ONLY=1、news_views は REMOTE_WRITE_ALLOWLIST 未登録なので PUT は常に 403) は
// 端末の localStorage のみ (docs/news_filter_ux.md §3)。
//
// read_only 判定は起動時の runtime-flags で先に分かるので事前に localStorage へ倒すが、
// 判定が後から確定する・一時的な不整合がある場合に備えて **PUT 失敗時も必ず
// localStorage へ退避する** (保存をサイレントに失わない)。失敗で退避したときは
// notice で短い通知を返す (呼び手が画面に出す)。
// 既定ビュー (BUILTIN_VIEWS) は両方で常に使える (このフックは利用者保存分だけを返す)。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { newsViewsApi } from "../../api/newsViews";
import { useRuntimeFlags } from "../../hooks/useRuntimeFlags";
import { isUserViewId, slugifyViewId, type NewsFilters, type NewsView } from "./views";

const MIRROR = import.meta.env.VITE_MIRROR === "1";
const LOCAL_KEY = "news-views:user";

const LOCAL_SAVE_NOTICE = "この端末に保存しました";
const LOCAL_FALLBACK_NOTICE = "サーバへの保存に失敗したため、この端末に保存しました";

function readLocal(): NewsView[] {
  try {
    const raw = localStorage.getItem(LOCAL_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    return Array.isArray(parsed) ? (parsed as NewsView[]) : [];
  } catch {
    return [];
  }
}

function writeLocal(views: NewsView[]): void {
  try {
    localStorage.setItem(LOCAL_KEY, JSON.stringify(views));
  } catch {
    /* localStorage 不可環境は無視 (この端末だけ保存されない) */
  }
}

/** server と local を id で合成する (local 優先。フォールバック退避分を取りこぼさない)。 */
function mergeViews(serverViews: readonly NewsView[], localViews: readonly NewsView[]): NewsView[] {
  const merged = [...localViews];
  for (const v of serverViews) {
    if (!merged.some((m) => m.id === v.id)) merged.push(v);
  }
  return merged;
}

export interface UseNewsViewsResult {
  /** 利用者保存ビュー (既定ビューを含まない。server + local の合成)。 */
  userViews: NewsView[];
  loading: boolean;
  saveView: (label: string, filters: Partial<NewsFilters>) => void;
  renameView: (id: string, label: string) => void;
  deleteView: (id: string) => void;
  /** 直前の保存操作に伴う短い通知 (端末保存に倒れたことを示す)。無ければ null。 */
  notice: string | null;
}

export function useNewsViews(): UseNewsViewsResult {
  const flags = useRuntimeFlags();
  // 写しはサーバ API を持たない。公開 read-only instance (ops ビルドだが READ_ONLY=1) は
  // API はあるが news_views の PUT を遠隔許可していないため常に 403 — どちらも
  // 「この端末の localStorage」に倒す (事前判定。PUT 失敗時のフォールバックは別に持つ)。
  const forceLocal = MIRROR || flags.read_only;

  const [localViews, setLocalViews] = useState<NewsView[]>(() => readLocal());
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showNotice = useCallback((text: string) => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    setNotice(text);
    noticeTimer.current = setTimeout(() => setNotice(null), 4000);
  }, []);
  useEffect(() => () => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
  }, []);

  // ops: DB 版の一覧を取得 (read-only instance でも Tier1 認証済みなら読める)。
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["news-views"],
    queryFn: () => newsViewsApi.list(),
    enabled: !MIRROR,
    staleTime: 60_000,
    retry: false,
  });
  const serverViews = !MIRROR && !query.isError ? query.data ?? [] : [];

  // 表示は常に server + local を合成する — サーバ保存分も端末退避分も両方見える。
  const userViews = useMemo(() => mergeViews(serverViews, localViews), [serverViews, localViews]);

  const mutation = useMutation({ mutationFn: (next: NewsView[]) => newsViewsApi.save(next) });

  const persist = useCallback(
    async (next: NewsView[]) => {
      if (forceLocal) {
        // 書き込み先が最初から localStorage のみ (写し / read-only instance)。
        writeLocal(next);
        setLocalViews(next);
        showNotice(LOCAL_SAVE_NOTICE);
        return;
      }
      try {
        await mutation.mutateAsync(next);
        qc.setQueryData(["news-views"], next);
      } catch {
        // PUT が 403/失敗 (想定外の read-only 判定漏れ・一時的な通信障害等) →
        // **保存をサイレントに失わない**: localStorage へ退避して短い通知を出す。
        writeLocal(next);
        setLocalViews(next);
        showNotice(LOCAL_FALLBACK_NOTICE);
      }
    },
    [forceLocal, mutation, qc, showNotice],
  );

  const saveView = useCallback(
    (label: string, filters: Partial<NewsFilters>) => {
      const id = slugifyViewId(label);
      void persist([...userViews, { id, label, filters }]);
    },
    [userViews, persist],
  );

  const renameView = useCallback(
    (id: string, label: string) => {
      if (!isUserViewId(id)) return;
      void persist(userViews.map((v) => (v.id === id ? { ...v, label } : v)));
    },
    [userViews, persist],
  );

  const deleteView = useCallback(
    (id: string) => {
      if (!isUserViewId(id)) return;
      void persist(userViews.filter((v) => v.id !== id));
    },
    [userViews, persist],
  );

  // 他タブでの localStorage 変更は即時反映しない (同一タブ内のみ)。要件上十分 —
  // 複数タブの同時編集は個人運用で想定しない。
  useEffect(() => {
    setLocalViews(readLocal());
  }, []);

  return {
    userViews,
    loading: MIRROR ? false : query.isLoading,
    saveView,
    renameView,
    deleteView,
    notice,
  };
}
