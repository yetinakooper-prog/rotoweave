import { AlertTriangle, RefreshCcw, X } from "lucide-react";
import { useEffect, useRef } from "react";

export function stateTone(state: string) {
  if (["ready", "passed", "completed", "normal", "info"].includes(state)) return "good";
  if (["running", "warming", "starting", "draining", "switching", "queued", "verifying", "hashing", "candidate"].includes(state)) return "work";
  if (["failed", "error", "maintenance", "cancelled", "blocked", "mismatch", "incompatible", "warning"].includes(state)) return "bad";
  return "muted";
}

export function Badge({ state, children }: { state: string; children?: React.ReactNode }) {
  return <span className={`badge ${stateTone(state)}`}>{children || state}</span>;
}

export function Card({ title, icon: Icon, actions, children, className = "" }: { title: string; icon: React.ComponentType<{ size?: number }>; actions?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return <section className={`card ${className}`}><header><div><Icon size={18} /><h2>{title}</h2></div>{actions}</header>{children}</section>;
}

export function ErrorRecovery({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  return <div className="notice error" role="alert"><AlertTriangle /> <span>{error instanceof Error ? error.message : String(error)}</span>{retry && <button className="secondary" onClick={retry}><RefreshCcw />重试</button>}</div>;
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return <div className="skeleton" aria-label="正在加载">{Array.from({ length: rows }, (_, index) => <i key={index} />)}</div>;
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return <div className="empty"><strong>{title}</strong><span>{detail}</span></div>;
}

export function Drawer({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { closeRef.current?.focus(); const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose(); window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey); }, [onClose]);
  return <div className="drawer-backdrop" role="presentation" onMouseDown={event => event.target === event.currentTarget && onClose()}><aside className="drawer" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button ref={closeRef} className="icon-button" aria-label="关闭" onClick={onClose}><X /></button></header><div className="drawer-body">{children}</div></aside></div>;
}

export function formatBytes(value?: number) {
  const bytes = value || 0;
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${Math.round(bytes / 1024)} KiB`;
}

export function short(value: unknown, size = 10) {
  const text = String(value || "—");
  return text.length > size + 5 ? `${text.slice(0, size)}…${text.slice(-4)}` : text;
}
