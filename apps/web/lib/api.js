// Talks to the real backend (apps/api). No mock/sample data exists anywhere in this app.
//
// AUTH: the founder signs in (POST /auth/login) and receives a session token. It is kept in
// sessionStorage -- cleared when the tab closes -- and sent as a Bearer header. The static
// FOUNDER_API_KEY is deliberately NOT supported here any more: a NEXT_PUBLIC_* value is
// readable by anyone who opens devtools, so it can never be a secret in a browser.

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
const TOKEN_KEY = "sparserest.session";

let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn; };

export const session = {
  get: () => (typeof window === "undefined" ? null : window.sessionStorage.getItem(TOKEN_KEY)),
  set: (t) => window.sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => window.sessionStorage.removeItem(TOKEN_KEY),
};

function authHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  const token = session.get();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers: authHeaders(options.headers) });
  if (!res.ok) {
    let detail;
    try { detail = (await res.json()).detail; } catch { detail = res.statusText; }
    if (res.status === 401 && path !== "/auth/login") { session.clear(); onUnauthorized(); }
    const err = new Error(`${res.status}: ${typeof detail === "string" ? detail : "request failed"}`);
    err.status = res.status; err.detail = detail; err.retryAfter = res.headers.get("retry-after");
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

// EventSource cannot send an Authorization header, so SSE is read with fetch + a stream reader.
// Returns a function that cancels the stream.
export function streamTask(taskId, onEvent) {
  const controller = new AbortController();
  (async () => {
    try {
      const res = await fetch(`${API_BASE}/tasks/${taskId}/stream`, { headers: authHeaders(), signal: controller.signal });
      if (res.status === 401) { session.clear(); onUnauthorized(); return; }
      if (!res.ok || !res.body) { onEvent({ event_type: "stream.error", payload: { status: res.status } }); return; }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx); buffer = buffer.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (line) { try { onEvent(JSON.parse(line.slice(6))); } catch { /* ignore malformed frame */ } }
        }
      }
    } catch (e) {
      if (e.name !== "AbortError") onEvent({ event_type: "stream.error", payload: { message: String(e.message || e) } });
    }
  })();
  return () => controller.abort();
}

const qs = (params = {}) => {
  const s = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== "")).toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => request("/health"),
  authStatus: () => request("/auth/status"),
  login: (email, password, totp_code) => request("/auth/login", { method: "POST", body: JSON.stringify({ email, password, totp_code: totp_code || undefined }) }),
  logout: () => request("/auth/logout", { method: "POST" }),
  me: () => request("/auth/me"),
  listProducts: () => request("/products"),
  listAgents: (productId) => request(`/agents${qs({ product_id: productId })}`),
  inspectAgent: (agentId, productId) => request(`/agents/${agentId}${qs({ product_id: productId })}`),
  listTasks: (params = {}) => request(`/tasks${qs(params)}`),
  submitGoal: (goal) => request("/tasks", { method: "POST", body: JSON.stringify({ goal }) }),
  retryTask: (id) => request(`/tasks/${id}/retry`, { method: "POST" }),
  cancelTask: (id) => request(`/tasks/${id}/cancel`, { method: "POST" }),
  listApprovals: (status) => request(`/approvals${qs({ status })}`),
  bulkDecide: (approved) => request("/approvals/bulk-decide", { method: "POST", body: JSON.stringify({ approved, all_pending_outreach: true }) }),
  autopilotStatus: (productId) => request(`/autopilot/${productId}/status`),
  decideApproval: (id, approved) => request(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ approved }) }),
  listIncidents: (params = {}) => request(`/incidents${qs(params)}`),
  resolveIncident: (id) => request(`/incidents/${id}/resolve`, { method: "POST" }),
  worldState: (productId) => request(`/world/state${qs({ product_id: productId })}`),
  worldEvents: (afterId, productId) => request(`/world/events${qs({ after_id: afterId, product_id: productId })}`),
  funnel: (productId) => request(`/analytics/funnel${qs({ product_id: productId })}`),
  opsStatus: () => request("/ops/status"),
  opsWorkers: () => request("/ops/workers"),
};
