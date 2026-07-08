from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, TypeVar

from fanest.core.metadata import DynamicModule, ModuleMetadata, ProviderDefinition

T = TypeVar("T")


def Module(
    *,
    imports: Sequence[Any] | None = None,
    controllers: Sequence[type] | None = None,
    providers: Sequence[ProviderDefinition] | None = None,
    gateways: Sequence[type] | None = None,
    middlewares: Sequence[type] | None = None,
    exports: Sequence[Any] | None = None,
    global_module: bool = False,
) -> Callable[[type[T]], type[T]]:
    def decorator(cls: type[T]) -> type[T]:
        setattr(
            cls,
            "__fanest_module__",
            ModuleMetadata(
                imports=list(imports or []),
                controllers=list(controllers or []),
                providers=list(providers or []),
                gateways=list(gateways or []),
                middlewares=list(middlewares or []),
                exports=list(exports or []),
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


def dynamic_module(
    module: type,
    *,
    imports: Sequence[Any] | None = None,
    controllers: Sequence[type] | None = None,
    providers: Sequence[ProviderDefinition] | None = None,
    gateways: Sequence[type] | None = None,
    middlewares: Sequence[type] | None = None,
    exports: Sequence[Any] | None = None,
    global_module: bool = False,
    global_: bool | None = None,
) -> DynamicModule:
    return DynamicModule(
        module=module,
        imports=list(imports or []),
        controllers=list(controllers or []),
        providers=list(providers or []),
        gateways=list(gateways or []),
        middlewares=list(middlewares or []),
        exports=list(exports or []),
        global_module=global_module if global_ is None else global_,
    )
