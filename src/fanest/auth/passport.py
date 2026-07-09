import inspect
from typing import Any

from fanest import Inject, Injectable, Module, UnauthorizedException, use_existing, use_value
from fanest.auth.jwt import is_public
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

PASSPORT_OPTIONS = token("PASSPORT_OPTIONS")
PASSPORT_ASYNC_OPTIONS = token("PASSPORT_ASYNC_OPTIONS")


class PassportStrategy:
    name = "default"

    def authenticate(self, context) -> Any:
        raise NotImplementedError


@Injectable()
class PassportService:
    def __init__(self, options: dict[str, Any] | None = Inject(PASSPORT_OPTIONS, optional=True, default=None)) -> None:
        self._strategies: dict[str, PassportStrategy] = {}
        self.default_strategy = str((options or {}).get("default_strategy") or "default").strip() or "default"

    def register(self, strategy: PassportStrategy) -> None:
        name = str(getattr(strategy, "name", "") or "").strip()
        if not name:
            raise ValueError("Passport strategies must define a non-empty name")
        if name in self._strategies and self._strategies[name] is not strategy:
            raise ValueError(f"Passport strategy already registered: {name}")
        self._strategies[name] = strategy

    async def authenticate(self, name: str, context) -> Any:
        strategy_name = str(name or "").strip() or self.default_strategy
        if strategy_name not in self._strategies:
            raise UnauthorizedException(f"Unknown passport strategy: {strategy_name}")
        strategy = self._strategies[strategy_name]
        result = strategy.authenticate(context)
        if inspect.isawaitable(result):
            result = await result
        return result


@Injectable()
class PassportAsyncInitializer:
    def __init__(
        self,
        passport: PassportService,
        options: dict[str, Any] | None = Inject(PASSPORT_ASYNC_OPTIONS, optional=True, default=None),
    ) -> None:
        self.passport = passport
        self.options = options or {}

    async def on_module_init(self) -> None:
        for strategy in self.options.get("strategies", []):
            self.passport.register(_coerce_strategy(strategy))


def AuthGuard(strategy: str | None = None):
    class StrategyGuard:
        def __init__(self, passport: PassportService):
            self.passport = passport

        async def can_activate(self, context):
            if is_public(context.handler, context.controller.__class__):
                return True
            user = await self.passport.authenticate(strategy or self.passport.default_strategy, context)
            if not user:
                raise UnauthorizedException("Unauthorized")
            context.request.state.user = user
            return True

    return StrategyGuard


class PassportModule:
    @staticmethod
    def register(
        *strategies: type[PassportStrategy],
        default_strategy: str = "default",
        is_global: bool = False,
    ) -> type:
        _validate_strategy_names(strategies)
        options = _validate_passport_options({"default_strategy": default_strategy})

        @Module(
            providers=[use_value(PASSPORT_OPTIONS, options), PassportService, *strategies],
            exports=[PassportService],
            global_module=is_global,
        )
        class DynamicPassportModule:
            pass

        setattr(DynamicPassportModule, "__fanest_passport_strategies__", list(strategies))
        return DynamicPassportModule

    @staticmethod
    def register_async(
        *,
        use_factory: Any,
        inject: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        async def load_options(*dependencies: Any) -> dict[str, Any]:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await result
            if result is None:
                result = {}
            if isinstance(result, list | tuple):
                result = {"strategies": list(result)}
            if not isinstance(result, dict):
                raise ValueError("PassportModule.register_async factory must return a dict or a list of strategies")
            strategies = [_coerce_strategy(strategy) for strategy in result.get("strategies", [])]
            _validate_strategy_instances(strategies)
            return {
                "default_strategy": _validate_passport_options(result).get("default_strategy"),
                "strategies": strategies,
            }

        @Module(
            providers=[
                provider_factory(PASSPORT_ASYNC_OPTIONS, load_options, inject=inject or []),
                use_existing(PASSPORT_OPTIONS, PASSPORT_ASYNC_OPTIONS),
                PassportService,
                PassportAsyncInitializer,
            ],
            exports=[PassportService],
            global_module=is_global,
        )
        class DynamicPassportModule:
            pass

        return DynamicPassportModule


def _validate_passport_options(options: dict[str, Any]) -> dict[str, Any]:
    default_strategy = str(options.get("default_strategy") or "default").strip()
    if not default_strategy:
        raise ValueError("Passport default_strategy must be non-empty")
    return {**options, "default_strategy": default_strategy}


def _validate_strategy_names(strategies: tuple[type[PassportStrategy], ...]) -> None:
    seen_names: set[str] = set()
    for strategy in strategies:
        name = str(getattr(strategy, "name", "") or "").strip()
        if not name:
            raise ValueError("Passport strategies must define a non-empty name")
        if name in seen_names:
            raise ValueError(f"Passport strategy already registered: {name}")
        seen_names.add(name)


def _validate_strategy_instances(strategies: list[PassportStrategy]) -> None:
    seen_names: set[str] = set()
    for strategy in strategies:
        name = str(getattr(strategy, "name", "") or "").strip()
        if not name:
            raise ValueError("Passport strategies must define a non-empty name")
        if name in seen_names:
            raise ValueError(f"Passport strategy already registered: {name}")
        seen_names.add(name)


def _coerce_strategy(strategy: Any) -> PassportStrategy:
    if isinstance(strategy, PassportStrategy):
        return strategy
    if inspect.isclass(strategy) and issubclass(strategy, PassportStrategy):
        return strategy()
    raise ValueError("Passport strategies must be PassportStrategy instances or classes")
