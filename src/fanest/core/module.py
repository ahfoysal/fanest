from collections.abc import Callable
from dataclasses import replace
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
    global_module: bool = False,
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
                global_module=global_module,
            ),
        )
        return cls

    return decorator


def Global(cls: type[T]) -> type[T]:
    metadata = getattr(cls, "__fanest_module__", None)
    if metadata is None:
        raise TypeError(f"{cls.__name__} is not a FaNest module. Add @Module(...) before @Global.")
    setattr(cls, "__fanest_module__", replace(metadata, global_module=True))
    return cls
