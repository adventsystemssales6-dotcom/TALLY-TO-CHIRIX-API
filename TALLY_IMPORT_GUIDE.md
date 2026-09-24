# TallyPrime JSON Data Integration Guide: Chirix ERP Sales Import

This document provides complete operational, architectural, and troubleshooting documentation for converting **Chirix ERP** raw sales JSON into **TallyPrime** JSON format for native voucher import.

---

## 1. Data Transformation Matrix

| Chirix Field | Tally JSON Field | Format / Transformation | Description |
| :--- | :--- | :--- | :--- |
| `invoice_no` | `vouchernumber` | String (e.g. `"25"`) | Unique sales voucher number |
| `date` | `date`, `vchstatusdate`, `effectivedate` | `YYYYMMDD` (e.g. `"20260401"`) | Converted from ISO (`YYYY-MM-DD`) or `DD/MM/YYYY` |
| `customer.name` | `partyledgername`, `partyname`, `basicbuyername` | String | Must match Tally Party Ledger master |
| `customer.gstin` | `partygstin`, `consigneegstin` | String (15 chars) | Customer GST Identification Number |
| `customer.state` | `statename`, `placeofsupply` | String | State for Place of Supply & Tax calculation |
| `customer.pincode` | `partypincode`, `consigneepincode` | String | Postal Code |
| `customer.address` | `address`, `basicbuyeraddress` | Array of Strings | Metadata line followed by address strings |
| `dispatch_from.name` | `dispatchfromname` | String | Seller Company Name |
| `dispatch_from.gstin` | `cmpgstin` | String | Seller GSTIN |
| `irn` | `irn` | String (64 chars) | E-Invoice IRN Hash |
| `items[].item_name` | `allinventoryentries[].stockitemname` | String | Stock Item Name (Must exist in Tally) |
| `items[].quantity` | `actualqty`, `billedqty` | `" Qty Unit"` (e.g. `" 140 Nos"`) | Formatted with leading space & Unit symbol |
| `items[].rate` | `rate` | `"Rate/Unit"` (e.g. `"25000.00/Nos"`) | Rate per unit |
| `items[].amount` | `amount` | String float (e.g. `"3500000.00"`) | Line item total amount |
| `items[].batches` | `batchallocations[]` | Array of Batch Objects | Godown name, batch name, quantity, amount |
| `grand_total` | Party Ledger Debit Amount | Float | Total Voucher Debit Amount |

---

## 2. JSON Structure Hierarchy

TallyPrime JSON follows a standard top-down object structure:

```
{
    "tallymessage": [
        {
            "metadata": { "type": "Voucher", "vchtype": "Sales", "action": "Create", ... },
            "date": "YYYYMMDD",
            "vouchernumber": "INVOICE_NO",
            "partyledgername": "PARTY_NAME",
            "allinventoryentries": [
                {
                    "stockitemname": "ITEM_NAME",
                    "rate": "AMOUNT/UNIT",
                    "amount": "TOTAL_AMOUNT",
                    "batchallocations": [ ... ],
                    "accountingallocations": [ { "ledgername": "Sales", "amount": "ITEM_AMOUNT" } ],
                    "ratedetails": [ ... ]
                }
            ],
            "ewaybilldetails": [ ... ]
        }
    ]
}
```

---

## 3. Running the Converter

The Python converter `chirix_to_tally.py` converts single or bulk Chirix invoice records, validates accounting balances, and generates `tally-import.json`.

### Syntax
```bash
python chirix_to_tally.py <input_chirix_json_file> <output_tally_json_file>
```

### Example
```bash
python chirix_to_tally.py chirix_input_sample.json tally-import.json
```

---

## 4. Accounting Validation Rules

TallyPrime strictly enforces double-entry bookkeeping rules during voucher import:

1. **Debit = Credit Balance Constraint**:
   $$\text{Total Debit (Party Ledger)} = \sum \text{Item Amounts} + \text{CGST} + \text{SGST} + \text{IGST} + \text{Other Duty Ledgers}$$
   - If $| \text{Debit} - \text{Credit} | > 0.05$, the converter **rejects** the voucher and logs an imbalance error.

2. **Date Format Enforcement**:
   - Dates must be formatted as `YYYYMMDD`. Dates like `2026-04-01` will cause import errors.

3. **String Numeric Representations**:
   - Amounts and quantities inside Tally JSON are string representations (e.g. `"3500000.00"`, `" 140 Nos"`).

---

## 5. Common Tally Import Errors & Fixes

| Error | Root Cause | Fix / Solution |
| :--- | :--- | :--- |
| `Line 1: Ledger 'XYZ' does not exist` | Party or Sales ledger name in JSON does not match Tally Master | Ensure master is created in Tally under `Sundry Debtors` or `Sales Accounts` with identical spelling and casing. |
| `Voucher total mismatch / Unbalanced Voucher` | Total Debit does not equal Credit | Check calculation in raw JSON. Verify tax amounts and item totals add up to `grand_total`. |
| `Invalid Date Format` | Date passed as `YYYY-MM-DD` or `DD-MM-YYYY` | Ensure `chirix_to_tally.py` parses and outputs date as `YYYYMMDD`. |
| `Duplicate GUID / RemoteID` | Re-using an existing `guid` or `remoteid` when importing | Generate a fresh `uuid4()` for new vouchers. |
| `Stock Item 'ABC' not found` | Inventory item name in `stockitemname` does not exist in Tally | Create the Stock Item master under `Gateway of Tally -> Create -> Stock Item`. |
| `Godown / Batch Allocation Error` | Godown or batch name specified in `batchallocations` is missing | Ensure Godowns (e.g., `Sathy`, `Saravanampatti`) and `Primary Batch` exist or enable multi-godown/batch features in Tally F11 settings. |

---

## 6. HTTP / REST API Integration with Tally

To send converted vouchers directly into a running TallyPrime instance listening on port `9000`:

```python
import requests

def send_to_tally(tally_json_path, tally_host="http://localhost:9000"):
    with open(tally_json_path, 'r', encoding='utf-8') as f:
        json_payload = f.read()
        
    headers = {'Content-Type': 'application/json'}
    response = requests.post(tally_host, data=json_payload, headers=headers)
    print("Tally Response Status:", response.status_code)
    print("Tally Response Body:", response.text)
```
