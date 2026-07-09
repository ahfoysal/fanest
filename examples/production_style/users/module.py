from fanest import Module
from fanest.queues import QueueModule

from examples.production_style.users.controller import (
    AdminUsersController,
    AuthController,
    UsersController,
)
from examples.production_style.users.repository import InMemoryUserRepository
from examples.production_style.users.service import UsersService


@Module(
    imports=[QueueModule.register_queue("notifications")],
    controllers=[AuthController, UsersController, AdminUsersController],
    providers=[InMemoryUserRepository, UsersService],
    exports=[InMemoryUserRepository, UsersService],
)
class UsersModule:
    pass
