from collections.abc import AsyncIterable, Iterable
from pathlib import Path
from typing import Any, BinaryIO

from fastapi.responses import FileResponse, StreamingResponse


class StreamableFile:
    def __init__(
        self,
        content: bytes | Iterable[bytes] | AsyncIterable[bytes] | BinaryIO,
        *,
        content_type: str = "application/octet-stream",
        filename: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.content_type = content_type
        self.filename = filename
        self.headers = headers or {}
        self.path: Path | None = None

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        content_type: str = "application/octet-stream",
        filename: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> "StreamableFile":
        stream = cls(b"", content_type=content_type, filename=filename, headers=headers)
        stream.path = Path(path)
        return stream

    def to_response(self, headers: dict[str, str] | None = None):
        merged_headers = {**(headers or {}), **self.headers}
        if self.path is not None:
            return FileResponse(
                self.path,
                media_type=self.content_type,
                filename=self.filename,
                headers=merged_headers,
            )
        headers = merged_headers
        if self.filename:
            headers.setdefault("content-disposition", f'attachment; filename="{self.filename}"')
        return StreamingResponse(
            self._body(),
            media_type=self.content_type,
            headers=headers,
        )

    def _body(self) -> Any:
        if isinstance(self.content, bytes):
            return iter([self.content])
        return self.content
