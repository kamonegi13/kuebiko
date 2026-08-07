import { useEffect, useState, type Dispatch, type SetStateAction } from "react";

// localStorage 永続な useState。フィルタ選択を画面更新後も保持する (UX)。
// readonly/mobile 等で localStorage 不可でも write/read 失敗は無視してメモリ動作する。
export function usePersistedState<T>(
  key: string,
  initial: T,
): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    try {
      const s = localStorage.getItem(key);
      return s !== null ? (JSON.parse(s) as T) : initial;
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* localStorage 不可環境は無視 */
    }
  }, [key, value]);
  return [value, setValue];
}
