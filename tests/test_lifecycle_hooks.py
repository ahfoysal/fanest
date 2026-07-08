from fastapi.testclient import TestClient

from fanest import FaNestFactory, Injectable, Module


events: list[str] = []


@Injectable()
class FirstLifecycleService:
    async def on_module_init(self):
        events.append("first:init")

    async def on_application_bootstrap(self):
        events.append("first:bootstrap")

    async def before_application_shutdown(self):
        events.append("first:before_shutdown")

    async def on_module_destroy(self):
        events.append("first:destroy")

    async def on_application_shutdown(self):
        events.append("first:shutdown")


@Injectable()
class SecondLifecycleService:
    async def on_module_init(self):
        events.append("second:init")

    async def on_application_bootstrap(self):
        events.append("second:bootstrap")

    async def before_application_shutdown(self):
        events.append("second:before_shutdown")

    async def on_module_destroy(self):
        events.append("second:destroy")

    async def on_application_shutdown(self):
        events.append("second:shutdown")


@Module(providers=[FirstLifecycleService, SecondLifecycleService])
class LifecycleModule:
    pass


def test_lifecycle_hooks_run_in_nest_style_order():
    events.clear()

    with TestClient(FaNestFactory.create(LifecycleModule)):
        assert events == [
            "first:init",
            "second:init",
            "first:bootstrap",
            "second:bootstrap",
        ]

    assert events == [
        "first:init",
        "second:init",
        "first:bootstrap",
        "second:bootstrap",
        "second:before_shutdown",
        "first:before_shutdown",
        "second:destroy",
        "first:destroy",
        "second:shutdown",
        "first:shutdown",
    ]
