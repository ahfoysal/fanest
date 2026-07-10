"""Policy-based (CASL-style) authorization.

Provides an :class:`Ability` value object built with :class:`AbilityBuilder`, a
``@CheckPolicies(...)`` decorator, and :class:`PoliciesGuard` — the NestJS
``@casl/ability`` recipe adapted for FaNest. Policy handlers receive the
current user's ability (and optionally the execution context) and return whether
access is allowed::

    class AbilityFactory:
        def create_for_user(self, user):
            builder = AbilityBuilder()
            if user and user.get("role") == "admin":
                builder.can("manage", "all")
            else:
                builder.can("read", Article)
                builder.can("update", Article, when=lambda a: a.author_id == user["id"])
            return builder.build()

    @Module(providers=[AbilityFactory, use_value(ABILITY_FACTORY, ...)])  # or bind ABILITY_FACTORY
    class AppModule: ...

    @Controller("articles")
    class ArticlesController:
        @Get("/")
        @UseGuards(PoliciesGuard)
        @CheckPolicies(lambda ability: ability.can("read", Article))
        async def list(self): ...
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from fanest.common.exceptions import ForbiddenException
from fanest.core.metadata import InjectMarker
from fanest.core.providers import Inject
from fanest.core.providers import token as _token

#: Injection token for the app-provided ability factory. Bind it to an object
#: exposing ``create_for_user(user)`` (or a callable) that returns an ``Ability``.
ABILITY_FACTORY = _token("ABILITY_FACTORY")

MANAGE = "manage"
ALL = "all"


def _subject_name(subject: Any) -> str:
    if subject is None:
        return ALL
    if isinstance(subject, str):
        return subject
    if isinstance(subject, type):
        return subject.__name__
    return type(subject).__name__


class Ability:
    """An ordered list of CASL-style rules ``(action, subject, allowed, condition)``.

    The last matching rule wins, so define broad ``can`` rules first and narrow
    them with later ``cannot`` rules (matching NestJS/CASL semantics).
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: list[tuple[str, str, bool, Callable[[Any], bool] | None]] | None = None) -> None:
        self._rules = list(rules or [])

    def can(self, action: str, subject: Any = ALL) -> bool:
        name = _subject_name(subject)
        instance = subject if not isinstance(subject, (str, type)) and subject is not None else None
        allowed = False
        for rule_action, rule_subject, permit, condition in self._rules:
            if rule_action not in (MANAGE, action):
                continue
            if rule_subject not in (ALL, name):
                continue
            # A conditional rule is only evaluated against a concrete instance.
            # For a type/class-level check (no instance), the rule still matches
            # — the permission is potentially available, exactly like CASL's
            # ability.can(action, SubjectClass).
            if condition is not None and instance is not None and not condition(instance):
                continue
            allowed = permit
        return allowed

    def cannot(self, action: str, subject: Any = ALL) -> bool:
        return not self.can(action, subject)

    def rules(self) -> list[tuple[str, str, bool, Callable[[Any], bool] | None]]:
        return list(self._rules)


class AbilityBuilder:
    """Fluent builder for an :class:`Ability` (mirrors CASL's ``AbilityBuilder``)."""

    def __init__(self) -> None:
        self._rules: list[tuple[str, str, bool, Callable[[Any], bool] | None]] = []

    def can(self, action: str, subject: Any = ALL, *, when: Callable[[Any], bool] | None = None) -> "AbilityBuilder":
        self._rules.append((action, _subject_name(subject), True, when))
        return self

    def cannot(self, action: str, subject: Any = ALL, *, when: Callable[[Any], bool] | None = None) -> "AbilityBuilder":
        self._rules.append((action, _subject_name(subject), False, when))
        return self

    def build(self) -> Ability:
        return Ability(self._rules)


#: A policy handler is either a callable ``(ability[, context]) -> bool`` or an
#: object exposing ``handle(ability[, context]) -> bool``.
PolicyHandler = Any


def CheckPolicies(*handlers: PolicyHandler) -> Callable[[Any], Any]:
    """Attach policy handlers to a controller or route handler. Evaluated by
    :class:`PoliciesGuard`; access is granted only if every handler returns true."""

    def decorator(target: Any) -> Any:
        existing = list(getattr(target, "__fanest_policies__", ()))
        existing.extend(handlers)
        setattr(target, "__fanest_policies__", existing)
        return target

    return decorator


class PoliciesGuard:
    """Guard that builds the current user's :class:`Ability` (via the injected
    ``ABILITY_FACTORY`` when present) and evaluates every ``@CheckPolicies``
    handler on the controller and route."""

    def __init__(self, ability_factory: Any = Inject(ABILITY_FACTORY, optional=True)):
        self.ability_factory = ability_factory

    async def can_activate(self, context: Any) -> bool:
        controller_cls = getattr(context.controller, "__class__", None)
        handlers = [
            *getattr(controller_cls, "__fanest_policies__", ()),
            *getattr(context.handler, "__fanest_policies__", ()),
        ]
        if not handlers:
            return True
        user = getattr(getattr(context.request, "state", None), "user", None)
        ability = await self._ability_for(user)
        for handler in handlers:
            result = self._invoke(handler, ability, context)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise ForbiddenException("Forbidden by policy")
        return True

    async def _ability_for(self, user: Any) -> Ability:
        factory = self.ability_factory
        if factory is None or isinstance(factory, InjectMarker):
            return Ability()
        create = (
            getattr(factory, "create_for_user", None)
            or getattr(factory, "build", None)
            or (factory if callable(factory) else None)
        )
        if create is None:
            return Ability()
        ability = create(user)
        if inspect.isawaitable(ability):
            ability = await ability
        return ability if isinstance(ability, Ability) else Ability()

    @staticmethod
    def _invoke(handler: PolicyHandler, ability: Ability, context: Any) -> Any:
        fn = handler.handle if hasattr(handler, "handle") else handler
        try:
            parameters = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            parameters = 1
        if parameters >= 2:
            return fn(ability, context)
        return fn(ability)
