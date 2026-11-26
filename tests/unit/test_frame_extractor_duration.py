"""get_duration_ms falls back to packet timestamps when the header has no duration (#225)."""
import asyncio

from cleanplex import frame_extractor as fe


class _Proc:
    def __init__(self, out: bytes):
        self._out = out
        self.returncode = 0

    async def communicate(self):
        return self._out, b""


def _patch(monkeypatch, outputs):
    calls = []

    async def fake_exec(*cmd, **kw):
        calls.append(cmd)
        return _Proc(outputs.pop(0))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(fe, "_FFPROBE_BIN", "/bin/sh")
    return calls


async def test_duration_uses_format_duration_when_present(monkeypatch):
    calls = _patch(monkeypatch, [b"12.5\n"])
    assert await fe.get_duration_ms("x.mkv") == 12500
    assert len(calls) == 1


async def test_duration_falls_back_to_last_packet_when_header_has_none(monkeypatch):
    _patch(monkeypatch, [b"N/A\n", b"4347.677000\n4347.760000\n"])
    assert await fe.get_duration_ms("x.mkv") == 4347760


async def test_duration_is_none_for_unreadable_file(monkeypatch):
    _patch(monkeypatch, [b"", b""])
    assert await fe.get_duration_ms("x.mp4") is None
