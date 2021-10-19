from __future__ import annotations
from typing import Any, TypeVar

from httpx_oauth.errors import GetIdEmailError

import base64
import codecs
import html
import pickle
from structlog import BoundLogger
from email import encoders
from pydantic import Field
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import AsyncGenerator, Callable, List, Optional, Generic
import aiogoogle as _aiogoogle
import mailparser
from .log import Logger
from bs4 import BeautifulSoup
from httpx_oauth.oauth2 import GetAccessTokenError, OAuth2, RefreshTokenError, RevokeTokenError
from httpx_oauth.clients.google import GoogleOAuth2, PROFILE_ENDPOINT

from app.utils import NamedTempFile

from .config import CONFIG
from .models import Attachment, LoggedInUser, MsgContent, AuthState, User, Token
from . import utils as u
from .prelude import *
from dataclasses import InitVar
from pydantic import BaseModel

ACCEPT_DELAY = dt.timedelta(hours=1) # naming?
MatrixUserId = str


class GmailTokenException(Exception):
	pass


class GmailUserNotRegistered(Exception):
	pass


# TODO: remove global
SERVICE_KEY = CONFIG.get_service_key()

_T = TypeVar("_T", bound='_BaseGmail')


class _BaseGmail(BaseModel, Generic[_T]):
	sender: str
	to: List[str]
	content: MsgContent
	thread_id: Optional[str] = None
	gmail_id: Optional[None] = None
	cc: List[str] = Field(default_factory=list)

	def without(self: _T, email_address: str) -> _T:
		assert email_address != self.sender, "Can't remove sender"
		new_mail = self.copy()
		new_mail.to = [t for t in self.to if t != email_address]
		new_mail.cc = [t for t in self.cc if t != email_address]
		return new_mail


class PreparedGmail(_BaseGmail):
	gmail_id: None = None


class Gmail(_BaseGmail):
	gmail_id: str
	thread_id: str


