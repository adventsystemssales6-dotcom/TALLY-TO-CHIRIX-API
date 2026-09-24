#!/usr/bin/env python3
"""
Chirix ERP to TallyPrime JSON Converter & Direct Push Utility
============================================================
Converts sales invoice JSON data (or native Tally JSON exports containing Masters & Vouchers) into TallyPrime format,
validates accounting balance (Debit = Credit), auto-creates missing masters in Tally,
and pushes masters and vouchers directly to TallyPrime over HTTP (Port 9000).

FIXED (see inline comments marked "# FIX:"):
  1. Order number/date now read from `invoiceorderlist[0]` (native Tally export nests it there,
     not at voucher root as `basicpurchaseorderno`/`basicorderdate`).
  2. HSN code now read from `gsthsnname` on each inventory allocation (native Tally export uses
     this field, not `hsncode`/`hsnsac`/`hsn`).
  3. New `extract_stock_item_hsn_map()` builds an HSN lookup directly from the Stock Item master
     records bundled in the same payload -- no HTTP round-trip needed for the common case.
     The HTTP fetch is kept as a fallback for items not present in the local payload.
"""

import os
import json
import re
import uuid
import sys
import logging
import urllib.request
import urllib.error
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional
from xml.sax.saxutils import escape as xml_escape

# ── Duplicate Push Protection ────────────────────────────────────────────────
# Session-level set — tracks invoices already pushed this server session.
# Key format: "InvNo|InvDt"  e.g. "RIKTN/26-27/138|06/08/2026"
PUSHED_INVOICE_KEYS: set = set()

def _dedup_key(invoice: dict) -> str:
    """Build a logical identity key from InvNo + InvDt."""
    inv_no = str(invoice.get("InvNo") or invoice.get("invoice_no") or "")
    inv_dt = str(invoice.get("InvDt") or invoice.get("date")        or "")
    return f"{inv_no}|{inv_dt}"

def is_already_pushed(invoice: dict) -> bool:
    """Return True if this invoice has already been pushed this session."""
    return _dedup_key(invoice) in PUSHED_INVOICE_KEYS

def mark_as_pushed(invoice: dict) -> None:
    """Record this invoice as pushed."""
    PUSHED_INVOICE_KEYS.add(_dedup_key(invoice))

def reset_pushed_set() -> None:
    """Clear the dedup set (call from /api/push if you want to allow re-push)."""
    PUSHED_INVOICE_KEYS.clear()


# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("tally_conversion.log", mode='a', encoding='utf-8')
    ]
)
logger = logging.getLogger("ChirixToTally")

TALLY_HTTP_URL = os.environ.get("TALLY_HTTP_URL", "http://127.0.0.1:9000")
# Optional: set this to target a specific company when more than one is loaded in TallyPrime.
# Leave blank ("") to import into whichever company is currently active in Tally.
TALLY_COMPANY_NAME = os.environ.get("TALLY_COMPANY_NAME", "")

# GST UQC (Unit Quantity Code) mapping — Tally requires UQC for GST compliance
UQC_MAP = {
    "Nos": "NOS", "Pcs": "PCS", "Pkt": "PKT", "Bags": "BAG",
    "Box": "BOX", "Btl": "BTL", "Bndl": "BDL", "Ctn": "CTN",
    "Dzn": "DOZ", "Gms": "GMS", "Kgs": "KGS", "Ltr": "LTR",
    "Mtr": "MTR", "Mts": "MTS", "Qntl": "QTL", "Rol": "ROL",
    "Set": "SET", "Sht": "SHT", "Sqf": "SQF", "Sqm": "SQM",
    "Tbs": "TBS", "Thd": "TGM", "Tns": "TON", "Tub": "TUB",
    "Unt": "UNT", "Yds": "YDS",
    "nos": "NOS", "pcs": "PCS", "kgs": "KGS", "kg": "KGS",
    "ltr": "LTR", "lt": "LTR", "gms": "GMS", "gm": "GMS",
    "mtr": "MTR", "mt": "MTR", "set": "SET", "box": "BOX",
}

STATE_CODE_MAP = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan",
    "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh",
    "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand",
    "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman & Diu", "26": "Dadra & Nagar Haveli", "27": "Maharashtra", "28": "Andhra Pradesh",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh"
}


def parse_and_format_date(raw_date: Any) -> str:
    """Converts raw date string (e.g. YYYY-MM-DD, ISO, YYYYMMDD) into Tally's YYYYMMDD format."""
    if not raw_date:
        return datetime.now().strftime("%Y%m%d")

    date_str = str(raw_date).strip().split("T")[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y%m%d")
        except ValueError:
            continue

    return datetime.now().strftime("%Y%m%d")


def generate_tally_guids() -> Tuple[str, str, str]:
    """Generates unique remoteid, vchkey, and guid strings for Tally metadata."""
    base_uuid = str(uuid.uuid4())
    remoteid = f"{base_uuid}-000000c4"
    vchkey = f"{base_uuid}-0000b420:00000010"
    guid = remoteid
    return remoteid, vchkey, guid


def get_item_sales_ledger(item: Dict[str, Any]) -> str:
    """
    Determines the Ledger Name for an accounting invoice line item.
    If item['sales_ledger'] is explicitly set to a custom non-generic ledger (not 'Sales' or 'Sales Account'),
    that ledger name is returned.
    Otherwise, the item's name/description (item['item_name']) is used as the ledger name so each product/service
    appears as a distinct Ledger entry under Particulars in Tally.
    """
    s_ledger = str(item.get("sales_ledger") or "").strip()
    item_name = str(item.get("item_name") or "").strip()

    if s_ledger and s_ledger.lower() not in ("sales", "sales account", "sales accounts"):
        return s_ledger
    if item_name:
        return item_name
    if s_ledger:
        return s_ledger
    return "Sales"


def is_voucher_object(obj: Dict[str, Any]) -> bool:
    """Determines whether a JSON object represents a Voucher record (vs a Master definition)."""
    if not isinstance(obj, dict):
        return False

    meta_type = str(obj.get("metadata", {}).get("type", "")).lower()
    if meta_type == "voucher":
        return True
    if meta_type in ("group", "ledger", "unit", "godown", "stock item", "stock group", "currency",
                      "taxunit", "gstin", "voucher type"):
        return False

    if any(k in obj for k in ("invoice_no", "vouchernumber", "allinventoryentries", "allledgerentries",
                               "partyledgername", "customer", "grand_total", "InvNo", "InvDt", "BuyerLglNm", "ItemList", "TotInvVal")):
        return True

    return False


def unwrap_val(val: Any) -> str:
    """Helper to unwrap nested dictionary values like {'type': 'String', 'value': 'Sundry Debtors'} into clean strings."""
    if isinstance(val, dict):
        if "value" in val:
            return unwrap_val(val.get("value"))
        if "name" in val:
            return unwrap_val(val.get("name"))
    if isinstance(val, list):
        for sub in val:
            res = unwrap_val(sub)
            if res and res != "True" and not res.startswith("{"):
                return res
        return ""
    if isinstance(val, (str, int, float)):
        return str(val).strip()
    return ""


def is_stock_item_master(obj: Dict[str, Any]) -> bool:
    """Determines whether a JSON object represents a Stock Item master record."""
    if not isinstance(obj, dict):
        return False
    return str(obj.get("metadata", {}).get("type", "")).lower() == "stock item"


def is_ledger_master(obj: Dict[str, Any]) -> bool:
    """Determines whether a JSON object represents a Ledger master record."""
    if not isinstance(obj, dict):
        return False
    meta_type = str(obj.get("metadata", {}).get("type", "")).lower()
    if meta_type == "ledger":
        return True
    if any(k in obj for k in ("ledgername", "parent", "parentgroup", "ledopeningbalance", "closingbalance")) and not is_voucher_object(obj):
        return True
    return False


def extract_all_items(data: Any) -> List[Dict[str, Any]]:
    """
    Extracts raw list of objects from JSON structure.
    Scans container keys (tallymessage, collection, invoices, vouchers, data, masters, stock_items, etc.)
    so that explicit master records and vouchers are preserved without incorrectly unpacking line-item arrays.
    """
    if not data:
        return []

    extracted: List[Dict[str, Any]] = []

    if isinstance(data, dict):
        if is_voucher_object(data) or is_stock_item_master(data) or is_ledger_master(data):
            return [data]

        found_container = False
        container_keys = ("tallymessage", "collection", "invoices", "vouchers", "data", "masters", "stock_items", "customers", "parties", "ledgers")
        for key in container_keys:
            val = data.get(key)
            if isinstance(val, list):
                found_container = True
                for item in val:
                    if isinstance(item, dict):
                        extracted.extend(extract_all_items(item))
                    elif isinstance(item, list):
                        for sub in item:
                            if isinstance(sub, dict):
                                extracted.extend(extract_all_items(sub))
            elif isinstance(val, dict):
                sub_items = extract_all_items(val)
                if sub_items:
                    found_container = True
                    extracted.extend(sub_items)

        if not found_container:
            for key in ("items",):
                val = data.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    if is_voucher_object(val[0]) or is_stock_item_master(val[0]) or is_ledger_master(val[0]):
                        found_container = True
                        for item in val:
                            extracted.extend(extract_all_items(item))
                        break

        if not found_container:
            extracted.append(data)

    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                extracted.extend(extract_all_items(item))

    return extracted


def extract_vouchers_from_json(data: Any) -> List[Dict[str, Any]]:
    """Extracts only Voucher objects from raw JSON. Returns empty list if no vouchers recognized."""
    items = extract_all_items(data)
    vouchers = [it for it in items if is_voucher_object(it)]
    if not vouchers and items:
        sample_keys = [list(it.keys())[:15] for it in items[:3]]
        logger.warning(f"No voucher objects recognized in input payload of {len(items)} items. Sample item keys: {sample_keys}")
        return []
    return vouchers


def extract_stock_item_hsn_map(data: Any) -> Dict[str, str]:
    """
    Builds {stock_item_name: hsn_code} by scanning all payload reference sites:
    1. Stock Item master records (hsndetails array or top-level HSN attributes).
    2. Voucher / Invoice inventory entries (items, allinventoryentries, inventoryallocations).
    3. Any generic item dictionary referencing stock item name and HSN code.
    This ensures complete HSN coverage across all sites whether payload contains masters, vouchers, or both.
    """
    hsn_map: Dict[str, str] = {}
    items = extract_all_items(data)

    # Site 1: Stock Item masters
    for it in items:
        if not isinstance(it, dict):
            continue
        if is_stock_item_master(it):
            name = str(it.get("metadata", {}).get("name") or it.get("name") or "").strip()
            if not name:
                continue

            # Check hsndetails list
            hsn_details = it.get("hsndetails", [])
            if isinstance(hsn_details, list) and hsn_details:
                try:
                    best = max(
                        (h for h in hsn_details if isinstance(h, dict) and (h.get("hsncode") or h.get("gsthsnname") or h.get("hsnsac") or h.get("hsn"))),
                        key=lambda h: str(h.get("applicablefrom", "")),
                        default=None
                    )
                except Exception:
                    best = None
                if best is None:
                    for h in reversed(hsn_details):
                        if isinstance(h, dict) and (h.get("hsncode") or h.get("gsthsnname") or h.get("hsnsac") or h.get("hsn")):
                            best = h
                            break
                if best:
                    code = str(best.get("hsncode") or best.get("gsthsnname") or best.get("hsnsac") or best.get("hsn") or "").strip()
                    if code:
                        hsn_map[name] = code

            # Check direct attributes on item master
            if name not in hsn_map:
                code = str(
                    it.get("gsthsnname")
                    or it.get("hsncode")
                    or it.get("hsnsac")
                    or it.get("hsn")
                    or it.get("hsn_code")
                    or ""
                ).strip()
                if code:
                    hsn_map[name] = code

    # Site 2: Voucher records (Invoices)
    vouchers = extract_vouchers_from_json(data)
    for raw_inv in vouchers:
        try:
            inv = normalize_invoice_data(raw_inv)
            for item in inv.get("items", []):
                item_name = str(item.get("item_name") or "").strip()
                item_hsn = str(item.get("hsn_code") or "").strip()
                if item_name and item_hsn and item_name not in hsn_map:
                    hsn_map[item_name] = item_hsn
        except Exception:
            pass

    # Site 3: Generic item dictionary in payload
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("stockitemname") or it.get("item_name") or it.get("name") or "").strip()
        code = str(
            it.get("gsthsnname")
            or it.get("hsncode")
            or it.get("hsnsac")
            or it.get("hsn")
            or it.get("hsn_code")
            or ""
        ).strip()
        if name and code and name not in hsn_map:
            hsn_map[name] = code

    if hsn_map:
        logger.info(f"Extracted HSN codes for {len(hsn_map)} stock item(s) from all payload reference sites.")
    return hsn_map


