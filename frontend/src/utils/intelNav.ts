// 実体間 cross-navigation の SSoT (single source of truth)。
// Intel Graph タブバー撤去後、ビュー移動は左 nav と同じ pathname 遷移 (/app/intel/<tab>) に
// 一本化する。各 call site が href / 遷移先を手書きせずここ経由にすることで、「どの実体を
// どのサーフェスに飛ばすか」を一元管理し、リンク切れ・id 落ち・行き先のばらつきを防ぐ。
//
// 同一ページ内の選択 (脅威アクタータブの actor 一覧、アクター辞書の table など) は従来どおり
// filters の in-place 選択 / drawer を使う。ここは cross-page 遷移リンク専用。

import type { Tab } from "../state/filters";

// ── Intel ビュー (synthesis / pmesii / threats / forecast / operations) ──
export function intelHref(tab: Tab): string {
  return `/app/intel/${tab}`;
}
export function goToIntel(tab: Tab): void {
  window.location.href = intelHref(tab);
}

// ── アクター ──
// 既定 = 観測ビュー (脅威アクタータブ)。記事・関係・共起を見る分析文脈はこちら。
// /app/intel/threats#actor=<id> は filters が初回ロード時にも消費し当該 actor を選択する。
export function actorHref(actorId: string): string {
  return `/app/intel/threats#actor=${encodeURIComponent(actorId)}`;
}
export function goToActor(actorId: string): void {
  window.location.href = actorHref(actorId);
}
// 辞書ビュー (alias / MITRE / 編集)。明示的な「辞書を開く」用。ActorsPage が ?actor= で
// 詳細ドロワーを直接開く。観測ビューとは役割が異なる (observation vs knowledge)。
export function actorDictHref(actorId: string): string {
  return `/app/actors?actor=${encodeURIComponent(actorId)}`;
}

// ── 状況台帳 (situation) ──
// 実体ページ (アクター辞書の関連情勢等) から台帳の個別 situation への着地 (C-lite)。
// filters.parseHash が situation param で ledger ビューを強制し、LedgerView が
// mount 時に該当カードを展開してスクロールする。
export function situationHref(situationId: string): string {
  return `/app/intel/synthesis#situation=${encodeURIComponent(situationId)}`;
}
