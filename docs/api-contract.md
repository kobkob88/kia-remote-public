# Car service API contract (v1)

The Python service (`server/`) serves the dashboard (`web/`) and this JSON API on
`http://127.0.0.1:8765` (listening on this PC only). Phones reach it at `https://car.example.com`
through a Cloudflare Tunnel behind Cloudflare Access. All bodies are JSON. Errors: HTTP 4xx/5xx
with `{"error": "plain sentence"}`, sometimes with more fields (`"needs"`, see unlock).

Start: `.venv\Scripts\python.exe -m server` (real car) or `.venv\Scripts\python.exe -m server --demo`
(fake car, no Kia calls, every command succeeds after ~3 s, state changes accordingly).

## Secrets and files
- Kia email, password, PIN and the Kia token live ONLY in Windows Credential Manager via `keyring`
  (service name `kia-remote`). Never in a file, never logged, never returned by the API.
- Presets, the command log and the Kia activity log live in `%LOCALAPPDATA%\kia-remote\` (JSON), not in the repo.
- Passkeys for unlock (public keys, sign counts, labels; not secrets) are in `passkeys.json` there
  (`passkeys-demo.json` in demo mode, so a demo enrolment never unlocks the real car).
- `settings.json` there holds settings (not secrets): the two Cloudflare Access values (below),
  `"region"` (which Kia Connect region, default `"canada"`; see PORTING.md) and `"app_name"` (the
  name under the home-screen icon; `manifest.webmanifest` is served with it).

## Cloudflare Access
A request that came through the tunnel carries Cloudflare's headers (`Cf-Connecting-IP` or
`Cf-Ray`). cloudflared connects from localhost too, so those headers are what tells it apart from
a browser on this PC. When `settings.json` has `access_team_domain` and `access_aud`, every tunnel
request (pages and API) must carry a valid `Cf-Access-Jwt-Assertion` JWT: RS256, signed by a key
from `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cached an hour; an unknown key
id refetches at most once a minute), `aud` containing `access_aud`, `iss` the team URL, not
expired (60 s leeway). Otherwise HTTP 403 `{"error": "Sign in through Cloudflare Access first.", "needs": "access"}`
(503 if Cloudflare's keys can't be read and none are cached). A browser on this PC (no Cloudflare
headers) needs no JWT. Without the two settings every tunnel request gets 403 (fails closed) and the service logs a
loud warning at startup; a browser on this PC still works.

## Approved devices
A browser that comes through the tunnel must also be approved (see docs/SECURITY.md). Until it
is, `GET /api/state` answers `{"demo": ..., "auth": "needs_device"}` and every other endpoint answers
HTTP 403 `{"error": ..., "needs": "device"}`. The exceptions are `/api/device/approve` and
`/api/diag`, plus `/api/login`, `/api/otp/*` and `/api/logout` while the PC isn't signed in to
Kia. Requests from this PC itself are never asked.

### POST /api/device/approve `{"email", "password", "pin", "label": "iPhone"}` -> state
Checked against the Kia account saved on the PC (not sent to Kia). Success sets the `car_device`
cookie (HttpOnly, Secure, SameSite=Strict, 400 days). Wrong: 403 `needs: "device"`. 5 wrong in 15
minutes: 429 for 15 minutes. PC signed out of Kia: 409. `POST /api/login` from an unapproved
browser also approves it once Kia accepts the password (it takes `"label"` too).
### GET /api/devices -> `{"devices": [ {"id", "label", "who", "via", "created_at", "last_used_at", "this_device"} ]}`
### DELETE /api/devices/{id} -> the same list (404 if there's no such device)

