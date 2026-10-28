"""Test-only passkey login helper backed by real WebAuthn crypto."""
import hashlib
import json
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url as encode


class PasskeyWitness:
    def __init__(self, rp_host="localhost", origin=None, credential_id=b"test-credential-id"):
        self.rp_host = rp_host
        self.origin = origin or f"http://{rp_host}"
        self.credential_id = credential_id
        self.private = ec.generate_private_key(ec.SECP256R1())

    def client_data(self, challenge, kind, origin=None):
        origin = origin or self.origin
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def registration(self, challenge):
        numbers = self.private.public_key().public_numbers()
        public_key = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        rp_hash = hashlib.sha256(self.rp_host.encode()).digest()
        data = (
            rp_hash
            + bytes([0x45])
            + struct.pack(">I", 0)
            + bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + public_key
        )
        return {
            "id": encode(self.credential_id),
            "rawId": encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": encode(self.client_data(challenge, "webauthn.create")),
                "attestationObject": encode(
                    cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
                ),
                "transports": ["internal"],
            },
        }

    def assertion(self, challenge, user_id, count=1):
        client = self.client_data(challenge, "webauthn.get")
        data = hashlib.sha256(self.rp_host.encode()).digest() + bytes([5]) + struct.pack(
            ">I", count
        )
        signature = self.private.sign(
            data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": encode(self.credential_id),
            "rawId": encode(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": encode(client),
                "authenticatorData": encode(data),
                "signature": encode(signature),
                "userHandle": user_id,
            },
        }


def witness_for(dashboard, credential_id=b"test-credential-id"):
    return PasskeyWitness(dashboard._passkey_rp_id, dashboard._passkey_origin, credential_id)


async def register_witness(client, dashboard, witness=None, name="Test device"):
    witness = witness or witness_for(dashboard)
    options = await (await client.post("/api/passkeys/register/options")).json()
    response = await client.post(
        "/api/passkeys/register/verify",
        json={"name": name, "credential": witness.registration(options["challenge"])},
    )
    assert response.status == 201, await response.text()
    return witness


async def login_witness(client, dashboard, witness=None):
    witness = witness or witness_for(dashboard)
    options = await (await client.post("/api/passkeys/login/options")).json()
    response = await client.post(
        "/api/passkeys/login/verify",
        json={
            "credential": witness.assertion(
                options["challenge"], dashboard._passkeys["user_id"]
            )
        },
    )
    assert response.status == 200, await response.text()
    return response


async def ensure_admin_session(client, dashboard, name="Test device"):
    """Bootstrap-register the first passkey (no session needed) and log in."""
    witness = await register_witness(client, dashboard, name=name)
    await login_witness(client, dashboard, witness)
    return witness
