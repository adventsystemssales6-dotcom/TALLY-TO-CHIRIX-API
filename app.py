#!/usr/bin/env python3
"""
Chirix ERP to TallyPrime Middleware Web Server
===============================================
Flask Web Application providing a UI for fetching Chirix ERP sales invoices via API,
validating accounting balances, previewing Tally JSON output, and pushing to TallyPrime.

Chirix API (definitive spec):
    URL     : GET https://www.chirixsolutions.com/api/data
    Headers : Chirix-Auth-Token: RIK - <token>  (or  FBT - <token>)
              Transaction-Type: SIV
              Content-Type: application/json
    Body    : { "dateFrom": "dd/mm/yyyy", "dateTo": "dd/mm/yyyy" }
    Response: { "success": true, "records": 3, "data": [...] }
"""

import os
import ssl
import sys
import json
import urllib.request
import urllib.error
import logging
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response

from config import get_config
from tally.connector import check_tally_status as connector_check_status, get_tally_mode

from chirix_to_tally import (
    transform_invoice_to_tally_json,
    validate_voucher_balance,
    push_vouchers_to_tally,
    ensure_tally_masters_exist,
    normalize_invoice_data,
    extract_vouchers_from_json,
    extract_all_items,
    is_already_pushed,
    mark_as_pushed,
    TALLY_HTTP_URL
)

# ── Configuration ────────────────────────────────────────────────────────────────
app_config = get_config()

# ── Base Directory Configuration (PyInstaller Support) ──────────────────────────
if getattr(sys, "frozen", False):
    base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TallyChirixMiddleware")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "tally_conversion.log")
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = "tally_conversion.log"

app = Flask(
    __name__,
    static_folder=os.path.join(base_dir, "static"),
    template_folder=os.path.join(base_dir, "templates")
)
app.config.from_object(app_config)

# ── Logging (cloud-safe: FileHandler only when writable) ────────────────────────
log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    log_handlers.append(logging.FileHandler(log_path, mode="a", encoding="utf-8"))
except (OSError, PermissionError):
    pass  # Read-only filesystem on cloud — skip file logging

