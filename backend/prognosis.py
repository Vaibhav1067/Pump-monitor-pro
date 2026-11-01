"""
Failure prediction: how long until a slowly drifting sensor reaches its danger limit.

Fits a straight line to each sensor's recent history and extends it to the limit that raises
the alarm (learned normal +/- channel_z_crit tolerances). No flow sensor and no trained model:
it only needs the sensors the pump already has.

A sudden fault is the alarms' job. A jump followed by a flat line, or a warm-up that is levelling
off, is rejected here: both halves of the fitted stretch must be moving at a similar rate.
Time is counted in running time, so a stopped pump does not use up the estimate.
"""


def fit_line(ts, ys):
    """Least squares. Returns (slope, intercept, standard error of the slope)."""
    n = len(ts)
    mt, my = sum(ts) / n, sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return 0.0, my, float("inf")
    slope = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / sxx
    icpt = my - slope * mt
    sse = sum((y - icpt - slope * t) ** 2 for t, y in zip(ts, ys))
    return slope, icpt, (sse / max(1, n - 2) / sxx) ** 0.5


class Series:
    """Fixed memory history of one sensor: when full, neighbours merge and each point covers twice the time."""

    def __init__(self, keep, merge):
        self.keep, self.merge = keep, merge
        self.ts, self.ys = [], []
        self.width = 1
        self.acc = []

    def add(self, t, y):
        self.acc.append((t, y))
        if len(self.acc) < self.width:
            return
        self.ts.append(sum(a[0] for a in self.acc) / len(self.acc))
        self.ys.append(sum(a[1] for a in self.acc) / len(self.acc))
        self.acc = []
        if len(self.ts) <= self.keep:
            return
        if self.merge:
            self.ts = [(a + b) / 2 for a, b in zip(self.ts[::2], self.ts[1::2])]
            self.ys = [(a + b) / 2 for a, b in zip(self.ys[::2], self.ys[1::2])]
            self.width *= 2
        else:
            del self.ts[0], self.ys[0]


