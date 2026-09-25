"""The UVB clinical decision reads and verifies through one injectable HIS port."""

from datetime import date, timedelta

import main


class _FakeMemoPort:
    def __init__(self, *, read_back: bool = True):
        prior = date.today() - timedelta(days=3)
        self.text = (
            f"UVB: 800 mj/cm2 (35) on ({prior:%Y/%m/%d}) "
            "add 50 mj/cm2, MAX: 1500 mj/cm2"
        )
        self.read_back = read_back
        self.writes = []
        self.reads = 0

    def find_main_window(self):
        return 10

    def locate_phototherapy(self, main_hwnd):
        assert main_hwnd == 10
        return 20, "uvb"

    def read_memo(self, memo_hwnd):
        assert memo_hwnd == 20
        self.reads += 1
        if self.read_back and self.writes:
            return self.writes[-1]
        return self.text

    def write_memo(self, memo_hwnd, text):
        assert memo_hwnd == 20
        self.writes.append(text)
        return True

    def update_excimer(self, *_args):
        raise AssertionError("UVB text must not enter the Excimer workflow")


def _forbid_global_his(monkeypatch):
    def forbidden(*_args):
        raise AssertionError("global HIS access bypassed the injected port")

    for name in (
        "_find_hospital_main_window", "_resolve_phototherapy_disposition",
        "_read_tmemo_text", "_write_tmemo_text", "_f23_pure_excimer_update",
    ):
        monkeypatch.setattr(main, name, forbidden)
    monkeypatch.setattr(main, "check_stop", lambda: None)
    monkeypatch.setattr(main, "_record_his_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_record_uvb_write", lambda *_args, **_kwargs: None)


def test_injected_his_memo_port_requires_successful_read_back(monkeypatch):
    _forbid_global_his(monkeypatch)
    warnings = []
    monkeypatch.setattr(
        main, "_show_uvb_warning",
        lambda _hwnd, title, _msg: warnings.append(title),
    )
    port = _FakeMemoPort()

    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is True
    assert port.reads == 3  # initial, pre-write freshness, post-write proof
    assert len(port.writes) == 1
    assert "850 mj/cm2 (36)" in port.writes[0]
    assert warnings == []

    mismatched = _FakeMemoPort(read_back=False)
    assert main._update_uvb_dose_core(
        "F2", strict=True, memo_port=mismatched) is False
    assert mismatched.reads == 3
    assert len(mismatched.writes) == 1
    assert "UVB 寫回驗證失敗" in warnings
