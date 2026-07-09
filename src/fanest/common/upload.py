import inspect
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fanest.common.exceptions import BadRequestException, PayloadTooLargeException
from fanest.core.metadata import ExecutionContext, ParameterSource


FileFilter = Callable[..., Any]
FileNameFactory = Callable[..., str]
DestinationFactory = Callable[..., str | Path]


@dataclass(frozen=True)
class DiskStorage:
    destination: str | Path | DestinationFactory
    filename: str | FileNameFactory | None = None


def disk_storage(
    *,
    destination: str | Path | DestinationFactory,
    filename: str | FileNameFactory | None = None,
) -> DiskStorage:
    return DiskStorage(destination=destination, filename=filename)


def memory_storage() -> None:
    return None


@dataclass(frozen=True)
class FileUploadOptions:
    limits: dict[str, int] | None = None
    file_filter: FileFilter | None = None
    storage: DiskStorage | None = None


@dataclass(frozen=True)
class UploadField:
    name: str
    max_count: int | None = None


class FileInterceptor:
    def __init__(
        self,
        field_name: str = "file",
        options: FileUploadOptions | dict[str, Any] | None = None,
        *,
        limits: dict[str, int] | None = None,
        file_filter: FileFilter | None = None,
        storage: DiskStorage | None = None,
    ) -> None:
        self.field_name = field_name
        self.options = _normalize_options(
            options,
            limits=limits,
            file_filter=file_filter,
            storage=storage,
        )

    async def intercept(self, context: ExecutionContext, call_next: Callable[[], Any]) -> Any:
        for upload in _iter_uploads(context, self.field_name, many=False):
            await _process_upload(upload, context, self.options)
        return await call_next()


class FilesInterceptor:
    def __init__(
        self,
        field_name: str = "files",
        max_count: int | None = None,
        options: FileUploadOptions | dict[str, Any] | None = None,
        *,
        limits: dict[str, int] | None = None,
        file_filter: FileFilter | None = None,
        storage: DiskStorage | None = None,
    ) -> None:
        self.field_name = field_name
        self.max_count = _normalize_max_count(max_count)
        self.options = _normalize_options(
            options,
            limits=limits,
            file_filter=file_filter,
            storage=storage,
        )

    async def intercept(self, context: ExecutionContext, call_next: Callable[[], Any]) -> Any:
        uploads = list(_iter_uploads(context, self.field_name, many=True))
        if self.max_count is not None and len(uploads) > self.max_count:
            raise BadRequestException(f"{self.field_name} accepts at most {self.max_count} files")
        for upload in uploads:
            await _process_upload(upload, context, self.options)
        return await call_next()


class FileFieldsInterceptor:
    def __init__(
        self,
        upload_fields: list[UploadField | dict[str, Any]],
        options: FileUploadOptions | dict[str, Any] | None = None,
        *,
        limits: dict[str, int] | None = None,
        file_filter: FileFilter | None = None,
        storage: DiskStorage | None = None,
    ) -> None:
        self.upload_fields = [_normalize_upload_field(field) for field in upload_fields]
        self.options = _normalize_options(
            options,
            limits=limits,
            file_filter=file_filter,
            storage=storage,
        )

    async def intercept(self, context: ExecutionContext, call_next: Callable[[], Any]) -> Any:
        for field in self.upload_fields:
            uploads = list(_iter_uploads(context, field.name, many=True))
            if field.max_count is not None and len(uploads) > field.max_count:
                raise BadRequestException(f"{field.name} accepts at most {field.max_count} files")
            for upload in uploads:
                await _process_upload(upload, context, self.options)
        return await call_next()


class AnyFilesInterceptor:
    def __init__(
        self,
        options: FileUploadOptions | dict[str, Any] | None = None,
        *,
        limits: dict[str, int] | None = None,
        file_filter: FileFilter | None = None,
        storage: DiskStorage | None = None,
        max_count: int | None = None,
    ) -> None:
        self.max_count = _normalize_max_count(max_count)
        self.options = _normalize_options(
            options,
            limits=limits,
            file_filter=file_filter,
            storage=storage,
        )

    async def intercept(self, context: ExecutionContext, call_next: Callable[[], Any]) -> Any:
        uploads = _all_uploads(context)
        if self.max_count is not None and len(uploads) > self.max_count:
            raise BadRequestException(f"request accepts at most {self.max_count} files")
        for upload in uploads:
            await _process_upload(upload, context, self.options)
        return await call_next()


def _normalize_options(
    options: FileUploadOptions | dict[str, Any] | None,
    *,
    limits: dict[str, int] | None,
    file_filter: FileFilter | None,
    storage: DiskStorage | None,
) -> FileUploadOptions:
    if isinstance(options, FileUploadOptions):
        base = options
    else:
        data = dict(options or {})
        base = FileUploadOptions(
            limits=data.get("limits"),
            file_filter=data.get("file_filter") or data.get("fileFilter"),
            storage=data.get("storage"),
        )
    return FileUploadOptions(
        limits=_normalize_limits(limits if limits is not None else base.limits),
        file_filter=file_filter if file_filter is not None else base.file_filter,
        storage=storage if storage is not None else base.storage,
    )


def _normalize_limits(limits: dict[str, int] | None) -> dict[str, int] | None:
    if limits is None:
        return None
    normalized: dict[str, int] = {}
    for key, value in limits.items():
        try:
            int_value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Upload limit {key!r} must be an integer") from exc
        if int_value < 0:
            raise ValueError(f"Upload limit {key!r} must be non-negative")
        normalized[key] = int_value
    return normalized


