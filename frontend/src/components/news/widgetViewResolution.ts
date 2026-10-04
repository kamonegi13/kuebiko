// ダッシュボードの記事フィード/事象ニュース widget が「ビュー」設定を解決するための
// 共有ロジック (docs/news_filter_ux.md §4)。view が選ばれていればその絞り込みを使い、
// 未選択 (旧保存設定を含む) は呼び手が渡す legacy 絞り込み (= ad-hoc view) を使う。
// これにより既存の保存済み widget 設定は変更なしで動き続ける。

import { BUILTIN_VIEWS, EMPTY_FILTERS, viewEffectiveFilters, type NewsFilters, type NewsView } from "./views";

export function resolveWidgetView(viewId: string, userViews: readonly NewsView[]): NewsView | null {
  if (!viewId) return null;
  return [...BUILTIN_VIEWS, ...userViews].find((v) => v.id === viewId) ?? null;
}

/** view が選ばれていれば viewEffectiveFilters、そうでなければ legacy (旧 field-by-field
 *  設定) を既定値で埋めたもの。``view`` も返す (widget の見出し・ヘッダーリンクに使う)。 */
export function resolveWidgetViewFilters(
  viewId: string,
  userViews: readonly NewsView[],
  legacy: Partial<NewsFilters>,
): { view: NewsView | null; filters: NewsFilters } {
  const view = resolveWidgetView(viewId, userViews);
  const filters = view ? viewEffectiveFilters(view) : { ...EMPTY_FILTERS, ...legacy };
  return { view, filters };
}
