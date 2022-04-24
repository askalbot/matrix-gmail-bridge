from __future__ import annotations

import base64
import codecs
import html
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import *

import aiogoogle as _aiogoogle
import mailparser
from bs4 import BeautifulSoup
from httpx_oauth.clients.google import GoogleOAuth2
from httpx_oauth.errors import GetIdEmailError
from httpx_oauth.oauth2 import GetAccessTokenError
from pydantic import BaseModel, Field
from structlog import BoundLogger

from app.utils import NamedTempFile

from . import utils as u
from .log import Logger
from .models import Attachment, MsgContent, Token
from .prelude import *

MatrixUserId = str
GmailId = str


class GmailTokenException(Exception):
	pass


class Gmail(BaseModel):
	gmail_id: str
	thread_id: str
	sender: str
	to: List[str]
	content: MsgContent
	cc: List[str] = Field(default_factory=list)

	def reciepients(self) -> List[str]:
		return self.to + self.cc

	def without_reciepient(self, email_address: str) -> 'Gmail':
		assert email_address != self.sender, "Can't remove sender"
		new_mail = self.copy()
		new_mail.to = [t for t in self.to if t != email_address]
		new_mail.cc = [t for t in self.cc if t != email_address]
		return new_mail


@dataclass
class GmailClient:
	email_id: str
	service: _aiogoogle.GoogleAPI
	ag: _aiogoogle.Aiogoogle
	token: Token
	email_name: Optional[str] = None

	_logger: BoundLogger = field(default_factory=lambda: Logger("gmail-client"))

	@classmethod
	async def new(
		cls,
		user_token: Token,
		service_key: ServiceKey,
		email_name: Optional[str] = None,
		email_address: Optional[str] = None,
	) -> 'GmailClient':

		ag = _aiogoogle.Aiogoogle(
			user_creds={
				"access_token": user_token.access_token,
				"refresh_token": user_token.refresh_token,
			},
			client_creds={
				"client_id": service_key.client_id,
				"client_secret": service_key.client_secret,
			}
		)

		service = await ag.discover('gmail', 'v1')

		return GmailClient(
			email_id=email_address or user_token.email,
			email_name=email_name,
			service=service,
			ag=ag,
			token=user_token,
		)

	async def get_new_mails(
		self,
		after_mail_id: Optional[str] = None,
		after_time: dt.datetime = dt.datetime.fromtimestamp(0),
	) -> AsyncGenerator[Gmail, None]:
		for msg_id in await self._get_new_email_ids(after_mail_id, after_time):
			raw, gmail = await self._fetch_mail(msg_id)
			logger.debug("new mail", mail_id=raw['id'], subject=gmail.content.subject)

			# sometimes gmail doesn't label messages sent by us as `SENT` or maybe takes time to do so.
			# in those cases we manually filter it
			if gmail.sender != self.email_id:
				yield gmail
			else:
				logger.warning(
					"Gmail Api returned mail sent by us but doesn't have label=SENT",
					mail_id=gmail.gmail_id,
					sender=gmail.sender,
					subject=gmail.content.subject
				)

	async def start_new_thread(self, content: MsgContent, to: List[str], cc: List[str] = []) -> str:
		logger.debug("Start new Thread", to=to, content_body_len=len(content.body), cc=cc)
		assert content.subject is not None
		message = self._build_email(content, to, cc)
		r = await self._send(message, None)
		return r['threadId'] # type: ignore

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

	def get_user_token(self) -> Token:
		new_token = self.token.copy()
		new_token.access_token = self.ag.user_creds['access_token']
		new_token.refresh_token = self.ag.user_creds['refresh_token']
		new_token.expiry = self.ag.user_creds['expires_at']
		self.token = new_token
		return new_token

	async def close(self):
		if self.ag.active_session is not None:
			await self.ag.active_session.close()

	@staticmethod
	def _parse_msg_body(parsed_mail: mailparser.MailParser, with_gmail_quote: bool = True) -> Tuple[str, str]:
		""" returns body, html_body """
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
		# req = self.service.users.threads.get(id=thread_id) # type: ignore
		try:
			thread = await self.ag.as_user(req)
		except Exception as e:
			logger.error("Failed to get thread data", exc_info=True, thread_id=thread_id)
		messages = thread['messages'] # type: ignore
		for message in messages[0]['payload']['headers']:
			if message['name'].lower() == 'subject':
				data['subject'] = message['value']
			if message['name'].lower() == 'message-id':
				data['In-Reply-To'] = message['value']
				data['References'] = message['value']
		return data

	async def _get_new_email_ids(
		self,
		after_mail_id: Optional[str] = None,
		after_time: dt.datetime = dt.datetime.fromtimestamp(0),
	) -> List[str]:
		page_token = None
		ids: List[str] = []

		after = after_time

		if after_mail_id is not None:
			raw, _ = await self._fetch_mail(after_mail_id)
			ts = (int(raw['internalDate']) / 1000) - 1
			after = max(dt.datetime.fromtimestamp(ts), after)

		while True:
			q = f"after:{int(after.timestamp())} AND NOT label:sent AND NOT from:{self.email_id}"
			req = self.service.users.messages.list(userId='me', pageToken=page_token, maxResults=500, q=q) # type: ignore
			r = await self.ag.as_user(req)
			ids.extend([m['id'] for m in r.get('messages', [])]) # type: ignore
			page_token = r.get('nextPageToken', None) # type: ignore
			if page_token is None:
				break

		# single id can be there multiple times in respose, so convert to a `Set` first
		return [id for id in sorted(set(ids)) if after_mail_id is None or id > after_mail_id]

	async def _fetch_mail(self, gmail_id: str) -> Tuple[dict, Gmail]:
		# handle case where gmail_id is not present
		req = self.service.users.messages.get(userId='me', id=gmail_id, format='raw') # type: ignore
		api_msg = await self.ag.as_user(req)
		parsed_mail = mailparser.parse_from_bytes(base64.urlsafe_b64decode(api_msg['raw'])) # type: ignore
		mail = self._parse_msg(api_msg, parsed_mail)

		return api_msg, mail


