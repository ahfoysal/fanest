from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import Field

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


def ApiBearerAuth() -> Callable[[T], T]:
    def decorator(target: T) -> T:
        setattr(target, "__fanest_bearer_auth__", True)
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
