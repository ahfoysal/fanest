from collections import defaultdict
from dataclasses import dataclass
from math import inf, isfinite
from typing import Any

from fanest import Controller, Get, Injectable, Module
from fastapi.responses import Response


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    kind: str
    help: str | None = None
    buckets: tuple[float, ...] = ()


def Counted(name: str, *, labels: dict[str, Any] | None = None):
    def decorator(handler):
        setattr(handler, "__fanest_metric_counter__", name)
        setattr(handler, "__fanest_metric_counter_labels__", labels or {})
        return handler

    return decorator


@Injectable()
class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._definitions: dict[str, MetricDefinition] = {}
        self._histogram_buckets: dict[str, tuple[float, ...]] = {}

    def inc(self, name: str, amount: float = 1, labels: dict[str, Any] | None = None) -> None:
        self.counter(name)
        self._validate_number(amount)
        if amount < 0:
            raise ValueError("Prometheus counters cannot be incremented by a negative amount")
        key = self._key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + amount

    def set_gauge(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        self.gauge(name)
        self._validate_number(value)
        self._gauges[self._key(name, labels)] = value

    def inc_gauge(self, name: str, amount: float = 1, labels: dict[str, Any] | None = None) -> None:
        self.gauge(name)
        self._validate_number(amount)
        key = self._key(name, labels)
        self._gauges[key] = self._gauges.get(key, 0) + amount

    def dec_gauge(self, name: str, amount: float = 1, labels: dict[str, Any] | None = None) -> None:
        self.inc_gauge(name, -amount, labels=labels)

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        self.histogram(name)
        self._validate_number(value)
        self._histograms[self._key(name, labels)].append(value)

    def get(self, name: str, labels: dict[str, Any] | None = None) -> float:
        return self._counters.get(self._key(name, labels), 0)

    def counter(self, name: str, *, help: str | None = None) -> None:
        self._define(name, "counter", help=help)

    def gauge(self, name: str, *, help: str | None = None) -> None:
        self._define(name, "gauge", help=help)

    def histogram(
        self,
        name: str,
        *,
        help: str | None = None,
        buckets: list[float] | tuple[float, ...] | None = None,
    ) -> None:
        normalized = self._normalize_buckets(buckets)
        self._define(name, "histogram", help=help, buckets=normalized)
        self._histogram_buckets[name] = normalized

    def clear(self) -> None:
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for definition in sorted(self._definitions.values(), key=lambda item: item.name):
            if definition.help:
                lines.append(f"# HELP {definition.name} {self._escape_help(definition.help)}")
            lines.append(f"# TYPE {definition.name} {definition.kind}")
        for (name, labels), value in sorted(self._counters.items()):
            lines.append(f"{name}{self._labels(labels)} {self._format(value)}")
        for (name, labels), value in sorted(self._gauges.items()):
            lines.append(f"{name}{self._labels(labels)} {self._format(value)}")
        for (name, labels), values in sorted(self._histograms.items()):
            count = len(values)
            total = sum(values)
            for bucket in self._histogram_buckets.get(name, ()):
                bucket_labels = dict(labels)
                bucket_labels["le"] = "+Inf" if bucket == inf else self._format(bucket)
                bucket_count = sum(1 for value in values if value <= bucket)
                lines.append(f"{name}_bucket{self._labels_from_dict(bucket_labels)} {bucket_count}")
            infinite_labels = dict(labels)
            infinite_labels["le"] = "+Inf"
            if not any(bucket == inf for bucket in self._histogram_buckets.get(name, ())):
                lines.append(f"{name}_bucket{self._labels_from_dict(infinite_labels)} {count}")
            lines.append(f"{name}_count{self._labels(labels)} {count}")
            lines.append(f"{name}_sum{self._labels(labels)} {self._format(total)}")
            for quantile, value in self._quantiles(values).items():
                quantile_labels = dict(labels)
                quantile_labels["quantile"] = quantile
                lines.append(f"{name}{self._labels_from_dict(quantile_labels)} {self._format(value)}")
        return "\n".join(lines) + ("\n" if lines else "")

    def _key(self, name: str, labels: dict[str, Any] | None = None) -> tuple[str, tuple[tuple[str, str], ...]]:
        self._validate_metric_name(name)
        normalized_labels = []
        for key, value in (labels or {}).items():
            self._validate_label_name(key)
            normalized_labels.append((key, str(value)))
        return name, tuple(sorted(normalized_labels))

    def _labels(self, labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        rendered = ",".join(f'{key}="{self._escape(value)}"' for key, value in labels)
        return f"{{{rendered}}}"

    def _labels_from_dict(self, labels: dict[str, Any]) -> str:
        return self._labels(tuple(sorted((key, str(value)) for key, value in labels.items())))

    def _escape(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    def _escape_help(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n")

    def _format(self, value: float) -> str:
        if value == inf:
            return "+Inf"
        if int(value) == value:
            return str(int(value))
        return str(value)

    def _quantiles(self, values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        ordered = sorted(values)
        return {
            "0.5": ordered[min(int(len(ordered) * 0.5), len(ordered) - 1)],
            "0.9": ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)],
            "0.99": ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)],
        }

    def _define(
        self,
        name: str,
        kind: str,
        *,
        help: str | None = None,
        buckets: tuple[float, ...] = (),
    ) -> None:
        self._validate_metric_name(name)
        existing = self._definitions.get(name)
        definition = MetricDefinition(name=name, kind=kind, help=help, buckets=buckets)
        if existing is not None and existing.kind != kind:
            raise ValueError(f"Metric {name!r} is already registered as {existing.kind}")
        if existing is None or help or buckets:
            self._definitions[name] = definition

    def _normalize_buckets(self, buckets: list[float] | tuple[float, ...] | None) -> tuple[float, ...]:
        values = tuple(float(value) for value in (buckets or (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)))
        for value in values:
            self._validate_number(value, allow_inf=True)
        return tuple(sorted(set(values)))

    def _validate_number(self, value: float, *, allow_inf: bool = False) -> None:
        if allow_inf and value == inf:
            return
        if not isfinite(value):
            raise ValueError("Metric values must be finite numbers")

    def _validate_metric_name(self, name: str) -> None:
        if not name or not (name[0].isalpha() or name[0] in "_:") or any(
            not (char.isalnum() or char in "_:") for char in name
        ):
            raise ValueError(f"Invalid Prometheus metric name: {name!r}")

    def _validate_label_name(self, name: str) -> None:
        if not name or not (name[0].isalpha() or name[0] == "_") or any(
            not (char.isalnum() or char == "_") for char in name
        ):
            raise ValueError(f"Invalid Prometheus label name: {name!r}")


class MetricsInterceptor:
    def __init__(self, registry: MetricsRegistry):
        self.registry = registry

    async def intercept(self, context, call_next):
        result = await call_next()
        counter = getattr(context.handler, "__fanest_metric_counter__", None)
        if counter is not None:
            self.registry.inc(
                counter,
                labels=getattr(context.handler, "__fanest_metric_counter_labels__", {}),
            )
        return result


@Controller("metrics")
class MetricsController:
    def __init__(self, registry: MetricsRegistry):
        self.registry = registry

    @Get("/")
    async def metrics(self):
        return Response(
            self.registry.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )


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
