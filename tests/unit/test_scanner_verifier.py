"""The feedback-trained verifier drops flagged clusters that look like past false positives (#165)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import cleanplex.scanner as scanner
from cleanplex import verifier
from tests.unit.test_scanner_refine import Film, _job, _scan, _scores

pytestmark = pytest.mark.usefixtures("setup_db")


def _film():
    return Film(_scores({10_000: 0.9, 12_000: 0.9}))


async def test_a_cluster_the_verifier_rejects_is_not_inserted(tmp_path):
    await _job(tmp_path)
    with patch.object(verifier, "judge_jpeg", return_value=(True, 2.5)) as judge:
        segs, _ = await _scan(_film(), tmp_path, verifier_loss_pct=10)
    assert segs == []
    assert judge.call_args.args[1:] == ("nudenet", 10)


async def test_a_cluster_the_verifier_accepts_is_inserted(tmp_path):
    await _job(tmp_path)
    with patch.object(verifier, "judge_jpeg", return_value=(False, -1.0)):
        segs, _ = await _scan(_film(), tmp_path, verifier_loss_pct=10)
    assert len(segs) == 1


async def test_the_verifier_is_not_consulted_when_switched_off(tmp_path):
    await _job(tmp_path)
    with patch.object(verifier, "judge_jpeg", return_value=(True, 2.5)) as judge:
        segs, _ = await _scan(_film(), tmp_path, verifier_loss_pct=0)
    assert len(segs) == 1
    judge.assert_not_called()
