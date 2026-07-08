from fastapi.testclient import TestClient

from fanest import (
    APP_FILTER,
    APP_GUARD,
    APP_INTERCEPTOR,
    APP_PIPE,
    Catch,
    Controller,
    FaNestFactory,
    Get,
    Module,
    Query,
    use_class,
)


class HeaderGuard:
    def can_activate(self, context):
        return context.request.headers.get("x-auth") == "ok"


class UpperPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name":
            return str(value).upper()
        return value


class EnvelopeInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        result["intercepted"] = True
        return result


@Catch(ValueError)
class ValueErrorFilter:
    def catch(self, exc, context):
        return {"filtered": str(exc)}


@Controller("enhancers")
class EnhancerController:
    @Get("/")
    async def index(self, name: str = Query()):
        return {"name": name}

    @Get("/fail")
    async def fail(self):
        raise ValueError("handled globally")


@Module(
    controllers=[EnhancerController],
    providers=[
        use_class(APP_GUARD, HeaderGuard),
        use_class(APP_PIPE, UpperPipe),
        use_class(APP_INTERCEPTOR, EnvelopeInterceptor),
        use_class(APP_FILTER, ValueErrorFilter),
    ],
)
class EnhancerModule:
    pass


def test_app_enhancer_provider_tokens_apply_globally():
    client = TestClient(FaNestFactory.create(EnhancerModule))

    blocked = client.get("/enhancers", params={"name": "ada"})
    allowed = client.get("/enhancers", params={"name": "ada"}, headers={"x-auth": "ok"})
    filtered = client.get("/enhancers/fail", headers={"x-auth": "ok"})

    assert blocked.status_code == 403
    assert allowed.json() == {"name": "ADA", "intercepted": True}
    assert filtered.json() == {"filtered": "handled globally"}
