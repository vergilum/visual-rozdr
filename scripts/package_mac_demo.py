"""Check the distributable ZIP after extraction and write a delivery manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

from smoke_desktop_app import check as check_gui

ROOT = Path(__file__).resolve().parents[1]


def main(arch: str) -> None:
    output = ROOT / "artifacts" / "mac-demo"
    version = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    release_id = version.replace(".", "")
    archive = output / f"VisualPozdr-mac-{arch}-{release_id}.zip"
    guide = ROOT / "docs" / "MAC_DEMO_USER_GUIDE.md"
    with zipfile.ZipFile(archive, "a") as bundle:
        bundle.write(guide, "READ-ME-MAC.md")
    with tempfile.TemporaryDirectory(prefix="visual-pozdr-zip-") as temporary:
        subprocess.run(["ditto", "-x", "-k", str(archive), temporary], check=True)
        app = Path(temporary) / "Visual Pozdr.app"
        check_gui(app)
    delivery = output / "delivery"
    delivery.mkdir(exist_ok=True)
    shutil.copy2(archive, delivery / archive.name)
    shutil.copy2(guide, delivery / "READ-ME-MAC.md")
    with archive.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    info = {
        "version": version,
        "architecture": arch, "build_os": platform.mac_ver()[0], "commit": commit,
        "workflow_run": os.environ.get("GITHUB_RUN_ID"), "archive": archive.name,
        "bytes": archive.stat().st_size, "sha256": checksum,
        "signing": "ad-hoc; no Developer ID or Apple notarization",
        "automated_checks": ["backend tests", "Electron platform tests", "packaged backend",
                             "H.264/AAC/JPEG", "Electron windows/themes/QR", "ZIP roundtrip"],
        "manual_checks_pending": ["Golden Gate 27.0.1", "real Telegram", "sound output", "projector"],
    }
    (delivery / "BUILD-INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    (delivery / "SHA256SUMS.txt").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arch", choices=["arm64", "x64"])
    main(parser.parse_args().arch)