def normalize_invoice_data(raw_inv: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes input JSON object into standard internal invoice dictionary.
    Supports Chirix ERP JSON format, native Tally JSON exports, and e-Invoice/GST JSON formats (InvNo, BuyerLglNm, ItemList).
    Extracts Party Details, Order Details, Stock Items, Taxes, and Round Off.
    """
    if not isinstance(raw_inv, dict):
        raise ValueError(f"Expected dict for raw_inv, got {type(raw_inv)}")

    # Check if raw_inv is e-Invoice / Custom GST JSON format (InvNo, BuyerLglNm, ItemList, etc.)
    if "InvNo" in raw_inv or "BuyerLglNm" in raw_inv or "ItemList" in raw_inv or "TotInvVal" in raw_inv:
        inv_no = str(raw_inv.get("InvNo") or "INV-1001").strip()
        date_val = str(raw_inv.get("InvDt") or datetime.now().strftime("%Y-%m-%d")).strip()
        party_name = str(raw_inv.get("BuyerLglNm") or raw_inv.get("BuyerNm") or "Sundry Debtor").strip()
        party_gstin = str(raw_inv.get("BuyerGstin") or "").strip()
        stcd = str(raw_inv.get("BuyerStcd") or "").strip().zfill(2)
        state_name = STATE_CODE_MAP.get(stcd) or str(raw_inv.get("BuyerLoc") or "Tamil Nadu").strip()
        
        addr1 = str(raw_inv.get("BuyerAddr1") or "").strip()
        addr2 = str(raw_inv.get("BuyerAddr2") or "").strip()
        addr3 = str(raw_inv.get("BuyerAddr3") or "").strip()
        loc = str(raw_inv.get("BuyerLoc") or "").strip()
        
        address_list = [a for a in [addr1, addr2, addr3, loc] if a]
        
        pincode_val = ""
        pin_match = re.search(r'\b\d{3}\s?\d{3}\b', f"{addr3} {loc}")
        if pin_match:
            pincode_val = pin_match.group(0).replace(" ", "")

        item_list = raw_inv.get("ItemList", [])
        items = []
        tot_cgst = 0.0
        tot_sgst = 0.0
        tot_igst = 0.0
        tot_ass = 0.0

        for it in item_list:
            if not isinstance(it, dict):
                continue
            item_name = str(it.get("PrdDesc") or it.get("item_name") or "Sales Item").strip()
            unit = str(it.get("Unit") or "NOS").strip()
            qty = float(it.get("Qty") or 1)
            rate = float(it.get("UnitPrice") or 0)
            amount = float(it.get("AssAmt") if ("AssAmt" in it and float(it.get("AssAmt", 0)) > 0) else it.get("TotAmt", 0))
            tot_ass += amount

            hsn = str(it.get("HsnCd") or "").strip()
            gst_rt = float(it.get("GstRt") or 18.0)

            c_amt = float(it.get("CgstAmt") or 0.0)
            s_amt = float(it.get("SgstAmt") or 0.0)
            i_amt = float(it.get("IgstAmt") or 0.0)

            tot_cgst += c_amt
            tot_sgst += s_amt
            tot_igst += i_amt

            items.append({
                "item_name": item_name,
                "unit": unit,
                "quantity": qty,
                "rate": rate,
                "amount": amount,
                "tax_rate": gst_rt,
                "sales_ledger": "Sales",
                "hsn_code": hsn,
                "cgst_amount": c_amt,
                "sgst_amount": s_amt,
                "igst_amount": i_amt
            })

        subtotal = float(raw_inv.get("TotAssVal") if ("TotAssVal" in raw_inv and float(raw_inv.get("TotAssVal", 0)) > 0) else tot_ass)
        cgst_amt = float(raw_inv.get("TotCgstVal") if ("TotCgstVal" in raw_inv and float(raw_inv.get("TotCgstVal", 0)) > 0) else tot_cgst)
        sgst_amt = float(raw_inv.get("TotSgstVal") if ("TotSgstVal" in raw_inv and float(raw_inv.get("TotSgstVal", 0)) > 0) else tot_sgst)
        igst_amt = float(raw_inv.get("TotIgstVal") if ("TotIgstVal" in raw_inv and float(raw_inv.get("TotIgstVal", 0)) > 0) else tot_igst)
        round_off = float(raw_inv.get("RndOffAmt") or 0.0)

        calc_grand = subtotal + cgst_amt + sgst_amt + igst_amt + round_off
        hdr_inv_val = float(raw_inv.get("TotInvVal", 0) or 0)
        
        if hdr_inv_val > 0 and abs(hdr_inv_val - calc_grand) > 0.05:
            # Chirix API header TotInvVal often holds net assessable value instead of gross value
            grand_total = calc_grand
        else:
            grand_total = hdr_inv_val if hdr_inv_val > 0 else calc_grand

        if round_off == 0.0:
            calc_diff = grand_total - (subtotal + cgst_amt + sgst_amt + igst_amt)
            if abs(calc_diff) > 0.001 and abs(calc_diff) <= 10.0:
                round_off = round(calc_diff, 2)

        order_no = str(
            raw_inv.get("OrderNo")
            or raw_inv.get("OrderNumber")
            or raw_inv.get("PoNo")
            or raw_inv.get("PONo")
            or raw_inv.get("po_number")
            or raw_inv.get("order_no")
            or raw_inv.get("orderno")
            or raw_inv.get("basicpurchaseorderno")
            or raw_inv.get("order_ref")
            or ""
        ).strip()
        order_date = (
            raw_inv.get("OrderDt")
            or raw_inv.get("OrderDate")
            or raw_inv.get("PoDt")
            or raw_inv.get("PODate")
            or raw_inv.get("po_date")
            or raw_inv.get("order_date")
            or raw_inv.get("basicorderdate")
            or date_val
        )

        return {
            "invoice_no": inv_no,
            "date": date_val,
            "order_no": order_no,
            "order_date": order_date,
            "customer": {
                "name": party_name,
                "gstin": party_gstin,
                "state": state_name,
                "country": "India",
                "pincode": pincode_val,
                "address": address_list,
                "gst_registration_type": "Regular" if party_gstin else "Unregistered"
            },
            "dispatch_from": {},
            "irn": "",
            "items": items,
            "subtotal": subtotal,
            "taxes": {
                "cgst_rate": 9.0, "cgst_amount": cgst_amt,
                "sgst_rate": 9.0, "sgst_amount": sgst_amt,
                "igst_rate": 18.0 if igst_amt > 0 else 0.0, "igst_amount": igst_amt
            },
            "round_off": round_off,
            "grand_total": grand_total
        }

    # Check if raw_inv is a Tally TALLYMESSAGE JSON structure or native Tally export
    if "vouchernumber" in raw_inv or "partyledgername" in raw_inv or "allinventoryentries" in raw_inv or "allledgerentries" in raw_inv or raw_inv.get("metadata", {}).get("type") == "Voucher":
        inv_no = str(raw_inv.get("vouchernumber") or raw_inv.get("invoice_no") or "INV-1001").strip()
        date_val = raw_inv.get("date") or raw_inv.get("vchstatusdate") or datetime.now().strftime("%Y%m%d")
        party_name = raw_inv.get("partyledgername") or raw_inv.get("partyname") or raw_inv.get("basicbuyername") or "Sundry Debtor"
        party_gstin = raw_inv.get("partygstin") or raw_inv.get("consigneegstin") or raw_inv.get("basicbuyerssalestaxno") or ""
        state_name = raw_inv.get("statename") or raw_inv.get("placeofsupply") or "Tamil Nadu"
        country_name = raw_inv.get("countryofresidence") or "India"
        pincode_val = raw_inv.get("partypincode") or raw_inv.get("pincode") or ""

        # FIX: order number/date live inside invoiceorderlist[0] in native Tally exports,
        # not as flat basicpurchaseorderno/basicorderdate keys at voucher root.
        invoice_order_list = raw_inv.get("invoiceorderlist", [])
        first_order = {}
        if isinstance(invoice_order_list, list) and invoice_order_list and isinstance(invoice_order_list[0], dict):
            first_order = invoice_order_list[0]

        order_no = str(
            first_order.get("basicpurchaseorderno")
            or raw_inv.get("basicpurchaseorderno")
            or raw_inv.get("order_no")
            or raw_inv.get("orderno")
            or raw_inv.get("po_number")
            or raw_inv.get("order_ref")
            or ""
        ).strip()
        order_date = (
            first_order.get("basicorderdate")
            or raw_inv.get("basicorderdate")
            or raw_inv.get("order_date")
            or date_val
        )

        address_raw = raw_inv.get("address") or raw_inv.get("basicbuyeraddress") or []
        if isinstance(address_raw, str):
            address_list = [a.strip() for a in address_raw.split("\n") if a.strip()]
        elif isinstance(address_raw, list):
            address_list = [str(a).strip() for a in address_raw if str(a).strip() and str(a) != "True" and not str(a).startswith("{")]
        else:
            address_list = []

        # Extract ledger entries from ledgerentries or allledgerentries
        l_entries = raw_inv.get("ledgerentries", []) or raw_inv.get("allledgerentries", [])

        # Extract stock items from allinventoryentries OR inventoryallocations inside ledger entries
        items = []
        raw_items = raw_inv.get("allinventoryentries", []) or raw_inv.get("inventoryentries", [])
        if not raw_items and l_entries:
            for l_entry in l_entries:
                if isinstance(l_entry, dict):
                    if "inventoryallocations" in l_entry:
                        raw_items.extend(l_entry["inventoryallocations"])
                    elif "inventoryentries" in l_entry:
                        raw_items.extend(l_entry["inventoryentries"])

        # If no inventory entries exist, extract line items directly from non-party, non-duty ledger entries (Accounting Invoice Mode)
        subtotal = 0.0
        if not raw_items and l_entries:
            for l_entry in l_entries:
                if not isinstance(l_entry, dict):
                    continue
                lname = str(l_entry.get("ledgername", "")).strip()
                lname_lower = lname.lower()
                is_party = bool(l_entry.get("ispartyledger") or lname_lower == party_name.lower())
                is_duty = any(k in lname_lower for k in ("cgst", "sgst", "igst", "round off", "roundoff", "tax", "vat"))
                if not is_party and not is_duty:
                    amt = abs(float(l_entry.get("amount", 0)))
                    hsn = str(l_entry.get("gsthsnname") or l_entry.get("hsncode") or l_entry.get("hsnsac") or "").strip()
                    desc = str(l_entry.get("gsthsndescription") or lname).strip()
                    item_tax_rate = 18.0
                    for rd in l_entry.get("ratedetails", []):
                        if isinstance(rd, dict) and str(rd.get("gstratedutyhead", "")).lower() in ("integrated tax", "igst"):
                            try:
                                item_tax_rate = float(str(rd.get("gstrate", "18")).strip())
                            except (ValueError, TypeError):
                                pass
                            break
                    if amt > 0:
                        items.append({
                            "item_name": desc,
                            "unit": "Nos",
                            "quantity": 1.0,
                            "rate": amt,
                            "amount": amt,
                            "tax_rate": item_tax_rate,
                            "sales_ledger": lname,
                            "hsn_code": hsn,
                            "batches": []
                        })
                        subtotal += amt

        for it in raw_items:
            if not isinstance(it, dict):
                continue
            item_name = it.get("stockitemname") or "Stock Item"
            rate_str = str(it.get("rate", "0/Nos")).strip()
            rate_val = float(rate_str.split("/")[0]) if "/" in rate_str else float(it.get("rate", 0))
            amount_val = abs(float(it.get("amount", 0)))
            subtotal += amount_val

            qty_str = str(it.get("actualqty", "1 Nos")).strip()
            qty_val = float(qty_str.split()[0]) if qty_str and qty_str.split()[0].replace('.', '', 1).isdigit() else 1.0
            unit = qty_str.split()[-1] if len(qty_str.split()) > 1 else "Nos"

            # Read batches from batchallocations (Tally format) or batches (Chirix format)
            raw_batches = it.get("batchallocations") or it.get("batches") or []
            batches = []
            for b in raw_batches:
                if not isinstance(b, dict):
                    continue
                b_qty_str = str(b.get("actualqty", "")).strip()
                b_qty = float(b_qty_str.split()[0]) if b_qty_str and b_qty_str.split()[0].replace('.', '', 1).isdigit() else qty_val
                batches.append({
                    "godown": b.get("godownname") or b.get("godown") or "Main Location",
                    "batch_name": b.get("batchname") or b.get("batch_name") or "Primary Batch",
                    "quantity": b_qty,
                    "amount": abs(float(b.get("amount", amount_val)))
                })

            hsn_code = str(
                it.get("gsthsnname")
                or it.get("hsncode")
                or it.get("hsnsac")
                or it.get("hsn")
                or it.get("hsn_code")
                or ""
            ).strip()
            if not hsn_code:
                if isinstance(it.get("hsndetails"), list):
                    for hd in it.get("hsndetails", []):
                        if isinstance(hd, dict):
                            hsn_code = str(hd.get("hsncode") or hd.get("gsthsnname") or hd.get("hsnsac") or hd.get("hsn") or "").strip()
                            if hsn_code:
                                break
            if not hsn_code:
                for rd in it.get("ratedetails", []):
                    if isinstance(rd, dict):
                        hsn_code = str(rd.get("hsncode") or rd.get("hsnsac") or "").strip()
                        if hsn_code:
                            break

            # Determine tax rate from ratedetails if available
            item_tax_rate = 18.0
            for rd in it.get("ratedetails", []):
                if isinstance(rd, dict) and str(rd.get("gstratedutyhead", "")).lower() in ("integrated tax", "igst"):
                    try:
                        item_tax_rate = float(str(rd.get("gstrate", "18")).strip())
                    except (ValueError, TypeError):
                        pass
                    break

            items.append({
                "item_name": item_name,
                "unit": unit,
                "quantity": qty_val,
                "rate": rate_val,
                "amount": amount_val,
                "tax_rate": item_tax_rate,
                "sales_ledger": "Sales",
                "hsn_code": hsn_code,
                "batches": batches
            })

        # ── Extract tax amounts from allledgerentries / ledgerentries ──────────────
        all_ledger_entries = list(raw_inv.get("allledgerentries", []) or raw_inv.get("ledgerentries", []))
        seen_ledger_names: set = {str(e.get("ledgername", "")).strip().lower() for e in all_ledger_entries}
        # Also scan accountingallocations inside each inventory entry for tax ledgers
        for it in raw_items:
            if isinstance(it, dict):
                for acct in it.get("accountingallocations", []):
                    if isinstance(acct, dict):
                        lname = str(acct.get("ledgername", "")).strip().lower()
                        if lname and lname not in ("sales", party_name.lower()) and lname not in seen_ledger_names:
                            all_ledger_entries.append(acct)
                            seen_ledger_names.add(lname)

        cgst_amt = float(raw_inv.get("cgst_amount", 0.0))
        sgst_amt = float(raw_inv.get("sgst_amount", 0.0))
        igst_amt = float(raw_inv.get("igst_amount", 0.0))
        round_off = float(raw_inv.get("round_off") or raw_inv.get("roundoff") or 0.0)
        grand_total = float(raw_inv.get("grand_total") or raw_inv.get("amount") or 0.0)

        for l_entry in all_ledger_entries:
            if not isinstance(l_entry, dict):
                continue
            lname = str(l_entry.get("ledgername", "")).strip().lower()
            amt = float(l_entry.get("amount", 0))
            if l_entry.get("ispartyledger") or lname == party_name.lower():
                if grand_total == 0.0:
                    grand_total = abs(amt)
            elif "cgst" in lname or "central tax" in lname:
                if cgst_amt == 0.0:
                    cgst_amt = abs(amt)
            elif "sgst" in lname or "state tax" in lname:
                if sgst_amt == 0.0:
                    sgst_amt = abs(amt)
            elif "igst" in lname or "integrated tax" in lname:
                if igst_amt == 0.0:
                    igst_amt = abs(amt)
            elif "round off" in lname or "roundoff" in lname:
                if round_off == 0.0:
                    round_off = amt
            elif lname == party_name.lower() or l_entry.get("ispartyledger"):
                if grand_total == 0.0:
                    grand_total = abs(amt)

        # Also search top-level ledger entries for party total
        for l_entry in l_entries:
            if not isinstance(l_entry, dict):
                continue
            lname = str(l_entry.get("ledgername", "")).strip().lower()
            amt = float(l_entry.get("amount", 0))
            if l_entry.get("ispartyledger") or lname == party_name.lower():
                if grand_total == 0.0:
                    grand_total = abs(amt)

        # If still no grand_total, derive from item totals + tax
        if grand_total == 0.0:
            grand_total = subtotal + cgst_amt + sgst_amt + igst_amt

        # If grand_total still zero, sum all inventory amounts as fallback
        if grand_total == 0.0:
            grand_total = subtotal

        if round_off == 0.0 and grand_total > 0.0:
            calc_diff = grand_total - (subtotal + cgst_amt + sgst_amt + igst_amt)
            if abs(calc_diff) > 0.001 and abs(calc_diff) <= 10.0:
                round_off = calc_diff

        return {
            "invoice_no": inv_no,
            "date": date_val,
            "order_no": order_no,
            "order_date": order_date,
            "vchentrymode": raw_inv.get("vchentrymode", "Accounting Invoice"),
            "customer": {
                "name": party_name,
                "gstin": party_gstin,
                "state": state_name,
                "country": country_name,
                "pincode": pincode_val,
                "address": address_list,
                "gst_registration_type": raw_inv.get("gstregistrationtype", "Regular")
            },
            "dispatch_from": {
                "name": raw_inv.get("dispatchfromname", "Company"),
                "gstin": raw_inv.get("cmpgstin", ""),
                "state": raw_inv.get("dispatchfromstatename", state_name),
                "pincode": raw_inv.get("dispatchfrompincode", "")
            },
            "irn": raw_inv.get("irn", ""),
            "items": items if items else [{
                "item_name": "General Goods",
                "unit": "Nos",
                "quantity": 1,
                "rate": grand_total,
                "amount": grand_total,
                "sales_ledger": "Sales"
            }],
            "subtotal": subtotal if items else grand_total,
            "taxes": {
                "cgst_rate": 9.0, "cgst_amount": cgst_amt,
                "sgst_rate": 9.0, "sgst_amount": sgst_amt,
                "igst_rate": 18.0 if igst_amt > 0 else 0.0, "igst_amount": igst_amt
            },
            "round_off": round_off,
            "grand_total": grand_total
        }

    # Standard Chirix JSON Format
    customer = raw_inv.get("customer", {})
    if isinstance(customer, str):
        customer = {"name": customer}

    items_raw = raw_inv.get("items", [])
    # FIX: normalize hsn_code key variants for the plain Chirix format too, so
    # whatever key name Chirix's API happens to use still lands on hsn_code.
    items = []
    for it in items_raw:
        it = dict(it) if isinstance(it, dict) else {}
        it["hsn_code"] = str(
            it.get("hsn_code") or it.get("hsncode") or it.get("hsnsac") or it.get("hsn") or it.get("gsthsnname") or ""
        ).strip()
        items.append(it)

    subtotal = float(raw_inv.get("subtotal", sum(float(i.get("amount", 0)) for i in items)))
    taxes = raw_inv.get("taxes", {})
    cgst = float(taxes.get("cgst_amount", 0.0))
    sgst = float(taxes.get("sgst_amount", 0.0))
    igst = float(taxes.get("igst_amount", 0.0))
    grand_total = float(raw_inv.get("grand_total", subtotal + cgst + sgst + igst))

    round_off = float(raw_inv.get("round_off") or raw_inv.get("roundoff") or taxes.get("round_off") or taxes.get("roundoff") or 0.0)
    if round_off == 0.0:
        calc_diff = grand_total - (subtotal + cgst + sgst + igst)
        if abs(calc_diff) > 0.001 and abs(calc_diff) <= 10.0:
            round_off = calc_diff

    inv_no_raw = raw_inv.get("invoice_no") or raw_inv.get("vouchernumber")
    cust_raw = customer.get("name") or raw_inv.get("partyledgername")

    if grand_total <= 0 and not inv_no_raw and not cust_raw:
        raise ValueError(f"Invoice data unrecognized — no invoice_no, customer, or grand_total found in keys: {list(raw_inv.keys())}")

    order_no = str(raw_inv.get("order_no") or raw_inv.get("orderno") or raw_inv.get("basicpurchaseorderno") or raw_inv.get("order_ref") or raw_inv.get("po_number") or "").strip()
    order_date = raw_inv.get("order_date") or raw_inv.get("basicorderdate") or raw_inv.get("date", datetime.now().strftime("%Y-%m-%d"))

    address_raw = customer.get("address") or raw_inv.get("address") or raw_inv.get("buyeraddress") or []
    if isinstance(address_raw, str):
        address_list = [a.strip() for a in address_raw.split("\n") if a.strip()]
    elif isinstance(address_raw, list):
        address_list = [str(a).strip() for a in address_raw if str(a).strip() and str(a) != "True" and not str(a).startswith("{")]
    else:
        address_list = []

    return {
        "invoice_no": str(inv_no_raw or "INV-1001"),
        "date": raw_inv.get("date", datetime.now().strftime("%Y-%m-%d")),
        "order_no": order_no,
        "order_date": order_date,
        "customer": {
            "name": str(cust_raw or "Sundry Debtor"),
            "gstin": customer.get("gstin") or raw_inv.get("partygstin") or raw_inv.get("consigneegstin") or raw_inv.get("buyer_gstin") or "",
            "state": customer.get("state") or raw_inv.get("statename") or raw_inv.get("placeofsupply") or "Tamil Nadu",
            "country": customer.get("country") or raw_inv.get("countryofresidence") or "India",
            "pincode": customer.get("pincode") or raw_inv.get("partypincode") or "",
            "address": address_list,
            "gst_registration_type": customer.get("gst_registration_type") or raw_inv.get("gstregistrationtype") or "Regular"
        },
        "dispatch_from": raw_inv.get("dispatch_from", {}),
        "irn": raw_inv.get("irn", ""),
        "items": items if items else [{
            "item_name": "Sales Item",
            "unit": "Nos",
            "quantity": 1,
            "rate": grand_total,
            "amount": grand_total,
            "sales_ledger": "Sales"
        }],
        "subtotal": subtotal,
        "taxes": taxes,
        "round_off": round_off,
        "grand_total": grand_total
    }


def validate_voucher_balance(raw_invoice: Dict[str, Any]) -> Tuple[bool, float, float, str]:
    """Validates that Total Debit == Total Credit."""
    inv = normalize_invoice_data(raw_invoice)
    grand_total = float(inv.get("grand_total", 0.0))
    items = inv.get("items", [])

    subtotal_calc = sum(float(item.get("amount", 0.0)) for item in items)
    taxes = inv.get("taxes", {})
    cgst = float(taxes.get("cgst_amount", 0.0))
    sgst = float(taxes.get("sgst_amount", 0.0))
    igst = float(taxes.get("igst_amount", 0.0))
    round_off = float(inv.get("round_off", 0.0))

    total_credit = subtotal_calc + cgst + sgst + igst + round_off
    total_debit = grand_total

    diff = abs(total_debit - total_credit)
    if diff > 0.05:
        msg = f"Imbalance detected! Debit (Party) = {total_debit:.2f}, Credit (Sales+Tax+RoundOff) = {total_credit:.2f}, Difference = {diff:.2f}"
        return False, total_debit, total_credit, msg

    return True, total_debit, total_credit, "Balanced"


def transform_invoice_to_tally_json(raw_invoice: Dict[str, Any], entry_mode: str = "Accounting Invoice") -> Dict[str, Any]:
    """Transforms raw Chirix / Tally JSON into Tally TALLYMESSAGE JSON format with full metadata (supports Accounting Invoice and Item Invoice modes)."""
    inv = normalize_invoice_data(raw_invoice)
    invoice_no = inv["invoice_no"]
    customer = inv["customer"]
    customer_name = customer["name"]
    items = inv["items"]

    tally_date = parse_and_format_date(inv.get("date", ""))
    order_date = parse_and_format_date(inv.get("order_date", ""))
    cust_address = customer.get("address", [])
    if isinstance(cust_address, str):
        cust_address = [cust_address]

    remoteid, vchkey, guid = generate_tally_guids()
    vch_entry_mode = raw_invoice.get("vchentrymode") or inv.get("vchentrymode") or entry_mode

    if vch_entry_mode == "Accounting Invoice":
        grand_total = float(inv.get("grand_total", 0.0))
        taxes = inv.get("taxes", {})
        cgst_amt = float(taxes.get("cgst_amount", 0.0))
        sgst_amt = float(taxes.get("sgst_amount", 0.0))
        igst_amt = float(taxes.get("igst_amount", 0.0))
        round_off = float(inv.get("round_off", 0.0))

        ledgerentries = []
        # Party Ledger Entry
        ledgerentries.append({
            "ledgername": customer_name,
            "isdeemedpositive": True,
            "ispartyledger": True,
            "amount": f"-{grand_total:.2f}",
            "billallocations": [
                {
                    "name": invoice_no,
                    "billtype": "New Ref",
                    "amount": f"-{grand_total:.2f}"
                }
            ]
        })

        # Line item revenue ledgers — consolidated by sales_ledger name
        # Group items that share the same sales ledger so each unique ledger
        # appears exactly ONCE with the sum of all its item amounts.
        from collections import OrderedDict
        ledger_groups: OrderedDict = OrderedDict()
        for item in items:
            sales_ledger = get_item_sales_ledger(item)
            amount_val = float(item.get("amount", 0))
            if sales_ledger not in ledger_groups:
                ledger_groups[sales_ledger] = {
                    "amount": 0.0,
                    "first_item": item  # keep metadata (HSN, tax_rate) from first item
                }
            ledger_groups[sales_ledger]["amount"] += amount_val

        for sales_ledger, grp in ledger_groups.items():
            first_item = grp["first_item"]
            amount_val = grp["amount"]
            item_name = str(first_item.get("item_name", "Sales Service")).strip()
            tax_rate = float(first_item.get("tax_rate", 18.0))
            half_tax = tax_rate / 2.0
            hsn_code_val = str(first_item.get("hsn_code", "")).strip()

            rate_details = [
                {"gstratedutyhead": "CGST", "gstratevaluationtype": "Based on Value", "gstrate": f" {half_tax:g}"},
                {"gstratedutyhead": "SGST/UTGST", "gstratevaluationtype": "Based on Value", "gstrate": f" {half_tax:g}"},
                {"gstratedutyhead": "IGST", "gstratevaluationtype": "Based on Value", "gstrate": f" {tax_rate:g}"},
                {"gstratedutyhead": "Cess", "gstratevaluationtype": "\u0004 Not Applicable"},
                {"gstratedutyhead": "State Cess", "gstratevaluationtype": "\u0004 Not Applicable"}
            ]

            ledgerentries.append({
                "ledgername": sales_ledger,
                "gsthsnname": hsn_code_val,
                "gsthsndescription": item_name,
                "isdeemedpositive": False,
                "ispartyledger": False,
                "amount": f"{amount_val:.2f}",
                "vatexpamount": f"{amount_val:.2f}",
                "ratedetails": rate_details
            })
            logger.info(f"Invoice {invoice_no}: Sales ledger '{sales_ledger}' consolidated -> INR {amount_val:.2f}")

        # Tax ledgers
        if cgst_amt > 0:
            ledgerentries.append({
                "ledgername": "CGST",
                "isdeemedpositive": False,
                "ispartyledger": False,
                "amount": f"{cgst_amt:.2f}",
                "vatexpamount": f"{cgst_amt:.2f}"
            })
        if sgst_amt > 0:
            ledgerentries.append({
                "ledgername": "SGST",
                "isdeemedpositive": False,
                "ispartyledger": False,
                "amount": f"{sgst_amt:.2f}",
                "vatexpamount": f"{sgst_amt:.2f}"
            })
        if igst_amt > 0:
            ledgerentries.append({
                "ledgername": "IGST",
                "isdeemedpositive": False,
                "ispartyledger": False,
                "amount": f"{igst_amt:.2f}",
                "vatexpamount": f"{igst_amt:.2f}"
            })

        if abs(round_off) > 0.001:
            is_deemed_pos = round_off < 0
            ledgerentries.append({
                "ledgername": "Round Off",
                "isdeemedpositive": is_deemed_pos,
                "ispartyledger": False,
                "amount": f"{round_off:.2f}"
            })

        tally_record = {
            "metadata": {
                "type": "Voucher",
                "remoteid": remoteid,
                "vchkey": vchkey,
                "vchtype": "Sales",
                "action": "Create",
                "objview": "Invoice Voucher View"
            },
            "address": [{"metadata": True, "type": "String"}] + cust_address,
            "basicbuyeraddress": [{"metadata": True, "type": "String"}] + cust_address,
            "date": tally_date,
            "vchstatusdate": tally_date,
            "guid": guid,
            "statename": customer.get("state", "Tamil Nadu"),
            "countryofresidence": customer.get("country", "India"),
            "partygstin": customer.get("gstin", ""),
            "basicbuyerssalestaxno": customer.get("gstin", ""),
            "vouchertypename": "Sales",
            "partyname": customer_name,
            "partyledgername": customer_name,
            "vouchernumber": invoice_no,
            "basicbuyername": customer_name,
            "partymailingname": customer_name,
            "partypincode": customer.get("pincode", ""),
            "placeofsupply": customer.get("state", "Tamil Nadu"),
            "basicpurchaseorderno": inv.get("order_no", ""),
            "basicorderdate": order_date,
            "vchentrymode": "Accounting Invoice",
            "persistedview": "Invoice Voucher View",
            "irn": inv.get("irn", ""),
            "effectivedate": tally_date,
            "isinvoice": True,
            "ledgerentries": ledgerentries
        }
        return tally_record

    allinventoryentries = []
    for item in items:
        item_name = str(item.get("item_name", "Stock Item")).strip()
        unit = str(item.get("unit", "Nos")).strip()
        qty = float(item.get("quantity", 1))
        rate_val = float(item.get("rate", 0))
        amount_val = float(item.get("amount", 0))
        sales_ledger = str(item.get("sales_ledger", "Sales")).strip()
        tax_rate = float(item.get("tax_rate", 18.0))

        batches = item.get("batches", [])
        batch_allocations = []
        if batches:
            for b in batches:
                godown = str(b.get("godown", "Main Location")).strip()
                batch_name = str(b.get("batch_name", "Primary Batch")).strip()
                b_qty = float(b.get("quantity", qty))
                b_amt = float(b.get("amount", amount_val))
                batch_allocations.append({
                    "godownname": godown,
                    "batchname": batch_name,
                    "orderno": inv.get("order_no", ""),
                    "destinationgodownname": godown,
                    "amount": f"{b_amt:.2f}",
                    "actualqty": f" {int(b_qty) if b_qty.is_integer() else b_qty} {unit}",
                    "billedqty": f" {int(b_qty) if b_qty.is_integer() else b_qty} {unit}"
                })
        else:
            batch_allocations.append({
                "godownname": "Main Location",
                "batchname": "Primary Batch",
                "orderno": inv.get("order_no", ""),
                "destinationgodownname": "Main Location",
                "amount": f"{amount_val:.2f}",
                "actualqty": f" {int(qty) if qty.is_integer() else qty} {unit}",
                "billedqty": f" {int(qty) if qty.is_integer() else qty} {unit}"
            })

        half_tax = tax_rate / 2.0
        rate_details = [
            {"gstratedutyhead": "Central Tax", "gstratevaluationtype": "Based on Value", "gstrate": f" {half_tax:g}"},
            {"gstratedutyhead": "State Tax", "gstratevaluationtype": "Based on Value", "gstrate": f" {half_tax:g}"},
            {"gstratedutyhead": "Integrated Tax", "gstratevaluationtype": "Based on Value", "gstrate": f" {tax_rate:g}"}
        ]

        hsn_code_val = str(item.get("hsn_code", "")).strip()
        inv_entry = {
            "stockitemname": item_name,
            "rate": f"{rate_val:.2f}/{unit}",
            "amount": f"{amount_val:.2f}",
            "actualqty": f" {int(qty) if qty.is_integer() else qty} {unit}",
            "billedqty": f" {int(qty) if qty.is_integer() else qty} {unit}",
            "gsthsnname": hsn_code_val,
            "hsncode": hsn_code_val,
            "batchallocations": batch_allocations,
            "accountingallocations": [
                {
                    "ledgername": sales_ledger,
                    "isdeemedpositive": False,
                    "amount": f"{amount_val:.2f}"
                }
            ],
            "ratedetails": rate_details
        }
        allinventoryentries.append(inv_entry)

    tally_record = {
        "metadata": {
            "type": "Voucher",
            "remoteid": remoteid,
            "vchkey": vchkey,
            "vchtype": "Sales",
            "action": "Create",
            "objview": "Invoice Voucher View"
        },
        "address": [{"metadata": True, "type": "String"}] + cust_address,
        "basicbuyeraddress": [{"metadata": True, "type": "String"}] + cust_address,
        "date": tally_date,
        "vchstatusdate": tally_date,
        "guid": guid,
        "statename": customer.get("state", "Tamil Nadu"),
        "countryofresidence": customer.get("country", "India"),
        "partygstin": customer.get("gstin", ""),
        "basicbuyerssalestaxno": customer.get("gstin", ""),
        "vouchertypename": "Sales",
        "partyname": customer_name,
        "partyledgername": customer_name,
        "vouchernumber": invoice_no,
        "basicbuyername": customer_name,
        "partymailingname": customer_name,
        "partypincode": customer.get("pincode", ""),
        "placeofsupply": customer.get("state", "Tamil Nadu"),
        "basicpurchaseorderno": inv.get("order_no", ""),
        "basicorderdate": order_date,
        "vchentrymode": "Item Invoice",
        "persistedview": "Invoice Voucher View",
        "irn": inv.get("irn", ""),
        "effectivedate": tally_date,
        "isinvoice": True,
        "allinventoryentries": allinventoryentries
    }

    return tally_record


def convert_invoice_to_xml_message(raw_invoice: Dict[str, Any], hsn_lookup: Optional[Dict[str, str]] = None, entry_mode: str = "Accounting Invoice") -> str:
    """Converts raw invoice to Tally TALLYMESSAGE XML. HSN codes auto-fetched from Tally if hsn_lookup provided."""
    inv = normalize_invoice_data(raw_invoice)
    tally_date = parse_and_format_date(inv.get("date", ""))
    invoice_no = inv["invoice_no"]
    customer = inv["customer"]
    customer_name = customer["name"]
    customer_gstin = customer.get("gstin", "")
    customer_state = customer.get("state", "Tamil Nadu")
    customer_country = customer.get("country", "India")
    customer_pincode = customer.get("pincode", "")
    gst_reg_type = customer.get("gst_registration_type", "Regular")
    cust_address = customer.get("address", [])

    order_no = inv.get("order_no", "")
    order_date = parse_and_format_date(inv.get("order_date", ""))

    items = inv["items"]
    taxes = inv.get("taxes", {})
    round_off = float(inv.get("round_off", 0.0))
    grand_total = float(inv.get("grand_total", 0.0))

    if grand_total <= 0:
        logger.warning(f"Skipping invoice - Invoice {invoice_no}: grand_total is zero - refusing to push an empty voucher")
        return ""

    vch_entry_mode = raw_invoice.get("vchentrymode") or inv.get("vchentrymode") or entry_mode

    logger.info(f"Invoice {invoice_no}: order_no={order_no!r} order_date={order_date!r} "
                f"mode={vch_entry_mode!r} item_hsns={[i.get('hsn_code') for i in items]!r}")

    order_xml = ""
    if order_no:
        order_xml = f"""
      <BASICPURCHASEORDERNO>{order_no}</BASICPURCHASEORDERNO>
      <BASICORDERDATE>{order_date}</BASICORDERDATE>
      <BASICORDERREF.LIST>
       <BASICORDERREF>{order_no}</BASICORDERREF>
      </BASICORDERREF.LIST>
      <INVOICEORDERLIST.LIST>
       <BASICPURCHASEORDERNO>{order_no}</BASICPURCHASEORDERNO>
       <BASICORDERDATE>{order_date}</BASICORDERDATE>
      </INVOICEORDERLIST.LIST>"""

    buyer_address_xml = ""
    consignee_address_xml = ""
    address_list_xml = ""
    if cust_address:
        lines_xml = "".join([f"\n       <BASICBUYERADDRESS>{line}</BASICBUYERADDRESS>" for line in cust_address if line])
        buyer_address_xml = f"      <BASICBUYERADDRESS.LIST>{lines_xml}\n      </BASICBUYERADDRESS.LIST>"

        lines_con = "".join([f"\n       <BASICCONSIGNEEADDRESS>{line}</BASICCONSIGNEEADDRESS>" for line in cust_address if line])
        consignee_address_xml = f"      <BASICCONSIGNEEADDRESS.LIST>{lines_con}\n      </BASICCONSIGNEEADDRESS.LIST>"

        addr_lines_xml = "".join([f"\n       <ADDRESS>{line}</ADDRESS>" for line in cust_address if line])
        address_list_xml = f"      <ADDRESS.LIST>{addr_lines_xml}\n      </ADDRESS.LIST>"

    vch_mode_xml = f"\n      <VCHENTRYMODE>{vch_entry_mode}</VCHENTRYMODE>" if vch_entry_mode else ""

    xml = f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <VOUCHER VCHTYPE="Sales" ACTION="Create" OBJVIEW="Invoice Voucher View">
      <DATE>{tally_date}</DATE>
      <EFFECTIVEDATE>{tally_date}</EFFECTIVEDATE>
      <IGNOREORIGVCHDATE>Yes</IGNOREORIGVCHDATE>
      <IGNOREPOSVALIDATION>Yes</IGNOREPOSVALIDATION>
      <IGNOREDUPLICATE>Yes</IGNOREDUPLICATE>
      <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
      <VOUCHERNUMBER>{invoice_no}</VOUCHERNUMBER>
      <PARTYLEDGERNAME>{customer_name}</PARTYLEDGERNAME>{vch_mode_xml}
      <PERSISTEDVIEW>Invoice Voucher View</PERSISTEDVIEW>
      <ISINVOICE>Yes</ISINVOICE>
      
      {order_xml}

      <!-- Buyer (Bill to) Details -->
      <BASICBUYERNAME>{customer_name}</BASICBUYERNAME>
      <PARTYMAILINGNAME>{customer_name}</PARTYMAILINGNAME>
      <STATENAME>{customer_state}</STATENAME>
      <COUNTRYOFRESIDENCE>{customer_country}</COUNTRYOFRESIDENCE>
      <PARTYGSTIN>{customer_gstin}</PARTYGSTIN>
      <BASICBUYERSSALESTAXNO>{customer_gstin}</BASICBUYERSSALESTAXNO>
      <GSTREGISTRATIONTYPE>{gst_reg_type}</GSTREGISTRATIONTYPE>
      <PARTYPINCODE>{customer_pincode}</PARTYPINCODE>
      <PLACEOFSUPPLY>{customer_state}</PLACEOFSUPPLY>
      
      <!-- Consignee (Ship to) Details -->
      <BASICCONSIGNEENAME>{customer_name}</BASICCONSIGNEENAME>
      <CONSIGNEEMAILINGNAME>{customer_name}</CONSIGNEEMAILINGNAME>
      <CONSIGNEESTATENAME>{customer_state}</CONSIGNEESTATENAME>
      <CONSIGNEECOUNTRYNAME>{customer_country}</CONSIGNEECOUNTRYNAME>
      <CONSIGNEEGSTIN>{customer_gstin}</CONSIGNEEGSTIN>
      <CONSIGNEEPINCODE>{customer_pincode}</CONSIGNEEPINCODE>
      
      {buyer_address_xml}
      {consignee_address_xml}
      {address_list_xml}

      <!-- Party Ledger Entry (Debit = Negative Amount) -->
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{customer_name}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE>
       <ISPARTYLEDGER>Yes</ISPARTYLEDGER>
       <ISLASTDEEMEDPOSITIVE>Yes</ISLASTDEEMEDPOSITIVE>
       <AMOUNT>-{grand_total:.2f}</AMOUNT>
       <BILLALLOCATIONS.LIST>
        <NAME>{invoice_no}</NAME>
        <BILLTYPE>New Ref</BILLTYPE>
        <AMOUNT>-{grand_total:.2f}</AMOUNT>
       </BILLALLOCATIONS.LIST>
      </LEDGERENTRIES.LIST>
"""

    cgst = float(taxes.get("cgst_amount", 0.0))
    sgst = float(taxes.get("sgst_amount", 0.0))
    igst = float(taxes.get("igst_amount", 0.0))

    # ── Accounting Invoice mode: consolidate items by sales ledger ──────────────
    # Build a merged ledger map so each unique sales ledger name appears only ONCE
    # in the XML, with the total amount across all items mapped to that ledger.
    if vch_entry_mode == "Accounting Invoice":
        from collections import OrderedDict
        acct_ledger_groups: OrderedDict = OrderedDict()
        for item in items:
            sales_ledger = get_item_sales_ledger(item)
            amount_val = float(item.get("amount", 0))
            if sales_ledger not in acct_ledger_groups:
                acct_ledger_groups[sales_ledger] = {
                    "amount": 0.0,
                    "first_item": item
                }
            acct_ledger_groups[sales_ledger]["amount"] += amount_val

        for sales_ledger, grp in acct_ledger_groups.items():
            first_item = grp["first_item"]
            amount_val = grp["amount"]
            item_name = str(first_item.get("item_name", "Stock Item")).strip()
            tax_rate = float(first_item.get("tax_rate", 18.0))
            half_tax = tax_rate / 2.0
            taxability_str = "Taxable" if tax_rate > 0 else "Exempt"

            item_hsn = str(first_item.get("hsn_code", "")).strip()
            if not item_hsn and hsn_lookup:
                item_hsn = hsn_lookup.get(item_name, "")
            hsn_xml = f"\n       <GSTHSNNAME>{item_hsn}</GSTHSNNAME>\n       <HSNCODE>{item_hsn}</HSNCODE>" if item_hsn else ""

            rate_details_xml = f"""
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {half_tax:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {half_tax:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {tax_rate:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>&#4; Not Applicable</GSTRATEVALUATIONTYPE>
       </RATEDETAILS.LIST>"""

            xml += f"""
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>{xml_escape(sales_ledger)}</LEDGERNAME>
       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
       <ISPARTYLEDGER>No</ISPARTYLEDGER>
       <GSTHSNNAME>{item_hsn}</GSTHSNNAME>
       <HSNCODE>{item_hsn}</HSNCODE>
       <GSTHSNDESCRIPTION>{xml_escape(item_name)}</GSTHSNDESCRIPTION>
       <AMOUNT>{amount_val:.2f}</AMOUNT>
       <VATEXPAMOUNT>{amount_val:.2f}</VATEXPAMOUNT>{rate_details_xml}
      </LEDGERENTRIES.LIST>"""
            logger.info(f"XML Invoice {invoice_no}: Sales ledger '{sales_ledger}' consolidated -> INR {amount_val:.2f}")

    else:
        # ── Item Invoice mode: iterate every item normally (no consolidation needed) ─
        for item in items:
            item_name = str(item.get("item_name", "Stock Item")).strip()
            unit = str(item.get("unit", "Nos")).strip()
            qty = float(item.get("quantity", 1))
            rate_val = float(item.get("rate", 0))
            amount_val = float(item.get("amount", 0))
            sales_ledger = get_item_sales_ledger(item)
            tax_rate = float(item.get("tax_rate", 18.0))
            half_tax = tax_rate / 2.0

            item_hsn = str(item.get("hsn_code", "")).strip()
            if not item_hsn and hsn_lookup:
                item_hsn = hsn_lookup.get(item_name, "")
            hsn_xml = f"\n       <GSTHSNNAME>{item_hsn}</GSTHSNNAME>\n       <HSNCODE>{item_hsn}</HSNCODE>" if item_hsn else ""

            rate_details_xml = f"""
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Central Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {half_tax:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>State Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {half_tax:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Integrated Tax</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
        <GSTRATE> {tax_rate:g}</GSTRATE>
       </RATEDETAILS.LIST>
       <RATEDETAILS.LIST>
        <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
        <GSTRATEVALUATIONTYPE>&#4; Not Applicable</GSTRATEVALUATIONTYPE>
       </RATEDETAILS.LIST>"""
            batches = item.get("batches", [])
            batch_alloc_xml = ""
            order_no_tag = f"\n        <ORDERNO>{order_no}</ORDERNO>" if order_no else ""

            if batches:
                for b in batches:
                    godown = str(b.get("godown", "Main Location")).strip()
                    batch_name = str(b.get("batch_name", "Primary Batch")).strip()
                    b_qty = float(b.get("quantity", qty))
                    b_amt = float(b.get("amount", amount_val))
                    batch_alloc_xml += f"""
       <BATCHALLOCATIONS.LIST>
        <GODOWNNAME>{godown}</GODOWNNAME>
        <BATCHNAME>{batch_name}</BATCHNAME>{order_no_tag}
        <AMOUNT>{b_amt:.2f}</AMOUNT>
        <ACTUALQTY> {int(b_qty) if b_qty.is_integer() else b_qty} {unit}</ACTUALQTY>
        <BILLEDQTY> {int(b_qty) if b_qty.is_integer() else b_qty} {unit}</BILLEDQTY>
       </BATCHALLOCATIONS.LIST>"""
            else:
                batch_alloc_xml = f"""
       <BATCHALLOCATIONS.LIST>
        <GODOWNNAME>Main Location</GODOWNNAME>
        <BATCHNAME>Primary Batch</BATCHNAME>{order_no_tag}
        <AMOUNT>{amount_val:.2f}</AMOUNT>
        <ACTUALQTY> {int(qty) if qty.is_integer() else qty} {unit}</ACTUALQTY>
        <BILLEDQTY> {int(qty) if qty.is_integer() else qty} {unit}</BILLEDQTY>
       </BATCHALLOCATIONS.LIST>"""

            xml += f"""
      <ALLINVENTORYENTRIES.LIST>
       <STOCKITEMNAME>{item_name}</STOCKITEMNAME>
       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
       <RATE>{rate_val:.2f}/{unit}</RATE>
       <AMOUNT>{amount_val:.2f}</AMOUNT>
       <ACTUALQTY> {int(qty) if qty.is_integer() else qty} {unit}</ACTUALQTY>
       <BILLEDQTY> {int(qty) if qty.is_integer() else qty} {unit}</BILLEDQTY>
       <TAXABILITY>Taxable</TAXABILITY>
       <GSTAPPLICABLE>Yes</GSTAPPLICABLE>{hsn_xml}{rate_details_xml}
       {batch_alloc_xml}
       <ACCOUNTINGALLOCATIONS.LIST>
        <LEDGERNAME>{sales_ledger}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
        <AMOUNT>{amount_val:.2f}</AMOUNT>
       </ACCOUNTINGALLOCATIONS.LIST>
      </ALLINVENTORYENTRIES.LIST>"""

    if cgst > 0:
        xml += f"""
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>CGST</LEDGERNAME>
       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
       <AMOUNT>{cgst:.2f}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
    if sgst > 0:
        xml += f"""
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>SGST</LEDGERNAME>
       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
       <AMOUNT>{sgst:.2f}</AMOUNT>
      </LEDGERENTRIES.LIST>"""
    if igst > 0:
        xml += f"""
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>IGST</LEDGERNAME>
       <ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE>
       <AMOUNT>{igst:.2f}</AMOUNT>
      </LEDGERENTRIES.LIST>"""

    if abs(round_off) > 0.001:
        is_deemed_pos = "No" if round_off >= 0 else "Yes"
        xml += f"""
      <LEDGERENTRIES.LIST>
       <LEDGERNAME>Round Off</LEDGERNAME>
       <ISDEEMEDPOSITIVE>{is_deemed_pos}</ISDEEMEDPOSITIVE>
       <AMOUNT>{round_off:.2f}</AMOUNT>
      </LEDGERENTRIES.LIST>"""

    xml += """
     </VOUCHER>
    </TALLYMESSAGE>"""
    return xml


def extract_stock_item_masters_info(raw_data: Any) -> Dict[str, Dict[str, Any]]:
    """
    Extracts Stock Item master details (name, parent, unit, HSN code, HSN description, GST tax rate, taxability)
    from explicit master records in payload as well as voucher inventory allocations.
    """
    stock_items: Dict[str, Dict[str, Any]] = {}
    items = extract_all_items(raw_data)

    # 1. Explicit Stock Item master objects in payload
    for it in items:
        if not isinstance(it, dict):
            continue
        meta_type = str(it.get("metadata", {}).get("type", "")).lower()
        is_item_obj = meta_type in ("stock item", "stockitem", "item") or (
            any(k in it for k in ("stockitemname", "baseunits", "hsndetails", "gstdetails"))
            and meta_type != "voucher" and not is_voucher_object(it)
        )

        if is_item_obj:
            name = str(it.get("metadata", {}).get("name") or it.get("name") or it.get("stockitemname") or "").strip()
            if not name:
                continue

            parent = str(it.get("parent") or it.get("stockgroup") or it.get("parent_group") or "Primary").strip()
            unit = str(it.get("baseunits") or it.get("unit") or it.get("uom") or "Nos").strip()

            # HSN details
            hsn_code = str(it.get("gsthsnname") or it.get("hsncode") or it.get("hsnsac") or it.get("hsn") or it.get("hsn_code") or "").strip()
            hsn_desc = str(it.get("hsndescription") or it.get("hsn_description") or "").strip()

            hsn_details = it.get("hsndetails", [])
            if isinstance(hsn_details, list) and hsn_details:
                for hd in reversed(hsn_details):
                    if isinstance(hd, dict):
                        if not hsn_code:
                            hsn_code = str(hd.get("hsncode") or hd.get("gsthsnname") or hd.get("hsnsac") or hd.get("hsn") or "").strip()
                        if not hsn_desc:
                            hsn_desc = str(hd.get("hsndescription") or hd.get("description") or "").strip()
                        if hsn_code:
                            break

            # Tax rate / GST details
            tax_rate = 18.0
            tax_rate_val = it.get("tax_rate") or it.get("gstrate") or it.get("igstrate")
            if tax_rate_val is not None:
                try:
                    tax_rate = float(tax_rate_val)
                except (ValueError, TypeError):
                    pass
            else:
                for rd in it.get("ratedetails", []) or it.get("gstdetails", []):
                    if isinstance(rd, dict) and str(rd.get("gstratedutyhead", "")).lower() in ("integrated tax", "igst"):
                        try:
                            tax_rate = float(str(rd.get("gstrate", "18")).strip())
                        except (ValueError, TypeError):
                            pass
                        break

            taxability = str(it.get("taxability") or "Taxable").strip()

            stock_items[name] = {
                "name": name,
                "parent": parent,
                "unit": unit,
                "hsn_code": hsn_code,
                "hsn_description": hsn_desc,
                "tax_rate": tax_rate,
                "taxability": taxability
            }

    # 2. Extract implicit stock items from Vouchers in payload
    vouchers = extract_vouchers_from_json(raw_data)
    for raw_inv in vouchers:
        try:
            inv = normalize_invoice_data(raw_inv)
            for item in inv.get("items", []):
                iname = str(item.get("item_name") or "").strip()
                if not iname:
                    continue

                i_unit = str(item.get("unit") or "Nos").strip()
                i_hsn = str(item.get("hsn_code") or "").strip()
                i_tax = float(item.get("tax_rate", 18.0))

                if iname not in stock_items:
                    stock_items[iname] = {
                        "name": iname,
                        "parent": "Primary",
                        "unit": i_unit,
                        "hsn_code": i_hsn,
                        "hsn_description": "",
                        "tax_rate": i_tax,
                        "taxability": "Taxable"
                    }
                else:
                    if not stock_items[iname].get("hsn_code") and i_hsn:
                        stock_items[iname]["hsn_code"] = i_hsn
                    if stock_items[iname].get("unit") == "Nos" and i_unit != "Nos":
                        stock_items[iname]["unit"] = i_unit
                    if i_tax != 18.0 and stock_items[iname].get("tax_rate") == 18.0:
                        stock_items[iname]["tax_rate"] = i_tax
        except Exception:
            pass

    return stock_items


def extract_ledger_masters_info(raw_data: Any) -> Dict[str, Dict[str, Any]]:
    """
    Extracts Ledger master details (name, parent, address, state, country, pincode, GSTIN, GST registration type, billwise)
    from explicit master records in payload as well as voucher party ledgers.
    """
    ledgers: Dict[str, Dict[str, Any]] = {
        "Sales": {"name": "Sales", "parent": "Sales Accounts"},
        "CGST": {"name": "CGST", "parent": "Duties & Taxes", "duty_head": "Central Tax"},
        "SGST": {"name": "SGST", "parent": "Duties & Taxes", "duty_head": "State Tax"},
        "IGST": {"name": "IGST", "parent": "Duties & Taxes", "duty_head": "Integrated Tax"},
        "Round Off": {"name": "Round Off", "parent": "Indirect Expenses"}
    }

    items = extract_all_items(raw_data)

    # 1. Explicit Ledger / Party / Customer master objects in payload
    for it in items:
        if not isinstance(it, dict):
            continue
        meta_type = str(it.get("metadata", {}).get("type", "")).lower()
        is_ledger_obj = meta_type in ("ledger", "party", "customer", "vendor", "account", "ledger master") or (
            any(k in it for k in ("ledgername", "partyledgername", "partyname", "partygstin", "basicbuyername", "gstregistrationtype"))
            and meta_type != "voucher" and not is_voucher_object(it)
        ) or ("name" in it and ("gstin" in it or "partygstin" in it or "address" in it or "statename" in it or "parent" in it) and not is_voucher_object(it))

        if is_ledger_obj:
            name = unwrap_val(
                it.get("metadata", {}).get("name")
                or it.get("name")
                or it.get("ledgername")
                or it.get("partyledgername")
                or it.get("partyname")
                or it.get("basicbuyername")
                or it.get("customer_name")
            )
            if not name or name.lower() in ("voucher", "stock item", "unit", "godown", "group"):
                continue

            parent = unwrap_val(it.get("parent") or it.get("group") or it.get("parentgroup") or "Sundry Debtors")
            if not parent:
                parent = "Sundry Debtors"

            op_bal = unwrap_val(it.get("ledopeningbalance") or it.get("openingbalance") or it.get("tbalopening") or "")
            closing_bal = unwrap_val(it.get("closingbalance") or "")
            phone = unwrap_val(it.get("ledgerphone") or "")
            contact = unwrap_val(it.get("ledgercontact") or "")

            addr_raw = it.get("address") or it.get("basicbuyeraddress") or it.get("partyaddress") or it.get("mailingaddress") or []
            if isinstance(addr_raw, str):
                addr_list = [a.strip() for a in addr_raw.split("\n") if a.strip()]
            elif isinstance(addr_raw, list):
                addr_list = [unwrap_val(a) for a in addr_raw if unwrap_val(a) and unwrap_val(a) != "True" and not unwrap_val(a).startswith("{")]
            else:
                addr_list = []

            state = unwrap_val(it.get("statename") or it.get("state") or it.get("placeofsupply") or "Tamil Nadu")
            country = unwrap_val(it.get("countryname") or it.get("countryofresidence") or it.get("country") or "India")
            pincode = unwrap_val(it.get("pincode") or it.get("partypincode") or "")
            gstin = unwrap_val(it.get("partygstin") or it.get("gstin") or it.get("basicbuyerssalestaxno") or "")
            gst_reg_type = unwrap_val(it.get("gstregistrationtype") or it.get("gst_registration_type") or "Regular")
            is_billwise = bool(it.get("isbillwiseon", it.get("is_billwise", parent in ("Sundry Debtors", "Sundry Creditors"))))

            ledgers[name] = {
                "name": name,
                "parent": parent,
                "opening_balance": op_bal,
                "closing_balance": closing_bal,
                "phone": phone,
                "contact": contact,
                "address": addr_list,
                "state": state,
                "country": country,
                "pincode": pincode,
                "gstin": gstin,
                "gst_registration_type": gst_reg_type,
                "is_billwise": is_billwise
            }

    # 2. Extract implicit party ledgers from Vouchers in payload
    vouchers = extract_vouchers_from_json(raw_data)
    for raw_inv in vouchers:
        try:
            inv = normalize_invoice_data(raw_inv)
            cust = inv.get("customer", {})
            cname = str(cust.get("name") or "").strip()
            if cname:
                if cname not in ledgers:
                    ledgers[cname] = {
                        "name": cname,
                        "parent": "Sundry Debtors",
                        "address": cust.get("address", []),
                        "state": cust.get("state", "Tamil Nadu"),
                        "country": cust.get("country", "India"),
                        "pincode": cust.get("pincode", ""),
                        "gstin": cust.get("gstin", ""),
                        "gst_registration_type": cust.get("gst_registration_type", "Regular"),
                        "is_billwise": True
                    }
                else:
                    l_entry = ledgers[cname]
                    if not l_entry.get("gstin") and cust.get("gstin"):
                        l_entry["gstin"] = cust.get("gstin")
                    if not l_entry.get("address") and cust.get("address"):
                        l_entry["address"] = cust.get("address")
                    if not l_entry.get("pincode") and cust.get("pincode"):
                        l_entry["pincode"] = cust.get("pincode")
                    if (not l_entry.get("state") or l_entry.get("state") == "Tamil Nadu") and cust.get("state"):
                        l_entry["state"] = cust.get("state")
        except Exception:
            pass

    # 3. Extract implicit Sales Accounts ledgers from item sales_ledger fields in Vouchers
    for raw_inv in vouchers:
        try:
            inv = normalize_invoice_data(raw_inv)
            for item in inv.get("items", []):
                s_ledger = get_item_sales_ledger(item)
                item_hsn = str(item.get("hsn_code") or "").strip()
                tax_rate = float(item.get("tax_rate", 18.0))
                if s_ledger:
                    if s_ledger not in ledgers:
                        ledgers[s_ledger] = {
                            "name": s_ledger,
                            "parent": "Sales Accounts",
                            "hsn_code": item_hsn,
                            "tax_rate": tax_rate
                        }
                    else:
                        if not ledgers[s_ledger].get("hsn_code") and item_hsn:
                            ledgers[s_ledger]["hsn_code"] = item_hsn
                        if "tax_rate" not in ledgers[s_ledger]:
                            ledgers[s_ledger]["tax_rate"] = tax_rate
        except Exception:
            pass

    return ledgers


def extract_unit_masters_info(raw_data: Any, stock_items: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Extracts Unit master details (name, uqc_code, decimal_places) from explicit unit records
    in payload as well as stock item units and voucher units.
    """
    units: Dict[str, Dict[str, Any]] = {"NOS": {"name": "NOS", "uqc": "NOS", "decimal_places": 0}, "Nos": {"name": "Nos", "uqc": "NOS", "decimal_places": 0}}
    items = extract_all_items(raw_data)

    # 1. Explicit Unit master objects in payload
    for it in items:
        if not isinstance(it, dict):
            continue
        meta_type = str(it.get("metadata", {}).get("type", "")).lower()
        if meta_type == "unit":
            name = str(it.get("metadata", {}).get("name") or it.get("name") or it.get("unitname") or "").strip()
            if not name:
                continue
            uqc = str(it.get("gstinuqc") or it.get("uqc") or UQC_MAP.get(name, name.upper())).strip()
            dec = int(it.get("decimalplaces", 0))
            units[name] = {"name": name, "uqc": uqc, "decimal_places": dec}

    # 2. Units from Stock Items
    for s_info in stock_items.values():
        u_name = s_info.get("unit", "NOS")
        if u_name and u_name not in units:
            uqc = UQC_MAP.get(u_name, u_name.upper())
            units[u_name] = {"name": u_name, "uqc": uqc, "decimal_places": 0}

    return units


def ensure_tally_masters_exist(raw_data: Any, tally_url: str = TALLY_HTTP_URL, hsn_lookup: Optional[Dict[str, str]] = None) -> str:
    """Stage 2: Syncs explicit and implicit invoice masters (Ledgers, Stock Items, Units, Godowns) with full details to TallyPrime over HTTP."""
    logger.info("STAGE 2: Ensuring all required Masters exist in TallyPrime...")

    local_hsn_map = dict(hsn_lookup) if hsn_lookup is not None else extract_stock_item_hsn_map(raw_data)
    payload_hsn_map = extract_stock_item_hsn_map(raw_data)
    for k, v in payload_hsn_map.items():
        if k not in local_hsn_map or not local_hsn_map[k]:
            local_hsn_map[k] = v

    stock_items_info = extract_stock_item_masters_info(raw_data)
    ledgers_info = extract_ledger_masters_info(raw_data)
    units_info = extract_unit_masters_info(raw_data, stock_items_info)

    godowns = set()
    stock_groups = set()
    vouchers = extract_vouchers_from_json(raw_data)
    for raw_inv in vouchers:
        try:
            inv = normalize_invoice_data(raw_inv)
            for item in inv.get("items", []):
                for b in item.get("batches", []):
                    g_name = b.get("godown")
                    if g_name:
                        godowns.add(g_name)
        except Exception:
            pass

    for s_info in stock_items_info.values():
        sg = s_info.get("parent")
        if sg and sg != "Primary":
            stock_groups.add(sg)

    master_xml_elements = ""

    # 1. LEDGERS XML
    for l_name, l_info in ledgers_info.items():
        if l_name in ("CGST", "SGST", "IGST"):
            duty_head = "CGST" if l_name == "CGST" else ("SGST/UTGST" if l_name == "SGST" else "IGST")
            master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <LEDGER NAME="{xml_escape(l_name)}" ACTION="Create">
      <NAME>{xml_escape(l_name)}</NAME>
      <PARENT>Duties &amp; Taxes</PARENT>
      <TAXTYPE>GST</TAXTYPE>
      <GSTDUTYHEAD>{duty_head}</GSTDUTYHEAD>
      <RATEOFTAXCALCULATION>0</RATEOFTAXCALCULATION>
     </LEDGER>
    </TALLYMESSAGE>"""
        elif l_info.get("parent") == "Sales Accounts" or l_name == "Sales" or l_name.startswith("Sales"):
            hsn_code = xml_escape(l_info.get("hsn_code", ""))
            tax_rate = float(l_info.get("tax_rate", 18.0))
            half_tax = tax_rate / 2.0
            taxability_val = "Taxable" if tax_rate > 0 else "Exempt"

            hsn_master_block = f"""
      <GSTHSNNAME>{hsn_code}</GSTHSNNAME>
      <HSNCODE>{hsn_code}</HSNCODE>
      <HSNDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <HSNCODE>{hsn_code}</HSNCODE>
       <HSNDESCRIPTION>{xml_escape(l_name)}</HSNDESCRIPTION>
       <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
      </HSNDETAILS.LIST>""" if hsn_code else ""

            gst_details_block = f"""
      <GSTDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <TAXABILITY>{taxability_val}</TAXABILITY>
       <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
       <STATEWISEDETAILS.LIST>
        <STATENAME>&#4; Any</STATENAME>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {half_tax:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {half_tax:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {tax_rate:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>&#4; Not Applicable</GSTRATEVALUATIONTYPE>
        </RATEDETAILS.LIST>
       </STATEWISEDETAILS.LIST>
      </GSTDETAILS.LIST>"""

            master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <LEDGER NAME="{xml_escape(l_name)}" ACTION="Create/Alter">
      <NAME>{xml_escape(l_name)}</NAME>
      <PARENT>Sales Accounts</PARENT>
      <GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>
      <ISGSTAPPLICABLE>Yes</ISGSTAPPLICABLE>
      <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
      <TAXABILITY>{taxability_val}</TAXABILITY>
      <ISTAXABLE>{"Yes" if taxability_val == "Taxable" else "No"}</ISTAXABLE>{hsn_master_block}{gst_details_block}
     </LEDGER>
    </TALLYMESSAGE>"""
        elif l_name == "Round Off":
            master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <LEDGER NAME="Round Off" ACTION="Create">
      <NAME>Round Off</NAME>
      <PARENT>Indirect Expenses</PARENT>
      <ROUNDINGMETHOD>&#4; Normal Rounding</ROUNDINGMETHOD>
      <ROUNDLIMIT> 1</ROUNDLIMIT>
     </LEDGER>
    </TALLYMESSAGE>"""
        else:
            parent = xml_escape(l_info.get("parent", "Sundry Debtors"))
            addr_lines = l_info.get("address", [])
            state = xml_escape(l_info.get("state", "Tamil Nadu"))
            country = xml_escape(l_info.get("country", "India"))
            pincode = xml_escape(l_info.get("pincode", ""))
            gstin = xml_escape(l_info.get("gstin", ""))
            gst_reg_type = xml_escape(l_info.get("gst_registration_type", "Regular"))
            billwise_xml = "\n      <ISBILLWISEON>Yes</ISBILLWISEON>" if l_info.get("is_billwise", True) else ""

            addr_items_xml = "".join([f"\n       <ADDRESS>{xml_escape(line)}</ADDRESS>" for line in addr_lines if line])
            addr_direct_xml = "".join([f"\n      <ADDRESS>{xml_escape(line)}</ADDRESS>" for line in addr_lines if line])
            addr_list_block = f"\n      <ADDRESS.LIST>{addr_items_xml}\n      </ADDRESS.LIST>" if addr_items_xml else ""

            # Build mailing address XML block (for LEDGERMAILINGDETAILS only — no raw addr_direct_xml to avoid duplicate tags)
            mailing_addr_items_xml = "".join([f"\n        <ADDRESS>{xml_escape(line)}</ADDRESS>" for line in addr_lines if line])
            mailing_addr_list_block = f"\n       <ADDRESS.LIST>{mailing_addr_items_xml}\n       </ADDRESS.LIST>" if mailing_addr_items_xml else ""

            master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <LEDGER NAME="{xml_escape(l_name)}" ACTION="Create">
      <NAME>{xml_escape(l_name)}</NAME>
      <PARENT>{parent}</PARENT>
      <IGNOREDUPLICATE>Yes</IGNOREDUPLICATE>
      <MAILINGNAME>{xml_escape(l_name)}</MAILINGNAME>{addr_list_block}
      <STATENAME>{state}</STATENAME>
      <LEDSTATENAME>{state}</LEDSTATENAME>
      <COUNTRYNAME>{country}</COUNTRYNAME>
      <COUNTRYOFRESIDENCE>{country}</COUNTRYOFRESIDENCE>
      <PINCODE>{pincode}</PINCODE>
      <PARTYGSTIN>{gstin}</PARTYGSTIN>
      <BASICBUYERSSALESTAXNO>{gstin}</BASICBUYERSSALESTAXNO>
      <GSTREGISTRATIONTYPE>{gst_reg_type}</GSTREGISTRATIONTYPE>{billwise_xml}
      <LEDGERMAILINGDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <MAILINGNAME>{xml_escape(l_name)}</MAILINGNAME>{mailing_addr_list_block}
       <STATE>{state}</STATE>
       <STATENAME>{state}</STATENAME>
       <LEDSTATENAME>{state}</LEDSTATENAME>
       <COUNTRY>{country}</COUNTRY>
       <COUNTRYNAME>{country}</COUNTRYNAME>
       <PINCODE>{pincode}</PINCODE>
      </LEDGERMAILINGDETAILS.LIST>
      <LEDGSTREGDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <GSTREGISTRATIONTYPE>{gst_reg_type}</GSTREGISTRATIONTYPE>
       <GSTIN>{gstin}</GSTIN>
       <PARTYGSTIN>{gstin}</PARTYGSTIN>
      </LEDGSTREGDETAILS.LIST>
      <GSTDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <GSTREGISTRATIONTYPE>{gst_reg_type}</GSTREGISTRATIONTYPE>
       <PARTYGSTIN>{gstin}</PARTYGSTIN>
      </GSTDETAILS.LIST>
     </LEDGER>
    </TALLYMESSAGE>"""

    # 2. UNITS XML
    for u_name, u_info in units_info.items():
        uqc_code = xml_escape(u_info.get("uqc", "NOS"))
        dec_places = u_info.get("decimal_places", 2)
        master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <UNIT NAME="{xml_escape(u_name)}" ACTION="Create">
      <NAME>{xml_escape(u_name)}</NAME>
      <ISSIMPLEUNIT>Yes</ISSIMPLEUNIT>
      <GSTINUQC>{uqc_code}</GSTINUQC>
      <DECIMALPLACES>{dec_places}</DECIMALPLACES>
     </UNIT>
    </TALLYMESSAGE>"""

    # 3. STOCK GROUPS XML
    for sg_name in stock_groups:
        master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <STOCKGROUP NAME="{xml_escape(sg_name)}" ACTION="Create">
      <NAME>{xml_escape(sg_name)}</NAME>
     </STOCKGROUP>
    </TALLYMESSAGE>"""

    # 4. GODOWNS XML
    for g_name in godowns:
        master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <GODOWN NAME="{xml_escape(g_name)}" ACTION="Create">
      <NAME>{xml_escape(g_name)}</NAME>
     </GODOWN>
    </TALLYMESSAGE>"""

    # 5. STOCK ITEMS XML
    for s_name, s_info in stock_items_info.items():
        raw_parent = s_info.get("parent", "Primary")
        parent_xml = f"\n      <PARENT>{xml_escape(raw_parent)}</PARENT>" if raw_parent and raw_parent != "Primary" else ""
        base_unit = xml_escape(s_info.get("unit", "Nos"))

        s_hsn = s_info.get("hsn_code") or local_hsn_map.get(s_name, "")
        hsn_escaped = xml_escape(s_hsn)
        hsn_desc_escaped = xml_escape(s_info.get("hsn_description", ""))

        tax_rate = float(s_info.get("tax_rate", 18.0))
        half_tax = tax_rate / 2.0
        taxability = xml_escape(s_info.get("taxability", "Taxable"))

        hsn_xml_block = ""
        if s_hsn:
            hsn_desc_xml = f"\n       <HSNDESCRIPTION>{hsn_desc_escaped}</HSNDESCRIPTION>" if hsn_desc_escaped else ""
            hsn_xml_block = f"""
      <GSTHSNNAME>{hsn_escaped}</GSTHSNNAME>
      <HSNCODE>{hsn_escaped}</HSNCODE>
      <HSNDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <HSNCODE>{hsn_escaped}</HSNCODE>{hsn_desc_xml}
       <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
      </HSNDETAILS.LIST>"""

        gst_details_block = f"""
      <GSTDETAILS.LIST>
       <APPLICABLEFROM>20200101</APPLICABLEFROM>
       <TAXABILITY>{taxability}</TAXABILITY>
       <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
       <STATEWISEDETAILS.LIST>
        <STATENAME>&#4; Any</STATENAME>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {half_tax:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {half_tax:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>Based on Value</GSTRATEVALUATIONTYPE>
         <GSTRATE> {tax_rate:g}</GSTRATE>
        </RATEDETAILS.LIST>
        <RATEDETAILS.LIST>
         <GSTRATEDUTYHEAD>Cess</GSTRATEDUTYHEAD>
         <GSTRATEVALUATIONTYPE>&#4; Not Applicable</GSTRATEVALUATIONTYPE>
        </RATEDETAILS.LIST>
       </STATEWISEDETAILS.LIST>
      </GSTDETAILS.LIST>"""

        master_xml_elements += f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
     <STOCKITEM NAME="{xml_escape(s_name)}" ACTION="Create">
      <NAME>{xml_escape(s_name)}</NAME>{parent_xml}
      <BASEUNITS>{base_unit}</BASEUNITS>
      <GSTAPPLICABLE>&#4; Applicable</GSTAPPLICABLE>
      <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
      <TAXABILITY>{taxability}</TAXABILITY>{hsn_xml_block}{gst_details_block}
     </STOCKITEM>
    </TALLYMESSAGE>"""

    static_vars_xml = f"\n     <STATICVARIABLES>\n      <SVCURRENTCOMPANY>{xml_escape(TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>\n     </STATICVARIABLES>" if TALLY_COMPANY_NAME else ""
    master_xml = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Import Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <IMPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>All Masters</REPORTNAME>{static_vars_xml}
   </REQUESTDESC>
   <REQUESTDATA>
    {master_xml_elements}
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>"""

    try:
        req = urllib.request.Request(tally_url, data=master_xml.encode('utf-8'), headers={'Content-Type': 'text/xml'})
        res = urllib.request.urlopen(req, timeout=60).read().decode('utf-8', errors='ignore')
        logger.info("STAGE 2 Master Sync Response: " + res.replace('\n', ' '))
        return res
    except Exception as e:
        logger.warning(f"Could not sync masters to Tally Prime: {str(e)}")
        return f"<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><EXCEPTIONS>1</EXCEPTIONS><ERRORS>1</ERRORS><MESSAGE>{str(e)}</MESSAGE></RESPONSE>"


def fetch_stock_item_hsn_from_tally(tally_url: str = TALLY_HTTP_URL) -> Dict[str, str]:
    """FALLBACK: Queries TallyPrime for HSN codes of items not found in the local payload masters."""
    query_xml = """<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Export Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <EXPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>List of Stock Items</REPORTNAME>
    <STATICVARIABLES>
     <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
    </STATICVARIABLES>
   </REQUESTDESC>
  </EXPORTDATA>
 </BODY>
</ENVELOPE>"""
    try:
        req = urllib.request.Request(tally_url, data=query_xml.encode('utf-8'), headers={'Content-Type': 'text/xml'})
        response_xml = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', errors='ignore')

        hsn_map = {}
        for match in re.finditer(r'<STOCKITEM\s+NAME="([^"]+)"[^>]*>(.*?)</STOCKITEM>', response_xml, re.DOTALL | re.IGNORECASE):
            item_name = match.group(1).strip()
            item_body = match.group(2)
            hsn_match = re.search(r'<(?:HSNCODE|GSTHSNNAME)>(.*?)</(?:HSNCODE|GSTHSNNAME)>', item_body, re.DOTALL | re.IGNORECASE)
            if hsn_match:
                hsn_code = hsn_match.group(1).strip()
                if hsn_code:
                    hsn_map[item_name] = hsn_code

        if not hsn_map:
            logger.info("HTTP HSN fallback query returned no <HSNCODE> tags "
                        "('List of Stock Items' may not expose master fields — this is expected "
                        "if local payload masters already covered all items).")
        else:
            logger.info(f"Auto-fetched HSN codes for {len(hsn_map)} stock item(s) from TallyPrime (fallback).")
        return hsn_map
    except Exception as e:
        logger.warning(f"Could not auto-fetch HSN codes from Tally (will proceed without): {e}")
        return {}


def push_vouchers_to_tally(raw_invoices: Any, tally_url: str = TALLY_HTTP_URL, entry_mode: str = "Accounting Invoice") -> Dict[str, Any]:
    """
    STRICT 3-STEP PIPELINE:
    Step 1: Check File & Extract/Validate Vouchers & Masters
    Step 2: Create All Masters in TallyPrime (<REPORTNAME>All Masters</REPORTNAME>)
    Step 3: Push Transactions / Vouchers to TallyPrime (<REPORTNAME>Vouchers</REPORTNAME>)
    """
    logger.info("=== STEP 1: CHECKING FILE & VALIDATING PAYLOAD ===")
    vouchers = extract_vouchers_from_json(raw_invoices)

    if vouchers:
        try:
            first_norm = normalize_invoice_data(vouchers[0])
            logger.info("=== FIRST EXTRACTED VOUCHER DEBUG ===")
            logger.info(f"  invoice_no : {first_norm.get('invoice_no')}")
            logger.info(f"  order_no   : {first_norm.get('order_no')}")
            logger.info(f"  order_date : {first_norm.get('order_date')}")
            logger.info(f"  customer   : {first_norm.get('customer', {}).get('name')}")
            logger.info(f"  gstin      : {first_norm.get('customer', {}).get('gstin')}")
            logger.info(f"  item_hsns  : {[i.get('hsn_code') for i in first_norm.get('items', [])]}")
            logger.info(f"  round_off  : {first_norm.get('round_off')}")
            logger.info(f"  grand_total: {first_norm.get('grand_total')}")
            logger.info("===============================================")
        except Exception as ex:
            logger.error(f"Debug print failed: {str(ex)}")

    # FIX: build HSN lookup from local payload masters FIRST (fast, reliable — no HTTP needed),
    # then fall back to the HTTP fetch only for items not covered locally.
    logger.info("=== STEP 1.5: BUILDING HSN LOOKUP (local masters first, then Tally fallback) ===")
    hsn_lookup = extract_stock_item_hsn_map(raw_invoices)
    remote_hsn = fetch_stock_item_hsn_from_tally(tally_url)
    for k, v in remote_hsn.items():
        hsn_lookup.setdefault(k, v)

    logger.info("=== STEP 2: CREATING & SYNCING ALL MASTERS IN TALLY ===")
    master_xml_res = ensure_tally_masters_exist(raw_invoices, tally_url, hsn_lookup=hsn_lookup)

    valid_voucher_xmls = []
    for inv in vouchers:
        try:
            xml_msg = convert_invoice_to_xml_message(inv, hsn_lookup=hsn_lookup, entry_mode=entry_mode)
            if xml_msg:
                valid_voucher_xmls.append(xml_msg)
        except ValueError as ve:
            logger.warning(f"Skipping invalid invoice: {ve}")

    if not valid_voucher_xmls:
        logger.info("No valid voucher objects with non-zero amounts found to push.")
        return {
            "master_xml_response": master_xml_res,
            "voucher_xml_response": "<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><EXCEPTIONS>0</EXCEPTIONS></RESPONSE>",
            "no_vouchers": True
        }

    logger.info(f"=== STEP 3: PUSHING {len(valid_voucher_xmls)} TRANSACTION VOUCHER(S) TO TALLY ===")
    voucher_msgs = "".join(valid_voucher_xmls)

    static_vars_xml = f"\n     <STATICVARIABLES>\n      <SVCURRENTCOMPANY>{xml_escape(TALLY_COMPANY_NAME)}</SVCURRENTCOMPANY>\n     </STATICVARIABLES>" if TALLY_COMPANY_NAME else ""
    import_envelope = f"""<ENVELOPE>
 <HEADER>
  <TALLYREQUEST>Import Data</TALLYREQUEST>
 </HEADER>
 <BODY>
  <IMPORTDATA>
   <REQUESTDESC>
    <REPORTNAME>Vouchers</REPORTNAME>{static_vars_xml}
   </REQUESTDESC>
   <REQUESTDATA>
    {voucher_msgs}
   </REQUESTDATA>
  </IMPORTDATA>
 </BODY>
</ENVELOPE>"""

    try:
        req = urllib.request.Request(tally_url, data=import_envelope.encode('utf-8'), headers={'Content-Type': 'text/xml'})
        voucher_xml_res = urllib.request.urlopen(req, timeout=120).read().decode('utf-8', errors='ignore')
        logger.info("================ TALLY VOUCHER IMPORT RESPONSE ================")
        logger.info(voucher_xml_res.strip())
        logger.info("==============================================================")
        return {
            "master_xml_response": master_xml_res,
            "voucher_xml_response": voucher_xml_res
        }
    except Exception as e:
        logger.error(f"HTTP POST to Tally Prime failed: {str(e)}")
        raise


def generate_tally_masters_json(raw_data: Any) -> List[Dict[str, Any]]:
    """Generates native Tally JSON objects for all required Masters (Ledgers, Units, Stock Items)."""
    master_records = []
    stock_items_info = extract_stock_item_masters_info(raw_data)
    ledgers_info = extract_ledger_masters_info(raw_data)
    units_info = extract_unit_masters_info(raw_data, stock_items_info)

    # 1. Ledgers JSON
    for l_name, l_info in ledgers_info.items():
        if l_name in ("CGST", "SGST", "IGST"):
            duty_head = "CGST" if l_name == "CGST" else ("SGST/UTGST" if l_name == "SGST" else "IGST")
            master_records.append({
                "metadata": {"type": "Ledger", "name": l_name, "reservedname": ""},
                "parent": "Duties & Taxes",
                "taxtype": "GST",
                "gstdutyhead": duty_head,
                "rateoftaxcalculation": " 0"
            })
        elif l_info.get("parent") == "Sales Accounts" or l_name == "Sales" or l_name.startswith("Sales"):
            master_records.append({
                "metadata": {"type": "Ledger", "name": l_name, "reservedname": ""},
                "parent": "Sales Accounts",
                "isgstapplicable": True,
                "gsttypeofsupply": "Goods"
            })
        elif l_name == "Round Off":
            master_records.append({
                "metadata": {"type": "Ledger", "name": "Round Off", "reservedname": ""},
                "parent": "Indirect Expenses",
                "roundingmethod": "Normal Rounding",
                "roundlimit": " 1"
            })
        else:
            parent = l_info.get("parent", "Sundry Debtors")
            addr_list = l_info.get("address", [])
            state = l_info.get("state", "Tamil Nadu")
            country = l_info.get("country", "India")
            pincode = l_info.get("pincode", "")
            gstin = l_info.get("gstin", "")
            gst_reg_type = l_info.get("gst_registration_type", "Regular")

            master_records.append({
                "metadata": {"type": "Ledger", "name": l_name, "reservedname": ""},
                "parent": parent,
                "mailingname": l_name,
                "address": [{"metadata": True, "type": "String"}] + addr_list,
                "statename": state,
                "priorstatename": state,
                "countryofresidence": country,
                "partypincode": pincode,
                "partygstin": gstin,
                "basicbuyerssalestaxno": gstin,
                "gstregistrationtype": gst_reg_type,
                "isbillwiseon": True,
                "ledgermailingdetails": [
                    {
                        "applicablefrom": "20260401",
                        "mailingname": l_name,
                        "address": [{"metadata": True, "type": "String"}] + addr_list,
                        "state": state,
                        "country": country,
                        "pincode": pincode
                    }
                ]
            })

    # 2. Units JSON
    for u_name, u_info in units_info.items():
        master_records.append({
            "metadata": {"type": "Unit", "name": u_name, "reservedname": ""},
            "issimpleunit": True,
            "gstinuqc": u_info.get("uqc", "NOS"),
            "decimalplaces": f" {u_info.get('decimal_places', 0)}"
        })

    # 3. Stock Items JSON
    for s_name, s_info in stock_items_info.items():
        master_records.append({
            "metadata": {"type": "StockItem", "name": s_name, "reservedname": ""},
            "parent": s_info.get("parent", "Primary"),
            "baseunits": s_info.get("unit", "NOS"),
            "gstapplicable": "Applicable",
            "gsttypeofsupply": "Goods",
            "taxability": s_info.get("taxability", "Taxable"),
            "gsthsnname": s_info.get("hsn_code", ""),
            "hsncode": s_info.get("hsn_code", "")
        })

    return master_records


def convert_and_push(input_file: str, output_file: str, push_to_tally: bool = True, entry_mode: str = "Accounting Invoice"):
    """Main workflow: Converts Chirix JSON, writes tally-import.json, and pushes to Tally."""
    logger.info(f"Processing Chirix JSON file: '{input_file}'...")

    try:
        with open(input_file, 'rb') as f:
            raw_bytes = f.read()
        if raw_bytes.startswith(b'\xff\xfe') or raw_bytes.startswith(b'\xfe\xff'):
            raw_content = raw_bytes.decode('utf-16', errors='ignore')
        else:
            try:
                raw_content = raw_bytes.decode('utf-8')
            except UnicodeDecodeError:
                raw_content = raw_bytes.decode('utf-16', errors='ignore')
        data = json.loads(raw_content)
    except Exception as e:
        logger.error(f"Failed to read input JSON file '{input_file}': {str(e)}")
        sys.exit(1)

    invoices = extract_vouchers_from_json(data)
    logger.info(f"Loaded {len(invoices)} voucher record(s).")

    if invoices:
        try:
            first_norm = normalize_invoice_data(invoices[0])
            logger.info("=== FIRST EXTRACTED VOUCHER DEBUG ===")
            logger.info(f"  invoice_no : {first_norm.get('invoice_no')}")
            logger.info(f"  order_no   : {first_norm.get('order_no')}")
            logger.info(f"  order_date : {first_norm.get('order_date')}")
            logger.info(f"  customer   : {first_norm.get('customer', {}).get('name')}")
            logger.info(f"  gstin      : {first_norm.get('customer', {}).get('gstin')}")
            logger.info(f"  item_hsns  : {[i.get('hsn_code') for i in first_norm.get('items', [])]}")
            logger.info(f"  round_off  : {first_norm.get('round_off')}")
            logger.info(f"  grand_total: {first_norm.get('grand_total')}")
            logger.info("===============================================")
        except Exception as ex:
            logger.error(f"Debug print failed: {str(ex)}")

    tally_messages = []
    
    # 1. Add Master JSON objects (Ledgers, Units, Stock Items)
    try:
        master_jsons = generate_tally_masters_json(data)
        tally_messages.extend(master_jsons)
        logger.info(f"Generated {len(master_jsons)} master JSON record(s).")
    except Exception as ex:
        logger.warning(f"Could not generate master JSON records: {ex}")

    valid_invoices = []
    for idx, inv in enumerate(invoices, 1):
        inv_no = inv.get("invoice_no", f"INDEX_{idx}")
        try:
            tally_rec = transform_invoice_to_tally_json(inv, entry_mode=entry_mode)
            tally_messages.append(tally_rec)
            valid_invoices.append(inv)
            logger.info(f"[{idx}/{len(invoices)}] Invoice #{inv_no}: Validated & Converted.")
        except Exception as e:
            logger.error(f"[{idx}/{len(invoices)}] Invoice #{inv_no}: Validation Error - {str(e)}")

    output_data = {"tallymessage": tally_messages}
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)
    logger.info(f"Saved Tally JSON import file to: '{output_file}'")

    if push_to_tally:
        logger.info("Direct push to Tally Prime enabled. Sending payload...")
        try:
            push_vouchers_to_tally(data, TALLY_HTTP_URL, entry_mode=entry_mode)
            logger.info("SUCCESS: Masters & Vouchers pushed directly to Tally Prime!")
        except Exception as e:
            logger.error(f"Direct push to Tally failed: {str(e)}")


if __name__ == "__main__":
    input_path = sys.argv[1] if len(sys.argv) > 1 else "chirix_input_sample.json"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "tally-import.json"
    do_push = "--no-push" not in sys.argv

    convert_and_push(input_path, output_path, push_to_tally=do_push)