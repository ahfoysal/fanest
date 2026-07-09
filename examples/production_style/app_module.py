from fanest import APP_INTERCEPTOR, Module, use_class
from fanest.auth import AuthModule
from fanest.config import ConfigModule
from fanest.events import EventEmitterModule
from fanest.graphql import GraphQLModule
from fanest.health import HealthModule
from fanest.mailer import MailerModule
from fanest.queues import QueueModule
from fanest.swagger import DocumentBuilder, SwaggerModule
from fanest.throttler import ThrottlerModule

from examples.production_style.common import RequestIdInterceptor, RequestIdService
from examples.production_style.graphql import UsersResolver
from examples.production_style.jobs import MaintenanceJobs
from examples.production_style.notifications import NotificationsModule
from examples.production_style.realtime import UsersGateway
from examples.production_style.settings import DEFAULT_CONFIG
from examples.production_style.users import UsersModule


@Module(
    imports=[
        ConfigModule.for_root(env_file=None, values=DEFAULT_CONFIG, is_global=True),
        AuthModule.for_root(
            secret=DEFAULT_CONFIG["jwt_secret"],
            issuer=DEFAULT_CONFIG["jwt_issuer"],
            audience=DEFAULT_CONFIG["jwt_audience"],
            is_global=True,
        ),
        EventEmitterModule.for_root(is_global=True),
        QueueModule.for_root(is_global=True),
        MailerModule.for_root(
            is_global=True,
            default_from=DEFAULT_CONFIG["mail_from"],
            templates={"welcome": "Hi {{ name }}, your FaNest account is ready."},
        ),
        ThrottlerModule.for_root(limit=100, ttl=60),
        HealthModule.register(),
        UsersModule,
        NotificationsModule,
        GraphQLModule.for_root(
            imports=[UsersModule],
            resolvers=[UsersResolver],
            path="graphql",
        ),
    ],
    providers=[
        RequestIdService,
        MaintenanceJobs,
        use_class(APP_INTERCEPTOR, RequestIdInterceptor),
    ],
    gateways=[UsersGateway],
)
class AppModule:
    pass


swagger_config = (
    DocumentBuilder()
    .set_title(DEFAULT_CONFIG["app_name"])
    .set_description("Production-style FaNest example using in-memory adapters.")
    .set_version("1.0.0")
    .add_bearer_auth()
    .add_tag("users", "User management")
    .add_tag("admin", "JWT-protected administration")
    .build()
)


def setup_swagger(app):
    SwaggerModule.setup("/api/docs", app, SwaggerModule.create_document(app, swagger_config))
