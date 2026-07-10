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
    # Not an Inertia request -> the middleware leaves it at the framework default
    # (NestJS: an empty POST handler responds 201 Created).
    assert response.status_code == 201


# --------------------------------------------------------------------------- #
# Chainable builder, request-aware closures, Arrayable, testing assertions
# --------------------------------------------------------------------------- #
class _UserResource:
    def __init__(self, data):
        self.data = data

    def to_array(self):
        return {"id": self.data["id"], "name": self.data["name"]}


@Controller("b")
class BuilderPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/dash")
    async def dash(self):
        return await (
            self.inertia.render("Dash", {"a": 1})
            .with_("b", 2)
            .with_({"c": 3})
            .with_view_data(seo="hello")
            .cache(60)
            .encrypt_history(True)
        )

    @Get("/res")
    async def res(self):
        return await self.inertia.render(
            "Res",
            {
                "me": _UserResource({"id": 9, "name": "Grace", "secret": "x"}),
                "who": lambda request: request.headers.get("x-who", "anon"),
            },
        )


@Module(imports=[InertiaModule.for_root(version="1")], controllers=[BuilderPages])
class BuilderApp:
    pass


def _builder_client():
    return TestClient(FaNestFactory.create(BuilderApp), raise_server_exceptions=False)


def test_chainable_builder_with_cache_encrypt():
    with _builder_client() as client:
        page = client.get("/b/dash", headers={"X-Inertia": "true", "X-Inertia-Version": "1"}).json()
    assert {page["props"]["a"], page["props"]["b"], page["props"]["c"]} == {1, 2, 3}
    assert page["cache"] == [60]
    assert page["encryptHistory"] is True


def test_arrayable_and_request_aware_closure():
    from fanest.inertia.testing import assert_inertia

    with _builder_client() as client:
        response = client.get(
            "/b/res", headers={"X-Inertia": "true", "X-Inertia-Version": "1", "x-who": "ada"}
        )
    page = assert_inertia(response).component("Res")
    assert page.props["me"] == {"id": 9, "name": "Grace"}  # to_array, no secret
    assert page.props["who"] == "ada"  # closure received the request
    page.missing("secret")


def test_assertable_inertia_fluent_api():
    from fanest.inertia.testing import assert_inertia

    @Controller("t")
    class TC:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/users")
        async def users(self):
            return await self.inertia.render("Users", {"users": [{"name": "Ada"}, {"name": "Linus"}]})

    @Module(imports=[InertiaModule.for_root(version="9")], controllers=[TC])
    class TApp:
        pass

    with TestClient(FaNestFactory.create(TApp)) as client:
        response = client.get("/t/users", headers={"X-Inertia": "true", "X-Inertia-Version": "9"})
    (
        assert_inertia(response)
        .component("Users")
        .has("users", 2)
        .where("users.0.name", "Ada")
        .url("/t/users")
        .version("9")
        .missing("secret")
    )


# --------------------------------------------------------------------------- #
# CSRF, method spoofing, route shorthand
# --------------------------------------------------------------------------- #
def test_method_spoofing_post_to_put_delete():
    from fanest import Delete, Put

    @Controller("")
    class MC:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Put("/u/{item_id}")
        async def update(self, item_id: int):
            return {"verb": "PUT", "id": item_id}

        @Delete("/u/{item_id}")
        async def delete(self, item_id: int):
            return {"verb": "DELETE"}

    @Module(imports=[InertiaModule.for_root(version="1", csrf=False)], controllers=[MC])
    class MApp:
        pass

    with TestClient(FaNestFactory.create(MApp), raise_server_exceptions=False) as client:
        assert client.post("/u/5", data={"_method": "PUT"}).json() == {"verb": "PUT", "id": 5}
        assert client.post("/u/7", headers={"X-HTTP-Method-Override": "DELETE"}).json()["verb"] == "DELETE"
        assert client.post("/u/9", files={"f": ("a.txt", b"x")}, data={"_method": "PUT"}).json()["id"] == 9


