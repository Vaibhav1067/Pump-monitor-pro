"""
Hardware check: read the ESP32 for 30 s and report on every sensor in plain words.
Run it with the pump running normally. Close the dashboard first, because only one program
can use the port.

  python tools/check_rig.py                  (Windows: double-click check.bat)
  python tools/check_rig.py --port COM5
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG  # noqa: E402
from backend.schema import Invalid, parse_line  # noqa: E402
from backend.sources import USB_BRIDGES  # noqa: E402


def find_port():
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.vid is not None]
    ports.sort(key=lambda p: p.vid not in USB_BRIDGES)
    return ports[0].device if ports else None


def read_rig(port, seconds):
    import serial
    good, faults, rejected, got_bytes = [], set(), {}, False
    with serial.serial_for_url(port, baudrate=CONFIG["serial"]["baud"], timeout=0.5) as ser:
        end, buf = time.monotonic() + seconds, b""
        while time.monotonic() < end:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            got_bytes, buf = True, buf + chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.startswith(b"# sensor fault"):
                    faults.add(line.decode("ascii", "replace").strip())
                    continue
                try:
                    good.append(parse_line(line, CONFIG))
                except Invalid as e:
                    if e.reason != "not_json":
                        rejected[str(e)] = rejected.get(str(e), 0) + 1
    return good, faults, rejected, got_bytes


def judge(rows):
    """-> [(sensor, ok, message)]. Only flags what points at wiring, not at the pump's health."""
    def spread(key):
        xs = [r[key] for r in rows]
        return min(xs), max(xs), sum(xs) / len(xs)
    out = []
    lo, hi, m = spread("rpm")
    out.append(("Speed", m >= 200, f"{m:.0f} rpm" if m >= 200 else "reads 0. Check the A3144 on GPIO26, its 10 kOhm pull-up and the magnet gap"))
    lo, hi, m = spread("current_a")
    out.append(("Current", m >= 0.1, f"{m:.2f} A" if m >= 0.1 else "reads 0. Check the ACS712 on GPIO34 and its divider, and that it carries the motor's live wire"))
    lo, hi, m = spread("flow_lpm")
    out.append(("Flow", m > 0.2, f"{m:.1f} L/min" if m > 0.2 else "reads 0. Check the YF-S201 on GPIO27, its arrow direction, and that water is moving"))
    lo = min(r["pressure_raw"] for r in rows)
    m = spread("pressure_bar")[2]
    out.append(("Pressure", lo > -0.3, f"{m:.2f} bar" if lo > -0.3 else "no signal. Check its 5 V supply, the divider and GPIO35"))
    lo, hi, m = spread("temp_c")
    out.append(("Temperature", 0 < m < 100, f"{m:.1f} C" if 0 < m < 100 else f"reads {m:.1f} C, which looks wrong. Check the DS18B20 on GPIO4 and its 4.7 kOhm pull-up"))
    lo, hi, m = spread("vib_rms")
    out.append(("Vibration", m > 0.002, f"{m:.3f} g" if m > 0.002 else "reads almost nothing with the pump running. Check the ADXL345 is bolted to the pump"))
    low = sum(r["float"] == 0 for r in rows)
    out.append(("Water level", low == 0, "OK" if low == 0 else "reads LOW. Check the tank. If it is full, check the float switch on GPIO25 (closed must mean water OK)"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()

    port = a.port or find_port()
    if not port:
        print("FAILED: no ESP32 found on USB.\nFix: use a data-capable USB cable, and install the CP210x or CH340 driver if no COM port appears in Device Manager.")
        return 1
    print(f"Reading {port} for {a.seconds:.0f} s. Keep the pump running normally...\n")
    try:
        rows, faults, rejected, got_bytes = read_rig(port, a.seconds)
    except Exception as e:      # noqa: BLE001 - any port failure ends in the same plain advice
        print(f"FAILED: could not read {port}: {e}\nFix: close the dashboard and Arduino Serial Monitor, then try again.")
        return 1

    for f in sorted(faults):
        print(f"SENSOR NOT FOUND  {f[2:]}")
        print("  temp=0: check the DS18B20 on GPIO4, 3.3 V, GND and the 4.7 kOhm pull-up")
        print("  adxl=0: check the ADXL345: SDA to GPIO21, SCL to GPIO22, 3.3 V, GND")
    for why, n in rejected.items():
        print(f"REJECTED {n}x  {why}")
    if not rows:
        print("\nFAILED: no valid readings.")
        if not got_bytes:
            print("Fix: the board sends nothing. Run flash.bat to load the firmware, then press its EN button.")
        elif not faults:
            print("Fix: the board sends something else. Run flash.bat to load this firmware.")
        return 1

    rate = len(rows) / a.seconds
    print(f"{len(rows)} valid readings ({rate:.1f} per second, expected about 1)\n")
    results = judge(rows)
    for name, ok, msg in results:
        print(f"  {'OK   ' if ok else 'CHECK'}  {name:12} {msg}")
    good = all(ok for _, ok, _ in results) and not rejected and not faults and rate > 0.7
    print("\nALL SENSORS LOOK WIRED CORRECTLY. You can start the dashboard now." if good
          else "\nFix the items above, then run this again.")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
