"use client";

import { useEffect, useState } from "react";
import { api, session, setUnauthorizedHandler } from "../lib/api";

// Wraps the app. Asks the backend whether login is needed; if so and there is no valid session, shows the form.
export default function LoginGate({ children }) {
  const [phase, setPhase] = useState("checking"); // checking | login | ready | down
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [identity, setIdentity] = useState(null);

  useEffect(() => {
    setUnauthorizedHandler(() => setPhase("login"));
    (async () => {
      try {
        const status = await api.authStatus();
        if (!status.login_required) return setPhase("ready");
        if (!session.get()) return setPhase("login");
        setIdentity(await api.me());
        setPhase("ready");
      } catch (e) {
        setPhase(e.status === 401 ? "login" : "down");
        if (e.status !== 401) setError(e.message);
      }
    })();
  }, []);

  const submit = async (ev) => {
    ev.preventDefault();
    setBusy(true); setError(null);
    try {
      const out = await api.login(email, password, totp);
      session.set(out.token);
      setIdentity({ email: out.email });
      setPassword(""); setTotp(""); setNeedTotp(false);
      setPhase("ready");
    } catch (e) {
      if (e.detail === "totp_required") { setNeedTotp(true); setError("Enter the 6-digit code from your authenticator app."); }
      else if (e.status === 429) setError(`Too many attempts. Try again in ${Math.ceil((e.retryAfter || 60) / 60)} min.`);
      else setError("Invalid credentials.");
    } finally { setBusy(false); }
  };

  const logout = async () => { try { await api.logout(); } catch {} session.clear(); setIdentity(null); setPhase("login"); };

  if (phase === "checking") return <div className="empty-state">Connecting...</div>;
  if (phase === "down") return <div style={{ padding: 24 }}><div className="error-banner">Can&apos;t reach the API: {error}</div></div>;
  if (phase === "login") {
    return (
      <div className="login-wrap">
        <form className="login-card" onSubmit={submit}>
          <div className="wordmark">SparseReST <span className="sub">Company OS</span></div>
          <label>Email<input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
          <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
          {needTotp && <label>Authenticator code<input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={totp} onChange={(e) => setTotp(e.target.value)} autoFocus /></label>}
          {error && <div className="error-banner">{error}</div>}
          <button type="submit" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</button>
        </form>
      </div>
    );
  }
  return children({ identity, logout });
}
