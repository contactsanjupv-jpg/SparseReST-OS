"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api";

// 2.5D isometric office rendered in plain SVG (no WebGL, no extra dependencies, runs anywhere).
// HONESTY RULE: every visual state comes from /world/state and /world/events -- real database rows.
// There is no idle "fake busy" animation: an agent only animates as working if a real run says so, and a
// run that stopped reporting shows as stalled. The ticker shows real activity_events and nothing else.

const TW = 120, TH = 60;                          // iso tile footprint
const iso = (c, r) => ({ x: (c - r) * (TW / 2), y: (c + r) * (TH / 2) });
const STATE_COLOR = { working: "#4a90d9", needs_approval: "#e8a33d", error: "#e0616b", stalled: "#b07acc", idle: "#6b7280" };
const STATE_LABEL = { working: "Working", needs_approval: "Needs your approval", error: "Failed", stalled: "Stalled (no recent report)", idle: "Idle" };
const DEPT_COLORS = ["#2c3a52", "#3a3350", "#2f4a45", "#4a3f2c", "#44323a", "#2f3f4a"];

function layoutDepartments(agents) {
  const byDept = {};
  agents.forEach((a) => { (byDept[a.department] = byDept[a.department] || []).push(a); });
  const names = Object.keys(byDept).sort();
  const cols = Math.ceil(Math.sqrt(names.length));
  return names.map((name, i) => {
    const size = Math.max(2, Math.ceil(Math.sqrt(byDept[name].length)) + 1);
    return { name, agents: byDept[name], size, gc: (i % cols) * 4.2, gr: Math.floor(i / cols) * 4.2, color: DEPT_COLORS[i % DEPT_COLORS.length] };
  });
}

function Room({ room, onSelect, selectedSlug }) {
  const o = iso(room.gc, room.gr);
  const n = room.size;
  const corners = [iso(0, 0), iso(n, 0), iso(n, n), iso(0, n)].map((p) => `${p.x},${p.y}`).join(" ");
  const label = iso(n / 2, n);
  return (
    <g transform={`translate(${o.x},${o.y})`}>
      <polygon points={corners} fill={room.color} stroke="#0e1014" strokeWidth="1.5" />
      <polygon points={[iso(0, n), iso(n, n), { x: iso(n, n).x, y: iso(n, n).y + 14 }, { x: iso(0, n).x, y: iso(0, n).y + 14 }].map((p) => `${p.x},${p.y}`).join(" ")} fill="#0e1014" opacity="0.55" />
      <text x={label.x} y={label.y + 30} textAnchor="middle" className="world-dept">{room.name.replace(/_/g, " ")}</text>
      {room.agents.map((a, i) => {
        const per = Math.max(1, n - 1);
        const p = iso(0.8 + (i % per), 0.8 + Math.floor(i / per));
        return <Agent key={a.slug} agent={a} x={p.x} y={p.y} onSelect={onSelect} selected={selectedSlug === a.slug} />;
      })}
    </g>
  );
}

function Agent({ agent, x, y, onSelect, selected }) {
  const color = STATE_COLOR[agent.state] || STATE_COLOR.idle;
  const animate = agent.state === "working" ? "agent-bob" : agent.state === "needs_approval" || agent.state === "error" ? "agent-pulse" : "";
  return (
    <g transform={`translate(${x},${y})`} className="world-agent" onClick={() => onSelect(agent)} tabIndex={0} role="button"
       aria-label={`${agent.name}: ${STATE_LABEL[agent.state]}`} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(agent)}>
      <title>{`${agent.name} — ${STATE_LABEL[agent.state]}${agent.task_goal ? `\n${agent.task_goal}` : ""}`}</title>
      <ellipse cx="0" cy="2" rx="13" ry="6" fill="#000" opacity="0.35" />
      <g className={animate}>
        <rect x="-8" y="-24" width="16" height="22" rx="5" fill={color} stroke={selected ? "#fff" : "#0e1014"} strokeWidth={selected ? 2 : 1} />
        <circle cx="0" cy="-32" r="8" fill="#e9d8c4" stroke="#0e1014" />
        <circle cx="-3" cy="-33" r="1.2" fill="#0e1014" /><circle cx="3" cy="-33" r="1.2" fill="#0e1014" />
        {agent.state === "needs_approval" && <text x="0" y="-45" textAnchor="middle" fontSize="14" fill={color}>!</text>}
        {agent.state === "error" && <text x="0" y="-45" textAnchor="middle" fontSize="14" fill={color}>✕</text>}
      </g>
      <text x="0" y="18" textAnchor="middle" className="world-name">{agent.name.replace(/ Agent$/i, "")}</text>
      {agent.state === "working" && agent.task_goal && (
        <g transform="translate(0,-52)"><rect x="-58" y="-16" width="116" height="18" rx="9" fill="#1b1f26" stroke={color} />
          <text x="0" y="-3" textAnchor="middle" className="world-bubble">{agent.task_goal.length > 20 ? agent.task_goal.slice(0, 19) + "…" : agent.task_goal}</text></g>
      )}
    </g>
  );
}

