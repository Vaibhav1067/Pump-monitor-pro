"""
Real pump recordings through the whole live pipeline, judged by what the operator would see:
the same rule as the dashboard's findAlarm() decides whether a banner shows.

  python tools/fetch_skab.py
  python tools/test_realworld.py

SKAB has no speed sensor, so speed is held steady. Results describe SKAB's pump, not yours.
"""
import argparse
import copy
import csv
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.alarms import AlarmManager  # noqa: E402
from backend.config import CONFIG, SENSORS  # noqa: E402
from backend.processor import Processor  # noqa: E402
from backend.scorer import load_scorer  # noqa: E402
from tools.train_skab import family  # noqa: E402

CHANNELS = ("hall", "flow", "pressure", "current", "temp", "vib")


def banner(frame):
    """Mirrors findAlarm() in web/index.html for a pump with a live, fresh link."""
    st = frame["status"]
    far = sum(frame["channels"].get(c) == "crit" for c in CHANNELS)
    if st in ("CRITICAL", "DRY-RUN PROTECTION") or (st == "WARNING" and far >= 2):
        return "crit"
    if st == "WARNING" or (st == "HEALTHY" and far):
        return "warn"
    return None


def lines(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    t0 = datetime.fromisoformat(rows[0]["datetime"])
    for i, r in enumerate(rows):
        a1, a2 = float(r["Accelerometer1RMS"]), float(r["Accelerometer2RMS"])
        msg = {"ts": 1000 + int((datetime.fromisoformat(r["datetime"]) - t0).total_seconds() * 1000), "seq": i + 1,
               "rpm": 1480.0, "temp_c": float(r["Temperature"]), "current_a": float(r["Current"]),
               "flow_lpm": float(r["Volume Flow RateRMS"]), "pressure_bar": max(-0.6, float(r["Pressure"])),
               "vib_x": a1, "vib_y": a2, "vib_z": ((a1 * a1 + a2 * a2) / 2) ** 0.5,
               "vib_peak": 3 * max(a1, a2), "float": 1}
        yield json.dumps(msg), int(float(r.get("anomaly") or 0))


def replay(path, model=None):
    cfg = copy.deepcopy(CONFIG)
    cfg["scorer"]["model_path"] = model or os.path.join(ROOT, "models", "skab_forest.json")
    scorer = load_scorer(cfg)
    scorer.fallback_on = False
    p, am = Processor(cfg, scorer, SENSORS), AlarmManager(cfg, SENSORS)
    out = []
    for i, (line, label) in enumerate(lines(path)):
        res = p.ingest(line, 1.7e9 + i)
        if not res["ok"]:
            raise AssertionError(f"{path} row {i}: {res}")
        am.evaluate(am.frame_conditions(res["frame"]), 1.7e9 + i)
        out.append((res["frame"], label))
    return out


def rising(flags):
    n, prev = 0, False
    for f in flags:
        n += bool(f and not prev)
        prev = bool(f)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="model to test instead of the shipped one")
    ap.add_argument("--only", help="only runs whose path contains this")
    a = ap.parse_args()
    paths = sorted(glob.glob(os.path.join(ROOT, "data", "skab", "*", "*.csv")))
    if a.only:
        paths = [p for p in paths if a.only in p]
    if not paths:
        sys.exit("no data; run: python tools/fetch_skab.py")
    healthy_s = healthy_alarm_s = healthy_events = 0
    per = {}
    causes = Counter()
    print(f"{'run':28}{'false alarms':>13}{'/hour':>7}{'caught':>8}{'delay':>7}{'level':>7}")
    for path in paths:
        rel = os.path.relpath(path, os.path.join(ROOT, "data", "skab"))
        frames = replay(path, a.model)
        scored = [(f, y) for f, y in frames if f["status"] not in ("STARTING", "LEARNING")]
        ok = [(f, y) for f, y in scored if not y]
        bad = [(f, y) for f, y in scored if y]
        flags = [banner(f) for f, _ in ok]
        events = rising(flags)
        healthy_s += len(ok)
        healthy_alarm_s += sum(bool(x) for x in flags)
        healthy_events += events
        for f, _ in ok:
            if banner(f):
                far = [c for c in CHANNELS if f["channels"].get(c) == "crit"]
                causes[f"{f['status']}+{','.join(far) or 'score only'}"] += 1
        hit = next((i for i, (f, _) in enumerate(bad) if banner(f)), None)
        level = max((banner(f) or "" for f, _ in bad), key=lambda v: ("", "warn", "crit").index(v)) if bad else ""
        per.setdefault(family(rel), []).append(hit is not None if bad else None)
        rate = 3600 * events / max(1, len(ok))
        print(f"{rel:28}{events:>13}{rate:>7.0f}{'yes' if hit is not None else ('-' if not bad else 'NO'):>8}"
              f"{'' if hit is None else str(hit) + ' s':>7}{level:>7}")
    print(f"\nHealthy running: {healthy_s / 3600:.1f} hours, {healthy_events} false alarms "
          f"({3600 * healthy_events / healthy_s:.1f} per hour), banner up {100 * healthy_alarm_s / healthy_s:.1f} % of the time")
    print("Most common false-alarm causes (seconds):", causes.most_common(8))
    print("Faults caught:", {k: f"{sum(1 for x in v if x)}/{len(v)}" for k, v in sorted(per.items()) if k != "healthy"})


if __name__ == "__main__":
    main()
