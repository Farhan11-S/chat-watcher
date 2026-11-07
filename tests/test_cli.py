from typer.testing import CliRunner

from chatwatcher.cli import app
from chatwatcher.config import ENV_CONFIG

runner = CliRunner()


def _config(tmp_path, extra=""):
    f = tmp_path / "config.toml"
    f.write_text(f'[paths]\ndata_dir = "data"\noutput_dir = "out"\n{extra}')
    return f


def test_init_then_status(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    f = _config(tmp_path)

    r = runner.invoke(app, ["status", "-c", str(f)])
    assert r.exit_code == 1 and "cw init" in r.output

    r = runner.invoke(app, ["init", "-c", str(f)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "data" / "chatwatcher.db").is_file()
    assert (tmp_path / "out").is_dir()

    r = runner.invoke(app, ["init", "-c", str(f)])
    assert "already up to date" in r.output

    r = runner.invoke(app, ["status", "-c", str(f)])
    assert r.exit_code == 0 and "videos" in r.output


def test_config_command_and_bad_config(tmp_path):
    f = _config(tmp_path, "[scoring]\ntop_n = 5\n")
    r = runner.invoke(app, ["config", "-c", str(f)])
    assert r.exit_code == 0 and "top_n = 5" in r.output

    bad = tmp_path / "bad.toml"
    bad.write_text("[scoring]\ntop_n = 'many'\n")
    r = runner.invoke(app, ["config", "-c", str(bad)])
    assert r.exit_code == 2


def test_doctor_flags_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    f = _config(tmp_path, f'[youtube]\nytdlp_path = "{tmp_path}/no-yt-dlp"\n')
    r = runner.invoke(app, ["doctor", "-c", str(f)])
    assert r.exit_code == 1
    assert "yt-dlp" in r.output and "not found" in r.output
    assert "ANTHROPIC_API_KEY" in r.output
