from __future__ import annotations

from pathlib import Path

from live_clipper import app_dirs


def test_default_app_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))
    assert app_dirs.default_app_home() == tmp_path / "home"


def test_default_output_root_stays_in_home_with_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))
    assert app_dirs.default_output_root(tmp_path / "home") == tmp_path / "home" / "output"


def test_prepare_app_home_creates_directories(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))
    result = app_dirs.prepare_app_home()
    assert result == home
    for subdir in app_dirs.WORK_SUBDIRS:
        assert (home / subdir).is_dir()
    assert (home / "output").is_dir()


def test_run_app_bootstraps_home(monkeypatch, tmp_path):
    from live_clipper import cli

    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    captured = {}

    def fake_run_web_server(*, host, port, paths):
        captured["host"] = host
        captured["port"] = port
        captured["paths"] = paths

    monkeypatch.setattr(cli, "run_web_server", fake_run_web_server)
    cli.run_app(host="127.0.0.1", port=9999)

    config_text = (home / "live-clipper.toml").read_text(encoding="utf-8")
    assert f'output_root = "{home / "output"}"' in config_text
    assert (home / ".env").exists()
    assert Path.cwd() == home
    assert captured["port"] == 9999
    assert captured["paths"].output_root == home / "output"


def test_app_subcommand_registered():
    from live_clipper.cli import build_parser

    args = build_parser().parse_args(["app", "--port", "9001"])
    assert args.command == "app"
    assert args.port == 9001
