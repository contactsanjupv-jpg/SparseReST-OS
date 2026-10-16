"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import AgentGrid from "../components/AgentGrid";
import AgentInspector from "../components/AgentInspector";
import TasksPanel from "../components/TasksPanel";
import ApprovalsPanel from "../components/ApprovalsPanel";
import IncidentsPanel from "../components/IncidentsPanel";
import CompanyWorld from "../components/CompanyWorld";
import LoginGate from "../components/LoginGate";
import StatusStrip from "../components/StatusStrip";

function Dashboard({ identity, logout }) {
  const [products, setProducts] = useState(null);
  const [selectedProductId, setSelectedProductId] = useState(""); // "" = All Company
  const [agents, setAgents] = useState(null);
  const [selectedAgent, setSelectedAgent] = useState(null);
  const [tab, setTab] = useState("tasks");
  const [view, setView] = useState("world"); // world | grid
  const [apiError, setApiError] = useState(null);

  useEffect(() => {
    api.listProducts().then(setProducts).catch((e) => setApiError(e.message));
  }, []);

  const loadAgents = () => {
    api
      .listAgents(selectedProductId || undefined)
      .then(setAgents)
      .catch((e) => setApiError(e.message));
  };

  useEffect(() => {
    loadAgents();
    const interval = setInterval(loadAgents, 4000);
    return () => clearInterval(interval);
  }, [selectedProductId]);

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="wordmark">
          SparseReST <span className="sub">Company OS Playground</span>
        </div>
        <div className="product-switch">
          <select value={selectedProductId} onChange={(e) => setSelectedProductId(e.target.value)}>
            <option value="">All Company</option>
            {products &&
              products.map((p) => (
                <option value={p.id} key={p.id}>
                  {p.name} ({p.lifecycle})
                </option>
              ))}
          </select>
        </div>
        <div className="topbar-right">
          <div className="tabs" role="tablist" aria-label="View">
            <button role="tab" aria-selected={view === "world"} className={`tab ${view === "world" ? "active" : ""}`} onClick={() => setView("world")}>Company World</button>
            <button role="tab" aria-selected={view === "grid"} className={`tab ${view === "grid" ? "active" : ""}`} onClick={() => setView("grid")}>Agent grid</button>
          </div>
          {identity?.email && <span>{identity.email}</span>}
          {logout && identity?.email && identity.email !== "open-dev-mode" && <button className="btn secondary" onClick={logout}>Sign out</button>}
        </div>
      </div>
      <StatusStrip />

      {apiError && (
        <div style={{ padding: "12px 24px" }}>
          <div className="error-banner">
            Can&apos;t reach the API at the configured URL: {apiError}. Is the backend running (uvicorn apps.api.main:app)?
          </div>
        </div>
      )}

      <div className="main">
        <div>
          <div className="section-title">
            {selectedProductId ? `Agents — ${products?.find((p) => p.id === selectedProductId)?.name || ""}` : "Agents — All Company"}
          </div>
          {view === "world"
            ? <CompanyWorld productId={selectedProductId || undefined} onSelectAgent={(a) => setSelectedAgent(agents?.find((x) => x.id === a.id) || a)} />
            : <AgentGrid agents={agents} onSelect={setSelectedAgent} />}
        </div>

        <div className="panel">
          <div className="tabs">
            <button className={`tab ${tab === "tasks" ? "active" : ""}`} onClick={() => setTab("tasks")}>
              Tasks
            </button>
            <button className={`tab ${tab === "approvals" ? "active" : ""}`} onClick={() => setTab("approvals")}>
              Approvals
            </button>
            <button className={`tab ${tab === "incidents" ? "active" : ""}`} onClick={() => setTab("incidents")}>
              Incidents
            </button>
          </div>

          {tab === "tasks" && <TasksPanel productId={selectedProductId || undefined} />}
          {tab === "approvals" && <ApprovalsPanel />}
          {tab === "incidents" && <IncidentsPanel />}
        </div>
      </div>

      {selectedAgent && (
        <AgentInspector agent={selectedAgent} productId={selectedProductId || undefined} onClose={() => setSelectedAgent(null)} />
      )}
    </div>
  );
}

export default function Home() {
  return <LoginGate>{(auth) => <Dashboard {...auth} />}</LoginGate>;
}
