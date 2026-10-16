"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";

export default function ApprovalsPanel({ refreshSignal }) {
  const [approvals, setApprovals] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    api
      .listApprovals("pending")
      .then(setApprovals)
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 3000);
    return () => clearInterval(interval);
  }, [refreshSignal]);

  const decideAll = async (approved) => {
    if (!window.confirm(`${approved ? "Approve" : "Reject"} ALL ${outreach.length} pending outreach drafts?`)) return;
    try {
      await api.bulkDecide(approved);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  const outreach = (approvals || []).filter((a) => a.requested_by === "sales");

  const decide = async (id, approved) => {
    try {
      await api.decideApproval(id, approved);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      {!approvals && <div className="empty-state">Loading approvals...</div>}
      {approvals && approvals.length === 0 && <div className="empty-state">Nothing pending approval.</div>}
      {outreach.length > 1 && (
        <div className="actions" style={{ marginBottom: 12 }}>
          <button className="btn" onClick={() => decideAll(true)}>Approve all {outreach.length} outreach drafts</button>
          <button className="btn secondary" onClick={() => decideAll(false)}>Reject all</button>
        </div>
      )}
      {approvals &&
        approvals.map((a) => (
          <div className="list-item" key={a.id}>
            <div className="list-item-title">{a.summary}</div>
            {a.details && a.details.body && (
              <details style={{ margin: "6px 0" }}>
                <summary>Read the email ({a.details.draft_source})</summary>
                <div className="mono" style={{ whiteSpace: "pre-wrap", fontSize: 12, padding: "8px 0" }}>
                  <b>To:</b> {a.details.to}{"\n"}<b>Subject:</b> {a.details.subject}{"\n\n"}{a.details.body}
                  {a.details.hook ? `\n\n[personalised from their site: "${a.details.hook}"]` : ""}
                </div>
              </details>
            )}
            <div className="list-item-meta mono">
              <span className="badge">{a.requested_by}</span>
              <span className="badge">{a.risk_level} risk</span>
            </div>
            <div className="actions">
              <button className="btn" onClick={() => decide(a.id, true)}>
                Approve
              </button>
              <button className="btn secondary" onClick={() => decide(a.id, false)}>
                Reject
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}
