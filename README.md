# Car Remote

A fast, private remote for your Kia: lock, unlock, climate with seat and steering-wheel heat,
horn and lights, schedules and presets. It installs on your phone's home screen like an app.
Your car shows up as a real 360° picture of your model in your paint, and the picture turns to
show whichever door, the trunk or the hood is open.

<img src="docs/screenshot.png" alt="The dashboard on a phone, in demo mode" width="320">

It runs on **a computer you own**: a Windows PC, a Mac or a Raspberry Pi. That computer talks to
Kia, and your phone reaches it through a Cloudflare Tunnel guarded by Cloudflare Access. No
third-party server ever sees your Kia password.

> **Unofficial, and at your own risk.** This project is not made by, affiliated with or endorsed
> by Kia. It uses the same private API as the official app, and Kia can change or block that at
> any time. **Kia locks your account after a few failed sign-ins.** It is provided "as is",
> without any warranty: read the [Disclaimer](#disclaimer) before you use it.

## Where it works

| Region | Brand | Status |
|---|---|---|
| Canada | Kia | **Tested** on a 2026 Sportage hybrid: every command, the photo and the 360 view |
| Anywhere else | Kia, Hyundai, Genesis | Not yet. The library supports most regions; see [PORTING.md](PORTING.md) to add yours |

| Computer | Status |
|---|---|
| Windows 10 / 11 | **Tested.** Runs at logon; secrets in Windows Credential Manager |
| macOS | Supported, **not yet tested on a real Mac.** Runs at login; secrets in the Keychain |
| Raspberry Pi / Linux | Supported, **not yet tested on a real Pi.** Runs at boot (systemd); secrets in a file only that user can read |

If you run it on a Mac or a Pi, please open an issue saying whether it worked.

Porting to another region is designed to be done with an AI coding assistant: PORTING.md has a
prompt you paste in, plus a safe order for trying things on a real car. Send your region back as
a pull request.

## How it works

```
your phone (home-screen app)
   │  HTTPS
   ▼
Cloudflare Access ── "who are you?" one-time code to an allowed email
   │
Cloudflare Tunnel ── outbound from your computer: no open ports, home IP hidden
   │
your computer: Python service on 127.0.0.1:8765
   │  checks the Access token again, asks a new browser to prove the Kia account,
   │  asks fingerprint/PIN for unlock, keeps the Kia session in the password store
   ▼
Kia Connect (the same API the official app uses)
```

What's in it:

- **Dashboard:** lock and unlock, climate with temperature, defrost, steering-wheel and seat
  heat, horn and lights, flash lights, and a forced refresh (rate-limited, because it wakes the
  car). Fuel, range, 12 V battery, doors, windows, tires, warnings and location.
- **The car itself:** your model in your paint, as a 360° turntable from kia.ca. It turns to the
  open door, the trunk or the hood. Drag it to spin it.
- **Climate presets** ("Winter morning", "Quick warm"…) and **schedules** (e.g. warm the car
  at 7:40 on weekdays). The computer sends schedules, so they run even when your phone is off.
- **Vehicle health:** Kia's monthly report (a check of each system, with engine starts, drive
  and idle time and distance for the month), next to the warning lights the car reports now.
- **Approved devices:** a new phone or browser has to enter the Kia account once, even after
  Cloudflare lets it in. You can see and remove approved devices in the menu.
- **Unlock second factor:** Face ID or fingerprint (passkeys), or the Kia PIN.
- **Activity log:** every command, its result, and who sent it.
- **Works like an app:** installs to the home screen, opens instantly from the last known
  state, and recovers by itself after the phone wakes up.

More detail:

- [docs/SETUP.md](docs/SETUP.md): every setup step for Windows, macOS and Raspberry Pi,
  including Cloudflare.
- [docs/SECURITY.md](docs/SECURITY.md): what protects the car, and what an attacker would need.
- [PORTING.md](PORTING.md): adding your region or brand.
- [docs/api-contract.md](docs/api-contract.md): the JSON API.

## What you need

- A computer that stays on: Windows, a Mac, or a Raspberry Pi (a Pi 4 or 5 with Raspberry Pi OS
  64-bit). It must be on whenever you want to use the app.
- Python 3.12 or newer.
- A Kia Connect account with the car on it, and its 4-digit PIN.
- For phone access away from home: a domain on Cloudflare (the free plan is fine) and Cloudflare
  Zero Trust (free for up to 50 users).

## Quick start

Try it with a fake car first. On Windows:

```powershell
git clone https://github.com/kobkob88/kia-remote-public kia-remote
cd kia-remote
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m server --demo     # http://localhost:8765
```

On a Mac or a Raspberry Pi:

```bash
git clone https://github.com/kobkob88/kia-remote-public kia-remote
cd kia-remote
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m server --demo             # http://localhost:8765
```

Then run it without `--demo` and sign in with your Kia Connect email, password and PIN. Kia sends
a one-time code the first time because it sees a new device. After that the saved session is
reused, so restarts don't ask again. Not sure your account works with this at all? Run
`scripts/check_connection.py` first. It only reads the car's status and saves nothing.

**[docs/SETUP.md](docs/SETUP.md)** takes it from there: Cloudflare Tunnel and Access, approving
your phone, and starting the service automatically on each kind of computer.

## Settings

Everything goes in `settings.json` in the data folder (below). Restart the service after a
change.

