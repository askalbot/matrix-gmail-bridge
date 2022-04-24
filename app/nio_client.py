from __future__ import annotations
import json
import re
import textwrap
import uuid
from typing import *
from urllib.parse import quote_plus as quote_url
from urllib.parse import quote_plus as url_quote
from urllib.parse import urlencode

import aiohttp
import httpx
import nio
from nio import AsyncClient
from nio.api import Api

from . import utils as u
from .log import Logger
from .models import Attachment, AttachmentType
from .prelude import *

REQ_PRE = "/_matrix/client/r0"


class RequestException(Exception):

	def __init__(self, e: Union[nio.ErrorResponse, Tuple[aiohttp.ClientResponse, Dict]]):
		self.e = e
		super().__init__(e)

	@classmethod
	async def from_aio_resp(cls, e: aiohttp.ClientResponse) -> 'RequestException':
		assert not e.ok
		d = await e.json()
		return cls((e, d))

	def err_code(self) -> Optional[str]:
		if isinstance(self.e, nio.ErrorResponse):
			return self.e.status_code
		else:
			return self.e[1].get('err_code')

	def is_forbidden(self) -> bool:
		return self.err_code() == 'M_FORBIDDEN'


class BadEventException(Exception):

	def __init__(self, event: nio.BadEventType):
		self.event = event
		super().__init__(f"bad event, {event=}")


