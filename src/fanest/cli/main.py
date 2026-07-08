from pathlib import Path

import typer

app = typer.Typer(help="FaNest command line tools.")
generate_app = typer.Typer(help="Generate FaNest artifacts.")
app.add_typer(generate_app, name="generate")


@app.command()
def new(name: str, dry_run: bool = typer.Option(False, "--dry-run")) -> None:
    target = Path(name)
    if dry_run:
        typer.echo(f"Would create FaNest application in {target}")
        return
    target.mkdir(parents=True, exist_ok=False)
    (target / "src").mkdir()
    (target / "src" / "__init__.py").write_text("", encoding="utf-8")
    (target / "main.py").write_text(_main_template(), encoding="utf-8")
    typer.echo(f"Created FaNest application in {target}")


@app.command()
def start(
    app_path: str = "main:app",
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = True,
) -> None:
    import uvicorn

    uvicorn.run(app_path, host=host, port=port, reload=reload)


@generate_app.command("resource")
def generate_resource(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    resource = _resource_dir(name, dry_run, exist_ok=False)
    class_name = _class_name(name)
    _write_file(resource / f"{name}_service.py", _service_template(class_name), dry_run)
    _write_file(resource / f"{name}_controller.py", _controller_template(name, class_name), dry_run)
    _write_file(resource / f"{name}_module.py", _module_template(name, class_name), dry_run)
    typer.echo(f"Generated resource {name}")


@generate_app.command("module")
def generate_module(
    name: str,
    dry_run: bool = typer.Option(False, "--dry-run"),
    flat: bool = typer.Option(False, "--flat"),
) -> None:
    resource = _resource_dir(name, dry_run)
    class_name = _class_name(name)
    target = Path("src") / f"{name}_module.py" if flat else resource / f"{name}_module.py"
    _write_file(target, _module_template(name, class_name), dry_run)
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


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def _ensure_resource_dir(name: str, *, exist_ok: bool = True) -> Path:
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
    return f'''from pydantic import BaseModel


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
