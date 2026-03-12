"""WhatsApp multi-instance channel adapter using Evolution API.

Extends the single-instance adapter to support N simultaneous WhatsApp
numbers — one per clinic — all served by a single OpenLegion process.

How it works
------------
1. On startup, the channel calls ``GET /instance/fetchInstances`` on the
   Evolution API and registers a FastAPI webhook route for every instance
   it finds:  ``/channels/whatsapp_evolution_multi/{instance_name}/webhook``

2. A background task polls for new/removed instances every
   ``EVOLUTION_MULTI_POLL_INTERVAL`` seconds (default 60) so you can add
   a new clinic without restarting OpenLegion.

3. Each instance is mapped to a *clinic context* that is injected into
   every agent call as extra system context, so the same ``recepcionista``
   agent automatically answers as "Clínica Alfa" or "Clínica Beta" etc.

4. The clinic→instance mapping comes from ``config/clinics.yaml`` (or env
   var ``EVOLUTION_CLINIC_MAP`` as JSON).  If no mapping is found the
   instance name itself is used as the clinic identifier.

mesh.yaml example
-----------------
    channels:
      whatsapp_evolution_multi:
        api_url: ${EVOLUTION_API_URL}
        api_key: ${EVOLUTION_API_KEY}
        default_agent: recepcionista
        poll_interval: 60          # seconds between instance discovery
        webhook_secret: ${EVOLUTION_WEBHOOK_SECRET}

config/clinics.yaml example
---------------------------
    clinics:
      clinica-alfa:
        clinic_id: "alfa"
        display_name: "Clínica Alfa"
        address: "Rua das Flores, 100 - Porto Alegre"
        phone_display: "+55 51 3000-0001"
        agent: recepcionista        # override default agent (optional)
      clinica-beta:
        clinic_id: "beta"
        display_name: "Clínica Beta"
        address: "Av. Ipiranga, 200 - Porto Alegre"
        phone_display: "+55 51 3000-0002"
        agent: recepcionista

Required .env variables
-----------------------
    EVOLUTION_API_URL       Base URL of your Evolution API
    EVOLUTION_API_KEY       Global API key

Optional .env variables
-----------------------
    EVOLUTION_WEBHOOK_SECRET   HMAC secret for webhook signature validation
    EVOLUTION_MULTI_POLL_INTERVAL   Seconds between instance refresh (default 60)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.channels.base import Channel, PairingManager, chunk_text
from src.shared.utils import setup_logging

logger = setup_logging("channels.whatsapp_evolution_multi")

MAX_WA_LEN = 4096
DEFAULT_POLL_INTERVAL = 60  # seconds
_CLINICS_CONFIG_PATH = Path("config/clinics.yaml")


# ── Clinic context ────────────────────────────────────────────────────────────

class ClinicContext:
    """Metadata for a single clinic/WhatsApp instance."""

    def __init__(
        self,
        instance_name: str,
        clinic_id: str = "",
        display_name: str = "",
        address: str = "",
        phone_display: str = "",
        agent: str = "",
    ):
        self.instance_name = instance_name
        self.clinic_id = clinic_id or instance_name
        self.display_name = display_name or instance_name
        self.address = address
        self.phone_display = phone_display
        self.agent = agent  # optional per-clinic agent override

    def system_context(self) -> str:
        """Extra system prompt injected for every message from this clinic."""
        lines = [
            f"Você está atendendo pelo WhatsApp da {self.display_name}.",
        ]
        if self.address:
            lines.append(f"Endereço da clínica: {self.address}")
        if self.phone_display:
            lines.append(f"Telefone da clínica: {self.phone_display}")
        lines.append(f"ID interno da clínica: {self.clinic_id}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, instance_name: str, data: dict) -> "ClinicContext":
        return cls(
            instance_name=instance_name,
            clinic_id=data.get("clinic_id", instance_name),
            display_name=data.get("display_name", instance_name),
            address=data.get("address", ""),
            phone_display=data.get("phone_display", ""),
            agent=data.get("agent", ""),
        )


def _load_clinics_config() -> dict[str, dict]:
    """Load instance→clinic mapping from config/clinics.yaml or EVOLUTION_CLINIC_MAP env."""
    # Env var takes precedence (useful for Docker secrets / CI)
    env_map = os.getenv("EVOLUTION_CLINIC_MAP", "")
    if env_map:
        try:
            return json.loads(env_map)
        except json.JSONDecodeError:
            logger.warning("EVOLUTION_CLINIC_MAP is not valid JSON — ignoring")

    if _CLINICS_CONFIG_PATH.exists():
        try:
            data = yaml.safe_load(_CLINICS_CONFIG_PATH.read_text())
            return data.get("clinics", {}) if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to load {_CLINICS_CONFIG_PATH}: {e}")

    return {}


# ── Per-instance sub-channel ──────────────────────────────────────────────────

class EvolutionInstance:
    """Manages one WhatsApp instance: HTTP client, pairing, message dispatch."""

    def __init__(
        self,
        clinic: ClinicContext,
        api_url: str,
        api_key: str,
        webhook_secret: str,
        parent: "WhatsAppEvolutionMultiChannel",
    ):
        self.clinic = clinic
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.parent = parent
        self._http: httpx.AsyncClient | None = None
        self._phone_numbers: set[str] = set()
        self._denied_notified: set[str] = set()
        pairing_file = f"config/whatsapp_evolution_multi_{clinic.instance_name}_paired.json"
        self._pairing = PairingManager(pairing_file)

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            headers={"apikey": self.api_key, "Content-Type": "application/json"},
            timeout=30,
        )
        logger.info(
            f"[{self.clinic.display_name}] Instance started "
            f"(instance={self.clinic.instance_name}, clinic_id={self.clinic.clinic_id})"
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def send_text(self, to: str, text: str) -> None:
        if not self._http:
            return
        clean = to.replace("+", "").replace("-", "").replace(" ", "")
        url = f"{self.api_url}/message/sendText/{self.clinic.instance_name}"
        for part in chunk_text(text, MAX_WA_LEN):
            try:
                await self._http.post(url, json={"number": clean, "text": part})
            except Exception as e:
                logger.warning(f"[{self.clinic.instance_name}] Send failed to {to}: {e}")

    async def process_webhook(self, request: Request) -> JSONResponse:
        """Handle incoming webhook POST from Evolution API for this instance."""
        if self.webhook_secret:
            raw_body = await request.body()
            sig = request.headers.get("X-Evolution-Hmac-Sha256", "")
            expected = hmac.new(
                self.webhook_secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logger.warning(f"[{self.clinic.instance_name}] Invalid webhook signature")
                return JSONResponse({"status": "invalid_signature"})

        try:
            body = await request.json()
        except Exception as e:
            logger.warning(f"[{self.clinic.instance_name}] Failed to parse payload: {e}")
            return JSONResponse({"status": "ok"})

        if body.get("event") == "messages.upsert":
            data = body.get("data", {})
            messages = data if isinstance(data, list) else [data]
            for message in messages:
                asyncio.create_task(self._process_message(message))

        return JSONResponse({"status": "ok"})

    async def _process_message(self, message: dict) -> None:
        key = message.get("key", {})
        if key.get("fromMe", False):
            return

        remote_jid = key.get("remoteJid", "")
        phone = remote_jid.split("@")[0] if "@" in remote_jid else remote_jid
        if not phone:
            return

        msg_content = message.get("message", {})
        text = (
            msg_content.get("conversation")
            or msg_content.get("extendedTextMessage", {}).get("text")
            or ""
        ).strip()

        if not text:
            if self._pairing.owner and self._pairing.is_allowed(phone):
                await self.send_text(phone, "Só consigo processar mensagens de texto por enquanto.")
            return

        # ── Pairing flow ──────────────────────────────────────────────────────
        if self._pairing.owner is None:
            if text.lower().startswith("/start") or text.lower().startswith("!start"):
                parts = text.split(None, 1)
                code_arg = parts[1].strip() if len(parts) > 1 else ""
                expected = self._pairing.pairing_code
                if not expected or code_arg != expected:
                    await self.send_text(
                        phone,
                        f"[{self.clinic.display_name}] Pareamento necessário.\n"
                        "Envie: /start <pairing_code>",
                    )
                    return
                self._pairing.claim_owner(phone)
                self._phone_numbers.add(phone)
                await self.send_text(
                    phone,
                    f"✅ Pareado como dono da {self.clinic.display_name}.\n"
                    f"Número: {phone}\nUse /allow <número> para liberar outros usuários.",
                )
                return
            else:
                await self.send_text(
                    phone,
                    f"[{self.clinic.display_name}] Bot requer pareamento.\n"
                    "Envie /start <pairing_code> para começar.",
                )
                return

        if not self._pairing.is_allowed(phone):
            if phone not in self._denied_notified:
                self._denied_notified.add(phone)
                await self.send_text(
                    phone,
                    f"Acesso negado. Peça ao administrador da {self.clinic.display_name} "
                    f"para liberar seu número: {phone}",
                )
            return

        # ── Owner-only commands ───────────────────────────────────────────────
        if text.startswith(("/allow ", "!allow ")):
            if not self._pairing.is_owner(phone):
                await self.send_text(phone, "Apenas o dono pode usar /allow.")
                return
            target = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if target:
                self._pairing.allow(target)
                await self.send_text(phone, f"✅ {target} liberado na {self.clinic.display_name}.")
            return

        if text.startswith(("/revoke ", "!revoke ")):
            if not self._pairing.is_owner(phone):
                await self.send_text(phone, "Apenas o dono pode usar /revoke.")
                return
            target = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            if target and self._pairing.revoke(target):
                await self.send_text(phone, f"✅ Acesso de {target} revogado.")
            return

        # ── Regular dispatch with clinic context injected ─────────────────────
        self._phone_numbers.add(phone)
        if text.startswith("!"):
            text = "/" + text[1:]

        # Determine which agent handles this clinic
        agent_name = self.clinic.agent or self.parent.default_agent or None

        # Inject clinic context as a system prefix so the agent "knows" which clinic it is
        clinic_ctx = self.clinic.system_context()
        contextualized_text = f"[CONTEXT]\n{clinic_ctx}\n[/CONTEXT]\n\n{text}"

        try:
            response = await self.parent.handle_message(
                phone,
                contextualized_text,
                agent=agent_name,
                channel_id=self.clinic.instance_name,
            )
        except TypeError:
            # Fallback if handle_message doesn't accept extra kwargs
            try:
                response = await self.parent.handle_message(phone, contextualized_text)
            except Exception as e:
                logger.error(f"[{self.clinic.instance_name}] Dispatch failed for {phone}: {e}")
                response = "Desculpe, ocorreu um erro. Tente novamente em instantes."
        except Exception as e:
            logger.error(f"[{self.clinic.instance_name}] Dispatch failed for {phone}: {e}")
            response = "Desculpe, ocorreu um erro. Tente novamente em instantes."

        if response:
            await self.send_text(phone, response)


# ── Main multi-instance channel ───────────────────────────────────────────────

class WhatsAppEvolutionMultiChannel(Channel):
    """Multi-instance Evolution API channel — one WhatsApp number per clinic.

    Discovers instances dynamically from Evolution API and routes each
    incoming message to the correct agent with clinic context injected.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        webhook_secret: str = "",
        default_agent: str = "",
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        **kwargs,
    ):
        super().__init__(default_agent=default_agent, **kwargs)
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.poll_interval = int(poll_interval)
        self._instances: dict[str, EvolutionInstance] = {}  # instance_name → EvolutionInstance
        self._router: APIRouter | None = None
        self._poll_task: asyncio.Task | None = None
        self._http: httpx.AsyncClient | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            headers={"apikey": self.api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        await self._discover_instances()
        # Start background polling for new/removed instances
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"WhatsAppEvolutionMultiChannel started — "
            f"{len(self._instances)} instance(s) registered"
        )

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        for inst in self._instances.values():
            await inst.stop()
        self._instances.clear()
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("WhatsAppEvolutionMultiChannel stopped")

    # ── Instance discovery ────────────────────────────────────────────────────

    async def _fetch_instances(self) -> list[str]:
        """Return list of instance names from Evolution API."""
        if not self._http:
            return []
        try:
            resp = await self._http.get(f"{self.api_url}/instance/fetchInstances")
            resp.raise_for_status()
            data = resp.json()
            # Response is a list of dicts with at least {"instance": {"instanceName": ...}}
            names = []
            for item in data:
                if isinstance(item, dict):
                    inst_block = item.get("instance", {})
                    name = inst_block.get("instanceName") or item.get("instanceName")
                    if name:
                        names.append(name)
            return names
        except Exception as e:
            logger.warning(f"Failed to fetch instances from Evolution API: {e}")
            return []

    async def _discover_instances(self) -> None:
        """Sync running EvolutionInstance objects with what Evolution API reports."""
        clinics_cfg = _load_clinics_config()
        instance_names = await self._fetch_instances()

        current = set(self._instances.keys())
        discovered = set(instance_names)

        # Add new instances
        for name in discovered - current:
            clinic_data = clinics_cfg.get(name, {})
            clinic = ClinicContext.from_dict(name, clinic_data)
            inst = EvolutionInstance(
                clinic=clinic,
                api_url=self.api_url,
                api_key=self.api_key,
                webhook_secret=self.webhook_secret,
                parent=self,
            )
            await inst.start()
            self._instances[name] = inst
            logger.info(f"Registered new instance: {name} → clinic '{clinic.display_name}'")

        # Remove stale instances
        for name in current - discovered:
            await self._instances[name].stop()
            del self._instances[name]
            logger.info(f"Removed stale instance: {name}")

    async def _poll_loop(self) -> None:
        """Background task: periodically re-discover instances."""
        while True:
            try:
                await asyncio.sleep(self.poll_interval)
                await self._discover_instances()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Instance poll error: {e}")

    # ── Notifications ─────────────────────────────────────────────────────────

    async def send_notification(self, text: str) -> None:
        """Broadcast to all known phone numbers across all instances."""
        for inst in self._instances.values():
            for phone in list(inst._phone_numbers):
                try:
                    await inst.send_text(phone, text)
                except Exception as e:
                    logger.warning(f"Notification failed {inst.clinic.instance_name}/{phone}: {e}")

    # ── FastAPI Router ────────────────────────────────────────────────────────

    def create_router(self) -> APIRouter:
        """Mount one GET + POST webhook per instance under a shared prefix."""
        router = APIRouter(prefix="/channels/whatsapp_evolution_multi")
        channel_ref = self

        @router.get("/webhook/{instance_name}")
        async def verify_webhook(instance_name: str):
            logger.info(f"Webhook verification for instance: {instance_name}")
            return JSONResponse({"status": "ok", "instance": instance_name})

        @router.post("/webhook/{instance_name}")
        async def receive_webhook(instance_name: str, request: Request):
            inst = channel_ref._instances.get(instance_name)
            if inst is None:
                # Unknown instance — try a refresh then retry once
                logger.warning(
                    f"Unknown instance '{instance_name}' — triggering discovery"
                )
                await channel_ref._discover_instances()
                inst = channel_ref._instances.get(instance_name)
            if inst is None:
                logger.warning(f"Instance '{instance_name}' still not found after refresh")
                return JSONResponse({"status": "ok"})
            return await inst.process_webhook(request)

        @router.get("/instances")
        async def list_instances():
            """Health/status endpoint — list all registered instances."""
            return JSONResponse({
                "count": len(channel_ref._instances),
                "instances": [
                    {
                        "instance_name": name,
                        "clinic_id": inst.clinic.clinic_id,
                        "display_name": inst.clinic.display_name,
                        "active_users": len(inst._phone_numbers),
                        "webhook_url": f"/channels/whatsapp_evolution_multi/webhook/{name}",
                    }
                    for name, inst in channel_ref._instances.items()
                ],
            })

        self._router = router
        return router
