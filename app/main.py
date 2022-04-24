import argparse
import hmac
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict

import fastapi
from fastapi import FastAPI, Request
from nio.events.room_events import Event
from starlette.responses import JSONResponse

from app.models import User

from . import utils as u
from .bridge import UserBridge
from .config import *
from .db import Db, MatrixKv
from .nio_client import AppserviceClient
from .prelude import *

app = FastAPI()


@dataclass
class AppState:
	config: BridgeConfig = get_config()
	appservice: Optional[AppserviceClient] = None
	db: Optional[Db] = None
	bridges: Dict[str, UserBridge] = field(default_factory=dict)
	_sync_task: Optional[aio.Task] = None

	async def on_matrix_event(self, event: Event):
		assert self.db is not None
		assert self.appservice is not None
		sender = event.sender
		if sender == self.appservice.appservice_id or self.appservice.is_our_bot(sender):
			return
		if event.sender in self.bridges:
			await self.bridges[event.sender].handle_matrix_event(event)
		else:
			user = User(matrix_id=event.sender)
			await self.db.upsert_user(user)
			self.bridges[event.sender] = await UserBridge.new(
				self.db,
				self.appservice,
				user=user,
				gmail_service_key=self.config.get_service_key(),
			)
			await self.bridges[event.sender].handle_matrix_event(event)

	async def start(self):
		config = self.config
		self.appservice = AppserviceClient(
			homeserver_url=config.HOMESERVER_URL,
			homeserver_name=config.HOMESERVER_NAME,
			namespace_prefix=config.NAMESPACE_PREFIX,
		)
		appservice = self.appservice

		await aio.wait_for(appservice.wait_for_server(), timeout=60)
		await appservice.login(config.AS_TOKEN)

		self.db = Db(MatrixKv(appservice, config.NAMESPACE_PREFIX + "state"))
		active_users = await self.db.all_active_users()

		logger.info("Got active users", total_active_users=len(active_users))
		for user in active_users:
			self.bridges[user.matrix_id] = await UserBridge.new(self.db, appservice, user, config.get_service_key())

		self._sync_task = aio.create_task(self.sync_loop())

	async def sync_loop(self):
		while True:
			for user, bridge in self.bridges.items():
				# TODO: handle concurrency/rate-limits
				try:
					await bridge.sync_gmails()
				except Exception as e:
					logger.error("Error syncing gmails", exc_info=True, matrix_id=user)
			await aio.sleep(self.config.GMAIL_RECHECK_SECONDS)

	async def stop(self):
		if self._sync_task is not None:
			self._sync_task.cancel()
			await self._sync_task

		tasks = []
		for bridge in self.bridges.values():
			tasks.append(bridge.close())
		await aio.gather(*tasks)


APP_STATE = AppState()


@app.on_event("startup")
async def start():
	loop = aio.get_event_loop()
	loop.set_exception_handler(u.custom_exception_handler)
	await APP_STATE.start()
	logger.debug("started")


@app.middleware("http")
async def verify_homeserver_token(request: Request, call_next):
	access_token = request.query_params['access_token']
	if not hmac.compare_digest(access_token, APP_STATE.config.HS_TOKEN):
		return JSONResponse({"errcode": "M_FORBIDDEN"}, status_code=fastapi.status.HTTP_403_FORBIDDEN)
	else:
		return await call_next(request)


@app.put("/transactions/{tid}")
async def transaction(tid: str, body: Dict[str, Any]):
	logger.info("recieved transaction", tid=tid, events=len(body['events']))
	for event_dict in body['events']:
		event = Event.parse_event(event_dict)
		logger.debug("Event Recvd", type=type(event))

		if not isinstance(event, Event):
			logger.debug("Bad Event", bad_event=event)
			continue

		await APP_STATE.on_matrix_event(event)


@app.get("/rooms/{room_alias}")
async def get_room(room_alias: str):
	appservice = APP_STATE.appservice
	assert appservice is not None
	if await appservice.resolve_room_alias(room_alias) is None:
		return JSONResponse({"errcode": "ALIAS_DOES_NOT_EXIST"}, 404)
	else:
		return JSONResponse({})


@app.get("/users/{mxid}")
async def get_user(mxid: str):
	appservice = APP_STATE.appservice
	assert appservice is not None

	senatized_mail = appservice.extract_bot_name(mxid)
	email = u.try_email_desanitize(senatized_mail)

	if email is not None and u.is_valid_email(email):
		await appservice.ensure_virtual_user(mxid)
		return JSONResponse({})
	else:
		logger.warn("request to join invalid mxid", mxid=mxid)
		return JSONResponse({"errcode": "NOT_VALID_EMAIL_MXID", "mxid": mxid}, 404)


@app.on_event("shutdown")
async def stop():
	await APP_STATE.stop()


def run_cli():

	class Command(Enum):
		hs_config = auto()
		bridge_config = auto()
		run_server = auto()

	parser = argparse.ArgumentParser(description='Process some integers.', prog='Gmail Bridge')
	parser.add_argument(
		'command',
		type=str,
		help='command to run',
		default=Command.run_server.name,
		choices=[c.name for c in Command],
	)

	args = parser.parse_args()
	c = Command[args.command]

	if c == Command.bridge_config:
		print(BridgeConfig.get_sample_yaml())
	elif c == Command.hs_config:
		print(get_config().get_hs_resistration_config())
	elif c == Command.run_server:
		import uvicorn
		uvicorn.run(app, port=get_config().PORT, host=APP_STATE.config.HOST) # type: ignore
	else:
		raise NotImplementedError(c)


if __name__ == "__main__":
	run_cli()