## GET /api/state
```json
{
  "demo": false,
  "auth": "ok",                 // "ok" | "needs_login" | "needs_otp" | "error"
  "auth_error": null,           // plain sentence when auth == "error"
  "otp": null,                  // when needs_otp: {"has_email": true, "has_sms": false}
  "vehicle": {                  // null unless auth == "ok"
    "name": "SPORTAGE", "model": "SPORTAGE", "year": 2026,
    "updated_at": "2026-11-15T22:06:23-05:00",   // when the CAR last reported (ISO, local offset)
    "locked": true,
    "doors":   {"front_left": {"open": false, "locked": true}, "front_right": {...},
                "rear_left": {...}, "rear_right": {...}},
    "trunk_open": false, "hood_open": false, "sunroof_open": false,
    "windows": {"front_left": false, "front_right": false, "rear_left": false, "rear_right": false},
    "engine_on": false,
    "fuel_percent": 100, "range_km": 640, "odometer_km": 1234,
    "battery_12v_percent": 97,
    "climate": {"on": false, "set_temp_c": 21, "defrost": false,
                "steering_heat": false, "rear_window_heat": false, "mirror_heat": false},
    "seats": {"front_left": "Off", "front_right": "Off", "rear_left": "Off", "rear_right": "Off"},
    "outside_temp_c": -3,
    "tires": {"warning": false, "front_left": false, "front_right": false,
              "rear_left": false, "rear_right": false},
    "warnings": {"washer_fluid": false, "brake_fluid": false, "oil": false,
                 "smart_key_battery": false, "aux_battery": false},
    "location": {"lat": 45.0, "lon": -75.0, "updated_at": "..."},   // or null
    "next_service_km": 8000
  },
  "pending": null,              // the command in flight, same shape as a log entry, or null
  "dial": {"min": 17, "max": 27}  // the car's temperature dial (C) for this region; HI/LO are the ends
}
```
Any field the car doesn't report is `null`; the dashboard must cope with nulls everywhere.
`climate.set_temp_c` can also be the string `"HI"` or `"LO"` when the car's dial is at either end
(Kia reports it that way). When Kia's last report says climate is on but is older than the end of the last successful
climate start's timer (`finished_at` + `duration_min`) or a successful stop, the service reports
`climate.on: false` (and `engine_on: false`) with `climate.ended_at` (ISO): the car shuts off on
its own and often doesn't report it. `outside_temp_c` is always `null` on Kia Connect Canada, which doesn't
send it; the dashboard hides that tile.

## POST /api/login   `{"email": "...", "password": "...", "pin": "1234"}`
Saves to Credential Manager, logs in. Returns the new state (may be `needs_otp`).
## POST /api/otp/send   `{"method": "EMAIL" | "SMS"}` -> `{"sent": true}`
## POST /api/otp/verify `{"code": "123456"}` -> state
## POST /api/logout -> clears saved credentials and token, returns state (`needs_login`).

## POST /api/refresh   `{"force": false}`
`force: false` reads Kia's cached last-known state (does not wake the car; cheap).
`force: true` wakes the car for fresh data (slow ~30 s, costs 12V battery: the service refuses
more than one forced refresh per 10 minutes with HTTP 429). Returns state.

## POST /api/command
```json
{"action": "lock" | "unlock" | "climate_start" | "climate_stop" | "hazard" | "horn_lights",
 "options": {                      // climate_start only, all optional
   "set_temp_c": 22,               // 17-27 in 0.5 steps (the car's dial). Kia's table goes 14-31.5,
                                   // but past the dial the car falls back to 22. "HI"/"LO" are
                                   // accepted and sent as 27/17: the word itself makes the car
                                   // start with climate off.
   "duration_min": 10,             // 1-10
   "defrost": true, "heating": 1,  // 0 off, 1 steering wheel + rear window + mirrors,
                                   // 2 rear window + mirrors, 3 steering wheel only
   "front_left_seat": 8, "front_right_seat": 0,   // 0 off, 6/7/8 heat low/med/high, 3/4/5 cool
   "rear_left_seat": 0, "rear_right_seat": 0 }}
```
Returns `{"id": "cmd_...", "status": "pending"}` immediately; the service tracks it in the background
(Kia can stay pending ~2 min). One command at a time: HTTP 409 if one is pending.

