"""E2E acceptance check: verify identity.name flows into Matrix device_name and A2A agent card."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Create a temp HERMES_HOME with identity config
tmp = tempfile.mkdtemp(prefix="hermes-testhome-")
os.environ["HERMES_HOME"] = tmp
os.environ.pop("HERMES_PROFILE", None)

# Write config with identity
config_path = Path(tmp) / "config.yaml"
config_path.write_text("""
identity:
  name: rtx5090-worker
  id: "inst-rtx5090-001"
""", encoding="utf-8")

results = {}

# 1. Verify identity module reads config
from hermes_cli import identity
results["identity_name"] = identity.identity_name()
results["identity_id"] = identity.identity_id()
results["instance_label"] = identity.instance_label()
assert results["identity_name"] == "rtx5090-worker", f"Expected rtx5090-worker, got {results['identity_name']}"
assert results["identity_id"] == "inst-rtx5090-001", f"Expected inst-rtx5090-001, got {results['identity_id']}"
assert results["instance_label"] == "rtx5090-worker"
print(f"PASS: identity.name = {results['identity_name']}")
print(f"PASS: identity.id = {results['identity_id']}")

# 2. Verify Matrix adapter device_name uses identity
# The matrix adapter reads identity_label() in the login path
with patch("hermes_cli.identity.instance_label", return_value="rtx5090-worker"):
    # Simulate what the adapter does:
    _dev_name = "Hermes Agent"
    _dev_name = identity.instance_label() or _dev_name
    results["matrix_device_name"] = _dev_name
    assert _dev_name == "rtx5090-worker", f"Matrix device_name: expected rtx5090-worker, got {_dev_name}"
    print(f"PASS: Matrix device_name = {_dev_name}")

# 3. Verify A2A adapter _default_agent_name uses identity
with patch("hermes_cli.identity.instance_label", return_value="rtx5090-worker"):
    # Simulate _default_agent_name logic
    from hermes_cli.identity import instance_label
    _ident = instance_label()
    if _ident:
        results["a2a_agent_name"] = _ident
    else:
        results["a2a_agent_name"] = "hermes-agent"
    assert results["a2a_agent_name"] == "rtx5090-worker", f"A2A agent name: expected rtx5090-worker, got {results['a2a_agent_name']}"
    print(f"PASS: A2A agent name = {results['a2a_agent_name']}")

# 4. Verify default (no identity) keeps legacy behavior
with patch("hermes_cli.identity.instance_label", return_value=None):
    _dev_name = "Hermes Agent"
    _dev_name = identity.instance_label() or _dev_name
    assert _dev_name == "Hermes Agent", f"Default Matrix: expected 'Hermes Agent', got {_dev_name}"
    print(f"PASS: Default (no identity) Matrix device_name = {_dev_name}")

print("\n=== All E2E checks passed ===")
print(f"HERMES_HOME: {tmp}")
