import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from fanest import Inject, Injectable, Module, use_value
from fanest.core.metadata import ParameterSource
from fanest.core.providers import token
from fanest.core.providers import use_factory as provider_factory

I18N_OPTIONS = token("I18N_OPTIONS")


class I18nResolver(Protocol):
    def resolve(self, context: Any) -> str | None: ...


@dataclass(frozen=True)
class AcceptLanguageResolver:
    default: str | None = None

    def resolve(self, context: Any) -> str | None:
        header = context.request.headers.get("accept-language")
        if not header:
            return self.default
        return _preferred_language(header, self.default or "en")


@dataclass(frozen=True)
class HeaderResolver:
    header: str = "x-lang"

    def resolve(self, context: Any) -> str | None:
        return context.request.headers.get(self.header)


@dataclass(frozen=True)
class QueryResolver:
    parameter: str = "lang"

    def resolve(self, context: Any) -> str | None:
        return context.request.query_params.get(self.parameter)


@dataclass(frozen=True)
class CookieResolver:
    cookie: str = "lang"

    def resolve(self, context: Any) -> str | None:
        return context.request.cookies.get(self.cookie)


@dataclass(frozen=True)
class I18nOptions:
    translations: dict[str, dict[str, Any]]
    fallback_language: str = "en"
    fallbacks: dict[str, str] = field(default_factory=dict)
    resolvers: tuple[I18nResolver | Callable[[Any], str | None], ...] = field(default_factory=tuple)


def I18nLang(default: str = "en") -> Any:
    def factory(data: Any, context):
        resolvers = _configured_resolvers(context) or data.get("resolvers", (AcceptLanguageResolver(default),))
        for resolver in resolvers:
            language = _resolve_language(resolver, context)
            if language:
                return language
        return default

    return ParameterSource(source="custom", default={"factory": factory, "data": {"resolvers": (AcceptLanguageResolver(default),)}})


@Injectable()
class I18nService:
    def __init__(self, options: dict[str, Any] | I18nOptions = Inject(I18N_OPTIONS)):
        normalized = _normalize_options(options)
        self.translations = normalized.translations
        self.fallback_language = normalized.fallback_language
        self.fallbacks = normalized.fallbacks
        self.resolvers = normalized.resolvers

    def translate(
        self,
        key: str,
        *,
        lang: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> str:
        language = lang or self.fallback_language
        value = None
        for candidate in self._language_chain(language):
            value = self._lookup(candidate, key)
            if value is not None:
                break
        if value is None:
            value = key
        return self._interpolate(value, args or {})

    def resolve_language(self, context: Any, default: str | None = None) -> str:
        for resolver in self.resolvers:
            language = _resolve_language(resolver, context)
            if language:
                return language
        return default or self.fallback_language

    t = translate

    def _language_chain(self, language: str) -> list[str]:
        chain = [language]
        if language in self.fallbacks:
            chain.append(self.fallbacks[language])
        base_language = language.split("-", 1)[0]
        if base_language != language:
            chain.append(base_language)
        chain.append(self.fallback_language)
        return list(dict.fromkeys(candidate for candidate in chain if candidate))

    def _lookup(self, language: str, key: str) -> str | None:
        current: Any = self.translations.get(language, {})
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current if isinstance(current, str) else None

    def _interpolate(self, value: str, args: dict[str, Any]) -> str:
        rendered = value
        for name in _placeholders(value):
            replacement = _lookup_arg(args, name)
            if replacement is not None:
                pattern = r"\{\s*" + re.escape(name) + r"\s*\}"
                # Use a function replacement so backslashes / group-reference
                # sequences in the value ('C:\Users', r'a\1b', r'\g<0>') are
                # inserted literally instead of being interpreted by re.sub.
                text = str(replacement)
                rendered = re.sub(pattern, lambda _match, text=text: text, rendered)
        return rendered


def _preferred_language(header: str, default: str) -> str:
    candidates: list[tuple[float, int, str]] = []
    for index, raw_part in enumerate(header.split(",")):
        part = raw_part.strip()
        if not part:
            continue
        language, *params = [item.strip() for item in part.split(";")]
        quality = 1.0
        for param in params:
            if param.startswith("q="):
                try:
                    quality = float(param[2:])
                except ValueError:
                    quality = 0.0
        if language:
            candidates.append((quality, -index, language))
    if not candidates:
        return default
    return max(candidates)[2] or default


class I18nModule:
    @staticmethod
    def for_root(
        *,
        translations: dict[str, dict[str, Any]],
        fallback_language: str = "en",
        fallbacks: dict[str, str] | None = None,
        resolvers: list[I18nResolver | Callable[[Any], str | None]] | tuple[I18nResolver | Callable[[Any], str | None], ...] | None = None,
        is_global: bool = False,
    ) -> type:
        options = I18nOptions(
            translations=translations,
            fallback_language=fallback_language,
            fallbacks=fallbacks or {},
            resolvers=tuple(resolvers or (AcceptLanguageResolver(fallback_language),)),
        )

        @Module(
            providers=[
                use_value(
                    I18N_OPTIONS,
                    options,
                ),
                I18nService,
            ],
            exports=[I18nService],
            global_module=is_global,
        )
        class DynamicI18nModule:
            pass

        return DynamicI18nModule

    @staticmethod
    def for_root_async(
        *,
        use_factory: Callable[..., dict[str, Any] | I18nOptions | Awaitable[dict[str, Any] | I18nOptions]],
        inject: list[Any] | None = None,
        imports: list[Any] | None = None,
        is_global: bool = False,
    ) -> type:
        async def options_factory(*dependencies: Any) -> I18nOptions:
            result = use_factory(*dependencies)
            if inspect.isawaitable(result):
                result = await cast(Awaitable[Any], result)
            return _normalize_options(result)

        @Module(
            imports=imports or [],
            providers=[
                provider_factory(I18N_OPTIONS, options_factory, inject=inject or []),
                I18nService,
            ],
            exports=[I18nService],
            global_module=is_global,
        )
        class DynamicI18nModule:
            pass

        return DynamicI18nModule


def _normalize_options(options: dict[str, Any] | I18nOptions) -> I18nOptions:
    if isinstance(options, I18nOptions):
        return options
    return I18nOptions(
        translations=options.get("translations", {}),
        fallback_language=options.get("fallback_language", "en"),
        fallbacks=options.get("fallbacks", {}),
        resolvers=tuple(options.get("resolvers", (AcceptLanguageResolver(options.get("fallback_language", "en")),))),
    )


def _resolve_language(resolver: I18nResolver | Callable[[Any], str | None], context: Any) -> str | None:
    if hasattr(resolver, "resolve"):
        return cast(Any, resolver).resolve(context)
    return cast(Callable[[Any], str | None], resolver)(context)


def _configured_resolvers(context: Any) -> tuple[I18nResolver | Callable[[Any], str | None], ...] | None:
    try:
        container = context.request.app.state.fanest_container
        service = container.resolve(I18nService)
    except Exception:
        return None
    return service.resolvers


def _placeholders(value: str) -> list[str]:
    placeholders: list[str] = []
    start = 0
    while True:
        left = value.find("{", start)
        if left < 0:
            return placeholders
        right = value.find("}", left + 1)
        if right < 0:
            return placeholders
        name = value[left + 1 : right].strip()
        if name:
            placeholders.append(name)
        start = right + 1


def _lookup_arg(args: dict[str, Any], key: str) -> Any | None:
    current: Any = args
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
