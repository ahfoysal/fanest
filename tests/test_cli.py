from pathlib import Path

from typer.testing import CliRunner

from fanest.cli import main as cli_main
from fanest.cli.main import app


def test_cli_dry_run_does_not_write_files(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "resource", "users", "--dry-run"])

    assert result.exit_code == 0
    assert "Would write src/users/users_service.py" in result.output
    assert not (tmp_path / "src").exists()


def test_cli_generates_resource_and_extra_artifacts(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    resource = runner.invoke(app, ["generate", "resource", "users"])
    dto = runner.invoke(app, ["generate", "dto", "users"])
    middleware = runner.invoke(app, ["generate", "middleware", "request_id"])
    decorator = runner.invoke(app, ["generate", "decorator", "current_user"])

    assert resource.exit_code == 0
    assert dto.exit_code == 0
    assert middleware.exit_code == 0
    assert decorator.exit_code == 0
    assert (tmp_path / "src/users/users_controller.py").exists()
    assert "CreateUsersDto" in Path(tmp_path / "src/users/users_dto.py").read_text()
    assert (tmp_path / "src/request_id/request_id_middleware.py").exists()
    assert (tmp_path / "src/current_user/current_user_decorator.py").exists()


def test_cli_registers_generated_module_in_parent_module(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    app_module = src / "app_module.py"
    app_module.write_text(
        "from fanest import Module\n\n\n@Module(controllers=[])\nclass AppModule:\n    pass\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["generate", "module", "users", "--module", "app_module.py"])

    assert result.exit_code == 0
    content = app_module.read_text(encoding="utf-8")
    assert "from .users.users_module import UsersModule" in content
    assert "@Module(imports=[UsersModule], controllers=[])" in content


def test_cli_register_module_dry_run_does_not_mutate_parent(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    app_module = src / "app_module.py"
    original = "from fanest import Module\n\n\n@Module()\nclass AppModule:\n    pass\n"
    app_module.write_text(original, encoding="utf-8")

    result = runner.invoke(
        app,
        ["generate", "module", "users", "--module", "app_module.py", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "Would update" in result.output
    assert app_module.read_text(encoding="utf-8") == original


def test_cli_generates_workspace_and_library(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme")
    library = runner.invoke(app, ["generate", "library", "common"])

    assert workspace.exit_code == 0
    assert library.exit_code == 0
    assert (tmp_path / "acme/apps/api/main.py").exists()
    assert (tmp_path / "acme/libs/common/common_module.py").exists()


def test_cli_dev_and_run_accept_file_paths(monkeypatch):
    calls = []

    def fake_run_uvicorn(app_path, **options):
        calls.append((app_path, options))

    monkeypatch.setattr(cli_main, "_run_uvicorn", fake_run_uvicorn)
    runner = CliRunner()

    dev = runner.invoke(app, ["dev", "main.py", "--port", "9000"])
    run = runner.invoke(app, ["run", "src/main.py", "--app", "application", "--workers", "2"])

    assert dev.exit_code == 0
    assert run.exit_code == 0
    assert calls[0] == ("main:app", {"host": "127.0.0.1", "port": 9000, "reload": True})
    assert calls[1] == (
        "src.main:application",
        {"host": "0.0.0.0", "port": 8000, "reload": False, "workers": 2},
    )
