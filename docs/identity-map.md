# Instance Identity Map

**Fork:** `VovaElkTver/hermes-autonomous-agent` · **Branch:** `main` @ 7ff5714
**Audit date:** 2026-10-20

## Decision (Architect, 20 Oct 2025)

- `identity.name` and `identity.id` are new top-level keys in `config.yaml`.
- `identity.name` is a **slug** (1-32 chars, `[a-z0-9._-]`, starts/ends with alnum).
  Default value `"hermes"` means *unset*; all consumers keep legacy behaviour
  when it is at default.
- `identity.id` is an optional stable instance identifier for the registry
  binding (`transport → position_id`).
- **Paths are NOT renamed.** `HERMES_HOME=~/.hermes-<label>` remains a
  *convention*, not an identity (per Architect decision).
- The installer prompts for the name (default `hermes`) and writes
  `identity.name` / `identity.id` into `config.yaml`.

## Places where instance identity is formed or printed

| # | Location (file : line) | What it reads today | What it should read (`identity.*`) | Change |
|---|---|---|---|---|
| 1 | `plugins/platforms/matrix/adapter.py:1176` | Hardcoded `"Hermes Agent"` as Matrix `device_name` on password login | `identity.name` (slug) or legacy `"Hermes Agent"` if unset | **Patch** |
| 2 | `plugins/platforms/matrix/adapter.py:1218` | `_acct_id = self._user_id or "hermes"` — Olm crypto-store key fallback | `identity.name` if explicitly set, else `"hermes"` (byte-compatible for existing installs) | **Patch** |
| 3 | `plugins/platforms/a2a/adapter.py:79` (`_default_agent_name`) | Env `A2A_AGENT_NAME` → `hermes-<hostname>` → `"hermes-agent"` | `identity.name` (explicit) → `A2A_AGENT_NAME` → `hermes-<hostname>` → `"hermes-agent"` | **Patch** |
| 4 | `plugins/platforms/a2a/adapter.py:420` | `f"Hermes {slug}"` per-profile A2A agent-card name | `f"{brand} {slug}"` where `brand = identity.name` or `"Hermes"` | **Patch** |
| 5 | `scripts/write_install_stamp.py` | Build-time provenance: commit, branch, version, `updateMechanism`. **No instance identity at all.** | No change. Stamp = code provenance, not instance identity. Instance identity lives in `config.yaml`. | **No change** (documented) |
| 6 | `hermes_cli/gateway.py:2197,2311` (`generate_systemd_unit`, `get_service_name`) | Unit name `hermes-gateway` / `hermes-gateway-<profile>`; `Description=Hermes Agent Gateway` (constant) | Unit name derives from **profile**, not instance label — correct. Description could use `identity.name` but is cosmetic. | **No change** (cosmetic only; profile-based naming is correct) |
| 7 | `hermes_cli/banner.py:221,234` | `"Hermes Agent v{version}"` | `identity.name` display form (default `"Hermes Agent"`) | **Patch** |
| 8 | `hermes_cli/_startup_fast.py:203` | `"Hermes Agent v{version} ({date})"` | Same as banner | **Patch** |
| 9 | `hermes_cli/setup.py:712` | `"Hermes Agent Setup Wizard"` | Product string, not instance ID. Leave as product branding. | **No change** (product banner) |
| 10 | `hermes_cli/commands.py:348` | `"Show Hermes Agent version"` (slash-registry description) | Product string in the command registry. Not per-instance. | **No change** (product string) |
| 11 | `agent/transports/codex_app_server_session.py:269` | `client_name="hermes"`, `client_title="Hermes Agent"` | Client id sent to the external Codex app-server; per-instance renaming risks the server-side client registry. | **No change** (external client registry, not instance identity) |
| 12 | `agent/copilot_acp_client.py:56` | `clientInfo: {name: "hermes-agent", title: "Hermes Agent"}` | Client id sent to the external Copilot ACP server; same registry risk as row 11. | **No change** (external client registry, not instance identity) |
| 13 | `hermes_cli/skin_engine.py:47` | `agent_name: f"{who} Agent"` (skin branding) | User-configurable skin. Not instance identity. | **No change** |
| 14 | `hermes_cli/observability/` (shared_metrics) | Telemetry payload carries `profile_count_bucket`, profile names. Opt-in (`telemetry.shared_metrics.enabled`). No instance name. | Could tag with `identity.id` for multi-instance attribution. Privacy-sensitive; defer. | **No change** (defer) |
| 15 | `agent/legacy_cli.py:33-34` | `prog="hermes-agent"`, `"Legacy single-query Hermes Agent runner"` | Product string. | **No change** |
| 16 | `hermes_cli/uninstall.py:789` | `"Hermes Agent Uninstaller"` | Product string. | **No change** |
| 17 | `hermes_cli/update_cmd.py:1826` | `"☤ Updating Hermes Agent..."` | Product string. | **No change** |
| 18 | `hermes_cli/web_server.py:366` | `FastAPI(title="Hermes Agent", ...)` | Product string. | **No change** |

## New config keys

Registered as a top-level `identity` block in `hermes_cli/config_defaults.py::DEFAULT_CONFIG`
(so `hermes config set identity.name <slug>` works and the key is known to the
config writers).

```yaml
# config.yaml
identity:
  name: hermes          # slug; default "hermes" = unset
  id: ""               # optional stable instance ID for registry binding
```

## New module

`hermes_cli/identity.py` — pure helpers:

| Function | Returns |
|---|---|
| `identity_name()` | `str` — `identity.name` or `"hermes"` |
| `instance_label()` | `str \| None` — `None` when default/unset (use for override-when-set pattern) |
| `identity_id()` | `str \| None` |
| `validate_instance_slug(s)` | `bool` |

## Installer changes

- `scripts/install.sh` — `stage_config()` prompts for instance name (interactive
  only), validates slug, appends `identity:` block to `config.yaml`.
- `scripts/install.ps1` — `Stage-Config` does the same via `Read-Host`.
- `scripts/install.cmd` — thin wrapper, no change needed.
- `hermes_cli/setup.py` — new wizard step `setup_instance_identity`.
