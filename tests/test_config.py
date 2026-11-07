from pathlib import Path

import pytest
from pydantic import ValidationError

from chatwatcher.config import ENV_CONFIG, Config, load_config


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_CONFIG, raising=False)
    monkeypatch.chdir(tmp_path)


def test_defaults_without_file():
    cfg = load_config()
    assert cfg.source is None
    assert cfg.scoring.top_n == 10
    assert cfg.clip.format == "bv*+ba[ext=m4a]/bv*+ba/b"
    assert cfg.paths.db_path == Path("data/chatwatcher.db")


def test_file_overrides_and_relative_paths(tmp_path):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    f = conf_dir / "config.toml"
    f.write_text('[paths]\ndata_dir = "d"\n[scoring]\ntop_n = 3\n[claude]\nmodel = "opus"\n')
    cfg = load_config(f)
    assert cfg.source == f
    assert cfg.scoring.top_n == 3
    assert cfg.claude.model == "opus"
    assert cfg.scoring.pre_roll_s == 25  # untouched default
    assert cfg.paths.data_dir == conf_dir.resolve() / "d"
    assert cfg.paths.output_dir == conf_dir.resolve() / "output"


def test_env_var_and_cwd_lookup(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[scoring]\ntop_n = 4\n")
    assert load_config().scoring.top_n == 4

    other = tmp_path / "other.toml"
    other.write_text("[scoring]\ntop_n = 7\n")
    monkeypatch.setenv(ENV_CONFIG, str(other))
    assert load_config().scoring.top_n == 7


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_unknown_key_rejected(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("[scoring]\ntop_nn = 3\n")
    with pytest.raises(ValidationError):
        load_config(f)


def test_instances_do_not_share_sections():
    a, b = Config(), Config()
    a.paths.data_dir = Path("/x")
    assert b.paths.data_dir == Path("data")


def test_example_config_matches_defaults():
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    cfg = load_config(example)
    defaults = Config()
    dumped = cfg.model_dump(exclude={"paths"})
    assert dumped == defaults.model_dump(exclude={"paths"})
