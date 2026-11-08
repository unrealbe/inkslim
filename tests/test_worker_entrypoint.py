from __future__ import annotations

import logging

from conversion import __main__ as worker_main


def test_worker_entrypoint_builds_store_processor_and_runs(monkeypatch, tmp_path):
    calls = {}

    class FakeStore:
        def __init__(self, database_path, jobs_dir, *, max_storage_bytes, retention_hours):
            calls["database_path"] = database_path
            calls["jobs_dir"] = jobs_dir
            calls["max_storage_bytes"] = max_storage_bytes
            calls["retention_hours"] = retention_hours

    class FakeProcessor:
        def __init__(self, render_limits, *, timeout_seconds, native_text_sample_interval):
            calls["render_limits"] = render_limits
            calls["timeout_seconds"] = timeout_seconds
            calls["native_text_sample_interval"] = native_text_sample_interval

    class FakeWorker:
        def __init__(self, store, processor, *, poll_seconds):
            calls["store"] = store
            calls["processor"] = processor
            calls["poll_seconds"] = poll_seconds

        def run_forever(self):
            calls["ran"] = True

    monkeypatch.setenv("INKSLIM_DATA_DIR", str(tmp_path / "conversion"))
    monkeypatch.setenv("QUEUE_POLL_SECONDS", "3.5")
    monkeypatch.setenv("PDF_RENDER_DPI", "240")
    monkeypatch.setenv("MIN_RENDER_DPI", "100")
    monkeypatch.setenv("MAX_RENDER_PIXELS", "40000000")
    monkeypatch.setenv("MAX_RENDER_DIMENSION", "11000")
    monkeypatch.setattr(worker_main, "JobStore", FakeStore)
    monkeypatch.setattr(worker_main, "PdfConversionJobProcessor", FakeProcessor)
    monkeypatch.setattr(worker_main, "ConversionWorker", FakeWorker)

    worker_main.main()

    assert calls["database_path"] == tmp_path / "conversion" / "jobs.sqlite3"
    assert calls["jobs_dir"] == tmp_path / "conversion" / "jobs"
    assert calls["max_storage_bytes"] == 5 * 1024**3
    assert calls["retention_hours"] == 24
    assert calls["render_limits"].dpi == 240
    assert calls["render_limits"].minimum_dpi == 100
    assert calls["render_limits"].max_pixels == 40_000_000
    assert calls["render_limits"].max_dimension == 11_000
    assert calls["poll_seconds"] == 3.5
    assert calls["timeout_seconds"] == 120
    assert calls["native_text_sample_interval"] == 10
    assert calls["ran"] is True