| Key | Meaning |
|---|---|
| `access_team_domain`, `access_aud` | Cloudflare Access (see SETUP.md). Until both are set, everything through the tunnel is refused |
| `region` | Which Kia Connect region; default `"canada"`. See PORTING.md |
| `app_name` | The name under the home-screen icon, e.g. `"Sportage"`. Default "Car Remote" |
| `secret_store` | `"keyring"` (the system password store) or `"file"` (`secrets.json`). Default: `"file"` on Linux, `"keyring"` elsewhere |

## Where things live

| | Data folder | Kia email, password, PIN and session |
|---|---|---|
| Windows | `%LOCALAPPDATA%\kia-remote\` | Windows Credential Manager (`kia-remote`) |
| macOS | `~/Library/Application Support/kia-remote/` | The login Keychain (`kia-remote`) |
| Linux / Pi | `~/.local/share/kia-remote/` | `secrets.json` in the data folder, readable only by that user |

The data folder also holds presets, schedules, the command log, the Kia activity log, the last health report, approved devices, passkeys
(public keys only), settings, the service log and the cached car pictures. Secrets are never
logged and never returned by the API. "Sign out of Kia" in the menu deletes them.

## Unlock: fingerprint or PIN

Unlocking asks for a second factor; lock, climate and lights don't. On a phone with a passkey set
up, Unlock shows the fingerprint or Face ID prompt. Otherwise, or if that's cancelled, it asks for
the Kia PIN. To set up a passkey on a phone, tap "Use fingerprint next time" in the PIN sheet, or
"Fingerprint for unlock" in the menu. That needs the Kia PIN once. Five wrong PINs in 15 minutes
pause the PIN for 15 minutes.

Passkeys are tied to the address: one made on `https://car.example.com` doesn't work on
localhost, and the other way round. On the computer itself, open **http://localhost:8765** (not
127.0.0.1, which can't hold a passkey).

## Built-in safety

- If Kia rejects the password or locks the account, the service stops, forgets the saved password
  and never retries on its own.
- A forced refresh wakes the car and uses 12 V battery, so it's allowed once per 10 minutes. A
  normal refresh reads Kia's last known copy and doesn't wake the car.
- One command at a time.

## Demo mode

`--demo` runs a made-up car (fuel 72%, locked, parked in Ottawa). Commands stay pending for about
3 seconds, then succeed and change the fake car. A new device is approved with
`demo@example.com` / `demo` / `1234`. Any 6 digits work as the sign-in code, except `000000`,
which is treated as wrong. The unlock PIN is `1234`. Demo mode never touches saved credentials,
and demo passkeys are kept separate, so they can never unlock the real car.

## For developers

- `server/`: FastAPI service. `server/regions/` has everything that differs per region.
- `web/`: the dashboard, plain ES modules with no build step. Bump `APP_VERSION` in `web/app.js`
  and `VERSION` in `web/sw.js` together.
- `docs/api-contract.md`: the JSON API.

```bash
.venv/bin/python -m pytest        # Windows: .venv\Scripts\python.exe -m pytest
```

The tests use a fake Kia API and an in-memory keyring, so they make no real calls. Browser checks
need Playwright: run `npm i --no-save playwright-core` in a temp folder and set `PW_DIR` to that
folder. Then run `node web/dev/mock-server.mjs`, and in another window `node web/dev/shoot.mjs`
(screenshots) or `node web/dev/passkey-e2e.mjs` (Chromium's virtual fingerprint against
`--demo`).

## Credits

- [hyundai_kia_connect_api](https://github.com/Hyundai-Kia-Connect/hyundai_kia_connect_api) does
  the hard work of talking to Kia.
- [bluelinky](https://github.com/Hacksore/bluelinky) documented the Canadian horn/lights endpoint.
- Car pictures are Kia's own, from kiaconnect.ca and kia.ca, downloaded by your computer for your
  car. They are not part of this repository.

## Disclaimer

**Use this software entirely at your own risk.** By using it, you accept that:

- **No warranty, no liability.** It is provided "as is", without warranty of any kind, as the
  [MIT License](LICENSE) says in full. The authors and contributors are not liable for any damage
  or loss arising from its use. That includes, among other things, a car left unlocked, a
  drained battery, a locked or suspended Kia account, theft, injury, fines, or a command that
  did or didn't reach the car.
- **Not Kia.** This is an independent hobby project. It is not made, endorsed, supported or
  reviewed by Kia Corporation, Kia Canada, Hyundai Motor Group or any of their affiliates. "Kia",
  "Kia Connect" and the model names are trademarks of their owners and are used only to say what
  this software works with.
- **Kia's terms are yours to follow.** Using an unofficial client may go against your Kia Connect
  terms of use, and Kia may block it or suspend accounts at any time. Read those terms and decide
  for yourself.
- **Check before you trust it.** "Locked" or "climate on" is what Kia last reported, which can be
  out of date or wrong. Don't rely on this app for anything where safety matters, and use the
  official app or the car itself when it counts.
- **Remote start is your responsibility.** Never start the engine or climate with the car in an
  enclosed space such as a closed garage (carbon monoxide). Follow local laws on idling and remote
  starting.
- **You run the server, so you secure it.** The security of your installation (your computer,
  your email, your Cloudflare account, who you approve) is in your hands. See
  [docs/SECURITY.md](docs/SECURITY.md).
- **Nothing is collected.** The authors receive no data from your installation. Your computer
  talks only to Kia, kia.ca (for the car's pictures) and Cloudflare (your tunnel).

## License

MIT, see [LICENSE](LICENSE).
