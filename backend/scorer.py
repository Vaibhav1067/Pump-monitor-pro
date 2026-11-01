"""
Anomaly scorers. Every scorer maps an ordered feature dict to a score in [0, 1].

  DemoScorer            - standardised distance from the healthy baseline.
                          A transparent stand-in for Isolation Forest.
  IsolationForestScorer - pure-Python inference of a scikit-learn IsolationForest
                          exported to JSON by tools/train_iforest.py.
  RandomForestScorer    - pure-Python inference of a forest trained on real healthy and
                          faulty pump runs by tools/train_skab.py.

>>> TRAINED MODEL HOOK: load_scorer() below is the single place where the model is chosen.
"""
import json
import math
import os
import struct


def standardise(features, feat_cfg):
    """z_i = (f_i - baseline_i) / scale_i for every configured feature."""
    return {f["key"]: (features[f["key"]] - f["mean"]) / f["scale"] for f in feat_cfg}


class DemoScorer:
    """
    D     = sqrt( (1/N) * sum_i clip(z_i, -z_clip, +z_clip)^2 )
    score = 1 - exp( -(D / d0)^2 )

    D is the RMS standardised distance of the feature vector from the healthy
    baseline. With d0 = 2, a healthy rig (D ~ 0.5) scores ~0.06; D = 1.19 crosses
    the WARNING band (0.3) and D = 1.91 crosses CRITICAL (0.6).
    """
    name = "Demo scorer (standardised distance)"
    kind = "demo"

    def __init__(self, cfg):
        self.feat_cfg = cfg["features"]
        self.d0 = cfg["scorer"]["d0"]
        self.z_clip = cfg["scorer"]["z_clip"]

    def score(self, features):
        z = standardise(features, self.feat_cfg)
        zc = [max(-self.z_clip, min(self.z_clip, v)) for v in z.values()]
        d = math.sqrt(sum(v * v for v in zc) / len(zc))
        return 1.0 - math.exp(-(d / self.d0) ** 2), {"distance": d}


def _f32(v):
    """Round to float32: scikit-learn trees compare features as float32, so must we."""
    return struct.unpack("f", struct.pack("f", v))[0]


def _c(n):
    """Average path length of an unsuccessful BST search (Liu et al. 2008)."""
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * (math.log(n - 1.0) + 0.5772156649) - 2.0 * (n - 1.0) / n


class IsolationForestScorer:
    """
    Inference for a forest exported by tools/train_iforest.py. Format:
      {"type": "isolation_forest", "features": [...12 keys...],
       "max_samples": int,
       "trees": [{"left": [...], "right": [...], "feature": [...],
                  "threshold": [...], "n": [...]}, ...],
       "calibration": {"lo": float, "warn_raw": float, "crit_raw": float,
                       "warn": 0.3, "crit": 0.6}}
    raw   = 2 ** (-E[h(x)] / c(max_samples))      (~0.4 normal, -> 1 anomalous)
    score = piecewise-linear map of raw through three anchors:
              lo -> 0,  warn_raw -> warn,  crit_raw -> crit  (slope continues above, clipped to 1)
    The anchors are fitted by tools/train_iforest.py (see its docstring).
    """
    kind = "iforest"

    def __init__(self, model, path):
        if model.get("type") != "isolation_forest":
            raise ValueError("not an isolation_forest export")
        self.keys = model["features"]
        self.trees = model["trees"]
        self.cn = _c(model["max_samples"])
        cal = model["calibration"]
        self.anchors = [(cal["lo"], 0.0), (cal["warn_raw"], cal["warn"]), (cal["crit_raw"], cal["crit"])]
        self.name = f"Isolation Forest ({os.path.basename(path)}, {len(self.trees)} trees)"

    def raw(self, x):
        x = [_f32(v) for v in x]
        total = 0.0
        for t in self.trees:
            node, depth = 0, 0
            left, right = t["left"], t["right"]
            while left[node] != -1:
                node = left[node] if x[t["feature"][node]] <= t["threshold"][node] else right[node]
                depth += 1
            total += depth + _c(t["n"][node])
        return 2.0 ** (-(total / len(self.trees)) / self.cn)

    def score(self, features):
        r = self.raw([features[k] for k in self.keys])
        return self.calibrate(r), {"raw": r}

    def calibrate(self, r):
        (r0, s0), (r1, s1), (r2, s2) = self.anchors
        if r <= r1:
            s = s0 + (s1 - s0) * (r - r0) / (r1 - r0)
        else:
            s = s1 + (s2 - s1) * (r - r1) / (r2 - r1)
        return max(0.0, min(1.0, s))


