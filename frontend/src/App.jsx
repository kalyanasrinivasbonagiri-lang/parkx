import { useEffect, useState } from "react";
import { getCurrentUser } from "./api.js";
import AuthPage from "./pages/AuthPage.jsx";
import DriverDashboard from "./pages/DriverDashboard.jsx";
import StaffDashboard from "./pages/StaffDashboard.jsx";
import AdminDashboard from "./pages/AdminDashboard.jsx";

export default function App() {
  const [user, setUser] = useState(null);
  const [checking, setChecking] = useState(true);
  useEffect(() => {
    if (!localStorage.getItem("parkx_access_token")) { setChecking(false); return; }
    getCurrentUser().then(setUser).catch(() => localStorage.removeItem("parkx_access_token")).finally(() => setChecking(false));
  }, []);
  function signOut() { localStorage.removeItem("parkx_access_token"); setUser(null); }
  if (checking) return <main className="boot-screen"><span className="spinner" /> Loading your workspace…</main>;
  if (!user) return <AuthPage onAuthenticated={setUser} />;
  const common = { user, onSignOut: signOut };
  if (user.role === "admin") return <AdminDashboard {...common} />;
  if (user.role === "staff") return <StaffDashboard {...common} />;
  return <DriverDashboard {...common} />;
}