**Unlock needs a second factor** (lock, climate, hazard and horn don't): the body also carries
either `"assertion": {...}` (a WebAuthn assertion, below) or `"pin": "1234"` (the saved Kia PIN,
compared in constant time, never echoed or logged). Missing or wrong: HTTP 403
`{"error": ..., "needs": "verification"}`. 5 wrong PINs within 15 minutes close the PIN path for
15 minutes: HTTP 429 `{"error": ..., "needs": "verification"}` (passkeys still work). A busy car
(409) or a signed-out service (401) answers before the PIN is checked, so those don't cost a try.
Demo mode: the PIN is `1234`, passkeys work the same way.
## GET /api/command/{id}
`{"id", "action", "options", "status": "pending"|"success"|"failed"|"timeout"|"error",
  "message": null | "plain sentence", "sent_at": ISO, "finished_at": ISO|null,
  "who": "local" | "<Access email>" | null,     // null: tunnel request with Access not set up
  "via": null | "pin" | "passkey:<label>"}`      // how an unlock was verified
After success the service does a cheap (non-forced) refresh so /api/state reflects it.

## GET /api/log -> `{"entries": [ ...last 50 commands, newest first... ]}`
`command-log.json` keeps the last 500.

## GET /api/kia-calls -> `{"entries": [ ...every call to Kia, newest first... ]}`
The Kia activity log (`kia-calls.json`, last 2,000 calls; dots menu > Kia activity). One entry per
service call that actually asked Kia for something (a cached answer that didn't isn't logged):
`{"at": ISO, "why": "startup"|"sign_in"|"code_send"|"code_check"|"app_open"|"refresh"|"wake"|
"command"|"check"|"after_command"|"car_info", "action": null|"lock"|..., "who": null|"<email>"|
"schedule HH:MM", "ok": bool, "ms": int, "reached_car": bool, "steps": [...],
"endpoints": ["lstvhclsts", ...], "message": null|"why it failed"}`.
`reached_car` is true for commands, a wake (forced refresh) and any request to an endpoint in the
region's `car_endpoints` (Canada: the remote commands, `rltmvhclsts` and `fndmcr`, Find My Car,
which the library calls on a normal read only after the car moved or once per run when no
location is known). Everything else is answered from Kia's servers and doesn't touch the car.
`app_open` reads happen only while a dashboard is open and polling (at most every 2 minutes);
nothing reaches Kia while no dashboard is open, except schedules. Each call is also a
`Kia call:` line in `server.log`.

## GET /api/car-photo -> `image/png`
Kia Connect Canada's own picture of this car, as kiaconnect.ca shows it:
`https://kiaconnect.ca/cms/{first 4 of modelCode}/{modelYear}/{exteriorColor}/CWP/cwp_45degree.png`
(transparent, 1280 x 567). The codes come from Kia's vehicle list, read once per sign-in. The PC
downloads it once and keeps it as `car-photo-<hash>.png` in the data folder; the response may be
cached a week. 404 when signed out, in demo mode, or when Kia has no picture (then not asked again
for 10 minutes). The dashboard shows it instead of the drawing unless a door, window, hood, trunk
or sunroof is open or a tire warns, which only the drawing can show.

