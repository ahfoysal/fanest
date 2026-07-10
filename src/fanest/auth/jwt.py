import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast

import jwt as _pyjwt

from fanest import ForbiddenException, Inject, Injectable, Module, UnauthorizedException, use_class, use_value
from fanest.core.enhancers import APP_GUARD
from fanest.core.metadata import ParameterSource
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

JWT_OPTIONS = token("JWT_OPTIONS")
UNSAFE_JWT_ALGORITHMS = {"none"}
jwt: Any = _pyjwt


def _validate_jwt_options(options: dict[str, Any]) -> dict[str, Any]:
    secret = options.get("secret")
    algorithm = str(options.get("algorithm", "HS256"))
    required_claims = options.get("required_claims", [])
    expires_in_seconds = options.get("expires_in_seconds", 3600)
    leeway = options.get("leeway", 0)
    issuer = options.get("issuer")
    audience = options.get("audience")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("AuthModule requires a non-empty JWT secret")
    if algorithm.lower() in UNSAFE_JWT_ALGORITHMS:
        raise ValueError("AuthModule does not allow unsigned JWT algorithms")
    if expires_in_seconds is not None and expires_in_seconds < 0:
        raise ValueError("AuthModule expires_in_seconds cannot be negative")
    if leeway < 0:
        raise ValueError("AuthModule leeway cannot be negative")
    if issuer is not None and not str(issuer).strip():
        raise ValueError("AuthModule issuer cannot be empty")
    if isinstance(audience, str) and not audience.strip():
        raise ValueError("AuthModule audience cannot be empty")
    if isinstance(audience, list) and any(not str(item).strip() for item in audience):
        raise ValueError("AuthModule audience entries cannot be empty")
    if not isinstance(required_claims, list | tuple | set):
        raise ValueError("AuthModule required_claims must be a list, tuple, or set")
    normalized_claims = [str(claim).strip() for claim in required_claims]
    if any(not claim for claim in normalized_claims):
        raise ValueError("AuthModule required_claims cannot contain empty names")
    options["required_claims"] = normalized_claims
    return options


def _ensure_safe_algorithm(algorithm: str) -> str:
    normalized = str(algorithm).strip()
    if not normalized:
        raise ValueError("JWT algorithm cannot be empty")
    if normalized.lower() in UNSAFE_JWT_ALGORITHMS:
        raise ValueError("JWT unsigned algorithms are not allowed")
    return normalized


def _ensure_safe_algorithms(algorithms: Any) -> list[str]:
    if isinstance(algorithms, str):
        candidates = [algorithms]
    else:
        candidates = list(algorithms)
    if not candidates:
        raise ValueError("JWT algorithms cannot be empty")
    return [_ensure_safe_algorithm(algorithm) for algorithm in candidates]


@Injectable()
class JwtService:
    def __init__(self, options: dict[str, Any] = Inject(JWT_OPTIONS)):
        self.secret = options["secret"]
        self.algorithm = options.get("algorithm", "HS256")
        self.expires_in_seconds = options.get("expires_in_seconds", 3600)
        self.issuer = options.get("issuer")
        self.audience = options.get("audience")
        self.leeway = options.get("leeway", 0)
        self.required_claims = list(options.get("required_claims", []))

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
        not_before_seconds = options.pop("not_before_seconds", None)
        if not_before_seconds is not None:
            token_payload["nbf"] = datetime.now(timezone.utc) + timedelta(
                seconds=not_before_seconds
            )
        issuer = options.pop("issuer", self.issuer)
        if issuer is not None and "iss" not in token_payload:
            token_payload["iss"] = issuer
        audience = options.pop("audience", self.audience)
        if audience is not None and "aud" not in token_payload:
            token_payload["aud"] = audience
        secret = options.pop("secret", self.secret)
        algorithm = _ensure_safe_algorithm(options.pop("algorithm", self.algorithm))
        return jwt.encode(token_payload, secret, algorithm=algorithm, **options)

    def verify(self, token: str, **options: Any) -> dict[str, Any]:
        secret = options.pop("secret", self.secret)
        algorithms = _ensure_safe_algorithms(
            options.pop("algorithms", [options.pop("algorithm", self.algorithm)])
        )
        issuer = options.pop("issuer", self.issuer)
        audience = options.pop("audience", self.audience)
        leeway = options.pop("leeway", self.leeway)
        required_claims = options.pop("required_claims", self.required_claims)
        verify_options = dict(options.pop("options", {}) or {})
        if required_claims:
            verify_options["require"] = list(required_claims)
        if issuer is not None:
            options["issuer"] = issuer
        if audience is not None:
            options["audience"] = audience
        if leeway:
            options["leeway"] = leeway
        if verify_options:
            options["options"] = verify_options
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


