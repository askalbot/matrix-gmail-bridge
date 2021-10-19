from io import BytesIO
from app.config import CONFIG
import copy
from enum import Enum, auto
from functools import cached_property
from pydantic import BaseModel, validator, Field


from .prelude import *
from typing import Final, Literal, cast


class AttachmentType(Enum):
	image = auto()
	audio = auto()
	video = auto()
	unknown = auto()

	@classmethod
	def from_mime_type(cls, mime: str) -> 'AttachmentType':
		if mime.startswith("image"):
			return cls.image
		elif mime.startswith("video"):
			return cls.video
		elif mime.startswith("audio"):
			return cls.audio
		else:
			return cls.unknown


class Attachment(BaseModel):
	mime_type: str
	# TODO: make SpooledTempFile
	content: bytes
	name: str
	class Config:
		arbitrary_types_allowed = True
		keep_untouched = (cached_property,)

	def content_io(self) -> BytesIO:
		return BytesIO(self.content)

	@cached_property
	def type(self) -> 'AttachmentType':
		return AttachmentType.from_mime_type(self.mime_type)

	@cached_property
	def main_type(self) -> str:
		return self.mime_type.split("/", 1)[0]

	@cached_property
	def sub_type(self) -> str:
		return self.mime_type.split("/", 1)[1]


class MsgContent(BaseModel):
	# assuming reply is always html
	body: str
	html_body: str
	attachment: List[Attachment] = Field(default_factory=list)
	subject: Optional[str] = None

	def with_subject(self, subject: str) -> 'MsgContent':
		new_msg = copy.copy(self)
		new_msg.subject = subject
		return new_msg


class AuthState(Enum):
	logged_out = auto()
	waiting_for_token = auto()
	logged_in = auto()


class Token(BaseModel):
	access_token: str
	refresh_token: str
	email: str
	expiry: dt.datetime
	raw: dict

	@classmethod
	def from_raw(cls, raw: dict) -> 'Token':
		return cls(
			access_token=raw['access_token'],
			refresh_token=raw['refresh_token'],
			email=raw['email'],
			expiry=dt.datetime.fromtimestamp(raw['expires_at'], tz=dt.timezone.utc),
			raw=raw,
		)

	def refreshed_token(self, refresh_result: dict) -> 'Token':
		raw = self.raw
		raw.update(refresh_result)
		token = Token.from_raw(self.raw)
		return token

	def is_expired(self):
		return dt.datetime.now(tz=dt.timezone.utc) > self.expiry


class User(BaseModel):
	"""
	use token.email if email_address is not specified by user
	"""
	matrix_id: str

	email_name: Optional[str] = CONFIG.DEFAULT_EMAIL_NAME
	last_mail_id: Optional[str] = None

	auth_state: AuthState = AuthState.logged_out
	token: Optional[Token] = None
	email_address: Optional[str] = None

	@validator("email_address", always=True)
	def populate_s(cls, v, values):
		if v is None and values.get('token') is not None:
			return cast(Token, values['token']).email
		return v

	def narrow(self) -> Union['User', 'LoggedInUser']:
		if self.auth_state == AuthState.logged_in:
			return LoggedInUser.parse_obj(self)
		return self
	
	def logged_in(self, token: Token, email_address: Optional[str] = None) -> 'LoggedInUser':
		user = self.copy()
		user.email_address = email_address
		user.token = token
		user.auth_state = AuthState.logged_in
		u = User.parse_obj(user)
		return LoggedInUser.parse_obj(u)


class LoggedInUser(User):
	token: Token
	email_address: str
	auth_state: Literal[AuthState.logged_in] = AuthState.logged_in

	def logged_out(self) -> 'User':
		user = User.parse_obj(self.copy())
		user.email_address = None
		user.token = None
		user.auth_state = AuthState.logged_out
		return user
