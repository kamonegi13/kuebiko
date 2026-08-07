// ISO 3166-1 alpha-2 → 日本語国名。ラベルは backend 配信 vocab "country"
// (config/countries.yaml が SSoT) を解決する。vocab キーは大文字 ISO2 なので
// 必ず toUpperCase() して引く。未知コードは大文字化した原値へ fallback。
import { vocabLabel } from "../hooks/useVocab";

/** ISO2 コード (大小問わず) を日本語国名にする。未知コードは大文字化した原値を返す。 */
export function countryLabel(iso: string | null | undefined): string {
  if (!iso) return "";
  return vocabLabel("country", iso.toUpperCase()) || iso.toUpperCase();
}
