from pathlib import Path
import importlib
import json
import os
import py_compile
import sys

import pytest
import typer
from typer.testing import CliRunner

from fanest import __version__
from fanest.cli import main as cli_main
from fanest.cli.main import app


def test_cli_dry_run_does_not_write_files(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "resource", "users", "--dry-run"])

    assert result.exit_code == 0
    assert "Would write src/users/users_service.py" in result.output
    assert not (tmp_path / "src").exists()


def test_cli_new_generates_runnable_project_scaffold(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["new", "blog_api"])

    assert result.exit_code == 0
    assert (tmp_path / "blog_api/main.py").exists()
    assert (tmp_path / "blog_api/pyproject.toml").exists()
    assert (tmp_path / "blog_api/.gitignore").exists()
    assert (tmp_path / "blog_api/tests/test_app.py").exists()
    pyproject = (tmp_path / "blog_api/pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "blog-api"' in pyproject
    assert 'requires-python = ">=3.10"' in pyproject
    assert '"fanest[standard]"' in pyproject


def test_cli_new_rejects_names_that_break_python_packaging(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["new", "bad name"])

    assert result.exit_code != 0
    assert "Project name may contain only" in result.output
    assert not (tmp_path / "bad name").exists()


def test_cli_new_and_workspace_report_existing_target(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blog_api").mkdir()
    (tmp_path / "acme").mkdir()

    new_result = runner.invoke(app, ["new", "blog_api"])
    workspace_result = runner.invoke(app, ["workspace", "acme"])

    assert new_result.exit_code != 0
    assert "Target directory already exists: blog_api" in new_result.output
    assert workspace_result.exit_code != 0
    assert "Target directory already exists: acme" in workspace_result.output


def test_cli_new_force_overwrites_scaffold_files(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "blog_api"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "main.py").write_text("broken\n", encoding="utf-8")

    result = runner.invoke(app, ["new", "blog_api", "--force"])

    assert result.exit_code == 0
    assert "FaNestFactory.create(AppModule)" in (project / "main.py").read_text(encoding="utf-8")


def test_cli_build_compiles_fresh_project_scaffold(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    new_result = runner.invoke(app, ["new", "blog_api"])
    monkeypatch.chdir(tmp_path / "blog_api")
    build_result = runner.invoke(app, ["build"])

    assert new_result.exit_code == 0
    assert build_result.exit_code == 0
    assert "Build OK: ." in build_result.output


def test_cli_build_compiles_single_python_file(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path("main.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = runner.invoke(app, ["build", "main.py"])

    assert result.exit_code == 0
    assert "Build OK: main.py" in result.output


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


def test_cli_resource_generator_emits_crud_handlers(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "resource", "users"])

    assert result.exit_code == 0
    controller = (tmp_path / "src/users/users_controller.py").read_text(encoding="utf-8")
    service = (tmp_path / "src/users/users_service.py").read_text(encoding="utf-8")
    module = (tmp_path / "src/users/users_module.py").read_text(encoding="utf-8")
    assert "Post" in controller
    assert "Patch" in controller
    assert "Delete" in controller
    assert "async def create" in service
    assert "async def update" in service
    assert "CreateUsersDto" in module


def test_cli_repl_loads_application_for_command_mode(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path("main.py").write_text(
        "async def app(scope, receive, send):\n"
        "    pass\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["repl", "main.py", "--command", "print(callable(app))"])

    assert result.exit_code == 0
    assert "FaNest REPL loaded main:app" in result.output
    assert "True" in result.output


def test_cli_repl_exposes_application_graph(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["new", "blog_api"])
    monkeypatch.chdir(tmp_path / "blog_api")

    result = runner.invoke(
        app,
        [
            "repl",
            "main.py",
            "--command",
            "print(graph['root_module']); print(any(route['path'] == '/' for route in graph['routes']))",
        ],
    )

    assert result.exit_code == 0
    assert "AppModule" in result.output
    assert "True" in result.output


def test_cli_generates_commander_style_command_app(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "command", "export-users"])

    assert result.exit_code == 0
    command_file = tmp_path / "src/export_users/export_users_command.py"
    assert command_file.exists()
    content = command_file.read_text(encoding="utf-8")
    assert "typer.Typer" in content
    assert '@cli.command("export_users")' in content


def test_cli_accepts_kebab_case_artifact_names_and_emits_python_modules(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "resource", "user-profile"])

    assert result.exit_code == 0
    assert (tmp_path / "src/user_profile/user_profile_controller.py").exists()
    assert (tmp_path / "src/user_profile/user_profile_service.py").exists()
    module_content = (tmp_path / "src/user_profile/user_profile_module.py").read_text(
        encoding="utf-8"
    )
    assert "class UserProfileModule" in module_content
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.invalidate_caches()
        imported = importlib.import_module("src.user_profile.user_profile_module")
    finally:
        sys.path.remove(str(tmp_path))
        for module_name in list(sys.modules):
            if module_name == "src" or module_name.startswith("src."):
                sys.modules.pop(module_name, None)
    assert imported.UserProfileModule.__name__ == "UserProfileModule"


def test_cli_generate_alias_and_nest_style_artifacts(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    results = [
        runner.invoke(app, ["g", "class", "billing"]),
        runner.invoke(app, ["g", "provider", "billing"]),
        runner.invoke(app, ["g", "exception", "billing"]),
        runner.invoke(app, ["g", "resolver", "billing"]),
        runner.invoke(app, ["g", "repository", "billing"]),
        runner.invoke(app, ["g", "test", "billing"]),
    ]

    assert all(result.exit_code == 0 for result in results)
    assert (tmp_path / "src/billing/billing.py").exists()
    assert (tmp_path / "src/billing/billing_provider.py").exists()
    assert (tmp_path / "src/billing/billing_exception.py").exists()
    assert (tmp_path / "src/billing/billing_resolver.py").exists()
    assert (tmp_path / "src/billing/billing_repository.py").exists()
    assert (tmp_path / "tests/test_billing.py").exists()


def test_cli_generate_short_aliases(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    results = [
        runner.invoke(app, ["g", "mo", "users"]),
        runner.invoke(app, ["g", "co", "users"]),
        runner.invoke(app, ["g", "s", "users"]),
        runner.invoke(app, ["g", "gu", "auth"]),
        runner.invoke(app, ["g", "pi", "parse_int"]),
        runner.invoke(app, ["g", "itc", "trace"]),
        runner.invoke(app, ["g", "f", "http"]),
        runner.invoke(app, ["g", "ga", "chat"]),
        runner.invoke(app, ["g", "mi", "request_id"]),
        runner.invoke(app, ["g", "d", "current_user"]),
        runner.invoke(app, ["g", "lib", "common"]),
        runner.invoke(app, ["g", "cl", "plain"]),
        runner.invoke(app, ["g", "pr", "cache"]),
        runner.invoke(app, ["g", "ex", "domain"]),
        runner.invoke(app, ["g", "r", "profile"]),
        runner.invoke(app, ["g", "repo", "users"]),
        runner.invoke(app, ["g", "spec", "users"]),
    ]

    assert all(result.exit_code == 0 for result in results)
    assert (tmp_path / "src/users/users_module.py").exists()
    assert (tmp_path / "src/chat/chat_gateway.py").exists()
    assert (tmp_path / "libs/common/common_module.py").exists()
    assert (tmp_path / "tests/test_users.py").exists()


def test_cli_generate_protects_existing_files_and_force_overwrites(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    service = tmp_path / "src/users/users_service.py"
    service.parent.mkdir(parents=True)
    service.write_text("custom user code\n", encoding="utf-8")

    blocked = runner.invoke(app, ["generate", "service", "users"])
    forced = runner.invoke(app, ["generate", "service", "users", "--force"])

    assert blocked.exit_code != 0
    assert "Refusing to overwrite existing file" in blocked.output
    assert forced.exit_code == 0
    assert "class UsersService" in service.read_text(encoding="utf-8")


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


def test_cli_registers_generated_module_in_root_main(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    Path("main.py").write_text(
        "from fanest import Controller, FaNestFactory, Get, Injectable, Module\n\n\n"
        "@Injectable()\n"
        "class AppService:\n"
        "    def info(self):\n"
        "        return {'status': 'running'}\n\n\n"
        "@Controller('/')\n"
        "class AppController:\n"
        "    def __init__(self, app_service: AppService):\n"
        "        self.app_service = app_service\n\n"
        "    @Get('/')\n"
        "    async def index(self):\n"
        "        return self.app_service.info()\n\n\n"
        "@Module(controllers=[AppController], providers=[AppService])\n"
        "class AppModule:\n"
        "    pass\n\n\n"
        "app = FaNestFactory.create(AppModule)\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["generate", "resource", "users", "--module", "main.py"])

    assert result.exit_code == 0
    content = Path("main.py").read_text(encoding="utf-8")
    assert "from src.users.users_module import UsersModule" in content
    assert "@Module(imports=[UsersModule], controllers=[AppController]" in content
    assert (tmp_path / "src/__init__.py").exists()

    namespace: dict[str, object] = {}
    sys.path.insert(0, str(tmp_path))
    try:
        exec(compile(content, "main.py", "exec"), namespace)
    finally:
        sys.path.remove(str(tmp_path))
    assert "app" in namespace


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
    assert (tmp_path / "acme/pyproject.toml").exists()
    assert (tmp_path / "acme/apps/api/main.py").exists()
    assert (tmp_path / "acme/apps/api/src/main.py").exists()
    assert (tmp_path / "acme/apps/api/tests/test_app.py").exists()
    assert (tmp_path / "acme/libs/__init__.py").exists()
    assert (tmp_path / "acme/libs/common/common_module.py").exists()
    config = json.loads((tmp_path / "acme/fanest.json").read_text(encoding="utf-8"))
    pyproject = (tmp_path / "acme/pyproject.toml").read_text(encoding="utf-8")
    assert config["defaultProject"] == "api"
    assert config["projects"]["api"]["sourceRoot"] == "apps/api/src"
    assert config["projects"]["common"]["type"] == "library"
    assert '"start:api" = "fanest dev apps/api/main.py"' in pyproject


def test_cli_generates_artifacts_into_default_workspace_project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme")
    service = runner.invoke(app, ["generate", "service", "users"])
    module = runner.invoke(app, ["generate", "module", "billing"])

    assert workspace.exit_code == 0
    assert service.exit_code == 0
    assert module.exit_code == 0
    assert (tmp_path / "acme/apps/api/src/users/users_service.py").exists()
    assert (tmp_path / "acme/apps/api/src/billing/billing_module.py").exists()
    assert not (tmp_path / "acme/src/users/users_service.py").exists()


def test_cli_generates_artifacts_into_current_workspace_project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme/apps/api")
    controller = runner.invoke(app, ["generate", "controller", "orders"])

    assert workspace.exit_code == 0
    assert controller.exit_code == 0
    assert (tmp_path / "acme/apps/api/src/orders/orders_controller.py").exists()


def test_cli_generates_workspace_library_from_application_directory(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme/apps/api")
    library = runner.invoke(app, ["generate", "library", "common"])

    assert workspace.exit_code == 0
    assert library.exit_code == 0
    assert (tmp_path / "acme/libs/common/common_module.py").exists()
    assert not (tmp_path / "acme/apps/api/libs/common/common_module.py").exists()
    config = json.loads((tmp_path / "acme/fanest.json").read_text(encoding="utf-8"))
    assert config["projects"]["common"]["sourceRoot"] == "libs/common"


def test_cli_generates_artifacts_into_named_workspace_project(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme")
    application = runner.invoke(app, ["generate", "application", "admin-api"])
    resource = runner.invoke(app, ["generate", "resource", "users", "--project", "admin_api"])

    assert workspace.exit_code == 0
    assert application.exit_code == 0
    assert resource.exit_code == 0
    assert (tmp_path / "acme/apps/admin_api/src/users/users_module.py").exists()
    config = json.loads((tmp_path / "acme/fanest.json").read_text(encoding="utf-8"))
    assert config["projects"]["admin_api"]["root"] == "apps/admin_api"


def test_cli_registers_workspace_project_module_import(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme")
    result = runner.invoke(app, ["generate", "resource", "users", "--module", "main.py"])

    assert workspace.exit_code == 0
    assert result.exit_code == 0
    content = (tmp_path / "acme/apps/api/src/main.py").read_text(encoding="utf-8")
    assert "from .users.users_module import UsersModule" in content
    assert "@Module(imports=[UsersModule], controllers=[AppController]" in content


def test_cli_generates_monorepo_application_aliases(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    app_result = runner.invoke(app, ["generate", "app", "admin-api"])
    application_result = runner.invoke(app, ["generate", "application", "worker_api"])

    assert app_result.exit_code == 0
    assert application_result.exit_code == 0
    assert (tmp_path / "apps/admin_api/main.py").exists()
    assert (tmp_path / "apps/admin_api/src/main.py").exists()
    assert (tmp_path / "apps/admin_api/tests/test_app.py").exists()
    assert (tmp_path / "apps/worker_api/main.py").exists()


def test_cli_generates_plugin_dynamic_module_scaffold(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["generate", "plugin", "redis-cache"])

    assert result.exit_code == 0
    plugin_file = tmp_path / "src/redis_cache/redis_cache_plugin.py"
    assert plugin_file.exists()
    content = plugin_file.read_text(encoding="utf-8")
    assert "REDIS_CACHE_OPTIONS" in content
    assert "class RedisCachePlugin" in content

    sys.path.insert(0, str(tmp_path))
    try:
        for module_name in list(sys.modules):
            if module_name == "src" or module_name.startswith("src."):
                sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        imported = importlib.import_module("src.redis_cache.redis_cache_plugin")
    finally:
        sys.path.remove(str(tmp_path))
        for module_name in list(sys.modules):
            if module_name == "src" or module_name.startswith("src."):
                sys.modules.pop(module_name, None)
    dynamic = imported.RedisCachePlugin.register(url="redis://localhost")
    assert dynamic.providers[0].use_value == {"url": "redis://localhost"}


def test_cli_workspace_build_and_check_default_entrypoint(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme/apps/api")
    check = runner.invoke(app, ["check", "main.py"])
    build = runner.invoke(app, ["build"])

    assert workspace.exit_code == 0
    assert check.exit_code == 0
    assert "Application target OK: main:app" in check.output
    assert build.exit_code == 0


def test_cli_check_workspace_entrypoint_from_workspace_root(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    workspace = runner.invoke(app, ["workspace", "acme"])
    monkeypatch.chdir(tmp_path / "acme")
    check = runner.invoke(app, ["check", "apps/api/main.py"])

    assert workspace.exit_code == 0
    assert check.exit_code == 0
    assert "Application target OK: main:app" in check.output


def test_cli_check_cleans_local_import_cache_and_sys_path(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text("async def app(scope, receive, send):\n    pass\n", encoding="utf-8")
    Path("main.py").write_text("from app.service import app\n", encoding="utf-8")
    original_sys_path = list(sys.path)

    first = runner.invoke(app, ["check", "main.py"])
    (package / "service.py").write_text("app = object()\n", encoding="utf-8")
    second = runner.invoke(app, ["check", "main.py"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Application target is not callable" in second.output
    assert sys.path == original_sys_path


def test_cli_check_executes_source_instead_of_stale_bytecode(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    valid = "async def app(scope, receive, send):\n    pass\n"
    invalid = "app = object()\n" + "#" * (len(valid) - len("app = object()\n"))
    source = Path("main.py")
    source.write_text(valid, encoding="utf-8")
    os.utime(source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(str(source), doraise=True)

    first = runner.invoke(app, ["check", "main.py"])
    source.write_text(invalid, encoding="utf-8")
    os.utime(source, (1_700_000_000, 1_700_000_000))
    second = runner.invoke(app, ["check", "main.py"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Application target is not callable" in second.output


def test_cli_check_supports_package_relative_imports(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "src"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text(
        "async def app(scope, receive, send):\n    pass\n",
        encoding="utf-8",
    )
    (package / "main.py").write_text("from .service import app\n", encoding="utf-8")

    result = runner.invoke(app, ["check", "src/main.py"])

    assert result.exit_code == 0
    assert "Application target OK: src.main:app" in result.output


def test_cli_rejects_project_path_traversal(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["new", "../escape"])

    assert result.exit_code != 0
    assert "Project name must be a single directory name" in result.output
    assert not (tmp_path.parent / "escape").exists()


def test_cli_rejects_invalid_project_distribution_names(tmp_path, monkeypatch):
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["new", "bad name"])

    assert result.exit_code != 0
    assert "Project name may contain only" in result.output
    assert not (tmp_path / "bad name").exists()


def test_cli_dev_and_run_accept_file_paths(tmp_path, monkeypatch):
    calls = []

    def fake_run_uvicorn(app_path, **options):
        calls.append((app_path, options))

    monkeypatch.setattr(cli_main, "_run_uvicorn", fake_run_uvicorn)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    Path("main.py").write_text("app = None\n", encoding="utf-8")
    Path("src").mkdir(exist_ok=True)
    Path("src/main.py").write_text("application = None\n", encoding="utf-8")

    dev = runner.invoke(app, ["dev", "main.py", "--port", "9000"])
    run = runner.invoke(app, ["run", "src/main.py", "--app", "application", "--workers", "2"])
    absolute = runner.invoke(app, ["dev", str(tmp_path / "main.py"), "--port", "9001"])

    assert dev.exit_code == 0
    assert run.exit_code == 0
    assert absolute.exit_code == 0
    assert calls[0] == (
        "main:app",
        {"app_dir": str(tmp_path), "host": "127.0.0.1", "port": 9000, "reload": True},
    )
    assert calls[1] == (
        "main:application",
        {
            "app_dir": str(tmp_path / "src"),
            "host": "0.0.0.0",
            "port": 8000,
            "reload": False,
            "workers": 2,
        },
    )
    assert calls[2] == (
        "main:app",
        {"app_dir": str(tmp_path), "host": "127.0.0.1", "port": 9001, "reload": True},
    )


def test_cli_uvicorn_runner_sets_app_dir_to_current_directory(tmp_path, monkeypatch):
    calls = []

    class FakeUvicorn:
        @staticmethod
        def run(app_path, **options):
            calls.append((app_path, options))

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.chdir(tmp_path)

    cli_main._run_uvicorn("main:app", host="127.0.0.1", port=0, reload=True)

    assert calls == [
        (
                "main:app",
                {
                    "host": "127.0.0.1",
                    "port": 0,
                    "reload": True,
                    "app_dir": str(tmp_path),
                },
        )
    ]


def test_cli_uvicorn_runner_reports_port_in_use(monkeypatch, capsys):
    class FakeUvicorn:
        @staticmethod
        def run(app_path, **options):
            raise AssertionError("uvicorn should not start when the port is unavailable")

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    monkeypatch.setattr(
        cli_main,
        "_ensure_port_available",
        lambda host, port: cli_main._port_in_use_error(port),
    )
    port = 8765

    with pytest.raises(typer.Exit):
        cli_main._run_uvicorn("main:app", host="127.0.0.1", port=port, reload=True)

    captured = capsys.readouterr()
    assert f"Port {port} is already in use. Try running with --port {port + 1}." in captured.err


def test_cli_dev_reports_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "_run_uvicorn", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(app, ["dev", "missing.py"])

    assert result.exit_code != 0
    assert "Application file not found" in result.output


def test_cli_check_validates_importable_asgi_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("main.py").write_text(
        "async def app(scope, receive, send):\n"
        "    pass\n",
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["check", "main.py"])

    assert result.exit_code == 0
    assert "Application target OK: main:app" in result.output


def test_cli_check_reports_invalid_target(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("main.py").write_text("app = object()\n", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(app, ["check", "main.py"])

    assert result.exit_code != 0
    assert "Application target is not callable" in result.output


def test_cli_info_and_build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("src").mkdir()
    Path("src/app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner = CliRunner()

    info = runner.invoke(app, ["info"])
    build = runner.invoke(app, ["build"])
    build_src = runner.invoke(app, ["build", "src"])

    assert info.exit_code == 0
    assert f"FaNest {__version__}" in info.output
    assert "Executable " in info.output
    assert "fastapi " in info.output
    assert "uvicorn " in info.output
    assert build.exit_code == 0
    assert build_src.exit_code == 0
    assert "Build OK: ." in build.output
    assert "Build OK: src" in build_src.output


def test_cli_rejects_trailing_separator_project_names():
    """PEP 508 distribution names must end with an alphanumeric, so a trailing
    '-', '.' or '_' (which would break the generated pyproject.toml) is rejected
    while valid kebab/snake names are accepted."""
    import pytest
    import typer

    from fanest.cli.main import _validate_project_name

    for valid in ("blog-api", "my_app", "svc2", "a"):
        _validate_project_name(valid)  # no raise
    for invalid in ("trailing-", "ends.", "ends_", "-lead"):
        with pytest.raises(typer.BadParameter):
            _validate_project_name(invalid)
