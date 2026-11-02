import pytest

from settings import get_required, load_env


def test_load_env_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_SETTING", raising=False)
    env = tmp_path / ".env"
    env.write_text("DEMO_SETTING=from_file\n")
    assert load_env(env) is True
    assert get_required("DEMO_SETTING") == "from_file"


def test_load_env_does_not_override_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SETTING", "from_shell")
    env = tmp_path / ".env"
    env.write_text("DEMO_SETTING=from_file\n")
    load_env(env)
    assert get_required("DEMO_SETTING") == "from_shell"


def test_load_env_missing_file_returns_false(tmp_path):
    assert load_env(tmp_path / "nope.env") is False


def test_get_required_raises_when_missing_or_empty(monkeypatch):
    monkeypatch.delenv("DEMO_SETTING", raising=False)
    with pytest.raises(RuntimeError, match="DEMO_SETTING"):
        get_required("DEMO_SETTING")
    monkeypatch.setenv("DEMO_SETTING", "")
    with pytest.raises(RuntimeError):
        get_required("DEMO_SETTING")
