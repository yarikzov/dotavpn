"""
crypto_api.py — CryptoPay (@CryptoBot) API wrapper
"""

import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_URL = os.getenv("CRYPTO_PAY_URL", "https://pay.crypt.bot/api")


class CryptoPayAPI:
    def __init__(self):
        self._token = CRYPTO_PAY_TOKEN
        self._headers = {"Crypto-Pay-API-Token": self._token}

    async def _post(self, method: str, **body) -> Optional[dict]:
        if not self._token:
            logger.error("CRYPTO_PAY_TOKEN not set")
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{CRYPTO_PAY_URL}/{method}",
                    headers=self._headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return data.get("result")
                    logger.error("CryptoPay %s error: %s", method, data.get("error"))
        except Exception as e:
            logger.error("CryptoPay POST %s error: %s", method, e)
        return None

    async def _get(self, method: str, **params) -> Optional[dict]:
        if not self._token:
            logger.error("CRYPTO_PAY_TOKEN not set")
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CRYPTO_PAY_URL}/{method}",
                    headers=self._headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        return data.get("result")
                    logger.error("CryptoPay %s error: %s", method, data.get("error"))
        except Exception as e:
            logger.error("CryptoPay GET %s error: %s", method, e)
        return None

    async def create_invoice(
        self,
        amount: float,
        asset: str = "USDT",
        description: str = "VPN subscription",
        payload: str = "",
    ) -> Optional[dict]:
        result = await self._post(
            "createInvoice",
            currency_type="crypto",
            asset=asset,
            amount=str(round(amount, 2)),
            description=description,
            payload=payload,
            paid_btn_name="callback",
            paid_btn_url=f"https://t.me/{os.getenv('BOT_USERNAME', 'VpnDotaBot')}",
        )
        if result:
            return {
                "invoice_id": result["invoice_id"],
                "pay_url": result["pay_url"],
                "status": result["status"],
            }
        return None

    async def check_invoice(self, invoice_id: int) -> str:
        result = await self._get("getInvoices", invoice_ids=invoice_id)
        if result and result.get("items"):
            return result["items"][0].get("status", "active")
        return "active"

    async def get_invoice_payload(self, invoice_id: int) -> Optional[str]:
        result = await self._get("getInvoices", invoice_ids=invoice_id)
        if result and result.get("items"):
            return result["items"][0].get("payload")
        return None

    async def get_balance(self) -> Optional[list]:
        return await self._get("getBalance")