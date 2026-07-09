from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module
from fanest.core.discovery import DiscoveredProvider
from fanest.metrics import Counted, DiscoveryGraphExporter, MetricsModule, MetricsRegistry


@Controller("work")
class WorkController:
    @Counted("work_requests_total")
    @Get("/")
    async def work(self):
        return {"ok": True}

    @Counted("labeled_requests_total", labels={"route": "labelled"})
    @Get("/labelled")
    async def labelled(self):
        return {"ok": True}


@Module(imports=[MetricsModule.for_root()], controllers=[WorkController])
class MetricsAppModule:
    pass


def test_metrics_module_counts_decorated_handlers():
    client = TestClient(FaNestFactory.create(MetricsAppModule))

    assert client.get("/work").json() == {"ok": True}
    assert client.get("/work").json() == {"ok": True}
    assert client.get("/work/labelled").json() == {"ok": True}
    metrics = client.get("/metrics").text

    assert "work_requests_total 2" in metrics
    assert 'labeled_requests_total{route="labelled"} 1' in metrics


def test_metrics_registry_supports_labels_gauges_observations_and_escaping():
    registry = MetricsRegistry()

    registry.counter("jobs_total", help="jobs\\processed\nby queue")
    registry.inc("jobs_total", labels={"queue": 'email"primary\\line\nbreak'})
    registry.set_gauge("workers_active", 2, labels={"pool": "default"})
    registry.observe("job_duration_seconds", 0.2, labels={"queue": "email"})
    registry.observe("job_duration_seconds", 0.4, labels={"queue": "email"})

    rendered = registry.render_prometheus()

    assert "# HELP jobs_total jobs\\\\processed\\nby queue" in rendered
    assert 'jobs_total{queue="email\\"primary\\\\line\\nbreak"} 1' in rendered
    assert 'workers_active{pool="default"} 2' in rendered
    assert 'job_duration_seconds_count{queue="email"} 2' in rendered
    assert 'job_duration_seconds_sum{queue="email"} 0.6000000000000001' in rendered
    assert 'job_duration_seconds{quantile="0.5",queue="email"} 0.4' in rendered


def test_metrics_registry_rejects_invalid_names_labels_and_values():
    registry = MetricsRegistry()

    for action in [
        lambda: registry.inc("bad-name"),
        lambda: registry.inc("jobs_total", labels={"bad-label": "x"}),
        lambda: registry.inc("jobs_total", amount=float("inf")),
        lambda: registry.set_gauge("workers_active", float("nan")),
    ]:
        try:
            action()
        except ValueError:
            pass
        else:
            raise AssertionError("invalid metric input should fail")


def test_discovery_graph_exporter_snapshots_providers_controllers_and_module_edges():
    class OrdersModule:
        pass

    class OrdersService:
        pass

    class OrdersController:
        pass

    class FakeDiscovery:
        def get_providers(self):
            return [
                DiscoveredProvider(
                    token=OrdersService,
                    instance=OrdersService(),
                    module_type=OrdersModule,
                    metatype=OrdersService,
                )
            ]

        def get_controllers(self):
            return [OrdersController]

    graph = DiscoveryGraphExporter(FakeDiscovery()).snapshot().to_dict()

    assert graph == {
        "nodes": [
            {"id": "module:OrdersModule", "label": "OrdersModule", "kind": "module", "module": None},
            {
                "id": "provider:OrdersModule:OrdersService",
                "label": "OrdersService",
                "kind": "provider",
                "module": "OrdersModule",
            },
            {
                "id": "controller:OrdersController",
                "label": "OrdersController",
                "kind": "controller",
                "module": None,
            },
        ],
        "edges": [
            {
                "source": "module:OrdersModule",
                "target": "provider:OrdersModule:OrdersService",
                "kind": "provides",
            }
        ],
    }
