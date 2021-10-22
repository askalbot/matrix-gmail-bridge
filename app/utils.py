import asyncio as aio
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from .prelude import logger

email_re = re.compile(
    r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*@(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
)

email_keyword = "_at_"


@dataclass
class MatrixUtility:
    namespace_prefix: str
    homeserver_name: str

    def extract_alias_thread(self, alias: str) -> str:
        alias_with_hs_name = alias.replace(f"#{self.namespace_prefix}", "", 1)
        return alias_with_hs_name[:-(len(self.homeserver_name) + 1)]

    def generate_alias(self, alias_name: str) -> str:
        return f"#{self.namespace_prefix}{alias_name}:{self.homeserver_name}"

    def generate_mxid(self, email: str) -> str:
        email = email_sanitize(email)
        return self._generate_mxid(email)

    def _generate_mxid(self, localpart: str) -> str:
        return f"@{self.namespace_prefix}{localpart}:{self.homeserver_name}"

    def is_bot_mxid(self, mxid: str) -> bool:
        return mxid.startswith(f"@{self.namespace_prefix}")

    def is_thread_alias(self, alias: str) -> bool:
        return alias.startswith(f"#{self.namespace_prefix}")

    def extract_email(self, mxid: str) -> str:
        localpart = extract_mxid_localpart(mxid, self.homeserver_name)
        localpart = localpart.replace(self.namespace_prefix, "", 1)
        return email_desanitize(localpart)

    def is_valid_email_mxid(self, mxid: str) -> bool:
        senatized_email = extract_mxid_localpart(mxid, self.homeserver_name)
        if "_at_" not in senatized_email:
            return False
        return is_valid_email(email_desanitize(senatized_email))

    def extract_alias_localpart(self, alias: str) -> str:
        return extract_alias_localpart(alias, self.homeserver_name)

    def extract_mxid_localpart(self, mxid: str) -> str:
        return extract_mxid_localpart(mxid, self.homeserver_name)

    def extract_localpart(self, val: str) -> str:
        return extract_localpart(val, self.homeserver_name)


def extract_alias_localpart(alias: str, homeserver_name: str) -> str:
    assert alias[0] == "#"
    alias_with_hs_name = alias.replace(f"#", "", 1)
    return alias_with_hs_name[:-(len(homeserver_name) + 1)]


def extract_mxid_localpart(mxid: str, homeserver_name: str) -> str:
    assert mxid[0] == "@"
    mxid_with_hs_name = mxid.replace(f"@", "", 1)
    return mxid_with_hs_name[:-(len(homeserver_name) + 1)]


def extract_localpart(val: str, homeserver_name: str) -> str:
    assert val[0] in ['#', '@']
    if val[0] == '@':
        return extract_mxid_localpart(val, homeserver_name)
    else:
        return extract_alias_localpart(val, homeserver_name)


def email_sanitize(email: str) -> str:
    return email.replace("@", email_keyword)


def email_desanitize(sanitized_email: str) -> str:
    index = sanitized_email.rindex(email_keyword)
    email = sanitized_email[:index] + "@" + sanitized_email[index + len(email_keyword):]
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
    include_stack = sys.exc_info[0] == None # type: ignore
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
