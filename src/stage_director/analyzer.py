"""분석 작업 (스펙 §4.1): 음원 내려받기 → librosa 측정 → jobs.result. 그래프 실행과 별도의 스레드 풀에서 돈다.

jobs 행의 kind 는 analysis, id 는 Next.js 가 정한 jobId(audio_tracks.id). 결과는 done 후 7일 보관(retention.py)되어
탭을 닫아도 다음 폴링 때 Next.js 가 받아 audio_tracks.analysis 에 저장한다.
"""

import hashlib
import logging
import tempfile
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any

from stage_director.analysis.measure import measure_file
from stage_director.audio import AudioError, check_url, fetch_audio
from stage_director.jobs import Job, JobStore
from stage_director.models import AnalysisStatus
from stage_director.runner import RunError

log = logging.getLogger(__name__)

MAX_CONCURRENT_ANALYSES = 1  # librosa 한 건이 수백 MB 를 쓴다. 풀이 비어 있지 않으면 다음 분석은 queued 로 기다린다
MAX_ANALYSIS_AUDIO_BYTES = 30 * 1024 * 1024  # 3분 wav(44.1kHz 스테레오 16bit)가 약 31MB. 음원 버킷의 크기 상한도 이 값 이하로 맞춘다
MAX_DURATION_SEC = 180  # 스펙 §5 audio_tracks.duration_sec 최대
DURATION_TOLERANCE_SEC = 1.0  # 인코더 패딩 때문에 180.02초 같은 곡이 나온다
SUFFIX_BY_MIME = {
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


class AnalysisError(Exception):
    """분석할 수 없는 음원. code 가 그대로 jobs.error 와 API 응답에 나간다: decode_failed / too_long."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def build_result(data: bytes, mime: str, measure: Callable[[str], Any] = measure_file) -> dict[str, Any]:
    """음원 바이트 → {"analysis": AnalysisSnapshot(camelCase), "fileHash": sha256}. 시드 곡 스크립트도 이 함수를 쓴다."""
    with tempfile.NamedTemporaryFile(suffix=SUFFIX_BY_MIME.get(mime, ".bin")) as f:
        f.write(data)
        f.flush()
        try:
            snapshot = measure(f.name)
        except Exception as e:  # librosa·soundfile·audioread 가 포맷마다 다른 예외를 던진다
            raise AnalysisError("decode_failed") from e
    if snapshot.duration_sec <= 0:
        raise AnalysisError("decode_failed")
    if snapshot.duration_sec > MAX_DURATION_SEC + DURATION_TOLERANCE_SEC:
        raise AnalysisError("too_long")
    return {"analysis": snapshot.model_dump(by_alias=True), "fileHash": hashlib.sha256(data).hexdigest()}


class AnalysisRunner:
    def __init__(
        self,
        jobs: JobStore,
        executor: Executor,
        *,
        fetch: Callable[..., tuple[bytes, str]] = fetch_audio,
        build: Callable[[bytes, str], dict[str, Any]] = build_result,
        allowed_hosts: tuple[str, ...] = (),
    ):
        self._jobs, self._executor, self._fetch, self._build, self._allowed_hosts = jobs, executor, fetch, build, allowed_hosts

    def start(self, job_id: str, audio_url: str) -> AnalysisStatus:
        """멱등. 새 작업이면 시작, error 면 처음부터 다시, 그 밖에는 현재 상태를 그대로 돌려준다."""
        try:
            check_url(audio_url, self._allowed_hosts)  # 큐에 넣기 전에 거절해서 호출자가 바로 알게 한다
        except AudioError as e:
            raise RunError(422, "invalid_audio_url", str(e)) from e
        if self._jobs.create(job_id, kind="analysis", status="queued"):
            self._executor.submit(self._run, job_id, audio_url)
        else:
            self._analysis_job(job_id)
            if self._jobs.transition(job_id, from_={"error"}, to="queued"):
                self._jobs.set_progress(job_id, 0.0)
                self._executor.submit(self._run, job_id, audio_url)
        return self.status(job_id)

    def status(self, job_id: str) -> AnalysisStatus:
        job = self._analysis_job(job_id)
        return AnalysisStatus(job_id=job.id, status=job.status, progress=job.progress, result=job.result, error=job.error)

    def _analysis_job(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None or job.kind != "analysis":  # 그래프 작업 id 로 들어온 요청도 모르는 작업으로 취급
            raise RunError(404, "job_not_found")
        return job

    def _run(self, job_id: str, audio_url: str) -> None:
        if not self._jobs.transition(job_id, from_={"queued"}, to="running"):
            return  # 대기하는 사이 다른 인스턴스의 시작 정리(fail_running)가 error 로 바꿨거나 행이 지워졌다. 실행하지 않는다
        # ponytail: 진행률은 단계(내려받기 전 0.1, 후 0.4, 측정 후 1.0)만 알린다. librosa 는 중간 진행을 알려 주지 않는다.
        try:
            self._jobs.set_progress(job_id, 0.1)
            data, mime = self._fetch(audio_url, max_bytes=MAX_ANALYSIS_AUDIO_BYTES, allowed_hosts=self._allowed_hosts)
            self._jobs.set_progress(job_id, 0.4)
            result = self._build(data, mime)
            self._jobs.set_progress(job_id, 1.0)
            self._jobs.transition(job_id, from_={"running"}, to="done", result=result)
        except AudioError as e:
            log.warning("analysis %s: 음원을 받을 수 없다: %s", job_id, e)
            self._jobs.transition(job_id, from_={"running"}, to="error", error="audio_unavailable")
        except AnalysisError as e:
            log.warning("analysis %s: %s", job_id, e.code)
            self._jobs.transition(job_id, from_={"running"}, to="error", error=e.code)
        except Exception:
            log.exception("analysis %s failed", job_id)
            self._jobs.transition(job_id, from_={"running"}, to="error", error="internal_error")
