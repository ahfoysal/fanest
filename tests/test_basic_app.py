from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Injectable, Module


@Injectable()
class HelloService:
    def hello(self):
        return {"hello": "fanest"}


@Controller("hello")
class HelloController:
    def __init__(self, hello_service: HelloService):
        self.hello_service = hello_service

    @Get("/")
    async def index(self):
        return self.hello_service.hello()


@Module(controllers=[HelloController], providers=[HelloService])
class HelloModule:
    pass


def test_registers_controller_route_with_constructor_injection():
    app = FaNestFactory.create(HelloModule)
    client = TestClient(app)

    response = client.get("/hello")

    assert response.status_code == 200
    assert response.json() == {"hello": "fanest"}
