from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, RouterModule


@Controller("dashboard")
class AdminDashboardController:
    @Get("/")
    async def index(self):
        return {"page": "admin-dashboard"}


@Controller()
class MetricsController:
    @Get("summary")
    async def summary(self):
        return {"page": "metrics-summary"}


@Controller("cats")
class CatsController:
    @Get("/")
    async def index(self):
        return {"page": "cats"}


@Module(controllers=[MetricsController])
class MetricsModule:
    pass


@Module(controllers=[AdminDashboardController], imports=[MetricsModule])
class AdminModule:
    pass


@Module(controllers=[CatsController])
class CatsModule:
    pass


@Module(
    imports=[
        AdminModule,
        CatsModule,
        RouterModule.register(
            [
                {
                    "path": "admin",
                    "module": AdminModule,
                    "children": [{"path": "metrics", "module": MetricsModule}],
                },
                {"path": "pets", "module": CatsModule},
            ]
        ),
    ]
)
class AppModule:
    pass


def test_router_module_prefixes_nest_under_parents():
    client = TestClient(FaNestFactory.create(AppModule))

    assert client.get("/admin/dashboard").json() == {"page": "admin-dashboard"}
    assert client.get("/admin/metrics/summary").json() == {"page": "metrics-summary"}
    assert client.get("/pets/cats").json() == {"page": "cats"}
    # Unprefixed paths no longer exist.
    assert client.get("/dashboard").status_code == 404
    assert client.get("/summary").status_code == 404


def test_router_module_composes_with_global_prefix():
    client = TestClient(FaNestFactory.create(AppModule, global_prefix="api"))

    assert client.get("/api/admin/dashboard").json() == {"page": "admin-dashboard"}
    assert client.get("/api/admin/metrics/summary").json() == {"page": "metrics-summary"}


@Module(imports=[CatsModule, RouterModule.register([{"path": "v-cats", "module": CatsModule}])])
class OtherAppModule:
    pass


def test_router_registrations_are_scoped_per_application():
    prefixed = TestClient(FaNestFactory.create(OtherAppModule))
    plain = TestClient(FaNestFactory.create(CatsModule))

    assert prefixed.get("/v-cats/cats").json() == {"page": "cats"}
    # A different app using the same module is not affected by the other
    # app's RouterModule registration.
    assert plain.get("/cats").json() == {"page": "cats"}
    assert plain.get("/v-cats/cats").status_code == 404
