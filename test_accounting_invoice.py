#!/usr/bin/env python3
import json
import sys
from chirix_to_tally import normalize_invoice_data, transform_invoice_to_tally_json, convert_invoice_to_xml_message

# 1. Native Tally Accounting Invoice Payload (provided by user)
native_tally_accounting_inv = {
    "tallymessage": [
        {
            "metadata": {
                "type": "Voucher", 
                "remoteid": "f9dd5090-b152-45e9-9f26-0191e3c2c251-00000043", 
                "vchkey": "f9dd5090-b152-45e9-9f26-0191e3c2c251-0000b497:00000008", 
                "vchtype": "Sales", 
                "action": "Create", 
                "objview": "Invoice Voucher View"
            }, 
            "date": "20260729", 
            "vchstatusdate": "20260729", 
            "guid": "f9dd5090-b152-45e9-9f26-0191e3c2c251-00000043", 
            "statename": "Tamil Nadu", 
            "countryofresidence": "India", 
            "placeofsupply": "Tamil Nadu", 
            "vouchertypename": "Sales", 
            "partyname": "FIREBALL TECHNOLOGIES", 
            "cmpgstin": "33AABFT4937A1ZL", 
            "partyledgername": "FIREBALL TECHNOLOGIES", 
            "vouchernumber": "1", 
            "basicbuyername": "FIREBALL TECHNOLOGIES", 
            "vchentrymode": "Accounting Invoice", 
            "isinvoice": True, 
            "ledgerentries": [
                {
                    "ledgername": "FIREBALL TECHNOLOGIES", 
                    "isdeemedpositive": True, 
                    "ispartyledger": True, 
                    "amount": "-24780.00", 
                    "billallocations": [
                        {
                            "name": "1", 
                            "billtype": "New Ref", 
                            "amount": "-24780.00"
                        }
                    ]
                }, 
                {
                    "ledgername": "Sales Gst 18 %", 
                    "gsthsnname": "01011010", 
                    "gsthsndescription": "Live Horses", 
                    "isdeemedpositive": False, 
                    "ispartyledger": False, 
                    "amount": "1000.00", 
                    "vatexpamount": "1000.00", 
                    "ratedetails": [
                        {"gstratedutyhead": "Central Tax", "gstratevaluationtype": "Based on Value", "gstrate": " 9"},
                        {"gstratedutyhead": "State Tax", "gstratevaluationtype": "Based on Value", "gstrate": " 9"},
                        {"gstratedutyhead": "Integrated Tax", "gstratevaluationtype": "Based on Value", "gstrate": " 18"}
                    ]
                }, 
                {
                    "ledgername": "Sales Gst 18 %", 
                    "gsthsnname": "01011010", 
                    "isdeemedpositive": False, 
                    "ispartyledger": False, 
                    "amount": "10000.00"
                }, 
                {
                    "ledgername": "Sales Gst 18 %", 
                    "gsthsnname": "01011010", 
                    "isdeemedpositive": False, 
                    "ispartyledger": False, 
                    "amount": "10000.00"
                }, 
                {
                    "ledgername": "CGST", 
                    "amount": "1890.00"
                }, 
                {
                    "ledgername": "SGST", 
                    "amount": "1890.00"
                }
            ]
        }
    ]
}

# 2. Sample Chirix Item Invoice Payload
chirix_item_inv = {
    "invoice_no": "CHIRIX-INV-5001",
    "date": "2026-08-01",
    "customer": {
        "name": "FIREBALL TECHNOLOGIES",
        "gstin": "33AABFT4937A1ZL",
        "state": "Tamil Nadu"
    },
    "items": [
        {
            "item_name": "Software Development Services",
            "quantity": 1,
            "rate": 21000.00,
            "amount": 21000.00,
            "hsn_code": "01011010",
            "sales_ledger": "Sales Gst 18 %",
            "tax_rate": 18.0
        }
    ],
    "taxes": {
        "cgst_amount": 1890.00,
        "sgst_amount": 1890.00,
        "igst_amount": 0.0
    },
    "grand_total": 24780.00
}

def test_conversion():
    print("--- TEST 1: Parsing Native Tally Accounting Invoice ---")
    norm1 = normalize_invoice_data(native_tally_accounting_inv["tallymessage"][0])
    print("Normalized Invoice No:", norm1["invoice_no"])
    print("Normalized Customer:", norm1["customer"]["name"])
    print("Normalized Grand Total:", norm1["grand_total"])
    print("Extracted Items Count:", len(norm1["items"]))
    for i in norm1["items"]:
        print("  - Item:", i["item_name"], "| Sales Ledger:", i["sales_ledger"], "| Amount:", i["amount"], "| HSN:", i["hsn_code"])

    print("\n--- TEST 2: Converting Chirix Item Invoice to Tally Accounting Invoice JSON ---")
    tally_json = transform_invoice_to_tally_json(chirix_item_inv, entry_mode="Accounting Invoice")
    print("Voucher Entry Mode:", tally_json.get("vchentrymode"))
    print("Ledger Entries Count:", len(tally_json.get("ledgerentries", [])))
    for le in tally_json.get("ledgerentries", []):
        print("  - Ledger:", le.get("ledgername"), "| Amount:", le.get("amount"), "| HSN:", le.get("gsthsnname"))

    print("\n--- TEST 3: Converting Chirix Item Invoice to Tally Accounting Invoice XML ---")
    xml_output = convert_invoice_to_xml_message(chirix_item_inv, entry_mode="Accounting Invoice")
    print("XML Snippet:")
    print(xml_output[:400])

if __name__ == "__main__":
    test_conversion()