def test_csrf_double_submit():
    @Controller("")
    class CC:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/")
        async def home(self):
            return await self.inertia.render("Home", {})

        @Post("/save")
        async def save(self):
            return {"ok": True}

    @Module(imports=[InertiaModule.for_root(version="1", csrf=True)], controllers=[CC])
    class CApp:
        pass

    with TestClient(FaNestFactory.create(CApp), raise_server_exceptions=False) as client:
        client.get("/")  # issues + persists the XSRF-TOKEN cookie in the client jar
        token = client.cookies.get("XSRF-TOKEN")
        assert token
        assert client.post("/save").status_code == 419  # cookie sent, no header -> rejected
        ok = client.post("/save", headers={"X-XSRF-TOKEN": token})  # cookie auto-sent + matching header
        assert ok.status_code == 201  # NestJS default for a POST handler returning a body


def test_inertia_route_shorthand():
    from fanest.inertia import inertia_route

    @Module(imports=[InertiaModule.for_root(version="1")], controllers=[inertia_route("/about", "About", {"team": 3})])
    class RApp:
        pass

    with TestClient(FaNestFactory.create(RApp)) as client:
        page = client.get("/about", headers={"X-Inertia": "true", "X-Inertia-Version": "1"}).json()
    assert page["component"] == "About"
    assert page["props"]["team"] == 3


# --------------------------------------------------------------------------- #
# Inertia v2 prop wrappers: merge prepend (#17), once props (#16), defer rescue (#24)
# --------------------------------------------------------------------------- #
@Controller("w")
class WrapperPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/prepend")
    async def prepend(self):
        return await self.inertia.render(
            "W",
            {
                "feed": self.inertia.merge([1, 2], prepend=True),
                "log": self.inertia.merge(["a"]),  # plain append
            },
        )

    @Get("/once")
    async def once(self):
        return await self.inertia.render(
            "W",
            {
                "config": self.inertia.once(lambda: {"theme": "dark"}, expires_at=123),
                "user": {"name": "Ada"},
            },
        )

    @Get("/share-once")
    async def share_once(self):
        self.inertia.share_once("flags", lambda: {"beta": True})
        return await self.inertia.render("W", {"user": {"name": "Ada"}})

    @Get("/rescue")
    async def rescue(self):
        def boom():
            raise ValueError("deferred prop blew up")

        return await self.inertia.render("W", {"data": self.inertia.defer(boom, rescue=True)})


@Module(imports=[InertiaModule.for_root(version="w1")], controllers=[WrapperPages])
class WrapperApp:
    pass


def _wrapper_client():
    return TestClient(FaNestFactory.create(WrapperApp), raise_server_exceptions=False)


def test_merge_prepend_emits_prepend_props():
    with _wrapper_client() as client:
        page = client.get("/w/prepend", headers={"X-Inertia": "true", "X-Inertia-Version": "w1"}).json()
    # prepend merge is advertised under prependProps, plain merge under mergeProps.
    assert page["prependProps"] == ["feed"]
    assert page["mergeProps"] == ["log"]
    assert "feed" not in page.get("mergeProps", [])


def test_deep_wins_over_prepend():
    @Controller("dp")
    class DeepPrependPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/page")
        async def page(self):
            return await self.inertia.render(
                "DP", {"chat": self.inertia.merge({"m": []}, deep=True, prepend=True)}
            )

    @Module(imports=[InertiaModule.for_root(version="1")], controllers=[DeepPrependPages])
    class DeepPrependApp:
        pass

    with TestClient(FaNestFactory.create(DeepPrependApp)) as client:
        page = client.get("/dp/page", headers={"X-Inertia": "true", "X-Inertia-Version": "1"}).json()
    # a deep+prepend prop is a deepMergeProp (inertia-laravel rejects deep from prepend/append)
    assert page["deepMergeProps"] == ["chat"]
    assert "chat" not in page.get("prependProps", [])


def test_once_props_sent_and_advertised():
    with _wrapper_client() as client:
        page = client.get("/w/once", headers={"X-Inertia": "true", "X-Inertia-Version": "w1"}).json()
    assert page["props"]["config"] == {"theme": "dark"}  # evaluated + sent
    assert page["onceProps"] == {"config": {"prop": "config", "expiresAt": 123}}


