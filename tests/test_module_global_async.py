import asyncio

from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Global, Inject, Module, UseGuards, dynamic_module, token, use_factory
from fanest.auth import AuthModule, JwtAuthGuard, JwtService
from fanest.config import ConfigModule, ConfigService
from fanest.throttler import Throttle, ThrottlerGuard, ThrottlerModule

ASYNC_MESSAGE = token("ASYNC_MESSAGE")
GLOBAL_MESSAGE = token("GLOBAL_MESSAGE")
DYNAMIC_MESSAGE = token("DYNAMIC_MESSAGE")
DICT_DYNAMIC_MESSAGE = token("DICT_DYNAMIC_MESSAGE")
SHARED_DYNAMIC_CONNECTION = token("SHARED_DYNAMIC_CONNECTION")


async def async_message_factory():
    return "ready"


@Controller("async-provider")
class AsyncProviderController:
    def __init__(self, message: str = Inject(ASYNC_MESSAGE)):
        self.message = message

    @Get("/")
    async def index(self):
        return {"message": self.message}


@Module(
    controllers=[AsyncProviderController],
    providers=[use_factory(ASYNC_MESSAGE, async_message_factory)],
)
class AsyncProviderModule:
    pass


def test_async_factory_provider_is_awaited_during_lifespan():
    with TestClient(FaNestFactory.create(AsyncProviderModule)) as client:
        assert client.get("/async-provider").json() == {"message": "ready"}


@Global
@Module(providers=[use_factory(GLOBAL_MESSAGE, lambda: "global")], exports=[GLOBAL_MESSAGE])
class SharedGlobalModule:
    pass


class UsesGlobalService:
    def __init__(self, message: str = Inject(GLOBAL_MESSAGE)):
        self.message = message


@Controller("global")
class GlobalConsumerController:
    def __init__(self, service: UsesGlobalService):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(providers=[UsesGlobalService], controllers=[GlobalConsumerController])
class FeatureWithoutDirectImportModule:
    pass


@Module(imports=[SharedGlobalModule, FeatureWithoutDirectImportModule])
class GlobalRootModule:
    pass


def test_global_module_exports_are_visible_to_other_modules_once_imported():
    client = TestClient(FaNestFactory.create(GlobalRootModule))

    assert client.get("/global").json() == {"message": "global"}


async def async_config_factory():
    return {"APP_NAME": "FaNest"}


@Controller("config-async")
class AsyncConfigController:
    def __init__(self, config: ConfigService):
        self.config = config

    @Get("/")
    async def index(self):
        return {"name": self.config.get("APP_NAME")}


@Module(
    imports=[ConfigModule.for_root_async(use_factory=async_config_factory, env_file=None)],
    controllers=[AsyncConfigController],
)
class AsyncConfigModule:
    pass


def test_config_module_for_root_async():
    with TestClient(FaNestFactory.create(AsyncConfigModule)) as client:
        assert client.get("/config-async").json() == {"name": "FaNest"}


async def async_jwt_options():
    return {"secret": "async-secret-value-with-enough-entropy"}


@Controller("auth-async")
class AsyncAuthController:
    @UseGuards(JwtAuthGuard)
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(imports=[AuthModule.for_root_async(use_factory=async_jwt_options)], controllers=[AsyncAuthController])
class AsyncAuthModule:
    pass


def test_auth_module_for_root_async():
    app = FaNestFactory.create(AsyncAuthModule)
    with TestClient(app) as client:
        token_value = app.state.fanest_container.resolve(JwtService).sign({"sub": "1"})
        response = client.get("/auth-async", headers={"authorization": f"Bearer {token_value}"})

    assert response.status_code == 200


async def async_throttler_options():
    return {"limit": 1, "ttl": 60}


@Controller("throttler-async")
@UseGuards(ThrottlerGuard)
class AsyncThrottlerController:
    @Throttle()
    @Get("/")
    async def index(self):
        return {"ok": True}


@Module(
    imports=[ThrottlerModule.for_root_async(use_factory=async_throttler_options)],
    controllers=[AsyncThrottlerController],
)
class AsyncThrottlerModule:
    pass


def test_throttler_module_for_root_async():
    with TestClient(FaNestFactory.create(AsyncThrottlerModule)) as client:
        assert client.get("/throttler-async").status_code == 200
        assert client.get("/throttler-async").status_code == 429


@Module()
class DynamicFeatureModule:
    @staticmethod
    def for_root(message: str):
        return dynamic_module(
            DynamicFeatureModule,
            providers=[use_factory(DYNAMIC_MESSAGE, lambda: message)],
            exports=[DYNAMIC_MESSAGE],
        )


class UsesDynamicMessage:
    def __init__(self, message: str = Inject(DYNAMIC_MESSAGE)):
        self.message = message


@Controller("dynamic-module")
class DynamicModuleController:
    def __init__(self, service: UsesDynamicMessage):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(
    imports=[DynamicFeatureModule.for_root("dynamic-ready")],
    controllers=[DynamicModuleController],
    providers=[UsesDynamicMessage],
)
class DynamicModuleRoot:
    pass


def test_dynamic_module_helper_merges_runtime_metadata():
    client = TestClient(FaNestFactory.create(DynamicModuleRoot))

    assert client.get("/dynamic-module").json() == {"message": "dynamic-ready"}


