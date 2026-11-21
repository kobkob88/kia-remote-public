"""Real-car mode against a fake Kia API and an in-memory keyring.

Covers sign-in with a one-time code, reusing the saved token across restarts, lockout
handling (never retried), and that no secret ever reaches an API response or a file.
"""

from __future__ import annotations

import datetime as dt
import os
import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.kia import KiaCar
from server.secrets_store import SERVICE, SecretStore
from conftest import EMAIL, OTP_CODE, PASSWORD, PIN, wait_for_command

CREDS = {"email": EMAIL, "password": PASSWORD, "pin": PIN}


def make_client(tmp_path, vm_factory):
    backend = KiaCar(SecretStore(), vm_factory=vm_factory)
    backend.poll_interval = 0.05
    app = create_app(demo=False, data_dir=tmp_path / "data", backend=backend,
                     web_dir=tmp_path / "web")
    return TestClient(app, client=("127.0.0.1", 50000))


def sign_in(client):
    s = client.post("/api/login", json=CREDS).json()
    assert s["auth"] == "needs_otp"
    assert s["otp"] == {"has_email": True, "has_sms": False}
    assert client.post("/api/otp/send", json={"method": "EMAIL"}).json() == {"sent": True}
    s = client.post("/api/otp/verify", json={"code": OTP_CODE}).json()
    assert s["auth"] == "ok"
    return s


def test_starts_signed_out(tmp_path, memory_keyring, vm_factory, world):
    with make_client(tmp_path, vm_factory) as c:
        s = c.get("/api/state").json()
        assert s == {"demo": False, "auth": "needs_login", "auth_error": None, "otp": None,
                     "vehicle": None, "pending": None, "dial": {"min": 17, "max": 27}}
        assert c.post("/api/refresh").status_code == 401
        assert c.post("/api/command", json={"action": "lock"}).status_code == 401
    assert world.login_calls == 0