def test_once_props_skipped_when_client_already_cached_them():
    with _wrapper_client() as client:
        page = client.get(
            "/w/once",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "w1",
                "X-Inertia-Except-Once-Props": "config",
            },
        ).json()
    # client says it already has `config` -> not re-sent, not re-advertised
    assert "config" not in page["props"]
    assert "onceProps" not in page
    assert page["props"]["user"] == {"name": "Ada"}


def test_share_once_registers_a_once_prop():
    with _wrapper_client() as client:
        page = client.get("/w/share-once", headers={"X-Inertia": "true", "X-Inertia-Version": "w1"}).json()
    assert page["props"]["flags"] == {"beta": True}
    assert page["onceProps"] == {"flags": {"prop": "flags", "expiresAt": None}}


def test_defer_rescue_swallows_callback_error_on_partial():
    with _wrapper_client() as client:
        # first load advertises the deferred group without evaluating it
        first = client.get("/w/rescue", headers={"X-Inertia": "true", "X-Inertia-Version": "w1"}).json()
        assert first["deferredProps"] == {"default": ["data"]}
        assert "data" not in first["props"]
        # the follow-up partial reload evaluates it: rescue turns the error into None
        follow = client.get(
            "/w/rescue",
            headers={
                "X-Inertia": "true",
                "X-Inertia-Version": "w1",
                "X-Inertia-Partial-Component": "W",
                "X-Inertia-Partial-Data": "data",
            },
        )
    assert follow.status_code == 200
    assert follow.json()["props"]["data"] is None


# --------------------------------------------------------------------------- #
# #28 disable versioning
# --------------------------------------------------------------------------- #
def test_version_false_disables_versioning_and_409():
    @Controller("nv")
    class NVPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/page")
        async def page(self):
            return await self.inertia.render("NV", {"ok": True})

    @Module(imports=[InertiaModule.for_root(version=False)], controllers=[NVPages])
    class NVApp:
        pass

    with TestClient(FaNestFactory.create(NVApp), raise_server_exceptions=False) as client:
        # a mismatched version header must NOT force a 409 when versioning is off
        response = client.get(
            "/nv/page", headers={"X-Inertia": "true", "X-Inertia-Version": "anything"}
        )
    assert response.status_code == 200
    assert response.json()["version"] == ""


# --------------------------------------------------------------------------- #
# #14 with_all_errors normalization
# --------------------------------------------------------------------------- #
@Controller("ve")
class ErrorNormPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/errs")
    async def errs(self):
        return await self.inertia.render("E", {"errors": {"email": ["required", "invalid"]}})


def test_errors_default_to_first_message():
    @Module(imports=[InertiaModule.for_root(version="1")], controllers=[ErrorNormPages])
    class DefaultErrApp:
        pass

    with TestClient(FaNestFactory.create(DefaultErrApp)) as client:
        page = client.get("/ve/errs", headers={"X-Inertia": "true", "X-Inertia-Version": "1"}).json()
    assert page["props"]["errors"] == {"email": "required"}


def test_with_all_errors_keeps_every_message():
    @Module(
        imports=[InertiaModule.for_root(version="1", with_all_errors=True)],
        controllers=[ErrorNormPages],
    )
    class AllErrApp:
        pass

    with TestClient(FaNestFactory.create(AllErrApp)) as client:
        page = client.get("/ve/errs", headers={"X-Inertia": "true", "X-Inertia-Version": "1"}).json()
    assert page["props"]["errors"] == {"email": ["required", "invalid"]}


