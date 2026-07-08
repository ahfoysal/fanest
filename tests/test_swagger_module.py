from fastapi.testclient import TestClient

from fanest import Controller, FaNestFactory, Get, Module, Param
from fanest.swagger import ApiOperation, ApiParam, ApiQuery, ApiTags, DocumentBuilder, SwaggerModule


@ApiTags("docs")
@Controller("docs")
class DocsController:
    @ApiOperation(summary="Find a document", description="Returns one document")
    @ApiParam("doc_id", "Document id")
    @ApiQuery("verbose", "Verbose response")
    @Get("/{doc_id}")
    async def find_one(self, doc_id: str = Param()):
        return {"id": doc_id}


@Module(controllers=[DocsController])
class DocsModule:
    pass


def test_swagger_decorators_and_module_setup():
    app = FaNestFactory.create(DocsModule)
    config = (
        DocumentBuilder()
        .set_title("Docs API")
        .set_version("2.0.0")
        .add_bearer_auth()
        .add_tag("docs", "Documentation")
        .build()
    )
    document = SwaggerModule.create_document(app, config)
    SwaggerModule.setup("/api-docs", app, document)

    client = TestClient(app)
    operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/{doc_id}"]["get"]

    assert operation["summary"] == "Find a document"
    assert operation["tags"] == ["docs"]
    assert client.get("/api-docs").status_code == 200
