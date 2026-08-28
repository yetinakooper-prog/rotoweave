import { useEffect, useRef } from "react";

export const V4_TIP_DURATION_MS = 3000;

export function useAutoDismissNoticeV4<T>(notice: T | null, clear: () => void, enabled = true): void {
  const clearRef = useRef(clear);
  useEffect(() => { clearRef.current = clear; }, [clear]);
  useEffect(() => {
    if (!enabled || notice === null) return undefined;
    const timer = window.setTimeout(() => clearRef.current(), V4_TIP_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, [enabled, notice]);
}
