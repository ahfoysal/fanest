from fanest import Inject, Injectable, Module, ModuleRef, forward_ref
from fanest.core.container import FaNestContainer


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
