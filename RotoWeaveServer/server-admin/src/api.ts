let csrfToken = "";
const BASE = "/api/admin/v2";

export async function ensureSession(): Promise<string> {
  if (csrfToken) return csrfToken;
  const response = await fetch(`${BASE}/session`, { cache: "no-store" });
  if (!response.ok) throw new Error(`无法建立管理会话（${response.status}）`);
  const body = await response.json() as { csrfToken: string };
  csrfToken = body.csrfToken;
  return csrfToken;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || "GET").toUpperCase();
  const headers = new Headers(init?.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-RotoWeave-Admin-CSRF", await ensureSession());
    if (init?.body && !(init.body instanceof Blob) && !(init.body instanceof ArrayBuffer)) {
      headers.set("Content-Type", "application/json");
    }
  }
  const response = await fetch(`${BASE}${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { message?: string };
    throw new Error(body.message || `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

export const eventsUrl = `${BASE}/events`;