@dataclass
class GmailClient:
	last_mail_id: str
	email_id: str
	service: _aiogoogle.GoogleAPI
	ag: _aiogoogle.Aiogoogle
	email_name: Optional[str] = None
	last_sync_time: dt.datetime = field(default_factory=lambda: dt.datetime.now() - ACCEPT_DELAY)

	_new_mail_lock: aio.Lock = field(default_factory=aio.Lock)
	_logger: BoundLogger = field(default_factory=lambda: Logger("gmail-client"))

	async def close(self):
		if self.ag.active_session is not None:
			await self.ag.active_session.close()

	@classmethod
	async def from_user(cls, user_state: LoggedInUser) -> 'GmailClient':
		ag = _aiogoogle.Aiogoogle(
			user_creds={
				"access_token": user_state.token.access_token,
				"refresh_token": user_state.token.refresh_token,
			},
			client_creds={
				"client_id": SERVICE_KEY.client_id,
				"client_secret": SERVICE_KEY.client_secret,
			}
		)
		service = await ag.discover('gmail', 'v1')

		return GmailClient(
			last_mail_id=user_state.last_mail_id or "0",
			email_id=user_state.email_address,
			email_name=user_state.email_name,
			service=service,
			ag=ag,
		)

	@staticmethod
	def _parse_msg_body(parsed_mail: mailparser.MailParser, with_gmail_quote: bool = True) -> Tuple[str, str]:
		""" returns body, html_body """
		# TODO: handle more cases
		if len(parsed_mail.text_plain) == 0 and len(parsed_mail.text_html) == 0:
			return "", "<div></div>"
		elif len(parsed_mail.text_plain) == 0:
			html_body = parsed_mail.text_html[0]
			body = BeautifulSoup(html_body, 'html.parser').get_text()
		elif len(parsed_mail.text_html) == 0:
			body = parsed_mail.text_plain[0]
			html_body = f"<div>{html.escape(body)}<div>"
		else:
			body = parsed_mail.text_plain[0]
			html_body = parsed_mail.text_html[0]

		if with_gmail_quote:
			return body, html_body

		html_body_soup = BeautifulSoup(html_body, 'html.parser')
		for div in html_body_soup.find_all("div", {'class': 'gmail_quote'}):
			# As we are recieving the mail, there can be all sorts of `foo_quote` but
			# `gmail_quote` should cover more then half of the cases
			div.decompose() # type: ignore
		return html_body_soup.get_text(), str(html_body_soup)

	def _parse_msg(self, api_msg: dict, parsed_mail: mailparser.MailParser) -> Gmail:
		subject: str = parsed_mail.subject # type: ignore

		def _parse_mail(m) -> str:
			if isinstance(m, str): # foo@bar.com
				return m
			if len(m) == 1: # [foo@bar.com]
				return m[0]
			if len(m) > 1: # [foo_name, foo@bar.com]
				return m[1]
			raise GmailBridgeException(f"Unknown mail format, {m=}, {type(m)=}")

		email = _parse_mail(parsed_mail.from_[0])
		cc = [_parse_mail(e) for e in parsed_mail.cc]
		to = [_parse_mail(e) for e in parsed_mail.to]
		body, html_body = self._parse_msg_body(parsed_mail, False)
		attachments = []
		for att in parsed_mail.attachments:
			name = att['filename']
			mime = att['mail_content_type']
			is_inline = att['content-disposition'].startswith("inline;")
			id = att['content-id'].replace("<", "").replace(">", "")
			if f"[image: {name}]" not in body and id not in html_body and is_inline:
				continue
			if att['binary']:
				content = codecs.decode(att['payload'].encode(), att['content_transfer_encoding'])
			else:
				content = att['payload'].encode()
			attachments.append(Attachment(mime_type=mime, content=content, name=name))

		return Gmail(
			gmail_id=api_msg['id'],
			thread_id=api_msg['threadId'],
			sender=email,
			to=to,
			cc=cc,
			content=MsgContent(body=body, html_body=html_body, attachment=attachments, subject=subject),
		)

	async def _send(self, message: Union[MIMEText, MIMEMultipart], thread_id: Optional[str] = None):
		body = {}
		if thread_id is not None:
			body['threadId'] = thread_id

		if isinstance(message, MIMEText):
			encoded_message = base64.urlsafe_b64encode(message.as_bytes())
			body['raw'] = encoded_message.decode()
			req = self.service.users.messages.send( # type: ignore
				userId='me', # type: ignore
				json=body, # type: ignore
			)
			return await self.ag.as_user(req)

		with NamedTempFile() as path:
			path.write_bytes(message.as_bytes())
			req = self.service.users.messages.send( # type: ignore
				userId='me', # type: ignore
				upload_file=str(path), # type: ignore
				json=body, # type: ignore
			)
			req.upload_file_content_type = "message/rfc822"
			return await self.ag.as_user(req)

	def _build_email(
		self,
		content: MsgContent,
		to: List[str],
		cc: List[str],
	) -> Union[MIMEText, MIMEMultipart]:
		with_attachments = len(content.attachment) != 0
		text_mime = MIMEText(content.html_body, 'html')
		if with_attachments:
			message = MIMEMultipart()
			message.attach(text_mime)
			for att in content.attachment:
				file_mime = MIMEBase(att.main_type, att.sub_type)
				file_mime.set_payload(att.content)
				file_mime.add_header('Content-Disposition', 'attachment', filename=att.name)
				encoders.encode_base64(file_mime)
				message.attach(file_mime)
		else:
			message = text_mime
		message['to'] = ", ".join(to)
		message['cc'] = ", ".join(cc)
		if self.email_name is not None:
			message['from'] = f'{self.email_name}<{self.email_id}>'
		else:
			message['from'] = self.email_id
		message['subject'] = content.subject
		return message

	async def _get_data_from_thread(self, thread_id: str) -> Dict:
		data = {}
		req = self.service.users.threads.get(userId='me', id=thread_id) # type: ignore
		thread = await self.ag.as_user(req)
		messages = thread['messages']
		for message in messages[0]['payload']['headers']:
			if message['name'].lower() == 'subject':
				data['subject'] = message['value']
			if message['name'].lower() == 'message-id':
				data['In-Reply-To'] = message['value']
				data['References'] = message['value']
		return data

	async def _get_new_email_ids(self, after: dt.datetime) -> List[str]:
		page_token = None
		ids = []
		while True:
			q = f"after:{int(after.timestamp())} AND NOT label:sent AND NOT from:{self.email_id}"
			req = self.service.users.messages.list(userId='me', pageToken=page_token, maxResults=500, q=q) # type: ignore
			r = await self.ag.as_user(req)
			ids.extend([m['id'] for m in r.get('messages', [])])
			page_token = r.get('nextPageToken', None)
			if page_token is None:
				break
		# single id can be there multiple times in respose, so convert to a `Set` first
		return sorted(filter(lambda i: i > self.last_mail_id, set(ids)))

	async def _fetch_mail(self, gmail_id: str) -> Tuple[dict, Gmail]:
		req = self.service.users.messages.get(userId='me', id=gmail_id, format='raw') # type: ignore
		api_msg = await self.ag.as_user(req)
		parsed_mail = mailparser.parse_from_string(base64.urlsafe_b64decode(api_msg['raw']).decode())
		mail = self._parse_msg(api_msg, parsed_mail)

		logger.debug("new mail", mail_id=api_msg['id'], subject=mail.content.subject)
		return api_msg, mail

	async def get_new_mails(self, only_after: dt.datetime) -> AsyncGenerator[Gmail, None]:
		"""
		a mail is only returned if {only_after <= mail.time and mail.id > self.last_mail_id}
		"""
		# lock so self.last_mail_id is not read-write at same time
		async with self._new_mail_lock:
			new_msg_ids = await self._get_new_email_ids(only_after)
			if len(new_msg_ids) != 0:
				logger.debug("New Mails", user=self.email_id, total=len(new_msg_ids))
			for msg_id in new_msg_ids:
				raw_msg, gmail = await self._fetch_mail(msg_id)

				# sometimes gmail doesn't label messages sent by us as `SENT` or maybe takes time to do so.
				# in those cases we manually filter it
				if gmail.sender != self.email_id:
					yield gmail
					self.last_mail_id = raw_msg['id']
				else:
					logger.warning(
						"Gmail Api returned mail sent by us but doesn't have label=SENT",
						mail_id=gmail.gmail_id,
						sender=gmail.sender,
						subject=gmail.content.subject
					)

	async def reply_to_thread(self, thread_id: str, content: MsgContent, to: List[str], cc: List[str] = []):
		logger.debug("Reply to thread", thread_id=thread_id, content_body_len=len(content.body), to=to, cc=cc)
		assert content.subject is None
		data = await self._get_data_from_thread(thread_id)
		subject = data['subject']

		if not subject.startswith("Re: "):
			subject = "Re: " + subject

		content = content.with_subject(subject)

		message = self._build_email(content, to, cc)
		message.add_header('References', data['References'])
		message.add_header('In-Reply-To', data['In-Reply-To'])

		await self._send(message, thread_id)

	async def start_new_thread(self, content: MsgContent, to: List[str], cc: List[str] = []) -> str:
		logger.debug("Start new Thread", to=to, content_body_len=len(content.body), cc=cc)
		assert content.subject is not None
		message = self._build_email(content, to, cc)
		r = await self._send(message, None)
		return r['threadId']


