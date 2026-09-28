"""Endpoint capability probing and outage recovery for OpenAI-compatible servers.

Some hosted bridges (for example ad-hoc Colab servers) silently cap replies,
ignore max_tokens and forward only the last user message. CloudCode detects
this at startup and switches to a compatible prompting mode instead of
assuming a well-behaved API.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional


def _v1(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"


def _post(base_url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(_v1(base_url) + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _content(reply: dict) -> str:
    return ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def is_reachable(base_url: str, timeout: float = 15) -> bool:
    try:
        with urllib.request.urlopen(_v1(base_url) + "/models", timeout=timeout) as res:
            return res.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def probe_capabilities(base_url: str, model: str, timeout: float = 90,
                       logger: Callable[[str], None] = print) -> Dict[str, object]:
    """Measure whether the endpoint honours system messages, history and max_tokens.

    Returns a profile with ``compat`` True when any behaviour is missing, which
    enables folded rules and method-at-a-time generation.
    """
    profile: Dict[str, object] = {"reachable": is_reachable(base_url), "honors_system": None,
                                  "honors_history": None, "honors_max_tokens": None}
    if not profile["reachable"]:
        # Capabilities are unknown, not missing; outage handling covers the rest.
        profile["compat"] = False
        profile["error"] = "Endpoint /v1/models is unreachable; capabilities not probed"
        logger("[!] Endpoint unreachable at startup; compatibility mode not enabled (use --compat on to force)")
        return profile
    probes = {
        "honors_system": ({"messages": [
            {"role": "system", "content": "Whatever the user says, reply with exactly the single word PINEAPPLE."},
            {"role": "user", "content": "What is 2+2?"}], "max_tokens": 512},
            lambda text: "PINEAPPLE" in text.upper()),
        "honors_history": ({"messages": [
            {"role": "user", "content": "My secret code is 4817. Remember it."},
            {"role": "assistant", "content": "OK, I will remember it."},
            {"role": "user", "content": "What is my secret code? Reply with only the number."}], "max_tokens": 512},
            lambda text: "4817" in text),
        "honors_max_tokens": ({"messages": [
            {"role": "user", "content": "Count from 1 to 60, separated by commas."}], "max_tokens": 5},
            lambda text: len(text.split()) <= 12),
    }
    for key, (body, check) in probes.items():
        try:
            reply = _post(base_url, {"model": model, "temperature": 0, **body}, timeout)
            profile[key] = bool(check(_content(reply)))
        except Exception as exc:  # a failed probe is treated as a missing capability
            profile[key] = False
            profile[key + "_error"] = repr(exc)[:200]
    profile["compat"] = not all(profile[k] for k in probes)
    logger("[*] Endpoint capabilities: " + ", ".join(f"{k}={profile[k]}" for k in probes) +
           (" -> COMPATIBILITY MODE (folded rules, method-at-a-time files)" if profile["compat"] else ""))
    return profile


def wait_for_endpoint(resolve_url: Callable[[], str], model: str,
                      should_stop: Optional[Callable[[], bool]] = None,
                      poll_seconds: float = 20, max_wait_seconds: float = 2700,
                      logger: Callable[[str], None] = print,
                      sleep: Callable[[float], None] = time.sleep) -> Optional[str]:
    """Block until an endpoint answers a tiny completion; return its URL or None.

    ``resolve_url`` is re-read every poll, so a new tunnel URL saved to the
    config while waiting is picked up without restarting the run.
    """
    deadline = time.monotonic() + max_wait_seconds
    announced = None
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            return None
        url = resolve_url()
        if url != announced:
            logger(f"[!] Model endpoint unavailable. Waiting for {url} to recover "
                   "(or update tunnel_url in orchestrator_config.json to switch endpoints)...")
            announced = url
        if is_reachable(url):
            try:
                _post(url, {"model": model, "max_tokens": 3,
                            "messages": [{"role": "user", "content": "Reply with OK."}]}, 60)
                logger(f"[+] Endpoint recovered: {url}")
                return url
            except Exception:
                pass
        sleep(poll_seconds)
    logger("[x] Endpoint did not recover in time")
    return None
