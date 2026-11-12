import { useState } from "react";
import { getCurrentUser, signIn, signUp } from "../api.js";
import AuthLayout from "../components/AuthLayout.jsx";

function Field({ label, name, type = "text", value, onChange, placeholder, autoComplete, required = true }) {
  return <label className="field"><span className="field__label">{label}</span><input className="field__input" type={type} name={name} value={value} onChange={onChange} placeholder={placeholder} autoComplete={autoComplete} required={required} /></label>;
}

export default function AuthPage({ onAuthenticated }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ name: "", email: "", phone: "", password: "", vehicle: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  function updateField(event) { setForm((current) => ({ ...current, [event.target.name]: event.target.value })); }
  async function handleSubmit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const result = mode === "login" ? await signIn(form.email, form.password) : await signUp({ name: form.name, email: form.email, phone: form.phone, password: form.password, vehicles: form.vehicle.trim() ? [form.vehicle.trim()] : [] });
      localStorage.setItem("parkx_access_token", result.access_token);
      onAuthenticated(await getCurrentUser());
    } catch (requestError) { setError(requestError.message); localStorage.removeItem("parkx_access_token"); }
    finally { setBusy(false); }
  }
  function changeMode() { setError(""); setMode((current) => current === "login" ? "register" : "login"); }
  return <AuthLayout mode={mode} onModeChange={changeMode}>
    <div className="form-heading"><p className="eyebrow">{mode === "login" ? "WELCOME BACK" : "START WITH A BETTER ARRIVAL"}</p><h2>{mode === "login" ? "Sign in to Parkx" : "Create your account"}</h2><p className="form-intro">{mode === "login" ? "Your next spot is closer than you think." : "A few details and you're ready to find your spot."}</p></div>
    <form className="auth-form" onSubmit={handleSubmit}>
      {mode === "register" && <><Field label="Full name" name="name" value={form.name} onChange={updateField} placeholder="e.g. Rahul Sharma" autoComplete="name" /><div className="field-row"><Field label="Email address" name="email" type="email" value={form.email} onChange={updateField} placeholder="you@example.com" autoComplete="email" /><Field label="Phone number" name="phone" type="tel" value={form.phone} onChange={updateField} placeholder="10-digit number" autoComplete="tel" /></div><Field label="Vehicle number" name="vehicle" value={form.vehicle} onChange={updateField} placeholder="Optional · e.g. KA01AB1234" autoComplete="off" required={false} /></>}
      {mode === "login" && <Field label="Email address" name="email" type="email" value={form.email} onChange={updateField} placeholder="you@example.com" autoComplete="email" />}
      <Field label="Password" name="password" type="password" value={form.password} onChange={updateField} placeholder={mode === "register" ? "At least 8 characters" : "Enter your password"} autoComplete={mode === "login" ? "current-password" : "new-password"} />
      {error && <div className="form-error" role="alert"><span className="form-error__icon">!</span>{error}</div>}
      <button className="button button--primary button--full" type="submit" disabled={busy}>{busy ? <><span className="spinner" /> {mode === "login" ? "Signing in…" : "Creating account…"}</> : <>{mode === "login" ? "Sign in" : "Create account"}<span aria-hidden="true">↗</span></>}</button>
    </form>
    <div className="form-note"><span className="form-note__lock">◇</span><span>Your account details are protected and only used to manage your bookings.</span></div>
  </AuthLayout>;
}
