import aiohttp
from .log import Logger
from nio import AsyncClient
import nio
from typing import *
import textwrap
import uuid

from nio.api import Api
import json
from . import utils as u
from urllib.parse import quote_plus as quote_url, urlencode
from .prelude import *
from .models import Attachment, AttachmentType

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

class BadEventException(Exception):
	def __init__(self, event: nio.BadEventType):
		self.event = event
		super().__init__(f"bad event, {event=}")


class NioClient(AsyncClient):
	async def appservice_login(self, access_token: str, appservice_id: str):
		self.access_token = access_token
		r = await self.whoami()
		assert isinstance(r, nio.responses.WhoamiResponse), r
		self.user_id = r.user_id

	def __init__(self,
		homeserver_url: str,
		homeserver_name: str,
		user: str = "",
		device_id: Optional[str] = "",
		store_path: Optional[str] = "",
		config: Optional[nio.AsyncClientConfig] = None,
		ssl: Optional[bool] = None,
		proxy: Optional[str] = None,
	):
		super().__init__(
			homeserver_url,
			user=user,
			device_id=device_id,
			store_path=store_path,
			config=config,
			ssl=ssl,
			proxy=proxy,
		)
		self.homeserver_name = homeserver_name
		self._logger = Logger("matrix-client")

	async def c_room_resolve_alias(self, room_alias: str) -> Optional[str]:
		r = await self.room_resolve_alias(room_alias)
		if isinstance(r, nio.RoomResolveAliasError):
			return None
		else:
			return r.room_id

	async def raw(
		self,
		method: Literal["POST", "GET", "PUT", "DELETE"],
		path: str,
		params: Union[List[Tuple[str, str]], Dict[str, str], None] = None,
		user: Optional[str] = None,
		data: Union[dict, list, str, None] = None,
		check: bool = True
	) -> aiohttp.ClientResponse:
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
		if check and not r.ok:
			raise await RequestException.from_aio_resp(r)
		return r

	async def c_join(self, room_id: str, mxid: Optional[str] = None):
		if mxid is None:
			mxid = self.user_id
		self._logger.debug("join room", mxid=mxid, room_id=room_id)
		await self.raw("POST", f"/join/{quote_url(room_id)}", user=mxid)

	async def c_invite_and_join(self, mxid: str, room_id: str, invite_using: Optional[str] = None):
		if invite_using is None:
			invite_using = self.user_id
		r = await self.raw("POST", f"/rooms/{room_id}/invite", user=invite_using, data={"user_id": mxid}, check=False)
		if r.status == 403 and (await r.json())['errcode'] == "M_FORBIDDEN":
			try:
				await self.c_join(room_id, mxid)
			except RequestException as e:
				if e.err_code() != "M_FORBIDDEN":
					raise e
				# already in room
			return

		r = await self.c_join(room_id, mxid)
		assert not isinstance(r, nio.ErrorResponse), r

	async def c_user_exists(self, mxid: str) -> bool:
		resp = await self.get_profile(mxid)
		if isinstance(resp, nio.ProfileGetResponse):
			return True
		elif resp.status_code == "M_NOT_FOUND":
			return False
		raise GmailBridgeException(f"Unexpected Response from Nio Library {resp=}")

	async def c_ensure_appservice_user(self, mxid: str):
		assert u.is_bot_mxid(mxid)

		if await self.c_user_exists(mxid):
			return

		localpart = u.extract_mxid_localpart(mxid)
		content = {
			"type": "m.login.application_service",
			# "@test:localhost" -> "test" (Can't register with a full mxid.)
			"username": localpart,
		}
		self._logger.debug("create user", content=content)
		await self.raw("POST", "/register", data=content)

	async def c_room_power_levels(self, room_id: str) -> Dict[str, int]:
		users = await self.c_room_members(room_id)
		r = await self.room_get_state_event(room_id, "m.room.power_levels")
		assert isinstance(r, nio.RoomGetStateEventResponse)
		powers: Dict[str, int] = r.content['users']
		for u in users:
			if u not in powers:
				powers[u] = 0

		rv = {u: powers[u] for u in users}
		assert rv == powers
		# TODO: remove assert check

		return powers

	async def c_room_members(self, room_id: str, as_user: Optional[str] = None) -> List[str]:
		if as_user is None:
				as_user = self.user_id
		r = await self.raw("GET", f"/rooms/{quote_url(room_id)}/joined_members", user=as_user)
		resp = await r.json()
		return list(resp['joined'])


	async def c_send_attachement(self, room_id: str, as_user: str, attachment: Attachment, info: Optional[Dict] = None) -> str:
		""" returns msg id """
		length = len(attachment.content)
		r = await self.upload(lambda a, b: attachment.content_io(), attachment.mime_type)
		assert isinstance(r, nio.UploadResponse), r
		url = r.content_uri
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
				**(info or {})
			},
			"url": url,
			"msgtype": msgtype,
		}
		r = await self.raw("PUT", f"/rooms/{quote_url(room_id)}/send/m.room.message/{uuid.uuid4()}", user=as_user)
		return (await r.json())['event_id']

	async def c_send_msg(self, room_id: str, body: str, html: Optional[str]=None, info: Optional[Dict] = None, as_user: Optional[str] = None) -> str:
		if as_user is None:
			as_user = self.user_id

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
		r = await self.raw("PUT",f"/rooms/{quote_url(room_id)}/send/m.room.message/{uuid.uuid4()}", user=as_user, data=content, check=False)
		if r.status==413 and ((await r.json())['errcode'])== "M_TOO_LARGE":
			if html is not None:
				self._logger.error(
					"mail too large. Trying without html",
					mail_id=content.get("gmail_id"), user=as_user, room_id=room_id
				)
				return await  self.c_send_msg(room_id=room_id, body=body, html=None, info=info, as_user=as_user)
			else:
				self._logger.error(
					"mail too large. Trimming ...",
					mail_id=content.get("gmail_id"), user=as_user, room_id=room_id
				)
				# TODO: fix the assumption for allowed_width to always be more then 1000
				allowed_width = 1000
				assert len(body) <= allowed_width, f"msg of len {len(body)} char got rejected by server. Fix width in code."
				body= textwrap.shorten(body, width=1000, placeholder=" [... trimmed due to matrix limit]")
				return await self.c_send_msg(room_id=room_id, body= body , html=None, info=info, as_user=as_user)

		if not r.ok:
			raise await RequestException.from_aio_resp(r)
		return (await r.json())['event_id']

	async def c_room_get_event(self, room_id: str, event_id: str, as_user: Optional[str] = None) -> nio.Event:
		if as_user is None:
			as_user = self.user_id
		self.room_get_event
		r = await self.raw("GET", f"/rooms/{quote_url(room_id)}/event/{quote_url(event_id)}", user=as_user)
		data = await r.json()
		event =  nio.Event.parse_event(data)
		if not isinstance(event, nio.Event):
			raise BadEventException(event)
		return event

	async def c_get_room_aliases(self, room_id: str) -> List[str]:
		r = await self.raw("GET", f"/rooms/{quote_url(room_id)}/aliases")
		return (await r.json())['aliases']

	async def c_get_room_name(self, room_id: str) -> str:
		r = await self.room_get_state_event(room_id, "m.room.name")
		assert isinstance(r, nio.RoomGetStateEventResponse)
		return r.content['name']

	async def c_set_alias(self, room_id: str, room_alias: str):
		await self.raw("PUT", f"/directory/room/{quote_url(room_alias)}", data={"room_id": room_id})