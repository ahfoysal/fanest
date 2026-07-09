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
        owns_scope = self.container.current_request_instances() is None
        request_scope = self.container.begin_request() if owns_scope else None
        try:
            if strict:
                return await self.container.resolve_local_async(token, self.module_key)
            return await self.container.resolve_async(token, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(token) from exc
        finally:
            if owns_scope and request_scope is not None:
                self.container.end_request(request_scope)

    def resolve_sync(self, token: Any, strict: bool = False) -> Any:
        if strict and self.module_key is None:
            raise StrictLookupError()
        owns_scope = self.container.current_request_instances() is None
        request_scope = self.container.begin_request() if owns_scope else None
        try:
            if strict:
                return self.container.resolve_local(token, self.module_key)
            return self.container.resolve(token, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(token) from exc
        finally:
            if owns_scope and request_scope is not None:
                self.container.end_request(request_scope)

    async def create(self, cls: type) -> Any:
        try:
            return await self.container.instantiate_async(cls, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(exc.args[0] if exc.args else cls) from exc

    def create_sync(self, cls: type) -> Any:
        try:
            return self.container.instantiate(cls, module_key=self.module_key)
        except KeyError as exc:
            raise UnknownProviderError(exc.args[0] if exc.args else cls) from exc

    def has(self, token: Any, strict: bool = False) -> bool:
        if strict and self.module_key is None:
            raise StrictLookupError()
        return self.container.has_provider(token, module_key=self.module_key, strict=strict)

    def is_registered(self, token: Any, strict: bool = False) -> bool:
        return self.has(token, strict=strict)

    def introspect(self, token: Any) -> dict[str, Any]:
        return self.container.describe_provider(token, module_key=self.module_key)

    def provider_tokens(self, strict: bool = False) -> tuple[Any, ...]:
        if strict and self.module_key is None:
            raise StrictLookupError()
        return self.container.provider_tokens(self.module_key, strict=strict)