class SecurityScopes:
    """The OAuth2 scopes required by the current route — FastAPI's
    ``SecurityScopes`` analog. Inject with ``CurrentSecurityScopes()``."""

    def __init__(self, scopes: list[str] | None = None):
        self.scopes = list(scopes or [])

    @property
    def scope_str(self) -> str:
        return " ".join(self.scopes)


class ScopesGuard:
    """Enforces ``@Scopes(...)`` requirements against the authenticated user's
    granted scopes (``scope`` space-delimited claim per RFC 6749, or a
    list-valued ``scope``/``scopes``/``scp`` claim). Every required scope must
    be granted, matching FastAPI's ``SecurityScopes`` semantics."""

    def can_activate(self, context):
        if is_public(context.handler, context.controller.__class__):
            return True
        required = scopes_for(context.handler, context.controller.__class__)
        if not required:
            return True
        user = getattr(context.request.state, "user", None)
        if user is None:
            raise UnauthorizedException("Missing bearer token")
        granted = granted_scopes(user)
        if all(scope in granted for scope in required):
            return True
        raise ForbiddenException(f"Insufficient scope: requires '{' '.join(required)}'")


def Scopes(*scopes: str, security_scheme: str = "oauth2"):
    """Require OAuth2 scopes on a handler or controller. Also records the
    OpenAPI security requirement for ``security_scheme`` so Swagger documents
    the scopes (pair with ``DocumentBuilder.add_oauth2``)."""

    def decorator(target):
        setattr(target, "__fanest_scopes__", list(scopes))
        metadata = dict(getattr(target, "__fanest_metadata__", {}))
        metadata["scopes"] = list(scopes)
        setattr(target, "__fanest_metadata__", metadata)
        securities = list(getattr(target, "__fanest_security__", []))
        securities.append({security_scheme: list(scopes)})
        setattr(target, "__fanest_security__", securities)
        return target

    return decorator


def scopes_for(handler: Any, controller: type | None = None) -> list[str]:
    scopes = _metadata_value(handler, "__fanest_scopes__", "scopes", None)
    if scopes is not None:
        return list(scopes)
    if controller is not None:
        scopes = _metadata_value(controller, "__fanest_scopes__", "scopes", None)
        if scopes is not None:
            return list(scopes)
    return []


def granted_scopes(user: Any) -> list[str]:
    """Extract the granted scopes from a decoded token payload."""
    if not isinstance(user, dict):
        return []
    for claim_name in ("scope", "scopes", "scp"):
        claim = user.get(claim_name)
        if claim is None:
            continue
        if isinstance(claim, str):
            return claim.split()
        if isinstance(claim, (list, tuple, set)):
            return [str(scope) for scope in claim]
    return []


def _security_scopes_factory(data: Any, context: Any) -> SecurityScopes:
    return SecurityScopes(scopes_for(context.handler, context.controller.__class__))


def CurrentSecurityScopes() -> Any:
    from fanest.common.decorators import create_param_decorator

    return create_param_decorator(_security_scopes_factory)(None)


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


def CurrentUser(default: Any = None) -> Any:
    return ParameterSource(source="state", name="user", default=default)


class AuthModule:
    @staticmethod
    def for_root(
        *,
        secret: str,
        algorithm: str = "HS256",
        expires_in_seconds: int | None = 3600,
        issuer: str | None = None,
        audience: str | list[str] | None = None,
        leeway: int | float = 0,
        required_claims: list[str] | None = None,
        is_global: bool = False,
        global_guard: bool = False,
    ) -> type:
        options = _validate_jwt_options({
            "secret": secret,
            "algorithm": algorithm,
            "expires_in_seconds": expires_in_seconds,
            "issuer": issuer,
            "audience": audience,
            "leeway": leeway,
            "required_claims": required_claims or [],
        })

        providers = [use_value(JWT_OPTIONS, options), JwtService]
        if global_guard:
            providers.extend([
                use_class(APP_GUARD, JwtAuthGuard),
                use_class(APP_GUARD, RolesGuard),
                use_class(APP_GUARD, ScopesGuard),
            ])

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
            providers.extend([
                use_class(APP_GUARD, JwtAuthGuard),
                use_class(APP_GUARD, RolesGuard),
                use_class(APP_GUARD, ScopesGuard),
            ])

        @Module(
            providers=providers,
            exports=[JwtService],
            global_module=is_global,
        )
        class DynamicAuthModule:
            pass

        return DynamicAuthModule


JwtModule = AuthModule
