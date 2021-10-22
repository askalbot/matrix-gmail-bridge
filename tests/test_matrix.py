import base64
import nio
from typing import Coroutine
from typing import cast
import uvicorn
import httpx
import math
import uuid
import app.nio_client
from threading import Thread
import time
import os
from app.config import get_config, BridgeConfig, override_config
from app.gmail import *
import pytest
from pytest_mock import MockerFixture
import asyncio as aio

SYNAPSE_URL = "http://localhost:8008"


@pytest.fixture(scope='session')
def event_loop():
    loop = aio.get_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope='session')
def test_config() -> BridgeConfig:
    # test_config = BridgeConfig(
    #     AES_KEY=base64.b64encode(os.urandom(16)).decode(),
    #     AS_TOKEN="test_as_token",
    #     HS_TOKEN="test_hs_token",
    #     HOMESERVER_URL=SYNAPSE_URL,
    #     HOMESERVER_NAME="jif.one",
    #     gmail_client_id="shouldn't matter",
    #     gmail_client_secret="shouldn't matter",
    #     gmail_project_id="shouldn't matter",
    # )
    test_config = BridgeConfig.example_config()
    test_config.HOMESERVER_NAME = "jif.one"
    test_config.GMAIL_RECHECK_SECONDS = 1
    override_config(test_config)
    return test_config


def is_synapse_running(url: str) -> bool:
    """ waits ~10s"""
    for i in range(35):
        try:
            httpx.get(url)
            return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


synapse_integration = pytest.mark.skipif(
    condition=not is_synapse_running(SYNAPSE_URL),
    reason="Synapse Should Be Running for integration tests",
)


@dataclass
class RecvdMail:
    content: MsgContent
    to: List[str]
    thread_id: str
    cc: List[str] = field(default_factory=list)
    first_in_thread: bool = False


@dataclass
class MockGmailClient:
    user: LoggedInUser
    email_id: str
    last_mail_id: Optional[str] = None
    email_name: Optional[str] = None
    new_mails: List[Gmail] = field(default_factory=list)
    recvd_mails: List[RecvdMail] = field(default_factory=list)

    _logger: BoundLogger = field(default_factory=lambda: Logger("gmail-client"))

    async def close(self):
        pass

    @classmethod
    async def from_user(cls, user_state: LoggedInUser, service_key: ServiceKey) -> 'MockGmailClient':
        return cls(user_state, user_state.email_address)

    async def get_new_mails(self) -> AsyncGenerator[Gmail, None]:
        for i in self.new_mails:
            yield i

    async def reply_to_thread(self, thread_id: str, content: MsgContent, to: List[str], cc: List[str] = []):
        self.recvd_mails.append(RecvdMail(content, to, thread_id, cc, False))

    async def start_new_thread(self, content: MsgContent, to: List[str], cc: List[str] = []) -> str:
        thread_id = uuid.uuid4().hex
        self.recvd_mails.append(RecvdMail(content, to, thread_id, cc, True))
        return thread_id


@dataclass
class MockGoogleAuth:
    service_key: ServiceKey
    oauth_client: GoogleOAuth2 = field(init=False)

    def __post_init__(self):
        self.default_factory = lambda: GoogleOAuth2(
            self.service_key.client_id,
            self.service_key.client_secret,
        )

    async def refresh_token(self, token: Token) -> Token:
        return token

    async def get_oauth_flow_url(self) -> str:
        return "https://some_url"

    async def get_access_token(self, token_code: str) -> Token:
        return Token(
            access_token=token_code,
            refresh_token=token_code,
            email="random_email@gmail.com",
            expiry=dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=1),
            raw={}
        )

    async def revoke_token(self, token: Token):
        return


@pytest.fixture(scope="session")
def mock_google(session_mocker: MockerFixture):
    session_mocker.patch("app.gmail.GmailClient", MockGmailClient)
    session_mocker.patch("app.gmail.GoogleAuth", MockGoogleAuth)