@dataclass
class GoogleAuth:
	oauth_client: GoogleOAuth2 = field(
		default_factory=lambda: GoogleOAuth2(
			SERVICE_KEY.client_id,
			SERVICE_KEY.client_secret,
		)
	)

	async def refresh_token(self, token: Token) -> Token:
		raw_token = await self.oauth_client.refresh_token(token.refresh_token)
		return token.refreshed_token(raw_token)

	async def get_oauth_flow_url(self) -> str:
		assert self.oauth_client.base_scopes is not None
		return await self.oauth_client.get_authorization_url(
			SERVICE_KEY.redirect_uri,
			scope=EMAIL_SCOPES + self.oauth_client.base_scopes,
		)

	async def get_access_token(self, token_code: str) -> Token:
		try:
			raw_tokens = await self.oauth_client.get_access_token(token_code, SERVICE_KEY.redirect_uri)
		except GetAccessTokenError as e:
			raise GmailTokenException from e

		try:
			id, email = await self.oauth_client.get_id_email(raw_tokens['access_token'])
			raw_tokens['email'] = email
		except GetIdEmailError as e:
			raise GmailTokenException from e

		missing_scopes = []
		for scope in EMAIL_SCOPES:
			if scope not in raw_tokens['scope'].split(" "):
				missing_scopes.append(scope)

		if len(missing_scopes) != 0:
			raise GmailTokenException(f"Scopes Missing: {missing_scopes=}")

		return Token.from_raw(raw_tokens)

	async def revoke_token(self, token: Token):
		await self.oauth_client.revoke_token(token.access_token)


