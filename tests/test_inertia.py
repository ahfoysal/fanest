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


def test_except_only_partial_keeps_all_other_props():
    with _client() as client:
        page = client.get(
            "/users",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "v1",
                "X-Inertia-Partial-Component": "Users/Index",
                "X-Inertia-Partial-Except": "users",
            },
        ).json()
    props = page["props"]
    # No Partial-Data header: everything except the excepted key is returned,
    # including previously ignored-on-first-load props requested by partials.
    assert "users" not in props
    assert "stats" in props
    assert "app_name" in props
    assert "auth" in props


def test_only_302_is_upgraded_to_303_for_put():
    with _client() as client:
        response = client.put(
            "/u/1", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}, follow_redirects=False
        )
    assert response.status_code == 303
    # And the version header is not leaked on JSON responses (Laravel parity).
    with _client() as client:
        json_response = client.get("/users", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"})
    assert "x-inertia-version" not in json_response.headers


# --------------------------------------------------------------------------- #
# Session integration: flash, validation errors, back(), empty responses
# --------------------------------------------------------------------------- #
from fanest import Post  # noqa: E402
from fanest.session import SessionModule  # noqa: E402


@Controller("forms")
class FormPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/edit")
    async def edit(self):
        return await self.inertia.render("Forms/Edit", {"values": {"name": ""}})

    @Post("/submit")
    async def submit(self):
        return self.inertia.with_errors({"name": "Name is required."})

    @Post("/submit-bagged")
    async def submit_bagged(self):
        return self.inertia.with_errors({"email": "Invalid."}, error_bag="newsletter")

    @Post("/flash")
    async def flash(self):
        self.inertia.flash("status", "Saved!")
        return self.inertia.back(fallback="/forms/edit")

    @Get("/status")
    async def status(self):
        return await self.inertia.render(
            "Forms/Status", {"status": self.inertia.get_flash("status")}
        )

    @Post("/empty")
    async def empty(self):
        return None


@Module(
    imports=[
        SessionModule.for_root(secret_key="inertia-session-secret"),
        InertiaModule.for_root(version="v1"),
    ],
    controllers=[FormPages],
)
class FormApp:
    pass


def _form_client():
    return TestClient(FaNestFactory.create(FormApp), raise_server_exceptions=False)


def test_validation_errors_flash_and_share_on_next_request():
    with _form_client() as client:
        response = client.post(
            "/forms/submit",
            headers={"X-Inertia": "true", "Referer": "http://testserver/forms/edit"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "http://testserver/forms/edit"

        page = client.get("/forms/edit", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}).json()
        assert page["props"]["errors"] == {"name": "Name is required."}

        # flash is consumed: errors are gone on the request after
        page = client.get("/forms/edit", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}).json()
        assert page["props"]["errors"] == {}


def test_error_bags_nest_flashed_errors():
    with _form_client() as client:
        client.post("/forms/submit-bagged", headers={"X-Inertia": "true"}, follow_redirects=False)
        page = client.get(
            "/forms/edit",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "v1",
                "X-Inertia-Error-Bag": "newsletter",
            },
        ).json()
    assert page["props"]["errors"] == {"newsletter": {"email": "Invalid."}}


def test_flash_data_available_exactly_once():
    with _form_client() as client:
        client.post("/forms/flash", headers={"X-Inertia": "true"}, follow_redirects=False)
        first = client.get("/forms/status", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}).json()
        second = client.get("/forms/status", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}).json()
    assert first["props"]["status"] == "Saved!"
    assert second["props"]["status"] is None


def test_flashed_errors_survive_a_version_mismatch_reload():
    with _form_client() as client:
        client.post("/forms/submit", headers={"X-Inertia": "true"}, follow_redirects=False)
        # stale version -> 409 forced reload; errors must be reflashed
        stale = client.get("/forms/edit", headers={"X-Inertia": "true", "X-Inertia-Version": "OLD"})
        assert stale.status_code == 409
        page = client.get("/forms/edit", headers={"X-Inertia": "true", "X-Inertia-Version": "v1"}).json()
    assert page["props"]["errors"] == {"name": "Name is required."}


def test_empty_inertia_response_redirects_back():
    with _form_client() as client:
        response = client.post(
            "/forms/empty",
            headers={"X-Inertia": "true", "Referer": "http://testserver/forms/edit"},
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"] == "http://testserver/forms/edit"


def test_non_inertia_empty_response_is_untouched():
    with _form_client() as client:
        response = client.post("/forms/empty", follow_redirects=False)
    assert response.status_code == 200
