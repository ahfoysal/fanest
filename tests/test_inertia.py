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
    # lazy + defer excluded on first load; always + merge + shared included; errors always present
    assert set(page["props"]) == {"users", "app_name", "messages", "auth", "flash", "errors"}
    assert page["deferredProps"] == {"feed": ["feed"]}
    assert page["mergeProps"] == ["messages"]
    assert page["props"]["auth"] == {"user": "ada"}
    # history booleans are always emitted (Inertia v2)
    assert page["clearHistory"] is False and page["encryptHistory"] is False


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
    assert set(props) == {"stats", "app_name", "errors"}  # lazy now included; always + errors always
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


# --------------------------------------------------------------------------- #
# Inertia v2 protocol coverage
# --------------------------------------------------------------------------- #
@Controller("v2")
class V2Pages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/page")
    async def page(self):
        return await self.inertia.render(
            "V2",
            {
                "feed": self.inertia.merge([1, 2]),                               # shallow merge
                "chat": self.inertia.deep_merge({"messages": []}, match_on=["id"]),  # deep + matchOn
                "user": {"name": "Ada", "email": "ada@x.com", "secret": "x"},
                "errors": {"name": "Name is required"},
            },
        )


@Module(imports=[InertiaModule.for_root(version="v2", root_element="root")], controllers=[V2Pages])
class V2App:
    pass


def _v2_client():
    return TestClient(FaNestFactory.create(V2App), raise_server_exceptions=False)


def test_v2_merge_deepmerge_matchon():
    with _v2_client() as client:
        page = client.get("/v2/page", headers={"X-Inertia": "true", "X-Inertia-Version": "v2"}).json()
    assert page["mergeProps"] == ["feed"]
    assert page["deepMergeProps"] == ["chat"]
    assert page["matchPropsOn"] == ["chat.id"]


def test_v2_error_bag_nesting():
    with _v2_client() as client:
        page = client.get(
            "/v2/page",
            headers={"X-Inertia": "true", "X-Inertia-Version": "v2", "X-Inertia-Error-Bag": "createUser"},
        ).json()
    assert page["props"]["errors"] == {"createUser": {"name": "Name is required"}}


def test_v2_reset_header_drops_merge():
    with _v2_client() as client:
        page = client.get(
            "/v2/page",
            headers={"X-Inertia": "true", "X-Inertia-Version": "v2", "X-Inertia-Reset": "feed"},
        ).json()
    # feed was reset -> no longer advertised as a merge prop (client replaces)
    assert "feed" not in page.get("mergeProps", [])


def test_v2_dot_notation_partial_only():
    with _v2_client() as client:
        page = client.get(
            "/v2/page",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "v2",
                "X-Inertia-Partial-Component": "V2",
                "X-Inertia-Partial-Data": "user.name",
            },
        ).json()
    # only user.name requested -> nested prune keeps just that path (+ always errors)
    assert page["props"]["user"] == {"name": "Ada"}


def test_v2_configurable_root_element():
    with _v2_client() as client:
        html_text = client.get("/v2/page").text
    assert 'id="root" data-page=' in html_text
