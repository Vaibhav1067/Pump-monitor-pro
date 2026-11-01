"""
Does the failure prediction show a useful warning, and stay quiet when it should? No rig needed:

  python tools/test_prognosis.py

  * synthetic gradual wear, steady and speeding up: warning time and error against what really happened
  * sudden faults, warm-ups, stops and starts, and hours of normal running: no prediction
  * real healthy SKAB recordings, if fetched: how often it would predict falsely

Everything except the SKAB part is synthetic, and that part has no slow failures in it.
"""
import copy
import glob
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.config import CONFIG, SENSORS  # noqa: E402
from backend.processor import Processor  # noqa: E402
from backend.scorer import load_scorer  # noqa: E402
from backend.simulator import Simulator  # noqa: E402
from tools.test_realworld import lines  # noqa: E402

failures = []
PREDICTS = ("trending", "reached")


def check(cond, what):
    print(f"  [{'PASS' if cond else 'FAIL'}] {what}")
    if not cond:
        failures.append(what)


class Rig:
    def __init__(self, seed=1, **wear):
        self.cfg = copy.deepcopy(CONFIG)
        self.cfg["scorer"]["model_path"] = os.path.join(ROOT, "models", "skab_forest.json")
        self.cfg["simulator"]["scenarios"]["wear"].update(wear)
        scorer = load_scorer(self.cfg, log=lambda *_: None)
        scorer.fallback_on = False
        self.p = Processor(self.cfg, scorer, SENSORS)
        self.sim = Simulator(self.cfg, seed=seed)
        self.n = 0

    def run(self, secs):
        out = []
        for _ in range(secs):
            self.sim.ramp_hold = self.p.scorer.learning
            self.n += 1
            out.append(self.p.ingest(self.sim.line(), 1.7e9 + self.n)["frame"])
        return out

    def settled(self):
        """Past learning and with the temperature levelled off."""
        self.run(500)
        return self


def wear_run(seed, **wear):
    """(seconds until first shown, true seconds to the limit, [(left, eta, lo, hi)]) for one wear run."""
    rig = Rig(seed, **wear).settled()
    rig.sim.set_scenario("wear")
    frames = rig.run(wear.get("ramp_s", 600) + 120)
    first = next((i for i, f in enumerate(frames) if f["prognosis"]["state"] == "trending"), None)
    limit = None
    for i, f in enumerate(frames):
        p = f["prognosis"]
        if p["state"] == "reached":
            limit = i
            break
    samples = []
    if first is not None and limit is not None:
        for i in range(first, limit):
            p = frames[i]["prognosis"]
            if p["state"] == "trending":
                samples.append((limit - i, p["eta_s"], p["eta_lo_s"], p["eta_hi_s"], p["sensor"]))
    return first, limit, samples


def errors(samples):
    """Average percent error of the estimate, and how often the real time fell inside the range."""
    pct = [abs(s[1] - s[0]) / s[0] for s in samples if s[0] >= 30]
    inside = [s[2] <= s[0] <= s[3] for s in samples if s[0] >= 30]
    return 100 * statistics.mean(pct), 100 * sum(inside) / len(inside)


def wear_report(name, seeds, **wear):
    rows = [wear_run(s, **wear) for s in seeds]
    ok = [r for r in rows if r[0] is not None and r[1] is not None and r[2]]
    check(len(ok) == len(rows), f"{name}: predicted before the limit in {len(ok)} of {len(rows)} runs")
    if not ok:
        return None
    lead = [r[1] - r[0] for r in ok]
    err = [errors(r[2]) for r in ok]
    print(f"    limit reached after {statistics.mean(r[1] for r in ok):.0f} s; warned {min(lead)}-{max(lead)} s ahead; "
          f"average error {statistics.mean(e[0] for e in err):.0f} %; real time inside the range {statistics.mean(e[1] for e in err):.0f} % of the time")
    return lead, err


def predicted_seconds(frames):
    return sum(f["prognosis"]["state"] in PREDICTS for f in frames)


