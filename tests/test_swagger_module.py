from fastapi import UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fanest import (
    Cookie,
    Controller,
    FaNestFactory,
    FilesInterceptor,
    Get,
    Header,
    Module,
    Param,
    Post,
    Query,
    UploadedFile,
    UploadedFiles,
    UseInterceptors,
)
from fanest.swagger import (
    ApiBearerAuth,
    ApiBasicAuth,
    ApiBadRequestResponse,
    ApiBody,
    ApiConsumes,
    ApiCookie,
    ApiCreatedResponse,
    ApiDefaultResponse,
    ApiExcludeEndpoint,
    ApiExcludeController,
    ApiExtension,
    ApiExtraModels,
    ApiHideProperty,
    ApiHeader,
    ApiInternalServerErrorResponse,
    ApiNotFoundResponse,
    ApiOkResponse,
    ApiOperation,
    ApiOAuth2,
    ApiParam,
    ApiProduces,
    ApiProperty,
    ApiPropertyOptional,
    ApiQuery,
    ApiResponse,
    ApiSchema,
    ApiSecurity,
    ApiTags,
    DocumentBuilder,
    SwaggerModule,
    all_of,
    any_of,
    get_schema_path,
    one_of,
)


class CreateDocDto(BaseModel):
    title: str = ApiProperty(description="Document title", example="Plan")
    draft: bool = ApiPropertyOptional(description="Draft flag")
    internal_note: str = ApiHideProperty()


class ErrorDto(BaseModel):
    message: str


class ValidationErrorDto(BaseModel):
    errors: list[str]


@ApiSchema(name="CatDto", description="A cat pet")
class Cat(BaseModel):
    kind: str = ApiProperty(enum=["cat"], example="cat")
    meows: bool


class Dog(BaseModel):
    kind: str = ApiProperty(enum=["dog"], example="dog")
    barks: bool


class PetEnvelope(BaseModel):
    pet: dict = ApiProperty(
        one_of=[Cat, Dog],
        discriminator={"propertyName": "kind"},
        description="Polymorphic pet payload",
    )
    aliases: list[str] = ApiProperty(type=str, is_array=True)


@ApiHeader("x-controller", "Controller header")
@ApiConsumes("application/json")
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
    @ApiCookie("session_id", "Session cookie")
    @Get("/{doc_id}")
    async def find_one(  # type: ignore[assignment]
        self,
        doc_id: str = Param(),
        verbose: bool = Query("verbose", default=False),
        request_id: str | None = Header("x-request-id", default=None),
        session_id: str | None = Cookie("session_id", default=None),
    ):
        return {"id": doc_id}

    @ApiBasicAuth()
    @ApiOAuth2(["docs:read"])
    @ApiCreatedResponse("Created")
    @ApiNotFoundResponse("Missing")
    @Get("/basic")
    async def basic(self):
        return {"ok": True}

    @ApiBody(
        "Create payload",
        type=CreateDocDto,
        required=True,
        examples={"sample": {"value": {"title": "Plan", "draft": False}}},
    )
    @ApiResponse(
        {
            "status": 202,
            "description": "Accepted with header",
            "headers": {"x-job-id": {"schema": {"type": "string"}}},
        }
    )
    @Post("/body")
    async def body(self):
        return {"ok": True}

    @ApiExtraModels(Cat, Dog, PetEnvelope)
    @ApiBody(schema=one_of(Cat, Dog, discriminator={"propertyName": "kind"}), required=True)
    @ApiOkResponse(
        "Pet envelope",
        model=PetEnvelope,
        headers={"x-pet": {"schema": {"type": "string"}}},
    )
    @ApiDefaultResponse("Default problem", schema=any_of(ErrorDto, ValidationErrorDto))
    @ApiInternalServerErrorResponse("Server problem", schema=all_of(ErrorDto))
    @Post("/pets")
    async def pets(self):
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

    @ApiConsumes("multipart/form-data")
    @Post("/uploads")
    @UseInterceptors(FilesInterceptor("photos", max_count=3))
    async def uploads(self, files=UploadedFiles("photos")):
        return {"count": len(files)}

    @Post("/implicit-upload")
    @UseInterceptors(FilesInterceptor("attachments", max_count=2))
    async def implicit_upload(self, attachments=UploadedFiles("attachments", default=None)):
        return {"count": len(attachments or [])}