class AppserviceClient(AsyncClient):
	"""
	Wrapper over Nio Client that is friendly to appservices.
	Support for making request through a virtual user (using arg `as_bot`)
	"""

	def __init__(
		self,
		homeserver_url: str,
		homeserver_name: str,
		namespace_prefix: str,
		appservice_id: str = "",
		device_id: Optional[str] = "",
		store_path: Optional[str] = "",
		config: Optional[nio.AsyncClientConfig] = None,
		ssl: Optional[bool] = None,
		proxy: Optional[str] = None,
	):
		super().__init__(
			homeserver_url,
			user=appservice_id,
			device_id=device_id,
			store_path=store_path,
			config=config,
			ssl=ssl,
			proxy=proxy,
		)
		self.namespace_prefix = namespace_prefix
		self.homeserver_name = homeserver_name
		self._logger = Logger("matrix-client")

	@property
	def appservice_id(self) -> str:
		return self.user_id

	def is_our_bot(self, mxid: str) -> bool:
		return mxid.startswith(f"@{self.namespace_prefix}")

	def is_our_mxid(self, mxid: str) -> bool:
		return self.is_our_bot(mxid) or mxid == self.appservice_id

	def is_our_alias(self, alias: str) -> bool:
		return alias.startswith(f"#{self.namespace_prefix}")

	def generate_bot_mxid(self, name: str) -> str:
		return f"@{self.namespace_prefix}{name}:{self.homeserver_name}"

	def extract_bot_name(self, mxid: str) -> str:
		assert self.is_our_bot(mxid), f"Not an appservice bot: {mxid}"
		localpart = u.extract_mxid_localpart(mxid)
		return localpart.removeprefix(self.namespace_prefix)

	def extract_alias_name(self, alias: str) -> str:
		assert self.is_our_alias(alias), f"Not an appservice alias: {alias}"
		localpart = u.extract_alias_localpart(alias)
		return localpart.removeprefix(self.namespace_prefix)

	@staticmethod
	def parse_media_url(media_url: str) -> Tuple[str, str]:
		""" returns (homerserver_name, media_id)"""
		m = re.fullmatch(r"mxc://(?P<hs>.*)/(?P<id>.*)", media_url)
		assert m is not None, f"Not a media url {media_url=}"
		return m.group('hs'), m.group('id')

	def generate_room_alias(self, name: str) -> str:
		return f"#{self.namespace_prefix}{name}:{self.homeserver_name}"

	async def login(self, access_token: str):
		self.access_token = access_token
		r = await self.whoami()
		assert isinstance(r, nio.responses.WhoamiResponse), r
		self.user_id = r.user_id

	async def resolve_room_alias(self, room_alias: str) -> Optional[str]:
		r = await self.room_resolve_alias(room_alias)
		if isinstance(r, nio.RoomResolveAliasError):
			if r.status_code == "M_NOT_FOUND":
				return None
			raise RequestException(r)
		else:
			return r.room_id

	async def join_room(self, room_id: str, as_bot: Optional[str] = None):
		self._logger.debug("join room", mxid=as_bot, room_id=room_id)
		await self._raw("POST", f"/join/{quote_url(room_id)}", user=as_bot)

	async def invite_and_join_room(self, mxid: str, room_id: str, as_bot: Optional[str] = None):
		"""
		Requires `mxid` to be virtual user of current appservice
		Ignores if mxid is already in room
		"""
		r = await self._raw("POST", f"/rooms/{room_id}/invite", user=as_bot, data={"user_id": mxid}, strict=False)
		if r.status == 403 and (await r.json())['errcode'] == "M_FORBIDDEN":
			try:
				await self.join_room(room_id, mxid)
			except RequestException as e:
				if e.err_code() != "M_FORBIDDEN":
					raise e
				# already in room
			return

		r = await self.join_room(room_id, mxid)
		assert not isinstance(r, nio.ErrorResponse), r

	async def user_exists(self, mxid: str) -> bool:
		resp = await self.get_profile(mxid)
		if isinstance(resp, nio.ProfileGetResponse):
			return True
		elif resp.status_code == "M_NOT_FOUND":
			return False
		raise GmailBridgeException(f"Unexpected Response from Nio Library {resp=}")

	async def ensure_virtual_user(self, bot_id: str):
		if await self.user_exists(bot_id):
			return

		localpart = u.extract_mxid_localpart(bot_id)
		content = {
			"type": "m.login.application_service",
			# "@test:localhost" -> "test" (Can't register with a full mxid.)
			"username": localpart,
		}
		self._logger.debug("create user", content=content)
		await self._raw("POST", "/register", data=content)

	async def set_room_power_levels(self, room_id: str, power_levels: Dict[str, int]) -> Dict[str, int]:
		r = await self.room_get_state_event(room_id, "m.room.power_levels")
		assert isinstance(r, nio.RoomGetStateEventResponse), r
		new_content = r.content
		new_content['users'].update(power_levels)
		r = await self._raw("PUT", f"/rooms/{url_quote(room_id)}/state/m.room.power_levels", data=new_content)
		return new_content['users']

	async def get_room_power_levels(self, room_id: str) -> Dict[str, int]:
		users = await self.get_room_members(room_id)
		r = await self.room_get_state_event(room_id, "m.room.power_levels")
		assert isinstance(r, nio.RoomGetStateEventResponse), r
		default_power = r.content['users_default']
		powers: Dict[str, int] = r.content['users']

		return {u: powers.get(u, default_power) for u in users}

	async def get_room_members(self, room_id: str, as_bot: Optional[str] = None) -> List[str]:
		r = await self._raw("GET", f"/rooms/{quote_url(room_id)}/joined_members", user=as_bot)
		resp = await r.json()
		return list(resp['joined'])

	async def c_send_attachement(self, room_id: str, as_bot: str, attachment: Attachment, info: Dict = {}) -> str:
		""" returns msg id """
		length = len(attachment.content)
		resp, _decrypt_info = await self.upload(lambda a, b: attachment.content_io(), attachment.mime_type, filesize=length)
		assert isinstance(resp, nio.UploadResponse), resp
		url = resp.content_uri

		if attachment.type == AttachmentType.image:
			msgtype = "m.image"
		elif attachment.type == AttachmentType.audio:
			msgtype = "m.audio"
		elif attachment.type == AttachmentType.video:
			msgtype = "m.video"
		elif attachment.type == AttachmentType.unknown:
			msgtype = "m.file"
		else:
			raise GmailBridgeException(f"Not Implemented MsgType, {attachment=}")

		content = {
			"body": attachment.name,
			"info": {
				"size": length,
				**info
			},
			"url": url,
			"msgtype": msgtype,
		}
		resp = await self._raw(
			"PUT", f"/rooms/{quote_url(room_id)}/send/m.room.message/{uuid.uuid4()}", user=as_bot, data=content
		)
		return (await resp.json())['event_id']

	async def c_send_msg(
		self,
		room_id: str,
		body: str,
		html: Optional[str] = None,
		info: Optional[Dict] = None,
		as_bot: Optional[str] = None
	) -> str:

		format_info = {}

		if html is not None:
			format_info = {
				"format": "org.matrix.custom.html",
				"formatted_body": html,
			}

		content = {
			**(info or {}),
			"body": body,
			"msgtype": "m.text",
			**(format_info),
		}
		r = await self._raw(
			"PUT", f"/rooms/{quote_url(room_id)}/send/m.room.message/{uuid.uuid4()}", user=as_bot, data=content, strict=False
		)
		if r.status == 413 and ((await r.json())['errcode']) == "M_TOO_LARGE":
			if html is not None:
				self._logger.error(
					"mail too large. Trying without html", mail_id=content.get("gmail_id"), user=as_bot, room_id=room_id
				)
				return await self.c_send_msg(room_id=room_id, body=body, html=None, info=info, as_bot=as_bot)
			else:
				self._logger.error(
					"mail too large. Trimming ...",
					mail_id=content.get("gmail_id"),
					user=as_bot,
					room_id=room_id,
				)
				# TODO: fix the assumption for allowed_width to always be more then 1000
				allowed_width = 1000
				assert len(body) <= allowed_width, f"msg of len {len(body)} char got rejected by server. Fix width in code."
				body = textwrap.shorten(body, width=allowed_width, placeholder=" [... trimmed due to matrix limit]")
				return await self.c_send_msg(room_id=room_id, body=body, html=None, info=info, as_bot=as_bot)

		if not r.ok:
			raise await RequestException.from_aio_resp(r)
		return (await r.json())['event_id']

	async def c_room_get_event(self, room_id: str, event_id: str, as_bot: Optional[str] = None) -> nio.Event:
		self.room_get_event
		r = await self._raw("GET", f"/rooms/{quote_url(room_id)}/event/{quote_url(event_id)}", user=as_bot)
		data = await r.json()
		event = nio.Event.parse_event(data)
		if not isinstance(event, nio.Event):
			raise BadEventException(event)
		return event

	async def set_room_alias(self, room_id: str, room_alias: str, as_bot: Optional[str] = None) -> None:
		await self._raw("PUT", f"/directory/room/{quote_url(room_alias)}", data={"room_id": room_id}, user=as_bot)

	async def get_room_aliases(self, room_id: str) -> List[str]:
		r = await self._raw("GET", f"/rooms/{quote_url(room_id)}/aliases")
		return (await r.json())['aliases']

	async def get_room_name(self, room_id: str) -> str:
		r = await self.room_get_state_event(room_id, "m.room.name")
		assert isinstance(r, nio.RoomGetStateEventResponse)
		return r.content['name']

	async def wait_for_server(self):
		retry = 0
		while True:
			try:
				await self._raw("GET", "/")
				return
			except (aiohttp.ClientConnectorError, aiohttp.ClientConnectionError) as e:
				if retry > 10:
					raise e
				retry += 1
				logger.info("waiting for server", retry=retry)
				await aio.sleep(5)
			except RequestException:
				return True

	async def get_old_events(self, room_id: str, after_event_id: Optional[str] = None, flexible_visibility: bool = False):
		"""
		`flexible_visibility` handles the case where m.room.history_visibility is not 'world_readable' or 'shared'
		in case it can't find the old (after_event_id) event, it'll try to return as many events as it can see. 
		ref: https://spec.matrix.org/v1.2/client-server-api/#room-history-visibility
		"""

		since = None
		rv: List[nio.Event] = []
		found = False
		event_visible = True

		# check visibility
		if flexible_visibility and after_event_id is not None:
			try:
				await self.c_room_get_event(room_id, after_event_id)
			except RequestException as e:
				if not e.is_forbidden():
					raise
				event_visible = False

		while True:
			sync_filter = {
				"room": {
					"rooms": [room_id],
					# "timeline": { }
				},
			}
			params = {
				"user_id": "@_gmail_bridge_nnkitsaini_at_gmail.com:dev.matrix",
				"filter": json.dumps(sync_filter),
			}

			if since is not None:
				params['since'] = since

			r = await self._raw("GET", "/sync", params=params)
			data = await r.json()

			if room_id not in data.get("rooms", {}).get("join", {}):
				# No More visible events
				break

			room_timeline = data['rooms']['join'][room_id]['timeline']
			since = room_timeline['prev_batch']
			events = room_timeline['events']

			for event in reversed(events):
				# matrix events returned in sync don't have room_id
				event['room_id'] = room_id
				if event['event_id'] == after_event_id:
					assert event_visible, "event_visible is false but still got the event, (calculation of event_visible is wrong)"
					found = True
					break

				parsed = nio.Event.parse_event(event)
				if isinstance(parsed, (nio.UnknownBadEvent, nio.BadEvent)):
					logger.warn("Recieved Bad event from server", event_id=event['event_id'], parsed=parsed)
					continue
				rv.append(parsed)

			if found:
				break

		if after_event_id is not None and event_visible:
			if not found:
				logger.error(
					"Can't find event in room sync, even though it's visible",
					event_id=after_event_id,
					room_id=room_id,
				)
				assert found, "See Previous Error Log"

		return list(reversed(rv))

	async def _raw(
		self,
		method: Literal["POST", "GET", "PUT", "DELETE"],
		path: str,
		params: Union[List[Tuple[str, str]], Dict[str, str], None] = None,
		user: Optional[str] = None,
		data: Union[dict, list, str, None] = None,
		strict: bool = True
	) -> aiohttp.ClientResponse:

		if user == self.appservice_id:
			user = None

		if not path.startswith("/"):
			path = "/" + path

		if isinstance(params, dict):
			params = list(params.items())
		if params is None:
			params = []

		if user:
			params.append(("user_id", user))
		if not (data is None or isinstance(data, str)):
			data = json.dumps(data)

		param_str = urlencode(params)
		r = await self.send(
			method,
			f"{REQ_PRE}{path}?{param_str}",
			headers={"Authorization": f"Bearer {self.access_token}"},
			data=data,
		)
		await r.read()
		if strict and not r.ok:
			raise await RequestException.from_aio_resp(r)
		return r


