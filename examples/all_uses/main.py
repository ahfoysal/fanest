from pydantic import BaseModel, EmailStr

from fanest import (
    Body,
    Cookie,
    Controller,
    FaNestFactory,
    Get,
    Inject,
    Injectable,
    MaxFileSizeValidator,
    Module,
    NotFoundException,
    Param,
    ParseFilePipe,
    Post,
    Query,
    Req,
    SetHeader,
    SubscribeMessage,
    Sse,
    StreamableFile,
    UseFilters,
    UseGuards,
    UseInterceptors,
    UsePipes,
    UploadedFile,
    ValidationPipe,
    WebSocketGateway,
    use_value,
)
from fanest.auth import AuthModule, CurrentUser, JwtAuthGuard, JwtService, Roles, RolesGuard
from fanest.cache import CacheInterceptor, CacheModule, CacheTTL
from fanest.config import ConfigModule, ConfigService
from fanest.health import HealthModule
from fanest.schedule import Cron, Interval, SchedulerRegistry, Timeout
from fanest.sqlalchemy import SqlAlchemyModule
from fanest.swagger import (
    ApiBearerAuth,
    ApiConsumes,
    ApiHeader,
    ApiOperation,
    ApiProperty,
    ApiTags,
    DocumentBuilder,
    SwaggerModule,
)
from fanest.throttler import Throttle, ThrottlerGuard, ThrottlerModule
from fanest.websockets import WebSocketManager

APP_NAME = "APP_NAME"


class CreateUserDto(BaseModel):
    email: EmailStr = ApiProperty(description="User email", example="ada@example.com")
    name: str = ApiProperty(description="Display name", example="Ada")


@Injectable()
class UsersService:
    def __init__(
        self,
        config: ConfigService,
        jwt_service: JwtService,
        app_name: str = Inject(APP_NAME),
    ):
        self.config = config
        self.jwt_service = jwt_service
        self.app_name = app_name
        self.users = [{"id": 1, "email": "ada@example.com", "name": "Ada", "roles": ["admin"]}]

    def find_all(self, search: str | None = None):
        if not search:
            return self.users
        return [user for user in self.users if search.lower() in user["name"].lower()]

    def find_one(self, user_id: int):
        for user in self.users:
            if user["id"] == user_id:
                return user
        raise NotFoundException("User not found")

    def create(self, dto: CreateUserDto):
        user = {"id": len(self.users) + 1, "email": dto.email, "name": dto.name, "roles": ["user"]}
        self.users.append(user)
        return user

    def login(self):
        return {
            "access_token": self.jwt_service.sign({"sub": "1", "roles": ["admin"]}),
            "token_type": "bearer",
        }

    def info(self):
        return {
            "app": self.app_name,
            "env": self.config.get("APP_ENV", "local"),
            "users": len(self.users),
        }


class HttpErrorFilter:
    def catch(self, exc, context):
        raise exc


@ApiTags("users")
@UseFilters(HttpErrorFilter)
@Controller("users")
class UsersController:
    def __init__(self, users_service: UsersService):
        self.users_service = users_service

    @ApiOperation(summary="List users")
    @UseInterceptors(CacheInterceptor)
    @CacheTTL(30)
    @Get("/")
    async def find_all(self, search: str | None = Query(default=None)):
        return self.users_service.find_all(search)

    @ApiOperation(summary="Create a user")
    @ApiHeader("x-request-id", "Optional request id")
    @UsePipes(ValidationPipe())
    @Post("/")
    async def create(self, dto: CreateUserDto = Body()):
        return self.users_service.create(dto)

    @Get("/{user_id}")
    async def find_one(self, user_id: int = Param()):
        return self.users_service.find_one(user_id)

    @ApiConsumes("multipart/form-data")
    @UsePipes(ParseFilePipe([MaxFileSizeValidator(1024 * 1024)]))
    @Post("/avatar")
    async def upload_avatar(self, file=UploadedFile()):
        return {"filename": file.filename}

    @SetHeader("x-report", "users")
    @Get("/report")
    async def report(self):
        return StreamableFile(b"id,name\n1,Ada\n", content_type="text/csv", filename="users.csv")

    @Sse("/events")
    async def events(self):
        yield {"event": "users.updated", "data": {"count": len(self.users_service.users)}}

    @Get("/session")
    async def session(self, session_id: str | None = Cookie("session_id")):
        return {"session_id": session_id}

    @Post("/login")
    async def login(self):
        return self.users_service.login()


@ApiBearerAuth()
@ApiTags("admin")
@Controller("admin")
@UseGuards(JwtAuthGuard, RolesGuard, ThrottlerGuard)
class AdminController:
    def __init__(self, users_service: UsersService):
        self.users_service = users_service

    @Roles("admin")
    @Throttle(limit=5, ttl=60)
    @Get("me")
    async def me(self, user: dict = CurrentUser(), req=Req()):
        return {"user": user, "client": req.client.host if req.client else None}

    @Roles("admin")
    @Get("info")
    async def info(self):
        return self.users_service.info()


@WebSocketGateway("/chat")
class ChatGateway:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    async def on_connect(self, websocket):
        self.manager.join("chat", websocket)

    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return {"echo": data}

    @SubscribeMessage("broadcast")
    async def broadcast(self, data, websocket):
        await self.manager.broadcast("chat", "message", data, exclude=websocket)
        return {"sent": True}


@Injectable()
class JobsService:
    def __init__(self, registry: SchedulerRegistry):
        self.registry = registry

    @Interval(30)
    async def heartbeat(self):
        return None

    @Cron("*/60 * * * * *")
    async def cleanup(self):
        return None

    @Timeout(5, name="startup-check")
    async def startup_check(self):
        return self.registry.get("startup-check").name


@Module(
    imports=[
        ConfigModule.for_root(env_file=".env"),
        AuthModule.for_root(secret="change-this-secret-before-production"),
        CacheModule.register(),
        ThrottlerModule.for_root(limit=20, ttl=60),
        SqlAlchemyModule.for_root(database_url="sqlite+aiosqlite:///./fanest-example.db"),
        HealthModule.register(),
    ],
    controllers=[UsersController, AdminController],
    providers=[UsersService, JobsService, use_value(APP_NAME, "FaNest All Uses")],
    gateways=[ChatGateway],
)
class AppModule:
    pass


app = FaNestFactory.create(
    AppModule,
    title="FaNest All Uses",
    description="A compact application showing the main FaNest framework features.",
    version="0.1.0",
    global_prefix="api",
    cors=True,
    global_pipes=[ValidationPipe()],
)

swagger_config = (
    DocumentBuilder()
    .set_title("FaNest All Uses")
    .set_description("REST, DI, auth, cache, throttling, cron jobs, WebSockets, and Swagger.")
    .set_version("0.1.0")
    .add_bearer_auth()
    .add_tag("users", "Public user routes")
    .add_tag("admin", "Protected admin routes")
    .build()
)
SwaggerModule.setup("/api/docs", app, SwaggerModule.create_document(app, swagger_config))
