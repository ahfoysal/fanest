from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect
from typing import Any, cast

from fanest import (
    ConnectedSocket,
    FaNestFactory,
    MessageBody,
    Module,
    SubscribeMessage,
    Catch,
    UseGuards,
    UseFilters,
    UseInterceptors,
    UsePipes,
    WebSocketGateway,
    WebSocketManager,
    SocketIoServer,
    ValidationPipe,
    create_param_decorator,
    forward_ref,
    use_factory,
)
from fanest.core.container import FaNestContainer
from fanest.platform_fastapi.adapter import FastApiAdapter
from fanest.websockets import UnsupportedSocketIoProtocolError


@WebSocketGateway("/chat")
class ChatGateway:
    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return {"echo": data}


@Module(gateways=[ChatGateway])
class ChatModule:
    pass


def test_websocket_gateway_dispatches_messages():
    client = TestClient(FaNestFactory.create(ChatModule))

    with client.websocket_connect("/chat") as websocket:
        websocket.send_json({"event": "echo", "data": "hello"})
        assert websocket.receive_json() == {"event": "echo", "data": {"echo": "hello"}}


@WebSocketGateway("/rooms")
class RoomGateway:
    def __init__(self, manager: WebSocketManager):
        self.manager = manager

    async def on_connect(self, websocket):
        self.manager.join("general", websocket)

    @SubscribeMessage("publish")
    async def publish(self, data, websocket):
        await self.manager.broadcast("general", "published", data, exclude=websocket)
        return {"sent": True}


@Module(gateways=[RoomGateway])
class RoomModule:
    pass


def test_websocket_manager_supports_rooms_and_broadcasts():
    client = TestClient(FaNestFactory.create(RoomModule))

    with client.websocket_connect("/rooms") as sender:
        with client.websocket_connect("/rooms") as receiver:
            sender.send_json({"event": "publish", "data": {"body": "hello"}})
            assert receiver.receive_json() == {"event": "published", "data": {"body": "hello"}}
            assert sender.receive_json() == {"event": "publish", "data": {"sent": True}}


class WebSocketTokenGuard:
    def can_activate(self, context):
        return context.request.query_params.get("token") == "ok"


class UpperMessagePipe:
    def transform(self, value, metadata):
        return str(value).upper()


@WebSocketGateway("/secure-chat")
@UseGuards(WebSocketTokenGuard)
class SecureChatGateway:
    @SubscribeMessage("shout")
    @UsePipes(UpperMessagePipe())
    async def shout(self, data, websocket):
        return {"message": data}


@Module(gateways=[SecureChatGateway])
class SecureChatModule:
    pass


@WebSocketGateway("/unannotated-body")
@UsePipes(ValidationPipe())
class UnannotatedBodyGateway:
    @SubscribeMessage("ping")
    async def ping(self, data=MessageBody()):
        return {"payload": data}


@Module(gateways=[UnannotatedBodyGateway])
class UnannotatedBodyModule:
    pass


