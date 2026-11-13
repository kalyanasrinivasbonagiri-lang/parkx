# Parkx — Smart Parking Slot Booking System

Parkx is a course project for booking parking spaces by city, parking lot, and time. Drivers select a space and vehicle, pay through a simulated payment flow, and receive a QR booking pass. Lot staff verify passes at the gate and record entry and exit. Administrators manage lots, spaces, staff accounts, occupancy, and reports.

## Roles

| Role | Main tasks |
| --- | --- |
| Driver | Browse cities and lots, choose an available space, make a simulated payment, view the QR pass, manage bookings |
| Staff | Look up a booking or scan its QR, confirm entry and exit, view current slot statuses, block or unblock a space for maintenance |
| Admin | Create lots and space grids, create staff accounts, monitor occupancy, review reports |

## Technology

- **Backend:** Python, FastAPI, PyMongo
- **Database:** MongoDB Atlas replica set
- **Frontend:** React and Vite
- **Live updates:** MongoDB change streams and WebSocket
- **Authentication:** JWT bearer tokens with `admin`, `staff`, and `driver` roles

MongoDB is used for transactions, unique reservation indexes, a TTL index for payment holds, geospatial and city indexes, a time-series occupancy collection, and aggregation reports.

## Requirements

- Python 3.11 or later
- `uv`
- Node.js and npm
- MongoDB Atlas replica-set URI with network access configured

Atlas transactions and change streams require a replica set. A MongoDB Atlas cluster provides this configuration.

## Configure and run the backend

Open a PowerShell terminal in the project folder:

```powershell
Set-Location D:\sem5\nosql\parkx\backend
uv venv
\.venv\Scripts\Activate.ps1
uv pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `backend/.env` and set:

```dotenv
MONGODB_URI=mongodb+srv://<username>:<password>@<cluster-host>/?retryWrites=true&w=majority
MONGODB_DATABASE=smart_parking
JWT_SECRET_KEY=<a-long-random-secret>
```

Replace all placeholders. If the database password contains special URL characters, percent-encode them in the URI. Keep `.env` private; it is ignored by Git.

Start the API from the `backend` directory:

```powershell
uv run uvicorn app.main:app --reload
```

Useful URLs:

- Health check: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

The API prepares required indexes and the occupancy time-series collection on startup.

## Configure and run the frontend

Open a second PowerShell terminal:

```powershell
Set-Location D:\sem5\nosql\parkx\frontend
npm install
npm run dev
```

Open the local URL Vite prints, usually <http://localhost:5173>. Keep both terminals running. The frontend uses `http://127.0.0.1:8000` for the API by default. To use a different API URL, create `frontend/.env.local` with:

```dotenv
VITE_API_BASE_URL=http://127.0.0.1:8000
```

### Test on a phone over Wi-Fi

Connect the phone and development computer to the same Wi-Fi network. Find the computer's IPv4 address with `ipconfig`, then:

1. In `backend/.env`, set `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://<computer-ip>:5173`.
2. In `frontend/.env.local`, set `VITE_API_BASE_URL=http://<computer-ip>:8000`.
3. Start FastAPI with `uv run uvicorn app.main:app --reload --host 0.0.0.0` from `backend` and Vite with `npm run dev` from `frontend`.
4. Open `http://<computer-ip>:5173` on the phone. Allow local network access through Windows Firewall if prompted.

Vite listens on the local network for this development workflow. The production build uses the configured API URL, or the same origin when deployed behind a reverse proxy.

## Demo data and accounts

With the backend environment configured, run the seed script from the `backend` directory:

```powershell
uv run python -m scripts.seed
```

The seed script creates repeatable demo lots, spaces, users, bookings, and occupancy records. Seeded accounts use this demo password:

```text
ParkxDemo123!
```

| Role | Demo email |
| --- | --- |
| Admin | `admin@parkx.demo` |
| Staff | `meena@parkx.demo` |
| Staff | `arjun@parkx.demo` |
| Driver | `rahul@parkx.demo` |
| Driver | `priya@parkx.demo` |
| Driver | `amit@parkx.demo` |

Only admins can create staff accounts in the application. Public sign-up creates driver accounts only.

## Typical demo flow

1. Sign in as a driver and choose a city, lot, arrival date and time, duration, vehicle, and free space.
2. Complete the simulated payment. The demo includes a failed-payment option; no external payment gateway is used.
3. Open **My bookings** and show the QR booking pass or booking code.
4. Sign in as staff for the same lot and look up the booking or scan its QR. Confirm entry during the allowed entry window, then record exit.
5. Sign in as admin to create lots and staff, inspect current occupancy, and view reports.

## Deployment

For production service settings, environment variables, CORS, WebSockets, and a pre-publish checklist, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Repository layout

```text
backend/
  app/
    main.py
    config.py
    db.py
    auth.py
    routers/
    services/
    jobs/
    realtime/
  scripts/
    seed.py
  tests/
frontend/
  src/
    pages/
    components/
```

## Notes

- Payment is simulated. The server calculates payment amounts from booking data.
- Booking times can be entered to the minute; duration and price are calculated in whole hours.
- A QR pass can be verified immediately. Gate entry is permitted from 15 minutes before the booking start until 15 minutes after it.
- The employee camera scanner uses browser QR detection when supported. Booking-code lookup is available as a fallback.
- Do not commit `.env`, database credentials, JWT secrets, `.venv`, `node_modules`, or build output.
