"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";

const SEVERITY_ICON = { info: "\u2139", warning: "\ud83d\udfe1", error: "\ud83d\udfe0", critical: "\ud83d\udd34" };

export default function IncidentsPanel({ refreshSignal }) {
  const [incidents, setIncidents] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    api
      .listIncidents({ status: "open" })
      .then(setIncidents)
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [refreshSignal]);

  const resolve = async (id) => {
    try {
      await api.resolveIncident(id);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      {!incidents && <div className="empty-state">Loading incidents...</div>}
      {incidents && incidents.length === 0 && <div className="empty-state">No open incidents.</div>}
      {incidents &&
        incidents.map((inc) => (
          <div className="list-item" key={inc.id}>
            <div className="list-item-title">
              {SEVERITY_ICON[inc.severity] || ""} {inc.title}
            </div>
            {inc.cause && <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>Cause: {inc.cause}</div>}
            {inc.impact && <div style={{ fontSize: 12, color: "var(--text-muted)" }}>Impact: {inc.impact}</div>}
            {inc.action_required && (
              <div style={{ fontSize: 12, color: "var(--accent)", marginTop: 4 }}>Action: {inc.action_required}</div>
            )}
            <div className="actions">
              <button className="btn secondary" onClick={() => resolve(inc.id)}>
                Mark resolved
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}
