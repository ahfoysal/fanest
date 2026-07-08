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
