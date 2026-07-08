from collections.abc import Callable
import inspect
from typing import Any, TypeVar

from pydantic import Field

T = TypeVar("T")

_FANEST_EXTRA_MODELS: list[type] = []


def ApiTags(*tags: str) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_swagger_tags__", list(tags))
        return target

    return decorator


def ApiResponse(status_code: int, description: str | None = None, model: type | None = None):
    def decorator(handler):
        route = getattr(handler, "__fanest_route__", None)
        if route is None:
            responses: dict[int, dict[str, Any]] = {}
        else:
            responses = dict(route.options.get("responses", {}))
        response: dict[str, Any] = {}
        if description:
            response["description"] = description
        if model:
            response["model"] = model
        responses[status_code] = response
        if route is not None:
            route.options["responses"] = responses
        else:
            setattr(handler, "__fanest_pending_responses__", responses)
        return handler

    return decorator


def ApiOkResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(200, description, model)


def ApiCreatedResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(201, description, model)


def ApiAcceptedResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(202, description, model)


def ApiNoContentResponse(description: str | None = None):
    return ApiResponse(204, description)


def ApiBadRequestResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(400, description, model)


def ApiUnauthorizedResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(401, description, model)


def ApiForbiddenResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(403, description, model)


def ApiNotFoundResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(404, description, model)


def ApiConflictResponse(description: str | None = None, model: type | None = None):
    return ApiResponse(409, description, model)


def ApiOperation(*, summary: str | None = None, description: str | None = None):
    def decorator(handler):
        _set_route_option(handler, "summary", summary)
        _set_route_option(handler, "description", description)
        return handler

    return decorator


def ApiParam(name: str, description: str | None = None):
    def decorator(handler):
        _append_openapi_extra(handler, "parameters", _parameter(name, "path", description))
        return handler

    return decorator


def ApiQuery(name: str, description: str | None = None, required: bool = False):
    def decorator(handler):
        parameter = _parameter(name, "query", description)
        parameter["required"] = required
        _append_openapi_extra(handler, "parameters", parameter)
        return handler

    return decorator


def ApiBody(description: str | None = None):
    def decorator(handler):
        if description:
            _append_openapi_extra(handler, "requestBody", {"description": description})
        return handler

    return decorator


def ApiHeader(name: str, description: str | None = None, required: bool = False):
    def decorator(handler):
        parameter = _parameter(name, "header", description)
        parameter["required"] = required
        _append_openapi_extra(handler, "parameters", parameter)
        return handler

    return decorator


def ApiConsumes(*mime_types: str):
    def decorator(handler):
        _merge_openapi_extra(
            handler,
            {
                "requestBody": {
                    "content": {
                        mime_type: {"schema": {"type": "object"}} for mime_type in mime_types
                    }
                }
            },
        )
        return handler

    return decorator


def ApiProduces(*mime_types: str):
    def decorator(handler):
        _merge_openapi_extra(
            handler,
            {
                "responses": {
                    "200": {
                        "content": {
                            mime_type: {"schema": {"type": "object"}} for mime_type in mime_types
                        }
                    }
                }
            },
        )
        return handler

    return decorator


def ApiExcludeEndpoint():
    def decorator(handler):
        _set_route_option(handler, "include_in_schema", False)
        return handler

    return decorator


def ApiSecurity(name: str, scopes: list[str] | None = None) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        securities = list(getattr(target, "__fanest_security__", []))
        securities.append({name: scopes or []})
        setattr(target, "__fanest_security__", securities)
        return target

    return decorator


def ApiBearerAuth(name: str = "bearer") -> Callable[[T], T]:
    def decorator(target: T) -> T:
        if name == "bearer":
            setattr(target, "__fanest_bearer_auth__", True)
            return target
        return ApiSecurity(name)(target)

    return decorator


def ApiBasicAuth(name: str = "basic") -> Callable[[T], T]:
    return ApiSecurity(name)


def ApiCookieAuth(name: str = "cookie") -> Callable[[T], T]:
    return ApiSecurity(name)


def ApiExtraModels(*models: type) -> Callable[[T], T]:
    def decorator(target: T) -> T:
        existing = list(getattr(target, "__fanest_extra_models__", []))
        existing.extend(models)
        setattr(target, "__fanest_extra_models__", existing)
        for model in models:
            if model not in _FANEST_EXTRA_MODELS:
                _FANEST_EXTRA_MODELS.append(model)
        return target

    return decorator


def ApiExtension(name: str, value: Any) -> Callable[[T], T]:
    extension_name = name if name.startswith("x-") else f"x-{name}"

    def decorator(target: T) -> T:
        if inspect.isfunction(target) or hasattr(target, "__fanest_route__"):
            _merge_openapi_extra(target, {extension_name: value})
        else:
            extensions = dict(getattr(target, "__fanest_openapi_extensions__", {}))
            extensions[extension_name] = value
            setattr(target, "__fanest_openapi_extensions__", extensions)
        return target

    return decorator


def ApiProperty(
    default: Any = ...,
    *,
    description: str | None = None,
    example: Any = None,
    examples: list[Any] | None = None,
    deprecated: bool | None = None,
    **extra: Any,
) -> Any:
    kwargs = dict(extra)
    if description is not None:
        kwargs["description"] = description
    if example is not None:
        kwargs["examples"] = [example]
    if examples is not None:
        kwargs["examples"] = examples
    if deprecated is not None:
        kwargs["deprecated"] = deprecated
    return Field(default, **kwargs)


def ApiPropertyOptional(
    default: Any = None,
    *,
    description: str | None = None,
    example: Any = None,
    examples: list[Any] | None = None,
    deprecated: bool | None = None,
    **extra: Any,
) -> Any:
    return ApiProperty(
        default,
        description=description,
        example=example,
        examples=examples,
        deprecated=deprecated,
        **extra,
    )


def ApiHideProperty(default: Any = None) -> Any:
    return Field(default, exclude=True, json_schema_extra={"hidden": True})


def _set_route_option(handler: Any, key: str, value: Any) -> None:
    if value is None:
        return
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        route.options[key] = value
        return
    pending = dict(getattr(handler, "__fanest_pending_route_options__", {}))
    pending[key] = value
    setattr(handler, "__fanest_pending_route_options__", pending)


def _append_openapi_extra(handler: Any, key: str, value: Any) -> None:
    extra = _openapi_extra(handler)

    if key == "parameters":
        extra[key] = [*extra.get(key, []), value]
    else:
        extra[key] = value

    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        route.options["openapi_extra"] = extra
    else:
        setattr(handler, "__fanest_pending_openapi_extra__", extra)


def _merge_openapi_extra(handler: Any, value: dict[str, Any]) -> None:
    extra = _openapi_extra(handler)
    _deep_update(extra, value)
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        route.options["openapi_extra"] = extra
    else:
        setattr(handler, "__fanest_pending_openapi_extra__", extra)


def _openapi_extra(handler: Any) -> dict[str, Any]:
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        return dict(route.options.get("openapi_extra", {}))
    return dict(getattr(handler, "__fanest_pending_openapi_extra__", {}))


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _parameter(name: str, location: str, description: str | None) -> dict[str, Any]:
    parameter: dict[str, Any] = {
        "name": name,
        "in": location,
        "schema": {"type": "string"},
    }
    if description:
        parameter["description"] = description
    if location == "path":
        parameter["required"] = True
    return parameter
