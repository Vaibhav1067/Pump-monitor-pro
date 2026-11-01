"""
Train a random forest on SKAB, real healthy and faulty runs from a water-pump test rig,
and export it to the JSON format backend/scorer.py:RandomForestScorer runs on the Pi.

  python tools/fetch_skab.py
  python tools/train_skab.py --out models/skab_forest.json

SKAB's pump runs at a different operating point from ours, and even between its own runs
(motor current about 2.4 A in the healthy file, about 1 A in the valve runs). So nothing is
trained on raw values: every window becomes a deviation from the first BASELINE_WINDOWS
windows of its own run, which is how the live scorer treats the running pump. SKAB has no
speed sensor or vibration peak, so rpm_mean and vib_peak are not model inputs.

Testing holds out whole experiments. "Seen" folds keep other runs of the same fault type
in training; "unseen" folds hold out the whole fault type. Results describe SKAB's pump,
not ours.
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.config import CONFIG  # noqa: E402
from backend.features import extract  # noqa: E402
from backend.scorer import RandomForestScorer, learn_baseline, relative  # noqa: E402

BASELINE_WINDOWS = 120
FLOOR_REL, FLOOR_ABS = 0.01, 1e-3
KEYS = [f["key"] for f in CONFIG["features"] if f["key"] not in ("rpm_mean", "vib_peak")]
WARN_FPR, CRIT_FPR = 0.01, 0.002     # share of held-out healthy windows allowed over each band


def family(rel):
    folder, name = rel.replace("\\", "/").split("/")
    n = int(name[:-4]) if name[:-4].isdigit() else 0
    if folder != "other":
        return {"anomaly-free": "healthy", "valve1": "inlet valve", "valve2": "outlet valve"}[folder]
    return ("leak/added fluid" if n <= 4 else "rotor imbalance" if n <= 9 else "water added" if n <= 11
            else "cavitation" if n <= 13 else "hot water")


def run_vectors(path):
    """-> (X, y) for one run, deviations from the run's own healthy start."""
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    size = CONFIG["window"]
    feats, labels, buf = [], [], []
    for row in rows:
        a1, a2 = float(row["Accelerometer1RMS"]), float(row["Accelerometer2RMS"])
        vib = ((a1 * a1 + a2 * a2) / 2) ** 0.5
        buf.append({"ts": datetime.fromisoformat(row["datetime"]).timestamp() * 1000, "rpm": 0.0,
                    "current_a": float(row["Current"]), "temp_c": float(row["Temperature"]),
                    "pressure_bar": float(row["Pressure"]),
                    "vib_rms": vib, "vib_peak": vib})
        buf = buf[-size:]
        if len(buf) == size:
            feats.append(extract(buf))
            labels.append(int(float(row.get("anomaly") or 0)))
    if any(labels[:BASELINE_WINDOWS]):
        return None
    base = learn_baseline(feats[:BASELINE_WINDOWS], KEYS, FLOOR_REL, FLOOR_ABS)
    return ([relative(f, base, KEYS) for f in feats[BASELINE_WINDOWS:]], labels[BASELINE_WINDOWS:])


def fit(np, X, y, trees, seed):
    from sklearn.ensemble import RandomForestClassifier
    return RandomForestClassifier(n_estimators=trees, max_depth=8, min_samples_leaf=10, max_features="sqrt",
                                  class_weight="balanced_subsample", random_state=seed, n_jobs=-1).fit(X, y)


def formula_score(np, X):
    """The hand-set formula on the same deviations, for comparison."""
    d = np.sqrt(np.mean(np.clip(X, -8, 8) ** 2, axis=1))
    return 1 - np.exp(-(d / 2) ** 2)


def confirmed(flags):
    """Status debounce: a band counts from its second consecutive window."""
    out, prev = [], False
    for f in flags:
        out.append(bool(f and prev))
        prev = bool(f)
    return out


def evaluate(np, runs, groups, trees, seed):
    """Out-of-fold forest probabilities for each run, holding out one group at a time."""
    p = {}
    for g in sorted(set(groups.values())):
        train = [r for r in runs if groups[r] != g]
        X = np.vstack([runs[r][0] for r in train])
        y = np.concatenate([runs[r][1] for r in train])
        m = fit(np, X, y, trees, seed)
        for r in runs:
            if groups[r] == g:
                p[r] = m.predict_proba(runs[r][0])[:, 1]
    return p


def limits(np, runs, scores, groups):
    """Alarm limit for each run, set only from healthy windows of the other folds, so the
    limit never sees the run it is tested on. Any scorer gets the same false-alarm target."""
    out = {}
    for g in set(groups.values()):
        others = [scores[r][runs[r][1] == 0] for r in runs if groups[r] != g]
        q = float(np.quantile(np.concatenate(others), 1 - WARN_FPR))
        out.update({r: q for r in runs if groups[r] == g})
    return out


