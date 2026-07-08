from collections.abc import Callable
from typing import TypeVar

from fanest.core.metadata import ModuleMetadata, ProviderDefinition

T = TypeVar("T")


def Module(
    *,
    imports: list[type] | None = None,
    controllers: list[type] | None = None,
    providers: list[ProviderDefinition] | None = None,
    gateways: list[type] | None = None,
    middlewares: list[type] | None = None,
    exports: list[type] | None = None,
) -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(
            cls,
            "__fanest_module__",
            ModuleMetadata(
                imports=imports or [],
                controllers=controllers or [],
                providers=providers or [],
                gateways=gateways or [],
                middlewares=middlewares or [],
                exports=exports or [],
            ),
        )
        return cls

    return decorator
