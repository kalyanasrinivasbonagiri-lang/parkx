import BrandMark from "./BrandMark.jsx";

const NAV = {
  driver: [["discover", "Discover parking", "⌖"], ["bookings", "My bookings", "▤"]],
  staff: [["gate", "Gate desk", "⌖"], ["slots", "Slot status", "▦"]],
  admin: [["overview", "Overview", "◫"], ["lots", "Lots & slots", "▦"], ["employees", "Employees", "♙"], ["reports", "Reports", "▥"]],
};

export default function AppShell({ user, onSignOut, active, onNavigate, children }) {
  const role = user.role === "staff" ? "staff" : user.role;
  return <div className="app-shell"><aside className="sidebar"><div className="sidebar-brand"><BrandMark compact /></div><p className="sidebar-label">WORKSPACE</p><nav>{NAV[role].map(([id, label, icon]) => <button key={id} className={`nav-item ${active === id ? "is-active" : ""}`} onClick={() => onNavigate(id)}><span className="nav-icon">{icon}</span>{label}</button>)}</nav><div className="sidebar-bottom"><div className="sidebar-help"><span className="help-icon">?</span><span><strong>Need a hand?</strong><small>Every arrival, made easier.</small></span></div><div className="sidebar-meta">PARKX · SMART PARKING</div></div></aside><main className="workspace"><header className="topbar"><div className="breadcrumb">Parkx <span>/</span> {NAV[role].find(([id]) => id === active)?.[1] || "Workspace"}</div><div className="topbar-user"><span className="live-dot" /><span className="topbar-status">Connected</span><span className="topbar-divider" /><div className="user-avatar">{user.name?.[0]?.toUpperCase()}</div><div className="user-label"><strong>{user.name}</strong><small>{role === "staff" ? "Employee" : role}</small></div><button className="icon-button" title="Sign out" onClick={onSignOut}>↗</button></div></header><section className="workspace-content">{children}</section></main></div>;
}
