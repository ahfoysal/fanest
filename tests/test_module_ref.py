import pytest

from fanest import FaNestFactory, Inject, Injectable, Module, ModuleRef, forward_ref, use_factory
from fanest.core.container import FaNestContainer, ForwardRefProxy
from fanest.core.module_ref import StrictLookupError, UnknownProviderError


@Injectable()
class RefService:
    def __init__(self, module_ref: ModuleRef):
        self.module_ref = module_ref

    def get_value(self):
        return self.module_ref.get(ValueService).value


@Injectable()
class LateService:
    def __init__(self, value: "ValueService" = Inject(forward_ref(lambda: ValueService))):
        self.value = value


@Injectable()
class ValueService:
    value = "ok"


@Module(providers=[RefService, LateService, ValueService])
class RefModule:
    pass


def test_module_ref_get_and_forward_ref_token():
    container = FaNestContainer()
    for provider in [RefService, LateService, ValueService]:
        container.register(provider)

    assert container.resolve(RefService).get_value() == "ok"
    assert container.resolve(LateService).value.value == "ok"
    assert isinstance(container.resolve(LateService).value, ForwardRefProxy)


@Injectable(scope="request")
class RequestScopedService:
    created = 0

    def __init__(self):
        type(self).created += 1


@Injectable(scope="transient")
class TransientScopedService:
    created = 0

    def __init__(self):
        type(self).created += 1


class ComposedService:
    def __init__(self, value: ValueService, request: RequestScopedService):
        self.value = value
        self.request = request


async def async_value_factory(value: ValueService):
    return {"value": value.value}


def test_module_ref_get_strict_false_introspection_and_errors():
    container = FaNestContainer()
    for provider in [ValueService, RequestScopedService]:
        container.register(provider)
    module_ref = container.resolve(ModuleRef)

    assert module_ref.get(ValueService, strict=False).value == "ok"
    assert module_ref.has(ValueService) is True
    assert module_ref.is_registered("missing") is False
    assert ValueService in module_ref.provider_tokens()

    info = module_ref.introspect(RequestScopedService)
    assert info["token"] is RequestScopedService
    assert info["scope"] == "request"
    assert info["type"] == "class"
    assert info["multi"] is False

    assert module_ref.get("missing", default="fallback") == "fallback"
    with pytest.raises(UnknownProviderError, match="No provider found"):
        module_ref.get("missing")
    with pytest.raises(StrictLookupError, match="Strict module-local lookup"):
        module_ref.get(ValueService, strict=True)


@Injectable()
class AppVisibleService:
    value = "visible"


@Module(providers=[AppVisibleService])
class AppVisibleModule:
    pass


def test_module_ref_introspection_sees_root_visible_module_providers():
    app = FaNestFactory.create(AppVisibleModule)
    module_ref = app.state.fanest_container.resolve(ModuleRef)

    assert module_ref.has(AppVisibleService) is True
    assert AppVisibleService in module_ref.provider_tokens()

    before = module_ref.introspect(AppVisibleService)
    instance = module_ref.get(AppVisibleService)
    after = module_ref.introspect(AppVisibleService)

    assert before["token"] is AppVisibleService
    assert before["resolved"] is False
    assert instance.value == "visible"
    assert after["resolved"] is True


@pytest.mark.anyio
async def test_module_ref_resolve_supports_async_factories_and_request_scope():
    container = FaNestContainer()
    container.register(ValueService)
    container.register(RequestScopedService)
    container.register(use_factory("async-value", async_value_factory, inject=[ValueService]))
    module_ref = container.resolve(ModuleRef)

    RequestScopedService.created = 0

    first = await module_ref.resolve(RequestScopedService)
    second = await module_ref.resolve(RequestScopedService)
    async_value = await module_ref.resolve("async-value")

    assert first is not second
    assert RequestScopedService.created == 2
    assert async_value == {"value": "ok"}


@pytest.mark.anyio
async def test_module_ref_create_and_resolve_sync_handle_scoped_dependencies():
    container = FaNestContainer()
    for provider in [ValueService, RequestScopedService, TransientScopedService]:
        container.register(provider)
    module_ref = container.resolve(ModuleRef)

    RequestScopedService.created = 0
    TransientScopedService.created = 0

    created = await module_ref.create(ComposedService)
    first_transient = module_ref.resolve_sync(TransientScopedService)
    second_transient = module_ref.resolve_sync(TransientScopedService)

    assert created.value.value == "ok"
    assert isinstance(created.request, RequestScopedService)
    assert first_transient is not second_transient
    assert TransientScopedService.created == 2


@Injectable()
class CircularA:
    def __init__(self, b: "CircularB"):
        self.b = b

    def name(self):
        return "a"

    def b_name(self):
        return self.b.name()


@Injectable()
class CircularB:
    def __init__(self, a: CircularA = Inject(forward_ref(lambda: CircularA))):
        self.a = a

    def name(self):
        return "b"

    def a_name(self):
        return self.a.name()


def test_forward_ref_constructor_cycle_uses_lazy_proxy():
    container = FaNestContainer()
    container.register(CircularA)
    container.register(CircularB)

    instance = container.resolve(CircularA)

    assert instance.b_name() == "b"
    assert isinstance(instance.b.a, ForwardRefProxy)
    assert instance.b.a_name() == "a"


class UnsafeCircularA:
    def __init__(self, b: "UnsafeCircularB"):
        self.b = b


class UnsafeCircularB:
    def __init__(self, a: UnsafeCircularA):
        self.a = a


def test_plain_constructor_cycle_still_fails_clearly():
    container = FaNestContainer()
    container.register(UnsafeCircularA)
    container.register(UnsafeCircularB)

    with pytest.raises(RuntimeError, match="Circular dependency detected"):
        container.resolve(UnsafeCircularA)
