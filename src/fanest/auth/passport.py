import inspect
from typing import Any

from fanest import Injectable, Module, UnauthorizedException


class PassportStrategy:
    name = "default"

    def authenticate(self, context) -> Any:
        raise NotImplementedError


@Injectable()
class PassportService:
    def __init__(self) -> None:
        self._strategies: dict[str, PassportStrategy] = {}

    def register(self, strategy: PassportStrategy) -> None:
        self._strategies[strategy.name] = strategy

    async def authenticate(self, name: str, context) -> Any:
        if name not in self._strategies:
            raise UnauthorizedException(f"Unknown passport strategy: {name}")
        strategy = self._strategies[name]
        result = strategy.authenticate(context)
        if inspect.isawaitable(result):
            result = await result
        return result


def AuthGuard(strategy: str = "default"):
    class StrategyGuard:
        def __init__(self, passport: PassportService):
            self.passport = passport

        async def can_activate(self, context):
            user = await self.passport.authenticate(strategy, context)
            if not user:
                raise UnauthorizedException("Unauthorized")
            context.request.state.user = user
            return True

    return StrategyGuard


class PassportModule:
    @staticmethod
    def register(*strategies: type[PassportStrategy], is_global: bool = False) -> type:
        @Module(providers=[PassportService, *strategies], exports=[PassportService], global_module=is_global)
        class DynamicPassportModule:
            pass

        setattr(DynamicPassportModule, "__fanest_passport_strategies__", list(strategies))
        return DynamicPassportModule
