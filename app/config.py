from .prelude import *
from typing import Optional
import yaml
from pydantic import BaseSettings, Field
import base64


class BridgeConfig(BaseSettings):
	# TODO: custom validation

	# Token
	AS_TOKEN: str = Field(
		description="token to use for making request with homeserver",
		example="secure-string-token",
	)
	HS_TOKEN: str = Field(
		description="token to verify when recieving events from homeserver",
		example="secure-different-string-token",
	)

	# GMAIL
	DEFAULT_EMAIL_NAME: Optional[str] = Field(
		description="(optional) default email display name to use for sending mails. ",
		default=None,
	)
	GMAIL_RECHECK_SECONDS: int = Field(
		description="Interval between polling gmail api for new mails",
		default=5 * 60,
	)

	# BRIDGE
	BRIDGE_ID: str = Field(
		description="Id to use for this matrix bridge",
		default="gmail",
	)
	BRIDGE_URL: str = Field(
		description="url(without port) by which homeserver can access the bridge",
		default="http://localhost",
	)
	PORT: int = Field(
		description="port to start the bridge on",
		default=8010,
	)
	SENDER_LOCALPART: str = Field(
		description="Bridge Localpart",
		default="appservice-gmail",
	)
	NAMESPACE_PREFIX: str = Field(
		description="prefix to use for room aliases and users created by this bridge",
		default="_gmail_bridge_",
	)

	# HOMESERVER
	HOMESERVER_URL: str = Field(
		description="url by which bridge can access homeserver",
		default="http://localhost:8008",
	)

	ADMIN_USER: str = Field(
		description="url by which bridge can access homeserver",
		default="http://localhost:8008",
	)

	HOMESERVER_NAME: str = Field(
		description="Name of homeserver",
		example="example.com",
	)

	HOST: str = Field(
		description='hostname to listen on',
		default='localhost',
	)

	# GMAIL SERVICE
	gmail_client_secret: str = Field(example="ababaabababababababab")
	gmail_client_id: str = Field(example="sdf-sdfkjsdf-")

	def get_service_key(self) -> ServiceKey:
		return ServiceKey(
			client_secret=self.gmail_client_secret,
			client_id=self.gmail_client_id,
		)

	def export_config_json(self) -> str:
		return self.json(exclude={"AS_TOKEN", "HS_TOKEN", "SERVICE_KEY"}, indent=2)

	def get_hs_resistration_config(self) -> str:

		return f"""
# registration.yaml

# An ID which is unique across all application services on your homeserver. This should never be changed once set.
id: "{self.BRIDGE_ID}"

# this is the base URL of the application service
url: "{self.BRIDGE_URL}:{self.PORT}"

rate_limited: false

# This is the token that the AS should use as its access_token when using the Client-Server API
# This can be anything you want.
as_token: {self.AS_TOKEN}

# This is the token that the HS will use when sending requests to the AS.
# This can be anything you want.
hs_token: {self.HS_TOKEN}

# this is the local part of the desired user ID for this AS (in this case @logging:localhost)
sender_localpart: {self.BRIDGE_ID}
namespaces:
  users: 
    - exclusive: true
      regex: "@{self.NAMESPACE_PREFIX}.*"
  aliases:
    - exclusive: true
      regex: "#{self.NAMESPACE_PREFIX}.*"
  rooms: []
"""

	@classmethod
	def get_sample_yaml(cls) -> str:
		props = cls.schema()['properties']
		rv = ""
		for name, field in props.items():
			if 'default' in field:
				value = field['default']
			else:
				value = field.get('example', 'null')
			if 'description' in field:
				rv += "# " + field['description'] + "\n"
			rv += name + ": " + str(value) + "\n\n"
		return rv

	@classmethod
	def from_yaml(cls, content: str) -> 'BridgeConfig':
		body = yaml.load(content, Loader=yaml.FullLoader)
		return cls(**body)

	@classmethod
	def example_config(cls) -> 'BridgeConfig':
		body = {}
		props = cls.schema()['properties']
		for name, field in props.items():
			if 'default' in field:
				value = field['default']
			else:
				value = field.get('example')
			body[name] = value
		return cls(**body)


# to support overrride in tests
_runtime_config_override: Optional[BridgeConfig] = None


def get_config() -> BridgeConfig:
	if _runtime_config_override is not None:
		return _runtime_config_override
	else:
		if CONFIG_PATH.exists():
			return BridgeConfig.from_yaml(CONFIG_PATH.read_text())
		else:
			raise FileNotFoundError(
				f"{CONFIG_PATH} does not exist. Provide path to config using GMAIL_BRIDGE_CONFIG_PATH env var"
			)


def override_config(config: BridgeConfig):
	global _runtime_config_override
	_runtime_config_override = config
