"""Thread caps for onnxruntime sessions (#123)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from cleanplex import ort_runtime


def test_auto_threads_leave_headroom_and_never_drop_below_one():
    with patch.object(ort_runtime.os, "cpu_count", return_value=12):
        assert ort_runtime.auto_threads(3) == 3
        assert ort_runtime.auto_threads(1) == 6
    with patch.object(ort_runtime.os, "cpu_count", return_value=2):
        assert ort_runtime.auto_threads(8) == 1


def test_explicit_thread_setting_wins_over_auto():
    assert ort_runtime.configure(workers=3, threads=5) == 5
    assert ort_runtime.session_options().intra_op_num_threads == 5


def test_zero_threads_derives_from_workers():
    with patch.object(ort_runtime.os, "cpu_count", return_value=8):
        assert ort_runtime.configure(workers=3) == 2


def test_nudenet_session_is_rebuilt_with_the_cap():
    ort_runtime.configure(workers=2, threads=2)
    detector = MagicMock()
    with patch("onnxruntime.InferenceSession") as session:
        ort_runtime.limit_nudenet_threads(detector, "/models/640m.onnx")
    args, kwargs = session.call_args
    assert args[0] == "/models/640m.onnx"
    assert kwargs["sess_options"].intra_op_num_threads == 2
    assert detector.onnx_session is session.return_value


def test_failure_to_rebuild_keeps_the_original_session():
    detector = MagicMock()
    original = detector.onnx_session
    with patch("onnxruntime.InferenceSession", side_effect=RuntimeError("boom")):
        ort_runtime.limit_nudenet_threads(detector, "/models/x.onnx")
    assert detector.onnx_session is original


def test_every_session_is_built_without_intra_op_spinning():
    options = MagicMock()
    with patch("onnxruntime.SessionOptions", return_value=options):
        ort_runtime.session_options()
    options.add_session_config_entry.assert_called_once_with("session.intra_op.allow_spinning", "0")


def test_the_process_is_made_nicer_by_the_configured_amount(monkeypatch):
    monkeypatch.setenv("CLEANPLEX_NICE", "7")
    with patch.object(ort_runtime.os, "nice", return_value=7) as nice:
        assert ort_runtime.lower_process_priority() == 7
    nice.assert_called_once_with(7)


def test_the_default_niceness_is_ten(monkeypatch):
    monkeypatch.delenv("CLEANPLEX_NICE", raising=False)
    with patch.object(ort_runtime.os, "nice", return_value=10) as nice:
        ort_runtime.lower_process_priority()
    nice.assert_called_once_with(10)


@pytest.mark.parametrize("value", ["0", "-3"])
def test_zero_or_negative_leaves_the_priority_alone(monkeypatch, value):
    monkeypatch.setenv("CLEANPLEX_NICE", value)
    with patch.object(ort_runtime.os, "nice") as nice:
        assert ort_runtime.lower_process_priority() == 0
    nice.assert_not_called()


def test_a_garbage_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("CLEANPLEX_NICE", "lots")
    with patch.object(ort_runtime.os, "nice", return_value=10) as nice:
        ort_runtime.lower_process_priority()
    nice.assert_called_once_with(10)


def test_a_platform_without_nice_is_left_alone(monkeypatch):
    monkeypatch.delenv("CLEANPLEX_NICE", raising=False)
    monkeypatch.delattr(ort_runtime.os, "nice")
    assert ort_runtime.lower_process_priority() == 0


def test_a_refused_priority_change_does_not_raise(monkeypatch):
    monkeypatch.delenv("CLEANPLEX_NICE", raising=False)
    with patch.object(ort_runtime.os, "nice", side_effect=OSError("denied")):
        assert ort_runtime.lower_process_priority() == 0
