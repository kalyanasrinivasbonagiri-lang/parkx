import { useCallback, useEffect, useRef, useState } from "react";
import AppShell from "../components/AppShell.jsx";
import { PageHeading, Notice, StatusPill, formatDate, money, useLiveRefresh, useNotice } from "../components/ui.jsx";
import { blockSlot, getStaffLots, getStaffSlots, getStaffToday, scanBooking, unblockSlot } from "../api.js";

function CameraScanner({ onRead, onError }) {
  const videoRef = useRef(null);
  const readRef = useRef(onRead); const errorRef = useRef(onError);
  readRef.current = onRead; errorRef.current = onError;
  useEffect(() => {
    let stream;
    let scanTimer;
    let active = true;
    async function startCamera() {
      const BarcodeDetectorApi = window.BarcodeDetector;
      if (!BarcodeDetectorApi) {
        errorRef.current("QR camera scanning is not supported by this browser. Enter the booking code below.");
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia) {
        errorRef.current("Camera access is unavailable. Enter the booking code below.");
        return;
      }
      try {
        const formats = await BarcodeDetectorApi.getSupportedFormats();
        if (!formats.includes("qr_code")) throw new Error("QR camera scanning is not supported by this browser.");
        const detector = new BarcodeDetectorApi({ formats: ["qr_code"] });
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" } }, audio: false });
        if (!active) { stream.getTracks().forEach((track) => track.stop()); return; }
        const video = videoRef.current;
        if (!video) return;
        video.srcObject = stream;
        await video.play();
        scanTimer = window.setInterval(async () => {
          if (!active || video.readyState < 2) return;
          try {
            const codes = await detector.detect(video);
            if (codes.length && codes[0].rawValue) {
              active = false;
              readRef.current(codes[0].rawValue);
            }
          } catch { /* Wait for the next video frame if a frame cannot be read. */ }
        }, 300);
      } catch (error) {
        errorRef.current(error.name === "NotAllowedError"
          ? "Allow camera access in your browser, or enter the booking code below."
          : error.message || "Could not start the camera. Enter the booking code below.");
      }
    }
    startCamera();
    return () => {
      active = false;
      if (scanTimer) window.clearInterval(scanTimer);
      if (stream) stream.getTracks().forEach((track) => track.stop());
    };
  }, []);
  return <div className="qr-reader"><video ref={videoRef} muted autoPlay playsInline aria-label="QR code camera preview" /></div>;
}

