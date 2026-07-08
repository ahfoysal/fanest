from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fanest.core.container import FaNestContainer


_UNSET = object()


class ModuleRefError(Exception):
    """Base error for ModuleRef lookup and instantiation failures."""


class UnknownProviderError(ModuleRefError, LookupError):
    def __init__(self, token: Any):
        self.token = token
        super().__init__(f"No provider found for token {token!r}.")


class StrictLookupError(ModuleRefError):
    def __init__(self) -> None:
        super().__init__(
            "Strict module-local lookup is not available for this ModuleRef. "
            "Use strict=False to search the application container."
        )


class ModuleRef:
    def __init__(self, container: "FaNestContainer", module_key: Any | None = None):
        self.container = container
        self.module_key = module_key

    def get(self, token: Any, strict: bool = False, default: Any = _UNSET) -> Any:
        if strict and self.module_key is None:
            raise StrictLookupError()
        try:
            if strict:
                return self.container.resolve_local(token, self.module_key)
            return self.container.resolve(token, module_key=self.module_key)
        except KeyError as exc:
            if default is not _UNSET:
                return default
            raise UnknownProviderError(token) from exc

    async def resolve(self, token: Any, strict: bool = False) -> Any:
        if strict and self.module_key is None:
            raise StrictLookupError()
        request_scope = self.container.begin_request()
        try:
            if strict:
                return await self.container.resolve_local_async(token, self.module_key)
            return await self.container.resolve_async(token, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(token) from exc
        finally:
            self.container.end_request(request_scope)

    def resolve_sync(self, token: Any, strict: bool = False) -> Any:
        if strict and self.module_key is None:
            raise StrictLookupError()
        request_scope = self.container.begin_request()
        try:
            if strict:
                return self.container.resolve_local(token, self.module_key)
            return self.container.resolve(token, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(token) from exc
        finally:
            self.container.end_request(request_scope)

    async def create(self, cls: type) -> Any:
        try:
            return await self.container.instantiate_async(cls)
        except KeyError as exc:
            raise UnknownProviderError(exc.args[0] if exc.args else cls) from exc

    def create_sync(self, cls: type) -> Any:
        try:
            return self.container.instantiate(cls)
        except KeyError as exc:
            raise UnknownProviderError(exc.args[0] if exc.args else cls) from exc

    def has(self, token: Any) -> bool:
        return self.container.has_provider(token)

    def is_registered(self, token: Any) -> bool:
        return self.has(token)

    def introspect(self, token: Any) -> dict[str, Any]:
        return self.container.describe_provider(token)

    def provider_tokens(self) -> tuple[Any, ...]:
        return self.container.provider_tokens()
