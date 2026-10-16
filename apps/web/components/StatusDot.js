"use client";

const LABELS = {
  idle: "Idle",
  thinking: "Thinking",
  working: "Working",
  waiting: "Waiting",
  needs_approval: "Needs approval",
  error: "Error",
  done: "Done",
  blocked: "Blocked",
};

export default function StatusDot({ status }) {
  const color = `var(--status-${status || "idle"}, var(--status-idle))`;
  return (
    <span style={{ display: "inline-flex", alignItems: "center" }}>
      <span className="status-dot" style={{ background: color }} />
      <span className="status-label">{LABELS[status] || status || "idle"}</span>
    </span>
  );
}