def test_websocket_gateway_runs_guards_and_pipes():
    client = TestClient(FaNestFactory.create(SecureChatModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/secure-chat?token=bad"):
            pass

    with client.websocket_connect("/secure-chat?token=ok") as websocket:
        websocket.send_json({"event": "shout", "data": "hello"})
        assert websocket.receive_json() == {
            "event": "shout",
            "data": {"message": "HELLO"},
        }


def test_unannotated_websocket_message_body_is_treated_as_any_for_pipes():
    client = TestClient(FaNestFactory.create(UnannotatedBodyModule))

    with client.websocket_connect("/unannotated-body") as websocket:
        websocket.send_json({"event": "ping", "data": {"ok": True}})
        assert websocket.receive_json() == {
            "event": "ping",
            "data": {"payload": {"ok": True}},
        }


def test_websocket_gateway_guard_runs_before_on_connect():
    class CountingGuard:
        def can_activate(self, context):
            return False

    @WebSocketGateway("/preauth")
    @UseGuards(CountingGuard)
    class PreAuthGateway:
        connected = 0

        async def on_connect(self, websocket):
            type(self).connected += 1

        @SubscribeMessage("echo")
        async def echo(self, data, websocket):
            return data

    @Module(gateways=[PreAuthGateway])
    class PreAuthModule:
        pass

    client = TestClient(FaNestFactory.create(PreAuthModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/preauth"):
            pass
    assert PreAuthGateway.connected == 0


def test_websocket_gateway_on_connect_method_guard_runs_before_accept():
    class RejectConnectGuard:
        def can_activate(self, context):
            return False

    @WebSocketGateway("/method-preauth")
    class MethodPreAuthGateway:
        connected = 0

        @UseGuards(RejectConnectGuard)
        async def on_connect(self, websocket):
            type(self).connected += 1

        @SubscribeMessage("echo")
        async def echo(self, data, websocket):
            return data

    @Module(providers=[RejectConnectGuard, MethodPreAuthGateway])
    class MethodPreAuthModule:
        pass

    client = TestClient(FaNestFactory.create(MethodPreAuthModule))

    with pytest.raises(Exception):
        with client.websocket_connect("/method-preauth"):
            pass
    assert MethodPreAuthGateway.connected == 0


@WebSocketGateway("/socketio")
class SocketIoGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    async def on_connect(self, websocket):
        self.server.join(websocket, "lobby")

    @SubscribeMessage("announce")
    async def announce(self, data, websocket):
        await self.server.to("lobby").emit("announcement", data, exclude=websocket)
        return {"sent": True}


@Module(gateways=[SocketIoGateway])
class SocketIoModule:
    pass


def test_socket_io_style_room_emitter():
    client = TestClient(FaNestFactory.create(SocketIoModule))

    with client.websocket_connect("/socketio") as sender:
        with client.websocket_connect("/socketio") as receiver:
            sender.send_json({"event": "announce", "data": {"text": "hi"}})
            assert receiver.receive_json() == {"event": "announcement", "data": {"text": "hi"}}
            assert sender.receive_json() == {"event": "announce", "data": {"sent": True}}


@WebSocketGateway("/socketio-namespace")
class SocketIoNamespaceGateway:
    def __init__(self, server: SocketIoServer):
        self.namespace = server.of("/admin")

    async def on_connect(self, websocket):
        self.namespace.join(websocket)

    @SubscribeMessage("announce")
    async def announce(self, data, websocket):
        await self.namespace.emit("admin-news", data, exclude=websocket)
        return {"sent": True}


@Module(gateways=[SocketIoNamespaceGateway])
class SocketIoNamespaceModule:
    pass


def test_socket_io_namespace_style_emitter_is_isolated_from_rooms():
    client = TestClient(FaNestFactory.create(SocketIoNamespaceModule))

    with client.websocket_connect("/socketio-namespace") as sender:
        with client.websocket_connect("/socketio-namespace") as receiver:
            sender.send_json({"event": "announce", "data": {"text": "hi"}})
            assert receiver.receive_json() == {
                "event": "admin-news",
                "data": {"text": "hi"},
                "namespace": "/admin",
            }
            assert sender.receive_json() == {"event": "announce", "data": {"sent": True}}


@Catch(ValueError)
class WebSocketValueErrorFilter:
    def catch(self, exc, context):
        return {"kind": "value", "message": str(exc)}


@WebSocketGateway("/filtered-ws")
@UseFilters(WebSocketValueErrorFilter)
class FilteredGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("bad socket")


@Module(gateways=[FilteredGateway])
class FilteredWsModule:
    pass


def test_websocket_gateway_uses_exception_filters():
    client = TestClient(FaNestFactory.create(FilteredWsModule))

    with client.websocket_connect("/filtered-ws") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"kind": "value", "message": "bad socket"},
        }


@Catch(ValueError)
class WebSocketJsonResponseFilter:
    def catch(self, exc, context):
        return JSONResponse(status_code=418, content={"kind": "json-response", "message": str(exc)})


@WebSocketGateway("/response-filter-ws")
@UseFilters(WebSocketJsonResponseFilter)
class ResponseFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("response object")


@Module(gateways=[ResponseFilterGateway])
class ResponseFilterWsModule:
    pass


def test_websocket_exception_filter_can_return_json_response():
    client = TestClient(FaNestFactory.create(ResponseFilterWsModule))

    with client.websocket_connect("/response-filter-ws") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"kind": "json-response", "message": "response object"},
        }