def learn_baseline(rows, keys, floor_rel, floor_abs):
    """Per-feature (mean, sd) over this pump's own healthy windows. The sd is floored so a
    very quiet or quantised sensor cannot turn a tiny change into a huge deviation."""
    base = {}
    for k in keys:
        xs = [r[k] for r in rows]
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        base[k] = (m, max(sd, floor_rel * abs(m), floor_abs))
    return base


def relative(features, base, keys):
    """Feature vector in units of this pump's own healthy spread, comparable across pumps."""
    return [(features[k] - base[k][0]) / base[k][1] for k in keys]


class RandomForestScorer:
    """
    Inference for a forest trained on real healthy and faulty runs by tools/train_skab.py.
    Format:
      {"type": "random_forest", "features": [...], "baseline_windows": int,
       "floor_rel": float, "floor_abs": float,
       "trees": [{"left": [...], "right": [...], "feature": [...],
                  "threshold": [...], "p": [...]}, ...],
       "calibration": {"lo": float, "warn_raw": float, "crit_raw": float,
                       "warn": 0.3, "crit": 0.6}}
    The training pump differs from this one, so the forest only ever sees deviations from
    the running pump's own baseline. That baseline is learned from the first
    baseline_windows scored windows, and the demo scorer covers that period.
    """
    kind = "forest"

    def __init__(self, model, path, cfg):
        if model.get("type") != "random_forest":
            raise ValueError("not a random_forest export")
        self.keys = model["features"]
        self.trees = model["trees"]
        self.n_base = model["baseline_windows"]
        self.floor_rel, self.floor_abs = model["floor_rel"], model["floor_abs"]
        cal = model["calibration"]
        self.anchors = [(cal["lo"], 0.0), (cal["warn_raw"], cal["warn"]), (cal["crit_raw"], cal["crit"])]
        self.name = f"Random forest ({os.path.basename(path)}, {len(self.trees)} trees)"
        self.fallback = DemoScorer(cfg)
        self.reset()

    def reset(self):
        """A different data source is a different pump: learn its baseline again."""
        self.rows, self.base = [], None

    def raw(self, x):
        x = [_f32(v) for v in x]
        total = 0.0
        for t in self.trees:
            node = 0
            left, right = t["left"], t["right"]
            while left[node] != -1:
                node = left[node] if x[t["feature"][node]] <= t["threshold"][node] else right[node]
            total += t["p"][node]
        return total / len(self.trees)

    def score(self, features):
        if self.base is None:
            self.rows.append(features)
            left = self.n_base - len(self.rows)
            if left <= 0:
                self.base = learn_baseline(self.rows, self.keys, self.floor_rel, self.floor_abs)
                self.rows = []
            s, extras = self.fallback.score(features)
            return s, {**extras, "learning_left": max(0, left)}
        r = self.raw(relative(features, self.base, self.keys))
        return _calibrate(self.anchors, r), {"raw": r}


def _calibrate(anchors, r):
    (r0, s0), (r1, s1), (r2, s2) = anchors
    if r <= r1:
        s = s0 + (s1 - s0) * (r - r0) / (r1 - r0)
    else:
        s = s1 + (s2 - s1) * (r - r1) / (r2 - r1)
    return max(0.0, min(1.0, s))


def load_scorer(cfg, log=print):
    # ------------------------------------------------------------------------
    # TRAINED MODEL HOOK
    # Set CONFIG["scorer"]["model_path"] (or pass --model) to a JSON file produced
    # by tools/train_skab.py or tools/train_iforest.py and it replaces the demo scorer.
    # To use another runtime (e.g. onnxruntime), add a class with
    # .name, .kind and .score(features) -> (score_0_1, extras) and return it here.
    # ------------------------------------------------------------------------
    path = cfg["scorer"].get("model_path")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                model = json.load(fh)
            if model.get("type") == "random_forest":
                return RandomForestScorer(model, path, cfg)
            return IsolationForestScorer(model, path)
        except (OSError, ValueError, KeyError) as e:
            log(f"[scorer] could not load {path!r} ({e}); falling back to demo scorer")
    return DemoScorer(cfg)
