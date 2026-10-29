"""Thread caps for onnxruntime sessions (#123)."""

from __future__ import annotations

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
