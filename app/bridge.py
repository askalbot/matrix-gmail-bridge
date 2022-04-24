import asyncio as aio
import html
import mimetypes
import re
from dataclasses import dataclass, field
from typing import *

import nio
import structlog

from . import utils as u
from .db import Db
from .gmail import (Gmail, GmailClient, GmailTokenException, GoogleAuth, TokenExpiredException)
from .log import Logger
from .models import *
from .models import LoggedInUser, User
from .nio_client import AppserviceClient, RequestException

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


"""
on_event:
	if event.sender in USERS:
		await USERS[event.sender].handle_matrix_event(event)
	else:
		USERS[event.sender] = UserBridge(user: user, db, nc, gm=None)
		USERS[event.sender].handle_matrix_event(event)
"""


@dataclass
class UserBridge:
	user: 'User'
	db: Db
	nc: AppserviceClient
	gmail_service_key: ServiceKey
	gm: Optional[GmailClient] = None
	_logger: structlog.BoundLogger = field(init=False)

	def __post_init__(self):
		self._logger = Logger('user_bridge')
		self._logger.bind(user_id=self.user.matrix_id)

	@classmethod
	async def new(cls, db: Db, nio_client: AppserviceClient, user: 'User', gmail_service_key: ServiceKey) -> 'UserBridge':
		gmail_client = None
		if user.token is not None:
			gmail_client = await GmailClient.new(
				user.token,
				service_key=gmail_service_key,
				email_address=user.email_address,
				email_name=user.email_name,
			)
		return cls(
			user,
			db,
			nio_client,
			gm=gmail_client,
			gmail_service_key=gmail_service_key,
		)

	async def handle_matrix_event(self, event: nio.Event):
		"""
			we have to handle:
			- Join/Invite events
			- Msgs in Oauth rooms
			- Msgs in thread room

		"""
		if await self.db.event_exists(event.event_id):
			logger.debug("Event Already Processed. Ignoring.", event_id=event.event_id, event=event)
			return

		self._logger.info("New Matrix event", event_id=event.event_id)
		if isinstance(event, nio.RoomMemberEvent):
			if event.membership == "invite":
				await self._accept_room_invite(event)
				if self._is_valid_bot_mxid(event.state_key):
					await self._replay_matrix_events(
						since_event_id=event.event_id,
						room_id=event.source['room_id'],
					)

		elif isinstance(event, (nio.RoomMessageText, nio.RoomMessageMedia)):
			room_id = event.source['room_id']
			members = await self.nc.get_room_members(room_id)

			other_users = []
			bots = []
			appservice_in_room = False
			for member in members:
				if member == self.nc.appservice_id:
					appservice_in_room = True
				elif self.nc.is_our_bot(member):
					bots.append(member)
				else:
					other_users.append(member)

			if len(other_users) > 1:
				await self.nc.c_send_msg(
					room_id, 'Bridge only supports one regular user per room. Please try in a room with no additiona user.'
				)
				return

			is_dm = len(bots) == 0
			if is_dm:
				if isinstance(event, nio.RoomMessageMedia):
					await self.nc.c_send_msg(room_id, 'Sorry, bridge does not support media messages in dm.')
					await self.nc.c_send_msg(room_id, OAUTH_INSTRUCTIONS)
				else:
					await self._handle_dm(event)
			else: # a thread room
				if not appservice_in_room:
					logger.error(
						f"Bot exists in room but Appservice Missing from room. Bot should've invited appservice.",
						room_id=room_id,
						bots=bots
					)
					await self.nc.invite_and_join_room(mxid=self.nc.appservice_id, room_id=room_id, as_bot=bots[0])
				try:
					await self._handle_thread_msg(event)
				except TokenExpiredException:
					await self.nc.c_send_msg(
						room_id,
						"Your token has expired (most prob it has been revoked). You'll be logged out now. Log back in to keep using bridge.",
					)
					await self._logout_user()

		else:
			self._logger.info("Dropping Event", e=type(event), id=event.event_id)
		await self.db.add_event(event.event_id)

	async def sync_gmails(self):
		try:
			await self._sync()
		except TokenExpiredException:
			logger.error("Token Expired for user. Logging them out.", exc_info=True, user=self.user)
			await self._logout_user()

	async def _sync(self):
		if self.gm is None:
			return
		logger.info("Syncing Gmail", user_id=self.user.matrix_id, after_mail_id=self.user.last_mail_id)
		async for gmail in self.gm.get_new_mails(
			after_mail_id=self.user.last_mail_id,
			after_time=dt.datetime.now() - dt.timedelta(days=1),
		):
			try:
				await self._handle_mail(gmail)
			except RequestException as e:
				# if forbidden then inform the user
				if not e.is_forbidden():
					raise
				room_id = await self._get_room_id_from_thread(gmail.thread_id)
				assert room_id is not None, f"Could not find existing room for thread {gmail.thread_id}. And still got ForbiddenException.{e=}"
				logger.info("Permission Error", exc_info=True)
				await self.nc.c_send_msg(
					room_id,
					f"The Bot doesn't have required permissions to work. Please provide atleast `state` and `event` permissions\n 50 should work in default cases or just set as `admin`",
				)
				break
			self.user.last_mail_id = gmail.gmail_id
			await self.db.upsert_user(self.user)

		self.user.token = self.gm.get_user_token()
		await self.db.upsert_user(self.user)

	async def close(self):
		if self.gm:
			await self.gm.close()

	async def _get_thread_from_room(self, room_id: str) -> Optional[str]:
		aliases = await self.nc.get_room_aliases(room_id)
		thread_aliases = [a for a in aliases if self.nc.is_our_alias(a)]
		if len(thread_aliases) > 1:
			assert len(thread_aliases) <= 1, f"{room_id} has multiple thread aliases, {thread_aliases=}"
			logger.error(f"Multiple Thread Aliases", aliases=aliases, room_id=room_id)
			return self._extract_alias_thread(thread_aliases[0])
		elif len(thread_aliases) == 1:
			return self._extract_alias_thread(thread_aliases[0])
		else:
			return None

	async def _handle_thread_msg(
		self,
		event: 'nio.RoomMessageText | nio.RoomMessageMedia',
	):
		room_id = event.source['room_id']
		user = event.sender
		content = await self._mevent_to_content(event)
		room_thread = await self._get_thread_from_room(room_id)

		power_levels = await self.nc.get_room_power_levels(room_id)
		bots = [b for b in power_levels.keys() if self.nc.is_our_bot(b)]
		to = [b for b in bots if power_levels[b] == 0]
		cc = [b for b in bots if power_levels[b] == 1]
		to = [self._extract_bot_email(u) for u in to]
		cc = [self._extract_bot_email(u) for u in cc]

		if self.gm is None:
			self._logger.warn("user not registered", user=user)
			await self.nc.c_send_msg(room_id, body=OAUTH_INSTRUCTIONS)
			return

		if room_thread is None:
			room_name = await self.nc.get_room_name(room_id)
			content.subject = room_name
			thread_id = await self.gm.start_new_thread(content, to=to, cc=cc)

			new_alias = self._generate_room_alias(thread_id)
			await self.nc.set_room_alias(room_id, new_alias)
		else:
			await self.gm.reply_to_thread(thread_id=room_thread, content=content, to=to, cc=cc)

	async def _mevent_to_content(self, msg: nio.RoomMessage, room_name: Optional[str] = None) -> MsgContent:
		if isinstance(msg, nio.RoomMessageText):
			body = msg.body
			formatted = msg.formatted_body
			if formatted is None:
				formatted = f"<div>{html.escape(body)}<div>"
			return MsgContent(body=body, html_body=formatted, subject=room_name)
		elif isinstance(msg, nio.RoomMessageMedia):
			name = msg.body
			mime_type = self._guess_mime(msg)
			hs_name, media_id = self.nc.parse_media_url(msg.url)
			r = await self.nc.download(hs_name, media_id)
			assert isinstance(r, nio.DownloadResponse), r
			attachment = Attachment(mime_type=mime_type, content=r.body, name=name)
			return MsgContent(
				body=name,
				html_body=f"<div>{html.escape(name)}</div>",
				attachment=[attachment],
				subject=room_name,
			)

		else:
			raise GmailBridgeException(f"Unsupported msg type, {msg=}")

	@classmethod
	def _guess_mime(cls, msg: nio.RoomMessageMedia) -> str:
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

	async def _handle_dm(self, event: nio.RoomMessageText):
		msg = event.body.strip().lower()
		room_id = event.source['room_id']
		gauth = GoogleAuth(self.gmail_service_key)
		## BASIC ACTIONS
		if msg.lower() == "status":
			json_status = self.user.json(indent=2, exclude={'token'})
			await self.nc.c_send_msg(room_id, f"Auth: \n{json_status}", html=f"Auth: <pre>{html.escape(json_status)}</pre>")
			return

		if (m := re.match(r"name (?P<name>.*)", msg)) is not None:
			self.user.email_name = m.group("name")
			await self.db.upsert_user(self.user)
			await self._restart_gmail_client()
			await self.nc.c_send_msg(room_id, f"Name Set to \"{m.group('name')}\"")
			return

		if (m := re.match(r"email (?P<email>.*)", msg)) is not None:
			self.user.email_address = m.group("email")
			await self.db.upsert_user(self.user)
			await self._restart_gmail_client()
			await self.nc.c_send_msg(room_id, f"Email Set to \"{m.group('email')}\"")
			return

		## AUTH ACTIONS
		if self.user.auth_state == AuthState.logged_out:
			if msg != "start":
				await self.nc.c_send_msg(room_id, OAUTH_INSTRUCTIONS)
				return
			url = await gauth.get_oauth_flow_url()
			await self.nc.c_send_msg(room_id, OAUTH_URL_TEMPLATE.format(url=url))
			self.user.auth_state = AuthState.waiting_for_token
			await self.db.upsert_user(self.user)

		elif self.user.auth_state == AuthState.waiting_for_token:
			try:
				token = await gauth.get_access_token(
					event.body.strip()
				) # use original body here, instead of `msg` to avoid any token manipulation
			except GmailTokenException as e:
				logger.warn("Exception while converting user provided token to access token", exc_info=True)
				error_msg = str(e) + ": please retry by allowing all scopes"
				await self.nc.c_send_msg(room_id, error_msg)
				return
			self.user = self.user.logged_in(token)
			await self.db.upsert_user(self.user)
			assert self.gm is None, f"User Not logged in but Gmail Client is running., {self.user.matrix_id=}"
			self.gm = await GmailClient.new(
				self.user.token,
				service_key=self.gmail_service_key,
				email_address=self.user.email_address,
				email_name=self.user.email_name,
			)
			await self.nc.c_send_msg(room_id, f"Login was successful as {token.email}")

		elif self.user.auth_state == AuthState.logged_in:
			if msg == "logout":
				await self._logout_user()
				await self.nc.c_send_msg(room_id, f"Logout was successful")
			else:
				await self.nc.c_send_msg(room_id, OAUTH_INSTRUCTIONS)

		else:
			raise NotImplementedError(self.user.auth_state)

	async def _logout_user(self):
		assert isinstance(self.user, LoggedInUser)
		assert self.gm is not None, f"User logged in but Gmail Client is not running, {self.user.matrix_id=}"
		self.user = self.user.logged_out()
		await self.db.upsert_user(self.user)
		await self.gm.close()
		self.gm = None

	async def _accept_room_invite(self, event: nio.RoomMemberEvent):
		room_id = event.source['room_id']
		if event.membership != "invite":
			return

		if self.nc.is_our_mxid(event.sender):
			# code that invites should also handle joining in case of virtual bots
			return

		if event.state_key == self.nc.appservice_id:
			await self.nc.join_room(room_id=room_id)
		else:
			if not self._is_valid_bot_mxid(event.state_key):
				return
			await self.nc.join_room(as_bot=event.state_key, room_id=room_id)
			await self.nc.invite_and_join_room(mxid=self.nc.appservice_id, room_id=room_id, as_bot=event.state_key)

	async def _replay_matrix_events(self, room_id: str, since_event_id: str):
		for event in await self.nc.get_old_events(room_id, since_event_id, flexible_visibility=True):
			await self.handle_matrix_event(event)

	def _is_valid_bot_mxid(self, mxid: str) -> bool:
		if not self.nc.is_our_mxid(mxid):
			return False
		localpart = u.extract_mxid_localpart(mxid)
		email = u.try_email_desanitize(localpart)
		if email is None:
			return False
		return True

	async def _restart_gmail_client(self):
		if self.gm is None:
			return
		await self.gm.close()
		assert self.user.token is not None, f"Can't start gmail_client without token, {self.user.matrix_id=}"
		self.gm = await GmailClient.new(
			self.user.token,
			service_key=self.gmail_service_key,
			email_address=self.user.email_address,
			email_name=self.user.email_name,
		)

	async def _get_room_id_from_thread(self, thread_id: str) -> Optional[str]:
		room_alias = self._generate_room_alias(thread_id)
		return await self.nc.resolve_room_alias(room_alias)

	async def _handle_mail(self, mail: Gmail):
		"""
			
			Every Gmail Thread is represented as a Matrix room.
			Every `cc` is room member with power_level == -1
			Every `to` is room member with power_level == 0
			`sender` is room member with power_level == 0

			-------
			Every attachement is seperate msg
		"""

		####  Room Setup
		room_alias = self._generate_room_alias(mail.thread_id)
		room_id = await self.nc.resolve_room_alias(room_alias)

		powers = self._get_bot_power_levels(mail.cc, mail.to, mail.sender)
		bots = list(powers.keys())

		await aio.gather(*[self.nc.ensure_virtual_user(m) for m in bots])

		if room_id is None:
			r = await self.nc.room_create(
				invite=[self.user.matrix_id] + bots,
				power_level_override=powers,
				alias=u.extract_localpart(room_alias),
				name=mail.content.subject,
			)
			assert isinstance(r, nio.RoomCreateResponse), r
			for b in bots:
				await self.nc.join_room(r.room_id, as_bot=b)
			room_id = r.room_id
		else:
			for b in bots:
				await self.nc.invite_and_join_room(b, room_id)
			await self.nc.set_room_power_levels(room_id, power_levels=powers)

		sender_mxid = self._generate_bot_mxid(mail.sender)

		attachement_metadata = {"to": mail.to, "cc": mail.cc, "mail_id": mail.gmail_id, "sender": mail.sender}

		### Attachements
		attachement_msg_ids = []
		for a in mail.content.attachment:
			attachement_msg_ids.append(await self.nc.c_send_attachement(
				room_id,
				sender_mxid,
				a,
				info=attachement_metadata,
			))

		### Body
		msg_metadata = {**attachement_metadata, "attachement_ids": attachement_msg_ids}
		await self.nc.c_send_msg(
			room_id=room_id,
			info=msg_metadata,
			as_bot=sender_mxid,
			body=mail.content.body,
			html=mail.content.html_body,
		)

	def _generate_room_alias(self, thread_id: str) -> str:
		assert isinstance(self.user, LoggedInUser)
		thread_and_email = thread_id + "." + u.email_sanitize(self.user.email_address)
		return self.nc.generate_room_alias(thread_and_email)

	def _extract_alias_thread(self, alias: str) -> str:
		thread_and_email = self.nc.extract_alias_name(alias)
		return thread_and_email.split(".")[0]

	def _generate_bot_mxid(self, email_address: str) -> str:
		return self.nc.generate_bot_mxid(u.email_sanitize(email_address))

	def _extract_bot_email(self, mxid: str) -> str:
		senatized_mail = self.nc.extract_bot_name(mxid)
		return u.email_desanitize(senatized_mail)

	def _get_bot_power_levels(self, cc: List[str], to: List[str], sender: str) -> Dict[str, int]:
		powers = {self._generate_bot_mxid(e): -1 for e in cc if e != self.user.email_address}
		powers.update({self._generate_bot_mxid(e): 0 for e in to + [sender] if e != self.user.email_address})
		return powers
