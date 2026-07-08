from fanest import Controller, FaNestFactory, Get, Injectable, Module


@Injectable()
class AppService:
    def get_info(self):
        return {"framework": "FaNest", "message": "NestJS-style Python is alive"}


@Controller("/")
class AppController:
    def __init__(self, app_service: AppService):
        self.app_service = app_service

    @Get("/")
    async def index(self):
        return self.app_service.get_info()


@Module(controllers=[AppController], providers=[AppService])
class AppModule:
    pass


app = FaNestFactory.create(AppModule, title="FaNest Basic Example")