@Catch(ValueError)
class WebSocketPlainResponseFilter:
    def catch(self, exc, context):
        return PlainTextResponse(f"plain:{exc}", status_code=418)


@WebSocketGateway("/plain-response-filter-ws")
@UseFilters(WebSocketPlainResponseFilter)
class PlainResponseFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("response object")


@Module(gateways=[PlainResponseFilterGateway])
class PlainResponseFilterWsModule:
    pass


def test_websocket_exception_filter_can_return_plain_response():
    client = TestClient(FaNestFactory.create(PlainResponseFilterWsModule))

    with client.websocket_connect("/plain-response-filter-ws") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": "plain:response object",
        }


@Catch(ValueError)
class ConnectJsonResponseFilter:
    def catch(self, exc, context):
        return JSONResponse(content={"kind": "connect", "message": str(exc)})


@WebSocketGateway("/connect-filter-ws")
@UseFilters(ConnectJsonResponseFilter)
class ConnectFilterGateway:
    async def on_connect(self, websocket):
        raise ValueError("connect failed")

    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return data


@Module(gateways=[ConnectFilterGateway])
class ConnectFilterWsModule:
    pass


def test_websocket_on_connect_exception_uses_filters_before_close():
    client = TestClient(FaNestFactory.create(ConnectFilterWsModule))

    with client.websocket_connect("/connect-filter-ws") as websocket:
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"kind": "connect", "message": "connect failed"},
        }
        with pytest.raises(Exception):
            websocket.receive_json()


@WebSocketGateway("/response-return-ws")
class ResponseReturnGateway:
    @SubscribeMessage("json")
    async def json_response(self, data, websocket):
        return JSONResponse(content={"kind": "handler-json", "payload": data})

    @SubscribeMessage("plain")
    async def plain_response(self, data, websocket):
        return PlainTextResponse(f"plain:{data}")

    @SubscribeMessage("bytes")
    async def bytes_response(self, data, websocket):
        return b"binary-ok"


@Module(gateways=[ResponseReturnGateway])
class ResponseReturnWsModule:
    pass


def test_websocket_handler_returned_responses_are_json_safe():
    client = TestClient(FaNestFactory.create(ResponseReturnWsModule))

    with client.websocket_connect("/response-return-ws") as websocket:
        websocket.send_json({"event": "json", "data": {"value": 1}})
        assert websocket.receive_json() == {
            "event": "json",
            "data": {"kind": "handler-json", "payload": {"value": 1}},
        }
        websocket.send_json({"event": "plain", "data": "ok"})
        assert websocket.receive_json() == {"event": "plain", "data": "plain:ok"}
        websocket.send_json({"event": "bytes", "data": None})
        assert websocket.receive_json() == {
            "event": "bytes",
            "data": {"__fanest_binary__": "YmluYXJ5LW9r", "encoding": "base64"},
        }


@WebSocketGateway("/decorated-socket")
class DecoratedSocketGateway:
    @SubscribeMessage("rename")
    async def rename(self, name: str = cast(Any, MessageBody("name")), websocket=ConnectedSocket()):
        assert websocket is not None
        return {"name": name}


@Module(gateways=[DecoratedSocketGateway])
class DecoratedSocketModule:
    pass


def test_websocket_gateway_supports_message_body_and_connected_socket_decorators():
    client = TestClient(FaNestFactory.create(DecoratedSocketModule))

    with client.websocket_connect("/decorated-socket") as websocket:
        websocket.send_json({"event": "rename", "data": {"name": "Ada"}})
        assert websocket.receive_json() == {"event": "rename", "data": {"name": "Ada"}}


async def async_socket_dependency_factory():
    return "socket-ready"


