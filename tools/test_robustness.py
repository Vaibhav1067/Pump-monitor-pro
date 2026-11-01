"""
What real hardware can do that the simulator normally doesn't. No rig needed:

  python tools/test_robustness.py

  * the firmware's JSON matches what the backend requires
  * random bytes and damaged messages never crash the pipeline
  * unusual but real readings are accepted instead of discarding the whole message
  * a pump that looks nothing like config.py: learns first, no false alarms, faults caught
  * dead speed sensor, missing pressure sensor, ESP32 reboot, low water during learning
  * a failing CSV file or a pipeline bug does not stop the stream
"""
import asyncio
import copy
import json
import os
import random
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.config import CONFIG, SENSORS  # noqa: E402
from backend.processor import Processor  # noqa: E402
from backend.schema import NUMERIC  # noqa: E402
from backend.scorer import load_scorer  # noqa: E402
from backend.server import Hub  # noqa: E402
from backend.simulator import Simulator  # noqa: E402

ALARMS = ("WARNING", "CRITICAL")
failures = []


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


def forest_cfg(**baseline):
    cfg = copy.deepcopy(CONFIG)
    cfg["scorer"]["model_path"] = os.path.join(ROOT, "models", "skab_forest.json")
    cfg["baseline"].update(baseline)
    return cfg


def live_pipeline(cfg):
    """The default model in the mode it runs in on a real pump."""
    scorer = load_scorer(cfg)
    scorer.fallback_on = False
    return Processor(cfg, scorer, SENSORS)


class Rig:
    """Drives a Processor from the simulator, optionally damaging each message first."""

    def __init__(self, cfg, seed=1, damage=None):
        self.sim = Simulator(cfg, seed=seed)
        self.p = live_pipeline(cfg)
        self.t = 1.7e9
        self.damage = damage

    def run(self, secs):
        out = []
        for _ in range(secs):
            self.t += 1
            msg = self.sim.step()
            if self.damage:
                self.damage(msg)
            res = self.p.ingest(json.dumps(msg), self.t)
            assert res["ok"], res
            out.append(res["frame"])
        return out


def test_firmware_contract():
    print("Firmware message format")
    with open(os.path.join(ROOT, "firmware", "esp32_pump_node", "esp32_pump_node.ino"), encoding="utf-8") as fh:
        ino = fh.read()
    keys = set(re.findall(r'\\"(\w+)\\":%', ino))
    check(set(CONFIG["required_fields"]) <= keys, f"firmware sends every required field ({len(keys)} fields)")
    check(keys <= set(NUMERIC) | {"ts", "seq", "float"}, "firmware sends no field the backend doesn't know")
    check("115200" in ino and CONFIG["serial"]["baud"] == 115200, "baud rate matches (115200)")
    for s in SENSORS:
        m = re.search(r"GPIO(\d+)", s["pin"])
        if m:
            check(re.search(rf"PIN_\w+ = {m.group(1)}\b", ino) or f"GPIO{m.group(1)}" in ino, f"{s['name']} pin GPIO{m.group(1)} is in the firmware")


def test_fuzz():
    print("Random and damaged messages (40,000)")
    rng = random.Random(7)
    cfg = forest_cfg()
    p = live_pipeline(cfg)
    sim = Simulator(cfg, seed=2)
    junk = [1e308, -1e308, 10 ** 400, None, True, "x", [], {}, -1, 0, 3.5e9]
    crashed, unserialisable = None, 0
    for i in range(40000):
        msg = sim.step()
        kind = i % 4
        if kind == 0:
            line = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 120)))
        elif kind == 1:
            msg[rng.choice(list(msg))] = rng.choice(junk)
            line = json.dumps(msg)
        elif kind == 2:
            line = json.dumps(msg)[:rng.randint(0, 150)]
        else:
            line = json.dumps(msg)
        try:
            res = p.ingest(line, 1.7e9 + i)
            if res["ok"]:
                json.dumps(res["frame"], allow_nan=False)
        except ValueError:
            unserialisable += 1
        except Exception as e:      # noqa: BLE001 - this is what the test looks for
            crashed = f"{e!r} on {line!r}"
            break
    check(crashed is None, f"pipeline never crashed{'' if crashed is None else ': ' + crashed}")
    check(unserialisable == 0, "every accepted frame is valid JSON for the browser")
    check(p.counters["invalid"] > 0 and p.counters["valid"] > 0, f"bad data rejected and counted ({p.counters['invalid']} rejected, {p.counters['valid']} valid)")


def test_real_extremes():
    print("Unusual but real readings")
    good = {"ts": 1000, "seq": 1, "rpm": 2880.0, "temp_c": 40.0, "current_a": 4.0, "flow_lpm": 20.0,
            "pressure_bar": 2.0, "vib_x": 0.1, "vib_y": 0.1, "vib_z": 0.1, "vib_peak": 0.3, "float": 1}
    for label, change in [("2-pole motor at 3,000 rpm", {"rpm": 3000}), ("60 Hz 2-pole motor at 3,600 rpm", {"rpm": 3600}),
                          ("large pump overrunning the flow sensor", {"flow_lpm": 60}),
                          ("pressure sensor unplugged (0 V)", {"pressure_bar": -0.625}),
                          ("hard knock on the accelerometer", {"vib_peak": 20}),
                          ("hot motor", {"temp_c": 110}), ("high current", {"current_a": 25}), ("cold rig", {"temp_c": -5})]:
        p = live_pipeline(forest_cfg())
        check(p.ingest(json.dumps({**good, **change}), 1.0)["ok"], f"accepted: {label}")
    p = live_pipeline(forest_cfg())
    check(not p.ingest(json.dumps({**good, "rpm": 9999}), 1.0)["ok"], "still rejected: impossible speed (9,999 rpm)")