def test_sign_in_with_code_then_restart_reuses_token(tmp_path, memory_keyring, vm_factory, world):
    with make_client(tmp_path, vm_factory) as c:
        s = sign_in(c)
        v = s["vehicle"]
        assert v["model"] == "SPORTAGE" and v["fuel_percent"] == 72 and v["range_km"] == 533
        assert v["odometer_km"] == 1234 and v["next_service_km"] == 8000
        assert v["outside_temp_c"] is None          # Canada doesn't report it
        assert v["doors"]["front_left"] == {"open": False, "locked": None}
        assert v["updated_at"].startswith("2026-11-1")
        assert v["location"]["lat"] == 45.0
    assert world.login_calls == 1
    stored = {k[1] for k in memory_keyring.data if k[0] == SERVICE}
    assert {"email", "password", "pin", "token"} <= stored
    token_json = json.loads(memory_keyring.data[(SERVICE, "token")])
    assert "password" not in token_json and "pin" not in token_json

    # Restart: the saved token is still valid, so no new sign-in and no code.
    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/state").json()["auth"] == "ok"
    assert world.login_calls == 1

    # Restart with an expired token: one quiet re-login on the same device id, no code.
    raw = json.loads(memory_keyring.data[(SERVICE, "token")])
    raw["valid_until"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    memory_keyring.data[(SERVICE, "token")] = json.dumps(raw)
    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/state").json()["auth"] == "ok"
    assert world.login_calls == 2


def test_expired_token_on_new_device_asks_for_code(tmp_path, memory_keyring, vm_factory, world):
    with make_client(tmp_path, vm_factory) as c:
        sign_in(c)
    world.known_devices.clear()           # Kia forgot this computer
    raw = json.loads(memory_keyring.data[(SERVICE, "token")])
    raw["valid_until"] = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    memory_keyring.data[(SERVICE, "token")] = json.dumps(raw)
    calls = world.login_calls
    with make_client(tmp_path, vm_factory) as c:
        s = c.get("/api/state").json()
        assert s["auth"] == "needs_otp" and s["otp"]["has_email"] is True
        assert world.login_calls == calls + 1   # the code request came from that one attempt
        c.post("/api/otp/send", json={"method": "EMAIL"})
        assert c.post("/api/otp/verify", json={"code": OTP_CODE}).json()["auth"] == "ok"


def test_wrong_code(tmp_path, memory_keyring, vm_factory):
    with make_client(tmp_path, vm_factory) as c:
        c.post("/api/login", json=CREDS)
        assert c.post("/api/otp/verify", json={"code": "111111"}).status_code == 400   # not sent yet
        c.post("/api/otp/send", json={"method": "EMAIL"})
        r = c.post("/api/otp/verify", json={"code": "111111"})
        assert r.status_code == 400 and "code" in r.json()["error"]
        assert c.get("/api/state").json()["auth"] == "needs_otp"
        assert c.post("/api/otp/send", json={"method": "SMS"}).status_code == 400   # no phone on file


def test_wrong_password_stops_and_never_retries(tmp_path, memory_keyring, vm_factory, world):
    with make_client(tmp_path, vm_factory) as c:
        r = c.post("/api/login", json={**CREDS, "password": "wrong-pass"})
        assert r.status_code == 401
        assert "kiaconnect.ca" in r.json()["error"]
        s = c.get("/api/state").json()
        assert s["auth"] == "error" and "kiaconnect.ca" in s["auth_error"]
        assert c.post("/api/refresh").status_code == 401
    assert world.login_calls == 1
    assert (SERVICE, "password") not in memory_keyring.data
    # A restart doesn't try again either.
    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/state").json()["auth"] == "needs_login"
    assert world.login_calls == 1


def test_locked_account_message(tmp_path, memory_keyring, vm_factory, world):
    world.blocked = True
    with make_client(tmp_path, vm_factory) as c:
        r = c.post("/api/login", json=CREDS)
        assert r.status_code == 403
        msg = r.json()["error"]
        assert "kiaconnect.ca" in msg and "Stop" in msg
    assert world.login_calls == 1


def test_commands_against_fake_kia(tmp_path, memory_keyring, vm_factory, world):
    with make_client(tmp_path, vm_factory) as c:
        sign_in(c)
        assert c.post("/api/command", json={"action": "unlock"}).status_code == 403
        assert c.post("/api/command", json={"action": "unlock", "pin": "0000"}).status_code == 403
        cid = c.post("/api/command", json={"action": "unlock", "pin": PIN}).json()["id"]
        assert c.post("/api/command", json={"action": "lock"}).status_code == 409
        entry = wait_for_command(c, cid)
        assert entry["status"] == "success"
        assert c.get("/api/state").json()["vehicle"]["locked"] is False   # cheap refresh after success

        world.action_status = [ORDER_FAILED]
        cid = c.post("/api/command", json={"action": "climate_start",
                                           "options": {"set_temp_c": 22.5}}).json()["id"]
        entry = wait_for_command(c, cid)
        assert entry["status"] == "failed" and entry["message"]
        assert "climate:22.5" in world.commands
        assert '"11H"' in world.posts[-1]           # 22.5 is index 17 (0x11) of Kia's 14-31.5 table

        world.action_status = [ORDER_SUCCESS]
        for end in ("HI", "LO"):
            cid = c.post("/api/command", json={"action": "climate_start",
                                               "options": {"set_temp_c": end}}).json()["id"]
            assert wait_for_command(c, cid)["status"] == "success"
            want = "1AH" if end == "HI" else "06H"            # 27 / 17: the word makes the car skip climate
            assert f'"value": "{want}"' in world.posts[-1]

        world.action_status = [ORDER_SUCCESS]
        for action, horn in (("hazard", False), ("horn_lights", True)):
            cid = c.post("/api/command", json={"action": action}).json()["id"]
            assert wait_for_command(c, cid)["status"] == "success"
            assert f"hornlight:{horn}" in world.commands


def test_no_secret_in_responses_or_files(tmp_path, memory_keyring, vm_factory, world):
    bodies = []
    with make_client(tmp_path, vm_factory) as c:
        def call(method, url, **kw):
            r = c.request(method, url, **kw)
            bodies.append(r.text)
            return r

        call("POST", "/api/login", json=CREDS)
        call("POST", "/api/otp/send", json={"method": "EMAIL"})
        call("POST", "/api/otp/verify", json={"code": OTP_CODE})
        call("GET", "/api/state")
        call("POST", "/api/refresh", json={"force": True})
        cid = call("POST", "/api/command", json={"action": "lock"}).json()["id"]
        wait_for_command(c, cid)
        call("GET", f"/api/command/{cid}")
        # Unlock's second factor: a wrong PIN, the right PIN, and a passkey enrolment with the PIN.
        call("POST", "/api/command", json={"action": "unlock", "pin": "0000"})
        unlock = call("POST", "/api/command", json={"action": "unlock", "pin": PIN}).json()["id"]
        wait_for_command(c, unlock)
        call("GET", f"/api/command/{unlock}")
        call("POST", "/api/passkeys/register/options", json={"label": "Phone"})
        call("POST", "/api/passkeys/register", json={"pin": PIN, "credential": {"id": "x"}})
        call("POST", "/api/passkeys/register", json={"pin": "0000", "credential": {"id": "x"}})
        call("GET", "/api/passkeys")
        call("GET", "/api/log")
        call("GET", "/api/presets")
        token = json.loads(memory_keyring.data[(SERVICE, "token")])
        call("POST", "/api/login", json={**CREDS, "password": "another-wrong-one"})
        call("GET", "/api/state")
        call("POST", "/api/logout")

    secrets = [EMAIL, PASSWORD, PIN, token["access_token"], token["refresh_token"],
               "another-wrong-one"]
    files = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "data").iterdir() if p.is_file()}
    assert "server.log" in files and "command-log.json" in files
    assert "login attempt" in files["server.log"]       # the library's careless line was logged...
    for secret in secrets:
        for body in bodies:
            assert secret not in body
        for name, text in files.items():
            assert secret not in text, f"secret leaked into {name}"
    # Logout cleared Credential Manager.
    assert not [k for k in memory_keyring.data if k[0] == SERVICE]


