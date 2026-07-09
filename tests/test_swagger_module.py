from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import Controller, FaNestFactory, Get, Module, Param, Post, UploadedFile
from fanest.swagger import (
    ApiBearerAuth,
    ApiBasicAuth,
    ApiBadRequestResponse,
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


class ValidationErrorDto(BaseModel):
    errors: list[str]


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
    async def find_one(self, doc_id: str = Param()):  # type: ignore[assignment]
        return {"id": doc_id}

    @ApiBasicAuth()
    @ApiCreatedResponse("Created")
    @ApiNotFoundResponse("Missing")
    @Get("/basic")
    async def basic(self):
        return {"ok": True}

    @Get("/mixed")
    @ApiCreatedResponse("Created below route")
    @ApiBadRequestResponse("Bad request below route", ValidationErrorDto)
    async def mixed_order(self):
        return {"ok": True}

    @ApiCreatedResponse("Created above route")
    @ApiBadRequestResponse("Bad request above route", ValidationErrorDto)
    @Get("/stacked")
    async def stacked_order(self):
        return {"ok": True}

    @ApiBearerAuth()
    @ApiConsumes("multipart/form-data")
    @Post("/upload")
    async def upload(self, file: UploadFile = UploadedFile("avatar")):  # type: ignore[assignment]
        return {"filename": file.filename}


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
    mixed_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/mixed"]["get"]
    assert mixed_operation["responses"]["201"]["description"] == "Created below route"
    assert mixed_operation["responses"]["400"]["description"] == "Bad request below route"
    assert "ValidationErrorDto" in str(mixed_operation["responses"]["400"])
    stacked_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/stacked"]["get"]
    assert stacked_operation["responses"]["201"]["description"] == "Created above route"
    assert stacked_operation["responses"]["400"]["description"] == "Bad request above route"
    upload_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/upload"]["post"]
    assert {"bearer": []} in upload_operation["security"]
    assert "multipart/form-data" in upload_operation["requestBody"]["content"]
    upload_schema_ref = upload_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    upload_schema_name = upload_schema_ref.rsplit("/", 1)[-1]
    upload_schema = document["components"]["schemas"][upload_schema_name]
    assert upload_schema["required"] == ["avatar"]
    assert upload_schema["properties"]["avatar"]["type"] == "string"
    assert upload_schema["properties"]["avatar"]["contentMediaType"] == "application/octet-stream"
    assert "/docs/internal" not in client.get("/api-docs/openapi.json").json()["paths"]
    assert CreateDocDto.model_json_schema()["properties"]["title"]["description"] == "Document title"
    assert CreateDocDto.model_json_schema()["properties"]["draft"]["description"] == "Draft flag"
    assert client.get("/api-docs").status_code == 200
    default_docs = client.get("/docs").text
    assert "/openapi.json" in default_docs
    assert client.get("/openapi.json").json() == client.get("/api-docs/openapi.json").json()
