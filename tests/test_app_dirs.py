from __future__ import annotations

from pathlib import Path

from live_clipper import app_dirs


def test_default_app_home_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))
    assert app_dirs.default_app_home() == tmp_path / "home"


def test_resolve_app_home_is_absolute_and_has_no_filesystem_side_effect(monkeypatch, tmp_path):
    home = tmp_path / "missing" / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))

    assert app_dirs.resolve_app_home() == home.resolve()
    assert not home.exists()


def test_default_output_root_stays_in_home_with_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(tmp_path / "home"))
    assert app_dirs.default_output_root(tmp_path / "home") == tmp_path / "home" / "output"


def test_default_workspace_root_isolated_by_home_override(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))

    assert app_dirs.default_workspace_root(home) == home / "workspace"


def test_default_workspace_root_uses_user_venus_on_macos(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_CLIPPER_HOME", raising=False)
    monkeypatch.setattr(app_dirs.sys, "platform", "darwin")
    monkeypatch.setattr(app_dirs.Path, "home", lambda: tmp_path)

    assert app_dirs.default_workspace_root(tmp_path / "app-home") == tmp_path / "Venus"


def test_default_workspace_root_falls_back_inside_app_home(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_CLIPPER_HOME", raising=False)
    monkeypatch.setattr(app_dirs.sys, "platform", "linux")
    home = tmp_path / "app-home"

    assert app_dirs.default_workspace_root(home) == home / "workspace"


def test_prepare_app_home_creates_directories(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))
    result = app_dirs.prepare_app_home()
    assert result == home
    for subdir in app_dirs.WORK_SUBDIRS:
        assert (home / subdir).is_dir()
    assert (home / "workspace" / "runs").is_dir()
    assert not (home / "input").exists()


def test_run_app_bootstraps_home(monkeypatch, tmp_path):
    from live_clipper import cli

    home = tmp_path / "home"
    monkeypatch.setenv("LIVE_CLIPPER_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    captured = {}

    def fake_run_web_server(*, host, port, paths, access_token=None, restricted_startup=None):
        captured["host"] = host
        captured["port"] = port
        captured["paths"] = paths
        captured["restricted_startup"] = restricted_startup

    monkeypatch.setattr(cli, "run_web_server", fake_run_web_server)

    def fake_start_embedded_service(*args, **kwargs):
        from live_clipper.project_storage import SCHEMA_VERSION, ProjectRepository

        service_dir = kwargs["service_dir"]
        with ProjectRepository(service_dir) as repository:
            versions = repository.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            assert tuple(row[0] for row in versions) == tuple(range(1, SCHEMA_VERSION + 1))
            assert repository.get_data_mode() == "projects"
        captured["embedded_service_started"] = True
        return {"ok": True}

    monkeypatch.setattr(cli, "start_embedded_service", fake_start_embedded_service)
    cli.run_app(host="127.0.0.1", port=9999)

    config_text = (home / "live-clipper.toml").read_text(encoding="utf-8")
    assert f'workspace_root = "{home / "workspace"}"' in config_text
    assert 'backend = "openai"' in config_text
    assert 'model = "whisper-1"' in config_text
    assert 'model_source = "modelscope"' in config_text
    assert (home / ".env").exists()
    assert Path.cwd() == home
    assert captured["port"] == 9999
    assert captured["paths"].output_root == home / "output"
    assert captured["paths"].service_dir == home / "work" / "service"
    assert captured["paths"].config_path == home / "live-clipper.toml"
    assert captured["restricted_startup"] is None
    assert captured["embedded_service_started"] is True


def test_write_default_config_keeps_cli_mlx_default(tmp_path):
    from live_clipper.config import write_default_config

    config_path = write_default_config(tmp_path / "live-clipper.toml")
    config_text = config_path.read_text(encoding="utf-8")
    assert 'backend = "mlx_whisper"' in config_text


def test_app_subcommand_registered():
    from live_clipper.cli import build_parser

    args = build_parser().parse_args(["app", "--port", "9001"])
    assert args.command == "app"
    assert args.port == 9001
