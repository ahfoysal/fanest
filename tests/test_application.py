from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Query


class UpperPipe:
    def transform(self, value, metadata):
        if metadata["name"] == "name" and isinstance(value, str):
            return value.upper()
        return value


@Controller("hello")
class HelloController:
    @Get("/")
    async def hello(self, name: str = Query(default="world")):
        return {"hello": name}


@Module(controllers=[HelloController])
class HelloModule:
    pass


def test_application_wrapper_configures_global_options():
    app = (
        FaNestFactory.create_application(HelloModule, title="Wrapped")
        .set_global_prefix("api")
        .enable_cors()
        .use_global_pipes(UpperPipe())
        .build()
    )
    response = TestClient(app).get("/api/hello?name=ada")

    assert app.title == "Wrapped"
    assert response.json() == {"hello": "ADA"}
