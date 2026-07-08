from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from fanest import ForbiddenException, Injectable, Module, UnauthorizedException
from fanest.core.metadata import ParameterSource


@Injectable()
class JwtService:
    _secret = "change-me"
    _algorithm = "HS256"
    _expires_in_seconds: int | None = 3600

    @classmethod
    def configure(
        cls,
        *,
        secret: str,
        algorithm: str = "HS256",
        expires_in_seconds: int | None = 3600,
    ) -> None:
        cls._secret = secret
        cls._algorithm = algorithm
        cls._expires_in_seconds = expires_in_seconds

    def sign(self, payload: dict[str, Any]) -> str:
        token_payload = dict(payload)
        if self._expires_in_seconds is not None:
            token_payload["exp"] = datetime.now(timezone.utc) + timedelta(
                seconds=self._expires_in_seconds
            )
        return jwt.encode(token_payload, self._secret, algorithm=self._algorithm)

    def verify(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self._secret, algorithms=[self._algorithm])


class JwtAuthGuard:
    def __init__(self, jwt_service: JwtService):
        self.jwt_service = jwt_service

    def can_activate(self, context):
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
        JwtService.configure(
            secret=secret,
            algorithm=algorithm,
            expires_in_seconds=expires_in_seconds,
        )

        @Module(providers=[JwtService], exports=[JwtService])
        class DynamicAuthModule:
            pass

        return DynamicAuthModule
