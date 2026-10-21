# Envelope Spec — Inter-Agent Event Format

**Status:** draft v1 · **Owner:** hermes-controller / agent-identity
**Date:** 2025-10-20

## Canonical identity

The canonical identity for inter-agent exchange is the **position_id** — a
stable, registry-assigned identifier (e.g. `architect-001`, `worker-rtx5090`).

Transport names (Matrix localpart, A2A agent name, MQ subject,
`origin_json` sender, etc.) are **bindings** in a registry, not identities.
They are resolved to the position_id by `hermes_cli.identity_resolver`.

An unregistered transport resolves to the literal `"unregistered"` —
**never** to a silent `"hermes"` fallback.

## Event line format

One event = one line, pipe-delimited, 7 fields:

```
ts | from_id -> to_id | about=<subject_id|объект реестра> | kind=<вид> | msg_id | transport | correlation_id
```

| Field | Type | Description |
|---|---|---|
| `ts` | ISO-8601 (UTC) | `YYYY-MM-DDTHH:MM:SS±HH:MM` |
| `from_id` | position_id | Sender's canonical position id |
| `to_id` | position_id | Recipient's canonical position id |
| `about` | string | Subject: a task/object id from the registry, or a free-form object ref |
| `kind` | enum | Interaction type (see below) |
| `msg_id` | UUID | Unique message id (auto-generated if omitted) |
| `transport` | string | Physical transport: `matrix`, `a2a`, `mq`, `direct`, … |
| `correlation_id` | string \| `-` | Correlates request/reply chains; `-` when absent |

### `kind` enum

| Value | Meaning |
|---|---|
| `информирование` | informational / status update |
| `консультация` | consultation / advisory |
| `просьба` | request / task delegation |
| `принятие` | acceptance / decision adopted |
| `согласование` | coordination / alignment |
| `проверка` | verification / review |
| `возражение` | objection / pushback |
| `передача` | hand-off / transfer |
| `эскалация` | escalation |
| `улучшение` | improvement / refinement |

## Registry format (JSON)

```json
{
  "bindings": [
    {
      "transport": "matrix:@vovak:mx.elk.mobi",
      "position_id": "architect-001",
      "host": "10.0.0.2",
      "path": "/opt/hermes",
      "notes": "primary architect"
    },
    {
      "transport": "a2a:hermes-rtx5090",
      "position_id": "worker-rtx5090",
      "host": "10.0.0.5",
      "path": "/home/rtx5090/.hermes-rtx5090"
    }
  ]
}
```

## Resolver API (`hermes_cli.identity_resolver`)

```python
from hermes_cli.identity_resolver import IdentityResolver, format_envelope, append_event

r = IdentityResolver.from_registry("registry.json")
pid = r.resolve("matrix:@vovak:mx.elk.mobi")  # → "architect-001"
pid = r.resolve("unknown")                    # → "unregistered"

line = format_envelope(
    from_id="architect-001",
    to_id="worker-rtx5090",
    about="task-9d0d38a24b",
    kind="просьба",
    transport="matrix",
    correlation_id="corr-task-9d0d38a24b",
)
append_event("logs/events.log", line)
```

## Log projection

Logs (`ctl_event`, journal, text files) are a **projection** of the envelope
fields. They do NOT guess identity from free text. The resolver maps
transport names → position_id at write time; the log stores the resolved
ids.

## Out of scope (separate task)

- Transport delivery (MQ/Matrix courier) — identity is embedded, delivery
  is the courier's job.
- Registry mutation / registration workflow.
- Multi-region / cross-host replication.
