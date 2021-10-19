import asyncio as aio
import re
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import CONFIG
from .prelude import logger

email_re = re.compile(
	r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
)

email_keyword = "_at_"



def is_valid_email(email: str) -> bool:
	return email_re.fullmatch(email.lower()) is not None


def extract_alias_thread(alias: str) -> str:
	alias_with_hs_name = alias.replace(f"#{CONFIG.NAMESPACE_PREFIX}", "", 1)
	return alias_with_hs_name[:-(len(CONFIG.HOMESERVER_NAME) + 1)]


def generate_alias(alias_name: str) -> str:
	return f"#{CONFIG.NAMESPACE_PREFIX}{alias_name}:{CONFIG.HOMESERVER_NAME}"

def extract_alias_localpart(alias: str) -> str:
	assert alias[0]=="#"
	alias_with_hs_name = alias.replace(f"#", "", 1)
	return alias_with_hs_name[:-(len(CONFIG.HOMESERVER_NAME) + 1)]

def extract_mxid_localpart(mxid: str) -> str:
	assert mxid[0]=="@"
	mxid_with_hs_name = mxid.replace(f"@", "", 1)
	return mxid_with_hs_name[:-(len(CONFIG.HOMESERVER_NAME) + 1)]

def extract_localpart(val: str) -> str:
	assert val[0] in ['#', '@']
	if val[0] == '@':
		return extract_mxid_localpart(val)
	else:
		return extract_alias_localpart(val)

def generate_mxid(email: str) -> str:
	email = email_sanitize(email)
	return _generate_mxid(email)


def _generate_mxid(localpart: str) -> str:
	return f"@{CONFIG.NAMESPACE_PREFIX}{localpart}:{CONFIG.HOMESERVER_NAME}"


def is_bot_mxid(mxid: str) -> bool:
	return mxid.startswith(f"@{CONFIG.NAMESPACE_PREFIX}")


def is_thread_alias(alias: str) -> bool:
	return alias.startswith(f"#{CONFIG.NAMESPACE_PREFIX}")


def email_sanitize(email: str) -> str:
	return email.replace("@", email_keyword)


def email_desanitize(sanitized_email: str) -> str:
	index = sanitized_email.rindex(email_keyword)
	email = sanitized_email[:index] + "@" + sanitized_email[index + len(email_keyword):]
	return email

def extract_email(mxid: str) -> str:
	localpart = extract_mxid_localpart(mxid)
	localpart=localpart.replace(CONFIG.NAMESPACE_PREFIX, "", 1)
	return email_desanitize(localpart)


def is_valid_email_mxid(mxid: str) -> bool:
	senatized_email = extract_mxid_localpart(mxid)
	if "_at_" not in senatized_email:
		return False
	return is_valid_email(email_desanitize(senatized_email))


@dataclass
class NamedTempFile:
	""" Returns a Path instead of file object """
	path: Path = field(init=False)

	def __post_init__(self):
		file = NamedTemporaryFile(delete=False)
		self.path = Path(file.name)

	def __enter__(self, *args) -> Path:
		return self.path

	def __exit__(self, *args):
		self.path.unlink()


def custom_exception_handler(loop, context) -> None:
	# first, handle with default handler
	loop.default_exception_handler(context)
	logger.exception("unhandled exception occurred")
	loop.stop()


# https://quantlane.com/blog/ensure-asyncio-task-exceptions-get-logged/
def handle_task_result(task: aio.Task) -> None:
	try:
		task.result()
	except aio.CancelledError:
		pass
	except Exception as e:
		raise e


if __name__ == "__main__":
	assert is_valid_email("a@gmail.com")
	assert not is_valid_email("b a@gmail.com")
	assert not is_valid_email("b a@gmail")
	assert is_valid_email("Hk8128@pm.me".lower())
	assert is_valid_email("Hk8128@pm.me")

	print(email_sanitize("ak@iffmail.com"))
	print(email_desanitize(email_sanitize("ak@iffmail.com")))
	alias = _generate_mxid("hey")
	print(extract_mxid_localpart(alias))
