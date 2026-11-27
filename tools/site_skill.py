#!/usr/bin/env python3
"""Tier L9 (TZ-tinyhumansai): site-skill minter — mint once, serve без модели.

Ценность pomerado, реализованная нативно: LLM участвует ОДИН раз при mint
(составляет YAML-навык и хост проверяет каждый шаг живьём), дальше навык
проигрывается детерминированно без модели.

Skill YAML (v1):
    name: check-uptime
    start_url: https://example.com
    steps:
      - {action: wait_for_text, text: "Up"}      # проверка
      - {action: get_text, expect_contains: "OK"} # asserts
      - {action: click, selector: "button.refresh"}
      - {action: type, selector: "#q", text: "query"}
      - {action: select, selector: "#s", option: "all"}
      - {action: scroll, down: true}
      - {action: read}                            # результат -> stdout
    save: status                                  # поле из read-результата (опц.)

Mint: каждая строка steps исполняется против start_url; провалившийся шаг =
отказ mint (навык не публикуется). Replay: тот же цикл, без модели, fail-fast,
при падении — снапшот в $HERMES_HOME/site_skills/<name>/failure-<ts>.

Граница процесса: агент-браузерный CLI (pinned 0.39.1) — GPL-код не импортируется.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

AB = str(Path.home() / "zoo/tinyhumansai/agent-browser/cli/target/release/agent-browser")
AB_ENV = {
    "AGENT_BROWSER_EXECUTABLE_PATH":
        str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
    "AGENT_BROWSER_ARGS": "--no-sandbox",
}
SKILLS_DIR = Path(os.environ.get("HERMES_SITE_SKILLS_DIR",
                                 str(Path.home() / ".hermes/site_skills")))


def _ab(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, **AB_ENV}
    return subprocess.run([AB, *argv], capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


_STEP_ACTIONS = {"wait_for_text", "wait_for_selector", "get_text", "get_html",
                 "click", "type", "fill", "select", "scroll", "read", "press", "screenshot"}


def _wait_for_text(text: str, timeout_s: int = 10) -> tuple[bool, str]:
    """No native text-wait in the CLI: polling eval on innerText (pinned CLI surface)."""
    js = f'document.body.innerText.includes({json.dumps(text)})'
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = _ab("eval", js, timeout=20)
        if r.returncode == 0 and "true" in r.stdout.lower():
            return True, ""
        time.sleep(0.4)
    return False, f"wait_for_text: {text!r} not present within {timeout_s}s"


def _run_step(start_url: str, step: dict, *, fresh: bool) -> tuple[bool, str, dict]:
    """Execute one step against the live browser. Returns (ok, note, capture)."""
    action = str(step.get("action") or "")
    if action not in _STEP_ACTIONS:
        return False, f"unknown action {action!r}", {}
    if fresh:
        # Known pitfall (journal S1): a closed daemon can answer the next request
        # with EOF ("half-dead"). open is idempotent navigation — bounded retry.
        opened = False
        note = ""
        for attempt in range(3):
            _ab("close", "--all", timeout=30)
            if attempt:
                time.sleep(1.0 + attempt)
            r = _ab("open", start_url)
            if r.returncode == 0:
                opened = True
                break
            note = f"open failed: {r.stderr[:200]}"
        if not opened:
            return False, note, {}
    if action == "wait_for_text":
        ok, note = _wait_for_text(str(step.get("text", "")))
        return ok, note, {}
    args: list[str] = []
    if action == "wait_for_selector":
        args = ["wait", str(step.get("selector", ""))]
    elif action == "get_text":
        args = ["get", "text", str(step.get("selector", "body"))]
    elif action == "get_html":
        args = ["get", "html", str(step.get("selector", "body"))]
    elif action == "read":
        args = ["read"]
    elif action in ("click", "press"):
        args = [action, str(step.get("selector") or step.get("key", ""))]
    elif action in ("type", "fill"):
        args = [action, str(step.get("selector", "")), str(step.get("text", ""))]
    elif action == "select":
        args = ["select", str(step.get("selector", "")), str(step.get("option", ""))]
    elif action == "scroll":
        args = ["scroll", "down" if step.get("down", True) else "up"]
    elif action == "screenshot":
        args = ["screenshot", str(step.get("path", "/tmp/site-skill-shot.png"))]
    r = _ab(*args)
    if r.returncode != 0:
        return False, f"{action} rc={r.returncode}: {r.stderr[:200]}", {}
    out = r.stdout
    if "expect_contains" in step and str(step["expect_contains"]) not in out:
        return False, f"{action}: expected substring not found", {}
    cap = {}
    if action == "read":
        cap["read"] = out[:4000]
    return True, "", cap


def _validate(doc: dict) -> str:
    if not str(doc.get("name") or "").replace("-", "").isalnum():
        return "name: alnum with hyphens required"
    if not str(doc.get("start_url") or "").startswith(("http://", "https://", "file://")):
        return "start_url must be http(s):// or file://"
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        return "steps: non-empty list required"
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or "action" not in s:
            return f"step {i}: mapping with 'action' required"
    return ""


def _skill_dir(name: str) -> Path:
    return SKILLS_DIR / name


def mint(path: Path) -> int:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    err = _validate(doc)
    if err:
        print(json.dumps({"published": False, "error": err}))
        return 2
    captured = {}
    for i, step in enumerate(doc["steps"]):
        ok, note, cap = _run_step(doc["start_url"], step, fresh=(i == 0))
        captured.update(cap)
        if not ok:
            _ab("close", "--all", timeout=30)
            print(json.dumps({"published": False, "failed_step": i, "error": note}))
            return 2
    _ab("close", "--all", timeout=30)
    d = _skill_dir(doc["name"])
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy(path, d / "skill.yaml")
    (d / "mint.json").write_text(json.dumps(
        {"minted_at": int(time.time()), "steps_verified": len(doc["steps"])}, indent=2))
    print(json.dumps({"published": True, "skill": doc["name"], "path": str(d),
                      "captured_keys": sorted(captured)}))
    return 0


def replay(name: str, *, screenshot_on_fail: bool = True) -> int:
    d = _skill_dir(name)
    yaml_path = d / "skill.yaml"
    if not yaml_path.exists():
        print(json.dumps({"ok": False, "error": f"skill {name!r} not minted"}))
        return 3
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    captured = {}
    for i, step in enumerate(doc["steps"]):
        ok, note, cap = _run_step(doc["start_url"], step, fresh=(i == 0))
        captured.update(cap)
        if not ok:
            fail = d / f"failure-{int(time.time())}"
            fail.mkdir(parents=True, exist_ok=True)
            if screenshot_on_fail:
                _ab("screenshot", str(fail / "last.png"), timeout=30)
                _ab("snapshot", "-i", timeout=60)
                (fail / "snapshot.txt").write_text(
                    _ab("snapshot", timeout=60).stdout[:20000], encoding="utf-8")
            _ab("close", "--all", timeout=30)
            print(json.dumps({"ok": False, "failed_step": i, "error": note,
                              "evidence": str(fail)}))
            return 1
    _ab("close", "--all", timeout=30)
    if doc.get("save"):
        key = str(doc["save"])
        (d / "last_result.json").write_text(json.dumps(
            {"saved_at": int(time.time()), key: captured.get("read", "")[:4000]},
            indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "skill": name, "steps": len(doc["steps"])}))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "mint" and len(sys.argv) == 3:
        sys.exit(mint(Path(sys.argv[2])))
    elif cmd == "replay" and len(sys.argv) == 3:
        sys.exit(replay(sys.argv[2]))
    else:
        print("usage: site_skill.py mint <skill.yaml> | replay <name>")
        sys.exit(3)
