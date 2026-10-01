"""Missing cosmetic assets must not block Tk on network or image generation."""
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from cmuh_common import icons, window_icon


@pytest.mark.parametrize("cache", ["missing", "stale", "invalid", "small", "unreadable"])
def test_tk_icon_fallback_never_downloads_or_builds(monkeypatch, tmp_path, cache):
    monkeypatch.setattr(icons, "get_app_dir", lambda: str(tmp_path))
    assets = tmp_path / "assets"
    if cache != "missing":
        assets.mkdir()
        (assets / "cmuh_app.ico").write_bytes(b"x" * (20 if cache == "small" else 800))
        (assets / "cmuh_icon_version.txt").write_text(
            "invalid" if cache == "invalid" else
            str(icons._CMUH_ICON_ASSET_VERSION - (cache == "stale")), encoding="ascii")
    if cache == "unreadable":
        def denied(_path):
            raise PermissionError("synthetic icon permission denied")
        monkeypatch.setattr(icons.os.path, "getsize", denied)
    downloads = []
    def no_download(*args, **kwargs):
        downloads.append((args, kwargs))
        raise requests.Timeout("synthetic unavailable icon host")
    monkeypatch.setattr(requests, "get", no_download)
    applied = []
    root = SimpleNamespace(iconbitmap=lambda p: applied.append(p),
                           after=lambda *_: applied.append("timer"))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    window_icon.apply_tk_window_icon(root)
    assert downloads == []
    assert applied == []
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_valid_cached_icon_keeps_existing_apply_and_delayed_reapply(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(icons, "get_app_dir", lambda: str(tmp_path))
    assets = tmp_path / "assets"
    assets.mkdir()
    path = assets / "cmuh_app.ico"
    # Exercise the real shipped cache, not a mocked cache lookup.
    path.write_bytes((Path(__file__).resolve().parents[1] / "assets/cmuh_app.ico").read_bytes())
    (assets / "cmuh_icon_version.txt").write_text(str(icons._CMUH_ICON_ASSET_VERSION), encoding="ascii")
    applied, native, callbacks = [], [], []
    root = SimpleNamespace(iconbitmap=applied.append,
                           after=lambda delay, fn: callbacks.append((delay, fn)))
    monkeypatch.setattr(window_icon, "_apply_windows_wm_seticon_from_ico",
                        lambda owner, p: native.append((owner, p)))
    monkeypatch.setattr(window_icon, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(requests, "get", lambda *_a, **_k: pytest.fail("cached icon used network"))
    window_icon.apply_tk_window_icon(root)
    assert applied == ([] if platform == "nt" else [str(path)])
    assert [delay for delay, _ in callbacks] == [80, 400]
    for _, callback in callbacks:
        callback()
    assert native == [(root, str(path))] * 3


def test_explicit_icon_generation_remains_available(monkeypatch, tmp_path):
    monkeypatch.setattr(icons, "get_app_dir", lambda: str(tmp_path))
    attempts = []
    def unavailable(url, **kwargs):
        attempts.append((url, kwargs["timeout"]))
        raise requests.Timeout("synthetic icon download failure")
    monkeypatch.setattr(requests, "get", unavailable)
    assert icons.ensure_cmuh_app_icon_path() is None
    assert attempts == [(url, 30) for url in icons._CMU_LOGO_PNG_URLS]
