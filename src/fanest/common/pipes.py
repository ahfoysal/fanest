from enum import Enum
import re
from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

from pydantic import BaseModel, TypeAdapter, ValidationError

from fanest.common.exceptions import BadRequestException


class ValidationPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        annotation = metadata.get("annotation")
        if annotation is None or annotation is Any:
            return value
        try:
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                if isinstance(value, annotation):
                    return value
                return annotation.model_validate(value)
            return TypeAdapter(annotation).validate_python(value)
        except ValidationError as exc:
            raise BadRequestException(exc.errors()) from exc


class ParseIntPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be an integer") from exc


class ParseBoolPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        raise BadRequestException(f"{metadata.get('name', 'value')} must be a boolean")


class ParseFloatPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a float") from exc


class ParseUUIDPipe:
    def transform(self, value: Any, metadata: dict[str, Any]) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise BadRequestException(f"{metadata.get('name', 'value')} must be a UUID") from exc


class ParseEnumPipe:
    def __init__(self, enum: type[Enum]):
        self.enum = enum

    def transform(self, value: Any, metadata: dict[str, Any]) -> Enum:
        try:
            return self.enum(value)
        except ValueError as exc:
            allowed = ", ".join(str(item.value) for item in self.enum)
            raise BadRequestException(
                f"{metadata.get('name', 'value')} must be one of: {allowed}"
            ) from exc


class ParseArrayPipe:
    def __init__(self, separator: str = ","):
        self.separator = separator

    def transform(self, value: Any, metadata: dict[str, Any]) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return [item for item in value.split(self.separator) if item != ""]
        raise BadRequestException(f"{metadata.get('name', 'value')} must be an array")


class DefaultValuePipe:
    def __init__(self, default: Any):
        self.default = default

    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        if value is None:
            return self.default
        return value


class FileValidator:
    def is_valid(self, file: Any) -> bool:
        raise NotImplementedError

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} failed validation"


class MaxFileSizeValidator(FileValidator):
    def __init__(self, max_size: int):
        self.max_size = max_size

    def is_valid(self, file: Any) -> bool:
        size = getattr(file, "size", None)
        return size is None or int(size) <= self.max_size

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} exceeds {self.max_size} bytes"


class FileTypeValidator(FileValidator):
    def __init__(self, file_type: str | re.Pattern[str] | Callable[[Any], bool]):
        self.file_type = file_type

    def is_valid(self, file: Any) -> bool:
        if callable(self.file_type):
            return bool(self.file_type(file))
        content_type = getattr(file, "content_type", None) or ""
        filename = getattr(file, "filename", None) or ""
        target = f"{content_type} {filename}"
        if isinstance(self.file_type, re.Pattern):
            return bool(self.file_type.search(target))
        return content_type == self.file_type or filename.endswith(self.file_type)

    def build_error_message(self, file: Any) -> str:
        return f"{getattr(file, 'filename', 'file')} has an invalid file type"


class ParseFilePipe:
    def __init__(
        self,
        validators: Iterable[FileValidator] | None = None,
        *,
        file_is_required: bool = True,
    ) -> None:
        self.validators = list(validators or [])
        self.file_is_required = file_is_required

    def transform(self, value: Any, metadata: dict[str, Any]) -> Any:
        if value is None:
            if self.file_is_required:
                raise BadRequestException(f"{metadata.get('name', 'file')} is required")
            return value
        files = value if isinstance(value, list) else [value]
        for file in files:
            for validator in self.validators:
                if not validator.is_valid(file):
                    raise BadRequestException(validator.build_error_message(file))
        return value
