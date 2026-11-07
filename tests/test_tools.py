import sys
from pathlib import Path

from chatwatcher.tools import find_deno, resolve_exe


def test_explicit_path(tmp_path):
    exe = tmp_path / "tool"
    exe.write_text("")
    assert resolve_exe(str(exe)) == str(exe)
    assert resolve_exe(str(tmp_path / "missing")) is None
    assert resolve_exe("") is None


def test_finds_venv_bin_without_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    python_name = Path(sys.executable).name
    assert resolve_exe(python_name) == str(Path(sys.executable).parent / python_name)
    assert resolve_exe("definitely-not-a-real-tool") is None


def test_find_deno_falls_back_to_installer_location(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    install = tmp_path / "denohome"
    monkeypatch.setenv("DENO_INSTALL", str(install))
    assert find_deno() is None

    (install / "bin").mkdir(parents=True)
    (install / "bin" / "deno").write_text("")
    assert find_deno() == str(install / "bin" / "deno")

    explicit = tmp_path / "my-deno"
    explicit.write_text("")
    assert find_deno(str(explicit)) == str(explicit)
    assert find_deno(str(tmp_path / "missing")) is None  # configured path is not second-guessed