@ApiExcludeController()
@Controller("hidden-docs")
class HiddenDocsController:
    @Get("/")
    async def index(self):
        return {"hidden": True}


@ApiExtraModels(ErrorDto)
@Module(controllers=[DocsController, HiddenDocsController])
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
        .add_cookie_auth("sid")
        .add_oauth2(
            authorization_url="https://auth.example.com/authorize",
            token_url="https://auth.example.com/token",
            scopes={"docs:read": "Read docs"},
        )
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
    assert [parameter["name"] for parameter in operation["parameters"]].count("x-request-id") == 1
    assert [parameter["name"] for parameter in operation["parameters"]].count("verbose") == 1
    assert [parameter["name"] for parameter in operation["parameters"]].count("session_id") == 1
    assert any(parameter["name"] == "x-controller" for parameter in operation["parameters"])
    assert "application/json" in operation["requestBody"]["content"]
    assert "application/json" in operation["responses"]["200"]["content"]
    assert document["components"]["securitySchemes"]["basic"]["scheme"] == "basic"
    assert document["components"]["securitySchemes"]["api_key"]["name"] == "x-api-key"
    assert document["components"]["securitySchemes"]["cookie"]["in"] == "cookie"
    assert document["components"]["securitySchemes"]["oauth2"]["type"] == "oauth2"
    assert client.get("/openapi.json").json()["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert "ErrorDto" in document["components"]["schemas"]
    assert "export class ApiClient" in client_source
    assert "fetch(`${this.baseUrl}/docs/{doc_id}`" in client_source
    basic_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/basic"]["get"]
    assert {"basic": []} in basic_operation["security"]
    assert {"oauth2": ["docs:read"]} in basic_operation["security"]
    assert basic_operation["responses"]["201"]["description"] == "Created"
    assert basic_operation["responses"]["404"]["description"] == "Missing"
    body_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/body"]["post"]
    assert body_operation["requestBody"]["description"] == "Create payload"
    assert body_operation["requestBody"]["required"] is True
    assert body_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CreateDocDto"
    }
    assert body_operation["responses"]["202"]["headers"]["x-job-id"]["schema"]["type"] == "string"
    assert "CreateDocDto" in document["components"]["schemas"]
    pets_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/pets"]["post"]
    pets_schema = pets_operation["requestBody"]["content"]["application/json"]["schema"]
    assert pets_schema["oneOf"] == [
        {"$ref": "#/components/schemas/CatDto"},
        {"$ref": "#/components/schemas/Dog"},
    ]
    assert pets_schema["discriminator"] == {"propertyName": "kind"}
    assert pets_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PetEnvelope"
    }
    assert pets_operation["responses"]["200"]["headers"]["x-pet"]["schema"]["type"] == "string"
    assert pets_operation["responses"]["default"]["content"]["application/json"]["schema"]["anyOf"] == [
        {"$ref": "#/components/schemas/ErrorDto"},
        {"$ref": "#/components/schemas/ValidationErrorDto"},
    ]
    assert pets_operation["responses"]["500"]["content"]["application/json"]["schema"]["allOf"] == [
        {"$ref": "#/components/schemas/ErrorDto"}
    ]
    assert document["components"]["schemas"]["CatDto"]["description"] == "A cat pet"
    assert get_schema_path(Cat) == "#/components/schemas/CatDto"
    pet_schema = document["components"]["schemas"]["PetEnvelope"]
    assert pet_schema["properties"]["pet"]["oneOf"] == [
        {"$ref": "#/components/schemas/CatDto"},
        {"$ref": "#/components/schemas/Dog"},
    ]
    assert pet_schema["properties"]["aliases"]["items"]["type"] == "string"
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
    uploads_operation = client.get("/api-docs/openapi.json").json()["paths"]["/docs/uploads"]["post"]
    uploads_schema_ref = uploads_operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    uploads_schema_name = uploads_schema_ref.rsplit("/", 1)[-1]
    uploads_schema = document["components"]["schemas"][uploads_schema_name]
    assert uploads_schema["required"] == ["photos"]
    assert uploads_schema["properties"]["photos"]["maxItems"] == 3
    assert uploads_schema["properties"]["photos"]["items"]["type"] == "string"
    assert uploads_schema["properties"]["photos"]["items"]["format"] == "binary"
    assert uploads_schema["properties"]["photos"]["items"]["contentMediaType"] == "application/octet-stream"
    implicit_operation = client.get("/api-docs/openapi.json").json()["paths"][
        "/docs/implicit-upload"
    ]["post"]
    implicit_schema = implicit_operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert implicit_schema["properties"]["attachments"]["maxItems"] == 2
    assert "required" not in implicit_schema
    assert "/docs/internal" not in client.get("/api-docs/openapi.json").json()["paths"]
    assert "/hidden-docs/" not in client.get("/api-docs/openapi.json").json()["paths"]
    assert CreateDocDto.model_json_schema()["properties"]["title"]["description"] == "Document title"
    assert CreateDocDto.model_json_schema()["properties"]["draft"]["description"] == "Draft flag"
    assert client.get("/api-docs").status_code == 200
    default_docs = client.get("/docs").text
    assert "/openapi.json" in default_docs
    assert client.get("/openapi.json").json() == client.get("/api-docs/openapi.json").json()


