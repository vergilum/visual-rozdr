"""Build native, self-contained macOS FFmpeg tools from checksum-pinned sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "ffmpeg": (
        "https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz",
        "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1",
    ),
    "x264": (
        "https://codeload.github.com/mirror/x264/tar.gz/c24e06c2e184345ceb33eb20a15d1024d9fd3497",
        "090d730e867fc63631782a1287974635d1237d0fa7c6fd1d09fd543620a56689",
    ),
}


def source_tree(name: str, work: Path) -> Path:
    url, checksum = SOURCES[name]
    archive = work / f"{name}.tar"
    digest = hashlib.sha256()
    with urlopen(url, timeout=90) as response, archive.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != checksum:
        raise RuntimeError(f"Checksum mismatch for {name}")
    destination = work / name
    destination.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")
    return next(destination.iterdir())


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("Build these tools on macOS (locally or in GitHub Actions).")
    arch = "arm64" if platform.machine() == "arm64" else "x64"
    work = ROOT / "build" / "mac-media" / arch
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    prefix = work / "prefix"
    output = ROOT / "runtime" / "ffmpeg" / f"darwin-{arch}"
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": "13.0",
           "PKG_CONFIG_PATH": str(prefix / "lib" / "pkgconfig")}
    jobs = str(min(os.cpu_count() or 2, 4))

    def run(args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=cwd, env=env, check=True)

    x264 = source_tree("x264", work)
    run(["./configure", f"--prefix={prefix}", "--enable-static", "--enable-pic",
         "--disable-cli", "--disable-opencl", "--disable-asm"], x264)
    run(["make", "-j", jobs], x264)
    run(["make", "install"], x264)

    ffmpeg = source_tree("ffmpeg", work)
    run(["./configure", f"--prefix={prefix}", "--disable-shared", "--enable-static",
         "--disable-autodetect", "--disable-doc", "--disable-debug", "--disable-ffplay",
         "--disable-network", "--disable-x86asm", "--enable-gpl", "--enable-libx264",
         "--pkg-config-flags=--static"], ffmpeg)
    run(["make", "-j", jobs, "ffmpeg", "ffprobe"], ffmpeg)
    for tool in ("ffmpeg", "ffprobe"):
        target = output / tool
        shutil.copy2(ffmpeg / tool, target)
        target.chmod(0o755)
        dependencies = subprocess.check_output(["otool", "-L", str(target)], text=True)
        for line in dependencies.splitlines()[1:]:
            library = line.strip().split(" (", 1)[0]
            if not library.startswith(("/usr/lib/", "/System/Library/")):
                raise RuntimeError(f"Non-system dependency in {tool}: {library}")
        run(["codesign", "--force", "--sign", "-", str(target)], ROOT)
        run([str(target), "-version"], ROOT)
    shutil.copy2(ffmpeg / "COPYING.GPLv2", output / "FFmpeg-COPYING.GPLv2")
    shutil.copy2(x264 / "COPYING", output / "x264-COPYING")
    (output / "SOURCES.json").write_text(json.dumps(SOURCES, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: native {arch} FFmpeg tools built at {output}")


if __name__ == "__main__":
    main()
