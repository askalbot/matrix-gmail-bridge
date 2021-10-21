from collections import defaultdict
from .log import Logger
from typing import NoReturn, Optional, TypeVar, Union, overload

from .nio_client import NioClient
from structlog import BoundLogger
import nio
from typing import *
from .gmail import GmailClientManager, Gmail, PreparedGmail, GmailTokenException, TokenExpiredException
import json
from dataclasses import dataclass, field
from .db import Db
from .config import BridgeConfig
from .models import LoggedInUser, User
from . import utils as u
from . import utils
import mimetypes
import asyncio as aio
from urllib.parse import quote_plus as quote_url
import html
from typing_extensions import TypeGuard
from .models import *


USER_MXID = str
THROTTLE_DURATION = dt.timedelta(hours=1)


OAUTH_INSTRUCTIONS = """
Usage:
	help - get this help msg
	start - start the oauth flow
	logout - revoke oauth token
	status - get your current oauth status + name + email. (status is one of LoggedOut, LoggedIn or WaitingForToken)
	name ${name} - use ${name} as sender name to send the emails

To use gmail-bridge you'll have to complete an oauth flow.
OAUTH_FLOW:
	NOTE: Oauth room should only have you and the bot.
	1. send "start" in room (without quotes) to start the flow
	2. Bot will send an url. go to that allow the specified scopes and send the token in room.
	3. Bot will send a confirmation if everything went right.
	4. send "logout" anytime to revoke the token.
"""

OAUTH_URL_TEMPLATE = """Visit the following link, select all scopes and send the token in this room.
{url}
"""

def min_time() -> dt.datetime:
	return dt.datetime.fromtimestamp(0)

