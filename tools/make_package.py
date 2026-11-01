"""
Build the zip to send to someone who will run this on their own hardware.

  python tools/make_package.py          -> build/PumpMonitorPro.zip

Only what's needed to run it goes in. The virtual environment, recordings, the SKAB
download and design notes stay out.
"""
import glob
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INCLUDE = ["FRIEND_SETUP.md", "README.md", "requirements.txt", "requirements-train.txt", "start.bat", "flash.bat",
           "check.bat", "share.bat", "backend/*.py", "web/index.html", "web/base.css", "web/tokens.css",
           "firmware/esp32_pump_node/*.ino", "models/*.json", "data/sample_session.csv", "deploy/*", "tools/*.py",
           "tools/firmware_host/*", "tools/*.sh", "logs/.gitkeep"]


def main():
    out = os.path.join(ROOT, "build", "PumpMonitorPro.zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for pattern in INCLUDE:
            matches = glob.glob(os.path.join(ROOT, pattern))
            if not matches:
                sys.exit(f"missing from the package: {pattern}")
            for path in matches:
                rel = os.path.relpath(path, ROOT).replace("\\", "/")
                z.write(path, f"PumpMonitorPro/{rel}")
                count += 1
    print(f"{count} files, {os.path.getsize(out) / 1e6:.1f} MB -> {out}")


if __name__ == "__main__":
    main()
