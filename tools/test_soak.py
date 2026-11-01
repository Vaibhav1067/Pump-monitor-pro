"""
Long-run and serial-line tests. No rig needed.

  python tools/test_soak.py

  * ESP32 boot noise, split and damaged lines, silent and wrong-firmware boards, over a real socket
  * 24 hours of data at full speed through the server's path: memory stays flat, no slowdown
  * the ESP32's millis() rolling over after 49.7 days
  * a real backend at 50 messages a second with three browsers connected, one of which stalls
"""
import asyncio
import copy
import gc
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tracemalloc

import websockets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.config import CONFIG  # noqa: E402
from backend.server import Hub  # noqa: E402
from backend.simulator import Simulator  # noqa: E402
from backend.sources import SerialSource  # noqa: E402

failures = []
GOOD = b'{"ts":%d,"seq":%d,"rpm":1480.0,"temp_c":38.0,"current_a":2.1,"pressure_bar":2.2,"vib_x":0.1,"vib_y":0.1,"vib_z":0.1,"vib_peak":0.3,"float":1}\n'


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


def forest_cfg():
    cfg = copy.deepcopy(CONFIG)
    cfg["scorer"]["model_path"] = os.path.join(ROOT, "models", "skab_forest.json")
    return cfg


def serve(chunks):
    """One-shot board on a local socket. Each chunk is bytes to send, or a delay in seconds."""
    srv = socket.create_server(("127.0.0.1", 0))

    def run():
        conn, _ = srv.accept()
        try:
            time.sleep(0.2)                 # a real ESP32 reboots on open; pyserial clears its buffer on open
            for c in chunks:
                if isinstance(c, (int, float)):
                    time.sleep(c)
                else:
                    conn.sendall(c)
            time.sleep(3)                   # stay connected until the probe gives up, like a real board
        except OSError:
            pass
        conn.close()
        srv.close()
    threading.Thread(target=run, daemon=True).start()
    return f"socket://127.0.0.1:{srv.getsockname()[1]}"


def probe(chunks, secs=2.5):
    cfg = copy.deepcopy(CONFIG)
    cfg["serial"]["probe_timeout_s"] = secs
    src = SerialSource(cfg)
    ser, first, _rest = src._probe(serve(chunks))
    if ser:
        ser.close()
        return True, first
    return False, first


def test_serial_line():
    print("What a real board does on the serial line")
    boot = (b"\xff\x00\xfe\x80 rst:0x1 (POWERON_RESET),boot:0x13\r\nets Jul 29 2019 12:21:46\r\n"
            b"\xc3\x28garbage at the wrong baud\r\n# esp32_pump_node ready adxl=1 acs_zero=2.501V\n")
    ok, first = probe([boot, 0.3, GOOD % (1250, 1)])
    check(ok and b'"seq":1' in first, "boot noise then data: board is recognised")
    ok, first = probe([boot, b'{"ts":1250,"seq":1,"rpm":14', 0.2, b'80.0,"temp_c":38.0,"current_a":2.1,"pressure_bar":2.2,"vib_x":0.1,"vib_y":0.1,"vib_z":0.1,"vib_peak":0.3,"float":1}\r\n'])
    check(ok, "a line split across reads and ending in CRLF is accepted")
    ok, first = probe([b'"current_a":2.1,"pressure_bar":2.2}\n', GOOD % (2250, 2)])
    check(ok and b'"seq":2' in first, "joining mid-line: the half line is ignored")
    ok, why = probe([boot, b"# sensor fault: temp=0 adxl=1\n", 0.5, b"# sensor fault: temp=0 adxl=1\n"], 1.5)
    check(not ok and "temperature sensor not found" in why, f"sensor fault is named: {why}")
    ok, why = probe([b"# sensor fault: temp=0 adxl=0\n"], 1.0)
    check(not ok and "temperature sensor and accelerometer" in why, f"both named: {why}")
    ok, why = probe([1.5], 1.0)
    check(not ok and "silent" in why, f"silent board: {why}")
    ok, why = probe([b"Hello from another program\n", b"\xf8" * 2000], 1.0)
    check(not ok and "not valid pump JSON" in why, f"wrong firmware: {why}")