@WebSocketGateway("/async-factory-socket")
class AsyncFactorySocketGateway:
    def __init__(self, dependency: str = cast(Any, forward_ref(lambda: "SOCKET_DEPENDENCY"))):
        self.dependency = dependency

    @SubscribeMessage("ping")
    async def ping(self, data):
        return {"dependency": self.dependency, "data": data}


@Module(
    gateways=[AsyncFactorySocketGateway],
    providers=[use_factory("SOCKET_DEPENDENCY", async_socket_dependency_factory)],
)
class AsyncFactorySocketModule:
    pass


def test_websocket_gateway_awaits_async_factory_dependencies():
    client = TestClient(FaNestFactory.create(AsyncFactorySocketModule))

    with client.websocket_connect("/async-factory-socket") as websocket:
        websocket.send_json({"event": "ping", "data": "hello"})
        assert websocket.receive_json() == {
            "event": "ping",
            "data": {"dependency": "socket-ready", "data": "hello"},
        }


@WebSocketGateway("/provider-listed-socket")
class ProviderListedSocketGateway:
    @SubscribeMessage("ping")
    async def ping(self, data):
        return {"ok": data}


@Module(providers=[ProviderListedSocketGateway])
class ProviderListedSocketModule:
    pass


def test_websocket_gateway_registered_as_provider_is_routable():
    client = TestClient(FaNestFactory.create(ProviderListedSocketModule))

    with client.websocket_connect("/provider-listed-socket") as websocket:
        websocket.send_json({"event": "ping", "data": True})
        assert websocket.receive_json() == {"event": "ping", "data": {"ok": True}}


@WebSocketGateway("/advanced-socket")
class AdvancedSocketGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    async def on_connect(self, websocket):
        self.server.join(websocket, "all")

    @SubscribeMessage("broadcast")
    async def broadcast(self, data, websocket):
        await self.server.emit("notice", data)

    @SubscribeMessage("custom")
    async def custom(self, data, websocket):
        return {"event": "custom-result", "data": data}


@Module(gateways=[AdvancedSocketGateway])
class AdvancedSocketModule:
    pass


@WebSocketGateway("/global-socket")
class GlobalSocketGateway:
    def __init__(self, server: SocketIoServer):
        self.server = server

    @SubscribeMessage("broadcast")
    async def broadcast(self, data, websocket):
        await self.server.emit("global", data)


@Module(gateways=[GlobalSocketGateway])
class GlobalSocketModule:
    pass


def test_socket_io_server_broadcasts_to_roomless_connections():
    client = TestClient(FaNestFactory.create(GlobalSocketModule))

    with client.websocket_connect("/global-socket") as sender:
        with client.websocket_connect("/global-socket") as receiver:
            sender.send_json({"event": "broadcast", "data": {"text": "hi"}})
            assert sender.receive_json() == {"event": "global", "data": {"text": "hi"}}
            assert receiver.receive_json() == {"event": "global", "data": {"text": "hi"}}


def test_socket_io_server_broadcasts_to_all_connections_and_custom_response_event():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket") as sender:
        with client.websocket_connect("/advanced-socket") as receiver:
            sender.send_json({"event": "broadcast", "data": {"text": "hi"}})
            assert sender.receive_json() == {"event": "notice", "data": {"text": "hi"}}
            assert receiver.receive_json() == {"event": "notice", "data": {"text": "hi"}}
            sender.send_json({"event": "custom", "data": {"value": 1}})
            assert sender.receive_json() == {"event": "custom-result", "data": {"value": 1}}


def test_websocket_gateway_reports_malformed_payloads():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket") as websocket:
        websocket.send_text("{")
        assert websocket.receive_json()["event"] == "error"


def test_websocket_gateway_reports_native_socketio_protocol_as_unsupported():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket") as websocket:
        websocket.send_text('42["broadcast",{"text":"hi"}]')
        response = websocket.receive_json()
        assert response["event"] == "error"
        assert "Native Socket.IO/Engine.IO frames are not supported" in response["data"]


def test_socketio_protocol_error_is_exported_for_protocol_edges():
    assert issubclass(UnsupportedSocketIoProtocolError, RuntimeError)


