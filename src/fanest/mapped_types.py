from typing import Any, cast, get_args, get_origin

from pydantic import BaseModel, create_model


def PartialType(model: type[BaseModel]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for name, field in model.model_fields.items():
        annotation = _optional(field.annotation)
        fields[name] = (annotation, None)
    return cast(
        type[BaseModel],
        create_model(f"Partial{model.__name__}", __base__=BaseModel, **cast(Any, fields)),
    )


def PickType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    for name in fields:
        field = model.model_fields[name]
        default = field.default if not field.is_required() else ...
        model_fields[name] = (field.annotation, default)
    return cast(
        type[BaseModel],
        create_model(f"Pick{model.__name__}", __base__=BaseModel, **cast(Any, model_fields)),
    )


def OmitType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    selected = [name for name in model.model_fields if name not in fields]
    return PickType(model, selected)


def IntersectionType(left: type[BaseModel], right: type[BaseModel]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    for model in [left, right]:
        for name, field in model.model_fields.items():
            default = field.default if not field.is_required() else ...
            model_fields[name] = (field.annotation, default)
    return cast(
        type[BaseModel],
        create_model(
            f"{left.__name__}{right.__name__}Intersection",
            __base__=BaseModel,
            **cast(Any, model_fields),
        ),
    )


def _optional(annotation: Any) -> Any:
    if get_origin(annotation) is type(None):
        return annotation
    args = get_args(annotation)
    if type(None) in args:
        return annotation
    return annotation | None
