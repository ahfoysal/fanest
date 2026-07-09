from copy import deepcopy
from typing import Any, cast, get_args, get_origin

from pydantic import BaseModel, create_model, field_validator


def PartialType(model: type[BaseModel]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    for name, field in model.model_fields.items():
        annotation = _optional(field.annotation)
        field_info = deepcopy(field)
        field_info.default = None
        field_info.default_factory = None
        fields[name] = (annotation, field_info)
    return cast(
        type[BaseModel],
        create_model(
            f"Partial{model.__name__}",
            __base__=BaseModel,
            __validators__=_field_validators_for(model, list(model.model_fields)),
            **cast(Any, fields),
        ),
    )


def PickType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    for name in fields:
        field = model.model_fields[name]
        model_fields[name] = (field.annotation, deepcopy(field))
    return cast(
        type[BaseModel],
        create_model(
            f"Pick{model.__name__}",
            __base__=BaseModel,
            __validators__=_field_validators_for(model, fields),
            **cast(Any, model_fields),
        ),
    )


def OmitType(model: type[BaseModel], fields: list[str]) -> type[BaseModel]:
    selected = [name for name in model.model_fields if name not in fields]
    return PickType(model, selected)


def IntersectionType(left: type[BaseModel], right: type[BaseModel]) -> type[BaseModel]:
    model_fields: dict[str, tuple[Any, Any]] = {}
    for model in [left, right]:
        for name, field in model.model_fields.items():
            model_fields[name] = (field.annotation, deepcopy(field))
    return cast(
        type[BaseModel],
        create_model(
            f"{left.__name__}{right.__name__}Intersection",
            __base__=BaseModel,
            __validators__={
                **_field_validators_for(left, list(left.model_fields)),
                **_field_validators_for(right, list(right.model_fields)),
            },
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


def _field_validators_for(model: type[BaseModel], selected_fields: list[str]) -> dict[str, Any]:
    decorators = getattr(model, "__pydantic_decorators__", None)
    if decorators is None:
        return {}
    selected = set(selected_fields)
    validators: dict[str, Any] = {}
    for name, decorator in decorators.field_validators.items():
        fields = tuple(field for field in decorator.info.fields if field in selected)
        if not fields:
            continue
        function = getattr(decorator.func, "__func__", decorator.func)
        validators[name] = field_validator(
            *fields,
            mode=decorator.info.mode,
            check_fields=False,
            json_schema_input_type=decorator.info.json_schema_input_type,
        )(classmethod(function))
    return validators
