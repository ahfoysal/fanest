from pathlib import Path
import code
import compileall
from contextlib import contextmanager
import errno
import importlib
import importlib.metadata as importlib_metadata
import json
import keyword
import platform
import py_compile
import re
import socket
import sys
import types
from typing import Any, Iterator

import typer
import fanest

app = typer.Typer(help="FaNest command line tools.")
generate_app = typer.Typer(help="Generate FaNest artifacts.")
app.add_typer(generate_app, name="generate")
app.add_typer(generate_app, name="g", help="Alias for generate.")

WORKSPACE_CONFIG = "fanest.json"


@app.command()
def new(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    _validate_project_name(name)
    target = Path(name)
    if target.exists() and not dry_run and not force:
        raise typer.BadParameter(f"Target directory already exists: {target}")
    if dry_run:
        typer.echo(f"Would create FaNest application in {target}")
        return
    target.mkdir(parents=True, exist_ok=force)
    (target / "tests").mkdir(exist_ok=force)
    _write_file(target / "main.py", _main_template(), dry_run, force=force)
    _write_file(target / "pyproject.toml", _project_pyproject_template(name), dry_run, force=force)
    _write_file(target / "README.md", _project_readme_template(name), dry_run, force=force)
    _write_file(target / ".gitignore", _gitignore_template(), dry_run, force=force)
    _write_file(target / "tests" / "test_app.py", _project_test_template(), dry_run, force=force)
    typer.echo(f"Created FaNest application in {target}")


@app.command()
def workspace(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    _validate_project_name(name)
    root = Path(name)
    if root.exists() and not dry_run and not force:
        raise typer.BadParameter(f"Target directory already exists: {root}")
    paths = [
        root / "apps",
        root / "libs",
        root / "apps" / "api",
        root / "apps" / "api" / "src",
        root / "apps" / "api" / "tests",
        root / "libs" / "common",
    ]
    if dry_run:
        for path in paths:
            typer.echo(f"Would create {path}")
        return
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    _write_file(root / "apps" / "api" / "src" / "__init__.py", "", dry_run, force=force)
    _write_file(root / "apps" / "api" / "src" / "main.py", _main_template(), dry_run, force=force)
    _write_file(
        root / "apps" / "api" / "main.py",
        _workspace_entrypoint_template(),
        dry_run,
        force=force,
    )
    _write_file(
        root / "apps" / "api" / "tests" / "test_app.py",
        _project_test_template(),
        dry_run,
        force=force,
    )
    _write_file(root / "pyproject.toml", _workspace_pyproject_template(name), dry_run, force=force)
    _write_file(root / WORKSPACE_CONFIG, _workspace_config_template("api"), dry_run, force=force)
    _write_file(root / ".gitignore", _gitignore_template(), dry_run, force=force)
    typer.echo(f"Created FaNest workspace in {root}")


@app.command()
def info() -> None:
    typer.echo(f"FaNest {fanest.__version__}")
    typer.echo(f"Python {platform.python_version()}")
    typer.echo(f"Platform {platform.platform()}")
    typer.echo(f"Executable {sys.executable}")
    for package in ("fastapi", "uvicorn", "pydantic", "typer"):
        typer.echo(f"{package} {_package_version(package)}")


def _package_version(package: str) -> str:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return "not installed"


@app.command()
def build(path: str = typer.Argument(".")) -> None:
    target = Path(path)
    if not target.exists():
        raise typer.BadParameter(f"Build path not found: {path}")
    if not _compile_target(target):
        raise typer.Exit(1)
    typer.echo(f"Build OK: {target}")


@app.command()
def start(
    app_path: str = "main:app",
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = True,
) -> None:
    _run_uvicorn(app_path, host=host, port=port, reload=reload)


@app.command()
def dev(
    path: str = typer.Argument("main.py"),
    host: str = "127.0.0.1",
    port: int = 8000,
    app_name: str = typer.Option("app", "--app"),
) -> None:
    app_path, app_dir = _resolve_app_target(path, app_name)
    _run_uvicorn(app_path, app_dir=str(app_dir), host=host, port=port, reload=True)


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
    app_path, app_dir = _resolve_app_target(path, app_name)
    _run_uvicorn(app_path, app_dir=str(app_dir), host=host, port=port, reload=False, **options)


@app.command()
def check(
    path: str = typer.Argument("main.py"),
    app_name: str = typer.Option("app", "--app"),
) -> None:
    app_path, app_dir = _resolve_app_target(path, app_name)
    module_name, _, attribute = app_path.partition(":")
    if not attribute:
        raise typer.BadParameter("Application target must use module:attribute format.")
    module = _load_check_module(path, module_name, app_dir)
    if not hasattr(module, attribute):
        raise typer.BadParameter(f"Application attribute not found: {attribute}")
    application = getattr(module, attribute)
    if hasattr(application, "build"):
        application = application.build()
    if not callable(application):
        raise typer.BadParameter(f"Application target is not callable: {app_path}")
    typer.echo(f"Application target OK: {app_path}")


@app.command()
def repl(
    path: str = typer.Argument("main.py"),
    app_name: str = typer.Option("app", "--app"),
    command: str | None = typer.Option(None, "--command", "-c"),
) -> None:
    app_path, app_dir = _resolve_app_target(path, app_name)
    module_name, _, attribute = app_path.partition(":")
    module = _load_check_module(path, module_name, app_dir)
    if not hasattr(module, attribute):
        raise typer.BadParameter(f"Application attribute not found: {attribute}")
    application = getattr(module, attribute)
    if hasattr(application, "build"):
        application = application.build()
    namespace = {
        "app": application,
        "graph": _repl_graph(application),
        "module": module,
        attribute: application,
    }
    typer.echo(f"FaNest REPL loaded {app_path}")
    if command:
        exec(command, namespace)
        return
    code.interact(local=namespace, banner="")


def _load_check_module(path: str, module_name: str, app_dir: Path | None = None):
    source = Path(path)
    if source.exists() and source.is_file() and source.suffix == ".py":
        import_root = app_dir or _app_dir_for_source(source)
        package_name = _package_name_for_source(source, import_root)
        check_name = module_name if package_name else f"_fanest_check_{abs(hash(source.resolve()))}"
        module = types.ModuleType(check_name)
        module.__file__ = str(source.resolve())
        module.__package__ = package_name
        with _prepended_sys_path(import_root):
            _clear_local_package_cache()
            importlib.invalidate_caches()
            with _temporary_module(check_name, module):
                exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), module.__dict__)
            _clear_local_package_cache(module_name)
        return module
    try:
        with _prepended_sys_path(app_dir or Path.cwd()):
            _clear_local_package_cache(module_name)
            importlib.invalidate_caches()
            return importlib.import_module(module_name)
    except Exception as exc:
        raise typer.BadParameter(f"Could not import module {module_name!r}: {exc}") from exc


