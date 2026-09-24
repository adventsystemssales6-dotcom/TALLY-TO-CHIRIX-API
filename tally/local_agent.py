#!/usr/bin/env python3
"""
Tally Local Agent — Lightweight Bridge for Cloud ↔ TallyPrime
==============================================================
Run this script on the SAME Windows PC where TallyPrime is running.
It exposes a small Flask server that the cloud-hosted middleware can
call to relay XML commands to the local TallyPrime HTTP server (:9000).

Usage:
    pip install flask
    python -m tally.local_agent

    # Or with a public tunnel:
    pip install flask
    python -m tally.local_agent &
    npx localtunnel --port 6190

    Then set TALLY_CONNECTOR_URL=https://<your-tunnel>.loca.lt on Render.

Endpoints:
    GET  /tally/status  — Check if TallyPrime is responding
    POST /tally/xml     — Relay an XML payload to TallyPrime and return response
    GET  /health        — Agent health check
"""

import os
import sys
import json
import urllib.request
import urllib.error

try:
    from flask import Flask, request, jsonify
except ImportError:
    print("Flask is required. Install with: pip install flask")
    sys.exit(1)

TALLY_URL = os.environ.get("TALLY_HTTP_URL", "http://127.0.0.1:9000")
AGENT_PORT = int(os.environ.get("AGENT_PORT", 6190))
AGENT_SECRET = os.environ.get("AGENT_SECRET", "")

agent = Flask(__name__)


# ── Optional authentication ─────────────────────────────────────────────────

@agent.before_request
def check_agent_secret():
    """If AGENT_SECRET is set, require it in the Authorization header."""
    if not AGENT_SECRET:
        return  # No auth required
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {AGENT_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401


# ── CORS for cloud caller ───────────────────────────────────────────────────

@agent.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ── Endpoints ────────────────────────────────────────────────────────────────

@agent.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "tally-local-agent", "tally_url": TALLY_URL})


@agent.route("/tally/status", methods=["GET"])
def tally_status():
    """Ping TallyPrime on localhost and report connection status."""
    try:
        req = urllib.request.Request(TALLY_URL, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return jsonify({"connected": True, "url": TALLY_URL, "status": resp.status})
    except urllib.error.HTTPError as e:
        return jsonify({"connected": True, "url": TALLY_URL, "status": e.code})
    except Exception as e:
        return jsonify({"connected": False, "url": TALLY_URL, "error": str(e)})


@agent.route("/tally/xml", methods=["POST"])
def tally_xml_relay():
    """
    Receive an XML payload from the cloud Flask app and relay it to TallyPrime.
    Expects JSON body: {"xml": "<ENVELOPE>...</ENVELOPE>"}
    Returns JSON: {"response": "<TALLY_RESPONSE_XML>"}
    """
    try:
        body = request.get_json(force=True)
        xml_payload = body.get("xml", "")
        if not xml_payload:
            return jsonify({"error": "No XML payload provided"}), 400

        req = urllib.request.Request(
            TALLY_URL,
            data=xml_payload.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=120)
        tally_response = resp.read().decode("utf-8", errors="ignore")

        return jsonify({"response": tally_response})

    except urllib.error.URLError as e:
        return jsonify({"error": f"Cannot reach TallyPrime at {TALLY_URL}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"Relay error: {e}"}), 500


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"┌──────────────────────────────────────────────────┐")
    print(f"│  Tally Local Agent                               │")
    print(f"│  Listening on  :  http://0.0.0.0:{AGENT_PORT:<15}│")
    print(f"│  Tally target  :  {TALLY_URL:<31}│")
    print(f"│  Auth required :  {'Yes' if AGENT_SECRET else 'No':<31}│")
    print(f"└──────────────────────────────────────────────────┘")
    print()
    print("  To expose this agent to the internet, run in another terminal:")
    print(f"    npx localtunnel --port {AGENT_PORT}")
    print()
    print("  Then set TALLY_CONNECTOR_URL on Render to the generated URL.")
    print()
    agent.run(host="0.0.0.0", port=AGENT_PORT, debug=False)
