# FFmpeg Runtime

`ffmpeg.exe` and `ffprobe.exe` in this directory are bundled into the Windows Electron installer at `resources/runtime/ffmpeg/`.

The backend uses these files first in packaged mode. Development mode uses this directory when present and falls back to the system `PATH`.

Source build: Gyan.FFmpeg full build 9.0.2. The bundled license is in `LICENSE`.