def main():
    print("Synthetic gradual wear")
    r = wear_report("steady wear", range(1, 7))
    if r:
        check(min(r[0]) >= 120, f"at least 2 minutes of warning (shortest {min(r[0])} s)")
        check(statistics.mean(e[0] for e in r[1]) <= 30, "average error at most 30 %")
    wear_report("wear speeding up", range(1, 7), ramp_power=2.0, ramp_s=800)
    r = wear_report("wear 10 times slower", (1, 2), ramp_s=6000)
    if r:
        check(min(r[0]) >= 1200, f"slow wear warned at least 20 minutes ahead (shortest {min(r[0])} s)")

    print("Stays quiet")
    normal = Rig(1)
    frames = normal.run(6 * 3600)
    check(predicted_seconds(frames) == 0, f"6 hours of normal running: {predicted_seconds(frames)} s with a prediction")
    for fault in ("blockage", "imbalance", "overload"):
        for seed in (1, 2):
            rig = Rig(seed).settled()
            rig.sim.set_scenario(fault)
            frames = rig.run(400)
            early = sum(f["prognosis"]["state"] == "trending" for f in frames[120:])
            check(early == 0, f"sudden {fault} (seed {seed}): no countdown once its temperature has settled ({early} s)")
    for tau in (90, 600, 1800):
        rig = Rig(1)
        rig.cfg["simulator"]["tau_s"]["temp_running"] = tau
        rig.sim.x["temp_c"] = rig.cfg["baseline"]["ambient_c"]
        frames = rig.run(3 * tau + 300)
        print(f"    cold start, warm-up time constant {tau} s: temperature predicted for {predicted_seconds(frames)} s")
    rig = Rig(1).settled()
    seen = 0
    for _ in range(30):
        rig.sim.set_pump(False)
        rig.run(60)
        rig.sim.set_pump(True)
        seen += predicted_seconds(rig.run(240))
    print(f"    30 stops and starts: predicted for {seen} s of 7200 s")
    check(seen <= 720, "stops and starts rarely trigger it")

    print("Behaviour")
    rig = Rig(1).settled()
    rig.sim.set_scenario("wear")
    frames = rig.run(200)
    check(frames[-1]["prognosis"]["state"] == "trending", "wear shows a prediction")
    rig.sim.set_pump(False)
    stopped = rig.run(120)
    check(stopped[-1]["prognosis"]["eta_s"] == stopped[15]["prognosis"]["eta_s"] and stopped[-1]["prognosis"]["state"] == "trending",
          "a stopped pump does not use up the estimate")
    rig.sim.set_pump(True)
    rig.run(40)
    rig.sim.set_scenario("normal")
    frames = rig.run(120)
    check(frames[-1]["prognosis"]["state"] == "stable", "repaired: the prediction clears")
    msgs = [a["msg"] for a in rig.p.ingest(rig.sim.line(), 1.8e9)["alerts"]]
    check(not msgs, "no repeated events once clear")
    rig = Rig(1).settled()
    rig.sim.set_scenario("wear")
    rig.run(200)
    rig.p.relearn()
    frames = rig.run(1)
    check(frames[-1]["prognosis"]["state"] == "learning", "Learn normal again clears the prediction")
    rig = Rig(1)
    first = rig.run(10)[-1]["prognosis"]
    check(first["state"] == "learning", "while learning it says so instead of predicting")

    paths = sorted(glob.glob(os.path.join(ROOT, "data", "skab", "anomaly-free", "*.csv")))
    if paths:
        print("Real healthy pump data (SKAB)")
        for path in paths:
            cfg = copy.deepcopy(CONFIG)
            cfg["scorer"]["model_path"] = os.path.join(ROOT, "models", "skab_forest.json")
            scorer = load_scorer(cfg, log=lambda *_: None)
            scorer.fallback_on = False
            p = Processor(cfg, scorer, SENSORS)
            frames = [p.ingest(line, 1.7e9 + i)["frame"] for i, (line, _) in enumerate(lines(path))]
            shown = [f["prognosis"] for f in frames if f["prognosis"]["state"] in PREDICTS]
            words = sorted({s["word"] for s in shown})
            print(f"    {os.path.basename(path)}: {len(frames) / 3600:.1f} h, predicted for {len(shown)} s ({100 * len(shown) / len(frames):.0f} %) {words}")

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
