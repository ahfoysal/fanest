from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from pydantic import BaseModel


class SerializeOptions:
    def __init__(
        self,
        *,
        include: set[str] | None = None,
        exclude: set[str] | None = None,
        exclude_none: bool = False,
    ) -> None:
        self.include = include
        self.exclude = exclude
        self.exclude_none = exclude_none


def Serialize(
    *,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    exclude_none: bool = False,
):
    def decorator(handler):
        setattr(
            handler,
            "__fanest_serialize__",
            SerializeOptions(include=include, exclude=exclude, exclude_none=exclude_none),
        )
        return handler

    return decorator


class ClassSerializerInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        options = getattr(context.handler, "__fanest_serialize__", SerializeOptions())
        return serialize_value(result, options)


def serialize_value(value: Any, options: SerializeOptions) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(
            include=options.include,
            exclude=options.exclude,
            exclude_none=options.exclude_none,
        )
    if is_dataclass(value):
        return serialize_value(asdict(cast(Any, value)), options)
    if isinstance(value, list):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, tuple):
        return [serialize_value(item, options) for item in value]
    if isinstance(value, Mapping):
        data = dict(value)
        if options.include is not None:
            data = {key: data[key] for key in options.include if key in data}
        if options.exclude is not None:
            data = {key: val for key, val in data.items() if key not in options.exclude}
        if options.exclude_none:
            data = {key: val for key, val in data.items() if val is not None}
        return data
    return value