def default_exc_handler(ex, user):
	raise ex


class TokenExpiredException(Exception):
	def __init__(self, cause: Union[RefreshTokenError, _aiogoogle.excs.AuthError]):
		self.cause = cause
		super().__init__(f"due to {self.cause}")


@dataclass
class GmailClientManager:
	"""
		use `await new(users)` to create an instance
	"""
	users: Dict[MatrixUserId, Tuple[LoggedInUser, GmailClient]]
	on_token_error: Callable[[TokenExpiredException, User], Any] = default_exc_handler
	last_sync_time: dt.datetime = field(default_factory=lambda: dt.datetime.now() - ACCEPT_DELAY)
	oauth_client: GoogleAuth = field(init=False, default_factory=GoogleAuth)
	_logger: BoundLogger = field(default_factory=lambda: logger)

	@classmethod
	async def new(cls, users: List[LoggedInUser]) -> 'GmailClientManager':
		_users = {}
		for u in users:
			_users[u.matrix_id] = (u, await GmailClient.from_user(u))
		return GmailClientManager(_users)

	def _get_user(self, mxid: str) -> Optional[User]:
		if mxid not in self.users:
			return None
		return self.users[mxid][0]

	def _get_gclient(self, mxid: str) -> Optional[GmailClient]:
		if mxid not in self.users:
			return None
		return self.users[mxid][1]

	async def upsert_user(self, user: LoggedInUser):
		if user.matrix_id in self.users:
			await self.remove_user(user.matrix_id)
		self.users[user.matrix_id] = (user, await GmailClient.from_user(user))

	async def remove_user(self, user_matrix_id: str):
		_, client = self.users.pop(user_matrix_id)
		await client.close()

	async def refresh_tokens(self) -> List[User]:
		updated_users = []
		for (u, _) in self.users.values():
			if u.token.is_expired():
				try:
					u.token = await self.oauth_client.refresh_token(u.token)
				except RefreshTokenError as e:
					await self.on_token_error(TokenExpiredException(e), u)
				updated_users.append(u)
				assert not u.token.is_expired()
		return updated_users

	async def listen_for_mails(self, ) -> AsyncGenerator[Tuple[LoggedInUser, Gmail], None]:
		retry = 0
		while True:
			try:
				started_at = dt.datetime.now()

				for (user, client) in self.users.values():
					try:
						async for mail in client.get_new_mails(self.last_sync_time):
							yield user, mail
					except _aiogoogle.excs.AuthError as e:
						await self.on_token_error(TokenExpiredException(e), user)

				self.last_sync_time = started_at
				retry = 0
				await aio.sleep(CONFIG.GMAIL_RECHECK_SECONDS)

			except Exception as e:
				retry += 1
				logger.exception("Listen for mails failed. Restarting", retry=retry)
				await aio.sleep(2**retry)

	async def send_mail(self, message: PreparedGmail) -> str:
		# TODO: should return `Gmail` instance
		""" Returns Thread Id """

		client = self._get_gclient(message.sender)
		assert client is not None
		thread_id = message.thread_id

		if thread_id is None:
			thread_id = await client.start_new_thread(message.content, message.to, message.cc)
		else:
			await client.reply_to_thread(thread_id, message.content, message.to, message.cc)

		return thread_id

