"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import StatusDot from "./StatusDot";

export default function AgentInspector({ agent, productId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .inspectAgent(agent.id, productId)
      .then((d) => !cancelled && setDetail(d))
      .catch((e) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [agent.id, productId]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <div style={{ fontWeight: 600, fontSize: 15 }}>{agent.name}</div>
            <div className="agent-meta">
              <StatusDot status={detail?.agent?.current_status || agent.current_status} />
            </div>
          </div>
          <button className="close-btn" onClick={onClose}>
            &times;
          </button>
        </div>

        {error && <div className="error-banner">{error}</div>}
        {!detail && !error && <div className="empty-state">Loading...</div>}

        {detail && (
          <>
            <div className="section-title">Recent tasks</div>
            {detail.recent_tasks.length === 0 && <div className="empty-state">No tasks yet.</div>}
            {detail.recent_tasks.map((t) => (
              <div className="list-item" key={t.id}>
                <div className="list-item-title">{t.goal}</div>
                <div className="list-item-meta mono">
                  <span className="badge">{t.status}</span>
                  <span>attempt {t.attempts}</span>
                  <span>{new Date(t.created_at).toLocaleString()}</span>
                </div>
              </div>
            ))}

            <div className="section-title" style={{ marginTop: 20 }}>
              Recent tool calls
            </div>
            {detail.recent_tool_calls.length === 0 && <div className="empty-state">No tool calls yet.</div>}
            {detail.recent_tool_calls.map((c, i) => (
              <div className="list-item" key={i}>
                <div className="list-item-title mono">{c.tool}</div>
                <div className="list-item-meta mono">
                  <span className="badge">{c.result}</span>
                  {c.cost_usd > 0 && <span>${c.cost_usd.toFixed(4)}</span>}
                  <span>{new Date(c.created_at).toLocaleString()}</span>
                </div>
              </div>
            ))}

            <div className="section-title" style={{ marginTop: 20 }}>
              Recent activity
            </div>
            {detail.recent_activity.length === 0 && <div className="empty-state">No activity yet.</div>}
            {detail.recent_activity.map((e, i) => (
              <div className="trace-line mono" key={i}>
                <span style={{ color: "var(--text-faint)" }}>{new Date(e.created_at).toLocaleTimeString()}</span>
                <span>{e.event_type}</span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
