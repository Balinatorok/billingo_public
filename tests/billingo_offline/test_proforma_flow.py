"""End-to-end checks of the Díjbekérő / Előlegszámla / Végszámla flow.

Runs the real billingo/api.py and billingo/proforma.py against an in-memory
Frappe and a scripted Billingo API.  Run: python3 tests/test_proforma_flow.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _app_root(start):
    """The directory holding the billingo package, wherever the tests sit."""
    path = start
    while path != os.path.dirname(path):
        for candidate in (path, os.path.join(path, "repo")):
            if os.path.isfile(os.path.join(candidate, "billingo", "api.py")):
                return candidate
        path = os.path.dirname(path)
    raise SystemExit("billingo/api.py not found above " + start)


sys.path.insert(0, _app_root(HERE))

import fakefrappe  # noqa: E402

frappe = fakefrappe.install()
http = fakefrappe.install_requests()

from billingo import api, proforma  # noqa: E402
from billingo import custom_fields  # noqa: E402
from fakefrappe import Resp, ValidationError, TODAY  # noqa: E402

RESULTS = []
SENT = []


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition)))
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  -> {detail}" if detail and not condition else ""))


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except (ValidationError, fakefrappe.PermissionError_) as e:
        return str(e)
    return None


def setup(currency="HUF", taxes=None, template="ÁFA 27% - Belföldi - HK", net_total=100000):
    frappe.reset()
    http.reset()
    SENT.clear()
    TODAY[0] = "2026-10-01"
    frappe.conf.update({"billingo_document_block_id": 100001, "billingo_api_key": "k"})
    frappe.store.put("Company", "Minta Kft", default_currency="HUF")
    frappe.store.put("Billingo Szamlatomb", "Webshop", billingo_block_id=100002)
    frappe.store.put(
        "Sales Order",
        "SAL-ORD-2026-00050",
        creation="2026-09-30 10:00:00",
        docstatus=1,
        customer="Teszt Vevő Kft",
        company="Minta Kft",
        currency=currency,
        conversion_rate=1 if currency == "HUF" else 360.0,
        net_total=net_total,
        transaction_date="2026-09-30",
        delivery_date="2026-10-10",
        payment_schedule=[],
        taxes=taxes if taxes is not None else [{"charge_type": "On Net Total", "rate": 27}],
        taxes_and_charges=template,
        items=[
            {"item_code": "A1", "item_name": "Kábel", "rate": 1000, "qty": 50, "uom": "db"},
            {"item_code": "B2", "item_name": "Adapter", "rate": 2500, "qty": 20, "uom": "db"},
        ],
        custom_billingo_szamlatomb=None,
    )

    # Partner, bank account and e-mail delivery are covered elsewhere; here
    # they only need to answer.
    for module in (api, proforma):
        module._get_or_create_partner = lambda customer: 777
        module._get_bank_account_id = lambda doc: 55
        module._send_document_via_billingo = lambda doc, result, raise_on_error=False: (
            SENT.append((doc.name, result.get("id"))) or {"ok": True}
        )


def so():
    return frappe.store.get("Sales Order", "SAL-ORD-2026-00050")


def make_invoice(name="ACC-SINV-2026-00090", orders=("SAL-ORD-2026-00050",), **extra):
    items = [
        {"item_code": "A1", "item_name": "Kábel", "rate": 1000, "qty": 50, "uom": "db",
         "sales_order": order}
        for order in orders
    ]
    row = frappe.store.put(
        "Sales Invoice",
        name,
        creation="2026-10-01 09:00:00",
        docstatus=0,
        customer="Teszt Vevő Kft",
        company="Minta Kft",
        currency="HUF",
        conversion_rate=1,
        posting_date="2026-10-01",
        due_date="2026-10-09",
        is_return=0,
        payment_schedule=[],
        taxes=[{"charge_type": "On Net Total", "rate": 27}],
        taxes_and_charges="ÁFA 27% - Belföldi - HK",
        items=items,
        custom_billingo_document_id=None,
    )
    row.update(extra)
    return frappe.get_doc("Sales Invoice", name)


def proforma_vendor(mode, description=None, amount=None):
    doc = frappe.get_doc("Sales Order", so()["name"])
    return proforma._order_vendor_id(doc, proforma._proforma_purpose(doc, mode, description, amount))


def no_doc_by_vendor(_):
    return Resp({"error": "not found"}, 404)


def run(name, fn):
    print(f"\n{name}")
    try:
        fn()
    except Exception:
        traceback.print_exc()
        check(f"{name}: no unexpected exception", False)


# ----------------------------------------------------------------------

def t_advance_proforma():
    setup()
    vendor = proforma_vendor("Előleg", "Előleg a rendelésre", 30000)
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9001, "invoice_number": "D-1", "type": "proforma"}))
    out = proforma.create_billingo_proforma("SAL-ORD-2026-00050", "Előleg", "Előleg a rendelésre", 30000)
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    check("proforma POSTed as type proforma", body["type"] == "proforma")
    check("one line, net 30000, 27%", body["items"] == [{
        "name": "Előleg a rendelésre", "unit_price": 30000.0, "unit_price_type": "net",
        "quantity": 1, "unit": "db", "vat": "27%", "comment": ""}], body["items"])
    check("hash vendor_id, not the order name", body["vendor_id"].startswith("erpnext-") and body["vendor_id"] == vendor)
    check("default block when the order has none", body["block_id"] == 100001)
    check("fulfillment = delivery date", body["fulfillment_date"] == "2026-10-10")
    check("payment deadline defaults to today + 8 days, not the order's terms", body["due_date"] == "2026-10-09", body["due_date"])
    check("deadline stored on the order for the banner", so()["custom_billingo_proforma_due_date"] == "2026-10-09")
    check("comment headed with the order", body["comment"].startswith("ERPNext Vevői rendelés SAL-ORD-2026-00050"))
    check("state stored", so()["custom_billingo_proforma_id"] == 9001 and so()["custom_billingo_proforma_mode"] == "Előleg"
          and so()["custom_billingo_advance_vat_rate"] == "27%" and so()["custom_billingo_sync_status"] == "Synced")
    check("e-mailed via Billingo", SENT == [("SAL-ORD-2026-00050", 9001)])
    check("order row locked first", ("Sales Order", "SAL-ORD-2026-00050") in frappe.store.locks)
    check("returns number", out["invoice_number"] == "D-1" and out["send_failed"] is False)

    err = raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Előleg", "x", 10)
    check("second Díjbekérő refused", err and "already has" in err, err)


def t_block_from_order():
    setup()
    so()["custom_billingo_szamlatomb"] = "Webshop"
    http.route("GET", f"/documents/vendor/{proforma_vendor('Teljes tételes')}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 1, "type": "proforma"}))
    proforma.create_billingo_proforma("SAL-ORD-2026-00050", "Teljes tételes")
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    check("block follows the order's Billingo számlatömb", body["block_id"] == 100002)
    check("itemized: every order line", [i["name"] for i in body["items"]] == ["A1 – Kábel", "B2 – Adapter"])


def t_due_date_chosen():
    setup()
    http.route("GET", f"/documents/vendor/{proforma_vendor('Teljes tételes')}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 2, "type": "proforma"}))
    proforma.create_billingo_proforma("SAL-ORD-2026-00050", "Teljes tételes", due_date="2026-10-04")
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    check("chosen payment deadline used", body["due_date"] == "2026-10-04")
    setup()
    err = raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Teljes tételes", due_date="2026-09-01")
    check("deadline in the past refused, nothing sent", err and "past" in err and http.calls == [], err)


def t_bad_inputs():
    setup()
    check("unknown mode", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Valami"))
    check("no description", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Előleg", " ", 10))
    check("zero amount", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Előleg", "x", 0))
    check("more than the order", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Előleg", "x", 100001))
    so()["docstatus"] = 0
    check("draft order refused", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Teljes tételes"))
    so()["docstatus"] = 1
    frappe.permissions_ok = False
    check("needs invoice-create permission", raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Teljes tételes"))
    check("nothing sent to Billingo", http.calls == [], http.calls)


def t_zero_vat_deposit_refused():
    setup(taxes=[], template="ÁFA 0% - EU 89.§ - HK")
    err = raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Előleg", "x", 100)
    check("0% (KBAET) deposit refused", err and "0% VAT" in err, err)
    check("nothing sent", http.calls == [])


def t_lost_response_recovered():
    setup()
    vendor = proforma_vendor("Teljes tételes")
    lookups = []

    def lookup(_):
        lookups.append(1)
        if len(lookups) == 1:
            return Resp({}, 404)
        return Resp({"id": 9100, "type": "proforma", "invoice_number": "D-9"})

    http.route("GET", f"/documents/vendor/{vendor}", lookup)
    http.route("POST", "/documents", lambda body: TimeoutError("read timed out"))
    proforma.create_billingo_proforma("SAL-ORD-2026-00050", "Teljes tételes")
    check("timeout -> found by vendor_id and adopted", so()["custom_billingo_proforma_id"] == 9100)


def t_duplicate_recovered():
    setup()
    vendor = proforma_vendor("Teljes tételes")
    state = {"n": 0}

    def lookup(_):
        state["n"] += 1
        return Resp({}, 404) if state["n"] == 1 else Resp({"id": 9200, "type": "proforma"})

    http.route("GET", f"/documents/vendor/{vendor}", lookup)
    http.route("POST", "/documents", lambda body: Resp({"error": {"message": "Duplicate vendor id."}}, 400))
    proforma.create_billingo_proforma("SAL-ORD-2026-00050", "Teljes tételes")
    check("duplicate vendor id -> adopted", so()["custom_billingo_proforma_id"] == 9200)


def t_wrong_type_under_reference():
    setup()
    vendor = proforma_vendor("Teljes tételes")
    http.route("GET", f"/documents/vendor/{vendor}", lambda _: Resp({"id": 5, "type": "invoice"}))
    err = raises(proforma.create_billingo_proforma, "SAL-ORD-2026-00050", "Teljes tételes")
    check("other document type under the reference -> stop", err and "unexpected" not in (err or "") and "'invoice'" in err, err)
    check("failure recorded on the order", so()["custom_billingo_sync_status"] == "Failed")
    check("no POST", not [c for c in http.calls if c[0] == "POST"])


def _with_advance_proforma(currency="HUF"):
    setup(currency=currency)
    so().update({
        "custom_billingo_proforma_mode": "Előleg",
        "custom_billingo_proforma_id": 9001,
        "custom_billingo_proforma_number": "D-1",
        "custom_billingo_advance_description": "Előleg a rendelésre",
        "custom_billingo_advance_amount": 30000,
        "custom_billingo_advance_vat_rate": "27%",
    })


def t_advance_invoice():
    _with_advance_proforma()
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9300, "invoice_number": "E-1", "type": "advance"}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([{"date": "2026-10-02"}]))
    TODAY[0] = "2026-10-15"
    out = proforma.convert_billingo_proforma("SAL-ORD-2026-00050", "2026-10-02")
    body = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/documents")][0][2]
    check("advance document", body["type"] == "advance" and body["paid"] is True)
    check("dated on the day the money arrived", body["fulfillment_date"] == "2026-10-02")
    check("same line as the Díjbekérő", body["items"][0]["unit_price"] == 30000.0 and body["items"][0]["vat"] == "27%")
    check("HUF rate 1", body["conversion_rate"] == 1)
    check("own vendor_id", body["vendor_id"] == vendor and body["vendor_id"] != proforma_vendor("Előleg", "Előleg a rendelésre", 30000))
    check("stored", so()["custom_billingo_advance_invoice_id"] == 9300)
    pay = http.calls[-1]
    check("Díjbekérő marked paid (not cancelled)", pay[0] == "PUT" and pay[1].endswith("/documents/9001/payments")
          and not [c for c in http.calls if c[1].endswith("/cancel")])
    check("payment: date, gross amount, method", pay[2] == [{"date": "2026-10-02", "price": 38100.0,
          "payment_method": "wire_transfer", "conversion_rate": 1}], pay[2])
    check("13 days late -> warning", out["late"] is True)
    check("second conversion refused", raises(proforma.convert_billingo_proforma, "SAL-ORD-2026-00050", "2026-10-02"))


def t_advance_paid_uses_billingo_gross():
    _with_advance_proforma()
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9303, "type": "advance", "gross_total": 38100}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([]))
    proforma.convert_billingo_proforma("SAL-ORD-2026-00050", "2026-10-01")
    check("paid amount = the advance invoice's own gross total", http.calls[-1][2][0]["price"] == 38100)


def t_advance_eur_rate():
    _with_advance_proforma(currency="EUR")
    frappe.store.put("Currency Exchange", "a", from_currency="EUR", to_currency="HUF", for_selling=1, date="2026-09-30", exchange_rate=366.31)
    frappe.store.put("Currency Exchange", "b", from_currency="EUR", to_currency="HUF", for_selling=1, date="2026-10-01", exchange_rate=368.32)
    frappe.store.put("Currency Exchange", "c", from_currency="EUR", to_currency="HUF", for_selling=1, date="2026-10-03", exchange_rate=370.00)
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9301, "type": "advance"}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([]))
    TODAY[0] = "2026-10-05"
    proforma.convert_billingo_proforma("SAL-ORD-2026-00050", "2026-10-02")
    body = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/documents")][0][2]
    check("EUR: MNB rate of the payment day (latest on/before)", body["conversion_rate"] == 368.32, body["conversion_rate"])

    _with_advance_proforma(currency="EUR")
    err = raises(proforma.convert_billingo_proforma, "SAL-ORD-2026-00050", "2026-09-30")
    check("EUR without stored rate -> stop", err and "exchange rate" in err, err)


def t_advance_recovered_other_date():
    _with_advance_proforma()
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", lambda _: Resp({"id": 9310, "type": "advance", "fulfillment_date": "2026-09-29"}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([]))
    out = proforma.convert_billingo_proforma("SAL-ORD-2026-00050", "2026-09-30")
    check("lost advance found, recorded, no second advance", so()["custom_billingo_advance_invoice_id"] == 9310
          and not [c for c in http.calls if c[0] == "POST" and c[1].endswith("/documents")])
    check("date difference reported", out["date_warning"] and "2026-09-29" in out["date_warning"])


def t_advance_guards():
    _with_advance_proforma()
    check("future payment date refused", raises(proforma.convert_billingo_proforma, "SAL-ORD-2026-00050", "2026-12-01"))
    so()["custom_billingo_advance_vat_rate"] = "5%"
    check("VAT differs from the Díjbekérő -> stop", raises(proforma.convert_billingo_proforma, "SAL-ORD-2026-00050", "2026-10-01"))
    check("nothing issued", not [c for c in http.calls if c[0] == "POST"])


def t_paid_failure_reported():
    _with_advance_proforma()
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9302, "type": "advance"}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp({"error": {"message": "nope"}}, 400))
    out = proforma.convert_billingo_proforma("SAL-ORD-2026-00050", "2026-10-01")
    check("advance kept although marking paid failed", so()["custom_billingo_advance_invoice_id"] == 9302)
    check("failure returned to the UI", bool(out["proforma_paid_error"]))


def t_withdraw():
    _with_advance_proforma()
    http.route("POST", "/documents/9001/cancel", lambda body: Resp({}))
    proforma.withdraw_billingo_proforma("SAL-ORD-2026-00050")
    check("cleared", not so()["custom_billingo_proforma_id"] and not so()["custom_billingo_advance_description"]
          and not so()["custom_billingo_proforma_due_date"])
    check("counted", so()["custom_billingo_proforma_count"] == 1)
    new_vendor = proforma_vendor("Előleg", "Előleg a rendelésre", 30000)
    so()["custom_billingo_proforma_count"] = 0
    old_vendor = proforma_vendor("Előleg", "Előleg a rendelésre", 30000)
    check("next Díjbekérő gets a new reference", new_vendor != old_vendor)
    check("other content -> other reference", proforma_vendor("Előleg", "Előleg a rendelésre", 40000) != old_vendor)

    _with_advance_proforma()
    so()["custom_billingo_advance_invoice_id"] = 1
    check("withdraw refused after advance invoice", raises(proforma.withdraw_billingo_proforma, "SAL-ORD-2026-00050"))

    _with_advance_proforma()
    http.route("POST", "/documents/9001/cancel", lambda body: Resp({"error": {"message": "x"}}, 400))
    check("cancel refused by Billingo -> kept linked", raises(proforma.withdraw_billingo_proforma, "SAL-ORD-2026-00050") and so()["custom_billingo_proforma_id"] == 9001)


# ---------------- Sales Invoice side -----------------------------------

def t_invoice_payload_advance():
    setup()
    inv = make_invoice()
    payload = api._build_billingo_payload(inv, 777, "invoice")
    check("no advance -> no advance_invoice", "advance_invoice" not in payload)

    so()["custom_billingo_advance_invoice_id"] = "9300"
    payload = api._build_billingo_payload(inv, 777, "invoice")
    check("advance referenced, not a negative line", payload.get("advance_invoice") == [9300]
          and all(i["unit_price"] > 0 for i in payload["items"]))

    so()["custom_billingo_advance_consumed_by"] = "ACC-SINV-2026-00001"
    payload = api._build_billingo_payload(inv, 777, "invoice")
    check("already deducted on another invoice -> not again", "advance_invoice" not in payload)

    so()["custom_billingo_advance_consumed_by"] = inv.name
    payload = api._build_billingo_payload(inv, 777, "invoice")
    check("deducted on this invoice -> kept (retry)", payload.get("advance_invoice") == [9300])

    plain = make_invoice(name="ACC-SINV-2026-00091", orders=())
    payload = api._build_billingo_payload(plain, 777, "invoice")
    check("invoice without order unchanged", "advance_invoice" not in payload)


def t_finalize_with_advance_marks_consumed():
    setup()
    so()["custom_billingo_advance_invoice_id"] = "9300"
    inv = make_invoice(custom_billingo_document_id=5000)
    frappe.store.put("Contact", "c")  # unused, email is stubbed below
    api._get_customer_email = lambda customer: "x@y.hu"
    http.route("GET", "/documents/5000", lambda _: Resp({"id": 5000, "vendor_id": "erpnext-abc"}))
    http.route("PUT", "/documents/5000", lambda body: Resp({"id": 5000, "invoice_number": "2026-400", "type": "invoice"}))
    api.finalize_billingo_invoice(inv)
    put = [c for c in http.calls if c[0] == "PUT"][0][2]
    check("final invoice PUT carries the advance", put.get("advance_invoice") == [9300])
    check("advance marked as used by this invoice", so()["custom_billingo_advance_consumed_by"] == inv.name)
    check("order row locked during submit", ("Sales Order", "SAL-ORD-2026-00050") in frappe.store.locks)

    inv2 = make_invoice(name="ACC-SINV-2026-00092", custom_billingo_document_id=5001)
    http.route("GET", "/documents/5001", lambda _: Resp({"id": 5001, "vendor_id": "erpnext-def"}))
    http.route("PUT", "/documents/5001", lambda body: Resp({"id": 5001, "invoice_number": "2026-401", "type": "invoice"}))
    api.finalize_billingo_invoice(inv2)
    put2 = [c for c in http.calls if c[0] == "PUT"][-1][2]
    check("second partial invoice: no second deduction", "advance_invoice" not in put2)


def t_multi_order_ambiguous():
    setup()
    frappe.store.put("Sales Order", "SAL-ORD-2026-00051", custom_billingo_advance_invoice_id="9300")
    api._get_customer_email = lambda customer: "x@y.hu"
    inv = make_invoice(orders=("SAL-ORD-2026-00050", "SAL-ORD-2026-00051"))
    api.sync_billingo_draft(inv)
    row = frappe.store.get("Sales Invoice", inv.name)
    check("draft: nothing sent, reason recorded", http.calls == [] and row["custom_billingo_sync_status"] == "Failed"
          and "several Sales Orders" in row["custom_billingo_error"])
    err = raises(api.finalize_billingo_invoice, inv)
    check("submit refused", err and "several Sales Orders" in err, err)


def t_plain_invoice_unchanged():
    setup()
    api._get_customer_email = lambda customer: "x@y.hu"
    inv = make_invoice(orders=())
    vendor = api._billingo_vendor_id(inv)
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 6000, "type": "draft"}))
    api.sync_billingo_draft(inv)
    check("normal draft still created", frappe.store.get("Sales Invoice", inv.name)["custom_billingo_document_id"] == 6000)


def t_cancel_releases_advance():
    setup()
    so().update({"custom_billingo_advance_invoice_id": "9300", "custom_billingo_advance_consumed_by": "ACC-SINV-2026-00090"})
    inv = make_invoice(custom_billingo_invoice_number="2026-400")
    proforma.release_advance_on_cancel(inv)
    check("cancel frees the deposit for the amended invoice", not so()["custom_billingo_advance_consumed_by"])
    check("reminder to storno in Billingo", "storno" in so()["custom_billingo_error"])
    so()["custom_billingo_advance_consumed_by"] = "ACC-SINV-2026-00001"
    proforma.release_advance_on_cancel(inv)
    check("another invoice's deduction untouched", so()["custom_billingo_advance_consumed_by"] == "ACC-SINV-2026-00001")


def t_concurrent_second_invoice_sees_lock():
    setup()
    so()["custom_billingo_advance_invoice_id"] = "9300"
    inv = make_invoice()
    frappe.store.locks.clear()
    proforma.advance_invoice_for(inv, for_update=True)
    check("at submit: deposit decision under the order lock", ("Sales Order", "SAL-ORD-2026-00050") in frappe.store.locks)
    frappe.store.locks.clear()
    api._build_billingo_payload(inv, 777, "draft")
    check("draft job: no order lock", ("Sales Order", "SAL-ORD-2026-00050") not in frappe.store.locks)
    api._build_billingo_payload(inv, 777, "invoice")
    check("final invoice payload: locked", ("Sales Order", "SAL-ORD-2026-00050") in frappe.store.locks)


def t_order_cancel_blocked():
    setup()
    order = frappe.get_doc("Sales Order", so()["name"])
    check("plain order may be cancelled", raises(proforma.block_order_cancel, order) is None)
    so().update({"custom_billingo_proforma_id": "1", "custom_billingo_proforma_number": "000009"})
    err = raises(proforma.block_order_cancel, frappe.get_doc("Sales Order", so()["name"]))
    check("open Díjbekérő blocks cancel", err and "Withdraw" in err, err)
    so().update({"custom_billingo_advance_invoice_id": "2", "custom_billingo_advance_invoice_number": "E-71"})
    err = raises(proforma.block_order_cancel, frappe.get_doc("Sales Order", so()["name"]))
    check("issued advance invoice blocks cancel", err and "E-71" in err, err)


def _with_full_proforma(paid=True):
    setup()
    so().update({"custom_billingo_proforma_mode": "Teljes tételes", "custom_billingo_proforma_id": "9400",
                 "custom_billingo_proforma_number": "000009", "grand_total": 127000,
                 "advance_paid": 127000 if paid else 0})
    if paid:
        frappe.store.put("Payment Entry", "ACC-PAY-7", docstatus=1, posting_date="2026-09-29")
        frappe.store.put("Payment Entry Reference", "r7", parent="ACC-PAY-7", reference_doctype="Sales Order",
                         reference_name="SAL-ORD-2026-00050", docstatus=1)
    api._get_customer_email = lambda customer: "x@y.hu"


def t_full_mode_has_no_conversion():
    _with_full_proforma()
    err = raises(proforma.convert_billingo_proforma, "SAL-ORD-2026-00050")
    check("Teljes tételes: no conversion button path, points to the invoice", err and "Sales Invoice" in err
          and http.calls == [], err)


def t_prepaid_invoice_settles_proforma():
    _with_full_proforma()
    inv = make_invoice()
    draft = api._build_billingo_payload(inv, 777, "draft")
    check("draft of a prepaid order: paid, no order lock",
          draft["paid"] is True and ("Sales Order", "SAL-ORD-2026-00050") not in frappe.store.locks)

    vendor = api._billingo_vendor_id(inv)
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 7000, "invoice_number": "2026-80", "type": "invoice"}))
    http.route("GET", "/documents/9400", lambda _: Resp({"id": 9400, "type": "proforma", "gross_total": 67170}))
    http.route("PUT", "/documents/9400/payments", lambda body: Resp([]))
    api._get_document_by_vendor_id_with_retry = lambda v, attempts=4, delay=1.5: None
    api.finalize_billingo_invoice(inv)
    issued = [c for c in http.calls if c[0] == "POST" and c[1].endswith("/documents")][0][2]
    check("invoice issued from the ERPNext invoice, as paid", issued["type"] == "invoice" and issued["paid"] is True
          and issued["fulfillment_date"] == "2026-10-01")
    pay = [c for c in http.calls if c[0] == "PUT" and c[1].endswith("/9400/payments")]
    check("Díjbekérő marked paid with its own total on the payment's date",
          pay and pay[0][2] == [{"date": "2026-09-29", "price": 67170.0, "payment_method": "wire_transfer", "conversion_rate": 1}],
          pay)
    check("order records which invoice settled it", so()["custom_billingo_proforma_paid_by"] == inv.name)
    check("no create-from-proforma used", not [c for c in http.calls if "create-from-proforma" in c[1]])

    http.calls.clear()
    inv2 = make_invoice(name="ACC-SINV-2026-00095")
    http.route("GET", f"/documents/vendor/{api._billingo_vendor_id(inv2)}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 7001, "invoice_number": "2026-81", "type": "invoice"}))
    api.finalize_billingo_invoice(inv2)
    issued2 = [c for c in http.calls if c[0] == "POST"][0][2]
    check("partial delivery: second invoice also paid, Díjbekérő not paid twice",
          issued2["paid"] is True and not [c for c in http.calls if c[1].endswith("/payments")])

    proforma.release_advance_on_cancel(inv)
    check("cancelling the settling invoice frees the mark", not so()["custom_billingo_proforma_paid_by"])

    err = raises(proforma.withdraw_billingo_proforma, "SAL-ORD-2026-00050")
    so()["custom_billingo_proforma_paid_by"] = inv.name
    err = raises(proforma.withdraw_billingo_proforma, "SAL-ORD-2026-00050")
    check("paid Díjbekérő cannot be withdrawn", err and "paid" in err, err)


def t_prepaid_settle_failure_does_not_block():
    _with_full_proforma()
    inv = make_invoice()
    http.route("GET", f"/documents/vendor/{api._billingo_vendor_id(inv)}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 7002, "invoice_number": "2026-82", "type": "invoice"}))
    http.route("GET", "/documents/9400", lambda _: Resp({"gross_total": 67170}))
    http.route("PUT", "/documents/9400/payments", lambda body: Resp({"error": "x"}, 400))
    api._get_document_by_vendor_id_with_retry = lambda v, attempts=4, delay=1.5: None
    api.finalize_billingo_invoice(inv)
    row = frappe.store.get("Sales Invoice", inv.name)
    check("invoice still synced, user told to mark by hand, Díjbekérő no longer withdrawable",
          row["custom_billingo_document_id"] == 7002 and frappe.messages and "by hand" in frappe.messages[-1][0]
          and so().get("custom_billingo_proforma_paid_by") == inv.name)


def t_unpaid_order_not_marked_paid():
    _with_full_proforma(paid=False)
    inv = make_invoice(outstanding_amount=63500, grand_total=63500)
    http.route("GET", f"/documents/vendor/{api._billingo_vendor_id(inv)}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 7003, "invoice_number": "2026-83", "type": "invoice"}))
    api._get_document_by_vendor_id_with_retry = lambda v, attempts=4, delay=1.5: None
    api.finalize_billingo_invoice(inv)
    issued = [c for c in http.calls if c[0] == "POST"][0][2]
    check("no payment recorded -> invoice not 'paid', Díjbekérő untouched",
          issued["paid"] is False and not [c for c in http.calls if c[1].endswith("/payments")]
          and not so().get("custom_billingo_proforma_paid_by"))

    _with_full_proforma(paid=False)
    inv = make_invoice(outstanding_amount=0, grand_total=63500)
    check("advances allocated on the invoice (nothing outstanding) -> paid",
          api._build_billingo_payload(inv, 777, "invoice")["paid"] is True)


def t_payment_entry_issues_advance():
    _with_advance_proforma()
    frappe.store.put("Payment Entry", "ACC-PAY-1", payment_type="Receive", party_type="Customer", posting_date="2026-10-01",
                     references=[{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2026-00050", "allocated_amount": 38100}])
    pe = frappe.get_doc("Payment Entry", "ACC-PAY-1")
    proforma.queue_advance_from_payment(pe)
    check("payment queues the advance invoice after commit", len(frappe.enqueued) == 1
          and frappe.enqueued[0][1]["enqueue_after_commit"] and frappe.enqueued[0][1]["amount"] == 38100.0)

    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9350, "invoice_number": "E-5", "type": "advance", "gross_total": 38100}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([]))
    kw = frappe.enqueued[0][1]
    proforma.issue_advance_from_payment(kw["sales_order"], kw["payment_entry"], kw["amount"], kw["payment_date"])
    body = [c for c in http.calls if c[0] == "POST"][0][2]
    check("advance = received gross amount, dated on the payment",
          body["items"][0]["unit_price"] == 38100.0 and body["items"][0]["unit_price_type"] == "gross"
          and body["fulfillment_date"] == "2026-10-01" and body["type"] == "advance")
    check("stored with its payment", so()["custom_billingo_advance_invoice_id"] == 9350
          and so()["custom_billingo_advance_payment_entry"] == "ACC-PAY-1")

    frappe.enqueued.clear()
    proforma.queue_advance_from_payment(pe)
    check("no second advance for a second payment", frappe.enqueued == [])

    _with_advance_proforma()
    so()["custom_billingo_proforma_mode"] = "Teljes tételes"
    frappe.store.put("Payment Entry", "ACC-PAY-2", payment_type="Receive", party_type="Customer", posting_date="2026-10-01",
                     references=[{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2026-00050", "allocated_amount": 1}])
    proforma.queue_advance_from_payment(frappe.get_doc("Payment Entry", "ACC-PAY-2"))
    check("Teljes tételes order: payment issues nothing", frappe.enqueued == [])


def t_payment_entry_edge_cases():
    _with_advance_proforma()
    frappe.store.put("Payment Entry", "ACC-PAY-3", payment_type="Receive", party_type="Customer",
                     posting_date="2026-10-01", paid_from_account_currency="EUR",
                     references=[{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2026-00050", "allocated_amount": 100}])
    proforma.queue_advance_from_payment(frappe.get_doc("Payment Entry", "ACC-PAY-3"))
    check("other currency than the order -> not automatic, shown on the order",
          frappe.enqueued == [] and so()["custom_billingo_sync_status"] == "Failed" and "EUR" in so()["custom_billingo_error"])

    _with_advance_proforma()
    so().update({"custom_billingo_advance_invoice_id": "9350", "custom_billingo_advance_invoice_number": "E-5",
                 "custom_billingo_advance_payment_entry": "ACC-PAY-1"})
    frappe.store.put("Payment Entry", "ACC-PAY-4", payment_type="Receive", party_type="Customer", posting_date="2026-10-01",
                     references=[{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2026-00050", "allocated_amount": 5}])
    proforma.queue_advance_from_payment(frappe.get_doc("Payment Entry", "ACC-PAY-4"))
    check("second payment -> visible warning, nothing issued", frappe.enqueued == [] and "ACC-PAY-4" in so()["custom_billingo_error"])

    frappe.store.put("Payment Entry", "ACC-PAY-1", payment_type="Receive", party_type="Customer",
                     references=[{"reference_doctype": "Sales Order", "reference_name": "SAL-ORD-2026-00050", "allocated_amount": 5}])
    proforma.flag_cancelled_advance_payment(frappe.get_doc("Payment Entry", "ACC-PAY-1"))
    inv = make_invoice()
    check("cancelled payment: advance no longer deducted, storno reminder",
          proforma.advance_invoice_for(inv) is None and "storno" in so()["custom_billingo_error"])


def t_payment_entry_job_ignores_user_permission():
    _with_advance_proforma()
    frappe.permissions_ok = False
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"id": 9351, "type": "advance"}))
    http.route("PUT", "/documents/9001/payments", lambda body: Resp([]))
    proforma.issue_advance_from_payment("SAL-ORD-2026-00050", "ACC-PAY-1", 1000, "2026-10-01")
    check("job issues the advance even if the payment's user cannot edit orders",
          so()["custom_billingo_advance_invoice_id"] == 9351)


def t_payment_entry_failure_recorded():
    _with_advance_proforma()
    vendor = proforma._order_vendor_id(frappe.get_doc("Sales Order", so()["name"]), "advance")
    http.route("GET", f"/documents/vendor/{vendor}", no_doc_by_vendor)
    http.route("POST", "/documents", lambda body: Resp({"error": {"message": "Validation Failed"}}, 400))
    proforma.issue_advance_from_payment("SAL-ORD-2026-00050", "ACC-PAY-9", 1000, "2026-10-01")
    check("job failure shown on the order with the manual fallback",
          so()["custom_billingo_sync_status"] == "Failed" and "ACC-PAY-9" in so()["custom_billingo_error"]
          and not so().get("custom_billingo_advance_invoice_id"))


def t_custom_fields():
    import sys as _s
    mod = _s.modules["frappe.custom.doctype.custom_field.custom_field"]
    custom_fields.create_billingo_custom_fields()
    fields = mod.created[-1]["Sales Order"]
    names = [f["fieldname"] for f in fields]
    needed = (set(proforma.ORDER_STATE_FIELDS) - {"advance_paid", "grand_total"}) | {
        "custom_billingo_proforma_number", "custom_billingo_proforma_count",
        "custom_billingo_advance_description", "custom_billingo_advance_amount",
        "custom_billingo_advance_vat_rate", "custom_billingo_sync_status", "custom_billingo_error",
        "custom_billingo_proforma_due_date", "custom_billingo_advance_payment_entry"}
    check("every field the code writes is created", needed <= set(names), needed - set(names))
    check("all data fields no_copy (not copied onto the invoice)", all(f.get("no_copy") for f in fields if f["fieldtype"] not in ("Section Break", "Column Break")))
    check("insert_after chain valid", all(f["insert_after"] in names + ["tc_name"] for f in fields))


for name, fn in list(globals().items()):
    if name.startswith("t_") and callable(fn):
        run(name, fn)

failed = [label for label, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