# --------------------------------------------------------------------------- #
# Round-2 swagger regressions
# --------------------------------------------------------------------------- #
def test_create_document_does_not_corrupt_across_configs():
    from fanest import Controller, FaNestFactory, Get, Module
    from fanest.swagger import DocumentBuilder, SwaggerModule

    @Controller("a")
    class A:
        @Get("/")
        async def read(self):
            return {}

    @Module(controllers=[A])
    class M:
        pass

    app = FaNestFactory.create(M)
    public = SwaggerModule.create_document(app, DocumentBuilder().set_title("Public API").build())
    admin = SwaggerModule.create_document(app, DocumentBuilder().set_title("Admin API").build())
    assert public is not admin
    assert public["info"]["title"] == "Public API"
    assert admin["info"]["title"] == "Admin API"


def test_add_security_requirements_are_registered():
    from fanest.swagger import DocumentBuilder

    config = (
        DocumentBuilder()
        .add_security("apiKey", {"type": "apiKey", "in": "header", "name": "X-Key"}, requirements=["read"])
        .build()
    )
    assert {"apiKey": ["read"]} in config["security"]


def test_generate_typescript_client_sanitizes_method_names():
    import re

    from fanest.swagger import SwaggerModule

    document = {
        "openapi": "3.0.0",
        "paths": {
            "/health-check": {"get": {"operationId": "find-pet"}},
            "/v1.2/items": {"get": {}},
        },
    }
    source = SwaggerModule.generate_typescript_client(document)
    methods = re.findall(r"async (\w+)\(", source)
    assert methods and all(name.isidentifier() for name in methods)


def test_reference_object_parameters_are_not_deduped_away():
    from fanest.swagger import SwaggerModule

    schema = {
        "openapi": "3.0.0",
        "paths": {
            "/x": {
                "get": {
                    "parameters": [
                        {"$ref": "#/components/parameters/TenantHeader"},
                        {"$ref": "#/components/parameters/TraceHeader"},
                    ]
                }
            }
        },
    }
    SwaggerModule._dedupe_operation_parameters(schema)
    refs = {p["$ref"] for p in schema["paths"]["/x"]["get"]["parameters"]}
    assert refs == {
        "#/components/parameters/TenantHeader",
        "#/components/parameters/TraceHeader",
    }


def test_int_enum_and_dict_schema_types():
    from enum import IntEnum

    from fanest.swagger.decorators import _schema_for_type

    class Priority(IntEnum):
        LOW = 1
        HIGH = 2

    assert _schema_for_type(Priority) == {"enum": [1, 2], "type": "integer"}
    assert _schema_for_type(dict[str, int]) == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }


def test_api_hide_property_hidden_from_component_schemas():
    from typing import Any, cast

    from pydantic import BaseModel

    from fanest import Body, Controller, FaNestFactory, Module, Post
    from fanest.swagger import ApiHideProperty, SwaggerModule

    class CreateDto(BaseModel):
        name: str
        internal_token: str = ApiHideProperty()

    @Controller("things")
    class T:
        @Post("/")
        async def create(self, dto: CreateDto = cast(Any, Body())):
            return {"ok": True}

    @Module(controllers=[T])
    class M:
        pass

    document = SwaggerModule.create_document(FaNestFactory.create(M))
    properties = document["components"]["schemas"]["CreateDto"]["properties"]
    assert "internal_token" not in properties
    assert all(not (isinstance(value, dict) and value.get("hidden")) for value in properties.values())
