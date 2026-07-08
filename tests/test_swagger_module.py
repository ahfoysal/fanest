from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import Controller, FaNestFactory, Get, Module, Param
from fanest.swagger import (
    ApiBearerAuth,
    ApiBasicAuth,
    ApiConsumes,
    ApiCreatedResponse,
    ApiExcludeEndpoint,
    ApiExtension,
    ApiExtraModels,
    ApiHideProperty,
    ApiHeader,
    ApiNotFoundResponse,
    ApiOperation,
    ApiParam,
    ApiProduces,
    ApiProperty,
    ApiPropertyOptional,
    ApiQuery,
    ApiSecurity,
    ApiTags,
    DocumentBuilder,
    SwaggerModule,
)


class CreateDocDto(BaseModel):
    title: str = ApiProperty(description="Document title", example="Plan")
    draft: bool = ApiPropertyOptional(description="Draft flag")
    internal_note: str = ApiHideProperty()


class ErrorDto(BaseModel):
    message: str


@ApiBearerAuth()
@ApiTags("docs")
@Controller("docs")
class DocsController:
    @ApiExcludeEndpoint()
    @Get("/internal")
    async def internal(self):
        return {"hidden": True}

    @ApiHeader("x-request-id", "Request id")
    @ApiExtension("rate-limit", {"bucket": "docs"})
    @ApiSecurity("api_key")
    @ApiConsumes("application/json")
    @ApiProduces("application/json")
    @ApiOperation(summary="Find a document", description="Returns one document")
    @ApiParam("doc_id", "Document id")
    @ApiQuery("verbose", "Verbose response")
    @Get("/{doc_id}")
    async def find_one(self, doc_id: str = Param()):
        return {"id": doc_id}

    @ApiBasicAuth()
    @ApiCreatedResponse("Created")
    @ApiNotFoundResponse("Missing")
    @Get("/basic")
    async def basic(self):
        return {"ok": True}


@ApiExtraModels(ErrorDto)
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
        .add_basic_auth()
        .add_api_key()
        .add_tag("docs", "Documentation")
        .build()
    )
    document = SwaggerModule.create_document(app, config)
    client_source = SwaggerModule.generate_typescript_client(document)
    SwaggerModule.setup("/api-docs", app, document)

    client = TestClient(app)
    operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/{doc_id}"]["get"]

    assert operation["summary"] == "Find a document"
    assert operation["x-rate-limit"] == {"bucket": "docs"}
    assert operation["tags"] == ["docs"]
    assert operation["security"] == [{"bearer": []}, {"api_key": []}]
    assert any(parameter["name"] == "x-request-id" for parameter in operation["parameters"])
    assert "application/json" in operation["requestBody"]["content"]
    assert "application/json" in operation["responses"]["200"]["content"]
    assert document["components"]["securitySchemes"]["basic"]["scheme"] == "basic"
    assert document["components"]["securitySchemes"]["api_key"]["name"] == "x-api-key"
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert "ErrorDto" in document["components"]["schemas"]
    assert "export class ApiClient" in client_source
    assert "fetch(`${this.baseUrl}/docs/{doc_id}`" in client_source
    basic_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/basic"]["get"]
    assert {"basic": []} in basic_operation["security"]
    assert basic_operation["responses"]["201"]["description"] == "Created"
    assert basic_operation["responses"]["404"]["description"] == "Missing"
    assert "/docs/internal" not in client.get("/api-docs/openapi.json").json()["paths"]
    assert CreateDocDto.model_json_schema()["properties"]["title"]["description"] == "Document title"
    assert CreateDocDto.model_json_schema()["properties"]["draft"]["description"] == "Draft flag"
    assert client.get("/api-docs").status_code == 200