@contextmanager
def _prepended_sys_path(path: Path) -> Iterator[None]:
    resolved = str(path.resolve())
    added = resolved not in sys.path
    if added:
        sys.path.insert(0, resolved)
    try:
        yield
    finally:
        if added:
            try:
                sys.path.remove(resolved)
            except ValueError:
                pass


@contextmanager
def _temporary_module(name: str, module: types.ModuleType) -> Iterator[None]:
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _clear_local_package_cache(target_module: str | None = None) -> None:
    packages = {"src", "app"}
    if target_module:
        sys.modules.pop(target_module, None)
        packages.add(target_module.split(".", 1)[0])
    for package in packages:
        if not (Path.cwd() / package).is_dir():
            continue
        for module_name in list(sys.modules):
            if module_name == package or module_name.startswith(f"{package}."):
                sys.modules.pop(module_name, None)


@generate_app.command("resource")
@generate_app.command("res")
def generate_resource(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
    module: str | None = typer.Option(None, "--module"),
    project: str | None = typer.Option(None, "--project", "-p"),
) -> None:
    name = _artifact_name(name)
    source_root = _source_root(project)
    if module and not dry_run:
        # Validate the parent module up front so a missing --module target fails
        # cleanly before any resource files are written.
        _resolve_parent_module(module, source_root)
    resource = _resource_dir(name, dry_run, exist_ok=force, source_root=source_root)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_service.py", _service_template(class_name), dry_run, force=force)
    _write_file(
        resource / f"{name}_controller.py",
        _controller_template(name, class_name),
        dry_run,
        force=force,
    )
    _write_file(
        resource / f"{name}_dto.py",
        _dto_template(class_name),
        dry_run,
        force=force,
    )
    _write_file(
        resource / f"{name}_module.py",
        _resource_module_template(name, class_name),
        dry_run,
        force=force,
    )
    if module:
        _register_module_import(module, name, class_name, dry_run, source_root=source_root)
    typer.echo(f"Generated resource {name}")


