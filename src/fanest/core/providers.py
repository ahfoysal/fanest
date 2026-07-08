from typing import Any

from fanest.core.metadata import (
    ClassProvider,
    ExistingProvider,
    ForwardRef,
    FactoryProvider,
    InjectMarker,
    InjectionToken,
    ValueProvider,
)


def token(name: str) -> InjectionToken:
    return InjectionToken(name)


def Inject(token: Any, *, optional: bool = False, default: Any = None) -> InjectMarker:
    return InjectMarker(token=token, optional=optional, default=default)


def Optional(token: Any, default: Any = None) -> InjectMarker:
    return Inject(token, optional=True, default=default)


def forward_ref(factory: Any) -> ForwardRef:
    return ForwardRef(factory=factory)


def use_class(provide: Any, use_class: type) -> ClassProvider:
    return ClassProvider(provide=provide, use_class=use_class)


def use_value(provide: Any, use_value: Any) -> ValueProvider:
    return ValueProvider(provide=provide, use_value=use_value)


def use_factory(provide: Any, use_factory: Any, inject: list[Any] | None = None) -> FactoryProvider:
    return FactoryProvider(provide=provide, use_factory=use_factory, inject=inject or [])


def use_existing(provide: Any, use_existing: Any) -> ExistingProvider:
    return ExistingProvider(provide=provide, use_existing=use_existing)
