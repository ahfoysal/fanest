from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


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


def ApiBearerAuth() -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_bearer_auth__", True)
        return target

    return decorator


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
    route = getattr(handler, "__fanest_route__", None)
    if route is not None:
        extra = dict(route.options.get("openapi_extra", {}))
    else:
        extra = dict(getattr(handler, "__fanest_pending_openapi_extra__", {}))

    if key == "parameters":
        extra[key] = [*extra.get(key, []), value]
    else:
        extra[key] = value

    if route is not None:
        route.options["openapi_extra"] = extra
    else:
        setattr(handler, "__fanest_pending_openapi_extra__", extra)


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