@Catch(ValueError)
class BrokenWebSocketFilter:
    def catch(self, exc, context):
        raise RuntimeError("filter failed")


@WebSocketGateway("/broken-filter")
@UseFilters(BrokenWebSocketFilter)
class BrokenFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data, websocket):
        raise ValueError("handler failed")

    @SubscribeMessage("echo")
    async def echo(self, data, websocket):
        return data


@Module(gateways=[BrokenFilterGateway])
class BrokenFilterModule:
    pass


def test_websocket_filter_exception_returns_error_without_closing_connection():
    client = TestClient(FaNestFactory.create(BrokenFilterModule))

    with client.websocket_connect("/broken-filter") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {"event": "error", "data": "filter failed"}
        websocket.send_json({"event": "echo", "data": "still-open"})
        assert websocket.receive_json() == {"event": "echo", "data": "still-open"}


async def async_ws_param_factory(data, context):
    return {"marker": data, "path": context.request.url.path}


AsyncSocketParam = create_param_decorator(async_ws_param_factory)


@WebSocketGateway("/async-param-socket")
class AsyncParamGateway:
    @SubscribeMessage("inspect")
    async def inspect_param(self, resolved=AsyncSocketParam("socket")):
        return resolved


@Module(gateways=[AsyncParamGateway])
class AsyncParamModule:
    pass


def test_websocket_gateway_awaits_async_custom_parameter_decorators():
    client = TestClient(FaNestFactory.create(AsyncParamModule))

    with client.websocket_connect("/async-param-socket") as websocket:
        websocket.send_json({"event": "inspect", "data": None})
        assert websocket.receive_json() == {
            "event": "inspect",
            "data": {"marker": "socket", "path": "/async-param-socket"},
        }


@WebSocketGateway("/disconnect-hook-socket")
class DisconnectHookGateway:
    disconnected = 0

    async def on_disconnect(self, websocket):
        type(self).disconnected += 1
        raise RuntimeError("cleanup failed")

    @SubscribeMessage("echo")
    async def echo(self, data):
        return data


@Module(gateways=[DisconnectHookGateway])
class DisconnectHookModule:
    pass


def test_websocket_disconnect_hook_exception_does_not_escape_teardown():
    DisconnectHookGateway.disconnected = 0
    client = TestClient(FaNestFactory.create(DisconnectHookModule))

    with client.websocket_connect("/disconnect-hook-socket") as websocket:
        websocket.send_json({"event": "echo", "data": "ok"})
        assert websocket.receive_json() == {"event": "echo", "data": "ok"}

    assert DisconnectHookGateway.disconnected == 1


class RecordingSocket:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.messages = []

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("closed")
        self.messages.append(payload)


@pytest.mark.anyio
async def test_websocket_manager_prunes_failed_connections_during_broadcast():
    manager = WebSocketManager()
    healthy = RecordingSocket()
    broken = RecordingSocket(fail=True)
    manager.connect(cast(Any, healthy))
    manager.connect(cast(Any, broken))

    await manager.broadcast_all("notice", {"ok": True})

    assert healthy.messages == [{"event": "notice", "data": {"ok": True}}]
    assert manager.connections() == [healthy]


@pytest.mark.anyio
async def test_websocket_adapter_send_failure_prunes_connection():
    container = FaNestContainer()
    adapter = FastApiAdapter(app=FastAPI(), container=container)
    broken = RecordingSocket(fail=True)
    manager = container.resolve(WebSocketManager)
    manager.connect(cast(Any, broken))

    assert await adapter._send_websocket_event(cast(Any, broken), "error", "closed") is False
    assert manager.connections() == []


@Catch(ValueError)
class ConnectValueErrorFilter:
    def catch(self, exc, context):
        return JSONResponse({"phase": "connect", "message": str(exc)})


@WebSocketGateway("/connect-filter")
@UseFilters(ConnectValueErrorFilter)
class ConnectCloseFilterGateway:
    disconnected = 0

    async def on_connect(self, websocket):
        raise ValueError("connect failed")

    async def on_disconnect(self, websocket):
        type(self).disconnected += 1

    @SubscribeMessage("echo")
    async def echo(self, data):
        return data


