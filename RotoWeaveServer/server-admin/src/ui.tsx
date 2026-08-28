import { AlertTriangle, CheckCircle2, Copy, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

type ConfirmOptions = { title: string; message: string; confirmLabel?: string; danger?: boolean };
type UiContextValue = { confirm: (options: ConfirmOptions) => Promise<boolean>; toast: (message: string, tone?: "good" | "bad") => void };
const UiContext = createContext<UiContextValue | null>(null);

export function useUi() {
  const value = useContext(UiContext);
  if (!value) throw new Error("UiProvider is missing");
  return value;
}

export function UiProvider({ children }: { children: React.ReactNode }) {
  const [dialog, setDialog] = useState<(ConfirmOptions & { resolve: (value: boolean) => void }) | null>(null);
  const [notice, setNotice] = useState<{ message: string; tone: "good" | "bad" }>();
  const trigger = useRef<HTMLElement | null>(null);
  const lastAction = useRef<HTMLElement | null>(null);
  const restorePending = useRef(false);
  useEffect(() => {
    const remember = (event: Event) => {
      const target = event.target instanceof HTMLElement ? event.target.closest<HTMLElement>("button,a,[role=button]") : null;
      if (target && !target.closest(".modal")) {
        lastAction.current = target;
        trigger.current = target;
      }
    };
    document.addEventListener("pointerdown", remember, true);
    document.addEventListener("click", remember, true);
    return () => {
      document.removeEventListener("pointerdown", remember, true);
      document.removeEventListener("click", remember, true);
    };
  }, []);
  const confirm = useCallback((options: ConfirmOptions) => {
    const focused = document.activeElement as HTMLElement | null;
    const eventTarget = window.event?.target;
    trigger.current = lastAction.current?.isConnected
      ? lastAction.current
      : focused && focused !== document.body
        ? focused
        : eventTarget instanceof HTMLElement && eventTarget !== document.body
          ? eventTarget
          : document.querySelector<HTMLElement>("#main-content");
    return new Promise<boolean>(resolve => setDialog({ ...options, resolve }));
  }, []);
  const close = useCallback((value: boolean) => {
    restorePending.current = true;
    setDialog(current => { current?.resolve(value); return null; });
  }, []);
  const toast = useCallback((message: string, tone: "good" | "bad" = "good") => {
    setNotice({ message, tone });
    window.setTimeout(() => setNotice(undefined), 3600);
  }, []);
  useEffect(() => {
    if (!dialog) return;
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && close(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [dialog, close]);
  useEffect(() => {
    if (dialog || !restorePending.current) return;
    // Restore only after React has removed the auto-focused modal control.
    // The delayed second pass also survives React StrictMode's effect probe and
    // browser focus teardown following Escape/click.
    const restore = () => {
      if (!document.querySelector("[role=dialog]") && trigger.current?.isConnected) {
        trigger.current.focus({ preventScroll: true });
        restorePending.current = false;
      }
    };
    window.setTimeout(restore, 0);
    window.setTimeout(restore, 80);
  }, [dialog]);
  return <UiContext.Provider value={{ confirm, toast }}>
    {children}
    {dialog && <div className="modal-backdrop" role="presentation" onMouseDown={event => event.currentTarget === event.target && close(false)}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <div className="modal-icon"><AlertTriangle /></div>
        <h2 id="confirm-title">{dialog.title}</h2><p>{dialog.message}</p>
        <div className="actions"><button className="secondary" autoFocus onClick={() => close(false)}>取消</button><button className={dialog.danger ? "danger" : ""} onClick={() => close(true)}>{dialog.confirmLabel || "确认"}</button></div>
      </section>
    </div>}
    {notice && <div className={`toast ${notice.tone}`} role="status">{notice.tone === "good" ? <CheckCircle2 /> : <AlertTriangle />}{notice.message}<button aria-label="关闭通知" onClick={() => setNotice(undefined)}><X /></button></div>}
  </UiContext.Provider>;
}

export function CopyButton({ value, label = "复制" }: { value: unknown; label?: string }) {
  const { toast } = useUi();
  return <button className="icon-button" title={label} aria-label={label} onClick={async () => { await navigator.clipboard.writeText(typeof value === "string" ? value : JSON.stringify(value, null, 2)); toast("已复制到剪贴板"); }}><Copy /></button>;
}
