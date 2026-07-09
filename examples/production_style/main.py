from fanest import FaNestFactory, ValidationPipe

from examples.production_style.app_module import AppModule, setup_swagger


app = FaNestFactory.create(
    AppModule,
    title="FaNest Production Style Example",
    description="A code-only example app covering FaNest modules, DI, HTTP, auth, GraphQL, WebSockets, queues, mail, events, and scheduling.",
    version="1.0.0",
    global_prefix="api",
    cors=True,
    global_pipes=[ValidationPipe()],
)

setup_swagger(app)
