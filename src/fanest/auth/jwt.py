import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast

import jwt

from fanest import ForbiddenException, Inject, Injectable, Module, UnauthorizedException, use_class, use_value
from fanest.core.enhancers import APP_GUARD
from fanest.core.metadata import ParameterSource
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

JWT_OPTIONS = token("JWT_OPTIONS")
UNSAFE_JWT_ALGORITHMS = {"none"}


def _validate_jwt_options(options: dict[str, Any]) -> dict[str, Any]:
    secret = options.get("secret")
    algorithm = str(options.get("algorithm", "HS256"))
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("AuthModule requires a non-empty JWT secret")
    if algorithm.lower() in UNSAFE_JWT_ALGORITHMS:
        raise ValueError("AuthModule does not allow unsigned JWT algorithms")
    return options


@Injectable()
class JwtService:
    def __init__(self, options: dict[str, Any] = Inject(JWT_OPTIONS)):
        self.secret = options["secret"]
        self.algorithm = options.get("algorithm", "HS256")
        self.expires_in_seconds = options.get("expires_in_seconds", 3600)

    def sign(self, payload: dict[str, Any], **options: Any) -> str:
        token_payload = dict(payload)
        # RFC 7519 requires `sub` to be a string, and PyJWT >= 2.10 rejects a
        # non-string `sub` on verify. Coerce it here so the common
        # `sub = user.id` (int) round-trips through sign()/verify().
        subject = token_payload.get("sub")
        if subject is not None and not isinstance(subject, str):
            token_payload["sub"] = str(subject)
        expires_in_seconds = options.pop("expires_in_seconds", self.expires_in_seconds)
        if expires_in_seconds is not None:
            token_payload["exp"] = datetime.now(timezone.utc) + timedelta(
                seconds=expires_in_seconds
            )
        secret = options.pop("secret", self.secret)
        algorithm = options.pop("algorithm", self.algorithm)
        return jwt.encode(token_payload, secret, algorithm=algorithm, **options)

    def verify(self, token: str, **options: Any) -> dict[str, Any]:
        secret = options.pop("secret", self.secret)
        algorithms = options.pop("algorithms", [options.pop("algorithm", self.algorithm)])
        return jwt.decode(token, secret, algorithms=algorithms, **options)

    def decode(self, token: str, **options: Any) -> dict[str, Any]:
        return jwt.decode(token, options={"verify_signature": False}, **options)

    async def sign_async(self, payload: dict[str, Any], **options: Any) -> str:
        return self.sign(payload, **options)

    async def verify_async(self, token: str, **options: Any) -> dict[str, Any]:
        return self.verify(token, **options)


class JwtAuthGuard:
    def __init__(self, jwt_service: JwtService):
        self.jwt_service = jwt_service

    def can_activate(self, context):
        if is_public(context.handler, context.controller.__class__):
            return True
        authorization = context.request.headers.get("authorization")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedException("Missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        if not token:
            raise UnauthorizedException("Missing bearer token")
        try:
            context.request.state.user = self.jwt_service.verify(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedException("Invalid bearer token") from exc
        return True


class RolesGuard:
    def can_activate(self, context):
        if is_public(context.handler, context.controller.__class__):
            return True
        required_roles = roles_for(context.handler, context.controller.__class__)
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
        metadata = dict(getattr(target, "__fanest_metadata__", {}))
        metadata["roles"] = list(roles)
        setattr(target, "__fanest_metadata__", metadata)
        return target

    return decorator


def Public():
    def decorator(target):
        setattr(target, "__fanest_public__", True)
        metadata = dict(getattr(target, "__fanest_metadata__", {}))
        metadata["is_public"] = True
        setattr(target, "__fanest_metadata__", metadata)
        return target

    return decorator


def is_public(handler: Any, controller: type | None = None) -> bool:
    if _metadata_value(handler, "__fanest_public__", "is_public", None) is True:
        return True
    return bool(controller is not None and _metadata_value(controller, "__fanest_public__", "is_public", None) is True)


def roles_for(handler: Any, controller: type | None = None) -> list[str]:
    roles = _metadata_value(handler, "__fanest_roles__", "roles", None)
    if roles is not None:
        return list(roles)
    if controller is not None:
        roles = _metadata_value(controller, "__fanest_roles__", "roles", None)
        if roles is not None:
            return list(roles)
    return []


def _metadata_value(target: Any, attr: str, key: str, default: Any) -> Any:
    if hasattr(target, attr):
        return getattr(target, attr)
    metadata = getattr(target, "__fanest_metadata__", {})
    if key in metadata:
        return metadata[key]
    func = getattr(target, "__func__", None)
    if func is not None:
        if hasattr(func, attr):
            return getattr(func, attr)
        metadata = getattr(func, "__fanest_metadata__", {})
        if key in metadata:
            return metadata[key]
    return default


def CurrentUser(default: Any = None) -> ParameterSource:
    return ParameterSource(source="state", name="user", default=default)


class AuthModule:
    @staticmethod
    def for_root(
        *,
        secret: str,
        algorithm: str = "HS256",
        expires_in_seconds: int | None = 3600,
        is_global: bool = False,
        global_guard: bool = False,
    ) -> type:
        options = _validate_jwt_options({
            "secret": secret,
            "algorithm": algorithm,
            "expires_in_seconds": expires_in_seconds,
        })

        providers = [use_value(JWT_OPTIONS, options), JwtService]
        if global_guard:
            providers.extend([use_class(APP_GUARD, JwtAuthGuard), RolesGuard])

        @Module(
            providers=providers,
            exports=[JwtService],
            global_module=is_global,
        )
        class DynamicAuthModule:
            pass

        return DynamicAuthModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any]],
        inject: list[Any] | None = None,
        is_global: bool = False,
        global_guard: bool = False,
    ) -> type:
        async def load_options(*dependencies: Any) -> dict[str, Any]:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await result
            return _validate_jwt_options(dict(cast(dict[str, Any], result or {})))

        providers = [provider_factory(JWT_OPTIONS, load_options, inject=inject or []), JwtService]
        if global_guard:
            providers.extend([use_class(APP_GUARD, JwtAuthGuard), RolesGuard])

        @Module(
            providers=providers,
            exports=[JwtService],
            global_module=is_global,
        )
        class DynamicAuthModule:
            pass

        return DynamicAuthModule


JwtModule = AuthModule