if __name__ == "__main__":
	from .config import get_config

	async def main():
		config = get_config()
		appservice = AppserviceClient(
			homeserver_url=config.HOMESERVER_URL,
			homeserver_name=config.HOMESERVER_NAME,
			namespace_prefix=config.NAMESPACE_PREFIX,
		)

		await appservice.login(config.AS_TOKEN)
		room_id = "!wQdAgeRWUZViEzLJhX:dev.matrix"
		room_id = "!YlvExXvYOXskOshVbV:dev.matrix"
		room_id = "!vNWUKTLZFRNgtMyJJv:dev.matrix"

		self = appservice

		# async def get_events(room_id: str, after_event_id: Optional[str] = None, flexible_visibility: bool = False):
		# 	"""
		# 	flexible_visibility handles the case where m.room.history_visibility is not 'world_readable' or 'shared'
		# 	in case it can't find the old event, it'll try to return as many as it can see.
		# 	ref: https://spec.matrix.org/v1.2/client-server-api/#room-history-visibility
		# 	"""
		# 	event_visible = True
		# 	if flexible_visibility and after_event_id is not None:
		# 		try:
		# 			await self.c_room_get_event(room_id, after_event_id)
		# 		except RequestException as e:
		# 			if not e.is_forbidden():
		# 				raise
		# 			event_visible = False

		# 	since = None

		# 	rv: List[nio.Event] = []
		# 	found = False

		# 	while True:
		# 		sync_filter = {
		# 			"room": {
		# 				"rooms": [room_id],
		# 				# "timeline": { }
		# 			},
		# 		}
		# 		params = {
		# 			"user_id": "@_gmail_bridge_nnkitsaini_at_gmail.com:dev.matrix",
		# 			"filter": json.dumps(sync_filter),
		# 		}

		# 		if since:
		# 			params['since'] = since

		# 		r = await self._raw("GET", "/sync", params=params)
		# 		data = await r.json()
		# 		if room_id not in data.get("rooms", {}).get("join", {}):
		# 			# No More visible events
		# 			break
		# 		room_timeline = data['rooms']['join'][room_id]['timeline']
		# 		since = room_timeline['prev_batch']
		# 		events = room_timeline['events']

		# 		for event in reversed(events):
		# 			if event['event_id'] == after_event_id:
		# 				assert event_visible, "event_visible is false but still got the event, (calculation of event_visible is wrong)"
		# 				found = True
		# 				break

		# 			parsed = nio.Event.parse_event(event)
		# 			if isinstance(parsed, (nio.UnknownBadEvent, nio.BadEvent)):
		# 				logger.warn("Recieved Bad event from server", event_id=event['event_id'], parsed=parsed)
		# 				continue
		# 			rv.append(parsed)

		# 		if found:
		# 			break

		# 	if after_event_id is not None and event_visible:
		# 		if not found:
		# 			logger.error(
		# 				"Can't find event in room sync, even though it's visible",
		# 				event_id=after_event_id,
		# 				room_id=room_id,
		# 			)
		# 			assert found, "See Previous Error Log"

		# 	return rv

		events = await self.get_old_events(room_id, "$9z4StkFDX8JYRusyZN1J8aBteyErjA8N1cL6B-A-N5A", True)
		print(len(events))
		print(events)

		# event_ids = [e.event_id for e in events]
		# rv = []

		# if invite_event_id not in event_ids or join_event_id not in event_ids:
		# 	self._logger.error(
		# 		f"Missed Message Error: Too many message missed. couldn't cover in single sync.",
		# 		invite_even_id=invite_event_id,
		# 		join_even_id=join_event_id,
		# 		all_event_ids=event_ids,
		# 		room_id=room_id,
		# 		bot_user=as_user,
		# 	)
		# 	return []

		# invite_idx = event_ids.index(invite_event_id)
		# join_idx = event_ids.index(join_event_id)

		# for e in events[invite_idx + 1:join_idx]:
		# 	if not isinstance(e, (nio.RoomMessageText, nio.RoomMessageMedia)):
		# 		continue
		# 	rv.append(e)

		# return rv

		# r = await appservice.room_get_state_event(room_id, "m.room.power_levels")
		# assert isinstance(r, nio.RoomGetStateEventResponse)
		# new_content = r.content
		# power_levels = {
		# 	# "@gmail:dev.matrix": 2,
		# 	"@_gmail_bridge_ankit_at_jpqr.com:dev.matrix": 2,
		# }
		# new_content['users'].update(power_levels)

		# # await appservice._raw(
		# # 	"GET",
		# # 	f"/_matrix/client/v3/rooms/{url_quote(room_id)}/state/m.room.power_levels",
		# # )
		# breakpoint()
		# r = await appservice._raw("PUT", f"/rooms/{url_quote(room_id)}/state/m.room.power_levels", data=new_content)
		# # r = await appservice._raw("PUT", f"/_matrix/client/v3/rooms/{url_quote(room_id)}/state/m.room.power_levels", data={})
		# print(r)

	aio.run(main())
