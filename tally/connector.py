"""
Tally Connector — Abstraction Layer for Cloud ↔ Local Tally Communication
==========================================================================
Provides a single interface for the Flask app to interact with TallyPrime,
regardless of whether Tally is on localhost (LOCAL mode) or behind a remote
connector agent (CLOUD mode).

CLOUD MODE:
    When TALLY_CONNECTOR_URL is set, all Tally HTTP calls are forwarded to
    the user's Windows PC via the lightweight local agent (local_agent.py).

LOCAL MODE (default):
    When TALLY_CONNECTOR_URL is empty, calls go directly to TallyPrime on
    localhost:9000 — identical to the original behaviour.
"""

import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("TallyConnector")

# ── Mode Detection ───────────────────────────────────────────────────────────

def get_tally_mode() -> str:
    """Returns 'cloud' if a remote connector URL is configured, else 'local'."""
    return "cloud" if os.environ.get("TALLY_CONNECTOR_URL", "").strip() else "local"


def get_tally_url() -> str:
    """Returns the effective Tally target URL based on current mode."""
    connector = os.environ.get("TALLY_CONNECTOR_URL", "").strip()
    if connector:
        return connector.rstrip("/")
    return os.environ.get("TALLY_HTTP_URL", "http://127.0.0.1:9000")


# ── Status Check ─────────────────────────────────────────────────────────────

def check_tally_status() -> dict:
    """
    Check whether TallyPrime is reachable.
    In CLOUD mode, pings the connector agent's /tally/status endpoint.
    In LOCAL mode, pings TallyPrime directly on port 9000.
    """
    mode = get_tally_mode()
    target = get_tally_url()

    try:
        if mode == "cloud":
            url = f"{target}/tally/status"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "connected": data.get("connected", False),
                    "url": target,
                    "mode": "cloud",
                    "status": resp.status,
                }
        else:
            req = urllib.request.Request(target, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                return {
                    "connected": True,
                    "url": target,
                    "mode": "local",
                    "status": resp.status,
                }
    except urllib.error.HTTPError as e:
        return {"connected": True, "url": target, "mode": mode, "status": e.code}
    except Exception as e:
        return {"connected": False, "url": target, "mode": mode, "error": str(e)}


# ── XML Relay ────────────────────────────────────────────────────────────────

def post_xml_to_tally(xml_payload: str, tally_url: str = None, timeout: int = 120) -> str:
    """
    Send an XML envelope to TallyPrime.
    In CLOUD mode, POSTs to the connector agent's /tally/xml endpoint which
    relays the XML to the local TallyPrime instance.
    In LOCAL mode, POSTs directly to TallyPrime HTTP port.

    Returns the raw XML response string from Tally.
    """
    mode = get_tally_mode()
    target = tally_url or get_tally_url()

    if mode == "cloud":
        # Wrap XML into a JSON payload for the connector agent
        relay_url = f"{target}/tally/xml"
        body = json.dumps({"xml": xml_payload}).encode("utf-8")
        req = urllib.request.Request(
            relay_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return data.get("response", "")
        except Exception as e:
            logger.error(f"Cloud connector relay failed: {e}")
            raise
    else:
        # Direct local call — original behaviour
        req = urllib.request.Request(
            target,
            data=xml_payload.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Direct Tally HTTP POST failed: {e}")
            raise
