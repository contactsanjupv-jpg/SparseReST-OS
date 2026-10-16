"use client";

import { useEffect, useRef, useState } from "react";
import { api, streamTask } from "../lib/api";

export default function TasksPanel({ productId, refreshSignal }) {
  const [tasks, setTasks] = useState(null);
  const [goal, setGoal] = useState("");
  const [error, setError] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [streamingTaskId, setStreamingTaskId] = useState(null);
  const [streamLines, setStreamLines] = useState([]);
  const eventSourceRef = useRef(null);

  const load = () => {
    api
      .listTasks(productId ? { product_id: productId } : {})
      .then(setTasks)
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 3000);
    return () => clearInterval(interval);
  }, [productId, refreshSignal]);

  const submit = async () => {
    if (!goal.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.submitGoal(goal.trim());
      setGoal("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  const retry = async (taskId) => {
    try {
      await api.retryTask(taskId);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  const cancel = async (taskId) => {
    try {
      await api.cancelTask(taskId);
      load();
    } catch (e) {
      setError(e.message);
    }
  };

  const startStream = (taskId) => {
    if (eventSourceRef.current) eventSourceRef.current();
    setStreamingTaskId(taskId);
    setStreamLines([]);
    // fetch-based stream (not EventSource) so the Authorization header can be sent.
    eventSourceRef.current = streamTask(taskId, (data) => setStreamLines((lines) => [...lines, data]));
  };

  useEffect(() => () => eventSourceRef.current && eventSourceRef.current(), []);

  return (
    <div>
      <div className="goal-box">
        <input
          placeholder="e.g. find 5 qualified prospects for xflow"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        <button onClick={submit} disabled={submitting}>
          {submitting ? "Sending..." : "Send to CEO"}
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {!tasks && <div className="empty-state">Loading tasks...</div>}
      {tasks && tasks.length === 0 && <div className="empty-state">No tasks yet -- submit a goal above.</div>}
      {tasks &&
        tasks.map((t) => (
          <div className="list-item" key={t.id}>
            <div className="list-item-title">{t.goal}</div>
            <div className="list-item-meta mono">
              <span className="badge">{t.agent_slug}</span>
              <span className="badge">{t.status}</span>
              <span>attempt {t.attempts}/{t.max_attempts}</span>
            </div>
            {t.last_error && <div style={{ fontSize: 11, color: "var(--status-error)", marginTop: 4 }}>{t.last_error}</div>}
            <div className="actions">
              <button className="btn secondary" onClick={() => startStream(t.id)}>
                Stream
              </button>
              {t.status === "failed" && (
                <button className="btn secondary" onClick={() => retry(t.id)}>
                  Retry
                </button>
              )}
              {["pending", "in_progress", "needs_approval"].includes(t.status) && (
                <button className="btn danger" onClick={() => cancel(t.id)}>
                  Cancel
                </button>
              )}
            </div>
          </div>
        ))}

      {streamingTaskId && (
        <div className="panel" style={{ marginTop: 14 }}>
          <div className="section-title">Live trace</div>
          {streamLines.length === 0 && <div className="empty-state">Waiting for events...</div>}
          {streamLines.map((line, i) => (
            <div className="trace-line mono" key={i}>
              <span>{line.event_type}</span>
              {line.payload && <span style={{ color: "var(--text-faint)" }}>{JSON.stringify(line.payload)}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
