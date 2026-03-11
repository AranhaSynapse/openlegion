# WhatsApp via Evolution API — Setup Guide

Este fork adiciona o canal `whatsapp_evolution` ao OpenLegion — uma alternativa
self-hosted ao canal WhatsApp oficial (que exige uma conta Meta Business).

## O que é a Evolution API?

[Evolution API](https://github.com/EvolutionAPI/evolution-api) é um servidor
open-source que expõe uma API HTTP para instâncias do WhatsApp via Baileys.
Você roda na sua própria VPS — sem conta Meta, sem aprovação de app, sem custo de API.

## Pré-requisitos

- Docker e Docker Compose na sua VPS
- Uma instância do Evolution API rodando (ver abaixo)
- Número de WhatsApp disponível para parear com a Evolution API

---

## 1. Subir a Evolution API na VPS

```yaml
# docker-compose.evolution.yml
version: "3.8"
services:
  evolution:
    image: atendai/evolution-api:latest
    container_name: evolution_api
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      SERVER_URL: http://localhost:8080
      AUTHENTICATION_TYPE: apikey
      AUTHENTICATION_API_KEY: SUA_CHAVE_AQUI
      AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES: "true"
      QRCODE_LIMIT: 30
      WEBSOCKET_ENABLED: "false"
    volumes:
      - evolution_data:/evolution/instances
volumes:
  evolution_data:
```

```bash
docker compose -f docker-compose.evolution.yml up -d
```

## 2. Criar instância e parear

```bash
# Criar instância
curl -X POST http://SEU_IP:8080/instance/create \
  -H "apikey: SUA_CHAVE_AQUI" \
  -H "Content-Type: application/json" \
  -d '{"instanceName": "meu-bot", "qrcode": true}'

# Ver QR Code (escanear com WhatsApp)
curl http://SEU_IP:8080/instance/qrcode/meu-bot?image=true \
  -H "apikey: SUA_CHAVE_AQUI" > qrcode.png
```

## 3. Configurar o Webhook na Evolution API

Após o OpenLegion estar rodando (porta 8420 por padrão), configure o webhook:

```bash
curl -X POST http://SEU_IP:8080/webhook/set/meu-bot \
  -H "apikey: SUA_CHAVE_AQUI" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "http://localhost:8420/channels/whatsapp_evolution/webhook",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": ["messages.upsert"]
  }'
```

> Se a Evolution API e o OpenLegion estiverem em containers diferentes,
> use o IP/hostname correto em vez de `localhost`.

## 4. Configurar o OpenLegion

### `.env`

```bash
# Adicione ao seu .env:
EVOLUTION_API_URL=http://localhost:8080
EVOLUTION_API_KEY=SUA_CHAVE_AQUI
EVOLUTION_INSTANCE_NAME=meu-bot
# EVOLUTION_WEBHOOK_SECRET=   # opcional, para validar assinatura HMAC
```

### `config/mesh.yaml`

Adicione a seção `channels` ao `mesh.yaml`:

```yaml
mesh:
  host: "0.0.0.0"
  port: 8420

llm:
  default_model: "openai/gpt-4o-mini"

collaboration: true

channels:
  whatsapp_evolution:
    api_url: ${EVOLUTION_API_URL}
    api_key: ${EVOLUTION_API_KEY}
    instance_name: ${EVOLUTION_INSTANCE_NAME}
    webhook_secret: ${EVOLUTION_WEBHOOK_SECRET}
    default_agent: assistant   # agente padrão para mensagens sem @mention
```

## 5. Registrar o canal no ChannelManager

Edite `src/cli/channels.py` e adicione o import e registro:

```python
# Após os outros imports de channels:
from src.channels.whatsapp_evolution import WhatsAppEvolutionChannel

# Na função que registra channels (procure por "whatsapp" no arquivo):
if "whatsapp_evolution" in channels_config:
    cfg = channels_config["whatsapp_evolution"]
    channel = WhatsAppEvolutionChannel(
        api_url=os.environ.get("EVOLUTION_API_URL", cfg.get("api_url", "")),
        api_key=os.environ.get("EVOLUTION_API_KEY", cfg.get("api_key", "")),
        instance_name=os.environ.get("EVOLUTION_INSTANCE_NAME", cfg.get("instance_name", "")),
        webhook_secret=os.environ.get("EVOLUTION_WEBHOOK_SECRET", cfg.get("webhook_secret", "")),
        default_agent=cfg.get("default_agent", ""),
        mesh=mesh,
    )
    manager.register(channel)
```

## 6. Iniciar e parear

```bash
openlegion start
```

Ao iniciar, um **pairing code** aparece no terminal. Envie para o número
pareado na Evolution API:

```
/start <pairing_code>
```

## Comandos disponíveis via WhatsApp

| Comando | Descrição |
|---------|-----------|
| `/start <code>` | Parear como dono do bot |
| `/agents` | Listar agentes disponíveis |
| `/use <agente>` | Trocar agente ativo |
| `@agente <msg>` | Enviar mensagem para agente específico |
| `/status` | Ver status dos agentes |
| `/costs` | Ver gastos de LLM do dia |
| `/broadcast <msg>` | Enviar para todos os agentes |
| `/reset` | Limpar conversa atual |
| `/help` | Ver todos os comandos |
| `/allow <número>` | Liberar outro usuário (só dono) |
| `/revoke <número>` | Revogar acesso (só dono) |
| `/paired` | Ver lista de usuários autorizados (só dono) |

## Arquivo adicionado neste fork

```
src/channels/
├── whatsapp.py              # original — Meta Cloud API (mantido)
└── whatsapp_evolution.py    # NOVO — Evolution API self-hosted
```

O canal original `whatsapp.py` **não foi modificado** — os dois coexistem.
Você pode ativar apenas `whatsapp_evolution` no `mesh.yaml` e ignorar o canal Meta.
