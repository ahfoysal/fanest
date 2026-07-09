from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Render
from fanest.serve_static import ServeStaticModule


def test_serve_static_module_mounts_assets(tmp_path):
    public = tmp_path / "public"
    public.mkdir()
    (public / "hello.txt").write_text("hello static", encoding="utf-8")

    @Module(imports=[ServeStaticModule.for_root(root_path=str(public), serve_root="/public")])
    class StaticModule:
        pass

    response = TestClient(FaNestFactory.create(StaticModule)).get("/public/hello.txt")

    assert response.text == "hello static"


def test_serve_static_module_mounts_multiple_roots_and_html_indexes(tmp_path):
    public = tmp_path / "public"
    admin = tmp_path / "admin"
    public.mkdir()
    admin.mkdir()
    (public / "hello.txt").write_text("hello public", encoding="utf-8")
    (admin / "index.html").write_text("<h1>Admin</h1>", encoding="utf-8")

    @Module(
        imports=[
            ServeStaticModule.for_roots(
                [
                    {"root_path": str(public), "serve_root": "/public", "name": "public"},
                    {"root_path": str(admin), "serve_root": "/admin", "name": "admin", "html": True},
                ]
            )
        ]
    )
    class MultiStaticModule:
        pass

    client = TestClient(FaNestFactory.create(MultiStaticModule))

    assert client.get("/public/hello.txt").text == "hello public"
    assert client.get("/admin").text == "<h1>Admin</h1>"


def test_serve_static_module_rejects_invalid_mount_configuration(tmp_path):
    missing = tmp_path / "missing"
    file_root = tmp_path / "asset.txt"
    file_root.write_text("not a directory", encoding="utf-8")

    try:
        ServeStaticModule.for_root(root_path=str(missing))
    except FileNotFoundError as exc:
        assert "Static assets directory not found" in str(exc)
    else:
        raise AssertionError("missing static directory should fail")

    try:
        ServeStaticModule.for_root(root_path=str(file_root))
    except NotADirectoryError as exc:
        assert "must be a directory" in str(exc)
    else:
        raise AssertionError("file static root should fail")

    try:
        ServeStaticModule.for_root(root_path=str(tmp_path), serve_root="assets")
    except ValueError as exc:
        assert "serve_root" in str(exc)
    else:
        raise AssertionError("relative static mount should fail")


def test_render_decorator_returns_html_response(tmp_path):
    template = tmp_path / "index.html"
    template.write_text("<h1>{{ title }}</h1>", encoding="utf-8")

    @Controller("pages")
    class PagesController:
        @Render(str(template))
        @Get("/")
        async def index(self):
            return {"title": "FaNest"}

    @Module(controllers=[PagesController])
    class PagesModule:
        pass

    response = TestClient(FaNestFactory.create(PagesModule)).get("/pages")

    assert response.headers["content-type"].startswith("text/html")
    assert response.text == "<h1>FaNest</h1>"
