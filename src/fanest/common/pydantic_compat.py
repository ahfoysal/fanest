from __future__ import annotations

from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

try:  # Pydantic v2
    from pydantic import TypeAdapter
except ImportError:  # pragma: no cover - exercised by fake v1 compatibility tests
    TypeAdapter = None  # type: ignore[assignment]

try:  # Pydantic v1 fallback
    from pydantic import parse_obj_as
except ImportError:  # pragma: no cover - Pydantic v2 keeps this as a deprecated shim
    parse_obj_as = None  # type: ignore[assignment]


ModelT = TypeVar("ModelT")


def pydantic_model_fields(model: type[Any]) -> tuple[str, ...]:
    fields = getattr(model, "model_fields", None)
    if fields is not None:
        return tuple(fields)
    return tuple(getattr(model, "__fields__", {}))


def pydantic_validate_model(model: type[ModelT], value: Any) -> ModelT:
    model_validate = getattr(model, "model_validate", None)
    if callable(model_validate):
        return cast(ModelT, model_validate(value))
    parse_obj = getattr(model, "parse_obj", None)
    if callable(parse_obj):
        return cast(ModelT, parse_obj(value))
    raise TypeError(f"{model!r} is not a supported Pydantic model type")


def pydantic_dump_model(model: Any) -> dict[str, Any]:
    model_dump = getattr(model, "model_dump", None)
    if callable(model_dump):
        return cast(dict[str, Any], model_dump())
    return cast(dict[str, Any], model.dict())


def pydantic_validate_type(annotation: Any, value: Any) -> Any:
    if TypeAdapter is not None:
        return TypeAdapter(annotation).validate_python(value)
    if parse_obj_as is None:
        raise RuntimeError("Pydantic validation requires TypeAdapter or parse_obj_as")
    return parse_obj_as(annotation, value)


__all__ = [
    "BaseModel",
    "ValidationError",
    "pydantic_dump_model",
    "pydantic_model_fields",
    "pydantic_validate_model",
    "pydantic_validate_type",
]
