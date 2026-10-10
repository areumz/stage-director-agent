import hashlib
import json

import numpy as np
import pytest
import soundfile

from stage_director.analysis.seed import analyze_seed, main
from stage_director.analyzer import AnalysisError, build_result


@pytest.fixture
def wav(tmp_path):
    sr = 22_050
    t = np.arange(sr * 3) / sr
    path = tmp_path / "곡 이름.wav"
    soundfile.write(path, 0.3 * np.sin(2 * np.pi * 440 * t), sr)
    return path


def test_seed_result_matches_what_the_upload_analysis_job_would_produce(wav):
    seed = analyze_seed(wav)
    online = build_result(wav.read_bytes(), "audio/x-wav")
    assert seed["analysis"] == online["analysis"] and seed["fileHash"] == online["fileHash"]
    assert seed["fileHash"] == hashlib.sha256(wav.read_bytes()).hexdigest()
    assert seed["durationSec"] == pytest.approx(3.0, abs=0.05) and seed["fileName"] == "곡 이름.wav"


def test_main_writes_one_json_per_file_and_reports_a_summary(wav, tmp_path, capsys):
    out = tmp_path / "out"
    assert main([str(wav), "--out", str(out)]) == 0
    written = json.loads((out / "곡 이름.json").read_text(encoding="utf-8"))
    assert set(written) == {"fileName", "fileHash", "durationSec", "analysis"} and "energyCurve" in written["analysis"]
    assert "구간" in capsys.readouterr().out


def test_main_keeps_going_after_an_undecodable_file_and_exits_nonzero(wav, tmp_path, capsys):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio" * 100)
    out = tmp_path / "out"
    assert main([str(bad), str(wav), "--out", str(out)]) == 1
    assert (out / "곡 이름.json").exists() and not (out / "bad.json").exists()
    assert "decode_failed" in capsys.readouterr().out


def test_main_keeps_going_after_a_missing_path_and_exits_nonzero(wav, tmp_path, capsys):
    out = tmp_path / "out"
    assert main([str(tmp_path / "없는 파일.mp3"), str(wav), "--out", str(out)]) == 1
    assert (out / "곡 이름.json").exists()
    assert "없는 파일.mp3: 실패" in capsys.readouterr().out


def test_analyze_seed_raises_for_undecodable_audio(tmp_path):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio" * 100)
    with pytest.raises(AnalysisError):
        analyze_seed(bad)