# --------------------------------------------------------------------------- #
# #4 / #21 exception -> Inertia error page
# --------------------------------------------------------------------------- #
def test_exception_filter_renders_inertia_error_page():
    from fanest.common.exceptions import FaNestHttpException, NotFoundException
    from fanest.inertia import InertiaExceptionFilter

    @Controller("err")
    class ErrPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/missing")
        async def missing(self):
            raise NotFoundException()

        @Get("/teapot")
        async def teapot(self):
            raise FaNestHttpException(418, "I'm a teapot")

    @Module(
        imports=[InertiaModule.for_root(version="1", share=lambda request: {"brand": "Aurora"})],
        controllers=[ErrPages],
    )
    class ErrApp:
        pass

    app = FaNestFactory.create(ErrApp, global_filters=[InertiaExceptionFilter])
    with TestClient(app, raise_server_exceptions=False) as client:
        # 404 is a configured error status -> rendered as the Error component
        resp = client.get("/err/missing", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
        assert resp.status_code == 404
        page = resp.json()
        assert page["component"] == "Error"
        assert page["props"]["status"] == 404
        assert page["props"]["brand"] == "Aurora"  # shared data re-attached

        # 418 is NOT a configured error status -> re-raised, not an Inertia page
        teapot = client.get("/err/teapot", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
        assert teapot.status_code == 418
        assert "component" not in teapot.json()


def test_exception_response_value_object():
    from fanest.inertia import ExceptionResponse

    @Controller("xr")
    class XRPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/maintenance")
        async def maintenance(self):
            resp = ExceptionResponse(self.inertia, 503, "Maintenance", {"reason": "upgrade"})
            resp.with_shared_data({"extra": "x"})
            assert resp.status_code() == 503
            return await resp.render()

    @Module(imports=[InertiaModule.for_root(version="1")], controllers=[XRPages])
    class XRApp:
        pass

    with TestClient(FaNestFactory.create(XRApp), raise_server_exceptions=False) as client:
        resp = client.get("/xr/maintenance", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
    assert resp.status_code == 503
    page = resp.json()
    assert page["component"] == "Maintenance"
    assert page["props"]["reason"] == "upgrade"
    assert page["props"]["extra"] == "x"  # with_shared_data merged in


# --------------------------------------------------------------------------- #
# #10 overridable HandleInertiaRequests middleware base
# --------------------------------------------------------------------------- #
def test_handle_inertia_requests_subclass():
    from fanest.inertia import HandleInertiaRequests

    class AppInertia(HandleInertiaRequests):
        def share(self, request):
            return {"tenant": "acme"}

        def version(self, request):
            return "handler-v"

        def root_view(self, request):
            return "custom"

        def encrypt_history(self):
            return True

    @Controller("h")
    class HPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/page")
        async def page(self):
            return await self.inertia.render("H", {"a": 1})

    @Module(
        imports=[
            InertiaModule.for_root(
                handler=AppInertia,
                template={"custom": lambda vite, head, body: f"<!DOCTYPE html><html><body>CUSTOM:{body}</body></html>"},
            )
        ],
        controllers=[HPages],
    )
    class HApp:
        pass

    with TestClient(FaNestFactory.create(HApp)) as client:
        page = client.get("/h/page", headers={"X-Inertia": "true", "X-Inertia-Version": "handler-v"}).json()
        assert page["version"] == "handler-v"
        assert page["props"]["tenant"] == "acme"
        assert page["encryptHistory"] is True
        # request-aware root_view picks the "custom" named template
        html_text = client.get("/h/page").text
    assert "CUSTOM:" in html_text


# --------------------------------------------------------------------------- #
# #34 preserve_fragment
# --------------------------------------------------------------------------- #
@Controller("frag")
class FragPages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/loc")
    async def loc(self):
        return self.inertia.location("/target", fragment="section")

    @Post("/back")
    async def back(self):
        return self.inertia.back(fallback="/home", fragment="anchor")

    @Get("/redir")
    async def redir(self):
        return self.inertia.redirect("/dest#existing")


def test_helpers_carry_url_fragment():
    @Module(imports=[InertiaModule.for_root(version="1")], controllers=[FragPages])
    class FragApp:
        pass

    with TestClient(FaNestFactory.create(FragApp), raise_server_exceptions=False) as client:
        loc = client.get("/frag/loc", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
        assert loc.status_code == 409
        assert loc.headers["x-inertia-location"] == "/target#section"

        back = client.post("/frag/back", headers={"X-Inertia": "true"}, follow_redirects=False)
        assert back.headers["location"] == "/home#anchor"

        redir = client.get("/frag/redir", follow_redirects=False)
        assert redir.headers["location"] == "/dest#existing"  # existing fragment preserved


def test_preserve_fragment_disabled():
    @Controller("nf")
    class NoFragPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/loc")
        async def loc(self):
            return self.inertia.location("/target", fragment="section")

    @Module(imports=[InertiaModule.for_root(version="1", preserve_fragment=False)], controllers=[NoFragPages])
    class NoFragApp:
        pass

    with TestClient(FaNestFactory.create(NoFragApp), raise_server_exceptions=False) as client:
        loc = client.get("/nf/loc", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
    assert loc.headers["x-inertia-location"] == "/target"  # fragment dropped


# --------------------------------------------------------------------------- #
# #30 ensure_pages_exist
# --------------------------------------------------------------------------- #
def test_ensure_pages_exist_guard(tmp_path):
    pages = tmp_path / "Pages"
    pages.mkdir()
    (pages / "Exists.tsx").write_text("export default () => null")

    @Controller("p")
    class PagePages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/exists")
        async def exists(self):
            return await self.inertia.render("Exists", {"ok": True})

        @Get("/missing")
        async def missing(self):
            return await self.inertia.render("Missing", {"ok": True})

    @Module(
        imports=[
            InertiaModule.for_root(version="1", ensure_pages_exist=True, page_paths=[str(pages)])
        ],
        controllers=[PagePages],
    )
    class PageApp:
        pass

    with TestClient(FaNestFactory.create(PageApp), raise_server_exceptions=False) as client:
        ok = client.get("/p/exists", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
        assert ok.status_code == 200
        # a component with no matching file surfaces a clear render-time error
        broken = client.get("/p/missing", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
    assert broken.status_code == 500


def test_ensure_component_exists_raises_directly():
    from fanest.inertia import InertiaComponentNotFoundError, InertiaConfig
    from fanest.inertia.module import _ensure_component_exists

    config = InertiaConfig(ensure_pages_exist=True, page_paths=["/nonexistent/pages"])
    try:
        _ensure_component_exists(config, "Ghost")
    except InertiaComponentNotFoundError as exc:
        assert "Ghost" in str(exc)
    else:
        raise AssertionError("expected InertiaComponentNotFoundError")


# --------------------------------------------------------------------------- #
# #31 SSR ops: throw_on_error + health
# --------------------------------------------------------------------------- #
def test_ssr_throw_on_error_surfaces_failure():
    import asyncio

    from fanest.inertia import InertiaSSR

    # A down SSR server: default swallows the failure, throw_on_error re-raises.
    down = {"enabled": True, "url": "http://127.0.0.1:9"}
    silent = InertiaSSR(down)
    assert asyncio.run(silent.render({"component": "X"})) is None

    loud = InertiaSSR({**down, "throw_on_error": True})
    try:
        asyncio.run(loud.render({"component": "X"}))
    except Exception:
        pass
    else:
        raise AssertionError("throw_on_error should surface the SSR failure")

    # health check against a down server is False, never raises
    assert asyncio.run(silent.is_healthy()) is False


def test_ssr_throw_on_error_bubbles_to_http_500():
    @Controller("ssr")
    class SsrPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/page")
        async def page(self):
            return await self.inertia.render("S", {"ok": True})

    @Module(
        imports=[
            InertiaModule.for_root(
                version="1", ssr={"enabled": True, "url": "http://127.0.0.1:9", "throw_on_error": True}
            )
        ],
        controllers=[SsrPages],
    )
    class SsrApp:
        pass

    with TestClient(FaNestFactory.create(SsrApp), raise_server_exceptions=False) as client:
        # a first (non-Inertia) visit takes the SSR path; the down server -> 500
        resp = client.get("/ssr/page")
    assert resp.status_code == 500


# --------------------------------------------------------------------------- #
# #32 encrypt-history route middleware
# --------------------------------------------------------------------------- #
def test_encrypt_history_routes_middleware():
    @Controller("enc")
    class EncPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/secret")
        async def secret(self):
            return await self.inertia.render("Secret", {"ok": True})

        @Get("/public")
        async def public(self):
            return await self.inertia.render("Public", {"ok": True})

        @Get("/admin/users")
        async def admin(self):
            return await self.inertia.render("Admin", {"ok": True})

    @Module(
        imports=[
            InertiaModule.for_root(version="1", encrypt_history_routes=["/enc/secret", "/enc/admin*"])
        ],
        controllers=[EncPages],
    )
    class EncApp:
        pass

    with TestClient(FaNestFactory.create(EncApp)) as client:
        headers = {"X-Inertia": "true", "X-Inertia-Version": "1"}
        assert client.get("/enc/secret", headers=headers).json()["encryptHistory"] is True
        assert client.get("/enc/admin/users", headers=headers).json()["encryptHistory"] is True  # prefix match
        assert client.get("/enc/public", headers=headers).json()["encryptHistory"] is False


# --------------------------------------------------------------------------- #
# Beta hardening: debug env-gate for the error filter
# --------------------------------------------------------------------------- #
def test_exception_filter_reraises_in_debug_mode():
    from fanest.common.exceptions import NotFoundException
    from fanest.inertia import InertiaExceptionFilter

    @Controller("dbg")
    class DbgPages:
        def __init__(self, inertia: InertiaService):
            self.inertia = inertia

        @Get("/missing")
        async def missing(self):
            raise NotFoundException()

    @Module(imports=[InertiaModule.for_root(version="1", debug=True)], controllers=[DbgPages])
    class DbgApp:
        pass

    app = FaNestFactory.create(DbgApp, global_filters=[InertiaExceptionFilter])
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/dbg/missing", headers={"X-Inertia": "true", "X-Inertia-Version": "1"})
    # debug -> the filter re-raises so the dev sees the real error, NOT an Inertia page
    assert resp.status_code == 404
    assert "component" not in resp.json()


# --------------------------------------------------------------------------- #
# Protocol conformance: pin the exact page-object shape so a core change can't
# silently break the Inertia contract (key set, always-present base keys,
# empty-key omission, v2 metadata shapes).
# --------------------------------------------------------------------------- #
@Controller("cf")
class ConformancePages:
    def __init__(self, inertia: InertiaService):
        self.inertia = inertia

    @Get("/full")
    async def full(self):
        return await self.inertia.render(
            "Full",
            {
                "plain": 1,
                "m": self.inertia.merge([1]),
                "p": self.inertia.merge([1], prepend=True),
                "d": self.inertia.deep_merge({"x": []}, match_on=["id"]),
                "later": self.inertia.defer(lambda: 1, group="g"),
                "cfg": self.inertia.once(lambda: 2, expires_at=5),
            },
        )

    @Get("/min")
    async def minimal(self):
        return await self.inertia.render("Min", {"x": 1})


@Module(imports=[InertiaModule.for_root(version="cf1")], controllers=[ConformancePages])
class ConformanceApp:
    pass


def _cf_client():
    return TestClient(FaNestFactory.create(ConformanceApp))


_BASE_KEYS = {"component", "props", "url", "version", "clearHistory", "encryptHistory"}


def test_conformance_full_page_object_shape():
    with _cf_client() as client:
        page = client.get("/cf/full", headers={"X-Inertia": "true", "X-Inertia-Version": "cf1"}).json()
    # exact top-level key set — nothing extra may leak in, nothing may drop out
    assert set(page) == _BASE_KEYS | {
        "mergeProps",
        "prependProps",
        "deepMergeProps",
        "matchPropsOn",
        "deferredProps",
        "onceProps",
    }
    # base keys carry their contract types/values
    assert page["component"] == "Full"
    assert page["version"] == "cf1"
    assert page["clearHistory"] is False and page["encryptHistory"] is False
    assert isinstance(page["clearHistory"], bool) and isinstance(page["encryptHistory"], bool)
    # v2 metadata: exact shapes (byte-parity with inertia-laravel)
    assert page["mergeProps"] == ["m"]
    assert page["prependProps"] == ["p"]
    assert page["deepMergeProps"] == ["d"]
    assert page["matchPropsOn"] == ["d.id"]
    assert page["deferredProps"] == {"g": ["later"]}
    assert page["onceProps"] == {"cfg": {"prop": "cfg", "expiresAt": 5}}
    # errors bag is always present, as a dict
    assert page["props"]["errors"] == {}


def test_conformance_minimal_page_omits_all_empty_keys():
    with _cf_client() as client:
        page = client.get("/cf/min", headers={"X-Inertia": "true", "X-Inertia-Version": "cf1"}).json()
    # a plain render emits ONLY the six always-present base keys
    assert set(page) == _BASE_KEYS
    assert set(page["props"]) == {"x", "errors"}