logging.basicConfig(
    level=getattr(logging, app_config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers
)


# ── Helpers ──────────────────────────────────────────────────────────────────────

def parse_xml_metrics(xml_str: str) -> dict:
    """Parses CREATED, ALTERED, EXCEPTIONS, ERRORS from a Tally response XML string."""
    if not isinstance(xml_str, str):
        return {"created": 0, "altered": 0, "exceptions": 0, "errors": 0}

    def _extract(tag):
        open_tag  = f"<{tag}>"
        close_tag = f"</{tag}>"
        if open_tag in xml_str:
            val = xml_str.split(open_tag)[1].split(close_tag)[0].strip()
            return int(val) if val.isdigit() else 0
        return 0

    return {
        "created":    _extract("CREATED"),
        "altered":    _extract("ALTERED"),
        "exceptions": _extract("EXCEPTIONS"),
        "errors":     _extract("ERRORS"),
    }


# ── Middleware ───────────────────────────────────────────────────────────────────

@app.after_request
def add_cors_headers(response):
    allowed = app.config.get("CORS_ORIGINS", "*")
    response.headers["Access-Control-Allow-Origin"]  = allowed
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ── Health Check (required by Render) ────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health_check():
    """Unauthenticated health endpoint for Render / load-balancer probes."""
    return jsonify({
        "status": "ok",
        "service": "chirix-tally-middleware",
        "tally_mode": get_tally_mode(),
    })


# ── Production Error Handlers ────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_request(e):
    return jsonify({"success": False, "error": "Bad request", "detail": str(e)}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"success": False, "error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"success": False, "error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    app.logger.error(f"Internal server error: {e}")
    return jsonify({"success": False, "error": "Internal server error"}), 500


# ── Routes ───────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Render the main web UI."""
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def check_tally_status():
    """Check whether TallyPrime is reachable (via connector in cloud mode, direct in local mode)."""
    return jsonify(connector_check_status())


def _fetch_single_chirix_call(api_url, api_key, date_from, date_to, ssl_ctx, timeout=30):
    """Executes a single HTTP GET request to Chirix API for a given date range."""
    payload = json.dumps({
        "dateFrom": date_from,
        "dateTo":   date_to
    }).encode("utf-8")

    headers = {
        "Chirix-Auth-Token": api_key,
        "Transaction-Type":  "SIV",
        "Content-Type":      "application/json"
    }

    chirix_req = urllib.request.Request(
        url=api_url, data=payload, headers=headers, method="GET"
    )

    try:
        with urllib.request.urlopen(chirix_req, timeout=timeout, context=ssl_ctx) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as http_err:
        err_body = ""
        try:
            err_body = http_err.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("message") or err_json.get("error") or err_body
        except Exception:
            msg = err_body or http_err.reason
        return {"success": False, "error": f"HTTP {http_err.code} — {msg}", "http_code": http_err.code}
    except urllib.error.URLError as url_err:
        return {"success": False, "error": f"Cannot reach Chirix API — {url_err.reason}", "http_code": 502}

    try:
        response_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        response_text = raw_bytes.decode("utf-16", errors="ignore")

    try:
        return json.loads(response_text)
    except Exception as je:
        return {"success": False, "error": f"Invalid JSON response: {je}", "http_code": 502}


@app.route("/api/fetch", methods=["POST"])
def fetch_from_chirix():
    """
    Proxy to the Chirix ERP Invoice API. Automatically breaks wide date ranges into
    monthly chunks to overcome Chirix ERP backend limitations.
    """
    try:
        body      = request.get_json(force=True) or {}
        api_key   = str(body.get("api_key",   "")).strip()
        date_from = str(body.get("date_from", "")).strip()   # dd/mm/yyyy
        date_to   = str(body.get("date_to",   "")).strip()   # dd/mm/yyyy
        api_url   = str(body.get("api_url",
                        "https://www.chirixsolutions.com/api/data")).strip()

        # ── Validate ─────────────────────────────────────────────────────────────
        if not api_key:
            return jsonify({"success": False, "error": "Chirix-Auth-Token is required."}), 400
        if not date_from or not date_to:
            return jsonify({"success": False,
                            "error": "dateFrom and dateTo are required (dd/mm/yyyy)."}), 400

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        app.logger.info(
            f"Chirix API → GET {api_url} | "
            f"Token: {api_key[:15]}… | "
            f"dateFrom={date_from}  dateTo={date_to}"
        )

        try:
            d_from = datetime.datetime.strptime(date_from, "%d/%m/%Y")
            d_to   = datetime.datetime.strptime(date_to,   "%d/%m/%Y")
        except ValueError:
            # Fallback to single call if format is unexpected
            chirix_data = _fetch_single_chirix_call(api_url, api_key, date_from, date_to, ssl_ctx)
            if not chirix_data.get("success", False):
                return jsonify({"success": False, "error": chirix_data.get("error", "Chirix API returned success=false")}), chirix_data.get("http_code", 400)
            records  = chirix_data.get("records", 0)
            invoices = chirix_data.get("data",    [])
            return jsonify({"success": True, "records": records, "invoices": invoices})

        # ── Build month-by-month chunk list ──────────────────────────────────────
        chunks = []
        curr = d_from
        while curr <= d_to:
            if curr.month == 12:
                next_m = datetime.datetime(curr.year + 1, 1, 1)
            else:
                next_m = datetime.datetime(curr.year, curr.month + 1, 1)
            last_day_m = next_m - datetime.timedelta(days=1)
            chunk_to   = min(last_day_m, d_to)

            chunks.append((curr.strftime("%d/%m/%Y"), chunk_to.strftime("%d/%m/%Y")))
            curr = chunk_to + datetime.timedelta(days=1)

        # ── Concurrent fetch for maximum performance ──────────────────────────────
        results = {}
        workers = min(10, max(1, len(chunks)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(_fetch_single_chirix_call, api_url, api_key, df_str, dt_str, ssl_ctx): idx
                for idx, (df_str, dt_str) in enumerate(chunks)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    results[idx] = future.result()
                except Exception as ex:
                    results[idx] = {"success": False, "error": str(ex)}

        # Check if first chunk failed due to invalid token / auth error
        if 0 in results and not results[0].get("success", False):
            err_res = results[0]
            if "HTTP 400" in err_res.get("error", "") or "HTTP 401" in err_res.get("error", "") or "HTTP 403" in err_res.get("error", ""):
                return jsonify({"success": False, "error": err_res.get("error")}), err_res.get("http_code", 400)

        # ── Aggregate and deduplicate invoices chronologically ───────────────────
        all_invoices = []
        seen_inv_keys = set()  # key = "InvNo|InvDt" — same format as _dedup_key()

        for idx in range(len(chunks)):
            chunk_from, chunk_to = chunks[idx]
            res_data = results.get(idx, {})
            if not res_data.get("success", False):
                app.logger.warning(
                    f"Chunk [{idx}] {chunk_from}→{chunk_to} FAILED: {res_data.get('error', 'unknown error')}"
                )
                continue

            invs = res_data.get("data", [])
            added = 0
            for inv in invs:
                inv_no = str(inv.get("InvNo") or inv.get("invoice_no") or "").strip()
                inv_dt = str(inv.get("InvDt") or inv.get("date") or "").strip()
                dedup_key = f"{inv_no}|{inv_dt}" if inv_no else str(inv)
                if dedup_key not in seen_inv_keys:
                    seen_inv_keys.add(dedup_key)
                    all_invoices.append(inv)
                    added += 1

            app.logger.info(
                f"Chunk [{idx}] {chunk_from}→{chunk_to}: API returned {len(invs)}, added {added} unique."
            )

        app.logger.info(f"Chirix aggregated {len(all_invoices)} unique record(s) across {len(chunks)} chunk(s).")
        return jsonify({"success": True, "records": len(all_invoices), "invoices": all_invoices})

    except json.JSONDecodeError as je:
        app.logger.error(f"Chirix response JSON parse error: {je}")
        return jsonify({"success": False, "error": f"Response is not valid JSON: {je}"}), 502
    except Exception as e:
        app.logger.error(f"Chirix Fetch Error: {e}")
        return jsonify({"success": False, "error": f"Fetch Error: {e}"}), 500


@app.route("/api/convert", methods=["POST"])
def convert_json():
    """
    STEP 1 — Parse input, validate accounting balance (Debit = Credit), preview output.
    Accepts: raw Chirix invoice array OR a single invoice dict.
    """
    try:
        entry_mode = request.args.get("entry_mode", "Accounting Invoice")

        if "file" in request.files:
            file      = request.files["file"]
            raw_bytes = file.read()
            if raw_bytes.startswith(b'\xff\xfe') or raw_bytes.startswith(b'\xfe\xff'):
                raw_content = raw_bytes.decode("utf-16", errors="ignore")
            else:
                try:
                    raw_content = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raw_content = raw_bytes.decode("utf-16", errors="ignore")
            data = json.loads(raw_content)
        else:
            data = request.get_json(force=True)
            if isinstance(data, dict) and "entry_mode" in data:
                entry_mode = data.get("entry_mode") or entry_mode

        invoices = extract_vouchers_from_json(data)

        tally_messages = []
        invoices_meta  = []

        for idx, inv in enumerate(invoices, 1):
            normalized = normalize_invoice_data(inv)
            inv_no     = normalized.get("invoice_no", f"INV-{idx}")
            is_bal, debit, credit, bal_msg = validate_voucher_balance(inv)

            try:
                tally_rec = transform_invoice_to_tally_json(inv, entry_mode=entry_mode)
                tally_messages.append(tally_rec)
                invoices_meta.append({
                    "invoice_no":  inv_no,
                    "customer":    normalized.get("customer", {}).get("name", "N/A"),
                    "date":        normalized.get("date", ""),
                    "grand_total": debit,
                    "credit_total": credit,
                    "balanced":    is_bal,
                    "message":     bal_msg
                })
            except Exception as ex:
                invoices_meta.append({
                    "invoice_no":  inv_no,
                    "customer":    normalized.get("customer", {}).get("name", "N/A"),
                    "date":        normalized.get("date", ""),
                    "grand_total": debit,
                    "credit_total": credit,
                    "balanced":    False,
                    "message":     str(ex)
                })

        return jsonify({
            "success":         True,
            "total_count":     len(invoices),
            "converted_count": len(tally_messages),
            "invoices_meta":   invoices_meta,
            "tally_json":      {"tallymessage": tally_messages},
            "raw_input":       data
        })

    except Exception as e:
        app.logger.error(f"Conversion Error: {e}")
        return jsonify({"success": False, "error": f"Conversion Error: {e}"}), 400


@app.route("/api/push", methods=["POST"])
def push_to_tally():
    """
    STRICT 3-STEP PIPELINE:
    1. Parse input
    2. Create / sync Masters in Tally
    3. Push Vouchers to Tally
    """
    try:
        req_data   = request.get_json(force=True)
        entry_mode = "Accounting Invoice"
        if isinstance(req_data, dict):
            entry_mode = req_data.get("entry_mode", "Accounting Invoice")
            payload    = req_data.get("invoices", req_data)
        else:
            payload = req_data

        # ── Duplicate protection ──────────────────────────────────────────────
        invoices = extract_vouchers_from_json(payload)
        duplicates = []
        to_push    = []
        for inv in invoices:
            if is_already_pushed(inv):
                from chirix_to_tally import _dedup_key
                duplicates.append(_dedup_key(inv))
                app.logger.warning(f"Skipping duplicate invoice: {_dedup_key(inv)}")
            else:
                to_push.append(inv)

        if not to_push:
            return jsonify({
                "success": False,
                "error":   f"All {len(duplicates)} invoice(s) were already pushed this session. "
                           f"Duplicate keys: {', '.join(duplicates)}"
            }), 409

        push_results = push_vouchers_to_tally(to_push, TALLY_HTTP_URL, entry_mode=entry_mode)

        # Mark invoices as pushed (after successful Tally send)
        for inv in to_push:
            mark_as_pushed(inv)

        if isinstance(push_results, str):
            master_xml = voucher_xml = push_results
        elif isinstance(push_results, dict):
            master_xml  = push_results.get("master_xml_response",  "")
            voucher_xml = push_results.get("voucher_xml_response", "")
        else:
            master_xml = voucher_xml = ""

        return jsonify({
            "success":             True,
            "message":             "Data pushed to TallyPrime!",
            "master_xml_response":  master_xml,
            "voucher_xml_response": voucher_xml,
            "master_metrics":       parse_xml_metrics(master_xml),
            "voucher_metrics":      parse_xml_metrics(voucher_xml),
            "tally_response":       voucher_xml or master_xml
        })


    except Exception as e:
        app.logger.error(f"Push Error: {e}")
        return jsonify({"success": False, "error": f"Tally Push Error: {e}"}), 500


# ── Entry point (development only — production uses gunicorn) ────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1" or app.config.get("DEBUG", False)
    print(f"Starting Chirix to Tally Middleware Web UI on http://localhost:{port}")
    print(f"  Tally mode : {get_tally_mode()}")
    print(f"  Debug      : {debug_mode}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
