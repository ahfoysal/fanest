import importlib
import pkgutil

import fanest


def test_package_all_exports_resolve():
    packages = [
        fanest,
        *(importlib.import_module(module.name) for module in pkgutil.iter_modules(fanest.__path__, "fanest.")),
    ]

    for package in packages:
        for name in getattr(package, "__all__", ()):
            assert hasattr(package, name), f"{package.__name__}.{name} is listed in __all__ but missing"


def test_common_and_core_subpackages_expose_root_common_apis():
    from fanest.common import (
        ConnectedSocket,
        GatewayTimeoutException,
        Headers,
        HostParam,
        MessageBody,
        MethodNotAllowedException,
        NotImplementedException,
        RequestTimeoutException,
        UnsupportedMediaTypeException,
    )
    from fanest.core import DiscoveryService, DiscoveredProvider, Global, LazyModuleLoader, Reflector
    from fanest.testing import OverrideBuilder, TestingModule

    assert ConnectedSocket is fanest.ConnectedSocket
    assert Headers is fanest.Headers
    assert HostParam is fanest.HostParam
    assert MessageBody is fanest.MessageBody
    assert GatewayTimeoutException is fanest.GatewayTimeoutException
    assert MethodNotAllowedException is fanest.MethodNotAllowedException
    assert NotImplementedException is fanest.NotImplementedException
    assert RequestTimeoutException is fanest.RequestTimeoutException
    assert UnsupportedMediaTypeException is fanest.UnsupportedMediaTypeException
    assert DiscoveryService is fanest.DiscoveryService
    assert DiscoveredProvider is fanest.DiscoveredProvider
    assert Global is fanest.Global
    assert LazyModuleLoader is fanest.LazyModuleLoader
    assert Reflector is fanest.Reflector
    assert TestingModule.create(object).override(object).__class__ is OverrideBuilder
