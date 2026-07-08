from typing import Any

from fanest import Inject, Injectable, Module, use_value
from fanest.core.metadata import ParameterSource
from fanest.core.providers import token

I18N_OPTIONS = token("I18N_OPTIONS")


def I18nLang(default: str = "en") -> ParameterSource:
    def factory(data: Any, context):
        header = context.request.headers.get("accept-language")
        if not header:
            return default
        return header.split(",", 1)[0].split(";", 1)[0].strip() or default

    return ParameterSource(source="custom", default={"factory": factory, "data": None})


@Injectable()
class I18nService:
    def __init__(self, options: dict[str, Any] = Inject(I18N_OPTIONS)):
        self.translations = options.get("translations", {})
        self.fallback_language = options.get("fallback_language", "en")

    def translate(
        self,
        key: str,
        *,
        lang: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> str:
        language = lang or self.fallback_language
        value = self.translations.get(language, {}).get(
            key,
            self.translations.get(self.fallback_language, {}).get(key, key),
        )
        for name, replacement in (args or {}).items():
            value = value.replace("{" + name + "}", str(replacement))
        return value

    t = translate


class I18nModule:
    @staticmethod
    def for_root(
        *,
        translations: dict[str, dict[str, str]],
        fallback_language: str = "en",
        is_global: bool = False,
    ) -> type:
        @Module(
            providers=[
                use_value(
                    I18N_OPTIONS,
                    {"translations": translations, "fallback_language": fallback_language},
                ),
                I18nService,
            ],
            exports=[I18nService],
            global_module=is_global,
        )
        class DynamicI18nModule:
            pass

        return DynamicI18nModule
