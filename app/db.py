from dataclasses import dataclass

from typing import *
import json
import abc
import nio
from dataclasses import dataclass
from typing import Optional
from .nio_client import AppserviceClient
from .models import AuthState, LoggedInUser, Token, User
from .prelude import *


class UserNotFound(GmailBridgeException):
	...


class Kv(abc.ABC):

	@abc.abstractmethod
	async def set(self, key: str, val: str):
		raise NotImplementedError()

	@abc.abstractmethod
	async def get(self, key: str) -> Optional[str]:
		raise NotImplementedError()


@dataclass
class DictKv(Kv):
	d: Dict[str, str] = field(default_factory=dict)

	async def get(self, key: str) -> Optional[str]:
		return self.d.get(key)

	async def set(self, key: str, val: str):
		self.d[key] = val


@dataclass
class MatrixKv(Kv):
	"""
	Room Alias should preferablly be under exclusive namespace
	"""
	client: AppserviceClient
	room_alias_to_use: str

	async def get_state_room(self) -> str:
		full_alias = f"#{self.room_alias_to_use}:{self.client.homeserver_name}"
		r = await self.client.room_resolve_alias(full_alias)
		if isinstance(r, nio.RoomResolveAliasResponse):
			return r.room_id
		r = await self.client.room_create(alias=self.room_alias_to_use)
		assert not isinstance(r, nio.RoomCreateError), r
		return r.room_id

	async def get(self, key: str) -> Optional[str]:
		room_id = await self.get_state_room()
		r = await self.client.room_get_state_event(room_id, "jif.db.matrix", key)
		if isinstance(r, nio.RoomGetStateEventResponse):
			return r.content['data']
		return None

	async def set(self, key: str, value: str) -> Optional[str]:
		room_id = await self.get_state_room()
		await self.client.room_put_state(room_id, "jif.db.matrix", {"data": value}, key)


@dataclass
class Db:
	kv: Kv

	async def all_active_users(self) -> List[LoggedInUser]:
		rv = []
		for i in await self._get_all_users():
			if isinstance(n := i.narrow(), LoggedInUser):
				rv.append(n)
		return rv

	async def get_user(self, user_id: str) -> User:
		if (e := await self.kv.get("u:" + user_id)) is not None:
			return User.parse_raw(e).narrow()
		return User(matrix_id=user_id)

	async def upsert_user(self, user: User):
		all_user_ids = await self._all_user_ids()
		await self.kv.set("u:" + user.matrix_id, user.json())

		if user.matrix_id not in all_user_ids:
			all_user_ids.append(user.matrix_id)
			await self.kv.set("all", json.dumps(all_user_ids))

	async def add_event(self, transaction_id: str):
		await self.kv.set("t:" + transaction_id, '{}')

	async def event_exists(self, transaction_id: str) -> bool:
		return await self.kv.get("t:" + transaction_id) is not None

	async def _all_user_ids(self) -> List[str]:
		return json.loads(await self.kv.get("all") or "[]")

	async def _get_all_users(self) -> List[User]:
		rv = []
		for user in await self._all_user_ids():
			rv.append(await self.get_user(user))
		return rv
