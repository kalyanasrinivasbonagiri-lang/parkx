import BrandMark from "./BrandMark.jsx";

function ParkingIllustration() {
  return (
    <div className="parking-art" aria-hidden="true">
      <div className="parking-art__glow" />
      <div className="parking-art__road">
        <span className="parking-art__dash parking-art__dash--one" />
        <span className="parking-art__dash parking-art__dash--two" />
        <span className="parking-art__dash parking-art__dash--three" />
      </div>
      <div className="parking-art__lot">
        <div className="parking-art__topline"><span>AVAILABLE NOW</span><span className="live-dot" /></div>
        <div className="parking-art__slots">
          <span className="slot slot--free">P</span>
          <span className="slot slot--booked">P</span>
          <span className="slot slot--free">P</span>
          <span className="slot slot--occupied">P</span>
          <span className="slot slot--free">P</span>
          <span className="slot slot--free">P</span>
        </div>
        <div className="parking-art__caption"><span>SMART PARKING</span><span>01 — 06</span></div>
      </div>
      <div className="parking-art__pin"><span className="parking-art__pin-inner">P</span></div>
      <div className="parking-art__circle parking-art__circle--large" />
      <div className="parking-art__circle parking-art__circle--small" />
    </div>
  );
}

export default function AuthLayout({ children, mode, onModeChange }) {
  return (
    <main className="auth-shell">
      <section className="auth-visual">
        <div className="auth-visual__texture" />
        <header className="auth-visual__header">
          <BrandMark />
          <div className="visual-location"><span className="visual-location__dot" /> PARKING, REIMAGINED</div>
        </header>
        <div className="auth-visual__content">
          <p className="eyebrow eyebrow--light">YOUR SPACE, BEFORE YOU ARRIVE</p>
          <h1>Good parking<br />changes <em>everything.</em></h1>
          <p className="auth-visual__copy">Find your place, book by the hour, and arrive knowing it’s yours.</p>
          <ParkingIllustration />
        </div>
        <footer className="auth-visual__footer">
          <span>BUILT FOR THE WAY YOU MOVE</span><span>01 / 03 CITIES</span>
        </footer>
      </section>

      <section className="auth-panel">
        <div className="auth-panel__top">
          <BrandMark compact />
          <div className="auth-switch">
            <span>{mode === "login" ? "New to Parkx?" : "Already have an account?"}</span>
            <button type="button" onClick={onModeChange}>
              {mode === "login" ? "Create account" : "Sign in"}
            </button>
          </div>
        </div>
        <div className="auth-panel__body">{children}</div>
        <footer className="auth-panel__footer">
          <span>© 2026 PARKX</span>
          <span><i /> A smoother arrival starts here</span>
        </footer>
      </section>
    </main>
  );
}

