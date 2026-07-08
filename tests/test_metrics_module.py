from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module
from fanest.metrics import Counted, MetricsModule


@Controller("work")
class WorkController:
    @Counted("work_requests_total")
    @Get("/")
    async def work(self):
        return {"ok": True}


@Module(imports=[MetricsModule.for_root()], controllers=[WorkController])
class MetricsAppModule:
    pass


def test_metrics_module_counts_decorated_handlers():
    client = TestClient(FaNestFactory.create(MetricsAppModule))

    assert client.get("/work").json() == {"ok": True}
    assert client.get("/work").json() == {"ok": True}
    assert "work_requests_total 2" in client.get("/metrics").text
