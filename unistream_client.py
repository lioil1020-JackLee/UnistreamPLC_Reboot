from __future__ import annotations

import base64
import asyncio
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from asyncua import Client as AsyncUaClient

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from websockets.sync.client import connect


USERNAME = "UniLogicUser"
HWVER_COMMAND = "/v3/hwVer"
SWVER_COMMAND = "/v1/swVer"
REBOOT_COMMAND = '/v1/put/workingMode {"mode":"Reboot"} '
PLC_PASSWORD_RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAt+lkeuDKEV5cFt6OAYQF
bN5lDGwSCA6OXawZXl3eg/+eHP7zm2FyL945NCvETzOcSCmyKlSIwQhJjlW1haYI
yNmqRAY7ilZwUCmiYuC+dCq9/Swl9kSzSSN06NgnNP3bydznq2X8PE7cl7CrEH6Z
4vFNOjoqNfMvvSSeogR0RnjekZm/aFQqcj4buk1uFNQQR9IEo2g+5786L7G/eeQB
ZJsGyXkhsZHoX+Ckdk1HJGyZZQWBP0WZt6SnnYLzArZIUwpFu1YCz2bmCeVcG+Cg
5YYb+TL2/qgTp4rYTTON0b3pcnp39+Vc1OEoQd2gamwCkV6t+ovdS5ysD91Kvg4Y
1QIDAQAB
-----END PUBLIC KEY-----
"""


class PLCError(RuntimeError):
    pass


@dataclass(slots=True)
class OperationResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class OperationLogger:
    lines: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.lines.append(message.rstrip())

    def dump(self) -> str:
        if not self.lines:
            return ""
        return "\n".join(self.lines) + "\n"


def build_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def export_public_key_pem(private_key: rsa.RSAPrivateKey) -> str:
    public_key = private_key.public_key()
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return public_bytes.decode("ascii")


def encrypt_plc_password(password: str) -> str:
    public_key = serialization.load_pem_public_key(PLC_PASSWORD_RSA_PUBLIC_KEY.encode("ascii"))
    encrypted = public_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_ws_token(value: str | list[int], private_key: rsa.RSAPrivateKey) -> str:
    if isinstance(value, str):
        cipher_bytes = base64.b64decode(value)
    elif isinstance(value, list):
        cipher_bytes = bytes(value)
    else:
        raise PLCError(f"Unsupported login token format: {type(value).__name__}")

    token = private_key.decrypt(cipher_bytes, padding.PKCS1v15())
    return token.decode("utf-8")


def normalize_message(message: str | bytes) -> str:
    if isinstance(message, bytes):
        return message.decode("utf-8", "replace")
    return message


def parse_api_response(message: str | bytes) -> dict:
    message = normalize_message(message)
    try:
        payload = json.loads(message)
    except json.JSONDecodeError as exc:
        raise PLCError(f"Received non-JSON response: {message}") from exc

    if not isinstance(payload, dict):
        raise PLCError(f"Unexpected response shape: {payload!r}")
    return payload


def response_status_ok(payload: dict) -> bool:
    status = str(payload.get("status", ""))
    error = str(payload.get("error", ""))
    return status == "200" and error.upper() == "OK"


class UniStreamClient:
    def __init__(self, ip: str, port: int, password: str, logger: OperationLogger) -> None:
        self.ip = ip
        self.port = port
        self.password = password
        self.logger = logger
        self.ssl_context = build_ssl_context()
        self.https_base = f"https://{ip}:{port}"
        self.wss_url = f"wss://{ip}:{port}/"

    def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"{self.https_base}{path}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url=url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

        try:
            with urllib.request.urlopen(request, context=self.ssl_context, timeout=10) as response:
                status_code = response.getcode()
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise PLCError(f"Cannot reach {url}: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PLCError(f"Invalid JSON from {path}: {raw}") from exc

        return status_code, payload

    def _get_json(self, path: str) -> tuple[int, dict]:
        url = f"{self.https_base}{path}"
        request = urllib.request.Request(
            url=url,
            method="GET",
            headers={"Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, context=self.ssl_context, timeout=10) as response:
                status_code = response.getcode()
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw = exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise PLCError(f"Cannot reach {url}: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PLCError(f"Invalid JSON from {path}: {raw}") from exc

        return status_code, payload

    def check(self) -> None:
        self.logger.add(f"Attempting PLC communication check at {self.https_base}{HWVER_COMMAND}")
        status_code, response = self._get_json(HWVER_COMMAND)
        self.logger.add(f"Received: {json.dumps(response, ensure_ascii=False)}")

        if status_code != 200:
            raise PLCError(f"PLC communication check failed: HTTP {status_code} {json.dumps(response, ensure_ascii=False)}")

        self.logger.add("PLC communication OK")

    def login(self) -> str:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key_pem = export_public_key_pem(private_key)
        encrypted_password = encrypt_plc_password(self.password)

        self.logger.add(f"Attempting login to {self.https_base}/v1/login")
        status_code, response = self._post_json(
            "/v1/login",
            {
                "username": USERNAME,
                "password": encrypted_password,
                "key": public_key_pem,
            },
        )

        login_summary = {"http": status_code}
        for key in ("status", "error"):
            if key in response:
                login_summary[key] = response.get(key)
        if isinstance(response.get("key"), str):
            login_summary["keyLength"] = len(response["key"])
        if isinstance(response.get("result"), str):
            login_summary["resultLength"] = len(response["result"])
        self.logger.add(f"Received: {json.dumps(login_summary, ensure_ascii=False)}")

        if status_code != 200:
            if response.get("result") == "fail":
                raise PLCError("Login failed: unauthorized PLC password.")
            raise PLCError(f"Login failed: HTTP {status_code} {json.dumps(response, ensure_ascii=False)}")

        encrypted_token = response.get("key")
        if not isinstance(encrypted_token, str):
            raise PLCError(f"Login response did not contain an encrypted WebSocket token: {json.dumps(response, ensure_ascii=False)}")

        token = decrypt_ws_token(encrypted_token, private_key)
        self.logger.add(f"Decrypted WebSocket auth token ({len(token)} chars)")
        return token

    def _authenticate_websocket(self, ws, token: str) -> None:
        ws.send(token)
        self.logger.add(f"Sent WebSocket auth token ({len(token)} chars)")
        message = normalize_message(ws.recv())
        self.logger.add(f"Received: {message}")
        payload = parse_api_response(message)
        if not response_status_ok(payload):
            raise PLCError(f"WebSocket authentication failed: {message}")

    def _send_text_command(self, ws, command: str) -> dict:
        ws.send(command)
        self.logger.add(f"Sent: {command} ({len(command.encode('utf-8'))} bytes)")
        message = normalize_message(ws.recv())
        self.logger.add(f"Received: {message}")
        payload = parse_api_response(message)
        if not response_status_ok(payload):
            raise PLCError(f"PLC returned error for {command}: {message}")
        return payload

    def validate(self) -> None:
        token = self.login()
        self.logger.add(f"Attempting to connect to {self.wss_url}")

        try:
            with connect(self.wss_url, ssl=self.ssl_context, open_timeout=10, close_timeout=2) as ws:
                self.logger.add(f"Connected to {self.wss_url}")
                self._authenticate_websocket(ws, token)
                swver_payload = self._send_text_command(ws, SWVER_COMMAND)

                data = swver_payload.get("data")
                if isinstance(data, str):
                    try:
                        json.loads(data)
                    except json.JSONDecodeError:
                        pass

        except OSError as exc:
            raise PLCError(f"Cannot open WebSocket {self.wss_url}: {exc}") from exc

        self.logger.add("Validated")

    def reboot(self) -> None:
        token = self.login()
        self.logger.add(f"Attempting to connect to {self.wss_url}")

        try:
            with connect(self.wss_url, ssl=self.ssl_context, open_timeout=10, close_timeout=2) as ws:
                self.logger.add(f"Connected to {self.wss_url}")
                self._authenticate_websocket(ws, token)
                self._send_text_command(ws, SWVER_COMMAND)
                reboot_payload = self._send_text_command(ws, REBOOT_COMMAND)
        except OSError as exc:
            raise PLCError(f"Cannot open WebSocket {self.wss_url}: {exc}") from exc

        data = reboot_payload.get("data")
        reboot_ok = data is True or str(data).lower() == "true"
        self.logger.add(f"reboot={reboot_ok}")
        if not reboot_ok:
            raise PLCError(f"PLC did not accept reboot command: {json.dumps(reboot_payload, ensure_ascii=False)}")


def run_operation(command: str, ip: str, port: int, password: str | None) -> OperationResult:
    logger = OperationLogger()
    password = password or ""
    client = UniStreamClient(ip=ip, port=port, password=password, logger=logger)

    try:
        if command == "check":
            client.check()
        elif command == "validate":
            client.validate()
        elif command == "reboot":
            client.reboot()
        else:
            raise PLCError(f"Unsupported command: {command}")
    except PLCError as exc:
        logger.add(str(exc))
        return OperationResult(returncode=4, stdout=logger.dump())
    except Exception as exc:  # pragma: no cover - safety net
        return OperationResult(returncode=99, stdout=logger.dump(), stderr=str(exc))

    return OperationResult(returncode=0, stdout=logger.dump())


def validate_plc(ip: str, port: int, password: str | None) -> OperationResult:
    return run_operation("validate", ip=ip, port=port, password=password)


def reboot_plc(ip: str, port: int, password: str | None) -> OperationResult:
    return run_operation("reboot", ip=ip, port=port, password=password)


def check_plc(ip: str, port: int, password: str | None = None) -> OperationResult:
    return run_operation("check", ip=ip, port=port, password=password)


def check_opcua(ip: str, opc_port: int) -> OperationResult:
    logger = OperationLogger()
    endpoint = f"opc.tcp://{ip}:{opc_port}"

    logger.add(f"Attempting OPC UA communication check at {endpoint}")

    async def _check() -> None:
        client = AsyncUaClient(url=endpoint)
        client.set_security_string("None")
        client.application_uri = "urn:lioil:unistream:opcua-check"
        # asyncua defaults to anonymous if username/password are not provided.
        await client.connect()
        await client.disconnect()

    try:
        asyncio.run(_check())
        logger.add("OPC UA communication OK")
        return OperationResult(returncode=0, stdout=logger.dump())
    except Exception as exc:
        logger.add(f"OPC UA communication failed: {exc}")
        return OperationResult(returncode=4, stdout=logger.dump())