class SharedDynamicConnection:
    created = 0

    def __init__(self):
        type(self).created += 1
        self.id = type(self).created


@Module()
class SharedDynamicDatabaseModule:
    @staticmethod
    def for_root():
        return dynamic_module(
            SharedDynamicDatabaseModule,
            providers=[use_factory(SHARED_DYNAMIC_CONNECTION, SharedDynamicConnection)],
            exports=[SHARED_DYNAMIC_CONNECTION],
        )


class UsesSharedDynamicConnection:
    def __init__(self, connection: SharedDynamicConnection = Inject(SHARED_DYNAMIC_CONNECTION)):
        self.connection = connection


@Controller("dynamic-a")
class DynamicAController:
    def __init__(self, service: UsesSharedDynamicConnection):
        self.service = service

    @Get("/")
    async def index(self):
        return {"id": self.service.connection.id}


@Controller("dynamic-b")
class DynamicBController:
    def __init__(self, service: UsesSharedDynamicConnection):
        self.service = service

    @Get("/")
    async def index(self):
        return {"id": self.service.connection.id}


@Module(
    imports=[SharedDynamicDatabaseModule.for_root()],
    controllers=[DynamicAController],
    providers=[UsesSharedDynamicConnection],
)
class DynamicFeatureAModule:
    pass


@Module(
    imports=[SharedDynamicDatabaseModule.for_root()],
    controllers=[DynamicBController],
    providers=[UsesSharedDynamicConnection],
)
class DynamicFeatureBModule:
    pass


@Module(imports=[DynamicFeatureAModule, DynamicFeatureBModule])
class DuplicateDynamicRootModule:
    pass


def test_identical_dynamic_module_imports_share_singleton_providers():
    SharedDynamicConnection.created = 0
    client = TestClient(FaNestFactory.create(DuplicateDynamicRootModule))

    assert client.get("/dynamic-a").json() == {"id": 1}
    assert client.get("/dynamic-b").json() == {"id": 1}
    assert SharedDynamicConnection.created == 1


@Module()
class DictDynamicFeatureModule:
    pass


class UsesGlobalDynamicMessage:
    def __init__(self, message: str = Inject(DICT_DYNAMIC_MESSAGE)):
        self.message = message


@Controller("dict-dynamic-module")
class DictDynamicModuleController:
    def __init__(self, service: UsesGlobalDynamicMessage):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(controllers=[DictDynamicModuleController], providers=[UsesGlobalDynamicMessage])
class DictDynamicConsumerModule:
    pass


@Module(
    imports=[
        {
            "module": DictDynamicFeatureModule,
            "providers": [use_factory(DICT_DYNAMIC_MESSAGE, lambda: "dict-dynamic-ready")],
            "exports": [DICT_DYNAMIC_MESSAGE],
            "global": True,
        },
        DictDynamicConsumerModule,
    ]
)
class DictDynamicModuleRoot:
    pass


def test_nest_style_dynamic_module_dict_can_be_global():
    client = TestClient(FaNestFactory.create(DictDynamicModuleRoot))

    assert client.get("/dict-dynamic-module").json() == {"message": "dict-dynamic-ready"}


ASYNC_DYNAMIC_MESSAGE = token("ASYNC_DYNAMIC_MESSAGE")
ASYNC_REQUEST_MESSAGE = token("ASYNC_REQUEST_MESSAGE")


@Module()
class AsyncDynamicFeatureModule:
    pass


async def async_dynamic_import():
    await asyncio.sleep(0)
    return {
        "module": AsyncDynamicFeatureModule,
        "providers": [use_factory(ASYNC_DYNAMIC_MESSAGE, lambda: "async-dynamic-ready")],
        "exports": [ASYNC_DYNAMIC_MESSAGE],
    }


class UsesAsyncDynamicMessage:
    def __init__(self, message: str = Inject(ASYNC_DYNAMIC_MESSAGE)):
        self.message = message


@Controller("async-dynamic-module")
class AsyncDynamicController:
    def __init__(self, service: UsesAsyncDynamicMessage):
        self.service = service

    @Get("/")
    async def index(self):
        return {"message": self.service.message}


@Module(
    imports=[async_dynamic_import()],
    controllers=[AsyncDynamicController],
    providers=[UsesAsyncDynamicMessage],
)
class AsyncDynamicRoot:
    pass


def test_create_async_supports_awaitable_dynamic_module_imports():
    app = asyncio.run(FaNestFactory.create_async(AsyncDynamicRoot))

    assert TestClient(app).get("/async-dynamic-module").json() == {
        "message": "async-dynamic-ready"
    }


async def async_request_message_factory():
    await asyncio.sleep(0)
    return {"message": "async-request-ready"}


@Controller("async-request-factory")
class AsyncRequestFactoryController:
    def __init__(self, value: dict = Inject(ASYNC_REQUEST_MESSAGE)):
        self.value = value

    @Get("/")
    async def index(self):
        return self.value


@Module(
    controllers=[AsyncRequestFactoryController],
    providers=[use_factory(ASYNC_REQUEST_MESSAGE, async_request_message_factory)],
)
class AsyncRequestFactoryModule:
    pass


def test_http_request_resolution_awaits_async_factory_dependencies():
    client = TestClient(FaNestFactory.create(AsyncRequestFactoryModule))

    assert client.get("/async-request-factory").json() == {
        "message": "async-request-ready"
    }
