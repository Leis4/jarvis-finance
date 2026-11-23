"""Synthetic server for real pinned SDK methods; no sockets or bank credentials."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import zlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.cookiejar import Cookie, CookieJar
from typing import Any
from urllib.parse import parse_qs, urlsplit

# RFC 3526 group 14; independent server computes v, B, S, M1 and M2.
N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020"
    "BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE135"
    "6D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7EDEE386BFB"
    "5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55"
    "D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3BE39E772C180E86039B27"
    "83A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D22618"
    "98FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF", 16,
)
ORIGIN = "https://online.sberbank.ru"
PRODUCTS = "/main-screen/rest/v2/m1/web/section/meta"
OPERATIONS = "/uoh-bh/v1/operations/list"
# Intentionally PUBLIC, generated disposable RSA key; never used for real secrets.
RSA_N_B64 = "7BIclh04q6znvA/8voF2MfJQSMIivT/iQw7B0HqRuJ+9hGxbeLspusnzs4scQyk77bJSpAeS/iPKvTXQB1PH5+z7jjwam9qV3BounmPgPX2hxmmbITse+0dlnGtI3vhHmOfHHje97BBduiy9nkEhXjYdgp2sZClH5gqGx5VjfUKWBfkQXyrDisW08kSigjCGmGBIFdp1vh2Y6mwY2aBAwXkbyakVNsZgC5rK0DDHBqkxe9x/VJ3OGMqZ63vJJx/hit1n6+qNlWbmB4tuHu1ZVI7oS2mmXLV/NZokDqNLll5PbN2AWz9S/wBRILuvNWAk16y/I5eIF94XjFaAQgWARQ=="
RSA_D_B64 = "VtQekZQz6OkS3xgeRU/G0n4my2wkbOlXZoTCAtViTb7rbQ6R4mks4xguu8YZ4hYDhd0GCnECvtkZU9i8KEXHjGWc7ObsfpC6eBdlODrSGqvf2eOz/FgLgC71j7/iMKivZO0zTsj/4Rjv8G9OWyvngwhyeD1YUAri+Bi9FCG9Uy96p8DCJjCWwT+Cnv1ZIiGh0WatM5ZM+t6UUC84pcul1r16rdqJBlu8joUo3ViTvRN3AvCI9LSAvAh8lAPd9NNZGVBO8i2B6iD001XC2y4jS/hVeLdRThIbqvs3R7834tdSWDsrVSF99WQkEPx/Kum1oK1qpHveMlIyoyjpnJwyTQ=="


def generated_captcha() -> bytes:
    """Render public ABCD fixture in PNG using only stdlib; never a bank image."""
    glyphs = ("01110100011000111111100011000110001", "11110100011000111110100011000111110",
              "01111100001000010000100001000001111", "11110100011000110001100011000111110")
    width, height = 160, 50
    pixels = [bytearray([255] * width) for _ in range(height)]
    for index, glyph in enumerate(glyphs):
        for y in range(7):
            for x in range(5):
                if glyph[y * 5 + x] == "1":
                    for dy in range(4):
                        for dx in range(4):
                            pixels[10 + y * 4 + dy][12 + index * 34 + x * 4 + dx] = 0
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in pixels))) + chunk(b"IEND", b""))


CAPTCHA_PNG = generated_captcha()


def _bytes(number: int) -> bytes:
    return number.to_bytes((number.bit_length() + 7) // 8, "big")


def _hash(*parts: bytes) -> bytes:
    return hashlib.sha512(b"".join(parts)).digest()


class SrpServer:
    """Independent server-side equations, with a deterministic synthetic secret b."""

    def __init__(self, password: str) -> None:
        self.salt, self.b = 0x1020304050607080, 0x31415926535897932384626433832795
        x = int.from_bytes(_hash(_bytes(self.salt), password.encode()), "big")
        self.v = pow(2, x, N)
        k = int.from_bytes(_hash(N.to_bytes(256, "big"), (2).to_bytes(256, "big")), "big")
        self.B = (k * self.v + pow(2, self.b, N)) % N

    def begin(self, public_a: str) -> dict[str, str]:
        a = int(public_a, 16)
        if not 0 < a < N:
            raise ValueError("Synthetic SRP invalid public value")
        a_bytes, b_bytes = a.to_bytes(256, "big"), self.B.to_bytes(256, "big")
        u = int.from_bytes(_hash(a_bytes, b_bytes), "big")
        shared = pow(a * pow(self.v, u, N) % N, self.b, N)
        key = _hash(shared.to_bytes(256, "big"))
        xor = bytes(x ^ y for x, y in zip(_hash(_bytes(N)), _hash(b"\x02"), strict=True))
        self.m1 = _hash(xor, _bytes(self.salt), a_bytes, b_bytes, key)
        self.m2 = _hash(a_bytes, _bytes(int.from_bytes(self.m1, "big")), key)
        return {"srp_s": format(self.salt, "x"), "srp_B": format(self.B, "x")}

    def verify(self, client_proof: str) -> str:
        supplied = int(client_proof, 16).to_bytes(64, "big")
        if not hmac.compare_digest(supplied, self.m1):
            raise ValueError("Synthetic SRP client proof rejected")
        return self.m2.hex()


def _der(tag: int, value: bytes) -> bytes:
    length = len(value)
    prefix = bytes([length]) if length < 128 else bytes([0x80 + len(_bytes(length))]) + _bytes(length)
    return bytes([tag]) + prefix + value


def _public_key() -> str:
    modulus = base64.b64decode(RSA_N_B64)
    return base64.b64encode(_der(0x30, _der(2, b"\0" + modulus) + _der(2, b"\x01\0\x01"))).decode()


def _mask(seed: bytes, size: int) -> bytes:
    return b"".join(hashlib.sha1(seed + counter.to_bytes(4, "big")).digest()
                    for counter in range((size + 19) // 20))[:size]


def _decode_pin(cipher: str) -> str:
    """Independent server RSA decryption and OAEP validation for synthetic PIN only."""
    n, d = (int.from_bytes(base64.b64decode(value), "big") for value in (RSA_N_B64, RSA_D_B64))
    raw = base64.b64decode(cipher, validate=True)
    if len(raw) != 256 or int.from_bytes(raw, "big") >= n:
        raise ValueError("Synthetic RSA invalid ciphertext")
    encoded = pow(int.from_bytes(raw, "big"), d, n).to_bytes(256, "big")
    if encoded[0] != 0:
        raise ValueError("Synthetic RSA invalid OAEP")
    seed = bytes(a ^ b for a, b in zip(encoded[1:21], _mask(encoded[21:], 20), strict=True))
    block = bytes(a ^ b for a, b in zip(encoded[21:], _mask(seed, 235), strict=True))
    if block[:20] != hashlib.sha1(b"").digest():
        raise ValueError("Synthetic RSA invalid label")
    tail = block[20:].lstrip(b"\0")
    if not tail.startswith(b"\x01"):
        raise ValueError("Synthetic RSA invalid padding")
    return tail[1:].decode("ascii")


class FixtureResponse:
    """Bounded response implementing the SDK HTTP boundary."""

    def __init__(self, payload: Any = None, *, text: str = "", status: int = 200,
                 headers: Mapping[str, str] | None = None, content: bytes | None = None) -> None:
        self.status_code, self.headers = status, dict(headers or {"content-type": "application/json"})
        self.text = text or json.dumps(payload)
        self.content = content if content is not None else self.text.encode()
        if len(self.content) > 1_048_576:
            raise ValueError("Synthetic response too large")

    def json(self) -> Any:
        return json.loads(self.text)


class ProtocolFixtureTransport:
    """Network-free allowlisted stateful server; journal contains shapes, never values."""

    def __init__(self, bundle: Any = None, *, bad_proof: bool = False, captcha: bool = True,
                 require_ready: bool = True, case: str | None = None) -> None:
        if case not in {None, "bad_proof", "missing_products", "six_operations", "missing_operations", "expired_session"}:
            raise ValueError("Unknown synthetic case")
        self.case = case
        self.jar = CookieJar()
        if bundle is not None:
            self.jar = bundle.to_seed(require_ready=require_ready).cookies
        self.srp = SrpServer("synthetic-password")
        self.bad_proof, self.captcha = bad_proof or case == "bad_proof", captcha
        self.closed, self.authenticated, self.pin_created = False, bool(list(self.jar)), False
        self.proof_verified, self.otp_pending = False, False
        self.journal: list[dict[str, Any]] = []
        self.override: FixtureResponse | Exception | None = None
        self.otp_state = "NOT_REQUIRED"

    def cookie_jar(self) -> CookieJar:
        return self.jar

    async def aclose(self) -> None:
        self.closed = True

    def _request(self, method: str, url: str, body: Mapping[str, Any] | None) -> str:
        parsed = urlsplit(url)
        path = parsed.path
        allowed = {"GET": {"/CSAFront/index.do", "/CSAFront/captcha.png", "/app/main", "/main"},
                   "FORM": {"/CSAFront/authMainJson.do"},
                   "POST": {"/CSAFront/api/v1/pin/create", "/CSAFront/api/v1/auth",
                            "/CSAFront/api/v1/pin/begin", "/CSAFront/api/v1/pin/logon", "/CSAFront/api/v1/pin/otp/confirm",
                            "/api/warmUpSession", PRODUCTS, OPERATIONS}}
        query = parse_qs(parsed.query, strict_parsing=True)
        captcha_query = (path == "/CSAFront/captcha.png" and set(query) == {"noc"}
                         and len(query["noc"]) == 1 and query["noc"][0].isascii()
                         and query["noc"][0].isdigit() and len(query["noc"][0]) <= 16)
        if self.closed or parsed.scheme != "https" or parsed.netloc != "online.sberbank.ru" or (parsed.query and not captcha_query) or parsed.fragment or path not in allowed.get(method, set()):
            raise ValueError("Synthetic transport request denied")
        if len(self.journal) >= 100:
            raise ValueError("Synthetic request budget exhausted")
        self.journal.append({"method": method, "path": path, "keys": sorted(body or {})})
        return path

    async def get(self, url: str, **kwargs: Any) -> FixtureResponse:
        path = self._request("GET", url, None)
        if path == "/CSAFront/index.do":
            config = {"authTypeByCookie": "pin" if list(self.jar) else "start", "baseApiUrl": ORIGIN + "/CSAFront",
                      "processId": "synthetic-process", "srpConfig": {"enabled": True, "N": format(N, "x"), "g": "2"},
                      "pinConfig": {"enabled": True, "hasPin": bool(list(self.jar)), "length": 5, "srp_N": format(N, "x"), "srp_g": "2"}, "validation": {"pin": {"length": 5}}}
            return FixtureResponse(text="window.config = " + json.dumps(config) + ";")
        if path == "/CSAFront/captcha.png":
            return FixtureResponse(content=CAPTCHA_PNG, headers={"content-type": "image/png"})
        if not self.pin_created and not self.authenticated:
            raise ValueError("Synthetic redirect before PIN enrollment")
        self.authenticated = True
        self._rotate()
        return FixtureResponse(text=json.dumps({"ufs.block.root.url": ORIGIN, "ufsHost": ORIGIN}))

    async def post_form(self, url: str, *, data: Mapping[str, str], **kwargs: Any) -> FixtureResponse:
        self._request("FORM", url, data)
        operation = data.get("operation")
        if operation == "button.begin":
            if data.get("login") != "synthetic-login":
                raise ValueError("Only synthetic credentials permitted")
            if self.captcha and data.get("captchaCode") != "ABCD":
                return FixtureResponse({"token": "synthetic-token", "error": {"captcha": True, "field": "captchaCode"}})
            return FixtureResponse({"token": "synthetic-token", "srpInfo": self.srp.begin(data["srp_A"])})
        if operation == "button.next" and "srp_M" in data:
            proof = self.srp.verify(data["srp_M"])
            self.proof_verified, self.otp_pending = True, True
            return FixtureResponse({"token": "synthetic-token", "srpInfo": {"srp_R": "00" if self.bad_proof else proof},
                                    "state": "NEED_CONFIRM", "timeout": 300})
        if operation in {"button.next", "button.getNewPass"} and self.otp_pending:
            if operation == "button.getNewPass":
                return FixtureResponse({"token": "synthetic-token", "timeout": 300})
            if data.get("confirmPassword") != "123456":
                return FixtureResponse({"state": "WRONG_PASS"})
            return FixtureResponse({"state": self.otp_state, "pinInfo": {"publicKey": _public_key()}})
        raise ValueError("Synthetic auth transition denied")

    def _rotate(self) -> None:
        for name in ("UFS-SESSION", "UFS-TOKEN"):
            value = f"synthetic-rotated-{name}-{len(self.journal)}"
            self.jar.set_cookie(Cookie(0, name, value, None, False, "online.sberbank.ru", True, False,
                                       "/", True, True, int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
                                       False, None, None, {"HttpOnly": ""}, False))

    async def post(self, url: str, *, json: Mapping[str, Any] | None, **kwargs: Any) -> FixtureResponse:
        path = self._request("POST", url, json)
        if self.override is not None:
            if isinstance(self.override, Exception):
                raise self.override
            return self.override
        if path == "/CSAFront/api/v1/pin/begin":
            self.pin_srp = SrpServer("31415")
            return FixtureResponse(self.pin_srp.begin(str((json or {})["srp_A"])))
        if path == "/CSAFront/api/v1/pin/logon":
            proof = self.pin_srp.verify(str((json or {})["srp_M"]))
            self.pin_created = True
            return FixtureResponse({"srp_R": "00" if self.bad_proof else proof, "confirmInfo": {"timeout": 300}})
        if path == "/CSAFront/api/v1/pin/otp/confirm":
            if (json or {}).get("confirmPassword") != "123456":
                return FixtureResponse({"error": {"code": "wrong_code"}})
            return FixtureResponse({"redirect": "/app/main"})
        if path == "/CSAFront/api/v1/pin/create":
            if not self.proof_verified or not self.otp_pending or _decode_pin(str((json or {}).get("pin"))) != "31415":
                raise ValueError("Synthetic PIN enrollment denied")
            self.pin_created = True
            return FixtureResponse({})
        if path == "/CSAFront/api/v1/auth":
            if not self.pin_created:
                raise ValueError("Synthetic auth finish denied")
            return FixtureResponse({"redirect": "/app/main"})
        if not self.authenticated:
            return FixtureResponse({}, status=401)
        self._rotate()
        if path == "/api/warmUpSession":
            if self.case == "expired_session":
                return FixtureResponse({}, status=401)
            return FixtureResponse({}, status=204)
        if path == PRODUCTS:
            if self.case == "missing_products":
                return FixtureResponse({"success": True, "body": {}})
            account = {"id": "synthetic-account", "number": "00000000000000001234", "name": "Synthetic account",
                       "balance": {"amount": "152340.00", "currencyCode": "RUB"}, "state": "OPEN"}
            cards = [{"id": f"synthetic-card-{suffix}", "number": f"00000000{suffix}", "isCTA": True,
                      "cardAccount": account["number"], "name": "Synthetic card", "state": "OPEN",
                      "availableTotalLimit": account["balance"]} for suffix in ("0090", "5083")]
            return FixtureResponse({"success": True, "body": {"sections": {"technicalSection": {"sectionProductData": {
                "ctaccounts": {"data": [account]}, "cardsInWallet": {"data": cards}}}}}})
        body = json or {}
        size, offset = body.get("paginationSize"), body.get("paginationOffset")
        if type(size) is not int or type(offset) is not int or not 1 <= size <= 101 or offset < 0:
            raise ValueError("Synthetic page invalid")
        operations = [{"uohId": f"synthetic-op-{i}", "date": "12.10.2026T10:00:00", "description": "Synthetic operation",
                       "operationAmount": {"amount": "-10.25" if i % 2 else "20.00", "currencyCode": "RUB"},
                       "billingAmount": {"amount": "-10.25" if i % 2 else "20.00", "currencyCode": "RUB"},
                       "state": {"category": "EXECUTED"}, "isFinancial": True,
                       "fromResource": {"id": (body.get("usedResource") or ["ct-account:synthetic-account"])[0]},
                       "toResource": {"id": "card:synthetic-card-0090"}} for i in range(5)]
        if self.case == "missing_operations":
            return FixtureResponse({"success": True, "body": {}})
        if self.case == "six_operations":
            operations.append({**operations[0], "uohId": "synthetic-sixth"})
            return FixtureResponse({"success": True, "body": {"operations": operations}})
        return FixtureResponse({"success": True, "body": {"operations": operations[offset:offset + size]}})