export default function CompanyWorld({ productId, onSelectAgent }) {
  const [state, setState] = useState(null);
  const [events, setEvents] = useState([]);
  const [error, setError] = useState(null);
  const cursor = useRef(0);
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    let alive = true, first = true;
    cursor.current = 0; setEvents([]);
    const tickState = async () => {
      try { const s = await api.worldState(productId || undefined); if (alive) { setState(s); setError(null); if (first) { cursor.current = Math.max(0, s.cursor - 15); first = false; } } }
      catch (e) { if (alive) setError(e.message); }
    };
    const tickEvents = async () => {
      try {
        const out = await api.worldEvents(cursor.current, productId || undefined);
        if (alive && out.events.length) { cursor.current = out.cursor; setEvents((prev) => [...prev, ...out.events].slice(-40)); }
      } catch { /* the state poll surfaces connectivity errors */ }
    };
    tickState().then(tickEvents);
    const a = setInterval(tickState, 3000), b = setInterval(tickEvents, 2000);
    return () => { alive = false; clearInterval(a); clearInterval(b); };
  }, [productId]);

  const rooms = useMemo(() => (state ? layoutDepartments(state.agents) : []), [state]);
  const bounds = useMemo(() => {
    if (!rooms.length) return "0 0 10 10";
    let minX = 1e9, minY = 1e9, maxX = -1e9, maxY = -1e9;
    rooms.forEach((r) => { const o = iso(r.gc, r.gr); [iso(0, 0), iso(r.size, 0), iso(r.size, r.size), iso(0, r.size)].forEach((p) => {
      minX = Math.min(minX, o.x + p.x); maxX = Math.max(maxX, o.x + p.x); minY = Math.min(minY, o.y + p.y - 80); maxY = Math.max(maxY, o.y + p.y + 50); }); });
    return `${minX - 30} ${minY} ${maxX - minX + 60} ${maxY - minY}`;
  }, [rooms]);

  const counts = useMemo(() => (state ? state.agents.reduce((m, a) => ({ ...m, [a.state]: (m[a.state] || 0) + 1 }), {}) : {}), [state]);
  const pick = (a) => { setSelected(a.slug); onSelectAgent && onSelectAgent(a); };

  if (error && !state) return <div className="error-banner">Company World can&apos;t load: {error}</div>;
  if (!state) return <div className="empty-state">Loading the office...</div>;

  const pipeline = Object.entries(state.pipeline).sort((x, y) => y[1] - x[1]);
  return (
    <div className="world">
      <div className="world-stage">
        <svg viewBox={bounds} role="img" aria-label="Isometric view of the company's agents grouped by department" preserveAspectRatio="xMidYMid meet">
          {rooms.map((r) => <Room key={r.name} room={r} onSelect={pick} selectedSlug={selected} />)}
        </svg>
        {error && <div className="world-stale">Connection lost — showing last known state</div>}
      </div>
      <aside className="world-side">
        <div className="world-card">
          <div className="section-title">Right now</div>
          {Object.keys(STATE_LABEL).map((k) => (
            <div className="world-legend" key={k}><span className="dot" style={{ background: STATE_COLOR[k] }} />{STATE_LABEL[k]}<b>{counts[k] || 0}</b></div>
          ))}
          <div className="world-legend">Pending approvals<b>{state.pending_approvals}</b></div>
          <div className="world-legend">Open incidents<b>{Object.values(state.open_incidents).reduce((a, b) => a + b, 0)}</b></div>
        </div>
        <div className="world-card">
          <div className="section-title">Prospect pipeline</div>
          {pipeline.length === 0 && <div className="empty-state">No prospects yet.</div>}
          {pipeline.map(([stage, n]) => (
            <div className="world-bar" key={stage}><span>{stage.replace(/_/g, " ")}</span>
              <div><i style={{ width: `${Math.max(6, (n / pipeline[0][1]) * 100)}%` }} /></div><b>{n}</b></div>
          ))}
        </div>
        <div className="world-card world-ticker">
          <div className="section-title">Live activity</div>
          {events.length === 0 && <div className="empty-state">Nothing has happened recently.</div>}
          {[...events].reverse().map((e) => (
            <div key={e.id} className="world-event"><span className="mono">{new Date(e.created_at).toLocaleTimeString()}</span> <b>{e.agent || "system"}</b> {e.event_type}</div>
          ))}
        </div>
      </aside>
    </div>
  );
}
