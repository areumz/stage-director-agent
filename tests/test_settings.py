import pytest

from stage_director.settings import DEFAULT_GEMINI_RPM, Settings

REQUIRED = {"INTERNAL_API_KEY": "k", "GEMINI_API_KEY": "g", "DATABASE_URL": "postgresql://x"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("GEMINI_MODEL", "GEMINI_FALLBACK_MODEL", "GEMINI_RPM", "AUDIO_URL_ALLOWED_HOSTS", *REQUIRED):
        monkeypatch.delenv(name, raising=False)
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)


def test_rpm_defaults_when_unset_or_empty(monkeypatch):
    assert Settings.from_env().gemini_rpm == DEFAULT_GEMINI_RPM
    monkeypatch.setenv("GEMINI_RPM", "")  # 배포 환경에서 빈 값으로 넘어와도 기본값
    assert Settings.from_env().gemini_rpm == DEFAULT_GEMINI_RPM


def test_rpm_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "60")
    assert Settings.from_env().gemini_rpm == 60
    monkeypatch.setenv("GEMINI_RPM", "0")
    assert Settings.from_env().gemini_rpm == 0  # 제한 없음


@pytest.mark.parametrize("value", ["ten", "-1", "1.5"])
def test_invalid_rpm_fails_at_startup(monkeypatch, value):
    monkeypatch.setenv("GEMINI_RPM", value)
    with pytest.raises(RuntimeError, match="GEMINI_RPM"):
        Settings.from_env()