class UvicornTestServer(uvicorn.Server):
    """Uvicorn test server

    Usage:
        @pytest.fixture
        server = UvicornTestServer()
        await server.up()
        yield
        await server.down()
    """
    def __init__(
        self,
        app,
        loop,
        host='127.0.0.1',
        port='8000',
    ):
        """Create a Uvicorn test server

        Args:
            app (FastAPI, optional): the FastAPI app. Defaults to main.app.
            host (str, optional): the host ip. Defaults to '127.0.0.1'.
            port (int, optional): the port. Defaults to PORT.
        """
        self.loop = loop
        self._startup_done = aio.Event()
        super().__init__(config=uvicorn.Config(app, host=host, port=port))

    async def startup(self, sockets: Optional[List] = None) -> None:
        """Override uvicorn startup"""
        await super().startup(sockets=sockets)
        self.config.setup_event_loop()
        self._startup_done.set()

    async def up(self) -> None:
        """Start up server asynchronously"""
        self._serve_task = self.loop.create_task(self.serve())
        await self._startup_done.wait()

    async def down(self) -> None:
        """Shut down server asynchronously"""
        self.should_exit = True
        await self._serve_task


@pytest.fixture(scope="session")
async def run_server(mock_google, test_config: BridgeConfig, event_loop):
    from app.main import app
    server = UvicornTestServer(app, event_loop, host="0", port=test_config.PORT)

    await server.up()
    success = False
    for i in range(35):
        async with httpx.AsyncClient() as c:
            try:
                await c.get(f"http://localhost:{test_config.PORT}")
                success = True
                break
            except httpx.HTTPError:
                await aio.sleep(0.3)
                continue
    if not success:
        raise RuntimeError("Can't start server")
    yield server
    await server.down()


@pytest.fixture(scope="function")
async def test_client(test_config: BridgeConfig) -> app.nio_client.NioClient:
    client = nio.AsyncClient(homeserver=test_config.HOMESERVER_URL)

    localpart = f"new_user{uuid.uuid4().hex}"
    user_id = f"@{localpart}:jif.one"
    password = "my_password"
    r = await client.register(localpart, password)
    assert not isinstance(r, nio.ErrorResponse), r

    client = app.nio_client.NioClient(
        homeserver_url=test_config.HOMESERVER_URL, homeserver_name=test_config.HOMESERVER_NAME, user=user_id
    )
    r = await client.login(password)
    assert isinstance(r, nio.LoginResponse), r
    return client


async def keep_checking(
    condition: Callable[[], Coroutine[None, None, bool]],
    total_duration: float = 10.0,
    gap_second: float = 0.3,
) -> bool:
    total_runs = math.ceil(total_duration / gap_second)
    success = False
    for i in range(total_runs):
        if await condition():
            success = True
            break
        else:
            await aio.sleep(gap_second)
    return success


@synapse_integration
@pytest.mark.asyncio
async def test_mock(mock_google, test_config: BridgeConfig, run_server, test_client: app.nio_client.NioClient):
    auth = MockGoogleAuth(test_config.get_service_key())
    from app.main import event_handler

    assert event_handler is not None, "event handler not set by app startup"
    gcm = event_handler.gclient
    gcm.users = cast(Dict[MatrixUserId, Tuple[LoggedInUser, MockGmailClient]], gcm.users) # type: ignore

    await gcm.upsert_user(User(matrix_id=test_client.user_id).logged_in(token=await auth.get_access_token("some code")))
    r = await test_client.room_create(name="my room", invite=['@_gmail_bridge_nnkit_at_protonmail.com:jif.one'])
    assert isinstance(r, nio.RoomCreateResponse), r
    gclient = gcm.users[test_client.user_id][1]

    # -------------------------- Matrix Msg should be recvd on gmail
    assert len(gclient.recvd_mails) == 0
    await aio.sleep(2)
    await test_client.room_send(
        room_id=r.room_id, message_type="m.room.message", content={
            "msgtype": "m.text",
            "body": "Hello world!"
        }
    )
    print("msg sent")

    async def mail_recvd_check() -> bool:
        return len(gclient.recvd_mails) == 1

    assert await keep_checking(mail_recvd_check), ("didn't recieve msg", await test_client.c_room_members(r.room_id))

    # -------------------------- Mail from new user should create new user in matrix

    recvd = gclient.recvd_mails.pop()
    gclient.new_mails.append(
        Gmail(
            sender="my_guy@gmail.com",
            to=[recvd.to[0]],
            content=MsgContent(body="body", html_body="html_body", subject="subject"),
            thread_id=recvd.thread_id,
            gmail_id=uuid.uuid4().hex,
        )
    )

    async def member_joined() -> bool:
        members = await test_client.c_room_members(r.room_id)
        return "@_gmail_bridge_my_guy_at_gmail.com:jif.one" in members

    assert await keep_checking(member_joined), "New User didn't join room"
