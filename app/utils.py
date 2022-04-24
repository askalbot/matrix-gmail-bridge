import asyncio as aio
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from typing import Optional
from .prelude import logger

email_re = re.compile(
	r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
)

AT_PLACEHOLDER = "_at_"


def extract_email(mxid: str, namespace_prefix: str) -> str:
	localpart = extract_mxid_localpart(mxid)
	localpart = localpart.replace(namespace_prefix, "", 1)
	return email_desanitize(localpart)


# def is_valid_email_mxid(mxid: str) -> bool:
# 	senatized_email = extract_mxid_localpart(mxid)
# 	if "_at_" not in senatized_email:
# 		return False
# 	return is_valid_email(email_desanitize(senatized_email))


def extract_alias_localpart(alias: str) -> str:
	match = re.fullmatch(r"#(.*):(.*?)", alias)
	assert match is not None, match
	return match.group(1)


def extract_mxid_localpart(mxid: str) -> str:
	match = re.fullmatch(r"@(.*):(.*?)", mxid)
	assert match is not None, match
	return match.group(1)


def extract_localpart(val: str) -> str:
	assert val[0] in ['#', '@']
	if val[0] == '@':
		return extract_mxid_localpart(val)
	else:
		return extract_alias_localpart(val)


def email_sanitize(email: str) -> str:
	return email.replace("@", AT_PLACEHOLDER)

def try_email_desanitize(sanitized_email: str) -> Optional[str]:
	if AT_PLACEHOLDER not in sanitized_email:
		return None
	index = sanitized_email.rindex(AT_PLACEHOLDER)
	email = sanitized_email[:index] + "@" + sanitized_email[index + len(AT_PLACEHOLDER):]
	if not is_valid_email(email):
		return None
	return email

def email_desanitize(sanitized_email: str) -> str:
	index = sanitized_email.rindex(AT_PLACEHOLDER)
	email = sanitized_email[:index] + "@" + sanitized_email[index + len(AT_PLACEHOLDER):]
	return email


def is_valid_email(email: str) -> bool:
	return email_re.fullmatch(email.lower()) is not None


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
	include_stack = sys.exc_info()[0] == None
	logger.exception("unhandled exception occurred", context=context, stack_info=include_stack)


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
