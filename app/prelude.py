import os
import html
from io import IOBase
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
import asyncio as aio
from typing import Optional, Tuple, List, TypedDict, Union, Dict, Set
from pprint import pprint
import base64
from pydantic import BaseModel
from .log import Logger


DEBUG = os.environ.get("DEBUG", "1") == "0"
NAMESPACE = os.environ.get("GMAIL-BRIDGE_NAMESPACE", "jifchat-gmail-bridge")


logger = Logger(service="gmail-bridge")


EMAIL_SCOPES = [
	'https://www.googleapis.com/auth/gmail.readonly',
	'https://www.googleapis.com/auth/gmail.compose',
	'https://www.googleapis.com/auth/gmail.send',
]


@dataclass
class ServiceKey:
	client_secret: str
	client_id: str
	project_id: str
	redirect_uri: str = "urn:ietf:wg:oauth:2.0:oob"


DEFAULT_CONFIG_PATH = Path("./bridge-config.yaml")
CONFIG_PATH = Path(os.environ.get("GMAIL_BRIDGE_CONFIG_PATH", DEFAULT_CONFIG_PATH))

if CONFIG_PATH != DEFAULT_CONFIG_PATH and not CONFIG_PATH.is_file():
	raise FileNotFoundError(f"{CONFIG_PATH} is not a file")


class GmailBridgeException(Exception):
	pass
