from fanest import Controller, Get, Injectable, Module


def Counted(name: str):
    def decorator(handler):
        setattr(handler, "__fanest_metric_counter__", name)
        return handler

    return decorator


@Injectable()
class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def get(self, name: str) -> int:
        return self._counters.get(name, 0)

    def render_prometheus(self) -> str:
        return "\n".join(f"{name} {value}" for name, value in sorted(self._counters.items()))


class MetricsInterceptor:
    def __init__(self, registry: MetricsRegistry):
        self.registry = registry

    async def intercept(self, context, call_next):
        result = await call_next()
        counter = getattr(context.handler, "__fanest_metric_counter__", None)
        if counter is not None:
            self.registry.inc(counter)
        return result


@Controller("metrics")
class MetricsController:
    def __init__(self, registry: MetricsRegistry):
        self.registry = registry

    @Get("/")
    async def metrics(self):
        return self.registry.render_prometheus()


class MetricsModule:
    @staticmethod
    def for_root(*, endpoint: bool = True, is_global: bool = False) -> type:
        controllers = [MetricsController] if endpoint else []

        @Module(
            controllers=controllers,
            providers=[MetricsRegistry, MetricsInterceptor],
            exports=[MetricsRegistry, MetricsInterceptor],
            global_module=is_global,
        )
        class DynamicMetricsModule:
            pass

        return DynamicMetricsModule