@generate_app.command("module")
@generate_app.command("mo")
def generate_module(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
    flat: bool = typer.Option(False, "--flat"),
    module: str | None = typer.Option(None, "--module"),
    project: str | None = typer.Option(None, "--project", "-p"),
) -> None:
    name = _artifact_name(name)
    source_root = _source_root(project)
    if module and not dry_run:
        _resolve_parent_module(module, source_root)
    class_name = _class_name(name)
    if flat:
        _validate_artifact_name(name)
        if not dry_run:
            source_root.mkdir(parents=True, exist_ok=True)
            (source_root / "__init__.py").touch()
        target = source_root / f"{name}_module.py"
    else:
        resource = _resource_dir(name, dry_run, source_root=source_root)
        target = resource / f"{name}_module.py"
    _write_file(target, _module_template(name, class_name), dry_run, force=force)
    if module:
        _register_module_import(module, name, class_name, dry_run, source_root=source_root, flat=flat)
    typer.echo(f"Generated module {name}")


@generate_app.command("controller")
@generate_app.command("co")
def generate_controller(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
    project: str | None = typer.Option(None, "--project", "-p"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run, source_root=_source_root(project))
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_controller.py",
        _controller_template(name, class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated controller {name}")


@generate_app.command("service")
@generate_app.command("s")
def generate_service(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
    project: str | None = typer.Option(None, "--project", "-p"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run, source_root=_source_root(project))
    class_name = _class_name(name)
    _write_file(resource / f"{name}_service.py", _service_template(class_name), dry_run, force=force)
    typer.echo(f"Generated service {name}")


@generate_app.command("guard")
@generate_app.command("gu")
def generate_guard(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_guard.py", _guard_template(class_name), dry_run, force=force)
    typer.echo(f"Generated guard {name}")


@generate_app.command("pipe")
@generate_app.command("pi")
def generate_pipe(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_pipe.py", _pipe_template(class_name), dry_run, force=force)
    typer.echo(f"Generated pipe {name}")


@generate_app.command("interceptor")
@generate_app.command("itc")
def generate_interceptor(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_interceptor.py",
        _interceptor_template(class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated interceptor {name}")


@generate_app.command("filter")
@generate_app.command("f")
def generate_filter(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_filter.py", _filter_template(class_name), dry_run, force=force)
    typer.echo(f"Generated filter {name}")


@generate_app.command("gateway")
@generate_app.command("ga")
def generate_gateway(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_gateway.py",
        _gateway_template(name, class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated gateway {name}")


@generate_app.command("dto")
def generate_dto(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_dto.py", _dto_template(class_name), dry_run, force=force)
    typer.echo(f"Generated dto {name}")


@generate_app.command("middleware")
@generate_app.command("mi")
def generate_middleware(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_middleware.py",
        _middleware_template(class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated middleware {name}")


@generate_app.command("decorator")
@generate_app.command("d")
def generate_decorator(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    _write_file(resource / f"{name}_decorator.py", _decorator_template(name), dry_run, force=force)
    typer.echo(f"Generated decorator {name}")


@generate_app.command("application")
@generate_app.command("app")
def generate_application(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    project_root = Path("apps") / name
    target = _workspace_scaffold_path(project_root)
    _write_file(target / "src" / "__init__.py", "", dry_run, force=force)
    _write_file(target / "src" / "main.py", _main_template(), dry_run, force=force)
    _write_file(target / "main.py", _workspace_entrypoint_template(), dry_run, force=force)
    _write_file(target / "tests" / "test_app.py", _project_test_template(), dry_run, force=force)
    _update_workspace_project(
        name,
        project_type="application",
        root=project_root,
        source_root=project_root / "src",
        dry_run=dry_run,
    )
    typer.echo(f"Generated application {name}")


@generate_app.command("library")
@generate_app.command("lib")
def generate_library(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    project_root = Path("libs") / name
    target = _workspace_scaffold_path(project_root)
    class_name = _class_name(name)
    _write_file(_workspace_scaffold_path(Path("libs")) / "__init__.py", "", dry_run, force=force)
    _write_file(
        target / "__init__.py",
        f"from .{name}_module import {class_name}Module\n",
        dry_run,
        force=force,
    )
    _write_file(target / f"{name}_module.py", _library_template(class_name), dry_run, force=force)
    _update_workspace_project(
        name,
        project_type="library",
        root=project_root,
        source_root=project_root,
        dry_run=dry_run,
    )
    typer.echo(f"Generated library {name}")


@generate_app.command("plugin")
def generate_plugin(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_plugin.py", _plugin_template(name, class_name), dry_run, force=force)
    typer.echo(f"Generated plugin {name}")


@generate_app.command("class")
@generate_app.command("cl")
def generate_class(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}.py", _class_template(class_name), dry_run, force=force)
    typer.echo(f"Generated class {name}")


@generate_app.command("provider")
@generate_app.command("pr")
def generate_provider(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_provider.py", _provider_template(class_name), dry_run, force=force)
    typer.echo(f"Generated provider {name}")


@generate_app.command("exception")
@generate_app.command("ex")
def generate_exception(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_exception.py",
        _exception_template(class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated exception {name}")


@generate_app.command("resolver")
@generate_app.command("r")
def generate_resolver(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_resolver.py", _resolver_template(class_name), dry_run, force=force)
    typer.echo(f"Generated resolver {name}")


@generate_app.command("repository")
@generate_app.command("repo")
def generate_repository(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_repository.py",
        _repository_template(class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated repository {name}")


@generate_app.command("command")
@generate_app.command("cmd")
def generate_command(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    _write_file(
        resource / f"{name}_command.py",
        _command_template(name, class_name),
        dry_run,
        force=force,
    )
    typer.echo(f"Generated command {name}")


@generate_app.command("test")
@generate_app.command("spec")
def generate_test(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    name = _artifact_name(name)
    target = Path("tests") / f"test_{name}.py"
    class_name = _class_name(name)
    _write_file(target, _test_template(class_name), dry_run, force=force)
    typer.echo(f"Generated test {name}")


def _artifact_name(name: str) -> str:
    normalized = name.strip().replace("-", "_")
    _validate_artifact_name(normalized)
    return normalized


def _validate_project_name(name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise typer.BadParameter("Project name must be a single directory name.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise typer.BadParameter(
            "Project name may contain only letters, numbers, dots, dashes and underscores."
        )


def _validate_artifact_name(name: str) -> None:
    """Reject names that would escape the project or produce invalid Python.

    A generator name becomes both a directory/file name and a Python class/module
    identifier, so it must be a plain Python identifier: no path separators or
    ``..`` (path traversal), no leading digits, no spaces/dashes, and not a keyword.
    """
    if not name or not name.isidentifier() or keyword.iskeyword(name):
        raise typer.BadParameter(
            f"Invalid name {name!r}: use a valid Python identifier or kebab-case name "
            "(letters, digits, dashes and underscores; not starting with a digit; "
            "not a reserved keyword), e.g. 'user_profile'."
        )


def _class_name(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def _source_root(project: str | None = None) -> Path:
    workspace_root = _find_workspace_root()
    if workspace_root is None:
        if project:
            raise typer.BadParameter("--project requires a FaNest workspace with fanest.json.")
        return Path("src")
    config = _load_workspace_config(workspace_root)
    project_name = project or _current_workspace_project(workspace_root, config) or config.get("defaultProject")
    projects = config.get("projects", {})
    if not isinstance(project_name, str) or project_name not in projects:
        available = ", ".join(sorted(projects)) or "none"
        raise typer.BadParameter(f"Unknown workspace project {project_name!r}. Available projects: {available}.")
    project_config = projects[project_name]
    if not isinstance(project_config, dict) or not isinstance(project_config.get("sourceRoot"), str):
        raise typer.BadParameter(f"Workspace project {project_name!r} is missing sourceRoot.")
    return _workspace_path(workspace_root, project_config["sourceRoot"])


def _find_workspace_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    candidates = [current, *current.parents]
    for candidate in candidates:
        if (candidate / WORKSPACE_CONFIG).exists():
            return candidate
    return None


def _load_workspace_config(root: Path) -> dict[str, Any]:
    try:
        config = json.loads((root / WORKSPACE_CONFIG).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid {WORKSPACE_CONFIG}: {exc}") from exc
    if not isinstance(config, dict):
        raise typer.BadParameter(f"{WORKSPACE_CONFIG} must contain a JSON object.")
    config.setdefault("projects", {})
    return config


def _current_workspace_project(root: Path, config: dict[str, Any]) -> str | None:
    cwd = Path.cwd().resolve()
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return None
    for name, project in projects.items():
        if not isinstance(project, dict) or not isinstance(project.get("root"), str):
            continue
        project_root = _workspace_path(root, project["root"]).resolve()
        try:
            cwd.relative_to(project_root)
        except ValueError:
            continue
        return str(name)
    return None


def _workspace_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    absolute = root / path
    try:
        return absolute.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return absolute


def _workspace_scaffold_path(path: Path) -> Path:
    workspace_root = _find_workspace_root()
    if workspace_root is None:
        return path
    return _workspace_path(workspace_root, path)


def _update_workspace_project(
    name: str,
    *,
    project_type: str,
    root: Path,
    source_root: Path,
    dry_run: bool,
) -> None:
    workspace_root = _find_workspace_root()
    if workspace_root is None:
        return
    config = _load_workspace_config(workspace_root)
    projects = config.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise typer.BadParameter("Workspace config projects must be an object.")
    projects[name] = {
        "type": project_type,
        "root": root.as_posix(),
        "sourceRoot": source_root.as_posix(),
    }
    if dry_run:
        typer.echo(f"Would update {workspace_root / WORKSPACE_CONFIG} with {name}")
        return
    (workspace_root / WORKSPACE_CONFIG).write_text(_json_template(config), encoding="utf-8")


def _ensure_resource_dir(
    name: str,
    *,
    exist_ok: bool = True,
    source_root: Path = Path("src"),
) -> Path:
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "__init__.py").touch()
    resource = source_root / name
    resource.mkdir(parents=True, exist_ok=exist_ok)
    (resource / "__init__.py").touch()
    return resource


def _resource_dir(
    name: str,
    dry_run: bool,
    *,
    exist_ok: bool = True,
    source_root: Path = Path("src"),
) -> Path:
    _validate_artifact_name(name)
    if dry_run:
        return source_root / name
    try:
        return _ensure_resource_dir(name, exist_ok=exist_ok, source_root=source_root)
    except FileExistsError as exc:
        raise typer.BadParameter(
            f"Resource directory already exists: {source_root / name}. Use --force to overwrite."
        ) from exc


def _write_file(path: Path, content: str, dry_run: bool, *, force: bool = False) -> None:
    if dry_run:
        typer.echo(f"Would write {path}")
        return
    if path.exists() and not force:
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except UnicodeDecodeError:
            pass
        raise typer.BadParameter(f"Refusing to overwrite existing file: {path}. Use --force.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _compile_target(target: Path) -> bool:
    if target.is_file():
        try:
            py_compile.compile(str(target), doraise=True, quiet=1)
        except py_compile.PyCompileError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            return False
        return True
    ignored = re.compile(r"(^|/)(\.venv|venv|__pycache__|\.git|\.mypy_cache|\.pytest_cache|\.ruff_cache|build|dist)(/|$)")
    return compileall.compile_dir(str(target), quiet=1, rx=ignored)


def _package_name_for_source(source: Path, app_dir: Path) -> str:
    try:
        relative = source.resolve().with_suffix("").relative_to(app_dir.resolve())
    except ValueError:
        return ""
    package_parts = relative.parts[:-1]
    if not package_parts:
        return ""
    current = app_dir
    for part in package_parts:
        current = current / part
        if not (current / "__init__.py").exists():
            return ""
    return ".".join(package_parts)


def _resolve_app_target(path: str, app_name: str = "app") -> tuple[str, Path]:
    if ":" in path:
        return path, Path.cwd()
    source = _resolve_app_source(path)
    app_dir = _app_dir_for_source(source)
    module_path = _module_path_for_source(source, app_dir)
    return f"{module_path}:{app_name}", app_dir


def _resolve_app_path(path: str, app_name: str = "app") -> str:
    return _resolve_app_target(path, app_name)[0]


def _resolve_app_source(path: str) -> Path:
    if ":" in path:
        raise typer.BadParameter("Application file path must not include ':'.")
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
    if not source.is_file() or source.suffix != ".py":
        raise typer.BadParameter(f"Application path must be a Python file or directory: {path}")
    return source


def _app_dir_for_source(source: Path) -> Path:
    source = source.resolve()
    cwd = Path.cwd().resolve()
    try:
        relative = source.relative_to(cwd)
    except ValueError:
        return source.parent
    parts = relative.parts
    if len(parts) == 1:
        return cwd
    if _is_importable_from_cwd(relative):
        return cwd
    return source.parent


def _module_path_for_source(source: Path, app_dir: Path) -> str:
    source = source.resolve()
    app_dir = app_dir.resolve()
    try:
        relative = source.with_suffix("").relative_to(app_dir)
    except ValueError:
        return source.stem
    return ".".join(part for part in relative.parts if part not in {".", ""})


def _is_importable_from_cwd(relative_source: Path) -> bool:
    current = Path.cwd()
    for parent in relative_source.with_suffix("").parents:
        if str(parent) == ".":
            break
        if not (current / parent / "__init__.py").exists():
            return False
    return True


def _run_uvicorn(app_path: str, **options: Any) -> None:
    import uvicorn

    options.setdefault("app_dir", str(Path.cwd()))
    options["app_dir"] = str(Path(str(options["app_dir"])).resolve())
    _ensure_port_available(str(options.get("host", "127.0.0.1")), int(options.get("port", 8000)))
    try:
        with _prepended_sys_path(Path(str(options["app_dir"]))):
            uvicorn.run(app_path, **options)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            _port_in_use_error(int(options.get("port", 8000)))
        raise


def _ensure_port_available(host: str, port: int) -> None:
    if port == 0:
        return
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = "::" if family == socket.AF_INET6 and host in {"0.0.0.0", ""} else host
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                _port_in_use_error(port)
            raise


def _port_in_use_error(port: int) -> None:
    typer.secho(
        f"Port {port} is already in use. Try running with --port {port + 1}.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(1)


def _repl_graph(application: Any) -> dict[str, Any]:
    state = getattr(application, "state", None)
    container = getattr(state, "fanest_container", None)
    root_module = getattr(state, "fanest_root_module", None)
    routes = []
    for route in getattr(application, "routes", []):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        routes.append({"path": path, "methods": sorted(methods)})
    graph: dict[str, Any] = {
        "root_module": _display_name(root_module),
        "routes": routes,
    }
    if container is not None:
        module_providers = getattr(container, "_module_providers", {})
        module_imports = getattr(container, "_module_imports", {})
        graph["modules"] = [
            {
                "name": _display_name(module),
                "imports": [_display_name(imported) for imported in module_imports.get(module, [])],
                "providers": [_display_name(token) for token in providers],
            }
            for module, providers in module_providers.items()
        ]
    else:
        graph["modules"] = []
    return graph


def _display_name(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    name = getattr(value, "__name__", None)
    if isinstance(name, str):
        return name
    return value.__class__.__name__


def _resolve_parent_module(parent_module: str, source_root: Path) -> Path:
    """Return the existing parent module file for ``--module`` registration.

    Tries the path as given, then relative to ``source_root``. Raises a clean
    ``typer.BadParameter`` if neither exists so callers can fail fast before
    generating any files.
    """
    target = Path(parent_module)
    if target.exists():
        return target
    candidate = source_root / parent_module
    if candidate.exists():
        return candidate
    raise typer.BadParameter(
        f"Parent module not found: {parent_module}. "
        "Expected an existing module file to register the import into."
    )


def _register_module_import(
    parent_module: str,
    child_name: str,
    child_class: str,
    dry_run: bool,
    *,
    source_root: Path = Path("src"),
    flat: bool = False,
) -> None:
    if dry_run:
        target = Path(parent_module)
        if not target.exists():
            target = source_root / parent_module
        typer.echo(f"Would update {target} with {child_class}Module")
        return
    target = _resolve_parent_module(parent_module, source_root)
    import_line = _module_import_line(
        target, child_name, child_class, source_root=source_root, flat=flat
    )
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


def _module_import_line(
    target: Path,
    child_name: str,
    child_class: str,
    *,
    source_root: Path = Path("src"),
    flat: bool = False,
) -> str:
    # The child module lives at ``<source_root>/<child>/<child>_module.py`` by
    # default, or flat at ``<source_root>/<child>_module.py`` with ``--flat``.
    child_module = f"{child_name}_module" if flat else f"{child_name}.{child_name}_module"
    for base in (source_root, Path("src")):
        try:
            relative = target.resolve().relative_to(base.resolve())
        except ValueError:
            continue
        # Emit a relative import with enough leading dots to climb from the
        # parent module's package up to ``source_root`` regardless of how deeply
        # the parent is nested (e.g. ``src/app/app_module.py``).
        depth = len(relative.parts) - 1
        dots = "." * (depth + 1)
        return f"from {dots}{child_module} import {child_class}Module\n"
    # Parent lives outside the source root (e.g. a top-level main.py): fall back
    # to an absolute import rooted at the source-root package.
    package = source_root.name
    return f"from {package}.{child_module} import {child_class}Module\n"


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
    package_name = _distribution_name(name)
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


def _workspace_entrypoint_template() -> str:
    return '''from src.main import app

__all__ = ["app"]
'''


def _workspace_pyproject_template(name: str) -> str:
    package_name = _distribution_name(name)
    return f'''[project]
name = "{package_name}"
version = "0.1.0"
description = "A FaNest workspace"
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
testpaths = ["apps/api/tests"]

[tool.fanest.scripts]
"start:api" = "fanest dev apps/api/main.py"
"build:api" = "fanest build apps/api"

[tool.ruff]
line-length = 100
'''


def _workspace_config_template(default_project: str) -> str:
    return _json_template(
        {
            "version": 1,
            "defaultProject": default_project,
            "projects": {
                default_project: {
                    "type": "application",
                    "root": f"apps/{default_project}",
                    "sourceRoot": f"apps/{default_project}/src",
                    "entryFile": "main",
                }
            },
        }
    )


def _json_template(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


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


def _distribution_name(name: str) -> str:
    return name.replace("_", "-").lower()


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
    def __init__(self):
        self._items = []
        self._next_id = 1

    async def find_all(self):
        return self._items

    async def find_one(self, item_id: int):
        return next((item for item in self._items if item["id"] == item_id), None)

    async def create(self, data):
        item = {{"id": self._next_id, **data.model_dump(exclude_unset=True)}}
        self._next_id += 1
        self._items.append(item)
        return item

    async def update(self, item_id: int, data):
        item = await self.find_one(item_id)
        if item is None:
            return None
        item.update(data.model_dump(exclude_unset=True))
        return item

    async def remove(self, item_id: int):
        item = await self.find_one(item_id)
        if item is None:
            return None
        self._items.remove(item)
        return item
'''


def _controller_template(name: str, class_name: str) -> str:
    return f'''from fanest import Body, Controller, Delete, Get, Param, Patch, Post

from .{name}_dto import Create{class_name}Dto, Update{class_name}Dto
from .{name}_service import {class_name}Service


@Controller("{name}")
class {class_name}Controller:
    def __init__(self, {name}_service: {class_name}Service):
        self.{name}_service = {name}_service

    @Get("/")
    async def find_all(self):
        return await self.{name}_service.find_all()

    @Get("/{{item_id}}")
    async def find_one(self, item_id: int = Param("item_id")):
        return await self.{name}_service.find_one(item_id)

    @Post("/")
    async def create(self, data: Create{class_name}Dto = Body()):
        return await self.{name}_service.create(data)

    @Patch("/{{item_id}}")
    async def update(
        self,
        item_id: int = Param("item_id"),
        data: Update{class_name}Dto = Body(),
    ):
        return await self.{name}_service.update(item_id, data)

    @Delete("/{{item_id}}")
    async def remove(self, item_id: int = Param("item_id")):
        return await self.{name}_service.remove(item_id)
'''


def _module_template(name: str, class_name: str) -> str:
    # A standalone module (``g module``) must not import a controller/service
    # that was never generated — that would raise ModuleNotFoundError on import.
    return f'''from fanest import Module


@Module()
class {class_name}Module:
    pass
'''


def _resource_module_template(name: str, class_name: str) -> str:
    # Used by ``g resource``, which also generates the controller and service.
    return f'''from fanest import Module

from .{name}_controller import {class_name}Controller
from .{name}_dto import Create{class_name}Dto, Update{class_name}Dto
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


def _plugin_template(name: str, class_name: str) -> str:
    options_token = f"{name.upper()}_OPTIONS"
    return f'''from typing import Any

from fanest import Module, dynamic_module, token, use_value


{options_token} = token("{options_token}")


@Module()
class {class_name}Plugin:
    @staticmethod
    def register(**options: Any):
        return dynamic_module(
            {class_name}Plugin,
            providers=[use_value({options_token}, dict(options))],
            exports=[{options_token}],
        )
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


@Resolver
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


def _command_template(name: str, class_name: str) -> str:
    return f'''import typer


cli = typer.Typer(help="{class_name} command application.")


@cli.command("{name}")
def run() -> None:
    typer.echo("{name} command executed")


if __name__ == "__main__":
    cli()
'''


def _test_template(class_name: str) -> str:
    return f'''def test_{class_name.lower()}():
    assert True
'''
