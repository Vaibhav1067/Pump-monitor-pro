"""
Engineering values and rule-based fault diagnosis. Pure functions, no I/O.

engineering() turns the window features into the quantities a pump engineer reads:
estimated electrical input, head, motor slip and vibration crest factor.

diagnose() explains an anomaly: it compares the standardised feature deviation
vector z with a signature vector per known fault (cosine similarity). It is a
transparent rule base, not a trained classifier, and is labelled that way in the UI.
"""
import math

G = 9.80665


def engineering(reading, features, running, cfg):
    e = cfg["engineering"]
    f = features
    p = f["pressure_mean"] if f else reading["pressure_bar"]
    i = f["current_rms"] if f else reading["current_a"]
    n = f["rpm_mean"] if f else reading["rpm"]
    vr = f["vib_rms"] if f else reading["vib_rms"]
    vp = f["vib_peak"] if f else reading["vib_peak"]

    p_in = e["supply_v"] * i * e["power_factor"]          # W   (estimate: V·I·PF)
    n_sync = 120.0 * e["line_hz"] / e["motor_poles"]
    out = {
        "p_in_w": p_in,
        "head_m": p * 1e5 / (e["density_kg_m3"] * G),
        "slip_pct": (100.0 * (n_sync - n) / n_sync) if running else None,
        "n_sync_rpm": n_sync,
        "crest": (vp / vr) if vr > 1e-3 else None,
        "p_bar": p,
    }
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def diagnose(z, reading, status, cfg):
    """Returns None when there is nothing to explain, else the ranked fault hypotheses."""
    d = cfg["diagnosis"]
    if reading["float"] == 0:
        fault = d["faults"]["dry_run"]
        return {"fault": "dry_run", "label": fault["label"], "confidence": 1.0,
                "action": fault["action"], "basis": "float switch LOW (interlock)",
                "ranking": [{"fault": "dry_run", "label": fault["label"], "similarity": 1.0}]}
    if z is None or status not in ("WARNING", "CRITICAL"):
        return None
    clip = cfg["scorer"]["z_clip"]
    zc = {k: max(-clip, min(clip, v)) for k, v in z.items()}
    zn = math.sqrt(sum(v * v for v in zc.values())) or 1.0
    ranking = []
    for key, fault in d["faults"].items():
        sig = fault.get("signature")
        if not sig:
            continue
        sn = math.sqrt(sum(v * v for v in sig.values()))
        sim = sum(zc.get(k, 0.0) * w for k, w in sig.items()) / (zn * sn)
        ranking.append({"fault": key, "label": fault["label"], "similarity": round(max(0.0, sim), 3)})
    ranking.sort(key=lambda r: -r["similarity"])
    best = ranking[0]
    if best["similarity"] < d["min_similarity"]:
        return {"fault": "unclassified", "label": "Unclassified anomaly", "confidence": best["similarity"],
                "action": d["unclassified_action"], "basis": "no known signature matches well",
                "ranking": ranking[:4]}
    fault = d["faults"][best["fault"]]
    top = sorted(zc.items(), key=lambda kv: -abs(kv[1]))[:3]
    basis = ", ".join(f"{k} z={v:+.1f}" for k, v in top)
    return {"fault": best["fault"], "label": fault["label"], "confidence": best["similarity"],
            "action": fault["action"], "basis": basis, "ranking": ranking[:4]}
