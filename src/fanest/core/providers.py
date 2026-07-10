from typing import Any

from fanest.core.metadata import (
    INQUIRER,
    REQUEST,
    ClassProvider,
    ExistingProvider,
    ForwardRef,
    FactoryProvider,
    InjectMarker,
    InjectionToken,
    ValueProvider,
)

__all__ = [
    "INQUIRER",
    "REQUEST",
    "Inject",
    "Optional",
    "Self",
    "SkipSelf",
    "forward_ref",
    "token",
    "use_class",
    "use_existing",
    "use_factory",
    "use_value",
]


def token(name: str) -> InjectionToken:
    return InjectionToken(name)


def Inject(
    token: Any,
    *,
    optional: bool = False,
    default: Any = None,
    self_only: bool = False,
    skip_self: bool = False,
) -> Any:
    return InjectMarker(
        token=token,
        optional=optional,
        default=default,
        self_only=self_only,
        skip_self=skip_self,
    )


def Optional(token: Any, default: Any = None) -> Any:
    return Inject(token, optional=True, default=default)


def Self(token: Any, *, optional: bool = False, default: Any = None) -> Any:
    return Inject(token, optional=optional, default=default, self_only=True)


def SkipSelf(token: Any, *, optional: bool = False, default: Any = None) -> Any:
    return Inject(token, optional=optional, default=default, skip_self=True)


def forward_ref(factory: Any) -> ForwardRef:
    return ForwardRef(factory=factory)


def use_class(provide: Any, use_class: type, scope: str | None = None) -> ClassProvider:
    return ClassProvider(provide=provide, use_class=use_class, scope=scope)


def use_value(provide: Any, use_value: Any) -> ValueProvider:
    return ValueProvider(provide=provide, use_value=use_value)


def use_factory(
    provide: Any,
    use_factory: Any,
    inject: list[Any] | None = None,
    scope: str = "singleton",
) -> FactoryProvider:
    return FactoryProvider(provide=provide, use_factory=use_factory, inject=inject or [], scope=scope)


def use_existing(provide: Any, use_existing: Any) -> ExistingProvider:
    return ExistingProvider(provide=provide, use_existing=use_existing)
