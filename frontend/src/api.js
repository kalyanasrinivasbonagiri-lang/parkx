const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
        ...(options.auth === false || !localStorage.getItem("parkx_access_token")
          ? {}
          : { Authorization: `Bearer ${localStorage.getItem("parkx_access_token")}` }),
      },
    });
  } catch {
    throw new Error("Can't reach the server. Check that the backend is running.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).join(" ")
      : payload.detail;
    throw new Error(message || "Something went wrong. Please try again.");
  }
  return payload;
}

export const signIn = (email, password) => request("/api/auth/login", {
  method: "POST", auth: false, body: JSON.stringify({ email, password }),
});
export const signUp = (form) => request("/api/auth/register", {
  method: "POST", auth: false, body: JSON.stringify(form),
});
export const getCurrentUser = () => request("/api/auth/me");

export const getCities = () => request("/api/driver/cities");
export const getLots = (city, startAt, hours) => request(`/api/driver/cities/${encodeURIComponent(city)}/lots?start_at=${encodeURIComponent(startAt)}&hours=${hours}`);
export const getSlots = (lotId, startAt, hours, type) => request(`/api/driver/lots/${lotId}/slots?start_at=${encodeURIComponent(startAt)}&hours=${hours}${type ? `&slot_type=${type}` : ""}`);
export const createBooking = (data) => request("/api/bookings", { method: "POST", body: JSON.stringify(data) });
export const payBooking = (id, method, action) => request(`/api/bookings/${id}/payment`, { method: "POST", body: JSON.stringify({ method, action }) });
export const getMyBookings = () => request("/api/bookings/mine");
export const cancelBooking = (id) => request(`/api/bookings/${id}/cancel`, { method: "POST" });
export const extendBooking = (id, hours) => request(`/api/bookings/${id}/extensions`, { method: "POST", body: JSON.stringify({ hours }) });
export const payExtension = (bookingId, extensionId, method, action) => request(`/api/bookings/${bookingId}/extensions/${extensionId}/payment`, { method: "POST", body: JSON.stringify({ method, action }) });
export const payOverstay = (id, method, action) => request(`/api/bookings/${id}/overstay-payment`, { method: "POST", body: JSON.stringify({ method, action }) });
export const getReminders = () => request("/api/bookings/reminders");

export const getStaffToday = () => request("/api/staff/bookings/today");
export const scanBooking = (data) => request("/api/staff/scan", { method: "POST", body: JSON.stringify(data) });
export const blockSlot = (id) => request(`/api/staff/slots/${id}/block`, { method: "POST" });
export const unblockSlot = (id) => request(`/api/staff/slots/${id}/unblock`, { method: "POST" });
export const getStaffSlots = () => request("/api/staff/slots");

export const getAdminLots = () => request("/api/admin/lots");
export const getEmployees = () => request("/api/admin/employees");
export const createLot = (data) => request("/api/admin/lots", { method: "POST", body: JSON.stringify(data) });
export const createSlots = (lotId, slots) => request(`/api/admin/lots/${lotId}/slots/bulk`, { method: "POST", body: JSON.stringify({ slots }) });
export const createEmployee = (data) => request("/api/admin/employees", { method: "POST", body: JSON.stringify(data) });
export const getOccupancy = () => request("/api/admin/dashboard/occupancy");
export const getReport = (name) => request(`/api/admin/reports/${name}`);
export const API_WS_URL = API_BASE_URL.replace(/^http/, "ws") + "/ws/live";
