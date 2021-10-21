import pytest
from app.db import *
import os

async def _execute_kv_test(kv: Kv):
	assert await kv.get("somethign") == None
	await kv.set("somethign", "2")
	assert await kv.get("somethign") == "2"
	await kv.set("somethign", "3")
	assert await kv.get("somethign") == "3"

@pytest.mark.asyncio
async def test_kv():
	await _execute_kv_test(DictKv())

@pytest.mark.asyncio
async def test_db():
	db = Db(DictKv(), os.urandom(16))

	assert len(await db._get_all_users()) == 0
	assert (await db.get_user("unknown_user")).auth_state == AuthState.logged_out

	log_out_user = User(matrix_id="matrix1")
	await db.upsert_user(log_out_user)
	assert len(await db.all_active_users()) == 0
	assert (await db.get_user("matrix1")).auth_state == AuthState.logged_out
	token = Token(
		access_token="",
		refresh_token="",
		email = "email",
		expiry = dt.datetime.now(),
		raw = {},
	)
	inserted = log_out_user.logged_in(token)
	await db.upsert_user(inserted)
	got = await db.get_user("matrix1")
	assert got.auth_state == AuthState.logged_in
	assert (await db.get_user("matrix1")).email_address == "email"
	assert len(await db.all_active_users()) == 1


	await db.upsert_user(log_out_user)
	assert (await db.get_user("matrix1")).auth_state == AuthState.logged_out
	assert len(await db.all_active_users()) == 0
	await db.upsert_user(User(**log_out_user.dict(exclude={"email_address"}), email_address="other_email"))
	log_out_dict = log_out_user.dict(exclude={"email_address"})
	assert User(**log_out_dict, email_address="other_email").email_address == "other_email"
	assert (await db.get_user("matrix1")).email_address == "other_email"
