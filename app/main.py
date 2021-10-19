from enum import Enum, auto

import hmac
from typing import Any, Dict

import fastapi
from fastapi import FastAPI
from nio.events.room_events import Event
from starlette.responses import JSONResponse
from dataclasses import dataclass

from app.gmail import GmailClientManager
from app.utils import is_valid_email_mxid
import datargs

from . import utils as u
from .nio_client import NioClient
from .config import *
from .db import Db, MatrixKv
from .event_handler import EventHandler
from .prelude import *


app = FastAPI()

MAX_EVENT_HANDLE_TRY = 5


event_handler: Optional[EventHandler] = None

@app.on_event("startup")
async def start():
	global gmail_task
	global event_handler

	loop = aio.get_event_loop()
	loop.set_exception_handler(u.custom_exception_handler)
	
	matrix_client: NioClient = NioClient(homeserver_url=CONFIG.HOMESERVER_URL, homeserver_name=CONFIG.HOMESERVER_NAME)
	await matrix_client.appservice_login(CONFIG.AS_TOKEN, CONFIG.BRIDGE_ID)

	db = Db(MatrixKv(matrix_client, CONFIG.NAMESPACE_PREFIX + "state"))
	# await matrix.wait_till_connect()

	gclient = await GmailClientManager.new(await db.all_active_users())
	event_handler = EventHandler(gclient, matrix_client, db)
	gclient.on_token_error = event_handler.handle_token_error

	# refresh tokens
	updated_users = await gclient.refresh_tokens()
	for user in updated_users:
		await db.upsert_user(user)

	gmail_task = loop.create_task(event_handler.run_gmail_loop())
	gmail_task.add_done_callback(u.handle_task_result)

	logger.debug("started")




@app.put("/transactions/{tid}")
async def transaction(tid: str, body: Dict[str, Any], access_token: str):
	if not hmac.compare_digest(access_token, CONFIG.HS_TOKEN):
		return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=fastapi.status.HTTP_403_FORBIDDEN)

	logger.info("transaction", tid=tid, events=len(body['events']))

	for event_dict in body['events']:
		event = Event.parse_event(event_dict)
		logger.debug("Event Recvd", type=type(event))
		for i in range(MAX_EVENT_HANDLE_TRY):
			try:
				assert event_handler is not None
				if isinstance(event, Event):
					await event_handler.handle_matrix_event(event)
				else:
					logger.debug("Bad Event", bad_event=event)
				break
			except Exception as e:
				if i == MAX_EVENT_HANDLE_TRY - 1:
					raise GmailBridgeException(f"Cannot Handle Event After {i} retries, {event=}")
				else:
					logger.warning("Cannot Handle Event", will_retry_after=2**i, try_no=f"{i}/{MAX_EVENT_HANDLE_TRY}", exc_info=True)
					await aio.sleep(2**i)


@app.get("/users/{user_id}")
async def get_user(user_id: str, access_token: str):
	if access_token != CONFIG.HS_TOKEN:
		return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=fastapi.status.HTTP_403_FORBIDDEN)

	if not is_valid_email_mxid(user_id):
		logger.warning("request for invalid user", user_id=user_id)
		return JSONResponse({"errcode": "gmail.NOT_VALID_EMAIL"}, 404)

	assert event_handler is not None
	await event_handler.nio_client.c_ensure_appservice_user(user_id)
	return JSONResponse({})

@app.on_event("shutdown")
async def stop():
	gmail_task.cancel()

if __name__ == "__main__":
	class Command(Enum):
		hs_config = auto()
		bridge_config = auto()
		run_server = auto()

	@dataclass
	class Args:
		command: Command = datargs.arg(positional=True, default=Command.run_server)

	arg = datargs.parse(Args)

	if arg.command == Command.bridge_config:
		print(Config.get_sample_yaml())
	elif arg.command == Command.hs_config:
		print(CONFIG.get_hs_resistration_config())
	else:
		import uvicorn
		uvicorn.run(app, port=CONFIG.PORT, host='0.0.0.0') # type: ignore
