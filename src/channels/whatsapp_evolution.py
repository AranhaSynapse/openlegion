"""WhatsApp channel adapter using Evolution API.

Drop-in replacement for the Meta Cloud API channel — uses a self-hosted
Evolution API instance instead of Meta's Business Graph API.

Same UX as the CLI REPL and the official WhatsApp channel:
  - Per-user active agent tracking (phone number as user key)
  - @agent mentions for routing to specific agents
  - /use, /agents, /status, /broadcast, /costs, /reset, /help commands
  - Agent name labels on all responses: [agent_name] response
  - Push notifications for cron/heartbeat results
  - Pairing: owner must send /start <pairing_code> to claim the bot.
    Code is generated during `openlegion start`. Others need /allow.

Uses httpx (already a core dependency) to call the Evolution API.
Webhook-based: mounts GET/POST endpoints on the mesh FastAPI app.

Required .env variables:
    EVOLUTION_API_URL            Base URL of your Evolution API (e.g. http://localhost:8080)
    EVOLUTION_API_KEY            Global or instance API key
    EVOLUTION_INSTANCE_NAME      Name of the Evolution instance (e.g. my-bot)

Optional .env variables:
    EVOLUTION_WEBHOOK_SECRET     If set, validates X-Evolution-Hmac-Sha256 header

mesh.yaml example:
    channels:
      whatsapp_evolution:
        api_url: ${EVOLUTION_API_URL}
        api_key: ${EVOLUTION_API_KEY}
        instance_name: ${EVOLUTION_INSTANCE_NAME}
        webhook_secret: ${EVOLUTION_WEBHOOK_SECRET}
        default_agent: assistant
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.channels.base import Channel, PairingManager, chunk_text
from src.shared.utils import setup_logging

logger = setup_logging("channels.whatsapp_evolution")

MAX_WA_LEN = 4096


class WhatsAppEvolutionChannel(Channel):
    """WhatsApp Evolution API adapter — self-hosted, no Meta account required."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        instance_name: str,
        webhook_secret: str = "",
        default_agent: str = "",
        **kwargs,
    ):
        super().__init__(default_agent=default_agent, **kwargs)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.instance_name = instance_name
        self.webhook_secret = webhook_secret
        self._http: httpx.AsyncClient | None = None
        self._phone_numbers: set[str] = set()
        self._denied_notified: set[str] = set()
        self._pairing = PairingManager("config/whatsapp_evolution_paired.json")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            headers={
                "apikey": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        owner = self._pairing.owner
        if owner:
            logger.info(f"WhatsApp Evolution channel started (owner: {owner})")
        elif self._pairing.pairing_code:
            logger.info("WhatsApp Evolution channel started (awaiting pairing code)")
        else:
            logger.info(
                "WhatsApp Evolution channel started (no pairing code — run setup again)"
            )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("WhatsApp Evolution channel stopped")

    # ── Sending ──────────────────────────────────────────────────────────────

    async def send_notification(self, text: str) -> None:
        """Push a cron/heartbeat notification to all known phone numbers."""
        if not self._http or not self._phone_numbers:
            return
        for phone in list(self._phone_numbers):
            try:
                for part in chunk_text(text, MAX_WA_LEN):
                    await self._send_text(phone, part)
            except Exception as e:
                logger.warning(f"Failed to notify {phone}: {e}")

    async def _send_text(self, to: str, text: str) -> None:
        """Send a text message via the Evolution API."""
        if not self._http:
            return
        # Evolution API expects only digits — strip +, spaces, dashes
        clean_number = to.replace("+", "").replace("-", "").replace(" ", "")
        url = f"{self.api_url}/message/sendText/{self.instance_name}"
        payload = {"number": clean_number, "text": text}
        try:
            await self._http.post(url, json=payload)
        except Exception as e:
            logger.warning(f"Evolution API send failed to {to}: {e}")

    # ── Pairing helpers ──────────────────────────────────────────────────────

    def _is_allowed(self, phone: str) -> bool:
        return self._pairing.is_allowed(phone)

    def _is_owner(self, phone: str) -> bool:
        return self._pairing.is_owner(phone)

    # ── Router (FastAPI) ─────────────────────────────────────────────────────

    def create_router(self) -> APIRouter:
        """Mount GET + POST webhook endpoints on the OpenLegion FastAPI app."""
        router = APIRouter(prefix="/channels/whatsapp_evolution")
        channel_ref = self

        @router.get("/webhook")
        async def verify_webhook(request: Request):
            """
            Evolution API sends a GET with ?hub.mode=subscribe to verify the
            webhook URL during instance setup. We simply return 200 OK.
            """
            logger.info("WhatsApp Evolution webhook verification request received")
            return JSONResponse({"status": "ok"})

        @router.post("/webhook")
        async def receive_webhook(request: Request):
            """Receive incoming messages from Evolution API."""
            # Optional HMAC signature check
            if channel_ref.webhook_secret:
                raw_body = await request.body()
                sig = request.headers.get("X-Evolution-Hmac-Sha256", "")
                expected = hmac.new(
                    channel_ref.webhook_secret.encode(),
                    raw_body,
                    hashlib.sha256,
                ).hexdigest()
                if not hmac.compare_digest(sig, expected):
                    logger.warning("Evolution webhook: invalid signature — ignoring")
                    return JSONResponse({"status": "invalid_signature"})

            try:
                body = await request.json()
            except Exception as e:
                logger.warning("Evolution webhook: failed to parse payload: %s", e)
                return JSONResponse({"status": "ok"})

            event = body.get("event", "")

            # Evolution API emits "messages.upsert" for new incoming messages
            if event == "messages.upsert":
                data = body.get("data", {})
                # data can be a single message object or a list
                messages = data if isinstance(data, list) else [data]
                for message in messages:
                    asyncio.create_task(channel_ref._process_message(message))

            return JSONResponse({"status": "ok"})

        return router

    # ── Message processing ───────────────────────────────────────────────────

    async def _process_message(self, message: dict) -> None:
        """Process a single incoming Evolution API message payload."""
        # Evolution API message shape:
        # { key: { remoteJid: "5511999999999@s.whatsapp.net", fromMe: false },
        #   message: { conversation: "hello" } }
        key = message.get("key", {})
        if key.get("fromMe", False):
            return  # ignore messages sent by the bot itself

        remote_jid = key.get("remoteJid", "")
        # Extract bare phone number from JID (e.g. "5511999999999@s.whatsapp.net")
        phone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not phone:
            return

        # Extract text from various message types
        msg_content = message.get("message", {})
        text = (
            msg_content.get("conversation")
            or msg_content.get("extendedTextMessage", {}).get("text")
            or ""
        ).strip()

        if not text:
            # Non-text message (image, audio, etc.)
            if self._pairing.owner and self._is_allowed(phone):
                await self._send_text(
                    phone, "Só consigo processar mensagens de texto por enquanto."
                )
            logger.info(f"Skipping non-text message from {phone}")
            return

        # ── Pairing flow ─────────────────────────────────────────────────────
        if self._pairing.owner is None:
            if text.lower().startswith("/start") or text.lower().startswith("!start"):
                parts = text.split(None, 1)
                code_arg = parts[1].strip() if len(parts) > 1 else ""
                expected = self._pairing.pairing_code
                if not expected or code_arg != expected:
                    await self._send_text(
                        phone,
                        "Pareamento necessário. Envie:\n  /start <pairing_code>\n"
                        "O código foi exibido durante `openlegion start`.",
                    )
                    logger.warning(f"Rejected /start without valid pairing code from {phone}")
                    return
                self._pairing.claim_owner(phone)
                logger.info(f"Paired owner via code: {phone}")
                self._phone_numbers.add(phone)
                await self._send_text(
                    phone,
                    f"✅ Pareado como dono. Seu número: {phone}\n"
                    "Use /allow <número> para liberar outros usuários.",
                )
                try:
                    help_text = await self.handle_message(phone, "/help")
                    if help_text:
                        for part in chunk_text(help_text, MAX_WA_LEN):
                            await self._send_text(phone, part)
                except Exception as e:
                    logger.debug("Help after pairing failed: %s", e)
                return
            else:
                await self._send_text(
                    phone,
                    "Este bot requer pareamento. Envie /start <pairing_code> para começar.",
                )
                return

        if not self._is_allowed(phone):
            if text.lower().startswith("/start") or text.lower().startswith("!start"):
                await self._send_text(
                    phone,
                    f"Acesso negado. Este bot já está pareado.\n"
                    f"Seu número: {phone}\n"
                    f"Peça ao dono para enviar: /allow {phone}",
                )
            elif phone not in self._denied_notified:
                self._denied_notified.add(phone)
                await self._send_text(
                    phone,
                    f"Acesso negado. Peça ao dono do bot para liberar seu número.\n"
                    f"Seu número: {phone}",
                )
            return

        # ── Owner-only commands ───────────────────────────────────────────────
        if text.startswith("/allow ") or text.startswith("!allow "):
            if not self._is_owner(phone):
                await self._send_text(phone, "Apenas o dono pode usar /allow.")
                return
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                await self._send_text(phone, "Uso: /allow <número>")
                return
            target = parts[1].strip()
            self._pairing.allow(target)
            await self._send_text(phone, f"✅ Usuário {target} liberado.")
            logger.info(f"Owner allowed user {target}")
            return

        if text.startswith("/revoke ") or text.startswith("!revoke "):
            if not self._is_owner(phone):
                await self._send_text(phone, "Apenas o dono pode usar /revoke.")
                return
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                await self._send_text(phone, "Uso: /revoke <número>")
                return
            target = parts[1].strip()
            if self._pairing.revoke(target):
                await self._send_text(phone, f"✅ Acesso de {target} revogado.")
            else:
                await self._send_text(phone, f"Usuário {target} não estava na lista.")
            return

        if text.lower() in ("/paired", "!paired"):
            if not self._is_owner(phone):
                await self._send_text(phone, "Apenas o dono pode ver info de pareamento.")
                return
            owner = self._pairing.owner
            allowed = self._pairing.allowed_list()
            lines = [f"Dono: {owner}"]
            if allowed:
                lines.append(f"Usuários liberados: {', '.join(allowed)}")
            else:
                lines.append("Nenhum usuário adicional liberado.")
            await self._send_text(phone, "\n".join(lines))
            return

        # ── Regular message dispatch ──────────────────────────────────────────
        self._phone_numbers.add(phone)

        # Translate ! prefix to / for base class
        if text.startswith("!"):
            text = "/" + text[1:]

        try:
            response = await self.handle_message(phone, text)
        except Exception as e:
            logger.error(f"Dispatch failed for {phone}: {e}")
            response = f"Erro: {e}"

        if response:
            for part in chunk_text(response, MAX_WA_LEN):
                try:
                    await self._send_text(phone, part)
                except Exception as e:
                    logger.warning(f"Failed to send response to {phone}: {e}")
