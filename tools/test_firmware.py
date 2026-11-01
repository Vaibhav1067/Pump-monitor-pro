"""
Runs the real firmware source on this PC against simulated sensors (tools/firmware_host) and
feeds what it prints through the backend's validator. No board needed, but no substitute for one:
it proves the sketch's logic and arithmetic, not the wiring, timing or electrical noise of a rig.

  pip install ziglang          (a C++ compiler; test machine only)
  python tools/test_firmware.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.config import CONFIG  # noqa: E402
from backend.schema import Invalid, parse_line  # noqa: E402

HOST = os.path.join(ROOT, "tools", "firmware_host")
SKETCH = os.path.join(ROOT, "firmware", "esp32_pump_node")
EXE = os.path.join(ROOT, "build", "firmware_host.exe")
failures = []


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


def build():
    os.makedirs(os.path.dirname(EXE), exist_ok=True)
    cmd = [sys.executable, "-m", "ziglang", "c++", "-std=c++17", "-O1", "-x", "c++", f"-I{HOST}", f"-I{SKETCH}",
           os.path.join(HOST, "main.cpp"), "-o", EXE]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        sys.exit("host build failed:\n" + p.stderr[-3000:])


def run(**kw):
    """-> (valid readings, fault lines, rejected reasons, all text lines)"""
    kw.setdefault("seconds", 12)
    out = subprocess.run([EXE, *[f"{k}={v}" for k, v in kw.items()]], capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"firmware host exited {out.returncode}: {out.stderr[-500:]}")
    rows, faults, rejected, lines = [], [], [], out.stdout.splitlines()
    for line in lines:
        if line.startswith("# sensor fault"):
            faults.append(line)
            continue
        try:
            rows.append(parse_line(line.encode(), CONFIG))
        except Invalid as e:
            if e.reason != "not_json":
                rejected.append(str(e))
    return rows, faults, rejected, lines


def mean(rows, key, skip=3):
    xs = [r[key] for r in rows[skip:]]
    return sum(xs) / len(xs)


def near(a, b, tol):
    return abs(a - b) <= tol


def main():
    build()
    print("Normal running: the sketch reports what the sensors are doing")
    cases = [("small pump", dict(rpm=1480, amps=2.0, bar=2.0, temp=40, vib=0.12)),
             ("2-pole household pump", dict(rpm=2880, amps=4.5, bar=1.6, temp=55, vib=0.30)),
             ("large pump", dict(rpm=2880, amps=7.0, bar=1.0, temp=60, vib=0.5)),
             ("near full-scale pressure", dict(rpm=1480, amps=2.0, bar=4.5, temp=40, vib=0.12)),
             ("DC motor", dict(rpm=2000, amps=3.0, bar=1.0, temp=35, vib=0.10, ac=0))]
    for label, c in cases:
        rows, faults, rejected, lines = run(**c)
        check(len(rows) >= 9 and not faults and not rejected, f"{label}: all lines valid ({len(rows)} readings)")
        check(near(mean(rows, "rpm"), c["rpm"], c["rpm"] * 0.03), f"{label}: speed {mean(rows, 'rpm'):.0f} vs {c['rpm']} rpm")
        check(near(mean(rows, "current_a"), c["amps"], 0.15), f"{label}: current {mean(rows, 'current_a'):.2f} vs {c['amps']} A")
        check(near(mean(rows, "pressure_bar"), c["bar"], 0.1), f"{label}: pressure {mean(rows, 'pressure_bar'):.2f} vs {c['bar']} bar")
        check(near(mean(rows, "temp_c"), c["temp"], 0.1), f"{label}: temperature {mean(rows, 'temp_c'):.1f} vs {c['temp']} C")
        check(near(mean(rows, "vib_rms"), c["vib"], c["vib"] * 0.15), f"{label}: vibration {mean(rows, 'vib_rms'):.3f} vs {c['vib']} g (gravity removed)")
        check(all(r["float"] == 1 for r in rows), f"{label}: water level OK")
        check(max(len(x) for x in lines) <= CONFIG["serial"]["max_line_bytes"], f"{label}: lines fit the serial limit")
        ts = [r["ts"] for r in rows]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        check(all(990 <= g <= 1100 for g in gaps) and [r["seq"] for r in rows] == list(range(1, len(rows) + 1)),
              f"{label}: one reading per second, numbered without gaps ({min(gaps)}-{max(gaps)} ms)")

    print("Pump stopped")
    rows, faults, rejected, _ = run(start_s=999)
    check(rows and mean(rows, "rpm") == 0, "speed reads 0")
    check(mean(rows, "current_a") < CONFIG["status"]["standby_current_a"], f"current noise {mean(rows, 'current_a'):.2f} A stays below the running threshold")
    rows, *_ = run(start_s=1, stop_s=6, seconds=12)
    check(rows[2]["rpm"] > 1000 and rows[-1]["rpm"] == 0, "speed returns to 0 after the pump stops")

    print("Sensors missing from the start")
    for label, kw, tag in [("temperature", {"no_temp": 1}, "temp=0"), ("accelerometer", {"no_adxl": 1}, "adxl=0")]:
        rows, faults, *_ = run(**kw)
        check(not rows and faults and tag in faults[-1], f"{label}: no made-up readings, fault names it ({faults[-1] if faults else 'none'})")
    rows, faults, rejected, _ = run(no_pressure=1)
    check(len(rows) >= 9 and not rejected and mean(rows, "pressure_bar") == 0 and mean(rows, "pressure_raw") < -0.3, "pressure: readings continue, and the missing sensor can be flagged")
    rows, faults, rejected, _ = run(no_acs=1)
    check(len(rows) >= 9 and not rejected, "current: readings continue")
    rows, faults, rejected, _ = run(rpm=0)
    check(len(rows) >= 9 and mean(rows, "rpm") == 0 and mean(rows, "current_a") > 1, "speed: readings continue, current still shows the pump running")
    rows, *_ = run(float_low=1)
    check(rows and all(r["float"] == 0 for r in rows), "water level low is reported")

    print("Sensors lost while running")
    rows, faults, rejected, _ = run(lose_temp_s=6, seconds=16)
    last = rows[-1]["ts"] if rows else 0
    check(rows and faults and "temp=0" in faults[-1] and last < 13000, f"temperature: stops within a few seconds, never frozen (last reading at {last / 1000:.0f} s)")
    rows, faults, rejected, _ = run(lose_adxl_s=6, seconds=12)
    check(rows and faults and "adxl=0" in faults[-1] and rows[-1]["ts"] < 8000, "accelerometer: reports a fault instead of 0 g")
    rows, faults, rejected, _ = run(lose_adxl_s=6, back_adxl_s=9, seconds=16)
    check(faults and rows and rows[-1]["ts"] > 13000 and rows[-1]["vib_rms"] > 0.05, "accelerometer: recovers by itself when it comes back")

    print("\nFIRMWARE TEST PASSED" if not failures else f"\nFIRMWARE TEST FAILED ({len(failures)}):\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
