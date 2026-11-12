import { useEffect, useState } from "react";
import { API_WS_URL } from "../api.js";

export function PageHeading({ eyebrow, title, description, action }) {
  return <div className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p className="page-description">{description}</p></div>{action}</div>;
}

export function Notice({ message, kind = "error", onClose }) {
  if (!message) return null;
  return <div className={`notice notice--${kind}`} role="status"><span>{kind === "error" ? "!" : "✓"}</span>{message}{onClose && <button onClick={onClose}>×</button>}</div>;
}

export function StatusPill({ status }) {
  return <span className={`status-pill status-pill--${String(status || "unknown").toLowerCase().replaceAll("_", "-")}`}>{String(status || "unknown").replaceAll("_", " ")}</span>;
}

export function useLiveRefresh(onChange) {
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const token = localStorage.getItem("parkx_access_token");
    if (!token) return undefined;
    const socket = new WebSocket(`${API_WS_URL}?access_token=${encodeURIComponent(token)}`);
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      try { const message = JSON.parse(event.data); if (message.type === "data_changed") onChange(); }
      catch { /* Ignore non-JSON keepalive messages. */ }
    };
    return () => socket.close();
  }, [onChange]);
  return connected;
}

export function useNotice() {
  const [notice, setNotice] = useState(null);
  useEffect(() => { if (!notice) return undefined; const timer = setTimeout(() => setNotice(null), 4500); return () => clearTimeout(timer); }, [notice]);
  return { notice, setNotice };
}

export function formatDate(value, options = {}) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", ...options }).format(new Date(value));
}

export function money(value) { return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(Number(value || 0)); }
