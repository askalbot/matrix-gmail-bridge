"""
CIPHER_TEXT_REPRESENTATION = nonce(16byte) + tag(16byte) + ciphertext
"""
import base64
from typing import Optional
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Cipher._mode_gcm import GcmMode
import Crypto.Cipher.AES

AES_MODE = Crypto.Cipher.AES.MODE_GCM
MAC_LEN = NONCE_LEN = 16

class DecryptionError(Exception):
	pass

def get_aes_cipher(aes_key: bytes, nonce: Optional[bytes] = None) -> GcmMode:
	r = AES.new(aes_key, AES_MODE, nonce=nonce, mac_len=MAC_LEN) # type: ignore
	assert isinstance(r, GcmMode)
	return r


def aes_encrypt_bytes(aes_key: bytes, data: bytes) -> bytes:
	cipher = get_aes_cipher(aes_key)
	ciphertext, tag = cipher.encrypt_and_digest(data)
	rv = cipher.nonce + tag + ciphertext # type: ignore
	return rv


def aes_decrypt_bytes(aes_key: bytes, data: bytes) -> bytes:
	nonce, data = data[:NONCE_LEN], data[NONCE_LEN:]
	tag, encrypted_data = data[:MAC_LEN], data[MAC_LEN:]

	cipher = get_aes_cipher(aes_key, nonce)
	try:
		return cipher.decrypt_and_verify(encrypted_data, tag)
	except ValueError as e:
		# TODO: reduce scope of except
		raise DecryptionError from e


def aes_encrypt_str(aes_key: bytes, string: str) -> str:
	content_in_bytes = string.encode('utf-8')
	encrypted_bytes = aes_encrypt_bytes(aes_key, content_in_bytes)
	return base64.b64encode(encrypted_bytes).decode('ascii')


def aes_decrypt_str(aes_key: bytes, string: str) -> str:
	encrypted_bytes = base64.b64decode(string)
	return aes_decrypt_bytes(aes_key, encrypted_bytes).decode('utf-8')
