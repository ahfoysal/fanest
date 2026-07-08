import pytest

from fanest import Controller, FaNestFactory, Get, Module
from fanest.auth import AuthModule, JwtService


@Controller("token")
class TokenController:
    def __init__(self, jwt_service: JwtService):
        self.jwt_service = jwt_service

    @Get("/")
    async def token(self):
        return {"token": self.jwt_service.sign({"sub": "1"})}


@Module(imports=[AuthModule.for_root(secret="first-secret-value-with-enough-entropy")], controllers=[TokenController])
class FirstAuthModule:
    pass


@Module(imports=[AuthModule.for_root(secret="second-secret-value-with-enough-entropy")], controllers=[TokenController])
class SecondAuthModule:
    pass


def test_dynamic_module_options_do_not_clobber_other_apps():
    first = FaNestFactory.create(FirstAuthModule)
    second = FaNestFactory.create(SecondAuthModule)

    first_jwt = first.state.fanest_container.resolve(JwtService)
    second_jwt = second.state.fanest_container.resolve(JwtService)

    token = first_jwt.sign({"sub": "123"})

    assert first_jwt.verify(token)["sub"] == "123"
    with pytest.raises(Exception):
        second_jwt.verify(token)
