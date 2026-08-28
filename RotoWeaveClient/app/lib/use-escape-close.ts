import { useEffect, useRef } from "react";

type EscapeEntry = { token: symbol; close: () => void };
const escapeStack: EscapeEntry[] = [];
let listening = false;

function handleEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const active = escapeStack.at(-1);
  if (!active) return;
  event.preventDefault();
  event.stopPropagation();
  active.close();
}

function syncListener(): void {
  if (escapeStack.length && !listening) {
    document.addEventListener("keydown", handleEscape, true);
    listening = true;
  } else if (!escapeStack.length && listening) {
    document.removeEventListener("keydown", handleEscape, true);
    listening = false;
  }
}

/** Close the active application dialog with Escape when closing is allowed. */
export function useEscapeClose(onClose: () => void, enabled = true): void {
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!enabled) return undefined;
    const entry: EscapeEntry = { token: Symbol("escape-close"), close: () => onCloseRef.current() };
    escapeStack.push(entry);
    syncListener();
    return () => {
      const index = escapeStack.findIndex((item) => item.token === entry.token);
      if (index >= 0) escapeStack.splice(index, 1);
      syncListener();
    };
  }, [enabled]);
}