export default function StaffDashboard({ user, onSignOut }) {
  const [view, setView] = useState("gate"); const [lots, setLots] = useState([]); const [areaLotId, setAreaLotId] = useState(""); const [bookings, setBookings] = useState([]); const [slots, setSlots] = useState([]); const [slotFilter, setSlotFilter] = useState("ALL"); const [result, setResult] = useState(null); const [scanValue, setScanValue] = useState(""); const [code, setCode] = useState(""); const [busy, setBusy] = useState(false); const [scannerOpen, setScannerOpen] = useState(false); const { notice, setNotice } = useNotice();
  const refresh = useCallback(async () => {
    try {
      if (view === "slots") {
        if (!areaLotId) { setSlots([]); return; }
        const data = await getStaffSlots(areaLotId);
        setSlots(data.slots);
      } else {
        if (!areaLotId) { setBookings([]); return; }
        const data = await getStaffToday(areaLotId);
        setBookings(data.bookings);
      }
    } catch (error) { setNotice({ kind: "error", message: error.message }); }
  }, [view, areaLotId, setNotice]);
  const live = useLiveRefresh(refresh);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    getStaffLots().then((data) => {
      setLots(data.lots);
      setAreaLotId((current) => current || data.lots[0]?.id || "");
    }).catch((error) => setNotice({ kind: "error", message: error.message }));
  }, [setNotice]);
  const slotStatuses = ["FREE", "BOOKED", "OCCUPIED", "HELD", "BLOCKED"];
  const slotCounts = Object.fromEntries(slotStatuses.map((status) => [status, slots.filter((slot) => slot.status === status).length]));
  const visibleSlots = slotFilter === "ALL" ? slots : slots.filter((slot) => slot.status === slotFilter);
  const areaPicker = <label className="staff-area-picker"><span>AREA FILTER</span><select className="field__input" value={areaLotId} onChange={(event) => { setAreaLotId(event.target.value); setResult(null); setScanValue(""); }}><option value="">Choose a lot</option>{lots.map((lot) => <option key={lot.id} value={lot.id}>{lot.name} · {lot.city}</option>)}</select></label>;
  const headingActions = <div className="staff-heading-actions">{areaPicker}<span className={`connection-badge ${live ? "is-live" : ""}`}><i />{live ? "Live updates" : "Connecting"}</span></div>;
  async function doScan(value, action = "LOOKUP") {
    const input = value.trim(); if (!input) return setNotice({ kind: "error", message: "Scan a QR or enter a booking code first." });
    setScanValue(input);
    setBusy(true); setResult(null); setScannerOpen(false);
    try { const scanData = input.startsWith("{") || input.split(".").length === 3 ? { action, qr_token: input } : { action, booking_code: input }; const data = await scanBooking(scanData); setResult(data); if (data.valid) setNotice({ kind: "success", message: action === "ENTRY" ? "Entry confirmed. Parking timer started." : action === "EXIT" ? "Exit recorded." : "Booking verified." }); else setNotice({ kind: "error", message: `Scan result: ${data.result.replaceAll("_", " ").toLowerCase()}.` }); await refresh(); }
    catch (error) { setNotice({ kind: "error", message: error.message }); } finally { setBusy(false); }
  }
  async function act(action) { if (!result?.booking) return; await doScan(scanValue || result.booking.booking_code, action); }
  async function toggleSlot(slot) { setBusy(true); try { if (slot.physical_state === "BLOCKED") await unblockSlot(slot.id); else await blockSlot(slot.id); setNotice({ kind: "success", message: `Slot ${slot.slot_number} updated.` }); const data = await getStaffSlots(areaLotId); setSlots(data.slots); } catch (error) { setNotice({ kind: "error", message: error.message }); } finally { setBusy(false); } }
  const content = view === "slots" ? <><PageHeading eyebrow="LOT OPERATIONS" title="Slot status" description="Choose your current work area to view its spaces. Booking conflicts are checked before a space can be blocked." action={headingActions} /><div className="panel table-panel"><div className="table-head"><div><h2>Spaces for this hour</h2><p>Filter by current reservation and physical status.</p></div><button className="button button--secondary button--small" onClick={refresh}>Refresh</button></div><div className="slot-filter-row">{[["ALL", "All spaces", slots.length], ["FREE", "Free", slotCounts.FREE], ["BOOKED", "Booked", slotCounts.BOOKED], ["OCCUPIED", "Occupied", slotCounts.OCCUPIED], ["HELD", "Held", slotCounts.HELD], ["BLOCKED", "Blocked", slotCounts.BLOCKED]].map(([key, label, count]) => <button key={key} className={`slot-filter ${slotFilter === key ? "is-active" : ""}`} onClick={() => setSlotFilter(key)}><span>{label}</span><strong>{count}</strong></button>)}</div>{visibleSlots.length ? <div className="table-scroll"><table><thead><tr><th>SPACE</th><th>TYPE</th><th>PHYSICAL STATE</th><th>ACTION</th></tr></thead><tbody>{visibleSlots.map((slot) => <tr key={slot.id}><td><strong>{slot.slot_number}</strong></td><td>{slot.slot_type}</td><td><StatusPill status={slot.status} /></td><td><button className="button button--secondary button--small" disabled={busy || ["BOOKED", "HELD", "OCCUPIED"].includes(slot.status)} onClick={() => toggleSlot(slot)}>{slot.physical_state === "BLOCKED" ? "Unblock" : "Block for maintenance"}</button></td></tr>)}</tbody></table></div> : <div className="empty-inline">No spaces match this filter for the current hour.</div>}</div></> : <><PageHeading eyebrow="EMPLOYEE GATE DESK" title={`Welcome, ${user.name.split(" ")[0]}`} description="Scan a driver's QR pass or look up their booking code to manage arrival and departure." action={headingActions} /><div className="staff-layout"><div className="panel scan-panel"><div className="scan-panel__heading"><div className="scan-icon">▦</div><div><p className="eyebrow">GATE VERIFICATION</p><h2>Scan a booking pass</h2></div></div><p className="panel-copy">Scan any booking QR or enter its code. Scanning is not limited by this area filter.</p>{scannerOpen ? <CameraScanner onRead={(text) => doScan(text)} onError={(message) => setNotice({ kind: "error", message })} /> : <button className="camera-start" onClick={() => setScannerOpen(true)}><span>▦</span><strong>Open QR scanner</strong><small>Camera access required</small></button>}<div className="or-divider"><span>OR ENTER BOOKING CODE</span></div><form className="code-form" onSubmit={(event) => { event.preventDefault(); doScan(code); }}><input className="field__input" value={code} onChange={(event) => setCode(event.target.value.toUpperCase())} placeholder="e.g. PX-4J82KQ" /><button className="button button--primary" disabled={busy}>Look up</button></form>{result && <div className={`scan-result ${result.valid ? "is-valid" : "is-invalid"}`}><div className="scan-result__top"><div><small>SCAN RESULT</small><h3>{result.result.replaceAll("_", " ")}</h3></div>{result.booking && <StatusPill status={result.booking.status} />}</div>{result.booking && <><div className="scan-driver"><div className="profile-avatar">{result.booking.driver_name?.[0]?.toUpperCase()}</div><div><strong>{result.booking.driver_name}</strong><span>{result.booking.vehicle_number} · Space {result.booking.slot_number}</span></div></div><div className="scan-details"><span><small>ARRIVAL</small>{formatDate(result.booking.start_at)}</span><span><small>DEPARTURE</small>{formatDate(result.booking.end_at)}</span></div>{result.booking.timer_running && <div className="timer-banner">◷ Parking timer is running · {Math.floor((result.booking.elapsed_seconds || 0) / 60)} minutes</div>}{result.booking.status === "CONFIRMED" && result.booking.entry_allowed === false && <div className="timer-banner">QR verified. {new Date(result.booking.entry_closes_at) > Date.now() ? `Entry opens at ${formatDate(result.booking.entry_opens_at)}.` : "The entry window has passed."}</div>}<div className="scan-actions">{result.booking.status === "CONFIRMED" && result.booking.entry_allowed && <button className="button button--primary" onClick={() => act("ENTRY")} disabled={busy}>Confirm entry</button>}{result.booking.status === "CHECKED_IN" && <button className="button button--primary" onClick={() => act("EXIT")} disabled={busy}>Record exit</button>}{result.booking.driver_phone && <a className="button button--secondary" href={`tel:${result.booking.driver_phone}`}>Call driver</a>}</div></>}</div>}</div><aside className="staff-side"><div className="stat-card stat-card--dark"><div className="stat-card__icon">◷</div><p>TODAY'S BOOKINGS</p><strong>{bookings.length}</strong><span>at the selected lot</span></div><div className="panel gate-note"><p className="eyebrow">QR VERIFICATION</p><h3>Scan as soon as it is ready</h3><p>Any staff account can verify a booking from any lot. The area filter only changes the arrivals and slot lists.</p></div></aside></div><div className="panel table-panel staff-bookings"><div className="table-head"><div><h2>Today's arrivals</h2><p>Bookings at the selected lot, sorted by start time.</p></div><button className="button button--secondary button--small" onClick={refresh}>Refresh</button></div>{bookings.length ? <div className="table-scroll"><table><thead><tr><th>BOOKING</th><th>DRIVER</th><th>VEHICLE / SPACE</th><th>TIME</th><th>STATUS</th></tr></thead><tbody>{bookings.map((booking) => <tr key={booking.booking_id}><td><strong>{booking.booking_code}</strong></td><td>{booking.driver_name}</td><td>{booking.vehicle_number} · {booking.slot_number}</td><td>{formatDate(booking.start_at)}</td><td><StatusPill status={booking.status} /></td></tr>)}</tbody></table></div> : <div className="empty-inline">No bookings are scheduled for today.</div>}</div></>;
  return <AppShell user={user} onSignOut={onSignOut} active={view} onNavigate={setView}><Notice message={notice?.message} kind={notice?.kind} onClose={() => setNotice(null)} />{content}</AppShell>;
}
