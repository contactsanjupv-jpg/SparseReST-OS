"use client";

import StatusDot from "./StatusDot";

export default function AgentGrid({ agents, onSelect }) {
  if (!agents) {
    return <div className="empty-state">Loading agents...</div>;
  }
  if (agents.length === 0) {
    return <div className="empty-state">No agents enabled for this view.</div>;
  }

  const byDepartment = {};
  for (const agent of agents) {
    if (!byDepartment[agent.department]) byDepartment[agent.department] = [];
    byDepartment[agent.department].push(agent);
  }

  return (
    <div>
      {Object.entries(byDepartment).map(([department, deptAgents]) => (
        <div className="department-group" key={department}>
          <div className="department-label">{department.replace("_", " ")}</div>
          <div className="agent-grid">
            {deptAgents.map((agent) => (
              <div
                key={agent.id}
                className="agent-card"
                style={{ borderLeftColor: `var(--status-${agent.current_status || "idle"}, var(--status-idle))` }}
                onClick={() => onSelect(agent)}
              >
                <div className="agent-name">{agent.name}</div>
                <div className="agent-meta">
                  <StatusDot status={agent.current_status} />
                  {agent.is_company_level && <span className="badge" style={{ marginLeft: 6 }}>company-level</span>}
                </div>
                <div className="agent-task mono">{agent.current_task_goal || "—"}</div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
