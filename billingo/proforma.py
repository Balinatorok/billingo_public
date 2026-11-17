"""Díjbekérő (proforma) and Előlegszámla (advance invoice) from a Sales Order.

Flow
  Sales Order (submitted) -> "Díjbekérő létrehozása" button
    mode "Előleg" (deposit): one free-text line, an amount typed by the user
      -> "Előlegszámlává alakítás" button once the money is in: a separate
         `advance` document dated on the payment day; the Díjbekérő is marked paid
      -> the Sales Invoice made from the order later carries
         `advance_invoice: [id]`, so Billingo deducts the deposit itself and
         reports it to NAV as an advance deduction
    mode "Teljes tételes" (full prepayment): every order line
      -> no extra step: the ordinary Sales Invoice made from the order issues
         the Billingo invoice on submit (marked paid), and the Díjbekérő is
         marked paid at the same time. Partial deliveries work as usual.

Raising a Díjbekérő and turning a deposit into an advance invoice are clicks,
never document events: they are business decisions with tax consequences.

The Sales Invoice side lives in api.py and calls into this module through
`invoice_draft_blocked`, `check_invoice_orders`, `advance_invoice_for`,
`prepaid_proforma_for`, `mark_advance_consumed` and `settle_prepaid_proforma`.
They are imported lazily there, so this module may import api.py at the top
without a cycle.
"""

import hashlib
import math

import frappe
import requests
from frappe.utils import add_days, cint, flt, getdate, nowdate

from billingo.api import (
    BILLINGO_BASE_URL,
    _build_comment,
    _create_billingo_document,
    _get_bank_account_id,
    _get_document,
    _get_document_block_id,
    _get_document_by_vendor_id,
    _get_error_text,
    _get_headers,
    _get_invoice_language,
    _get_or_create_partner,
    _get_vat_treatment,
    _map_items,
    _map_payment_method,
    _send_document_via_billingo,
)

PROFORMA_MODE_ADVANCE = "Előleg"
PROFORMA_MODE_FULL = "Teljes tételes"

# Default payment deadline of a Díjbekérő. It asks for money before the goods
# leave, so the customer's usual invoice terms (often 30 days) do not apply;
# the dialog offers this and the user can change it.
PROFORMA_DEFAULT_DUE_DAYS = 8

# Áfa tv. 163. § gives 15 days from receipt of an advance to issue its
# invoice. Warn before that runs out rather than after.
ADVANCE_INVOICE_WARNING_DAYS = 10

# Billingo state this module keeps on the Sales Order (see custom_fields.py).
ORDER_STATE_FIELDS = (
    "custom_billingo_proforma_id",
    "custom_billingo_proforma_mode",
    "custom_billingo_advance_invoice_id",
    "custom_billingo_advance_invoice_number",
    "custom_billingo_advance_consumed_by",
    "custom_billingo_proforma_number",
    "custom_billingo_proforma_paid_by",
    "advance_paid",
    "grand_total",
    "base_grand_total",
    "currency",
    "party_account_currency",
)

# Rounding slack when comparing paid and invoiced amounts.
PAID_TOLERANCE = 1.0


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