def test_different_pump():
    print("A pump nothing like config.py (2880 rpm, 4.5 A, 55 L/min, 1.6 bar, 45 C, 0.3 g)")
    base = dict(rpm=2880, current_a=4.5, flow_lpm=55.0, pressure_bar=1.6, temp_c=45.0, vib_rms=0.3)
    for seed in (1, 2, 3):
        rig = Rig(forest_cfg(**base), seed=seed)
        learn = rig.run(135)
        stats = Counter(f["status"] for f in learn)
        check(stats["LEARNING"] >= 110 and not any(f["status"] in ALARMS for f in learn), f"seed {seed}: learns first, no alarms ({dict(stats)})")
        normal = rig.run(600)
        bad = sum(f["status"] in ALARMS for f in normal)
        reds = sum("crit" in f["channels"].values() for f in normal)
        check(bad == 0, f"seed {seed}: no false alarms in 10 min of normal running ({bad})")
        check(reds == 0, f"seed {seed}: no sensor shown red in normal running ({reds})")
        check(abs(normal[-1]["normal"]["rpm_mean"] - 2880) < 30, "learned this pump's speed as normal")
        for fault in ("blockage", "imbalance", "overload"):
            rig.sim.set_scenario(fault)
            frames = rig.run(30)
            hit = next((i for i, f in enumerate(frames) if f["status"] in ALARMS), None)
            check(hit is not None, f"seed {seed}: {fault} raises an alarm" + (f" after {hit} s" if hit is not None else ""))
            rig.sim.set_scenario("normal")
            after = rig.run(150)
            check(after[-1]["status"] == "HEALTHY", f"seed {seed}: back to healthy after {fault}")


def test_bad_sensors():
    print("Broken sensors")

    def no_speed(m):
        m["rpm"] = 0.0
    rig = Rig(forest_cfg(), damage=no_speed)
    frames = rig.run(150)
    check(frames[-1]["running"] and frames[-1]["status"] == "HEALTHY", f"dead speed sensor: pump still seen as running ({frames[-1]['status']})")
    rig.sim.set_scenario("blockage")
    check(any(f["status"] in ALARMS for f in rig.run(30)), "dead speed sensor: a fault is still caught")

    def no_pressure(m):
        m["pressure_bar"] = -0.625
    rig = Rig(forest_cfg(), damage=no_pressure)
    frames = rig.run(150)
    check(frames[-1]["status"] == "HEALTHY" and frames[-1]["reading"]["pressure_raw"] < -0.3, "missing pressure sensor: monitoring continues and can be flagged")

    rig = Rig(forest_cfg())
    rig.run(140)
    rig.sim.t_ms = 0                                # ESP32 reset: millis() restarts
    frames = rig.run(20)
    check(frames[-1]["status"] == "HEALTHY" and not frames[-1]["learning_left"], "ESP32 reboot: keeps running and keeps what it learned")


def test_learning_safety():
    print("Learning period")
    rig = Rig(forest_cfg())
    rig.run(40)
    before = rig.p.scorer.learning_left
    rig.sim.set_scenario("dry_run")
    frames = rig.run(10)
    check(any(f["status"] == "DRY-RUN PROTECTION" for f in frames), "low water raises the alarm even while learning")
    check(rig.p.scorer.learning_left >= before - 3, "a dry pump is not learned as normal")
    rig.sim.set_scenario("normal")
    rig.sim.set_pump(True)
    rig.run(140)
    check(not rig.p.scorer.learning, "learning finishes once the pump is running normally")
    rig.p.relearn()
    check(rig.p.scorer.learning and rig.run(10)[-1]["status"] == "LEARNING", "Learn normal again starts over")


async def hub_checks():
    cfg = forest_cfg()
    hub = Hub(cfg)
    await hub.set_source("sim", announce=False)
    hub.task.cancel()
    sim = Simulator(cfg, seed=5)
    await hub.on_line(sim.line())
    hub.logger.start("test")
    hub.logger.fh.close()                           # like a removed USB stick or a full disk
    await hub.on_line(sim.line())
    check(not hub.logger.enabled, "CSV failure: recording stops and says so")
    n = len(hub.history)
    await hub.on_line(sim.line())
    check(len(hub.history) == n + 1, "CSV failure: monitoring continues")
    real, hub.processor.ingest = hub.processor.ingest, lambda *_: 1 / 0
    await hub.on_line(sim.line())
    hub.processor.ingest = real
    await hub.on_line(sim.line())
    check(len(hub.history) == n + 2 and hub.processor.counters["invalid_by_reason"].get("internal") == 1, "a pipeline bug on one message does not stop the stream")
    hub.source = type("Live", (), {"live": True})()
    hub._sync_scorer()
    check(not hub.scorer.fallback_on, "a real source switches off the hand-set stand-in")


def main():
    test_firmware_contract()
    test_fuzz()
    test_real_extremes()
    test_different_pump()
    test_bad_sensors()
    test_learning_safety()
    print("Server")
    asyncio.run(hub_checks())
    print("\nROBUSTNESS PASSED" if not failures else f"\nROBUSTNESS FAILED ({len(failures)}):\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
