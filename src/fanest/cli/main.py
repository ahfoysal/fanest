from pathlib import Path
import compileall
import importlib
import importlib.util
import platform
import re
import sys
from typing import Any

import typer
import fanest

app = typer.Typer(help="FaNest command line tools.")
generate_app = typer.Typer(help="Generate FaNest artifacts.")
app.add_typer(generate_app, name="generate")
app.add_typer(generate_app, name="g", help="Alias for generate.")


@app.command()
def new(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    target = Path(name)
    if dry_run:
        typer.echo(f"Would create FaNest application in {target}")
        return
    target.mkdir(parents=True, exist_ok=False)
    (target / "tests").mkdir()
    (target / "main.py").write_text(_main_template(), encoding="utf-8")
    (target / "pyproject.toml").write_text(_project_pyproject_template(name), encoding="utf-8")
    (target / "README.md").write_text(_project_readme_template(name), encoding="utf-8")
    (target / ".gitignore").write_text(_gitignore_template(), encoding="utf-8")
    (target / "tests" / "test_app.py").write_text(_project_test_template(), encoding="utf-8")
    typer.echo(f"Created FaNest application in {target}")


@app.command()
def workspace(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    root = Path(name)
    paths = [
        root / "apps",
        root / "libs",
        root / "apps" / "api",
        root / "apps" / "api" / "src",
        root / "libs" / "common",
    ]
    if dry_run:
        for path in paths:
            typer.echo(f"Would create {path}")
        return
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    (root / "apps" / "api" / "main.py").write_text(_main_template(), encoding="utf-8")
    typer.echo(f"Created FaNest workspace in {root}")


@app.command()
def info() -> None:
    typer.echo(f"FaNest {fanest.__version__}")
    typer.echo(f"Python {platform.python_version()}")
    typer.echo(f"Platform {platform.platform()}")


@app.command()
def build(path: str = typer.Argument("src")) -> None:
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"Build path not found: {path}")
    if not compileall.compile_dir(str(target), quiet=1):
        raise typer.Exit(1)
    typer.echo(f"Build OK: {target}")


@app.command()
def start(
    app_path: str = "main:app",
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = True,
) -> None:
    import uvicorn

    uvicorn.run(app_path, host=host, port=port, reload=reload)


@app.command()
def dev(
    path: str = typer.Argument("main.py"),
    host: str = "127.0.0.1",
    port: int = 8000,
    app_name: str = typer.Option("app", "--app"),
) -> None:
    _run_uvicorn(_resolve_app_path(path, app_name), host=host, port=port, reload=True)


@app.command()
def run(
    path: str = typer.Argument("main.py"),
    host: str = "0.0.0.0",
    port: int = 8000,
    app_name: str = typer.Option("app", "--app"),
    workers: int | None = typer.Option(None, "--workers"),
) -> None:
    options: dict[str, Any] = {}
    if workers is not None:
        options["workers"] = workers
    _run_uvicorn(_resolve_app_path(path, app_name), host=host, port=port, reload=False, **options)


@app.command()
def check(
    path: str = typer.Argument("main.py"),
    app_name: str = typer.Option("app", "--app"),
) -> None:
    app_path = _resolve_app_path(path, app_name)
    module_name, _, attribute = app_path.partition(":")
    if not attribute:
        raise typer.BadParameter("Application target must use module:attribute format.")
    module = _load_check_module(path, module_name)
    if not hasattr(module, attribute):
        raise typer.BadParameter(f"Application attribute not found: {attribute}")
    application = getattr(module, attribute)
    if hasattr(application, "build"):
        application = application.build()
    if not callable(application):
        raise typer.BadParameter(f"Application target is not callable: {app_path}")
    typer.echo(f"Application target OK: {app_path}")


def _load_check_module(path: str, module_name: str):
    source = Path(path)
    if source.exists() and source.is_file() and source.suffix == ".py":
        spec = importlib.util.spec_from_file_location(f"_fanest_check_{abs(hash(source.resolve()))}", source)
        if spec is None or spec.loader is None:
            raise typer.BadParameter(f"Could not import application file: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    try:
        sys.path.insert(0, str(Path.cwd()))
        return importlib.import_module(module_name)
    except Exception as exc:
        raise typer.BadParameter(f"Could not import module {module_name!r}: {exc}") from exc
    finally:
        try:
            sys.path.remove(str(Path.cwd()))
        except ValueError:
            pass


@generate_app.command("resource")
def generate_resource(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    module: str | None = typer.Option(None, "--module"),
) -> None:
    resource = _resource_dir(name, dry_run, exist_ok=False)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_service.py", _service_template(class_name), dry_run)
    _write_file(resource / f"{name}_controller.py", _controller_template(name, class_name), dry_run)
    _write_file(resource / f"{name}_module.py", _module_template(name, class_name), dry_run)
    if module:
        _register_module_import(module, name, class_name, dry_run)
    typer.echo(f"Generated resource {name}")


@generate_app.command("module")
def generate_module(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    flat: bool = typer.Option(False, "--flat"),
    module: str | None = typer.Option(None, "--module"),
) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    target = Path("src") / f"{name}_module.py" if flat else resource / f"{name}_module.py"
    _write_file(target, _module_template(name, class_name), dry_run)
    if module:
        _register_module_import(module, name, class_name, dry_run)
    typer.echo(f"Generated module {name}")


@generate_app.command("controller")
def generate_controller(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_controller.py", _controller_template(name, class_name), dry_run)
    typer.echo(f"Generated controller {name}")


@generate_app.command("service")
def generate_service(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_service.py", _service_template(class_name), dry_run)
    typer.echo(f"Generated service {name}")


@generate_app.command("guard")
def generate_guard(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_guard.py", _guard_template(class_name), dry_run)
    typer.echo(f"Generated guard {name}")


@generate_app.command("pipe")
def generate_pipe(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_pipe.py", _pipe_template(class_name), dry_run)
    typer.echo(f"Generated pipe {name}")


@generate_app.command("interceptor")
def generate_interceptor(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_interceptor.py", _interceptor_template(class_name), dry_run)
    typer.echo(f"Generated interceptor {name}")


@generate_app.command("filter")
def generate_filter(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_filter.py", _filter_template(class_name), dry_run)
    typer.echo(f"Generated filter {name}")


@generate_app.command("gateway")
def generate_gateway(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_gateway.py", _gateway_template(name, class_name), dry_run)
    typer.echo(f"Generated gateway {name}")


@generate_app.command("dto")
def generate_dto(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_dto.py", _dto_template(class_name), dry_run)
    typer.echo(f"Generated dto {name}")


@generate_app.command("middleware")
def generate_middleware(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_middleware.py", _middleware_template(class_name), dry_run)
    typer.echo(f"Generated middleware {name}")


@generate_app.command("decorator")
def generate_decorator(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    _write_file(resource / f"{name}_decorator.py", _decorator_template(name), dry_run)
    typer.echo(f"Generated decorator {name}")


@generate_app.command("library")
def generate_library(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    target = Path("libs") / name
    class_name = _class_name(name)
    _write_file(target / "__init__.py", f"from .{name}_module import {class_name}Module\n", dry_run)
    _write_file(target / f"{name}_module.py", _library_template(class_name), dry_run)
    typer.echo(f"Generated library {name}")


@generate_app.command("class")
def generate_class(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}.py", _class_template(class_name), dry_run)
    typer.echo(f"Generated class {name}")


@generate_app.command("provider")
def generate_provider(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_provider.py", _provider_template(class_name), dry_run)
    typer.echo(f"Generated provider {name}")


@generate_app.command("exception")
def generate_exception(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_exception.py", _exception_template(class_name), dry_run)
    typer.echo(f"Generated exception {name}")


@generate_app.command("resolver")
def generate_resolver(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_resolver.py", _resolver_template(class_name), dry_run)
    typer.echo(f"Generated resolver {name}")


@generate_app.command("repository")
def generate_repository(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_repository.py", _repository_template(class_name), dry_run)
    typer.echo(f"Generated repository {name}")


@generate_app.command("test")
def generate_test(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    target = Path("tests") / f"test_{name}.py"
    class_name = _class_name(name)
    _write_file(target, _test_template(class_name), dry_run)
    typer.echo(f"Generated test {name}")


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _ensure_resource_dir(name: str, *, exist_ok: bool = True) -> Path:
    Path("src").mkdir(exist_ok=True)
    (Path("src") / "__init__.py").touch()
    resource = Path("src") / name
    resource.mkdir(parents=True, exist_ok=exist_ok)
    (resource / "__init__.py").touch()
    return resource


def _resource_dir(name: str, dry_run: bool, *, exist_ok: bool = True) -> Path:
    if dry_run:
        return Path("src") / name
    return _ensure_resource_dir(name, exist_ok=exist_ok)


def _write_file(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        typer.echo(f"Would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _resolve_app_path(path: str, app_name: str = "app") -> str:
    if ":" in path:
        return path
    source = Path(path)
    if not source.exists():
        raise typer.BadParameter(f"Application file not found: {path}")
    if source.is_dir():
        for candidate in [source / "main.py", source / "src" / "main.py"]:
            if candidate.exists():
                source = candidate
                break
        else:
            raise typer.BadParameter(f"No main.py found in application directory: {path}")
    if source.suffix == ".py":
        source = source.with_suffix("")
    parts = [part for part in source.parts if part not in {".", ""}]
    module_path = ".".join(parts)
    return f"{module_path}:{app_name}"


def _run_uvicorn(app_path: str, **options: Any) -> None:
    import uvicorn

    uvicorn.run(app_path, **options)


def _register_module_import(parent_module: str, child_name: str, child_class: str, dry_run: bool) -> None:
    target = Path(parent_module)
    if not target.exists():
        target = Path("src") / parent_module
    import_line = _module_import_line(target, child_name, child_class)
    if dry_run:
        typer.echo(f"Would update {target} with {child_class}Module")
        return
    content = target.read_text(encoding="utf-8")
    if import_line not in content:
        content = import_line + content
    module_name = f"{child_class}Module"
    if "imports=[" in content and f"imports=[{module_name}" not in content:
        content = content.replace("imports=[", f"imports=[{module_name}, ", 1)
    elif "@Module(" in content and f"imports=[{module_name}" not in content:
        content = re.sub(r"@Module\((?P<body>[^)]*)\)", _module_with_import(module_name), content, count=1)
    target.write_text(content, encoding="utf-8")
    typer.echo(f"Updated {target} with {module_name}")


def _module_import_line(target: Path, child_name: str, child_class: str) -> str:
    child_module = f"{child_name}.{child_name}_module"
    try:
        target.relative_to(Path("src"))
    except ValueError:
        return f"from src.{child_module} import {child_class}Module\n"
    return f"from .{child_module} import {child_class}Module\n"


def _module_with_import(module_name: str):
    def replace(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        if not body:
            return f"@Module(imports=[{module_name}])"
        return f"@Module(imports=[{module_name}], {body})"

    return replace


def _main_template() -> str:
    return '''from fanest import Controller, FaNestFactory, Get, Injectable, Module


@Injectable()
class AppService:
    def info(self):
        return {"name": "FaNest", "status": "running"}


@Controller("/")
class AppController:
    def __init__(self, app_service: AppService):
        self.app_service = app_service

    @Get("/")
    async def index(self):
        return self.app_service.info()


@Module(controllers=[AppController], providers=[AppService])
class AppModule:
    pass


app = FaNestFactory.create(AppModule)
'''


def _project_pyproject_template(name: str) -> str:
    package_name = name.replace("_", "-")
    return f'''[project]
name = "{package_name}"
version = "0.1.0"
description = "A FaNest application"
requires-python = ">=3.10"
dependencies = [
    "fanest[standard]",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "ruff",
    "httpx",
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
'''


def _project_readme_template(name: str) -> str:
    return f'''# {name}

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
fanest dev main.py
```

Open `http://127.0.0.1:8000/docs`.
'''


def _gitignore_template() -> str:
    return '''.venv/
__pycache__/
.pytest_cache/
.ruff_cache/
*.pyc
.env
dist/
build/
*.egg-info/
'''


def _project_test_template() -> str:
    return '''from fastapi.testclient import TestClient

from main import app


def test_app_index():
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
'''


def _service_template(class_name: str) -> str:
    return f'''from fanest import Injectable


@Injectable()
class {class_name}Service:
    async def find_all(self):
        return []
'''


def _controller_template(name: str, class_name: str) -> str:
    return f'''from fanest import Controller, Get

from .{name}_service import {class_name}Service


@Controller("{name}")
class {class_name}Controller:
    def __init__(self, {name}_service: {class_name}Service):
        self.{name}_service = {name}_service

    @Get("/")
    async def find_all(self):
        return await self.{name}_service.find_all()
'''


def _module_template(name: str, class_name: str) -> str:
    return f'''from fanest import Module

from .{name}_controller import {class_name}Controller
from .{name}_service import {class_name}Service


@Module(controllers=[{class_name}Controller], providers=[{class_name}Service])
class {class_name}Module:
    pass
'''


def _guard_template(class_name: str) -> str:
    return f'''class {class_name}Guard:
    def can_activate(self, context):
        return True
'''


def _pipe_template(class_name: str) -> str:
    return f'''class {class_name}Pipe:
    def transform(self, value, metadata):
        return value
'''


def _interceptor_template(class_name: str) -> str:
    return f'''class {class_name}Interceptor:
    async def intercept(self, context, call_next):
        return await call_next()
'''


def _filter_template(class_name: str) -> str:
    return f'''class {class_name}Filter:
    def catch(self, exc, context):
        raise exc
'''


def _gateway_template(name: str, class_name: str) -> str:
    return f'''from fanest import SubscribeMessage, WebSocketGateway


@WebSocketGateway("/{name}")
class {class_name}Gateway:
    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return data
'''


def _dto_template(class_name: str) -> str:
    return f'''from __future__ import annotations

from pydantic import BaseModel


class Create{class_name}Dto(BaseModel):
    name: str


class Update{class_name}Dto(BaseModel):
    name: str | None = None
'''


def _middleware_template(class_name: str) -> str:
    return f'''class {class_name}Middleware:
    async def use(self, request, call_next):
        return await call_next(request)
'''


def _decorator_template(name: str) -> str:
    return f'''from fanest import create_param_decorator


{name} = create_param_decorator(lambda data, context: context.request.state)
'''


def _library_template(class_name: str) -> str:
    return f'''from fanest import Module


@Module()
class {class_name}Module:
    pass
'''


def _class_template(class_name: str) -> str:
    return f'''class {class_name}:
    pass
'''


def _provider_template(class_name: str) -> str:
    return f'''from fanest import Injectable


@Injectable()
class {class_name}Provider:
    pass
'''


def _exception_template(class_name: str) -> str:
    return f'''from fanest import BadRequestException


class {class_name}Exception(BadRequestException):
    pass
'''


def _resolver_template(class_name: str) -> str:
    return f'''from fanest.graphql import Query, Resolver


@Resolver()
class {class_name}Resolver:
    @Query("{class_name[0].lower() + class_name[1:]}")
    async def resolve(self):
        return {{"ok": True}}
'''


def _repository_template(class_name: str) -> str:
    return f'''from fanest import Injectable


@Injectable()
class {class_name}Repository:
    async def find_all(self):
        return []
'''


def _test_template(class_name: str) -> str:
    return f'''def test_{class_name.lower()}():
    assert True
'''