@Module(gateways=[ConnectCloseFilterGateway])
class ConnectFilterModule:
    pass


def test_websocket_on_connect_exception_uses_filters_and_closes_cleanly():
    ConnectCloseFilterGateway.disconnected = 0
    client = TestClient(FaNestFactory.create(ConnectFilterModule))

    with client.websocket_connect("/connect-filter") as websocket:
        assert websocket.receive_json() == {
            "event": "error",
            "data": {"phase": "connect", "message": "connect failed"},
        }
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()

    assert ConnectCloseFilterGateway.disconnected == 1


class NonJsonPayload:
    def __str__(self):
        return "non-json-payload"


@Catch(ValueError)
class NonJsonWebSocketFilter:
    def catch(self, exc, context):
        return NonJsonPayload()


@WebSocketGateway("/non-json-filter")
@UseFilters(NonJsonWebSocketFilter)
class NonJsonFilterGateway:
    @SubscribeMessage("fail")
    async def fail(self, data):
        raise ValueError("not json")


@Module(gateways=[NonJsonFilterGateway])
class NonJsonFilterModule:
    pass


def test_websocket_filter_non_json_payload_falls_back_to_string():
    client = TestClient(FaNestFactory.create(NonJsonFilterModule))

    with client.websocket_connect("/non-json-filter") as websocket:
        websocket.send_json({"event": "fail", "data": None})
        assert websocket.receive_json() == {
            "event": "error",
            "data": "non-json-payload",
        }


class WebSocketEnvelopeInterceptor:
    async def intercept(self, context, call_next):
        result = await call_next()
        return {"wrapped": result, "path": context.request.url.path}


@WebSocketGateway("/intercepted-socket")
@UseInterceptors(WebSocketEnvelopeInterceptor())
class InterceptedSocketGateway:
    @SubscribeMessage("echo")
    async def echo(self, data):
        return {"echo": data}


@Module(gateways=[InterceptedSocketGateway])
class InterceptedSocketModule:
    pass


def test_websocket_gateway_runs_interceptors_and_supports_ack_frames():
    client = TestClient(FaNestFactory.create(InterceptedSocketModule, global_prefix="api"))

    with client.websocket_connect("/api/intercepted-socket") as websocket:
        websocket.send_json({"event": "echo", "data": "hello", "id": "ack-1"})
        assert websocket.receive_json() == {
            "event": "ack",
            "data": {
                "id": "ack-1",
                "event": "echo",
                "data": {
                    "wrapped": {"echo": "hello"},
                    "path": "/api/intercepted-socket",
                },
            },
        }


@WebSocketGateway("/binary-socket")
class BinarySocketGateway:
    @SubscribeMessage("echo-bytes")
    async def echo_bytes(self, data):
        return memoryview(str(data).encode())


@Module(gateways=[BinarySocketGateway])
class BinarySocketModule:
    pass


def test_websocket_gateway_accepts_binary_json_envelopes_and_returns_json_safe_binary():
    client = TestClient(FaNestFactory.create(BinarySocketModule))

    with client.websocket_connect("/binary-socket") as websocket:
        websocket.send_bytes(b'{"event":"echo-bytes","data":"payload","ack":"bin-1"}')
        assert websocket.receive_json() == {
            "event": "ack",
            "data": {
                "id": "bin-1",
                "event": "echo-bytes",
                "data": {"__fanest_binary__": "cGF5bG9hZA==", "encoding": "base64"},
            },
        }


@WebSocketGateway("/socketio-namespace-rooms")
class SocketIoNamespaceRoomsGateway:
    def __init__(self, server: SocketIoServer):
        self.default = server
        self.admin = server.of("/admin")

    async def on_connect(self, websocket):
        self.default.join(websocket, "lobby")
        self.admin.join(websocket)
        self.admin.join_room(websocket, "lobby")

    @SubscribeMessage("announce")
    async def announce(self, data, websocket, namespace="/"):
        if namespace == "/admin":
            await self.admin.to("lobby").emit("admin-news", data, exclude=websocket)
            return {"sent": "admin"}
        await self.default.to("lobby").emit("default-news", data, exclude=websocket)
        return {"sent": "default"}


