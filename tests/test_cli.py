from pathlib import Path

from typer.testing import CliRunner

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
