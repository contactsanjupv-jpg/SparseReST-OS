"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";

// Answers "is the company actually running?" from real data: are workers alive, is the schema current,
// which integrations are live vs stubbed. Contains no secrets.
export default function StatusStrip() {
  const [s, setS] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api.opsStatus().then((v) => alive && setS(v)).catch(() => alive && setS(null));
    load(); const t = setInterval(load, 10000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  if (!s) return null;
  const q = s.queue || {};
  const incidents = Object.values(s.open_incidents || {}).reduce((a, b) => a + b, 0);
  return (
    <div className="status-strip mono">
      <span className={s.workers_alive ? "" : "bad"}>workers: {s.workers_alive}{s.workers_alive ? "" : " — none running, tasks will not execute"}</span>
      <span>inbound: {s.inbound_alive ? "running" : s.integrations.inbound_imap ? "configured, not running" : "off"}</span>
      <span className={s.schema_up_to_date ? "" : "bad"}>schema: {s.schema_up_to_date ? "current" : "MIGRATIONS PENDING"}</span>
      <span>queue: {q.pending || 0} pending · {q.in_progress || 0} running · {q.needs_approval || 0} awaiting approval</span>
      <span className={incidents ? "bad" : ""}>incidents: {incidents}</span>
      <span>llm: {s.integrations.llm} · enrichment: {s.integrations.enrichment} · outreach: {s.integrations.outreach}</span>
    </div>
  );
}
