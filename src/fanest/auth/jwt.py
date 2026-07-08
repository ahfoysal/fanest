from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import jwt

from fanest import ForbiddenException, Inject, Injectable, Module, UnauthorizedException, use_value
from fanest.core.metadata import ParameterSource
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

JWT_OPTIONS = token("JWT_OPTIONS")


@Injectable()
class JwtService:
    def __init__(self, options: dict[str, Any] = Inject(JWT_OPTIONS)):
        self.secret = options["secret"]
        self.algorithm = options.get("algorithm", "HS256")
        self.expires_in_seconds = options.get("expires_in_seconds", 3600)

    def sign(self, payload: dict[str, Any]) -> str:
        token_payload = dict(payload)
        if self.expires_in_seconds is not None:
            token_payload["exp"] = datetime.now(timezone.utc) + timedelta(
                seconds=self.expires_in_seconds
            )
        return jwt.encode(token_payload, self.secret, algorithm=self.algorithm)

    def verify(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self.secret, algorithms=[self.algorithm])


class JwtAuthGuard:
    def __init__(self, jwt_service: JwtService):
        self.jwt_service = jwt_service

    def can_activate(self, context):
        if is_public(context.handler, context.controller.__class__):
            return True
        authorization = context.request.headers.get("authorization")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedException("Missing bearer token")
        token = authorization.split(" ", 1)[1]
        try:
            context.request.state.user = self.jwt_service.verify(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedException("Invalid bearer token") from exc
        return True


class RolesGuard:
    def can_activate(self, context):
        if is_public(context.handler, context.controller.__class__):
            return True
        required_roles = getattr(context.handler, "__fanest_roles__", [])
        if not required_roles:
            return True
        user = getattr(context.request.state, "user", None)
        roles = []
        if isinstance(user, dict):
            roles = user.get("roles", [])
        if any(role in roles for role in required_roles):
            return True
        raise ForbiddenException("Insufficient role")


def Roles(*roles: str):
    def decorator(target):
        setattr(target, "__fanest_roles__", list(roles))
        return target

    return decorator


def Public():
    def decorator(target):
        setattr(target, "__fanest_public__", True)
        return target

    return decorator


def is_public(handler: Any, controller: type | None = None) -> bool:
    if getattr(handler, "__fanest_public__", False):
        return True
    func = getattr(handler, "__func__", None)
    if func is not None and getattr(func, "__fanest_public__", False):
        return True
    return bool(controller is not None and getattr(controller, "__fanest_public__", False))


def CurrentUser(default: Any = None) -> ParameterSource:
    return ParameterSource(source="state", name="user", default=default)


class AuthModule:
    @staticmethod
    def for_root(
        *,
        secret: str,
        algorithm: str = "HS256",
        expires_in_seconds: int | None = 3600,
    ) -> type:
        options = {
            "secret": secret,
            "algorithm": algorithm,
            "expires_in_seconds": expires_in_seconds,
        }

        @Module(providers=[use_value(JWT_OPTIONS, options), JwtService], exports=[JwtService])
        class DynamicAuthModule:
            pass

        return DynamicAuthModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
    ) -> type:
        @Module(
            providers=[provider_factory(JWT_OPTIONS, use_factory, inject=inject or []), JwtService],
            exports=[JwtService],
        )
        class DynamicAuthModule:
            pass

        return DynamicAuthModule
