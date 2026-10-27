"""NudeNet model files are validated and load failures fall back loudly (issue #97)."""

from __future__ import annotations

import logging

import httpx
import pytest

import cleanplex.scanner as scanner

HTML = b"<!DOCTYPE html>\n<html lang='en'>not a model</html>"


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "MODELS_DIR", tmp_path)
    return tmp_path


def _sparse(path, size, head=b"\x08\x07ONNX"):
    with open(path, "wb") as f:
        f.write(head)
        f.truncate(size)


def test_an_html_page_is_not_a_valid_model(tmp_path):
    page = tmp_path / "640m.onnx"
    _sparse(page, scanner.NUDENET_640_MIN_BYTES + 1, head=HTML)

    assert scanner._valid_model_file(page) is False


def test_a_truncated_download_is_not_a_valid_model(tmp_path):
    stub = tmp_path / "640m.onnx"
    _sparse(stub, 49_630)

    assert scanner._valid_model_file(stub) is False


def test_a_full_size_binary_file_is_accepted(tmp_path):
    model = tmp_path / "640m.onnx"
    _sparse(model, scanner.NUDENET_640_MIN_BYTES + 1)

    assert scanner._valid_model_file(model) is True


def test_download_points_at_the_published_weights():
    # The API asset URL first: GitHub sends anonymous browser-link downloads to a login page.
    assert scanner.NUDENET_640_DOWNLOAD_URLS[0].startswith("https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/")
    assert scanner.NUDENET_640_DOWNLOAD_URLS[1].endswith("/v3.4-weights/640m.onnx")


def test_download_asks_for_the_binary_not_a_page(models_dir, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers.get("accept"))
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(scanner.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    scanner._ensure_local_640m_model()

    assert seen and all(a == "application/octet-stream" for a in seen)


def _serve(monkeypatch, content_type, body):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    real_client = httpx.Client
    monkeypatch.setattr(scanner.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))


def test_an_existing_html_file_is_discarded_and_an_html_reply_rejected(models_dir, monkeypatch):
    (models_dir / "640m.onnx").write_bytes(HTML)
    _serve(monkeypatch, "text/html; charset=utf-8", HTML)

    assert scanner._ensure_local_640m_model() == ""
    assert not (models_dir / "640m.onnx").exists()
    assert not (models_dir / "640m.onnx.part").exists()


def test_an_undersized_binary_reply_is_rejected(models_dir, monkeypatch):
    _serve(monkeypatch, "application/octet-stream", b"\x08\x07" + b"0" * 1000)

    assert scanner._ensure_local_640m_model() == ""
    assert not (models_dir / "640m.onnx").exists()


def test_a_model_that_will_not_load_falls_back_to_320n_with_an_error(tmp_path, monkeypatch, caplog):
    """Regression for #97: a broken model must not make every frame look clean."""
    broken = tmp_path / "custom.onnx"
    broken.write_bytes(HTML)
    monkeypatch.setattr(scanner._thread_local, "nude_detector", None, raising=False)

    with caplog.at_level(logging.ERROR, logger="cleanplex.scanner"):
        detector = scanner._get_detector(model_name="320n", model_path=str(broken))

    assert detector is not None
    assert scanner._thread_local.nude_detector_key == (320, "")
    assert any("falling back to bundled 320n" in r.message for r in caplog.records)