from hyundai_kia_connect_api.const import ORDER_STATUS  # noqa: E402

ORDER_FAILED = ORDER_STATUS.FAILED
ORDER_SUCCESS = ORDER_STATUS.SUCCESS


@pytest.mark.parametrize("length", [10, 2500])
def test_token_round_trip_and_chunking(memory_keyring, length):
    from hyundai_kia_connect_api.Token import Token

    store = SecretStore()
    token = Token(username=EMAIL, password=PASSWORD, pin=PIN, access_token="A" * length,
                  refresh_token="R", device_id="dev", valid_until=dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    store.save_token(token)
    assert all(len(v) <= 1000 for v in memory_keyring.data.values())
    back = store.load_token(EMAIL, PASSWORD, PIN)
    assert back.access_token == "A" * length and back.device_id == "dev"
    assert back.password == PASSWORD and back.valid_until == token.valid_until
    store.clear()
    assert memory_keyring.data == {}


@pytest.mark.parametrize("raw, shown", [("HI", "HI"), ("LO", "LO"), (" hi ", "HI"), (21.5, 21.5),
                                        ("22", 22), ("OFF", None), (None, None)])
def test_set_temp_keeps_hi_and_lo(raw, shown):
    from server.kia import _set_temp
    assert _set_temp(raw) == shown


def test_vehicle_state_reads_hi_through_the_library():
    from hyundai_kia_connect_api import Vehicle
    from server.kia import vehicle_to_state
    v = Vehicle(id="veh-1", name="SPORTAGE", model="SPORTAGE", year=2026)
    v.air_temperature = ("HI", "c")       # what the Canadian API leaves for a maxed-out dial
    assert vehicle_to_state(v)["climate"]["set_temp_c"] == "HI"
    v.air_temperature = (21.5, "c")
    assert vehicle_to_state(v)["climate"]["set_temp_c"] == 21.5


def _timer_view(log, updated_at, on=True):
    import types
    from server.service import CarService
    vehicle = {"updated_at": updated_at, "engine_on": on, "climate": {"on": on, "set_temp_c": 22}}
    return CarService._with_timer(types.SimpleNamespace(_log=log), vehicle)


def _start(finished, minutes=10, status="success"):
    return {"action": "climate_start", "status": status, "options": {"duration_min": minutes},
            "sent_at": finished, "finished_at": finished}


def test_climate_shown_off_once_its_timer_ran_out_without_a_new_report():
    import datetime as dt
    now = dt.datetime.now().astimezone()
    ago = lambda m: (now - dt.timedelta(minutes=m)).isoformat(timespec="seconds")
    # Started 15 min ago for 10 min; the car last reported right after starting.
    v = _timer_view([_start(ago(15))], ago(14))
    assert v["climate"]["on"] is False and v["engine_on"] is False and v["climate"]["ended_at"]
    # Still within the timer: unchanged.
    assert _timer_view([_start(ago(5))], ago(4))["climate"]["on"] is True
    # The car reported after the timer ended and still says on (someone drove off): trust the car.
    assert _timer_view([_start(ago(15))], ago(1))["climate"]["on"] is True
    # A successful stop counts at once; a failed start counts for nothing.
    stop = {"action": "climate_stop", "status": "success", "finished_at": ago(2)}
    assert _timer_view([stop, _start(ago(5))], ago(4))["climate"]["on"] is False
    assert _timer_view([_start(ago(30), status="failed")], ago(29))["climate"]["on"] is True
    # Climate the app didn't start (no log entry): unchanged.
    assert _timer_view([], ago(60))["climate"]["on"] is True


def test_state_rereads_kias_copy_every_couple_of_minutes_in_the_background(tmp_path):
    import asyncio
    import types
    from server.service import CarService
    from server.storage import Storage

    calls = []
    backend = types.SimpleNamespace(demo=False, auth="ok", auth_error=None, otp=None,
                                    vehicle=lambda: None, refresh=lambda force: calls.append(force),
                                    dial=lambda: (17.0, 27.0))
    now = [1000.0]

    async def main():
        svc = CarService(backend, Storage(tmp_path, False), clock=lambda: now[0])
        svc.state()
        await asyncio.sleep(0.05)
        assert calls == []                 # just read at start
        now[0] += 121
        svc.state(); svc.state()           # one reread, not two
        await asyncio.sleep(0.05)
        assert calls == [False]            # cheap: never wakes the car
        now[0] += 30
        svc.state()
        await asyncio.sleep(0.05)
        assert calls == [False]
        await svc.stop()

    asyncio.run(main())


def test_engine_only_remote_start_also_ends_with_its_timer():
    import datetime as dt
    now = dt.datetime.now().astimezone()
    ago = lambda m: (now - dt.timedelta(minutes=m)).isoformat(timespec="seconds")
    v = _timer_view([_start(ago(15))], ago(14), on=False)
    v_engine = {**v}
    from server.service import CarService
    import types
    vehicle = {"updated_at": ago(14), "engine_on": True, "climate": {"on": False, "set_temp_c": None}}
    out = CarService._with_timer(types.SimpleNamespace(_log=[_start(ago(15))]), vehicle)
    assert out["engine_on"] is False and v_engine["engine_on"] is False


def test_car_photo_is_kias_picture_of_this_model_and_paint_kept_on_the_pc(tmp_path, memory_keyring, vm_factory, world):
    png = b"\x89PNG\r\n\x1a\n" + b"car" * 10
    asked = []

    def download(url):
        asked.append(url)
        return png

    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/car-photo").status_code == 404          # signed out
        sign_in(c)
        c.app.state.service.download_image = download
        r = c.get("/api/car-photo")
        assert r.status_code == 200 and r.content == png
        assert r.headers["content-type"] == "image/png" and "max-age" in r.headers["cache-control"]
        assert asked == ["https://kiaconnect.ca/cms/JWWD/2026/HRB/CWP/cwp_45degree.png"]
        assert c.get("/api/car-photo").content == png
        assert len(asked) == 1 and world.vehicle_lists == 1          # downloaded and looked up once
    assert list((tmp_path / "data").glob("car-photo-*.png"))


def test_no_car_photo_when_kia_has_none_and_no_asking_again_right_away(tmp_path, memory_keyring, vm_factory, world):
    world.paint = "../../x"                                          # not a paint code: never put in a URL
    with make_client(tmp_path, vm_factory) as c:
        sign_in(c)
        c.app.state.service.download_image = lambda url: pytest.fail("nothing to download")
        r = c.get("/api/car-photo")
        assert r.status_code == 404 and r.headers["cache-control"] == "no-store"
        assert c.get("/api/car-photo").status_code == 404
        assert world.vehicle_lists == 1


def test_region_comes_from_settings_and_unknown_regions_are_refused(tmp_path):
    from server import regions
    assert regions.get(None).key == "canada" and regions.get(" Canada ").key == "canada"
    with pytest.raises(ValueError, match="Known: canada"):
        regions.get("narnia")
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text('{"region": "narnia"}', encoding="utf-8")
    with pytest.raises(ValueError):
        create_app(demo=False, data_dir=data, web_dir=tmp_path / "web")


def test_region_sets_the_dial_and_the_sign_in_site(tmp_path, memory_keyring, vm_factory, world):
    from server import errors, regions
    from hyundai_kia_connect_api.exceptions import AuthenticationBlockedError

    class Test(regions.Region):
        key, account_site, library_region, dial_min, dial_max = "test", "kia.example", 2, 16.0, 30.0

    backend = KiaCar(SecretStore(), vm_factory=vm_factory, region=Test())
    app = create_app(demo=False, data_dir=tmp_path / "data", backend=backend, web_dir=tmp_path / "web")
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/state").json()["dial"] == {"min": 16, "max": 30}
    assert "kia.example" in backend._fail(AuthenticationBlockedError("x")).message
    assert "kiaconnect.ca" in errors.classify(AuthenticationBlockedError("x"), "kiaconnect.ca")[2]


def test_car_spin_downloads_the_turntable_once_and_serves_each_frame(tmp_path, memory_keyring, vm_factory, world, monkeypatch):
    from server.regions import canada
    webp = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"x" * 8
    asked = []
    monkeypatch.setattr(canada.Canada, "spin_frames",
                        lambda self, entry: [f"https://kia.example/{entry['paint']}/{n:02d}.webp" for n in range(1, 37)])
    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/car-spin").json() == {"frames": 0}            # signed out
        sign_in(c)
        c.app.state.service.download_image = lambda url: asked.append(url) or webp
        assert c.get("/api/car-spin").json() == {"frames": 36}
        assert c.get("/api/car-spin").json() == {"frames": 36}
        assert len(asked) == 36 and asked[0].endswith("/HRB/01.webp")
        r = c.get("/api/car-spin/36")
        assert r.status_code == 200 and r.content == webp and r.headers["content-type"] == "image/webp"
        assert c.get("/api/car-spin/37").status_code == 404


def test_kia_ca_catalog_matches_model_paint_and_nearest_year():
    from server.regions.kia_ca_360 import build_index, match
    import html as html_lib
    import json as json_lib
    data = {"vehicles": {"vehicleList": [
        {"model": "Sportage", "year": "2026", "trims": [{"colours": [{"colourCode": "HRB", "exterior360ImageUrls":
            [f"/360s/2026/sportage/heritage-blue/{n:02d}.{e}" for n in range(1, 37) for e in ("png", "webp")]}]}]},
        {"model": "EV6", "year": "2025", "trims": [{"colours": [{"colourCode": "C7S", "exterior360ImageUrls":
            [f"/360s/2025/ev6/wolf-grey/{n:02d}.webp" for n in range(1, 37)]}]}]},
    ]}}
    page = '<div vehicle-list="' + html_lib.escape(json_lib.dumps(data)) + '"></div>'
    index = build_index(page)
    assert len(index) == 2
    frames = match(index, "SPORTAGE", 2026, "HRB")
    assert len(frames) == 36 and frames[0] == "https://www.kia.ca/360s/2026/sportage/heritage-blue/01.webp"
    assert match(index, "SPORTAGE HYBRID", 2026, "HRB") == frames      # same first word
    assert match(index, "EV6", 2022, "C7S")[0].endswith("ev6/wolf-grey/01.webp")   # nearest year
    assert match(index, "SPORTAGE", 2026, "XXX") is None and match(index, "SOUL", 2022, "C7S") is None


# ---------------------------------------------------------------- secrets in a file (Linux, Raspberry Pi)

def test_file_secret_store_round_trip(tmp_path, forbidden_keyring):
    from hyundai_kia_connect_api.Token import Token

    path = tmp_path / "data" / "secrets.json"
    store = SecretStore(path)
    assert store.load_credentials() is None
    store.save_credentials(EMAIL, PASSWORD, PIN)
    token = Token(username=EMAIL, password=PASSWORD, pin=PIN, access_token="A" * 2500,
                  refresh_token="R", device_id="dev", valid_until=dt.datetime(2030, 1, 1, tzinfo=dt.UTC))
    store.save_token(token)
    again = SecretStore(path)                       # a restart reads the same file
    assert again.load_credentials() == (EMAIL, PASSWORD, PIN)
    assert again.load_pin() == PIN
    assert again.load_token(EMAIL, PASSWORD, PIN).access_token == "A" * 2500
    assert not list(path.parent.glob("*.tmp"))
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600
    again.clear()
    assert not path.exists()                        # sign-out leaves no file behind


def test_settings_choose_the_secret_store(tmp_path, forbidden_keyring, monkeypatch):
    import server.app as app_module

    made = []
    monkeypatch.setattr(app_module, "KiaCar", lambda store, region=None: made.append(store) or _NoCar(region))
    create_app(data_dir=tmp_path / "a", web_dir=tmp_path, settings={"secret_store": "file"}, configure_logging=False)
    assert made[-1].file is not None and made[-1].file.path == tmp_path / "a" / "secrets.json"
    create_app(data_dir=tmp_path / "b", web_dir=tmp_path, settings={"secret_store": "keyring"}, configure_logging=False)
    assert made[-1].file is None
    with pytest.raises(ValueError):
        create_app(data_dir=tmp_path / "c", web_dir=tmp_path, settings={"secret_store": "usb"}, configure_logging=False)


class _NoCar:
    demo, auth = False, "needs_login"

    def __init__(self, region):
        self.region = region

    def saved_pin(self):
        return None

    def saved_credentials(self):
        return None


KIA_REPORT = {
    "reportId": 1234567, "reportDate": "2026-10-25", "odometer": 5021.6, "odometerUnit": 1,
    "odometerUnitStr": "km",
    "systems": [
        {"name": "CHASSIS", "subSystems": [
            {"name": "Electronic Parking Brake", "code": "EPB", "status": "G", "img": "x.png"},
            {"name": "Tire Pressure Monitoring System", "code": "TPMS", "status": "R", "img": "y.png"}]},
        {"name": "HEV", "subSystems": [
            {"name": "Battery Management System", "code": "BMS", "status": "G", "img": "z.png"}]},
    ],
    "drivingInfo": {"startsCount": 120, "runTime": 118320, "runTimeUnit": 3,
                    "mileageTime": 84600, "mileageTimeUnit": 3, "idleEngineTime": 33720,
                    "idleEngineTimeUnit": 3, "runDistance": 980, "runDistanceUnit": 1,
                    "tirePressureFL": None},
}


def test_health_report_is_kept_from_the_librarys_own_read(tmp_path, memory_keyring, vm_factory, world):
    world.health_report = KIA_REPORT
    client = make_client(tmp_path, vm_factory)
    assert client.get("/api/health").status_code == 401          # signed out: nothing
    sign_in(client)
    report = client.get("/api/health").json()["report"]
    assert report["date"] == "2026-10-25" and report["odometer_km"] == 5022
    assert [(s["group"], s["code"], s["ok"]) for s in report["systems"]] == [
        ("chassis", "EPB", True), ("chassis", "TPMS", False), ("hybrid", "BMS", True)]
    assert report["driving"] == {"starts": 120, "engine_on_s": 118320, "drive_s": 84600,
                                 "idle_s": 33720, "distance_km": 980}
    assert (tmp_path / "data" / "health-report.json").exists()
    client.post("/api/logout")                                    # it belongs to that account
    assert not (tmp_path / "data" / "health-report.json").exists()


def test_no_health_report_before_kias_first(tmp_path, memory_keyring, vm_factory, world):
    client = make_client(tmp_path, vm_factory)
    sign_in(client)
    assert client.get("/api/health").json() == {"report": None}


def test_health_report_leaves_out_what_it_cant_read():
    from server import health
    assert health.parse(None) is None and health.parse({}) is None
    report = health.parse({"systems": [{"name": "X", "subSystems": [{"name": "Y", "status": ""}]}],
                           "drivingInfo": {"runTime": 5, "runTimeUnit": 9, "runDistance": 3,
                                           "runDistanceUnit": 2}})
    assert report["systems"] == [{"group": "x", "name": "Y", "code": None, "ok": None}]
    assert report["driving"]["engine_on_s"] is None               # unknown unit: not guessed
    assert report["driving"]["distance_km"] == 5                  # 3 miles


def test_kia_activity_log(tmp_path, memory_keyring, vm_factory, world):
    """Every call to Kia is logged with why it happened and whether it reached the car."""
    with make_client(tmp_path, vm_factory) as c:
        assert c.get("/api/kia-calls").json() == {"entries": []}   # signed out: nothing asked of Kia
        sign_in(c)
        whys = [e["why"] for e in c.get("/api/kia-calls").json()["entries"]]
        assert whys[:3] == ["code_check", "code_send", "sign_in"]

        c.post("/api/refresh", json={"force": False})
        c.post("/api/refresh", json={"force": True})
        world.action_status = [ORDER_SUCCESS]
        cid = c.post("/api/command", json={"action": "climate_start",
                                           "options": {"set_temp_c": 21}, "pin": PIN}).json()["id"]
        assert wait_for_command(c, cid)["status"] == "success"
        entries = c.get("/api/kia-calls").json()["entries"]
        by_why = {}
        for e in entries:
            by_why.setdefault(e["why"], e)
        assert by_why["refresh"]["reached_car"] is False and by_why["refresh"]["ok"] is True
        assert by_why["wake"]["reached_car"] is True
        cmd = by_why["command"]
        assert cmd["reached_car"] is True and cmd["action"] == "climate_start"
        assert "rmtstrt" in cmd["endpoints"]                 # the request itself, by endpoint
        assert by_why["check"]["reached_car"] is False       # asking Kia about it doesn't touch the car
        assert by_why["after_command"]["reached_car"] is False
        assert entries[0]["why"] == "after_command"          # newest first

        # A cached answer that never asked Kia isn't logged; a failed call is.
        n = len(entries)
        assert c.post("/api/refresh", json={"force": True}).status_code == 429   # stopped before Kia
        assert len(c.get("/api/kia-calls").json()["entries"]) == n

    # Kept across restarts (the restart's own sign-in comes on top).
    with make_client(tmp_path, vm_factory) as c:
        entries = c.get("/api/kia-calls").json()["entries"]
        assert entries[0]["why"] == "startup" and len(entries) == n + 1
    assert (tmp_path / "data" / "kia-calls.json").is_file()


def test_find_my_car_counts_as_reaching_the_car(tmp_path, memory_keyring, vm_factory, world):
    backend = KiaCar(SecretStore(), vm_factory=vm_factory)
    assert backend.reaches_car(["status"], ["lstvhclsts", "nxtsvc"]) is False
    assert backend.reaches_car(["status"], ["lstvhclsts", "fndmcr"]) is True
    assert backend.reaches_car(["wake"], []) is True