@dataclass
class EventHandler:
	gclient: GmailClientManager
	nio_client: NioClient
	db: Db
	config: BridgeConfig
	last_error_time: DefaultDict[USER_MXID, dt.datetime] = field(default_factory=lambda: defaultdict(min_time))
	util: utils.MatrixUtility = field(init=False)
	_logger: BoundLogger = field(default_factory=lambda: Logger("Event-Handler"))

	def __post_init__(self):
		self.util = utils.MatrixUtility(self.config.NAMESPACE_PREFIX, self.config.HOMESERVER_NAME)

	async def run_gmail_loop(self) -> NoReturn:
		retry = 0
		while True:
			try:
				async for user, mail in self.gclient.listen_for_mails():
					await self.handle_gmail_message(user, mail)
					user = user.copy()
					user.last_mail_id = mail.gmail_id
					await self.db.upsert_user(user)
				retry = 0
			except Exception as e:
				retry += 1
				self._logger.exception("Gmail Sync Error", retry=retry)
				await aio.sleep(2**retry)

	async def handle_matrix_event(self, event: nio.Event):
		if isinstance(event, nio.RoomMemberEvent):
			await self.m_handle_room_member_event(event)
		elif isinstance(event, (nio.RoomMessageText, nio.RoomMessageMedia)):
			await self.m_handle_room_message_event(event)
		else:
			self._logger.info("Dropping Event", e=type(event), id=event.event_id)

	async def m_handle_room_member_event(self, event: nio.RoomMemberEvent):
		room_id = event.source['room_id']
		if event.membership == "join" and self.util.is_bot_mxid(event.sender): # replay
			if 'replaces_state' not in event.source['unsigned']:
				# joined without invite event, no way to replay
				logger.debug("No Replay, Joined without invite", event_id=event.event_id, joined_using=event.sender)
				return 
			invite_event = await self.nio_client.c_room_get_event(room_id, event.source['unsigned']['replaces_state'], event.sender)
			inviter = invite_event.sender
			if inviter == self.nio_client.user_id or self.util.is_bot_mxid(inviter):
				# no need to replay since the inviter (part of room) is one of appservices bot.
				# so we recieved all of the events after the invite
				logger.debug("No Replay, Different bot already in room", event_id=event.event_id, joined_using=event.sender)
				return 

			# FIXME: currently second bot will try to replay if it was invited by the user
			# possible fix: look at room join/invite history to determine if there was any bot in room
			#    when invite_event was send causing this join.
			join_event = event
			missed_events = await self.get_missed_msg_events(
				room_id,
				event.sender, # Appication Service users can't access sync endpoint, instead have to use virtual user
				invite_event_id=event.source['unsigned']['replaces_state'],
				join_event_id=join_event.event_id,
			)
			if len(missed_events) != 0:
				self._logger.info("missed events, covering now", total=len(missed_events), room=room_id)

			for e in missed_events:
				e.source['room_id'] = room_id
				await self.handle_matrix_event(e)
			return
		if event.membership == "invite":
			if event.sender == self.nio_client.user_id or self.util.is_bot_mxid(event.sender):
				# code that invites should also handle joining
				return
			if event.state_key == self.nio_client.user_id:
				await self.nio_client.c_join(mxid=event.state_key, room_id=room_id)
				await self.nio_client.c_send_msg(room_id, OAUTH_INSTRUCTIONS)
			else:
				if not self.util.is_valid_email_mxid(event.state_key):
					return
				await self.nio_client.c_join(mxid=event.state_key, room_id=room_id)
				await self.nio_client.c_invite_and_join(mxid=self.nio_client.user_id, room_id=room_id, invite_using=event.state_key)
	
	async def _get_bots_in_room(self, room_id: str) -> List[str]:
		users = await self.nio_client.c_room_members(room_id)
		return [u for u in users if self.util.is_bot_mxid(u)]

	async def handle_token_error(self, e: TokenExpiredException, user: User):
		# TODO: send dm to user
		now = dt.datetime.now()
		if now - self.last_error_time[user.matrix_id] > THROTTLE_DURATION:
			self._logger.error("Token Expired, Please logout and login asap.", user=user, err=e, mxid=user.matrix_id)
			self.last_error_time[user.matrix_id] = now
		else:
			self._logger.warning("Throttled Error for expired message.", last_sent=self.last_error_time[user.matrix_id], throttle_dur=THROTTLE_DURATION)
	
	async def get_thread_from_room(self, room_id: str)-> Optional[str]:
		aliases = await self.nio_client.c_get_room_aliases(room_id)
		thread_aliases = [a  for a in aliases if self.util.is_thread_alias(a)]
		assert len(thread_aliases) <=1, f"{room_id} has multiple thread aliases, {thread_aliases=}"
		if len(thread_aliases) != 0:
			return self.util.extract_alias_thread(thread_aliases[0])

		
	async def m_send_mail(self, event: Union[nio.RoomMessageText, nio.RoomMessageMedia], ):
		content = await self.m_event_to_content(event)
		room_id = event.source['room_id']
		user = event.sender
		virtual_users = await self._get_bots_in_room(room_id)

		room_thread: Optional[str] = await self.get_thread_from_room(room_id)

		latest_msg: Optional[nio.RoomMessage] = await self.get_latest_msg(room_id=room_id, senders_in=virtual_users)
		to = cc = None
		if latest_msg is not None:
				# read to, cc from last mail to replicate the gmail-gui behavior
				if "to" in latest_msg.source['content'] and "cc" in latest_msg.source['content']:
					sender = latest_msg.sender
					sender_email = self.util.extract_email(sender)
					to = latest_msg.source['content']['to'] + [sender_email]
					cc = latest_msg.source['content']['cc']

		if to is None or cc is None:
			assert cc is None and to is None
			power_levels = await self.nio_client.c_room_power_levels(room_id)
			to = [u for u, p in power_levels.items() if p == 0 and u != self.nio_client.user_id]
			cc = [u for u, p in power_levels.items() if p == -1 and u != self.nio_client.user_id]
			to = [self.util.extract_email(u) for u in to]
			cc = [self.util.extract_email(u) for u in cc]

		if user not in self.gclient.users:
			self._logger.debug("user not registered", user=user, all_users=list(self.gclient.users))
			await self.nio_client.c_send_msg(room_id, body=OAUTH_INSTRUCTIONS)
			return

		if room_thread is None:
			room_name = await self.nio_client.c_get_room_name(room_id)
			content.subject = room_name
			mail = PreparedGmail(sender=user, to=to, cc=cc, thread_id=room_thread, content=content)
			try:
				thread_id = await self.gclient.send_mail(mail)
			except TokenExpiredException as e:
				await self.handle_token_error(e, user)
				return

			new_alias = self.util.generate_alias(thread_id)
			await self.nio_client.c_set_alias(room_id, new_alias)
		else:
			mail = PreparedGmail(sender=user, to=to, cc=cc, thread_id=room_thread, content=content)
			await self.gclient.send_mail(mail)

	async def m_handle_room_message_event(self, event: Union[nio.RoomMessageText, nio.RoomMessageMedia]):
		if event.sender == self.nio_client.user_id or self.util.is_bot_mxid(event.sender):
			return
		room_id = event.source['room_id']
		user = event.sender
		# users = await self.nio_client.c_room
		users = await self.nio_client.c_room_members(room_id)
		virtual_users = [u for u in users if self.util.is_bot_mxid(u)]

		is_auth_room = len(virtual_users) == 0
		is_mail_room = len(virtual_users) != 0

		if is_mail_room:
			return await self.m_send_mail(event)

		# auth room
		if isinstance(event, nio.RoomMessageMedia):
			await self.nio_client.c_send_msg(room_id, "Invalid Action")
			return

		msg = event.body

		if len(users) != 2:
			await self.nio_client.c_send_msg(
				room_id,
				"for security reasons, please perform oauth in seperate room. with no other users except bot.",
			)
			return
		user = await self.db.get_user(user)

		if msg == "help":
			await self.nio_client.c_send_msg(room_id, OAUTH_INSTRUCTIONS)

		elif msg == "status":
			await self.nio_client.c_send_msg(room_id, f"Auth: \n{user.json(indent=2, exclude={'token', 'matrix_id'})}")

		elif msg.startswith("name"):
			name = msg[len("name "):]
			user.email_name = name
			await self.db.upsert_user(user)
			if isinstance(u:=user.narrow(), LoggedInUser):
				await self.gclient.upsert_user(u)

		elif msg.startswith("email"):
			email = msg[len("email "):]
			user.email_address = email
			await self.db.upsert_user(user)
			if isinstance(u:=user.narrow(), LoggedInUser):
				await self.gclient.upsert_user(u)

		else:
			await self.m_handle_auth_msg(user, room_id, msg)
	
	async def m_handle_auth_msg(self, user: User, room_id: str, msg: str):
		if  user.auth_state== AuthState.logged_out:
			if msg != "start":
				await self.nio_client.c_send_msg(room_id, OAUTH_INSTRUCTIONS)
				return
			url = await self.gclient.oauth_client.get_oauth_flow_url()
			await self.nio_client.c_send_msg(room_id, OAUTH_URL_TEMPLATE.format(url=url))
			user.auth_state = AuthState.waiting_for_token
			await self.db.upsert_user(user)

		elif user.auth_state == AuthState.waiting_for_token:
			try:
				token = await self.gclient.oauth_client.get_access_token(msg)
			except GmailTokenException as e:
				error_msg = str(e) + ": please retry by allowing all scopes"
				await self.nio_client.c_send_msg(room_id, error_msg)
				return
			user = user.logged_in(token)
			await self.db.upsert_user(user)
			await self.gclient.upsert_user(user)
			await self.nio_client.c_send_msg(room_id, f"Login was successful as {token.email}")

		elif user.auth_state == AuthState.logged_in:
			if msg == "logout":
				assert isinstance(user, LoggedInUser)
				user = user.logged_out()
				await self.db.upsert_user(user)
				await self.gclient.remove_user(user.matrix_id)
				await self.nio_client.c_send_msg(room_id, f"Logout was successful")
			else:
				await self.nio_client.c_send_msg(room_id, OAUTH_INSTRUCTIONS)

		else:
			raise NotImplementedError(user.auth_state)


	async def handle_gmail_message(self, user: LoggedInUser, mail: Gmail):

		thread = mail.thread_id
		room_alias = self.util.generate_alias(thread)
		mail = mail.without(user.email_address)
		sender_mxid = self.util.generate_mxid(mail.sender)

		bots = [self.util.generate_mxid(e) for e in mail.to + mail.cc if e != user.email_address]
		bots.append(sender_mxid)
		powers = {self.util.generate_mxid(e): -1 for e in mail.cc if e != user.email_address}
		powers.update({self.util.generate_mxid(e): 0 for e in mail.to if e != user.email_address})

		room_id = await self.nio_client.c_room_resolve_alias(room_alias)
		await aio.gather(*[self.nio_client.c_ensure_appservice_user(m) for m in bots])


		if room_id is None:
			r = await self.nio_client.room_create(invite=[user.matrix_id]+bots, power_level_override=powers, alias=self.util.extract_localpart(room_alias), name=mail.content.subject)
			assert isinstance(r, nio.RoomCreateResponse), r
			for b in bots:
				await self.nio_client.c_join(r.room_id, mxid=b)
			room_id = r.room_id
		
		
		room_members = await self.nio_client.c_room_members(room_id)
		for a in bots:
			if a not in room_members:
				await self.nio_client.c_ensure_appservice_user(a)
				await self.nio_client.c_invite_and_join(a, room_id=room_id)
				# TODO: set power levels ?

		by = self.util.generate_mxid(mail.sender)
		attachement_msg_ids = await self.send_attachements(mail, user=by, room=room_id)
		info = {"to": mail.to, "cc": mail.cc, "message_id": mail.gmail_id, "attachemnt_ids": attachement_msg_ids}
		await self.nio_client.c_send_msg(room_id=room_id, info=info, as_user=by, body=mail.content.body, html=mail.content.html_body)

	async def send_attachements(self, mail: Gmail, user: str, room: str) -> List[str]:
		rv = []
		for a in mail.content.attachment:
			rv.append(await self.nio_client.c_send_attachement(room, user, a, info={
				"to": mail.to,
				"cc": mail.cc,
				"mail_id": mail.gmail_id
			}))
		return rv

	async def m_event_to_content(self, msg: nio.RoomMessage, room_name: Optional[str] = None) -> MsgContent:
		if isinstance(msg, nio.RoomMessageText):
			body = msg.body
			formatted = msg.formatted_body
			if formatted is None:
				formatted = f"<div>{html.escape(body)}<div>"
			return MsgContent(body=body, html_body=formatted, subject=room_name)
		elif isinstance(msg, nio.RoomMessageMedia):
			name = msg.body
			mime_type = self.guess_mime(msg)
			media_id = msg.url.split("/")[-1]
			r = await self.nio_client.download(self.nio_client.homeserver_name, media_id)
			assert isinstance(r, nio.DownloadResponse), r
			attachment = Attachment(mime_type=mime_type, content=r.body, name=name)
			return MsgContent(body=name, html_body=f"<div>{html.escape(name)}</div>", attachment=[attachment], subject=room_name)

		else:
			raise GmailBridgeException(f"Unsupported msg type, {msg=}")

	@classmethod
	def guess_mime(cls, msg: nio.RoomMessageMedia) -> str:
		if 'mimetype' in msg.source['content'].get('info', {}):
			return msg.source['content']['info']['mimetype']

		if isinstance(msg, nio.RoomMessageImage):
			mime_by_type = "image/*"
		elif isinstance(msg, nio.RoomMessageAudio):
			mime_by_type = "audio/*"
		elif isinstance(msg, nio.RoomMessageVideo):
			mime_by_type = "video/*"
		elif isinstance(msg, nio.RoomMessageFile):
			mime_by_type = "application/octet-stream"
		else:
			raise GmailBridgeException(f"Unknown Media Message, {msg=}")
		mime_type_by_name, _ = mimetypes.guess_type(msg.body)
		if mime_type_by_name is None:
			return mime_by_type

		main_mime_by_name = mime_type_by_name.split("/")[0]
		if mime_by_type.startswith(main_mime_by_name):
			return main_mime_by_name
		else:
			return mime_by_type

	async def get_latest_msg(self, room_id: str, senders_in: List[str] = []) -> Optional[nio.RoomMessage]:
		params = {
			"dir": "b",
			"limit": 1,
			"filter": json.dumps({
				"senders": senders_in,
				"types": ['m.room.message'],
			}),
		}
		r = await self.nio_client.raw("GET", f"/rooms/{quote_url(room_id)}/messages", params=params)
		events = (await r.json())['chunk']
		if len(events) == 0:
			return None
		rv = nio.RoomMessage.parse_event(events[0])
		assert isinstance(rv, nio.RoomMessage)
		return rv

	async def get_missed_msg_events(
		self,
		room_id: str,
		as_user: str,
		invite_event_id: str,
		join_event_id: str,
	) -> List[Union[nio.RoomMessageMedia, nio.RoomMessageText]]:
		# TODO: sync whole room, then return events to remove 100 event hack
		"""
		looks at max 100 events
		"""
		limit = 100
		sync_filter = {
			"room": {
				"rooms": [room_id],
				"timeline": {
					"limit": limit,
					"types": ['m.room.message', 'm.room.member'],
				}
			},
		}
		r = await self.nio_client.raw("GET", "/sync", params={
			"user_id": as_user,
			"filter": json.dumps(sync_filter),
		})
		events = (await r.json())['rooms']['join'][room_id]['timeline']['events']
		events = [nio.Event.parse_event(e) for e in events]
		events = [e for e in events if not isinstance(e, nio.UnknownBadEvent)]

		event_ids = [e.event_id for e in events]
		rv = []

		if invite_event_id not in event_ids or join_event_id not in event_ids:
			self._logger.error(
				f"Missed Message Error: Too many message missed. couldn't cover in single sync.",
				invite_even_id=invite_event_id,
				join_even_id=join_event_id,
				all_event_ids=event_ids,
				room_id=room_id,
				bot_user=as_user,
			)
			return []

		invite_idx = event_ids.index(invite_event_id)
		join_idx = event_ids.index(join_event_id)

		for e in events[invite_idx+1:join_idx]:
			if not isinstance(e, (nio.RoomMessageText, nio.RoomMessageMedia)):
				continue
			rv.append(e)

		return rv