def _normalize_max_count(max_count: Any) -> int | None:
    if max_count is None:
        return None
    try:
        value = int(max_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_count must be an integer") from exc
    if value < 0:
        raise ValueError("max_count must be non-negative")
    return value


def _normalize_upload_field(field: UploadField | dict[str, Any]) -> UploadField:
    if isinstance(field, UploadField):
        return field
    name = field.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("upload field requires a non-empty name")
    max_count = field.get("maxCount", field.get("max_count"))
    return UploadField(name=name, max_count=_normalize_max_count(max_count))


def _iter_uploads(context: ExecutionContext, field_name: str, *, many: bool) -> list[Any]:
    selected: list[Any] = []
    for name, parameter in inspect.signature(context.handler).parameters.items():
        source = parameter.default
        if not isinstance(source, ParameterSource):
            continue
        if source.source not in {"file", "files"}:
            continue
        if (source.name or name) != field_name:
            continue
        selected.extend(_as_upload_list(context.kwargs.get(name)))
        if not many:
            return selected[:1]
    if not selected and field_name in context.kwargs:
        selected.extend(_as_upload_list(context.kwargs[field_name]))
    return selected


def _as_upload_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item is not None]
    return [value]


def _all_uploads(context: ExecutionContext) -> list[Any]:
    uploads: list[Any] = []
    seen: set[int] = set()
    for name, parameter in inspect.signature(context.handler).parameters.items():
        source = parameter.default
        if not isinstance(source, ParameterSource) or source.source not in {"file", "files"}:
            continue
        for upload in _as_upload_list(context.kwargs.get(name)):
            marker = id(upload)
            if marker not in seen:
                uploads.append(upload)
                seen.add(marker)
    for value in context.kwargs.values():
        for upload in _as_upload_list(value):
            if not _looks_like_upload(upload):
                continue
            marker = id(upload)
            if marker not in seen:
                uploads.append(upload)
                seen.add(marker)
    return uploads


def _looks_like_upload(value: Any) -> bool:
    return hasattr(value, "filename") and hasattr(value, "file")


async def _process_upload(upload: Any, context: ExecutionContext, options: FileUploadOptions) -> None:
    max_size = (options.limits or {}).get("fileSize")
    if max_size is not None:
        size = _upload_size(upload)
        if size is not None and size > max_size:
            raise PayloadTooLargeException(
                f"{getattr(upload, 'filename', 'file')} exceeds {max_size} bytes"
            )
    if options.file_filter is not None:
        allowed = _call_upload_callback(options.file_filter, upload, context)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            raise BadRequestException(f"{getattr(upload, 'filename', 'file')} was rejected")
    if options.storage is not None:
        await _store_upload(upload, context, options.storage)


def _upload_size(upload: Any) -> int | None:
    size = getattr(upload, "size", None)
    if size is not None:
        return int(size)
    file = getattr(upload, "file", None)
    if file is None:
        return None
    try:
        position = file.tell()
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(position)
        return int(size)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


async def _store_upload(upload: Any, context: ExecutionContext, storage: DiskStorage) -> None:
    destination = await _resolve_destination(storage.destination, upload, context)
    if destination.exists() and not destination.is_dir():
        raise BadRequestException(f"Upload destination must be a directory: {destination}")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadRequestException(f"Upload destination cannot be created: {destination}") from exc
    filename = await _resolve_filename(storage.filename, upload, context)
    target = destination / filename
    file = getattr(upload, "file", None)
    if file is None:
        raise BadRequestException(f"{getattr(upload, 'filename', 'file')} cannot be stored")
    try:
        position = file.tell()
    except (AttributeError, OSError, TypeError, ValueError):
        position = None
    await upload.seek(0)
    try:
        with target.open("wb") as output:
            shutil.copyfileobj(file, output)
    except OSError as exc:
        raise BadRequestException(f"{getattr(upload, 'filename', 'file')} cannot be stored") from exc
    if position is not None:
        await upload.seek(position)
    setattr(upload, "stored_path", str(target))
    setattr(upload, "storage", {"destination": str(destination), "filename": filename, "path": str(target)})


async def _resolve_destination(
    destination: str | Path | DestinationFactory,
    upload: Any,
    context: ExecutionContext,
) -> Path:
    value = _call_upload_callback(destination, upload, context) if callable(destination) else destination
    if inspect.isawaitable(value):
        value = await value
    return Path(value)


async def _resolve_filename(
    filename: str | FileNameFactory | None,
    upload: Any,
    context: ExecutionContext,
) -> str:
    if callable(filename):
        value = _call_upload_callback(filename, upload, context)
        if inspect.isawaitable(value):
            value = await value
        return _safe_basename(str(value))
    if filename is not None:
        return _safe_basename(filename)
    original = getattr(upload, "filename", None) or "file"
    suffix = Path(original).suffix
    return f"{uuid4().hex}{suffix}"


def _safe_basename(filename: str) -> str:
    name = os.path.basename(filename)
    return name or f"{uuid4().hex}.upload"


def _call_upload_callback(callback: Callable[..., Any], upload: Any, context: ExecutionContext) -> Any:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(upload, context)
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
    ]
    has_varargs = any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if not has_varargs and len(parameters) <= 1:
        return callback(upload)
    return callback(upload, context)