@Module(gateways=[SocketIoNamespaceRoomsGateway])
class SocketIoNamespaceRoomsModule:
    pass


def test_socket_io_namespace_payloads_route_and_rooms_are_namespace_scoped():
    client = TestClient(FaNestFactory.create(SocketIoNamespaceRoomsModule))

    with client.websocket_connect("/socketio-namespace-rooms") as sender:
        with client.websocket_connect("/socketio-namespace-rooms") as receiver:
            sender.send_json(
                {
                    "event": "announce",
                    "namespace": "/admin",
                    "data": {"text": "secret"},
                    "id": "admin-1",
                }
            )
            assert receiver.receive_json() == {
                "event": "admin-news",
                "data": {"text": "secret"},
                "namespace": "/admin",
            }
            assert sender.receive_json() == {
                "event": "ack",
                "data": {"id": "admin-1", "event": "announce", "data": {"sent": "admin"}},
                "namespace": "/admin",
            }

            sender.send_json({"event": "announce", "data": {"text": "public"}})
            assert receiver.receive_json() == {
                "event": "default-news",
                "data": {"text": "public"},
            }
            assert sender.receive_json() == {
                "event": "announce",
                "data": {"sent": "default"},
            }


def test_websocket_manager_disconnect_removes_namespace_scoped_rooms():
    manager = WebSocketManager()
    socket = RecordingSocket()
    manager.connect(cast(Any, socket))
    manager.join("lobby", cast(Any, socket), namespace="/admin")

    manager.disconnect(cast(Any, socket))

    assert manager.connections("lobby", namespace="/admin") == []
    assert manager.namespace_connections("/admin") == []


@WebSocketGateway("/ack-none-socket")
class AckNoneGateway:
    @SubscribeMessage("side-effect")
    async def side_effect(self, data):
        return None


@Module(gateways=[AckNoneGateway])
class AckNoneModule:
    pass


def test_websocket_ack_is_sent_even_when_handler_returns_none():
    client = TestClient(FaNestFactory.create(AckNoneModule))

    with client.websocket_connect("/ack-none-socket") as websocket:
        websocket.send_json({"event": "side-effect", "data": {"ok": True}, "id": "none-1"})
        assert websocket.receive_json() == {
            "event": "ack",
            "data": {"id": "none-1", "event": "side-effect", "data": None},
        }


def test_websocket_gateway_rejects_engineio_transport_modes_cleanly():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket?transport=polling") as websocket:
        response = websocket.receive_json()
        assert response["event"] == "error"
        assert "Unsupported Engine.IO transport mode 'polling'" in response["data"]
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_websocket_gateway_rejects_engineio_handshakes_cleanly():
    client = TestClient(FaNestFactory.create(AdvancedSocketModule))

    with client.websocket_connect("/advanced-socket?transport=websocket&EIO=4") as websocket:
        response = websocket.receive_json()
        assert response["event"] == "error"
        assert "Native Engine.IO handshakes are not supported" in response["data"]
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_socket_io_server_lifecycle_events_are_emitted():
    events = []

    @WebSocketGateway("/socketio-lifecycle")
    class SocketIoLifecycleGateway:
        def __init__(self, server: SocketIoServer):
            server.on("connect", self.connected)
            server.on("disconnect", self.disconnected)

        def connected(self, websocket, namespace):
            events.append(("connect", namespace))

        def disconnected(self, websocket, namespace):
            events.append(("disconnect", namespace))

        @SubscribeMessage("echo")
        async def echo(self, data):
            return data

    @Module(gateways=[SocketIoLifecycleGateway])
    class SocketIoLifecycleModule:
        pass

    client = TestClient(FaNestFactory.create(SocketIoLifecycleModule))

    with client.websocket_connect("/socketio-lifecycle") as websocket:
        websocket.send_json({"event": "echo", "data": "ok"})
        assert websocket.receive_json() == {"event": "echo", "data": "ok"}

    assert events == [("connect", "/"), ("disconnect", "/")]
