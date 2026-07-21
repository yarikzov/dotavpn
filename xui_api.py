"""
xui_api.py — 3X-UI panel API wrapper
Handles login, client CRUD, traffic stats, and VLESS link generation.
"""

import json as _json
import logging
import os
import time
import urllib.parse as _up
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

XUI_URL        = os.getenv("XUI_URL",        "http://localhost:54321")
XUI_USERNAME   = os.getenv("XUI_USERNAME",   "admin")
XUI_PASSWORD   = os.getenv("XUI_PASSWORD",   "admin")
XUI_INBOUND_ID = int(os.getenv("XUI_INBOUND_ID", "1"))


def _retry_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://",  adapter)
    session.mount("https://", adapter)
    return session


class XUIApi:
    def __init__(self):
        self._session: Optional[requests.Session] = None
        self._last_login: float = 0

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _ensure_login(self) -> bool:
        if self._session and (time.time() - self._last_login) < 1800:
            return True
        return self._login()

    def _login(self) -> bool:
        try:
            s = _retry_session()
            resp = s.post(
                f"{XUI_URL}/login",
                data={"username": XUI_USERNAME, "password": XUI_PASSWORD},
                timeout=10,
            )
            data = resp.json()
            if data.get("success"):
                self._session    = s
                self._last_login = time.time()
                logger.info("3X-UI login successful")
                return True
            logger.error("3X-UI login failed: %s", data.get("msg"))
        except Exception as e:
            logger.error("3X-UI login error: %s", e)
        return False

    def _get(self, path: str, **kwargs) -> Optional[dict]:
        if not self._ensure_login():
            return None
        try:
            resp = self._session.get(f"{XUI_URL}{path}", timeout=10, **kwargs)
            return resp.json()
        except Exception as e:
            logger.error("XUI GET %s error: %s", path, e)
            return None

    def _post(self, path: str, **kwargs) -> Optional[dict]:
        if not self._ensure_login():
            return None
        try:
            resp = self._session.post(f"{XUI_URL}{path}", timeout=10, **kwargs)
            return resp.json()
        except Exception as e:
            logger.error("XUI POST %s error: %s", path, e)
            return None

    # ── Inbound info ──────────────────────────────────────────────────────────
    def get_inbound(self) -> Optional[dict]:
        data = self._get(f"/xui/API/inbounds/get/{XUI_INBOUND_ID}")
        if data and data.get("success"):
            return data.get("obj")
        return None

    # ── Client management ─────────────────────────────────────────────────────
    def add_client(
        self,
        uuid: str,
        email: str,
        expiry_time_ms: int,
        total_gb: int = 0,
    ) -> bool:
        payload = {
            "id": XUI_INBOUND_ID,
            "settings": _json.dumps({
                "clients": [{
                    "id":         uuid,
                    "email":      email,
                    "flow":       "xtls-rprx-vision",
                    "expiryTime": expiry_time_ms,
                    "totalGB":    total_gb * 1024 ** 3,
                    "limitIp":    0,
                    "enable":     True,
                    "tgId":       "",
                    "subId":      "",
                }],
            }),
        }
        data = self._post("/xui/API/inbounds/addClient", json=payload)
        if data and data.get("success"):
            logger.info("Added XUI client: %s / %s", email, uuid)
            return True
        logger.error("Failed to add XUI client %s: %s", email, data)
        return False

    def update_client_expiry(
        self,
        email: str,
        uuid: str,
        expiry_time_ms: int,
        total_gb: int = 0,
    ) -> bool:
        payload = {
            "id": XUI_INBOUND_ID,
            "settings": _json.dumps({
                "clients": [{
                    "id":         uuid,
                    "email":      email,
                    "flow":       "xtls-rprx-vision",
                    "expiryTime": expiry_time_ms,
                    "totalGB":    total_gb * 1024 ** 3,
                    "limitIp":    0,
                    "enable":     True,
                }],
            }),
        }
        data = self._post(f"/xui/API/inbounds/updateClient/{uuid}", json=payload)
        return bool(data and data.get("success"))

    def delete_client(self, email: str) -> bool:
        inbound = self.get_inbound()
        if not inbound:
            return False
        try:
            settings = _json.loads(inbound.get("settings", "{}"))
            clients  = settings.get("clients", [])
            uuid     = next(
                (c["id"] for c in clients if c.get("email") == email), None
            )
        except Exception:
            uuid = None

        if not uuid:
            logger.warning("delete_client: uuid not found for %s", email)
            return False

        data = self._post(f"/xui/API/inbounds/{XUI_INBOUND_ID}/delClient/{uuid}")
        if data and data.get("success"):
            logger.info("Deleted XUI client: %s", email)
            return True
        logger.error("Failed to delete XUI client %s: %s", email, data)
        return False

    def get_client_stats(self, email: str) -> Optional[dict]:
        data = self._get(f"/xui/API/inbounds/getClientTraffics/{email}")
        if data and data.get("success"):
            return data.get("obj")
        return None

    # ── VLESS link builder ────────────────────────────────────────────────────
    def get_vless_link(self, email: str, uuid: str) -> Optional[str]:
        inbound = self.get_inbound()
        if not inbound:
            return None
        try:
            stream       = _json.loads(inbound.get("streamSettings", "{}"))
            real         = stream.get("realitySettings", {})
            server_names = real.get("serverNames", [""])
            public_key   = real.get("settings", {}).get("publicKey", "")
            short_id     = (real.get("shortIds") or [""])[0]

            host = XUI_URL.split("://")[-1].split(":")[0]
            port = inbound.get("port", 443)
            sni  = server_names[0] if server_names else host

            params = _up.urlencode({
                "type":     "tcp",
                "security": "reality",
                "pbk":      public_key,
                "fp":       "chrome",
                "sni":      sni,
                "sid":      short_id,
                "spx":      "%2F",
                "flow":     "xtls-rprx-vision",
            })
            return f"vless://{uuid}@{host}:{port}?{params}#{_up.quote(email)}"
        except Exception as e:
            logger.error("get_vless_link error: %s", e)
            return None