def _order_vendor_id(sales_order, purpose):
    """Immutable Billingo reference for one document raised from an order.

    Same scheme as api._billingo_vendor_id - a hash of doctype, name and
    creation, so a reused Sales Order name can never collide with an older
    order's documents - plus the purpose, because one order can raise both a
    Díjbekérő and an advance invoice and Billingo keeps every vendor_id
    reserved, even after the document is deleted.
    """
    identity = (
        f"{sales_order.doctype}:{sales_order.name}:{sales_order.get('creation') or ''}:{purpose}"
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"erpnext-{digest}"


def _order_comment(sales_order):
    """api._build_comment, headed with the order instead of "ERPNext Számla"."""
    comment = _build_comment(sales_order)
    first_line, _, rest = comment.partition("\n")
    is_hungarian = _get_invoice_language(sales_order) == "hu"
    heading = (
        f"ERPNext Vevői rendelés {sales_order.name}"
        if is_hungarian
        else f"ERPNext Sales Order {sales_order.name}"
    )
    return heading + ("\n" + rest if rest else "")


def _conversion_rate_on(sales_order, on_date):
    """The HUF rate for a tax document dated `on_date`.

    The advance invoice is a tax document whose tax point is the day the money
    arrived, so it needs that day's rate, not the one the order was booked at.
    The site stores the MNB rate daily in Currency Exchange; take the latest
    one published on or before the date.
    """
    company_currency = frappe.get_cached_value("Company", sales_order.company, "default_currency")
    currency = sales_order.currency or company_currency
    if not currency or currency == company_currency:
        return 1

    rows = frappe.get_all(
        "Currency Exchange",
        filters={
            "from_currency": currency,
            "to_currency": company_currency,
            "for_selling": 1,
            "date": ["<=", str(on_date)],
        },
        fields=["exchange_rate", "date"],
        order_by="date desc",
        limit=1,
    )
    if not rows or not flt(rows[0].get("exchange_rate")):
        frappe.throw(
            f"No {currency}/{company_currency} exchange rate is stored for {on_date} or earlier. "
            "Add the MNB rate of that day in Currency Exchange and try again.",
            title="Billingo: exchange rate missing",
        )
    return flt(rows[0]["exchange_rate"])


def _build_document_payload(
    sales_order,
    partner_id,
    doc_type,
    items,
    fulfillment_date,
    due_date,
    vendor_id,
    conversion_rate=None,
):
    """A /documents payload for a Sales Order, built from api.py's own helpers.

    Language, bank account, payment method and the document block are resolved
    exactly as for a Sales Invoice. The block follows the order's
    "Billingo számlatömb" field, which ERPNext copies onto the invoice made
    from the order, so the Díjbekérő, the advance invoice and the final invoice
    come out of the same block.
    """
    payload = {
        "vendor_id": vendor_id,
        "partner_id": partner_id,
        "block_id": _get_document_block_id(sales_order),
        "type": doc_type,
        "fulfillment_date": str(fulfillment_date),
        "due_date": str(due_date or fulfillment_date),
        "payment_method": _map_payment_method(sales_order),
        "language": _get_invoice_language(sales_order),
        "currency": sales_order.currency or "HUF",
        "conversion_rate": conversion_rate or sales_order.conversion_rate or 1,
        "electronic": True,
        "paid": False,
        "items": items,
        "comment": _order_comment(sales_order),
    }

    bank_account_id = _get_bank_account_id(sales_order)
    if bank_account_id:
        payload["bank_account_id"] = bank_account_id

    return payload


def _build_advance_item(description, amount, vat_treatment):
    """The one free-text line a deposit Díjbekérő and its advance invoice share."""
    item = {
        "name": description,
        "unit_price": amount,
        "unit_price_type": "net",
        "quantity": 1,
        "unit": "db",
        "vat": vat_treatment["vat"],
        "comment": "",
    }
    if vat_treatment.get("entitlement"):
        item["entitlement"] = vat_treatment["entitlement"]
    return item


def _record_order_error(sales_order_name, error_detail):
    """Persist a failure on the Sales Order before the caller aborts.

    These are whitelisted button actions, not document hooks: the throw that
    follows rolls the request back, and the order would otherwise show no
    trace of a failure that may have left a real document in Billingo. The
    commit is deliberate and nothing else is pending in this request.
    """
    detail = (error_detail or "")[:500]
    frappe.db.set_value(
        "Sales Order",
        sales_order_name,
        {"custom_billingo_sync_status": "Failed", "custom_billingo_error": detail},
        update_modified=False,
    )
    frappe.db.commit()
    frappe.log_error(title="Billingo Proforma Error", message=error_detail)


def _record_order_state(sales_order_name, values):
    """Store what Billingo returned and make it durable at once.

    By now a real document exists in Billingo. Committing before the follow-up
    steps (marking paid, e-mail) keeps its ID even if one of those steps fails.
    """
    values = dict(values)
    values.setdefault("custom_billingo_sync_status", "Synced")
    values.setdefault("custom_billingo_error", "")
    frappe.db.set_value("Sales Order", sales_order_name, values, update_modified=False)
    frappe.db.commit()


def _proforma_due_date(due_date):
    """The Díjbekérő's payment deadline: the one chosen in the dialog, else today + 8 days."""
    today = getdate(nowdate())
    if not due_date:
        return add_days(today, PROFORMA_DEFAULT_DUE_DAYS)
    due_date = getdate(due_date)
    if due_date < today:
        frappe.throw("The Díjbekérő's payment deadline cannot be in the past.")
    return due_date


def _check_billingo_issue_permission(sales_order):
    """Issuing a Billingo document creates a real, NAV-reported paper.

    Write access to a Sales Order is a weaker bar than that, so also require
    the right to create invoices.
    """
    sales_order.check_permission("write")
    if not frappe.has_permission("Sales Invoice", "create"):
        frappe.throw(
            "You need permission to create Sales Invoices to issue Billingo documents.",
            frappe.PermissionError,
        )


def _load_locked_sales_order(sales_order_name, check_permission=True):
    """Lock the order row, then load it, so two clicks cannot both pass the checks.

    The lock is taken first: a document loaded before it could carry state
    that a concurrent click has changed in the meantime.
    """
    frappe.db.get_value("Sales Order", sales_order_name, "name", for_update=True)
    doc = frappe.get_doc("Sales Order", sales_order_name)
    if check_permission:
        _check_billingo_issue_permission(doc)
    if doc.docstatus != 1:
        frappe.throw("Only a submitted Sales Order can be sent to Billingo.")
    return doc


def _recover_order_document(sales_order, vendor_id, expected_type):
    """Return a document Billingo already holds under this vendor_id.

    A lost response - a timeout, a dropped connection - leaves a real document
    behind a reference that can never be reused. Looking before creating, and
    again after a duplicate error, keeps the order from becoming stuck.
    """
    try:
        existing = _get_document_by_vendor_id(vendor_id)
    except Exception:
        frappe.log_error(
            title="Billingo Proforma Lookup Failed",
            message=f"Could not check Billingo for vendor_id '{vendor_id}':\n{frappe.get_traceback()}",
        )
        return None

    if not existing:
        return None
    if (existing.get("type") or "").casefold() != expected_type:
        message = (
            f"Billingo already holds a '{existing.get('type')}' document "
            f"(id={existing.get('id')}) under the reference '{vendor_id}' of Sales Order "
            f"{sales_order.name}. Check it in Billingo before continuing; nothing was recorded "
            "on the Sales Order."
        )
        _record_order_error(sales_order.name, message)
        frappe.throw(message, title="Billingo: unexpected existing document")
    return existing


def _create_or_recover(sales_order, payload, expected_type, what):
    """POST the document, or adopt the one an earlier lost attempt created."""
    vendor_id = payload["vendor_id"]
    result = _recover_order_document(sales_order, vendor_id, expected_type)
    if result:
        return result

    try:
        return _create_billingo_document(payload)
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        if "duplicate vendor id" in (error_detail or "").casefold():
            result = _recover_order_document(sales_order, vendor_id, expected_type)
            if result:
                return result
        _record_order_error(sales_order.name, error_detail)
        frappe.throw(f"Billingo rejected the {what}: {error_detail}")
    except Exception:
        # A timeout or a dropped connection may still have created the
        # document. Ask before reporting a failure the user would retry.
        result = _recover_order_document(sales_order, vendor_id, expected_type)
        if result:
            return result
        _record_order_error(sales_order.name, frappe.get_traceback())
        frappe.throw(
            f"The {what} could not be created in Billingo. Check the Error Log, and check "
            "Billingo before trying again.",
            title="Billingo: request failed",
        )


# ----------------------------------------------------------------------
# Buttons
# ----------------------------------------------------------------------

@frappe.whitelist()
def create_billingo_proforma(
    sales_order: str,
    mode: str,
    description: str | None = None,
    amount: float | None = None,
    due_date: str | None = None,
) -> dict:
    """Raise a Díjbekérő in Billingo for a submitted Sales Order.

    The VAT rate is always the order's own and deliberately not an input: it
    is carried onto the advance invoice, which is a tax document, and a typo
    there would be replayed later with nobody watching.
    """
    if mode not in (PROFORMA_MODE_ADVANCE, PROFORMA_MODE_FULL):
        frappe.throw(f"Unknown Díjbekérő mode: {mode}")

    doc = _load_locked_sales_order(sales_order)
    if doc.get("custom_billingo_proforma_id"):
        frappe.throw(
            f"Sales Order {doc.name} already has a Billingo Díjbekérő "
            f"({doc.get('custom_billingo_proforma_number') or doc.custom_billingo_proforma_id}). "
            "Withdraw it first (Billingo > Díjbekérő visszavonása) to raise another.",
            title="Billingo: a Díjbekérő already exists",
        )

    vat_treatment = _get_vat_treatment(doc)

    if mode == PROFORMA_MODE_ADVANCE:
        zero_vat_reason = vat_treatment.get("entitlement") or vat_treatment.get("note")
        if zero_vat_reason:
            # 0% sales (EU, export, TAM, 37.§ reverse charge): whether and how a deposit needs an
            # advance invoice depends on the transaction, and this flow would
            # issue one automatically. Keep those to a human until the
            # accountant has settled the rule.
            frappe.throw(
                "A deposit (Előleg) Díjbekérő is only supported for orders with a VAT rate. "
                f"This order is 0% VAT ({zero_vat_reason}): use 'Teljes tételes', "
                "or handle the deposit in Billingo by hand after checking with the accountant.",
                title="Billingo: deposit not supported for 0% VAT orders",
            )
        description = (description or "").strip()
        amount = flt(amount)
        if not description:
            frappe.throw("A deposit Díjbekérő needs a description.")
        if amount <= 0:
            frappe.throw("A deposit Díjbekérő needs a positive amount.")
        if amount > flt(doc.net_total) + 0.005:
            frappe.throw(
                f"The deposit ({amount}) is more than the order's net total ({doc.net_total})."
            )
        items = [_build_advance_item(description, amount, vat_treatment)]
    else:
        items = _map_items(doc)

    due_date = _proforma_due_date(due_date)
    partner_id = _get_or_create_partner(doc.customer)
    payload = _build_document_payload(
        doc,
        partner_id,
        "proforma",
        items,
        # The expected delivery, not the order date: if Billingo carries this
        # onto the invoice it makes from the Díjbekérő, the order date would
        # certainly be wrong there.
        fulfillment_date=doc.get("delivery_date") or doc.transaction_date,
        due_date=due_date,
        vendor_id=_order_vendor_id(doc, _proforma_purpose(doc, mode, description, amount)),
    )
    result = _create_or_recover(doc, payload, "proforma", "Díjbekérő")

    values = {
        "custom_billingo_proforma_mode": mode,
        "custom_billingo_proforma_id": result.get("id"),
        "custom_billingo_proforma_number": result.get("invoice_number"),
        "custom_billingo_proforma_due_date": str(result.get("due_date") or due_date)[:10],
    }
    if mode == PROFORMA_MODE_ADVANCE:
        values.update(
            {
                "custom_billingo_advance_description": description,
                "custom_billingo_advance_amount": amount,
                "custom_billingo_advance_vat_rate": vat_treatment["vat"],
            }
        )
    _record_order_state(doc.name, values)

    sent = _send_document_via_billingo(doc, result)
    return {
        "mode": mode,
        "document_id": result.get("id"),
        "invoice_number": result.get("invoice_number"),
        "send_failed": not sent,
    }


@frappe.whitelist()
def convert_billingo_proforma(sales_order: str, payment_date: str | None = None) -> dict:
    """Turn a paid Díjbekérő into the document its mode calls for."""
    doc = _load_locked_sales_order(sales_order)

    proforma_id = doc.get("custom_billingo_proforma_id")
    if not proforma_id:
        frappe.throw(f"Sales Order {doc.name} has no Billingo Díjbekérő to convert.")

    mode = doc.get("custom_billingo_proforma_mode")
    if mode == PROFORMA_MODE_FULL:
        frappe.throw(
            "A 'Teljes tételes' Díjbekérő needs no conversion: once paid, make the Sales Invoice "
            "from this order and submit it. That issues the Billingo invoice and marks the "
            "Díjbekérő paid."
        )

    if mode == PROFORMA_MODE_ADVANCE:
        if doc.get("custom_billingo_advance_invoice_id"):
            frappe.throw(
                "This Díjbekérő already has an advance invoice "
                f"({doc.get('custom_billingo_advance_invoice_number') or doc.custom_billingo_advance_invoice_id})."
            )
        return _issue_advance_invoice(doc, proforma_id, payment_date)

    frappe.throw(f"Sales Order {doc.name} has no usable Billingo Díjbekérő mode.")


@frappe.whitelist()
def withdraw_billingo_proforma(sales_order: str) -> dict:
    """Cancel an unpaid Díjbekérő in Billingo and free the order for a new one.

    For a request that was wrong (amount, items) or that the customer will not
    pay. Refused once the Díjbekérő has produced an advance invoice or an
    invoice: from then on the corrections belong to those tax documents.
    """
    doc = _load_locked_sales_order(sales_order)
    proforma_id = doc.get("custom_billingo_proforma_id")
    if not proforma_id:
        frappe.throw(f"Sales Order {doc.name} has no Billingo Díjbekérő.")
    if doc.get("custom_billingo_advance_invoice_id") or doc.get("custom_billingo_proforma_paid_by"):
        frappe.throw(
            "This Díjbekérő has already been paid (advance invoice or invoice issued), so it "
            "cannot be withdrawn here. Correct those documents in Billingo instead."
        )

    # Billingo refuses /cancel on a Díjbekérő ("Cannot cancel the document",
    # staging 2026-10-02). A Díjbekérő is not a tax document, so deleting it is
    # the clean way out; if Billingo refuses that too, the order is released
    # anyway and the user is told to close the Díjbekérő in Billingo by hand.
    left_in_billingo = None
    cancel_error = _cancel_billingo_document(proforma_id)
    if cancel_error:
        delete_error = _delete_billingo_draft(proforma_id)
        if delete_error:
            left_in_billingo = cancel_error

    # Billingo keeps a vendor_id reserved for good, so the next Díjbekérő of
    # this order needs a reference of its own: count the withdrawn ones.
    _record_order_state(
        doc.name,
        {
            "custom_billingo_proforma_count": cint(doc.get("custom_billingo_proforma_count")) + 1,
            "custom_billingo_proforma_mode": None,
            "custom_billingo_proforma_id": None,
            "custom_billingo_proforma_number": None,
            "custom_billingo_proforma_due_date": None,
            "custom_billingo_advance_description": None,
            "custom_billingo_advance_amount": 0,
            "custom_billingo_advance_vat_rate": None,
            "custom_billingo_sync_status": None,
        },
    )
    return {
        "withdrawn": proforma_id,
        "proforma_number": doc.get("custom_billingo_proforma_number"),
        "left_in_billingo": bool(left_in_billingo),
    }


def _proforma_purpose(sales_order, mode, description=None, amount=None):
    """Reference purpose for a Díjbekérő: attempt number plus its content.

    A retry after a lost response must find the document the first attempt
    created - but only if it asks for the same thing. Were the content left
    out, a retry with another amount or mode would silently adopt the first
    attempt's document and store the new figures next to it.
    """
    count = cint(sales_order.get("custom_billingo_proforma_count"))
    if mode == PROFORMA_MODE_ADVANCE:
        content = f"{mode}|{description}|{flt(amount):.2f}"
    else:
        content = f"{mode}|{flt(sales_order.get('net_total')):.2f}"
    return f"proforma-{count}-{hashlib.sha256(content.encode()).hexdigest()[:12]}"


def _issue_advance_invoice(
    sales_order, proforma_id, payment_date, received_gross=None, payment_entry=None
):
    """Raise the advance invoice for a deposit that has been paid.

    received_gross: the amount actually received (from the Payment Entry). The
    advance invoice must cover what arrived, which may differ from what the
    Díjbekérő asked for; without it, the Díjbekérő's own line is used.

    fulfillment_date is the day the money arrived, not today: under Áfa tv.
    59. § that receipt is the tax point, and the button may be pressed days
    later. The VAT rate is the one stored with the Díjbekérő; the order is
    submitted, so recomputing it must give the same answer - if it does not,
    stop rather than issue a tax document that disagrees with its request.
    """
    payment_date = getdate(payment_date or nowdate())
    if payment_date > getdate(nowdate()):
        frappe.throw("The payment date cannot be in the future.")

    description = sales_order.get("custom_billingo_advance_description")
    amount = flt(sales_order.get("custom_billingo_advance_amount"))
    stored_vat = sales_order.get("custom_billingo_advance_vat_rate")
    if not description or amount <= 0 or not stored_vat:
        frappe.throw(
            f"Sales Order {sales_order.name} is missing the stored deposit details "
            "(description, amount or VAT rate), so its advance invoice cannot be built."
        )

    vat_treatment = _get_vat_treatment(sales_order)
    if vat_treatment["vat"] != stored_vat:
        frappe.throw(
            f"The order's VAT rate ({vat_treatment['vat']}) differs from the one on its "
            f"Díjbekérő ({stored_vat}). Check the order before issuing the advance invoice."
        )

    item = _build_advance_item(description, amount, vat_treatment)
    if received_gross is not None:
        if flt(received_gross) <= 0:
            frappe.throw("The received amount must be positive.")
        # Billingo derives the net and the VAT from a gross line itself.
        item["unit_price"] = flt(received_gross)
        item["unit_price_type"] = "gross"

    partner_id = _get_or_create_partner(sales_order.customer)
    payload = _build_document_payload(
        sales_order,
        partner_id,
        "advance",
        [item],
        fulfillment_date=payment_date,
        due_date=payment_date,
        vendor_id=_order_vendor_id(sales_order, "advance"),
        conversion_rate=_conversion_rate_on(sales_order, payment_date),
    )
    payload["paid"] = True
    result = _create_or_recover(sales_order, payload, "advance", "advance invoice")

    # A retry after a lost response finds the advance invoice the first attempt
    # issued. It is a real tax document, so it is recorded either way - but if
    # it carries another date, say so instead of reporting success silently.
    issued_date = str(result.get("fulfillment_date") or "")[:10]
    date_warning = None
    if issued_date and issued_date != str(payment_date):
        date_warning = (
            f"Billingo already had this order's advance invoice, dated {issued_date} instead of "
            f"{payment_date}. It has been recorded as it is; if the date is wrong, correct it in "
            "Billingo (storno and reissue)."
        )

    # Recorded and committed before the Díjbekérő is marked paid: losing this ID
    # would leave an issued advance invoice that no ERPNext record points at.
    _record_order_state(
        sales_order.name,
        {
            "custom_billingo_advance_invoice_id": result.get("id"),
            "custom_billingo_advance_invoice_number": result.get("invoice_number"),
            "custom_billingo_advance_payment_entry": payment_entry,
        },
    )

    # The Díjbekérő was paid, not voided: record the payment on it rather than
    # cancelling it, so Billingo shows it as settled (and sends no reminders).
    paid_error = _mark_proforma_paid(
        proforma_id,
        payment_date,
        flt(result.get("gross_total"))
        or (flt(received_gross) if received_gross is not None else _gross(amount, vat_treatment)),
        payload["payment_method"],
        payload["conversion_rate"],
    )
    sent = _send_document_via_billingo(sales_order, result)
    return {
        "mode": PROFORMA_MODE_ADVANCE,
        "document_id": result.get("id"),
        "invoice_number": result.get("invoice_number"),
        "proforma_paid_error": paid_error,
        "date_warning": date_warning,
        "send_failed": not sent,
        "late": (getdate(nowdate()) - payment_date).days > ADVANCE_INVOICE_WARNING_DAYS,
    }


def _gross(net_amount, vat_treatment):
    try:
        rate = float(str(vat_treatment.get("vat") or "0").rstrip("%"))
    except ValueError:
        rate = 0.0
    return round(flt(net_amount) * (1 + rate / 100), 2)


def _mark_proforma_paid(proforma_id, payment_date, gross_amount, payment_method, conversion_rate):
    """Record the customer's payment on the Díjbekérő, so Billingo shows it settled.

    PUT replaces the document's payment history, so repeating it is harmless.
    Reported, never raised: by now the invoice it paid for is issued, and
    aborting would only hide that from ERPNext.
    """
    try:
        response = requests.put(
            f"{BILLINGO_BASE_URL}/documents/{proforma_id}/payments",
            json=[
                {
                    "date": str(payment_date),
                    "price": gross_amount,
                    "payment_method": payment_method,
                    "conversion_rate": conversion_rate or 1,
                }
            ],
            headers=_get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        return None
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
    except Exception:
        error_detail = frappe.get_traceback()

    frappe.log_error(
        title="Billingo Proforma Payment Not Recorded",
        message=(
            f"The payment could not be recorded on Díjbekérő {proforma_id} in Billingo: "
            f"{error_detail}"
        ),
    )
    return error_detail


def _cancel_billingo_document(document_id):
    """Cancel (storno) a Díjbekérő that is withdrawn unpaid. Returns the error, or None."""
    try:
        response = requests.post(
            f"{BILLINGO_BASE_URL}/documents/{document_id}/cancel",
            headers=_get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        return None
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
    except Exception:
        error_detail = frappe.get_traceback()

    frappe.log_error(
        title="Billingo Proforma Cancel Failed",
        message=f"Díjbekérő {document_id} could not be cancelled in Billingo: {error_detail}",
    )
    return error_detail


def _delete_billingo_draft(document_id):
    """Delete a Billingo document that may be deleted (a draft or a Díjbekérő).

    Never fatal: returns None on success, the error text otherwise.
    """
    try:
        response = requests.delete(
            f"{BILLINGO_BASE_URL}/documents/{document_id}",
            headers=_get_headers(),
            timeout=15,
        )
        if response.status_code in (200, 204, 404):
            return None
        response.raise_for_status()
        return None
    except Exception:
        error_detail = frappe.get_traceback()

    frappe.log_error(
        title="Billingo Document Not Deleted",
        message=f"Billingo document {document_id} could not be deleted: {error_detail}",
    )
    return error_detail


# ----------------------------------------------------------------------
# Deposit received - Payment Entry
# ----------------------------------------------------------------------

def queue_advance_from_payment(payment_entry, method=None):
    """doc_events hook: Payment Entry on_submit.

    Recording the customer's payment against a Sales Order with a deposit
    Díjbekérő is the moment the advance invoice is due, so it is issued from
    there - nobody has to press a second button. The Billingo call runs after
    the Payment Entry has committed, so a failure there can never undo or block
    recording the payment; it shows up on the order instead, where the manual
    button remains as a fallback.
    """
    if payment_entry.get("payment_type") != "Receive" or payment_entry.get("party_type") != "Customer":
        return
    seen = set()
    for row in payment_entry.get("references") or []:
        if row.get("reference_doctype") != "Sales Order" or row.get("reference_name") in seen:
            continue
        seen.add(row.get("reference_name"))
        state = frappe.db.get_value(
            "Sales Order",
            row.get("reference_name"),
            [
                "custom_billingo_proforma_id",
                "custom_billingo_proforma_mode",
                "custom_billingo_advance_invoice_id",
            ],
            as_dict=True,
        )
        if (
            not state
            or not state.get("custom_billingo_proforma_id")
            or state.get("custom_billingo_proforma_mode") != PROFORMA_MODE_ADVANCE
        ):
            continue
        order_name = row.get("reference_name")
        if state.get("custom_billingo_advance_invoice_id"):
            # Every advance received is its own tax point; this flow issues one
            # advance invoice per order, so make the second payment visible.
            _note_on_order(
                order_name,
                f"Payment Entry {payment_entry.name} is a further payment on this order. No advance "
                "invoice was issued for it automatically: if it arrived before the final invoice, "
                "issue its advance invoice in Billingo by hand.",
            )
            continue
        order_currency = frappe.db.get_value("Sales Order", order_name, "currency")
        paid_currency = payment_entry.get("paid_from_account_currency") or order_currency
        if order_currency and paid_currency != order_currency:
            # allocated_amount is in the receivable account's currency; issuing
            # it as an amount in the order's currency would be wildly wrong.
            _note_on_order(
                order_name,
                f"Payment Entry {payment_entry.name} is in {paid_currency}, the order in "
                f"{order_currency}, so the advance invoice was not issued automatically. Use "
                "Billingo > Előlegszámla kiállítása kézzel.",
            )
            continue
        frappe.enqueue(
            "billingo.proforma.issue_advance_from_payment",
            queue="short",
            enqueue_after_commit=True,
            sales_order=row.get("reference_name"),
            payment_entry=payment_entry.name,
            amount=flt(row.get("allocated_amount")),
            payment_date=str(payment_entry.get("posting_date")),
        )


def _note_on_order(sales_order_name, message):
    """Show a Billingo problem on the order (red banner) inside the current transaction."""
    frappe.db.set_value(
        "Sales Order",
        sales_order_name,
        {"custom_billingo_sync_status": "Failed", "custom_billingo_error": message[:500]},
        update_modified=False,
    )


def drop_zero_tax_rows(payment_entry, method=None):
    """Payment Entry before_validate: remove 0% tax rows copied from the order.

    A payment against an order inherits the order's tax template. For a 0%
    order (EU 89.§, export, TAM) that is a row with no amount, and ERPNext then
    refuses to submit ("Either debit or credit amount is required for 4673",
    staging 2026-10-02). A zero row books nothing, so dropping it changes no
    figure; rows with a rate are left alone.
    """
    taxes = payment_entry.get("taxes") or []
    kept = [row for row in taxes if flt(row.get("rate")) or flt(row.get("tax_amount"))]
    if len(kept) == len(taxes):
        return
    payment_entry.set("taxes", kept)
    if not kept:
        payment_entry.set("sales_taxes_and_charges_template", None)


def flag_cancelled_advance_payment(payment_entry, method=None):
    """doc_events hook: Payment Entry on_cancel.

    The advance invoice issued for this payment stays valid in Billingo; it
    must be cancelled there. Until then it must not be deducted on the final
    invoice as if the money were still in.
    """
    for row in payment_entry.get("references") or []:
        if row.get("reference_doctype") != "Sales Order":
            continue
        name = row.get("reference_name")
        state = frappe.db.get_value(
            "Sales Order",
            name,
            ["custom_billingo_advance_payment_entry", "custom_billingo_advance_invoice_number",
             "custom_billingo_advance_consumed_by"],
            as_dict=True,
            for_update=True,
        )
        if not state or state.get("custom_billingo_advance_payment_entry") != payment_entry.name:
            continue
        if not state.get("custom_billingo_advance_consumed_by"):
            frappe.db.set_value(
                "Sales Order",
                name,
                "custom_billingo_advance_consumed_by",
                f"{payment_entry.name} (cancelled)",
                update_modified=False,
            )
        _note_on_order(
            name,
            f"Payment Entry {payment_entry.name} was cancelled, but its advance invoice "
            f"{state.get('custom_billingo_advance_invoice_number') or ''} is still valid in Billingo: "
            "cancel (storno) it there. It will not be deducted on the final invoice.",
        )


def issue_advance_from_payment(sales_order, payment_entry, amount, payment_date):
    """Background job queued by queue_advance_from_payment."""
    try:
        # Submitting the payment authorised this; the job runs as that user,
        # who need not have write access to Sales Orders.
        doc = _load_locked_sales_order(sales_order, check_permission=False)
        if doc.get("custom_billingo_advance_invoice_id"):
            # One advance invoice per order; a second payment is settled on the
            # final invoice through ERPNext's advance allocation.
            return
        if doc.get("custom_billingo_proforma_mode") != PROFORMA_MODE_ADVANCE:
            return
        _issue_advance_invoice(
            doc,
            doc.custom_billingo_proforma_id,
            payment_date,
            received_gross=amount,
            payment_entry=payment_entry,
        )
    except Exception as e:
        frappe.db.rollback()
        _record_order_error(
            sales_order,
            f"Payment Entry {payment_entry}: the advance invoice could not be issued "
            f"automatically ({e}). Use Billingo > Előlegszámla kiállítása on the order.",
        )


# ----------------------------------------------------------------------
# Sales Invoice side - called from api.py
# ----------------------------------------------------------------------

def _linked_sales_orders(sales_invoice):
    if sales_invoice.get("is_return"):
        return set()
    names = {row.get("sales_order") for row in sales_invoice.get("items") or []}
    names.discard(None)
    names.discard("")
    return names


def _has_billingo_state(state):
    return bool(
        state.get("custom_billingo_advance_invoice_id")
        or (
            state.get("custom_billingo_proforma_id")
            and state.get("custom_billingo_proforma_mode") == PROFORMA_MODE_FULL
        )
    )


def _order_state(sales_invoice, for_update=False):
    """(state of the one Sales Order behind the invoice, ambiguous flag).

    ambiguous is True when the invoice draws on several orders and at least one
    of them carries Billingo state: which advance applies, or which Díjbekérő was
    paid, cannot be answered by guessing.
    """
    names = _linked_sales_orders(sales_invoice)
    if not names:
        return {}, False

    states = {}
    for name in sorted(names):
        states[name] = (
            frappe.db.get_value(
                "Sales Order", name, list(ORDER_STATE_FIELDS), as_dict=True, for_update=for_update
            )
            or {}
        )

    if len(names) > 1:
        return {}, any(_has_billingo_state(state) for state in states.values())

    name = next(iter(names))
    state = dict(states[name])
    state["name"] = name
    return state, False


def _usable_advance_id(sales_invoice, state):
    """The advance this invoice may deduct, or None.

    An advance can only be deducted once. ERPNext invoices one order in as many
    parts as the deliveries take, and without this check each of them would
    carry the same deduction - under-billing the customer and understating
    output VAT by the deposit, once per invoice.
    """
    advance_id = cint(state.get("custom_billingo_advance_invoice_id"))
    if not advance_id:
        return None
    consumed_by = state.get("custom_billingo_advance_consumed_by")
    if consumed_by and consumed_by != sales_invoice.name:
        return None
    return advance_id


def advance_invoice_for(sales_invoice, for_update=False):
    """Billingo ID of the advance invoice to deduct on this invoice, or None.

    Used by api._build_billingo_payload. Billingo then deducts the advance the
    way its own "Bizonylat az előlegszámla alapján" does, and reports it to NAV
    as an advance deduction; a hand-built negative line would reach NAV as an
    ordinary negative item.
    """
    if sales_invoice.get("doctype") != "Sales Invoice":
        return None
    # At submit (for_update) this is a locking read: it returns the latest
    # committed state, so a second invoice submitted at the same moment waits
    # and then sees the deposit as used. The draft job must not lock: it holds
    # its transaction open across Billingo calls.
    state, ambiguous = _order_state(sales_invoice, for_update=for_update)
    if ambiguous:
        return None
    return _usable_advance_id(sales_invoice, state)


def prepaid_proforma_for(sales_invoice, for_update=False):
    """The order's 'Teljes tételes' Díjbekérő state if the money is in, else None.

    "In" is what ERPNext knows: the payments recorded against the order cover
    it (Sales Order advance_paid), or this invoice has nothing outstanding.
    Only then does the invoice go to Billingo as paid and the Díjbekérő get
    settled - a Díjbekérő alone proves nothing, and a "paid" invoice that was
    not paid would stop reminders and mislead NAV.
    """
    if sales_invoice.get("doctype") != "Sales Invoice":
        return None
    state, ambiguous = _order_state(sales_invoice, for_update=for_update)
    if ambiguous or not state.get("custom_billingo_proforma_id"):
        return None
    if state.get("custom_billingo_proforma_mode") != PROFORMA_MODE_FULL:
        return None
    # ERPNext keeps advance_paid in the party account's currency. A EUR order
    # of a customer with a forint receivable (Axro, staging 2026-10-02) has it
    # in HUF, so it is compared with the HUF total - the EUR total would make
    # any part payment look like full payment.
    order_total = flt(state.get("grand_total"))
    party_currency = state.get("party_account_currency")
    if party_currency and state.get("currency") and party_currency != state.get("currency"):
        order_total = flt(state.get("base_grand_total"))
    order_paid = flt(state.get("advance_paid")) >= order_total - PAID_TOLERANCE
    invoice_paid = (
        sales_invoice.get("outstanding_amount") is not None
        and flt(sales_invoice.get("outstanding_amount")) <= PAID_TOLERANCE
        and flt(sales_invoice.get("grand_total")) > 0
    )
    if not (order_paid or invoice_paid):
        return None
    return state


def allocate_order_advances(sales_invoice, method=None):
    """Sales Invoice before_validate: let ERPNext pull in the order's payments.

    A Payment Entry recorded against the order (the Díjbekérő being paid) stays
    an unallocated advance unless the invoice fetches it; the invoice then shows
    the full amount outstanding, and the customer looks like a debtor. Ticking
    ERPNext's own "allocate advances automatically" on new invoices of such
    orders closes that gap without anyone having to remember the button.

    For a foreign-currency invoice booked on a forint receivable the allocation
    is done here instead, capped: see _allocate_capped.
    """
    if sales_invoice.get("doctype") != "Sales Invoice" or sales_invoice.get("is_return"):
        return
    if sales_invoice.is_new() and not sales_invoice.get("allocate_advances_automatically"):
        state, ambiguous = _order_state(sales_invoice)
        if not ambiguous and state.get("custom_billingo_proforma_mode") and flt(state.get("advance_paid")) > 0:
            sales_invoice.allocate_advances_automatically = 1
    if sales_invoice.get("allocate_advances_automatically") and _is_cross_currency(sales_invoice):
        _allocate_capped(sales_invoice)


def _party_account_currency(sales_invoice):
    return sales_invoice.get("party_account_currency") or (
        sales_invoice.get("debit_to")
        and frappe.get_cached_value("Account", sales_invoice.debit_to, "account_currency")
    )


def _is_cross_currency(sales_invoice):
    party_currency = _party_account_currency(sales_invoice)
    return bool(party_currency) and party_currency != sales_invoice.get("currency")


def _floor_to(value, precision):
    factor = 10 ** cint(precision)
    return math.floor(flt(value) * factor + 1e-9) / factor


def _allocate_capped(sales_invoice):
    """ERPNext's own allocation, without its forint-rounding failure.

    ERPNext allocates up to the rounded forint total (1 738 002 Ft) but then
    checks against the unrounded one (4 724,50 EUR x 367,87 = 1 738 001,82 Ft)
    and refuses with "Előleg összege nem lehet nagyobb, mint HUF 1738001.82"
    (staging, 2026-10-02). Allocate the same entries, capped to what the check
    accepts; the fraction of a forint left over is closed after submit by
    settle_rounding_residual.
    """
    # company_currency is a read-only property on v16 documents; set_advances
    # reads it itself.
    sales_invoice.party_account_currency = _party_account_currency(sales_invoice)
    sales_invoice.set_advances()
    limit = flt(
        flt(sales_invoice.get("rounded_total") or sales_invoice.get("grand_total"))
        * flt(sales_invoice.get("conversion_rate")),
        sales_invoice.precision("grand_total"),
    )
    remaining = limit
    for row in sales_invoice.get("advances") or []:
        allocated = _floor_to(
            min(flt(row.get("allocated_amount")), remaining), row.precision("allocated_amount")
        )
        row.allocated_amount = max(allocated, 0)
        remaining -= row.allocated_amount
    # ERPNext would re-run its own allocation in validate and fail again.
    sales_invoice.allocate_advances_automatically = 0


def settle_rounding_residual(sales_invoice, method=None):
    """Sales Invoice on_submit: close the fraction of a forint the cap left open.

    Never fatal - the invoice is already issued; a failure is logged and the
    forint stays open for a manual Payment Reconciliation.

    - The payment had that forint left on the order: it is reconciled against
      the invoice, as ERPNext does for the rest (no profit or loss).
    - Otherwise the customer paid that much less: written off to 8699
      (Kerekítési különbözet, the loss side of the rounding rule).
    """
    if sales_invoice.get("doctype") != "Sales Invoice" or sales_invoice.get("is_return"):
        return
    if not _is_cross_currency(sales_invoice):
        return
    residual = flt(frappe.db.get_value("Sales Invoice", sales_invoice.name, "outstanding_amount"))
    if not 0 < residual <= PAID_TOLERANCE:
        return
    orders = _linked_sales_orders(sales_invoice)
    if not orders:
        return

    frappe.db.savepoint("billingo_rounding_residual")
    try:
        residual = _reconcile_order_leftovers(sales_invoice, orders, residual)
        if residual > 0:
            _write_off_residual(sales_invoice, residual)
    except Exception:
        frappe.db.rollback(save_point="billingo_rounding_residual")
        frappe.log_error(
            title="Billingo Rounding Residual",
            message=f"Sales Invoice {sales_invoice.name}: {frappe.get_traceback()}",
        )


def _reconcile_order_leftovers(sales_invoice, orders, residual):
    from erpnext.accounts.utils import reconcile_against_document

    payment_entries = {
        row.get("reference_name")
        for row in sales_invoice.get("advances") or []
        if row.get("reference_type") == "Payment Entry"
    }
    for payment_entry in sorted(payment_entries):
        leftovers = frappe.get_all(
            "Payment Entry Reference",
            filters={
                "parent": payment_entry,
                "parenttype": "Payment Entry",
                "reference_doctype": "Sales Order",
                "reference_name": ["in", sorted(orders)],
                "docstatus": 1,
            },
            fields=["name", "allocated_amount"],
        )
        for leftover in leftovers:
            amount = flt(leftover.get("allocated_amount"))
            if residual <= 0 or not 0 < amount <= PAID_TOLERANCE:
                continue
            allocated = min(amount, residual)
            reconcile_against_document(
                [
                    frappe._dict(
                        {
                            "voucher_type": "Payment Entry",
                            "voucher_no": payment_entry,
                            "voucher_detail_no": leftover.get("name"),
                            "against_voucher_type": "Sales Invoice",
                            "against_voucher": sales_invoice.name,
                            "account": sales_invoice.debit_to,
                            "party_type": "Customer",
                            "party": sales_invoice.customer,
                            "is_advance": "Yes",
                            "dr_or_cr": "credit_in_account_currency",
                            "unadjusted_amount": amount,
                            "allocated_amount": allocated,
                            "exchange_rate": 1,
                            "grand_total": flt(sales_invoice.get("base_grand_total")),
                            "outstanding_amount": residual,
                            "difference_account": frappe.get_cached_value(
                                "Company", sales_invoice.company, "exchange_gain_loss_account"
                            ),
                            "exchange_gain_loss": 0,
                            "difference_posting_date": sales_invoice.get("posting_date"),
                        }
                    )
                ]
            )
            residual = flt(residual - allocated, 2)
    return residual


def _write_off_residual(sales_invoice, residual):
    account = frappe.db.get_value(
        "Account",
        {"account_number": "8699", "company": sales_invoice.company, "is_group": 0},
        "name",
    )
    if not account:
        frappe.log_error(
            title="Billingo Rounding Residual",
            message=f"Sales Invoice {sales_invoice.name}: no 8699 account to write off {residual}.",
        )
        return
    journal = frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": sales_invoice.company,
            "posting_date": sales_invoice.get("posting_date"),
            "user_remark": f"Kerekítési különbözet (devizás számla forintra kerekítése): {sales_invoice.name}",
            "accounts": [
                {"account": account, "debit_in_account_currency": residual},
                {
                    "account": sales_invoice.debit_to,
                    "party_type": "Customer",
                    "party": sales_invoice.customer,
                    "credit_in_account_currency": residual,
                    "reference_type": "Sales Invoice",
                    "reference_name": sales_invoice.name,
                },
            ],
        }
    )
    journal.flags.ignore_permissions = True
    journal.insert()
    journal.submit()


def invoice_draft_blocked(sales_invoice):
    """Called by api.sync_billingo_draft: a message to record instead of syncing, or None.

    An empty string means "skip quietly": an invoice that will deduct an advance
    gets no Billingo draft at all. Billingo refuses a draft that names an advance
    invoice ("Only an invoice can be issued from an advance invoice", staging
    2026-10-02), so the invoice is created whole at submit instead.
    """
    state, ambiguous = _order_state(sales_invoice)
    if not ambiguous and _usable_advance_id(sales_invoice, state):
        return ""
    if ambiguous:
        return (
            "This invoice draws on several Sales Orders, and at least one of them has a Billingo "
            "advance or Díjbekérő. Nothing was sent to Billingo, because applying the wrong "
            "advance or payment would produce an incorrect invoice. Split the invoice per Sales "
            "Order, or handle this one in Billingo by hand."
        )
    return None


def check_invoice_orders(sales_invoice):
    """Called by api.finalize_billingo_invoice before anything is sent.

    Locks the order row, so two invoices of one order cannot both claim its
    advance or both settle its Díjbekérő.
    """
    _state, ambiguous = _order_state(sales_invoice, for_update=True)
    if ambiguous:
        frappe.throw(
            f"Sales Invoice {sales_invoice.name} draws on several Sales Orders, and at least one "
            "of them has a Billingo advance or Díjbekérő. Applying the wrong advance or payment "
            "would produce an incorrect invoice, so it cannot be submitted as it stands. Split it "
            "per Sales Order, or issue this one in Billingo by hand.",
            title="Billingo: several Sales Orders with Billingo state",
        )


def settle_prepaid_proforma(sales_invoice, payment_method, conversion_rate):
    """Mark the order's 'Teljes tételes' Díjbekérő paid, once, after its invoice is issued.

    Returns an error text for the user, or None.
    """
    state = prepaid_proforma_for(sales_invoice, for_update=True)
    if not state or state.get("custom_billingo_proforma_paid_by"):
        return None

    proforma_id = state["custom_billingo_proforma_id"]
    try:
        gross_total = flt(_get_document(proforma_id).get("gross_total"))
    except Exception:
        frappe.log_error(
            title="Billingo Proforma Lookup Failed",
            message=f"Could not read Díjbekérő {proforma_id}:\n{frappe.get_traceback()}",
        )
        gross_total = 0
    # Recorded whatever Billingo answers below: the invoice is issued, so the
    # Díjbekérő must no longer be withdrawable from ERPNext.
    frappe.db.set_value(
        "Sales Order",
        state["name"],
        "custom_billingo_proforma_paid_by",
        sales_invoice.name,
        update_modified=False,
    )
    if not gross_total:
        return "The Díjbekérő's total could not be read, so it was not marked paid."
    return _mark_proforma_paid(
        proforma_id,
        _order_payment_date(state["name"]) or getdate(sales_invoice.get("posting_date") or nowdate()),
        gross_total,
        payment_method,
        conversion_rate,
    )


def _order_payment_date(sales_order_name):
    """Date of the latest submitted payment recorded against the order, or None."""
    parents = frappe.get_all(
        "Payment Entry Reference",
        filters={
            "reference_doctype": "Sales Order",
            "reference_name": sales_order_name,
            "docstatus": 1,
        },
        pluck="parent",
    )
    if not parents:
        return None
    rows = frappe.get_all(
        "Payment Entry",
        filters={"name": ["in", list(set(parents))], "docstatus": 1},
        fields=["posting_date"],
        order_by="posting_date desc",
        limit=1,
    )
    return getdate(rows[0]["posting_date"]) if rows else None


def mark_advance_consumed(sales_invoice, advance_id):
    """Record which invoice used the advance, in the submit's own transaction."""
    state, ambiguous = _order_state(sales_invoice, for_update=True)
    if ambiguous or not state.get("name"):
        return
    if cint(state.get("custom_billingo_advance_invoice_id")) != cint(advance_id):
        return
    if state.get("custom_billingo_advance_consumed_by"):
        return
    frappe.db.set_value(
        "Sales Order",
        state["name"],
        "custom_billingo_advance_consumed_by",
        sales_invoice.name,
        update_modified=False,
    )


def release_advance_on_cancel(sales_invoice, method=None):
    """doc_events hook: Sales Invoice on_cancel.

    The invoice that deducted the deposit no longer stands, so the deposit is
    free again for the amended invoice. The Billingo invoice is not touched
    here: it has to be cancelled (storno) in Billingo, or the deposit would be
    deducted twice there - recorded on the order so it is not forgotten.
    """
    for name in _linked_sales_orders(sales_invoice):
        consumed_by, paid_by = frappe.db.get_value(
            "Sales Order",
            name,
            ["custom_billingo_advance_consumed_by", "custom_billingo_proforma_paid_by"],
            for_update=True,
        )
        if paid_by == sales_invoice.name:
            # The Díjbekérő stays paid in Billingo (the money did arrive); the
            # amended invoice records itself as the one that settled it.
            frappe.db.set_value(
                "Sales Order",
                name,
                "custom_billingo_proforma_paid_by",
                None,
                update_modified=False,
            )
        if consumed_by != sales_invoice.name:
            continue
        frappe.db.set_value(
            "Sales Order",
            name,
            {
                "custom_billingo_advance_consumed_by": None,
                "custom_billingo_error": (
                    f"Sales Invoice {sales_invoice.name} was cancelled, so its advance deduction "
                    "is free again for the next invoice. Cancel (storno) its Billingo invoice "
                    f"{sales_invoice.get('custom_billingo_invoice_number') or ''} in Billingo too."
                )[:500],
            },
            update_modified=False,
        )


def block_order_cancel(sales_order, method=None):
    """doc_events hook: Sales Order before_cancel.

    Billingo documents raised from the order would be left pointing at a
    cancelled order: an issued advance invoice or invoice is a tax document
    that cancelling in ERPNext does not undo, and an open Díjbekérő would
    still ask the customer to pay. Make the user deal with them first.
    """
    if sales_order.get("custom_billingo_advance_invoice_id"):
        frappe.throw(
            f"Sales Order {sales_order.name} already has a Billingo advance invoice "
            f"({sales_order.get('custom_billingo_advance_invoice_number')}). It cannot be "
            "cancelled: cancel (storno) the advance invoice in Billingo first, and correct the "
            "order's invoices in ERPNext.",
            title="Billingo: the order has an issued advance invoice",
        )
    if sales_order.get("custom_billingo_proforma_id") and not sales_order.get(
        "custom_billingo_proforma_paid_by"
    ):
        frappe.throw(
            f"Sales Order {sales_order.name} has an open Billingo Díjbekérő "
            f"({sales_order.get('custom_billingo_proforma_number') or sales_order.custom_billingo_proforma_id}). "
            "Withdraw it first (Billingo > Díjbekérő visszavonása), then cancel the order.",
            title="Billingo: the order has an open Díjbekérő",
        )
