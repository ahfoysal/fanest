import html
import json

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Put, Redirect
from fanest.inertia import InertiaModule, InertiaService


@Controller("")
class InertiaPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/users")
    async def users(self):
        return await self.inertia.render(
            "Users/Index",
            {
                "users": [{"id": 1, "name": "Ada"}],
                "stats": self.inertia.lazy(lambda: {"count": 99}),
                "app_name": self.inertia.always("Aurora"),
                "feed": self.inertia.defer(lambda: [1, 2, 3], group="feed"),
                "messages": self.inertia.merge(["hi"]),
            },
        )

    @Put("/u/{item_id}")
    @Redirect("/users")
    async def update(self, item_id: int):
        return None

    @Get("/external")
    async def external(self):
        return self.inertia.location("https://external.example.com")


@Module(
    imports=[
        InertiaModule.for_root(
            version="v1",
            vite={"dev_server": "http://localhost:5173", "entrypoints": ["src/main.tsx"]},
            share=lambda request: {"auth": {"user": "ada"}, "flash": {}},
        )
    ],
    controllers=[InertiaPages],
)
class InertiaApp:
    pass


def _client():
    return TestClient(FaNestFactory.create(InertiaApp), raise_server_exceptions=False)


def _page_from_html(text: str) -> dict:
    encoded = text.split('data-page="')[1].split('"></div>')[0]
    return json.loads(html.unescape(encoded))


def test_first_visit_returns_html_with_page_object_and_vite():
    with _client() as client:
        response = client.get("/users")
    assert "text/html" in response.headers["content-type"]
    assert "@vite/client" in response.text and "src/main.tsx" in response.text
    page = _page_from_html(response.text)
    assert page["component"] == "Users/Index"
    assert page["version"] == "v1"
    # lazy + defer excluded on first load; always + merge + shared included
    assert set(page["props"]) == {"users", "app_name", "messages", "auth", "flash"}
    assert page["deferredProps"] == {"feed": ["feed"]}
    assert page["mergeProps"] == ["messages"]
    assert page["props"]["auth"] == {"user": "ada"}


def test_inertia_visit_returns_json_page_object():
    with _client() as client:
        response = client.get("/users", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"})
    assert response.headers["x-inertia"] == "true"
    assert response.headers["vary"] == "X-Inertia"
    assert response.json()["component"] == "Users/Index"


def test_stale_asset_version_forces_full_reload():
    with _client() as client:
        response = client.get("/users", headers={"X-Inertia": "true", "X-Inertia-Version": "OLD"})
    assert response.status_code == 409
    assert response.headers["x-inertia-location"]


def test_partial_reload_only_requested_and_always_props():
    with _client() as client:
        response = client.get(
            "/users",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "v1",
                "X-Inertia-Partial-Component": "Users/Index",
                "X-Inertia-Partial-Data": "stats",
            },
        )
    props = response.json()["props"]
    assert set(props) == {"stats", "app_name"}  # lazy now included; always always included
    assert props["stats"] == {"count": 99}


def test_put_redirect_becomes_303():
    with _client() as client:
        response = client.put(
            "/u/1", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}, follow_redirects=False
        )
    assert response.status_code == 303


def test_location_external_redirect_returns_409():
    with _client() as client:
        response = client.get("/external", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"})
    assert response.status_code == 409
    assert response.headers["x-inertia-location"] == "https://external.example.com"