@dataclass
class GoogleAuth:
	"""
	Simple wrapper for httpx_oauth.GoogleOAuth2 that works with `Token` class
	"""
	service_key: ServiceKey
	oauth_client: GoogleOAuth2 = field(init=False)

	def __post_init__(self):
		self.oauth_client = GoogleOAuth2(
			self.service_key.client_id,
			self.service_key.client_secret,
		)

	async def refresh_token(self, token: Token) -> Token:
		raw_token = await self.oauth_client.refresh_token(token.refresh_token)
		token = token.copy()
		token.access_token = raw_token['access_token']
		token.refresh_token = raw_token['refresh_token']
		token.expiry = raw_token['expires_at']
		return token

	async def get_oauth_flow_url(self) -> str:
		url = await self.oauth_client.get_authorization_url(
			self.service_key.redirect_uri,
			scope=EMAIL_SCOPES,
		)
		print(url)
		return url

	async def get_access_token(self, token_code: str) -> Token:
		try:
			raw_tokens = await self.oauth_client.get_access_token(token_code, self.service_key.redirect_uri)
		except GetAccessTokenError as e:
			raise GmailTokenException from e

		try:
			_, email = await self.oauth_client.get_id_email(raw_tokens['access_token'])
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


TokenExpiredException = _aiogoogle.excs.AuthError

if __name__ == "__main__":
	from .config import get_config

	async def main2():
		o = GoogleAuth(get_config().get_service_key())
		url = await o.get_oauth_flow_url()
		print(url)
		token = input()
		at = await o.get_access_token(token)
		print(at)

	async def main():
		token = Token(
			**{
				"access_token":
				"ya29.A0ARrdaM_f2a7Y22TW4mYkRPg48g7zFutoxaqfZb__QdV6O1UattbRnSiPeVmKt01AaVtB7A154eo9PbSS0KU9og7pGu4Qhcyxv7XVZbUdOWBXHkdGWgwzQax9KvDHiVoch2Eucq-syhCfDCgsfABq0z4lSyW1cw",
				"refresh_token":
				"1//0gY3WXpQdE-xtCgYIARAAGBASNwF-L9IrNEzlSIKCPj1O--Jfyu9WcehnIvz3uH6bUNUxApKTR9mjlxCF-VPsoGCzbKtbDHIyuFI",
				"email": "nnkitsaini@gmail.com",
				"expiry": "2022-04-12T16:50:23.190769"
			} # type: ignore
		)
		"""
		Thread
		"""
		r = await GmailClient.new(user_token=token, service_key=get_config().get_service_key())
		self = r
		print(r.reply_to_thread)
		thread_id = "1801bff1c530b18f"
		threads = self.service.users.threads.list(userId='me') # type: ignore

		# req = self.service.users.threads.get(id=thread_id) # type: ignore
		# thread = await self.ag.as_user(req)
		thread = await self.ag.as_user(threads)
		breakpoint()
		print(thread)

	aio.run(main2())
