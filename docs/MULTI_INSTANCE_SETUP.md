# Multi-Instance WhatsApp Setup (MedicPro)

## Arquitetura

```
┌─────────────────────────────────────────────────────────────┐
│                     Evolution API :8080                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ clinica-alfa │  │ clinica-beta │  │  clinica-gamma   │  │
│  │ 📱 +5551...  │  │ 📱 +5551...  │  │  📱 +5551...     │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
└─────────┼────────────────┼───────────────────┼─────────────┘
          │ webhook         │ webhook            │ webhook
          ▼                 ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│              OpenLegion AI :8084                            │
│   /channels/whatsapp_evolution_multi/webhook/{instance}     │
│                                                             │
│   WhatsAppEvolutionMultiChannel                             │
│   ├── EvolutionInstance(clinica-alfa)  → clinic_id=alfa     │
│   ├── EvolutionInstance(clinica-beta)  → clinic_id=beta     │
│   └── EvolutionInstance(clinica-gamma) → clinic_id=gamma    │
│              ↓ injeta contexto da clínica                   │
│         Agent: recepcionista                                │
│              ↓                                              │
│         Skill: medicpro_api(clinic_id=...)                  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              MedicPro API :3000                             │
│  GET /v1/appointments/slots?clinicId={clinic_id}            │
│  POST /v1/appointments                                      │
└─────────────────────────────────────────────────────────────┘
```

## Setup passo a passo

### 1. Configurar instâncias no Evolution Manager

1. Abrir Evolution Manager: `http://localhost:8085`
2. Para cada clínica, criar uma instância:
   - Clicar em **New Instance**
   - Nome da instância: `clinica-alfa` (use kebab-case, sem espaços)
   - Clicar em **Connect** e escanear QR code com o WhatsApp da clínica

### 2. Configurar clinics.yaml

```bash
cp openlegion-ai/config/clinics.yaml.example openlegion-ai/config/clinics.yaml
# Editar com os dados reais de cada clínica
```

### 3. Configurar mesh.yaml para multi-instance

```yaml
channels:
  whatsapp_evolution_multi:
    api_url: ${EVOLUTION_API_URL}
    api_key: ${EVOLUTION_API_KEY}
    default_agent: recepcionista
    poll_interval: 60
    webhook_secret: ${EVOLUTION_WEBHOOK_SECRET}
```

### 4. Registrar webhooks nas instâncias

Após subir os containers, registrar o webhook de cada instância.
Substitua `SEU_DOMINIO` pelo IP/domínio do servidor:

```bash
# Para cada instância, registrar o webhook:
curl -X POST http://localhost:8083/webhook/set/clinica-alfa \
  -H 'apikey: medicpro-evolution-key-2024' \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "http://openlegion-ai:8420/channels/whatsapp_evolution_multi/webhook/clinica-alfa",
    "webhook_by_events": false,
    "webhook_base64": false,
    "events": ["MESSAGES_UPSERT"]
  }'

# Repetir para clinica-beta, clinica-gamma, etc.
```

### 5. Verificar instâncias registradas

```bash
curl http://localhost:8084/channels/whatsapp_evolution_multi/instances
```

Resposta esperada:
```json
{
  "count": 3,
  "instances": [
    {
      "instance_name": "clinica-alfa",
      "clinic_id": "alfa",
      "display_name": "Clínica Alfa",
      "active_users": 0,
      "webhook_url": "/channels/whatsapp_evolution_multi/webhook/clinica-alfa"
    }
  ]
}
```

## Adicionar nova clínica (sem downtime)

1. Criar instância no Evolution Manager e conectar WhatsApp
2. Adicionar bloco em `config/clinics.yaml`
3. Registrar webhook da nova instância (curl acima)
4. O OpenLegion detecta automaticamente em até 60 segundos

> Não precisa reiniciar o container!

## Mapa de portas

| Container | Porta | Serviço |
|---|---|---|
| agendadoutor-mongodb | 27017 | MongoDB |
| agendadoutor-api | 3000 | Backend API |
| agendadoutor-app | 8082 | App do médico |
| agendadoutor-landing | 8080 | Landing page |
| agendadoutor-crm | 8081 | Painel CRM |
| evolution-api | 8083 | WhatsApp server |
| evolution-manager | 8085 | UI de gerenciamento |
| openlegion-ai | 8084 | Agentes IA |