async def test_day():
    print("24 hours of data through the server's path, with faults and restarts")
    cfg = forest_cfg()
    hub = Hub(cfg)
    await hub.set_source("sim", announce=False)
    hub.task.cancel()
    hub.source = type("Live", (), {"live": True, "kind": "serial", "detail": "", "connected": True, "error": None})()
    hub._sync_scorer()
    hub.processor.reset()
    hub.logger.dir = os.path.join(ROOT, "build", "soak_logs")
    hub.logger.start("soak")
    sim, counts = Simulator(cfg, seed=11), {}
    cycle = ["normal"] * 8 + ["blockage", "normal", "imbalance", "normal", "overload", "normal", "dry_run", "normal", "stop", "normal"]
    tracemalloc.start()
    sizes, slowest, t0 = [], 0.0, time.perf_counter()
    for s in range(86400):
        if s % 300 == 0:
            step = cycle[(s // 300) % len(cycle)]
            sim.set_scenario("normal" if step == "stop" else step)
            sim.set_pump(step != "stop")
        t1 = time.perf_counter()
        await hub.on_line(sim.line())
        slowest = max(slowest, time.perf_counter() - t1)
        if hub.history:
            st = hub.history[-1]["status"]
            counts[st] = counts.get(st, 0) + 1
        if s % 7200 == 0:
            gc.collect()
            sizes.append(tracemalloc.get_traced_memory()[0] / 1e6)
    elapsed = time.perf_counter() - t0
    tracemalloc.stop()
    c = hub.processor.counters
    path = hub.logger.path
    hub.logger.stop()
    size = os.path.getsize(path) / 1e6
    os.remove(path)
    print(f"    {elapsed:.0f} s to run 86,400 messages. Statuses: {counts}. Memory MB every 2 h: {[round(x, 1) for x in sizes]}")
    check(c["valid"] == 86400 and c["invalid"] == 0, "no message lost or rejected")
    check(all(counts.get(k) for k in ("HEALTHY", "WARNING", "CRITICAL", "DRY-RUN PROTECTION", "STANDBY")), "every status was exercised")
    check(sizes[-1] - sizes[2] < 3, f"memory stays flat ({sizes[2]:.1f} -> {sizes[-1]:.1f} MB)")
    check(slowest < 0.25, f"slowest message took {slowest * 1000:.0f} ms (one arrives every 1000 ms)")
    check(len(hub.history) <= cfg["history_keep"] and len(hub.alerts) <= cfg["alerts_keep"], "history and event lists stay bounded")
    print(f"    A full day's recording is {size:.0f} MB")


def test_rollover():
    print("millis() rolling over after 49.7 days")
    from backend.processor import Processor
    from backend.scorer import load_scorer
    from backend.config import SENSORS
    cfg = forest_cfg()
    p = Processor(cfg, load_scorer(cfg), SENSORS)
    sim = Simulator(cfg, seed=3)
    sim.t_ms = 2 ** 32 - 200_000
    out, t = [], 1.7e9
    for _ in range(400):
        t += 1
        msg = sim.step()
        msg["ts"] %= 2 ** 32
        r = p.ingest(json.dumps(msg), t)
        out.append(r)
    check(all(r["ok"] for r in out), "every message accepted across the rollover")
    check(out[-1]["frame"]["status"] == "HEALTHY", f"still monitoring afterwards ({out[-1]['frame']['status']})")


async def test_real_server():
    print("A real backend: 50 messages a second, three browsers, one of them stalled")
    import psutil
    tcp, http = 7793, 8793
    conf = os.path.join(ROOT, "build", "soak_config.json")
    with open(conf, "w", encoding="utf-8") as fh:
        json.dump({}, fh)
    fake = subprocess.Popen([sys.executable, "tools/fake_esp32.py", "--tcp", str(tcp), "--quiet", "--rate", "50", "--seed", "9"], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    server = subprocess.Popen([sys.executable, "-m", "backend", "--source", "serial", "--serial-port", f"socket://127.0.0.1:{tcp}",
                               "--port", str(http), "--host", "127.0.0.1", "--config", conf], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url, socks = f"ws://127.0.0.1:{http}/ws", []
        for _ in range(60):
            try:
                socks.append(await websockets.connect(url, max_size=None))
                break
            except OSError:
                await asyncio.sleep(0.25)
        check(bool(socks), "backend started")
        socks.append(await websockets.connect(url, max_size=None))
        stalled = await websockets.connect(url, max_size=None, max_queue=1)     # never reads again
        proc = psutil.Process(server.pid)
        rss = []
        frames = [0, 0]

        async def reader(i, ws):
            while True:
                m = json.loads(await ws.recv())
                if m["type"] == "frame":
                    frames[i] += 1
        tasks = [asyncio.create_task(reader(i, ws)) for i, ws in enumerate(socks)]
        for _ in range(9):
            await asyncio.sleep(20)
            rss.append(proc.memory_info().rss / 1e6)
        for t in tasks:
            t.cancel()
        print(f"    frames {frames}, server memory MB {[round(x) for x in rss]}")
        check(min(frames) > 3000, f"healthy browsers keep receiving despite the stalled one ({min(frames)} frames)")
        check(rss[-1] - rss[2] < 25, f"server memory stays flat ({rss[2]:.0f} -> {rss[-1]:.0f} MB)")
        await stalled.close()
        for ws in socks:
            await ws.close()
    finally:
        for p in (server, fake):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def main():
    test_serial_line()
    asyncio.run(test_day())
    test_rollover()
    asyncio.run(test_real_server())
    print("\nSOAK PASSED" if not failures else f"\nSOAK FAILED ({len(failures)}):\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
