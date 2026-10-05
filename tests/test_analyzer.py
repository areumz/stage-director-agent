import hashlib
import io
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile

from stage_director.analyzer import (
    MAX_ANALYSIS_AUDIO_BYTES,
    AnalysisError,
    AnalysisRunner,
    build_result,
)
from stage_director.audio import AudioError
from stage_director.jobs import InMemoryJobStore
from stage_director.runner import RunError
from tests.conftest import DeferredExecutor, InlineExecutor

URL = "https://abc.supabase.co/storage/v1/object/sign/audio/a.mp3?token=t"
RESULT = {"analysis": {"durationSec": 12.5, "bpm": 100.0, "beatsSec": [], "energyCurve": [0.1], "onsetDensity": [1.0]}, "fileHash": "h"}


class Recorder:
    """fetch·build 를 대신하는 가짜. 부른 횟수와 받은 인자를 남긴다."""

    def __init__(self, *, fetch_error=None, build_error=None):
        self.fetch_error, self.build_error = fetch_error, build_error
        self.fetch_calls, self.build_calls = [], []

    def fetch(self, url, **kwargs):
        self.fetch_calls.append((url, kwargs))
        if self.fetch_error:
            raise self.fetch_error
        return b"audio-bytes", "audio/mpeg"

    def build(self, data, mime):
        self.build_calls.append((data, mime))
        if self.build_error:
            raise self.build_error
        return RESULT


def make(rec=None, executor=None, allowed=("supabase.co",)):
    rec = rec or Recorder()
    jobs = InMemoryJobStore()
    runner = AnalysisRunner(jobs, executor or InlineExecutor(), fetch=rec.fetch, build=rec.build, allowed_hosts=allowed)
    return runner, jobs, rec


# ── 작업 흐름 ─────────────────────────────────────────────────


def test_start_runs_to_done_with_the_result_and_full_progress():
    runner, _, rec = make()
    status = runner.start("a1", URL)
    assert (status.status, status.progress, status.result, status.error) == ("done", 1.0, RESULT, None)
    assert rec.fetch_calls[0][1] == {"max_bytes": MAX_ANALYSIS_AUDIO_BYTES, "allowed_hosts": ("supabase.co",)}
    assert rec.build_calls == [(b"audio-bytes", "audio/mpeg")]


def test_start_is_idempotent_and_does_not_analyze_twice():
    runner, _, rec = make()
    runner.start("a1", URL)
    assert runner.start("a1", URL).status == "done"
    assert len(rec.fetch_calls) == 1


def test_a_waiting_analysis_is_queued_until_a_thread_picks_it_up():
    executor = DeferredExecutor()
    runner, _, _ = make(executor=executor)
    assert runner.start("a1", URL).status == "queued"
    executor.run_all()
    assert runner.status("a1").status == "done"


def test_a_job_failed_while_queued_never_runs():
    executor = DeferredExecutor()
    runner, jobs, rec = make(executor=executor)
    runner.start("a1", URL)
    jobs.fail_running()  # 다른 인스턴스의 시작 정리가 이 작업을 먼저 error 로 바꿨다
    assert runner.status("a1").error == "interrupted"
    executor.run_all()
    assert rec.fetch_calls == [] and runner.status("a1").status == "error"


def test_invalid_urls_are_rejected_before_anything_is_queued():
    runner, jobs, rec = make()
    with pytest.raises(RunError) as e:
        runner.start("a1", "https://evil.example.com/a.mp3")
    assert (e.value.status, e.value.code) == (422, "invalid_audio_url")
    assert jobs.get("a1") is None and rec.fetch_calls == []


# ── 실패와 다시 시도 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("rec", "code"),
    [
        (Recorder(fetch_error=AudioError("HTTP 403")), "audio_unavailable"),
        (Recorder(build_error=AnalysisError("too_long")), "too_long"),
        (Recorder(build_error=AnalysisError("decode_failed")), "decode_failed"),
        (Recorder(build_error=RuntimeError("secret detail")), "internal_error"),
    ],
)
def test_failures_become_short_error_codes_without_details(rec, code):
    runner, _, _ = make(rec)
    status = runner.start("a1", URL)
    assert (status.status, status.error, status.result) == ("error", code, None)


def test_start_again_after_an_error_analyzes_from_scratch():
    runner, _, rec = make(Recorder(fetch_error=AudioError("HTTP 503")))
    assert runner.start("a1", URL).status == "error"
    rec.fetch_error = None
    status = runner.start("a1", URL)
    assert (status.status, status.error, status.result) == ("done", None, RESULT)
    assert len(rec.fetch_calls) == 2


def test_graph_job_ids_are_unknown_analysis_jobs():
    runner, jobs, _ = make()
    jobs.create("t1")  # kind=graph
    for call in (lambda: runner.status("t1"), lambda: runner.start("t1", URL)):
        with pytest.raises(RunError) as e:
            call()
        assert (e.value.status, e.value.code) == (404, "job_not_found")
    assert jobs.get("t1").status == "running"


def test_unknown_job_is_404():
    runner, _, _ = make()
    with pytest.raises(RunError) as e:
        runner.status("nope")
    assert e.value.code == "job_not_found"


# ── build_result ──────────────────────────────────────────────


def snapshot(duration):
    return SimpleNamespace(duration_sec=duration, model_dump=lambda by_alias: {"durationSec": duration})


def test_build_result_returns_the_camel_case_analysis_and_the_sha256():
    result = build_result(b"abc", "audio/mpeg", measure=lambda path: snapshot(100.0))
    assert result == {"analysis": {"durationSec": 100.0}, "fileHash": hashlib.sha256(b"abc").hexdigest()}


def test_build_result_hands_the_bytes_to_the_measure_function_as_a_file():
    seen = {}

    def measure(path):
        seen["suffix"], seen["data"] = path[-4:], Path(path).read_bytes()
        return snapshot(10.0)

    build_result(b"abc", "audio/mpeg", measure=measure)
    assert seen == {"suffix": ".mp3", "data": b"abc"}


def test_build_result_maps_undecodable_input_to_decode_failed():
    def measure(path):
        raise RuntimeError("NoBackendError")

    with pytest.raises(AnalysisError) as e:
        build_result(b"not audio", "audio/mpeg", measure=measure)
    assert e.value.code == "decode_failed"


@pytest.mark.parametrize(("duration", "code"), [(0.0, "decode_failed"), (181.5, "too_long"), (400.0, "too_long")])
def test_build_result_rejects_empty_and_over_long_audio(duration, code):
    with pytest.raises(AnalysisError) as e:
        build_result(b"x", "audio/mpeg", measure=lambda path: snapshot(duration))
    assert e.value.code == code


def test_build_result_allows_a_few_hundred_milliseconds_over_180_seconds():
    assert build_result(b"x", "audio/mpeg", measure=lambda path: snapshot(180.4))["analysis"]["durationSec"] == 180.4


def test_build_result_really_decodes_and_measures_a_wav():
    sr = 22_050
    t = np.arange(sr * 2) / sr
    buffer = io.BytesIO()
    soundfile.write(buffer, 0.3 * np.sin(2 * np.pi * 440 * t), sr, format="WAV")
    result = build_result(buffer.getvalue(), "audio/wav")
    assert result["analysis"]["durationSec"] == pytest.approx(2.0, abs=0.05)
    assert len(result["analysis"]["energyCurve"]) == 2


def test_build_result_rejects_text_pretending_to_be_an_mp3():
    with pytest.raises(AnalysisError) as e:
        build_result(b"this is not an mp3 file at all" * 100, "audio/mpeg")
    assert e.value.code == "decode_failed"
