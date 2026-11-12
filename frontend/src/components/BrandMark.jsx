export default function BrandMark({ compact = false }) {
  return (
    <a className={`brand${compact ? " brand--compact" : ""}`} href="#home" aria-label="Parkx home">
      <span className="brand__icon" aria-hidden="true">
        <svg viewBox="0 0 40 40" fill="none">
          <rect x="2" y="2" width="36" height="36" rx="12" fill="currentColor" />
          <path d="M14 29V11h8.2c5 0 8 2.6 8 7s-3 7-8 7H19v4h-5Zm5-8h3.1c2 0 3-.9 3-2.8s-1-2.8-3-2.8H19V21Z" fill="#F4F6F0" />
          <path d="M8 31h4" stroke="#C6F36A" strokeWidth="2" strokeLinecap="round" />
        </svg>
      </span>
      <span className="brand__word">park<span>x</span></span>
    </a>
  );
}

