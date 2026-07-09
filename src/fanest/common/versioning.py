from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


VERSION_NEUTRAL = "neutral"


class VersioningType(str, Enum):
    URI = "uri"
    HEADER = "header"
    MEDIA_TYPE = "media_type"
    CUSTOM = "custom"


@dataclass(frozen=True)
class VersioningOptions:
    type: VersioningType = VersioningType.URI
    default_version: str | None = None
    prefix: str = "v"
    header: str = "X-Version"
    key: str = "v"
    extractor: Callable[[Any], str | list[str] | tuple[str, ...] | None] | None = None


def normalize_versioning_options(
    options: VersioningOptions | dict[str, Any] | bool | None,
) -> VersioningOptions | None:
    if options is None or options is False:
        return None
    if options is True:
        return VersioningOptions()
    if isinstance(options, VersioningOptions):
        return options
    values = dict(options)
    if "type" in values:
        values["type"] = VersioningType(values["type"])
    if "defaultVersion" in values and "default_version" not in values:
        values["default_version"] = values.pop("defaultVersion")
    return VersioningOptions(**values)
