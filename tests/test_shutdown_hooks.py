import asyncio
import os
import signal

from fastapi.testclient import TestClient

from fanest import FaNestApplication, Injectable, Module


@Injectable()
class ShutdownRecorder:
    events: list = []

    def before_application_shutdown(self, signal_name):
        type(self).events.append(("before", signal_name))

    async def on_application_shutdown(self, signal_name):
        type(self).events.append(("shutdown", signal_name))

    def on_module_destroy(self):
        type(self).events.append(("destroy", None))


@Module(providers=[ShutdownRecorder])
class ShutdownModule:
    pass


def test_shutdown_hooks_receive_none_without_a_signal():
    ShutdownRecorder.events = []
    app = FaNestApplication(ShutdownModule)
    app.enable_shutdown_hooks()

    with TestClient(app.build()):
        pass

    assert ("before", None) in ShutdownRecorder.events
    assert ("shutdown", None) in ShutdownRecorder.events
    assert ("destroy", None) in ShutdownRecorder.events


def test_shutdown_hooks_chain_previous_handler_and_pass_signal_name():
    ShutdownRecorder.events = []
    chained: list = []

    def previous_handler(signum, frame):
        chained.append(signum)

    original = signal.signal(signal.SIGUSR1, previous_handler)
    try:
        app = FaNestApplication(ShutdownModule)
        app.enable_shutdown_hooks(["SIGUSR1"])
        asgi_app = app.build()

        async def run():
            async with asgi_app.router.lifespan_context(asgi_app):
                assert signal.getsignal(signal.SIGUSR1) is not previous_handler
                os.kill(os.getpid(), signal.SIGUSR1)
                await asyncio.sleep(0.05)

        asyncio.run(run())

        # The pre-existing handler still ran (chained, not replaced).
        assert chained == [signal.SIGUSR1]
        # Shutdown hooks observed the triggering signal.
        assert ("before", "SIGUSR1") in ShutdownRecorder.events
        assert ("shutdown", "SIGUSR1") in ShutdownRecorder.events
        # The original handler was restored after shutdown.
        assert signal.getsignal(signal.SIGUSR1) is previous_handler
    finally:
        signal.signal(signal.SIGUSR1, original)


@Injectable()
class LegacyRecorder:
    events: list = []

    def on_application_shutdown(self):
        type(self).events.append("shutdown")


@Module(providers=[LegacyRecorder])
class LegacyModule:
    pass


def test_zero_argument_shutdown_hooks_still_supported():
    LegacyRecorder.events = []
    app = FaNestApplication(LegacyModule)
    app.enable_shutdown_hooks()

    with TestClient(app.build()):
        pass

    assert LegacyRecorder.events == ["shutdown"]