class Prognosis:
    def __init__(self, cfg):
        self.pc = cfg["prognosis"]
        self.limit_z = cfg["status"]["channel_z_crit"]
        self.channels = self.pc["channels"]
        self.reset()

    def reset(self):
        keep = self.pc["max_points"]
        self.run_s = 0.0
        self.since_start = 0.0
        self.acc, self.acc_n = {c["id"]: 0.0 for c in self.channels}, 0
        self.acc_s = 0.0
        self.points = 0
        # A recent fine history for fast wear and a coarse one that reaches back weeks.
        self.fast = {c["id"]: Series(keep, False) for c in self.channels}
        self.slow = {c["id"]: Series(keep, True) for c in self.channels}
        self.hits = {c["id"]: 0 for c in self.channels}
        self.miss = {c["id"]: 0 for c in self.channels}
        self.shown = {}

    # ------------------------------------------------------------------ fitting
    def _fit_channel(self, ch, normal, tol):
        """The best straight-line estimate for one sensor, or None if it is not drifting towards a limit."""
        pc, cid = self.pc, ch["id"]
        recent = self.fast[cid].ys[-3:]
        if len(recent) < 3 or self.since_start < ch.get("settle_s", 0):
            return None
        now = sum(recent) / len(recent)
        sign = 1 if now >= normal else -1
        if ch["dir"] and sign != ch["dir"]:
            return None
        if abs(now - normal) < pc["level_z"] * tol:
            return None
        limit = normal + sign * self.limit_z * tol
        if abs(now - normal) >= self.limit_z * tol:
            return {"eta": 0.0, "lo": 0.0, "hi": 0.0, "limit": limit, "reached": True,
                    "slope": 0.0, "now": now, "ts": self.fast[cid].ts[-60:], "ys": self.fast[cid].ys[-60:]}

        # Longest stretch first: it gives the narrowest estimate, and a stretch reaching back
        # before the drift began fails the equal-rate test and falls through to a shorter one.
        for s in (self.slow[cid], self.fast[cid]):
            n = len(s.ts)
            while n >= pc["min_points"]:
                est = self._fit_stretch(s.ts[-n:], s.ys[-n:], sign, limit)
                if est:
                    return est
                n //= 2
        return None

    def _fit_stretch(self, ts, ys, sign, limit):
        pc = self.pc
        slope, icpt, se = fit_line(ts, ys)
        if slope * sign <= 0 or abs(slope) < pc["min_t"] * se:
            return None
        mid = len(ts) // 2
        first = fit_line(ts[:mid], ys[:mid])[0] * sign
        second = fit_line(ts[mid:], ys[mid:])[0] * sign
        if min(first, second) < pc["half_ratio"] * abs(slope):
            return None
        if second > pc["speedup_ratio"] * first:      # speeding up: only the latest stretch tells the rate now
            return None
        now = icpt + slope * ts[-1]
        eta = (limit - now) / slope
        if eta <= 0 or eta > pc["max_eta_s"]:
            return None
        fast, slow = abs(slope) + pc["ci_t"] * se, max(abs(slope) - pc["ci_t"] * se, abs(slope) * 0.2)
        gap, m = abs(limit - now), pc["range_margin"]
        return {"eta": eta, "lo": min(gap / fast, eta * (1 - m)), "hi": max(gap / slow, eta * (1 + m)),
                "limit": limit, "reached": False, "slope": slope, "now": now, "ts": ts, "ys": ys}

    # ------------------------------------------------------------------ stream
    def push(self, features, normals, dt, since_start):
        """One scored reading from a running pump. normals: {feature key: (normal, tolerance)}."""
        self.since_start = since_start
        self.run_s += dt
        self.acc_s += dt
        self.acc_n += 1
        for c in self.channels:
            self.acc[c["id"]] += features[c["feature"]]
        if self.acc_s < self.pc["bucket_s"]:
            return
        self.points += 1
        for c in self.channels:
            y = self.acc[c["id"]] / self.acc_n
            self.fast[c["id"]].add(self.run_s, y)
            self.slow[c["id"]].add(self.run_s, y)
            self.acc[c["id"]] = 0.0
        self.acc_n, self.acc_s = 0, 0.0
        for c in self.channels:
            self._update(c, *normals[c["feature"]])

    def _update(self, ch, normal, tol):
        """A prediction shows after `confirm` estimates in a row and clears after as many misses."""
        cid, confirm = ch["id"], self.pc["confirm"]
        est = self._fit_channel(ch, normal, tol)
        if est:
            self.hits[cid], self.miss[cid] = self.hits[cid] + 1, 0
            if self.hits[cid] >= confirm or cid in self.shown or est["reached"]:
                self.shown[cid] = {**est, "at": self.run_s, "normal": normal}
        else:
            self.hits[cid], self.miss[cid] = 0, self.miss[cid] + 1
            if self.miss[cid] >= confirm:
                self.shown.pop(cid, None)

    # ------------------------------------------------------------------ output
    def report(self, learning):
        """What the dashboard shows. The soonest limit is the headline."""
        if learning:
            return {"state": "learning"}
        need = self.pc["min_points"] * self.pc["bucket_s"]
        ranked = []
        for c in self.channels:
            e = self.shown.get(c["id"])
            if e:
                gone = self.run_s - e["at"]
                ranked.append((max(0.0, e["eta"] - gone), c, e, gone))
        if not ranked:
            if self.points < self.pc["min_points"]:
                return {"state": "collecting", "left_s": round(need - self.points * self.pc["bucket_s"])}
            return {"state": "stable"}
        ranked.sort(key=lambda r: r[0])
        eta, c, e, gone = ranked[0]
        step = max(1, len(e["ts"]) // 60)
        return {
            "state": "reached" if e["reached"] else "trending",
            "sensor": c["id"], "word": c["word"], "unit": c["unit"], "dp": c["dp"],
            "eta_s": round(eta),
            "eta_lo_s": round(max(0.0, e["lo"] - gone)), "eta_hi_s": round(max(0.0, e["hi"] - gone)),
            "now": e["now"], "limit": e["limit"], "normal": e["normal"],
            "rate_per_h": e["slope"] * 3600,
            "history": [[round(t - self.run_s, 1), round(y, 4)]
                        for t, y in zip(e["ts"][::step], e["ys"][::step])],
            "others": [{"word": r[1]["word"], "eta_s": round(r[0])} for r in ranked[1:]],
        }


def duration(s):
    """Plain running time: 45 s, 3 min, 2.5 h, 4 days."""
    if s < 60:
        return f"{max(0, round(s))} s"
    if s < 3600:
        return f"{round(s / 60)} min"
    if s < 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"