def report(np, title, runs, scores, warn):
    from sklearn.metrics import roc_auc_score
    print(f"\n{title}\n{'fault':18}{'runs':>5}{'caught':>8}{'median delay':>14}{'AUC':>7}")
    fams = sorted({family(r) for r in runs} - {"healthy"})
    for fam in fams:
        rs = [r for r in runs if family(r) == fam]
        caught, delays = 0, []
        for r in rs:
            y = runs[r][1]
            if not y.any():
                continue
            on = np.argmax(y == 1)
            hit = [i for i, c in enumerate(confirmed(scores[r] >= warn[r])) if c and y[i]]
            if hit:
                caught += 1
                delays.append(hit[0] - on)
        y = np.concatenate([runs[r][1] for r in rs])
        s = np.concatenate([scores[r] for r in rs])
        auc = roc_auc_score(y, s) if 0 < y.sum() < len(y) else float("nan")
        delay = f"{np.median(delays):.0f} s" if delays else "-"
        print(f"{fam:18}{len(rs):>5}{caught:>5}/{len(rs):<2}{delay:>14}{auc:>7.2f}")
    y = np.concatenate([runs[r][1] for r in runs])
    s = np.concatenate([scores[r] for r in runs])
    pred = np.concatenate([np.array(confirmed(scores[r] >= warn[r])) for r in runs])
    tp, fp = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum())
    false_starts = 0
    for r in runs:
        c = np.array(confirmed(scores[r] >= warn[r]), dtype=int)
        starts = np.flatnonzero(np.diff(c, prepend=0) == 1)
        false_starts += int(sum(runs[r][1][i] == 0 for i in starts))
    healthy_h = (y == 0).sum() / 3600
    print(f"all: AUC {roc_auc_score(y, s):.3f}, faulty windows flagged {100 * tp / max(1, int(y.sum())):.1f} %, "
          f"healthy windows flagged {100 * fp / max(1, int((y == 0).sum())):.1f} %, "
          f"false alarms {false_starts / healthy_h:.1f} per hour of healthy running")
    return {"auc": float(roc_auc_score(y, s)), "recall": tp / max(1, int(y.sum())),
            "fpr": fp / max(1, int((y == 0).sum())), "false_alarms_per_h": false_starts / healthy_h}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/skab")
    ap.add_argument("--out", default="models/skab_forest.json")
    ap.add_argument("--trees", type=int, default=40)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    import numpy as np

    paths = sorted(glob.glob(os.path.join(a.data, "*", "*.csv")))
    if not paths:
        sys.exit(f"no CSV files in {a.data}; run: python tools/fetch_skab.py")
    runs = {}
    for p in paths:
        rel = os.path.relpath(p, a.data)
        v = run_vectors(p)
        if v is None:
            print(f"skipped {rel}: fault starts inside the baseline period")
            continue
        runs[rel] = (np.asarray(v[0], dtype=float), np.asarray(v[1], dtype=int))
    n = sum(len(v[1]) for v in runs.values())
    bad = sum(int(v[1].sum()) for v in runs.values())
    print(f"{len(runs)} runs, {n} windows: {n - bad} healthy, {bad} faulty")

    run_folds = {r: i % 5 for i, r in enumerate(runs)}
    fault_folds = {r: family(r) for r in runs}
    by_run = evaluate(np, runs, run_folds, a.trees, a.seed)
    by_fault = evaluate(np, runs, fault_folds, a.trees, a.seed)

    healthy = np.concatenate([by_run[r][runs[r][1] == 0] for r in runs])
    warn_raw = float(np.quantile(healthy, 1 - WARN_FPR))
    crit_raw = float(np.quantile(healthy, 1 - CRIT_FPR))
    lo = float(np.median(healthy))
    crit_raw = max(crit_raw, warn_raw + 1e-3)
    warn_raw = max(warn_raw, lo + 1e-3)
    f_scores = {r: formula_score(np, runs[r][0]) for r in runs}
    summary = {
        "forest_other_runs_held_out": report(np, "FOREST, whole runs held out", runs, by_run,
                                             limits(np, runs, by_run, run_folds)),
        "forest_fault_type_held_out": report(np, "FOREST, whole fault type held out (never seen)", runs, by_fault,
                                             limits(np, runs, by_fault, fault_folds)),
        "formula_same_data": report(np, "FORMULA, same deviations and same false-alarm target", runs, f_scores,
                                    limits(np, runs, f_scores, run_folds)),
    }

    X = np.vstack([v[0] for v in runs.values()])
    y = np.concatenate([v[1] for v in runs.values()])
    m = fit(np, X, y, a.trees, a.seed)
    trees = []
    for est in m.estimators_:
        t = est.tree_
        v = t.value[:, 0, :]
        trees.append({"left": t.children_left.tolist(), "right": t.children_right.tolist(),
                      "feature": [int(f) for f in t.feature], "threshold": [float(th) for th in t.threshold],
                      "p": [round(float(q), 5) for q in v[:, 1] / v.sum(axis=1)]})
    st = CONFIG["status"]
    export = {"type": "random_forest", "features": KEYS, "baseline_windows": BASELINE_WINDOWS,
              "floor_rel": FLOOR_REL, "floor_abs": FLOOR_ABS, "trees": trees,
              "calibration": {"lo": lo, "warn_raw": warn_raw, "crit_raw": crit_raw,
                              "warn": st["warn"], "crit": st["crit"]},
              "trained_on": "SKAB, github.com/waico/SKAB (real test rig; GPL-3.0)", "n_train": len(X),
              "test_results": summary}

    # Parity check: pure-Python inference must match scikit-learn.
    py = RandomForestScorer(export, a.out, CONFIG)
    diffs = [abs(py.raw(list(x)) - q) for x, q in zip(X[:300], m.predict_proba(X[:300])[:, 1])]
    if max(diffs) > 1e-3:
        sys.exit(f"parity check failed: max |python - sklearn| = {max(diffs):.2e}")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(export, fh)
    print(f"\ncalibration lo={lo:.3f} warn={warn_raw:.3f} crit={crit_raw:.3f}; parity max diff {max(diffs):.1e}")
    print(f"wrote {a.out} ({os.path.getsize(a.out) / 1024:.0f} KiB). Run: python -m backend --model {a.out}")


if __name__ == "__main__":
    main()
