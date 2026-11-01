"""
Download SKAB (Skoltech Anomaly Benchmark), real labelled data from a water-pump test rig,
into data/skab/ (git-ignored). Used by tools/train_skab.py.

  python tools/fetch_skab.py

Source: https://github.com/waico/SKAB (GPL-3.0). Katser & Kozitsin (2020),
DOI 10.34740/KAGGLE/DSV/1693952. Only the CSV files are downloaded.
"""
import os
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/waico/SKAB/master/data/"
FILES = (["anomaly-free/anomaly-free.csv"]
         + [f"valve1/{i}.csv" for i in range(16)]
         + [f"valve2/{i}.csv" for i in range(4)]
         + [f"other/{i}.csv" for i in range(1, 15)])


def main():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "skab")
    total = 0
    for rel in FILES:
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with urllib.request.urlopen(BASE + rel, timeout=60) as r, open(path + ".part", "wb") as fh:
                fh.write(r.read())
            os.replace(path + ".part", path)
        total += os.path.getsize(path)
    print(f"{len(FILES)} files, {total / 1e6:.1f} MB in {root}")


if __name__ == "__main__":
    sys.exit(main())
