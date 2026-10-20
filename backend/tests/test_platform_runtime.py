from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import config


@pytest.mark.parametrize("machine,folder", [("arm64", "darwin-arm64"), ("x86_64", "darwin-x64")])
def test_native_mac_runtime_is_found_without_homebrew(tmp_path, monkeypatch, machine, folder):
    monkeypatch.setattr(config, "__file__", str(tmp_path / "backend" / "app" / "config.py"))
    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(config.platform, "machine", lambda: machine)
    monkeypatch.setattr(config.shutil, "which", lambda _: None)
    binary = tmp_path / "runtime" / "ffmpeg" / folder / "ffmpeg"
    binary.parent.mkdir(parents=True)
    binary.touch()
    assert config.resolve_tool_binary(None, "ffmpeg") == str(binary.resolve())
    explicit = tmp_path / "custom-ffmpeg"
    explicit.touch()
    assert config.resolve_tool_binary(explicit, "ffmpeg") == str(explicit.resolve())