## GET /api/car-spin -> `{"frames": 36}`
The car's 360-degree turntable, when the region has one (Canada: kia.ca's Build & Price "Exterior
360", matched by model, year and paint code; the nearest year when the car's own year isn't
listed). The first call downloads every frame once into `car-spin-<hash>/` in the data folder
(kia.ca's lineup is indexed in `kia-ca-360.json`, refreshed monthly). `{"frames": 0}` when there's
no turntable for this car, when signed out, or in demo mode.

## GET /api/car-spin/{n} -> `image/webp` (or `image/png`)
Frame `n` (1-based). Frame 1 is the front three-quarter view of the driver's side; frames are 10
degrees apart (5 front, 13 passenger side, 23 rear, 31 driver's side). May be cached a week; 404
for a frame that doesn't exist. The dashboard turns the car to the side that's open (one side,
trunk or hood), and shows the drawing when more than one of those, a window, the sunroof or a tire
needs showing.

## GET /api/health -> `{"report": {...} | null}`
Kia's monthly vehicle health report (Kia Connect makes one a month; the owner picks the day in the
Kia app). The library already downloads it during a normal status read when a new one appears; the
service keeps Kia's answer in `health-report.json` in the data folder (deleted on sign-out) and
`server/health.py` turns it into:

```json
{"date": "2026-10-25", "odometer_km": 5022,
 "systems": [{"group": "chassis", "name": "Electronic Parking Brake", "code": "EPB", "ok": true}],
 "driving": {"starts": 120, "engine_on_s": 118320, "drive_s": 84600, "idle_s": 33720,
             "distance_km": 980}}
```

`ok` is false for anything Kia didn't mark as a pass, null when Kia gave no result. A value the
service can't read (an unknown unit) is null rather than guessed. `null` report: none yet. 401
when signed out. No call to Kia, no car wake.

## Passkeys (fingerprint / Face ID for unlock)
WebAuthn with the phone's own authenticator (`authenticatorAttachment: "platform"`,
`userVerification: "required"`). The RP ID is the host name the browser used (`car.example.com`, or
`localhost` on this PC; origin `https://<host>`, or `http://localhost:<port>`). An IP address
can't be an RP ID, so on `127.0.0.1` these answer 400 pointing to `http://localhost:8765` (the PIN
still works there). Option and credential JSON use base64url for every byte field, as
`PublicKeyCredential.toJSON()` does. Challenges are single-use and expire after 2 minutes.
### POST /api/unlock/challenge -> PublicKeyCredentialRequestOptions JSON
`{"challenge", "rpId", "timeout", "userVerification": "required", "allowCredentials": [{"id", "type"}]}`;
404 `{"needs": "pin"}` when no passkey is registered for this host. Sign it with
`navigator.credentials.get`, then send `POST /api/command {"action": "unlock", "assertion": {...}}`.
A used or expired challenge, a key nobody registered, another origin, no user verification or a
bad signature: 403 `{"needs": "verification"}`.
### POST /api/passkeys/register/options `{"label": "iPhone"}` -> PublicKeyCredentialCreationOptions JSON
### POST /api/passkeys/register `{"credential": {...}, "pin": "1234"}`
Enrolling needs the Kia PIN (same lockout as unlock), so a Cloudflare Access session alone can't
add a fingerprint. Returns `{"id", "label", "rp_id", "created_at", "last_used_at"}`.
### GET /api/passkeys -> `{"passkeys": [ {"id", "label", "rp_id", "created_at", "last_used_at"} ]}`
### DELETE /api/passkeys/{id} -> the same list (404 if there's no such passkey)

## GET /api/presets -> `{"presets": [ {"id", "name", "icon", "options": {...climate options...}} ]}`
## PUT /api/presets  `{"presets": [...]}` -> same. Defaults on first run:
- "Winter morning": 22 C, defrost, heating 1, driver seat high heat, 10 min
- "Quick warm": 21 C, 5 min
- "Summer cool": 17 C, driver seat low cool, 10 min

## GET /api/schedules -> `{"schedules": [...], "next": {"id", "at"} | null}`
## PUT /api/schedules `{"schedules": [...]}` -> same
```jsonc
{"id": "s_abc", "enabled": true, "time": "07:45",          // 24-hour, the PC's local time
 "days": ["mon", "tue", "wed", "thu", "fri"],              // at least one; returned in week order
 "preset_id": "winter-morning"}                            // must be an existing preset
```
The service starts climate with that preset's options when a schedule is due (log entry
`who: "schedule 07:45"`, `via: "preset:<name>"`). It only runs while the PC and service are up;
a start missed by more than 5 minutes is skipped, not run late. If another command is running it
retries for 3 minutes, then logs an error entry. `next.at` is ISO with the local offset. A preset
a schedule uses can't be deleted (PUT /api/presets answers 409).
