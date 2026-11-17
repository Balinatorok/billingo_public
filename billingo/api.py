"""
Billingo <-> ERPNext integration.

Flow:
  Sales Invoice Draft  (after_insert / on_update, docstatus 0)
      -> push a Billingo `type: "draft"` document.
      -> first save: POST /documents
      -> every subsequent save while still Draft: DELETE the old
         draft + POST a new one (Billingo has no endpoint to edit a
         draft's contents in place - confirmed against the v3
         OpenAPI spec: PUT /documents/{id} always *converts* a draft
         to an invoice, it never just saves an edit).

  Sales Invoice Submit (on_submit, docstatus 1)
      -> PUT /documents/{id} on the SAME billingo id, with
         type: "invoice". This converts the draft into a real,
         numbered invoice in place - no new Billingo document id.
"""

import hashlib
import re
import json
import time  # p- needed by _get_document_by_vendor_id_with_retry

import frappe
import requests

BILLINGO_BASE_URL = "https://api.billingo.hu/v3"

DEFAULT_PAYMENT_METHOD = "wire_transfer"
DEFAULT_DOCUMENT_LANGUAGE = "hu"

MODE_OF_PAYMENT_MAP = {
    "Cash": "cash",
    "Wire Transfer": "wire_transfer",
    "Bank Draft": "wire_transfer",
    "Credit Card": "bankcard",
    "Cheque": "postai_csekk",
    "Debit Card": "bankcard",
    "Direct Debit": "wire_transfer",
    "Phone": "online_bankcard",
    "Utánvét": "cash_on_delivery",
    "Készpénz": "cash",
    "Átutalás": "wire_transfer",
    "Előreutalás": "elore_utalas",
    "Online bankkártya": "online_bankcard",
}

# Bank-account IDs are profile scoped in Billingo and must never be copied
# between the live and test profiles. Optional: account-number fragments that
# pick the intended account per currency when the active profile has several,
# e.g. {"HUF": "11111111-22222222"}. Simpler alternative: the site config keys
# billingo_bank_account_id_huf / _eur / _usd (see README). With exactly one
# account per currency in the profile, that one is used automatically.
PREFERRED_BANK_ACCOUNT_FRAGMENT_BY_CURRENCY = {}

# This is deliberately a site setting, rather than a source-code constant:
# document blocks belong to a Billingo profile, just like partners and bank
# accounts.  Set it with:
#   bench --site <site> set-config billingo_document_block_id <block-id>
DOCUMENT_BLOCK_CONFIG_KEY = "billingo_document_block_id"

# An invoice can pick a different Billingo invoice block (e.g. a separate
# webshop block) through its "Billingo számlatömb" field. That field
# links to a "Billingo Szamlatomb" record holding the block's Billingo ID, and
# is filled from the invoice's cost center. Empty field -> the default block
# above. The records live in the site's database, so the live and test
# profiles (different block IDs) are kept apart per site automatically.
INVOICE_BLOCK_FIELD = "custom_billingo_szamlatomb"
INVOICE_BLOCK_DOCTYPE = "Billingo Szamlatomb"
INVOICE_BLOCK_ID_FIELD = "billingo_block_id"


def _get_headers():
    api_key = frappe.conf.get("billingo_api_key")
    if not api_key:
        frappe.throw("billingo_api_key is not set in site_config.json")
    return {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_error_text(response):
    try:
        data = response.json()
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                # Billingo puts useful validation information in sibling
                # fields (notably ``errors``).  Preserve the complete API
                # response instead of reducing it to "Validation Failed".
                return json.dumps(data, ensure_ascii=False, default=str)
            if data.get("message"):
                return json.dumps(data, ensure_ascii=False, default=str)
            return json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        pass
    return response.text or "Unknown Billingo error"


# ----------------------------------------------------------------------
# Partner / customer
# ----------------------------------------------------------------------

def _parse_document_block_id(value, source):
    try:
        block_id = int(value)
    except (TypeError, ValueError):
        frappe.throw(f"{source} must be a positive integer.")

    if block_id <= 0:
        frappe.throw(f"{source} must be a positive integer.")
    return block_id


def _describe_document_blocks():
    """List the active profile's document blocks, to help configuring IDs."""
    try:
        response = requests.get(
            f"{BILLINGO_BASE_URL}/document-blocks",
            params={"per_page": 100},
            headers=_get_headers(),
            timeout=15,
        )
        response.raise_for_status()
        blocks = response.json().get("data") or []
    except Exception:
        return "The document blocks of the active Billingo profile could not be listed."

    if not blocks:
        return "The active Billingo profile has no document blocks."
    return "Available document blocks: " + "; ".join(
        f"{block.get('name')} (prefix: {block.get('prefix')}, id: {block.get('id')})"
        for block in blocks
    )


def _get_document_block_id(doc=None):
    """Return the Billingo invoice block for this invoice.

    The invoice's own block (INVOICE_BLOCK_FIELD) wins; without it the site's
    default block (DOCUMENT_BLOCK_CONFIG_KEY) is used.
    """
    selected = doc.get(INVOICE_BLOCK_FIELD) if doc is not None else None
    if selected:
        block_id = frappe.db.get_value(INVOICE_BLOCK_DOCTYPE, selected, INVOICE_BLOCK_ID_FIELD)
        if not block_id:
            frappe.throw(
                f"Billingo számlatömb '{selected}' has no Billingo block ID. "
                "Set it on the Billingo Szamlatomb record and save the invoice again. "
                + _describe_document_blocks(),
                title="Billingo: document block not configured",
            )
        return _parse_document_block_id(block_id, f"Billingo Szamlatomb '{selected}'")

    block_id = frappe.conf.get(DOCUMENT_BLOCK_CONFIG_KEY)
    if block_id in (None, ""):
        frappe.throw(
            f"{DOCUMENT_BLOCK_CONFIG_KEY} is not set in site_config.json. "
            "Configure a valid Billingo invoice document-block ID before syncing."
        )
    return _parse_document_block_id(block_id, DOCUMENT_BLOCK_CONFIG_KEY)


def _get_document_by_vendor_id(vendor_id):
    """Return a previously created Billingo document for an ERPNext reference."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/documents/vendor/{vendor_id}",
        headers=_get_headers(),
        timeout=15,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


# p- NEW: retrying lookup. Billingo can lag behind a POST that is still in
# p- flight (the queued draft job), so a single lookup may miss the draft.
def _get_document_by_vendor_id_with_retry(vendor_id, attempts=4, delay=1.5):
    """Look a document up by vendor_id, retrying briefly if not found yet."""
    for attempt in range(attempts):
        document = _get_document_by_vendor_id(vendor_id)
        if document:
            return document
        if attempt < attempts - 1:
            time.sleep(delay)
    return None


def _get_document(document_id):
    """Return one Billingo document, including its original vendor_id."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/documents/{document_id}",
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _get_online_szamla_status(document_id):
    """Return Billingo's read-only NAV Online Számla status for a document."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/documents/{document_id}/online-szamla",
        headers=_get_headers(),
        timeout=15,
    )
    if response.status_code == 404:
        # Billingo has no NAV record for drafts (and may not have one yet for
        # a newly issued invoice). This is an expected state, not a sync error.
        return None
    response.raise_for_status()
    return response.json()


def _is_billingo_cancellation(document):
    """Whether a Billingo document represents a cancellation/storno."""
    return (document.get("type") or "").casefold() in {
        "cancellation",
        "receipt_cancellation",
    } or (document.get("correction_type") or "").casefold() in {
        "canceled",
        "cancellation",
    }


def _find_related_billingo_cancellation(document):
    """Return a linked cancellation document, if Billingo exposes one.

    This is deliberately read-only. An invoice cancelled directly in Billingo
    must be visible in ERPNext, but it must never change the ERPNext invoice's
    accounting state or create a correction automatically.
    """
    if _is_billingo_cancellation(document):
        return document

    for related in document.get("related_documents") or []:
        related_id = related.get("id")
        if not related_id:
            continue
        related_document = _get_document(related_id)
        if _is_billingo_cancellation(related_document):
            return related_document
    return None


def _format_nav_messages(messages):
    """Make the API's NAV diagnostics readable in one ERPNext field."""
    parts = []
    for message in messages or []:
        if isinstance(message, str):
            parts.append(message)
            continue
        if isinstance(message, dict):
            value = message.get("message") or message.get("code") or json.dumps(
                message, ensure_ascii=False, default=str
            )
            parts.append(str(value))
    return "\n".join(parts)[:500]


def _set_billingo_reconciliation_value(doc, fieldname, value):
    """Write optional reconciliation fields without breaking older sites.

    The migration adds these fields for upgraded sites. The existence check
    lets the read-only reconciliation remain harmless during rolling deploys.
    """
    if frappe.db.has_column("Sales Invoice", fieldname):
        doc.db_set(fieldname, value)


def _reconcile_billingo_document(doc):
    """Refresh NAV and direct-cancellation facts for one Sales Invoice."""
    document_id = doc.get("custom_billingo_document_id")
    if not document_id:
        return {"checked": False, "reason": "No Billingo document ID"}

    document = _get_document(document_id)
    cancellation = _find_related_billingo_cancellation(document)
    if cancellation:
        cancellation_number = cancellation.get("invoice_number") or cancellation.get("id")
        _set_billingo_reconciliation_value(
            doc, "custom_billingo_direct_cancellation_detected", 1
        )
        _set_billingo_reconciliation_value(
            doc,
            "custom_billingo_cancellation_details",
            f"Cancelled directly in Billingo: {cancellation_number}",
        )
    else:
        _set_billingo_reconciliation_value(
            doc, "custom_billingo_direct_cancellation_detected", 0
        )
        _set_billingo_reconciliation_value(doc, "custom_billingo_cancellation_details", "")

    nav_status = None
    nav_messages = ""
    if (document.get("type") or "").casefold() == "draft":
        nav_status = "Not submitted (Billingo draft)"
    else:
        nav = _get_online_szamla_status(document_id)
        if nav is None:
            nav_status = "Not available yet"
        else:
            nav_status = nav.get("status") or "Unknown"
            nav_messages = _format_nav_messages(nav.get("messages"))

    _set_billingo_reconciliation_value(doc, "custom_billingo_nav_status", nav_status)
    _set_billingo_reconciliation_value(doc, "custom_billingo_nav_messages", nav_messages)
    _set_billingo_reconciliation_value(
        doc, "custom_billingo_nav_checked_at", frappe.utils.now_datetime()
    )
    return {
        "checked": True,
        "nav_status": nav_status,
        "cancelled_in_billingo": bool(cancellation),
    }


def _billingo_vendor_id(doc):
    """Return a stable Billingo reference that cannot be reused by Frappe.

    Sales Invoice names can be reissued after an invoice is deleted. Using the
    name itself as Billingo's globally unique ``vendor_id`` can therefore
    attach a later invoice to an earlier document. The creation timestamp is
    immutable for one Sales Invoice; hashing it keeps the external reference
    short and does not expose internal data in Billingo.
    """
    identity = f"{doc.doctype}:{doc.name}:{doc.get('creation') or ''}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return f"erpnext-{digest}"


def _clear_partner_id(customer_name):
    frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", None)


def _find_existing_partner(customer):
    """Reuse an exact-name partner in the active Billingo profile if present."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/partners",
        params={"query": customer.customer_name, "per_page": 100},
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()

    for partner in response.json().get("data", []):
        if (partner.get("name") or "").strip().casefold() == customer.customer_name.strip().casefold():
            return partner.get("id")
    return None


def _get_required_customer_address(customer_name):
    address_line, city, postal_code, country_code = _get_customer_address(customer_name)
    values = {
        "street address": address_line,
        "city": city,
        "postal code": postal_code,
    }
    missing = [label for label, value in values.items() if not value]
    if missing:
        frappe.throw(
            f"Cannot create a Billingo partner for Customer '{customer_name}': "
            f"missing {', '.join(missing)} on its linked Address."
        )
    return address_line, city, postal_code, country_code or "HU"


def _build_partner_payload(customer):
    address_line, city, postal_code, country_code = _get_required_customer_address(customer.name)
    email = _get_customer_email(customer.name)
    payload = {
        "name": customer.customer_name,
        "address": {
            "country_code": country_code or "HU",
            "post_code": postal_code or "",
            "city": city or "",
            "address": address_line or "",
        },
        "emails": [email] if email else [],
        "taxcode": customer.get("tax_id") or "",
    }
    if (country_code or "HU").upper() != "HU":
        # Foreign buyer. Billingo refuses a 0% intra-community (KBAET) line
        # unless the partner is 'Foreign' and carries its EU VAT number
        # (staging, 2026-10-02: "customer type must be set to 'Foreign'");
        # the EU VAT number travels in taxcode (ERPNext tax_id, e.g. DE...).
        payload["tax_type"] = "FOREIGN"
    elif customer.get("customer_type") == "Individual" and not customer.get("tax_id"):
        # Private person (e.g. a webshop buyer): Billingo reports the invoice
        # to NAV without a buyer tax number.
        payload["tax_type"] = "NO_TAX_NUMBER"
    return payload


def _build_partner_update_payload(remote_partner, desired_payload):
    """Preserve Billingo-managed partner fields when updating ERPNext data.

    Billingo's partner PUT endpoint accepts a complete Partner object. ERPNext
    owns the buyer identity, address and its primary recipient, but it does
    not own manually maintained bank, phone, ledger, tax-type or document
    settings in Billingo. Send those fields back unchanged and only replace
    the values ERPNext is authoritative for.
    """
    preserved_fields = (
        "iban",
        "swift",
        "account_number",
        "phone",
        "general_ledger_number",
        "tax_type",
        "custom_billing_settings",
        "group_member_tax_number",
    )
    payload = {
        key: remote_partner[key]
        for key in preserved_fields
        if key in remote_partner and remote_partner[key] is not None
    }
    payload.update(desired_payload)

    # Keep Billingo-only recipients; ERPNext's primary contact is added by
    # ``desired_payload`` and must not erase a recipient entered in Billingo.
    remote_emails = remote_partner.get("emails") or []
    desired_emails = desired_payload.get("emails") or []
    payload["emails"] = list(dict.fromkeys([*remote_emails, *desired_emails]))
    return payload


def _norm(value):
    """Compare the way a human would: case and padding are not a difference.

    Billingo normalizes some of what it is sent. Comparing raw strings would
    make a partner look permanently out of date and push an update on every
    single invoice save.
    """
    return " ".join((value or "").split()).casefold()


def _partner_matches(remote_partner, payload):
    """Compare what Billingo holds with what ERPNext currently says."""
    if _norm(remote_partner.get("name")) != _norm(payload.get("name")):
        return False
    if _norm(remote_partner.get("taxcode")) != _norm(payload.get("taxcode")):
        return False
    # A partner created before foreign buyers were marked as such must be
    # corrected, or every EU invoice to it is refused.
    if payload.get("tax_type") and remote_partner.get("tax_type") != payload["tax_type"]:
        return False

    remote_address = remote_partner.get("address") or {}
    wanted_address = payload.get("address") or {}
    for key in ("country_code", "post_code", "city", "address"):
        if _norm(remote_address.get(key)) != _norm(wanted_address.get(key)):
            return False

    # The address ERPNext knows about counts as a match even when Billingo
    # holds further recipients somebody added there by hand, so a correct
    # partner is not rewritten just to prune extra addresses.
    wanted_emails = [_norm(e) for e in payload.get("emails") or [] if _norm(e)]
    if not wanted_emails:
        return True
    remote_emails = [_norm(e) for e in remote_partner.get("emails") or [] if _norm(e)]
    return all(email in remote_emails for email in wanted_emails)


def _get_stored_partner(partner_id):
    """Fetch a profile-scoped partner; None when this API key cannot see it."""
    response = requests.get(
        f"{BILLINGO_BASE_URL}/partners/{partner_id}",
        headers=_get_headers(),
        timeout=15,
    )
    if response.ok:
        return response.json()
    if response.status_code in (403, 404):
        return None
    response.raise_for_status()
    return None


def _refresh_partner(partner_id, remote_partner, payload):
    """Push corrected ERPNext data onto a partner Billingo already holds.

    Partners were previously written once and never updated, so a corrected
    address, email or tax number in ERPNext never reached Billingo and every
    later invoice repeated the stale data.
    """
    if _partner_matches(remote_partner, payload):
        return partner_id

    # At most one update per partner per request. Without this, a difference
    # this code cannot resolve - anything Billingo stores in a form it will
    # not accept back - would mean a PUT on every draft save.
    guard_flag = f"billingo_partner_refreshed_{partner_id}"
    if frappe.flags.get(guard_flag):
        return partner_id
    frappe.flags[guard_flag] = True

    update_payload = _build_partner_update_payload(remote_partner, payload)

    # Record the change for traceability. The request preserves Billingo-only
    # fields, so direct Billingo configuration is not replaced by this update.
    frappe.log_error(
        title="Billingo Partner Updated",
        message=(
            f"Partner {partner_id} updated from ERPNext.\n"
            f"Before: {json.dumps(remote_partner, ensure_ascii=False, default=str)}\n"
            f"After:  {json.dumps(update_payload, ensure_ascii=False, default=str)}"
        ),
    )

    response = requests.put(
        f"{BILLINGO_BASE_URL}/partners/{partner_id}",
        json=update_payload,
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("id") or partner_id


def _get_or_create_partner(customer_name):
    customer = frappe.get_doc("Customer", customer_name)
    payload = _build_partner_payload(customer)

    existing_id = customer.get("custom_billingo_partner_id")
    if existing_id:
        remote_partner = _get_stored_partner(existing_id)
        if remote_partner is not None:
            return _refresh_partner(existing_id, remote_partner, payload)

        # IDs are only valid in the Billingo profile that created them.  A
        # changed API key can therefore make an otherwise valid Customer
        # record unusable.  Clear it and recover under the active profile.
        _clear_partner_id(customer_name)

    partner_id = _find_existing_partner(customer)
    if partner_id:
        frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", partner_id)
        remote_partner = _get_stored_partner(partner_id)
        if remote_partner is not None:
            return _refresh_partner(partner_id, remote_partner, payload)
        return partner_id

    response = requests.post(
        f"{BILLINGO_BASE_URL}/partners",
        json=payload,
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    partner_id = response.json()["id"]

    frappe.db.set_value("Customer", customer_name, "custom_billingo_partner_id", partner_id)
    return partner_id


def _resolve_billing_address_name(customer_name):
    """Pick the customer's billing address deliberately, not the first link.

    Billingo prints the partner's address as the buyer's address, which the
    Hungarian VAT Act requires to be the buyer's own (billing) address. A
    customer may have several linked Address records, and taking whichever
    row the database returned first could put a delivery address on an
    invoice.  Follow ERPNext's own choice first so the Billingo invoice and
    the ERPNext invoice never disagree.
    """
    primary_address = frappe.db.get_value("Customer", customer_name, "customer_primary_address")
    if primary_address and frappe.db.exists("Address", {"name": primary_address, "disabled": 0}):
        return primary_address

    linked_addresses = frappe.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name": customer_name,
            "parenttype": "Address",
        },
        pluck="parent",
    )
    if not linked_addresses:
        return None

    candidates = frappe.get_all(
        "Address",
        filters={"name": ["in", linked_addresses], "disabled": 0},
        fields=["name", "address_type", "is_primary_address"],
    )
    if not candidates:
        return None

    candidates.sort(
        key=lambda address: (
            0 if address.get("address_type") == "Billing" else 1,
            0 if address.get("is_primary_address") else 1,
            address.get("name") or "",
        )
    )
    return candidates[0].get("name")


def _get_customer_address(customer_name):
    address_name = _resolve_billing_address_name(customer_name)
    if not address_name:
        return "", "", "", ""

    address = frappe.get_doc("Address", address_name)
    if address.address_type != "Billing":
        # Once per request: this runs on every draft save, and an unfixed
        # data problem should not fill the Error Log with identical rows.
        warned_flag = f"billingo_address_warned_{customer_name}"
        if not frappe.flags.get(warned_flag):
            frappe.flags[warned_flag] = True
            frappe.log_error(
                title="Billingo Partner Address",
                message=(
                    f"Customer '{customer_name}' has no billing address; using "
                    f"'{address_name}' ({address.address_type}) for its Billingo partner. "
                    "Set the customer's primary address to its billing address."
                ),
            )

    address_line = " ".join(filter(None, [address.address_line1, address.address_line2]))
    country_code = frappe.db.get_value("Country", address.country, "code") if address.country else ""
    return address_line, address.city or "", address.pincode or "", (country_code or "").upper()


def _get_customer_email(customer_name):
    """Resolve the recipient Billingo should email the invoice to.

    Deterministic on purpose: a customer can have several Contacts (portal
    sign-ups add one per webshop user), and returning whichever row came
    back first could send an invoice to the wrong person.
    """
    primary_contact = frappe.db.get_value("Customer", customer_name, "customer_primary_contact")
    if primary_contact:
        primary_email = frappe.db.get_value("Contact", primary_contact, "email_id")
        if primary_email:
            return primary_email

    contact_names = frappe.get_all(
        "Dynamic Link",
        filters={
            "link_doctype": "Customer",
            "link_name": customer_name,
            "parenttype": "Contact",
        },
        pluck="parent",
    )
    if not contact_names:
        return ""

    contacts = frappe.get_all(
        "Contact",
        filters={"name": ["in", contact_names]},
        fields=["name", "email_id", "is_primary_contact"],
    )
    contacts.sort(
        key=lambda contact: (
            0 if contact.get("is_primary_contact") else 1,
            contact.get("name") or "",
        )
    )
    for contact in contacts:
        if contact.get("email_id"):
            return contact["email_id"]
    return ""


# ----------------------------------------------------------------------
# Items / VAT / payment method / comment (unchanged from working version)
# ----------------------------------------------------------------------

PERCENTAGE_CHARGE_TYPES = {"On Net Total", "On Previous Row Total", "On Previous Row Amount"}

# Billingo's entitlement values for the zero-rated Hungarian tax templates. The entitlement is sent alongside vat="0%".
ZERO_VAT_ENTITLEMENT_BY_TEMPLATE_MARKER = (
    ("89", "KBAET"),
    ("export", "EAM"),
    ("98", "EAM"),
    ("tam", "TAM"),
    ("37", "EUFAD37"),
)

# Entitlements that Billingo only accepts with a VAT code other than "0%".
# With vat="0%" the API allows only WITHOUT_ENTITLEMENT, TAM, NAM_1, NAM_2,
# AAM, KBAET, EAM; "EUFAD37" (Áfa tv. 37. § (1), EU reverse charge service)
# belongs to the "ÁTHK" code (Billingo áfakód-táblázat,
# checked 2026-11-05).
VAT_CODE_BY_ENTITLEMENT = {
    "EUFAD37": "ÁTHK",
}


def _get_shipping_charge_rows(sales_invoice):
    """Return only non-zero Actual rows created by the selected Shipping Rule."""
    if not sales_invoice.get("shipping_rule"):
        return []

    shipping_account = frappe.db.get_value("Shipping Rule", sales_invoice.shipping_rule, "account")
    if not shipping_account:
        frappe.log_error(
            f"Could not resolve the account for Shipping Rule '{sales_invoice.shipping_rule}' "
            f"on Sales Invoice {sales_invoice.name}.",
            "Billingo Shipping Rule Mapping",
        )
        return []

    return [
        tax_row
        for tax_row in (sales_invoice.get("taxes") or [])
        if (
            tax_row.charge_type == "Actual"
            and tax_row.account_head == shipping_account
            and tax_row.tax_amount
        )
    ]


def _map_extra_charges(sales_invoice):
    is_hungarian = _get_invoice_language(sales_invoice) == "hu"
    vat_treatment = _get_vat_treatment(sales_invoice)
    comment = (
        "Automatikusan hozzáadva a Sales Invoice adók táblából (tételes díj)"
        if is_hungarian
        else "Auto-added from Sales Invoice taxes table (flat charge)"
    )
    extra_items = []
    for tax_row in _get_shipping_charge_rows(sales_invoice):
        item = {
            "name": tax_row.description or tax_row.account_head or "Additional charge",
            "unit_price": tax_row.tax_amount,
            "unit_price_type": "net",
            "quantity": 1,
            "unit": "pcs",
            "vat": vat_treatment["vat"],
            "comment": comment,
        }
        if vat_treatment["entitlement"]:
            item["entitlement"] = vat_treatment["entitlement"]
        extra_items.append(item)
    return extra_items


def _get_vat_treatment(sales_invoice):
    """Resolve the one Billingo VAT/entitlement treatment used by all lines.

    Refuses rather than guessing. An invoice whose VAT cannot be determined
    used to be sent to Billingo at 0%, which turns a data-entry slip into an
    incorrectly issued legal document - it happened once in production
    (a domestic sale issued with no VAT). Stopping here surfaces the problem in ERPNext: the draft
    sync records it on the invoice, and the submit is aborted.
    """
    template_name = (sales_invoice.get("taxes_and_charges") or "").casefold()

    for tax_row in sales_invoice.get("taxes") or []:
        if tax_row.charge_type in PERCENTAGE_CHARGE_TYPES:
            if tax_row.rate:
                if re.search(r"(?<![\d.,])0\s*%", template_name):
                    # A zero-VAT template whose row carries a rate: the
                    # template itself is wrong (staging and production had
                    # 9% in the 89.§ one and 18% in the TAM one, 2026-10-02).
                    frappe.throw(
                        f"{sales_invoice.doctype} {sales_invoice.name} uses the tax template "
                        f"'{sales_invoice.get('taxes_and_charges')}', which is a 0% template, "
                        f"but its tax row carries {tax_row.rate}%. Fix the rate in the template "
                        "(and on this document) before sending anything to Billingo.",
                        title="Billingo: 0% template with a VAT rate",
                    )
                return {"vat": f"{int(tax_row.rate)}%", "entitlement": None}
            break

    for marker, entitlement in ZERO_VAT_ENTITLEMENT_BY_TEMPLATE_MARKER:
        if marker in template_name:
            return {
                "vat": VAT_CODE_BY_ENTITLEMENT.get(entitlement, "0%"),
                "entitlement": entitlement,
                "note": entitlement,
            }

    frappe.throw(
        f"Sales Invoice {sales_invoice.name} has no VAT rate that can be sent to Billingo. "
        "Its taxes table contains no percentage VAT row, and its Sales Taxes and Charges "
        f"Template ({sales_invoice.get('taxes_and_charges') or 'not set'}) is not one of the "
        "known zero-VAT templates. Set the correct tax template on the invoice - for a domestic "
        "sale that is the 27% one - and save again. Billingo will not be sent a 0% invoice by "
        "default, because an incorrect VAT rate on an issued invoice cannot be undone silently.",
        title="Billingo: VAT rate cannot be determined",
    )


def _format_item_name(row):
    """
    "ITEMCODE - Item Name" for the Billingo line-item title. Falls back to
    whichever of code/name is present, and avoids "CODE - CODE" when there's
    no distinct item_name.
    """
    item_code = row.item_code
    item_name = row.item_name or item_code
    if item_code and item_code != item_name:
        return f"{item_code} \u2013 {item_name}"
    return item_name or item_code


def _get_serial_numbers(row):
    """
    Serial numbers for one Sales Invoice Item row. ERPNext v15+ has two ways
    these can be stored, depending on the Item's "Use Serial No / Batch
    Fields" setting:
      - legacy / use_serial_batch_fields checked: newline-separated text
        directly in row.serial_no
      - current default: a linked "Serial and Batch Bundle" document, whose
        child rows (doctype "Serial and Batch Entry") each carry one
        serial_no for this line
    Check both so this works regardless of which mode the items use.
    """
    if row.get("serial_no"):
        return [s.strip() for s in row.serial_no.split("\n") if s.strip()]

    bundle = row.get("serial_and_batch_bundle")
    if not bundle:
        return []

    return [
        s for s in frappe.get_all(
            "Serial and Batch Entry",
            filters={"parent": bundle},
            pluck="serial_no",
        )
        if s
    ]


def _get_item_comment(row):
    serial_numbers = _get_serial_numbers(row)
    if serial_numbers:
        return "S/N: " + ", ".join(serial_numbers)
    return ""


def _prices_include_vat(sales_invoice):
    """True when the invoice's VAT row is "included in Basic Rate".

    Webshops typically sell at gross prices.
    Their invoices use the gross tax template, where the 27% VAT row is
    "included in print rate": the item rates are then the gross (VAT
    inclusive) prices, exactly as the customer saw them in the webshop.
    Billingo must get them as gross prices too - computing back from an
    integer net price made the invoice total 1-2 Ft higher than the
    webshop order (e.g. 112 281 instead of 112 280).
    B2B invoices keep the net pricing: their VAT row is not included.
    """
    for tax_row in sales_invoice.get("taxes") or []:
        if tax_row.charge_type in PERCENTAGE_CHARGE_TYPES and tax_row.rate:
            return bool(tax_row.get("included_in_print_rate"))
    return False


# Units as an English-reading buyer expects them; anything not listed is
# passed through unchanged (kg, m, pcs ...).
ENGLISH_UNITS = {
    "db": "pcs",
    "darab": "pcs",
    "karton": "carton",
    "doboz": "box",
    "csomag": "pack",
    "tekercs": "roll",
    "pár": "pair",
    "készlet": "set",
}


def _unit_for(uom, is_hungarian):
    unit = uom or ("db" if is_hungarian else "pcs")
    if is_hungarian:
        return unit
    return ENGLISH_UNITS.get(unit.strip().casefold(), unit)


def _map_items(sales_invoice):
    vat_treatment = _get_vat_treatment(sales_invoice)
    unit_price_type = "gross" if _prices_include_vat(sales_invoice) else "net"
    is_hungarian = _get_invoice_language(sales_invoice) == "hu"
    items = []
    for row in sales_invoice.items:
        item = {
            "name": _format_item_name(row),
            "unit_price": row.rate,
            "unit_price_type": unit_price_type,
            "quantity": row.qty,
            "unit": _unit_for(row.uom, is_hungarian),
            "vat": vat_treatment["vat"],
            "comment": _get_item_comment(row),
        }
        if vat_treatment["entitlement"]:
            item["entitlement"] = vat_treatment["entitlement"]
        items.append(item)
    items.extend(_map_extra_charges(sales_invoice))
    return items


def _get_mode_of_payment(doc):
    for row in doc.get("payment_schedule") or []:
        if row.get("mode_of_payment"):
            return row.mode_of_payment
        term_name = row.get("payment_term")
        if not term_name:
            continue
        mode_of_payment = frappe.db.get_value("Payment Term", term_name, "mode_of_payment")
        if mode_of_payment:
            return mode_of_payment
    return None


def _map_payment_method(doc):
    mode_of_payment = _get_mode_of_payment(doc)
    return MODE_OF_PAYMENT_MAP.get(mode_of_payment, DEFAULT_PAYMENT_METHOD)


# Payment-term and mode-of-payment names are usually Hungarian; an English
# document must not show "Azonnal / 0 nap" or "Előreutalás".
ENGLISH_MODES_OF_PAYMENT = {
    "előreutalás": "Payment in advance (bank transfer)",
    "átutalás": "Bank transfer",
    "banki átutalás": "Bank transfer",
    "készpénz": "Cash",
    "utánvét": "Cash on delivery",
    "bankkártya": "Card",
    "kártya": "Card",
}


def _english_mode_of_payment(mode_of_payment):
    return ENGLISH_MODES_OF_PAYMENT.get((mode_of_payment or "").strip().casefold(), mode_of_payment)


def _english_payment_term(term_name, fallback):
    """'Due immediately' / 'Net 30 days' from the Payment Term's credit days."""
    if not term_name:
        return fallback
    days = frappe.db.get_value("Payment Term", term_name, "credit_days")
    if days is None:
        return fallback
    days = frappe.utils.cint(days)
    return "Due immediately" if days == 0 else f"Net {days} days"


def _build_payment_term_lines(doc, is_hungarian=False):
    rows = doc.get("payment_schedule") or []
    lines = []
    for row in rows:
        term_name = row.get("payment_term")
        description = row.get("description")
        mode_of_payment = None

        if term_name:
            term_fields = frappe.db.get_value(
                "Payment Term", term_name, ["description", "mode_of_payment"], as_dict=True
            )
            if term_fields:
                description = description or term_fields.get("description")
                mode_of_payment = term_fields.get("mode_of_payment")

        if not description and not term_name:
            continue

        detail = description or term_name
        if not is_hungarian:
            detail = _english_payment_term(term_name, detail)
        extras = []
        if row.get("invoice_portion"):
            due_word = "esedékes" if is_hungarian else "due"
            extras.append(f"{row.invoice_portion}% {due_word}")
        if row.get("due_date"):
            by_word = "eddig" if is_hungarian else "by"
            extras.append(f"{by_word} {row.due_date}")

        term_label = "Fizetési feltétel" if is_hungarian else "Payment Term"
        line = f"{term_label}: {detail}"
        if extras:
            line += f" ({', '.join(extras)})"
        lines.append(line)

        if mode_of_payment:
            mode_label = "Fizetési mód" if is_hungarian else "Mode of Payment"
            shown = mode_of_payment if is_hungarian else _english_mode_of_payment(mode_of_payment)
            lines.append(f"{mode_label}: {shown}")

    return lines


# The reason a 0% line carries no VAT. The VAT Act requires it on the invoice
# (Áfa tv. 169. § m), and Billingo did not print it for KBAET on its own
# (staging, 2026-10-02). English documents carry the EU reference and the
# Hungarian section, so the buyer and NAV both find their law.
EXEMPTION_NOTES = {
    "KBAET": (
        "Adómentes Közösségen belüli termékértékesítés – Áfa tv. 89. §",
        "VAT exempt intra-Community supply of goods – Article 138 of Directive "
        "2006/112/EC (Áfa tv. 89. §)",
    ),
    "EAM": (
        "Adómentes termékexport – Áfa tv. 98. §",
        "VAT exempt export of goods – Article 146 of Directive 2006/112/EC (Áfa tv. 98. §)",
    ),
    "TAM": (
        "Tárgyi adómentes értékesítés – Áfa tv. 85–86. §",
        "VAT exempt supply (Áfa tv. 85–86. §)",
    ),
    "EUFAD37": (
        "Fordított adózás – a szolgáltatás teljesítési helye az Áfa tv. 37. § szerint a vevő országa",
        "Reverse charge – Article 196 of Directive 2006/112/EC (Áfa tv. 37. §)",
    ),
}


def _exemption_note(doc, is_hungarian):
    """The legal reason for a 0% document, or None. Never fatal."""
    try:
        treatment = _get_vat_treatment(doc)
    except Exception:
        return None
    note = EXEMPTION_NOTES.get(treatment.get("note") or treatment.get("entitlement"))
    if not note:
        return None
    return note[0] if is_hungarian else note[1]


def _proforma_reference(doc, is_hungarian):
    """'Díjbekérő: 000015' on an invoice made from a Díjbekérő order, or None.

    Not required by law (a Díjbekérő is not a tax document), but the buyer
    paid against that number and matches the invoice by it. Never fatal.
    """
    if doc.get("doctype") != "Sales Invoice":
        return None
    try:
        from billingo.proforma import _order_state

        state, ambiguous = _order_state(doc)
    except Exception:
        return None
    number = None if ambiguous else state.get("custom_billingo_proforma_number")
    if not number:
        return None
    return f"Díjbekérő: {number}" if is_hungarian else f"Proforma invoice: {number}"


# Item weights may be kept in grams (weight UOM "gramm"); ERPNext's
# total_net_weight just adds the numbers up without a unit, so 3 452 g went to
# Billingo as "3452 kg". Convert per row.
WEIGHT_UOM_TO_KG = {
    "kg": 1.0,
    "kilogramm": 1.0,
    "kilogram": 1.0,
    "g": 0.001,
    "gr": 0.001,
    "gramm": 0.001,
    "gram": 0.001,
    "dkg": 0.01,
    "t": 1000.0,
    "tonna": 1000.0,
}


def _net_weight_kg(doc):
    """Total net weight in kg, or None if a weighed row has an unknown unit."""
    total = 0.0
    for row in doc.get("items") or []:
        per_unit = frappe.utils.flt(row.get("weight_per_unit"))
        if not per_unit:
            continue
        factor = WEIGHT_UOM_TO_KG.get((row.get("weight_uom") or "kg").strip().casefold())
        if factor is None:
            return None
        total += per_unit * frappe.utils.flt(row.get("stock_qty") or row.get("qty")) * factor
    return total


def _net_weight_line(doc, is_hungarian):
    kg = _net_weight_kg(doc)
    if not kg or kg <= 0:
        return None
    text = f"{round(kg, 3):.3f}".rstrip("0").rstrip(".")
    if is_hungarian:
        text = text.replace(".", ",")
    label = "Nettó tömeg" if is_hungarian else "Net weight"
    return f"{label}: {text} kg"


def _kvikk_gross_weight_kg(doc):
    """Lemért (bruttó) csomagsúly kg-ban a számlához tartozó szállítólevelek
    Kvikk-adataiból (lp_kvikk_weight_g, csak ha van Kvikk követőszám), vagy None.

    A Kvikk-ablakban feladás előtt beírt mért súly a dobozzal együtt értendő,
    a nettó tömeg pedig a termékadatokból számolódik — a számlán mindkettő
    szerepel. (Csak ha a Delivery Note-on van lp_kvikk_weight_g mező.)"""
    try:
        if not frappe.db.has_column("Delivery Note", "lp_kvikk_weight_g"):
            return None
        dns = {row.get("delivery_note") for row in (doc.get("items") or []) if row.get("delivery_note")}
        if doc.get("name"):
            dns.update(
                frappe.get_all(
                    "Delivery Note Item",
                    filters={"against_sales_invoice": doc.name, "docstatus": ["<", 2]},
                    pluck="parent",
                )
            )
        if not dns:
            return None
        rows = frappe.get_all(
            "Delivery Note",
            filters={"name": ["in", list(dns)], "docstatus": ["<", 2]},
            fields=["lp_kvikk_weight_g", "lp_kvikk_parcel_number"],
        )
        grams = sum(frappe.utils.flt(r.get("lp_kvikk_weight_g")) for r in rows if r.get("lp_kvikk_parcel_number"))
        return grams / 1000.0 if grams > 0 else None
    except Exception:
        return None


def _gross_weight_line(doc, is_hungarian):
    kg = _kvikk_gross_weight_kg(doc)
    if not kg or kg <= 0:
        return None
    text = f"{round(kg, 3):.3f}".rstrip("0").rstrip(".")
    if is_hungarian:
        text = text.replace(".", ",")
    label = "Bruttó tömeg (csomag)" if is_hungarian else "Gross weight (parcel)"
    return f"{label}: {text} kg"


def _build_comment(doc):
    is_hungarian = _get_invoice_language(doc) == "hu"

    if is_hungarian:
        lines = [f"ERPNext Számla {doc.name}"]
    else:
        lines = [f"ERPNext Sales Invoice {doc.name}"]

    # The buyer's own order number (PO). Large customers match invoices by it
    # and send back invoices without it (Epson Europe, PO 4600332741, 2026-11-09).
    # For webshop orders it is the order number from the cart or the webshop.
    if doc.get("po_no"):
        label = "Vevő rendelésszáma" if is_hungarian else "Customer PO / reference"
        lines.append(f"{label}: {doc.po_no}")

    # The customer's own free-text order note from the B2B webshop cart. Only
    # that note goes out - other remarks on the document are internal.
    remarks = frappe.utils.strip_html(doc.get("remarks") or "").strip()
    if remarks.startswith("Megrendelő megjegyzése:"):
        lines.append(remarks)

    exemption = _exemption_note(doc, is_hungarian)
    if exemption:
        lines.append(exemption)

    proforma_line = _proforma_reference(doc, is_hungarian)
    if proforma_line:
        lines.append(proforma_line)

    lines.extend(_build_payment_term_lines(doc, is_hungarian))

    # On an English document the Incoterm says how the goods are handed over;
    # the shipping rule's Hungarian name ("Helyi átvétel") only confused the
    # foreign buyer.
    if doc.get("shipping_rule") and (is_hungarian or not doc.get("incoterm")):
        label = "Szállítási mód" if is_hungarian else "Shipping Rule"
        lines.append(f"{label}: {doc.shipping_rule}")

    weight_line = _net_weight_line(doc, is_hungarian)
    if weight_line:
        lines.append(weight_line)

    gross_line = _gross_weight_line(doc, is_hungarian)
    if gross_line:
        lines.append(gross_line)

    if doc.get("incoterm"):
        incoterm_line = f"Incoterm: {doc.incoterm}"
        if doc.get("named_place"):
            incoterm_line += f" ({doc.named_place})"
        lines.append(incoterm_line)

    if doc.get("tc_name") or doc.get("terms"):
        lines.append("Általános Szerződési Feltételek" if is_hungarian else "Terms and Conditions")

    if doc.get("tc_name"):
        lines.append(doc.tc_name)

    if doc.get("terms"):
        plain_terms = frappe.utils.strip_html(doc.terms).strip()
        if plain_terms:
            snippet = plain_terms[:300]
            if len(plain_terms) > 300:
                snippet += "..."
            lines.append(snippet)

    return "\n".join(lines)


def _is_foreign_customer(customer_name):
    """True when the customer's billing address is outside Hungary.

    The same test that makes the Billingo partner 'Foreign'. Never fatal: a
    customer without a usable address keeps the default language here (the
    partner sync reports the missing address on its own).
    """
    if not customer_name:
        return False
    try:
        country_code = _get_customer_address(customer_name)[3]
    except Exception:
        return False
    return bool(country_code) and country_code.upper() != "HU"


def _get_invoice_language(doc):
    """
    Determines the Billingo document language. Invoices are
    Hungarian by default; an optional site setting can override this for the
    whole site without relying on individual Customer.language values.
    """
    language = str(frappe.conf.get("billingo_document_language", DEFAULT_DOCUMENT_LANGUAGE)).lower()
    if _is_foreign_customer(doc.get("customer")):
        # A foreign buyer gets an English document.
        language = str(frappe.conf.get("billingo_foreign_document_language", "en")).lower()
    supported_languages = {"de", "en", "fr", "hr", "hu", "it", "ro", "sk", "us"}
    if language in supported_languages:
        return language
    frappe.log_error(
        f"Unsupported billingo_document_language '{language}'; using Hungarian.",
        "Billingo Language Configuration",
    )
    return DEFAULT_DOCUMENT_LANGUAGE


def _get_billingo_bank_accounts():
    response = requests.get(
        f"{BILLINGO_BASE_URL}/bank-accounts",
        params={"per_page": 100},
        headers=_get_headers(),
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def _get_bank_account_id(doc):
    """Resolve a bank account from the API key's active Billingo profile."""
    currency = (doc.currency or "").upper()
    if not currency:
        return None

    accounts = _get_billingo_bank_accounts()
    config_key = f"billingo_bank_account_id_{currency.lower()}"
    configured_id = frappe.conf.get(config_key)

    if configured_id not in (None, ""):
        try:
            configured_id = int(configured_id)
        except (TypeError, ValueError):
            frappe.throw(f"{config_key} must be a positive integer.")

        if any(account.get("id") == configured_id for account in accounts):
            return configured_id
        frappe.throw(
            f"{config_key} ({configured_id}) is not available to the current Billingo API key. "
            "Update the site config or use an API key from the correct Billingo profile."
        )

    accounts_for_currency = [account for account in accounts if account.get("currency") == currency]
    preferred_fragment = PREFERRED_BANK_ACCOUNT_FRAGMENT_BY_CURRENCY.get(currency)
    preferred_accounts = [
        account
        for account in accounts_for_currency
        if preferred_fragment
        and preferred_fragment in (account.get("account_number") or "").replace(" ", "")
    ]
    if len(preferred_accounts) == 1:
        return preferred_accounts[0]["id"]

    if len(accounts_for_currency) == 1:
        return accounts_for_currency[0]["id"]

    if not accounts_for_currency:
        frappe.throw(
            f"No Billingo bank account for {currency} is available to the current API key. "
            "Add the account in the active Billingo profile before submitting this invoice."
        )

    frappe.throw(
        f"More than one Billingo bank account is available for {currency}; set {config_key} "
        "in Site Config to the required account ID."
    )


def _build_billingo_payload(doc, partner_id, doc_type, vendor_id=None):
    payload = {
        # Makes retries idempotent when Billingo created a document but the
        # HTTP response did not reach ERPNext.
        "vendor_id": vendor_id or _billingo_vendor_id(doc),
        "partner_id": partner_id,
        "block_id": _get_document_block_id(doc),
        "type": doc_type,          # "draft" while ERPNext is Draft, "invoice" on submit
        "fulfillment_date": str(doc.posting_date),
        "due_date": str(doc.due_date or doc.posting_date),
        "payment_method": _map_payment_method(doc),
        "language": _get_invoice_language(doc),
        "currency": doc.currency or "EUR",
        "conversion_rate": doc.conversion_rate or 1,
        # Enables Billingo's own invoice-delivery option ("Küldés Billingón
        # keresztül") for the electronic document.
        "electronic": True,
        "paid": False,
        "items": _map_items(doc),
        "comment": _build_comment(doc),
    }

    bank_account_id = _get_bank_account_id(doc)
    if bank_account_id:
        payload["bank_account_id"] = bank_account_id

    # Sales Order paid through a Díjbekérő. Locked only for the real invoice
    # (submit), never in the draft job: see advance_invoice_for.
    from billingo.proforma import advance_invoice_for, prepaid_proforma_for

    lock = doc_type == "invoice"
    # A deposit already invoiced as an advance is deducted by Billingo itself
    # and reported to NAV as an advance deduction.
    # Only on the real invoice: Billingo refuses advance_invoice on a draft.
    advance_id = advance_invoice_for(doc, for_update=lock) if doc_type == "invoice" else None
    if advance_id:
        payload["advance_invoice"] = [advance_id]
    elif prepaid_proforma_for(doc, for_update=lock):
        # 'Teljes tételes' Díjbekérő: the customer paid in full before invoicing.
        payload["paid"] = True

    return payload


def _build_modification_payload(doc):
    """Payload accepted by Billingo's linked credit-note endpoint."""
    return {
        "due_date": str(doc.due_date or doc.posting_date),
        "payment_method": _map_payment_method(doc),
        "without_financial_fulfillment": False,
        "items": _map_items(doc),
        "comment": _build_comment(doc),
    }


def _record_draft_error(doc, error_detail):
    # update_modified=False: the draft job runs right after the user saves,
    # and bumping "modified" made the open form stale - the next Submit then
    # failed with "modified after you have opened it".
    doc.db_set("custom_billingo_sync_status", "Failed", update_modified=False)
    doc.db_set("custom_billingo_error", error_detail[:500], update_modified=False)
    frappe.log_error(error_detail, "Billingo Draft Sync Error")


def _raise_finalization_error(doc, error_detail):
    """Log the remote error and abort the ERPNext submit transaction."""
    frappe.log_error(error_detail, "Billingo Finalization Error")
    frappe.throw(
        f"Billingo finalization failed for Sales Invoice {doc.name}: {error_detail}",
        title="Billingo finalization failed",
    )


def _mark_document_synced(doc, result):
    # update_modified=False: also called from the draft job (see _record_draft_error).
    doc.db_set("custom_billingo_document_id", result.get("id"), update_modified=False)
    doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"), update_modified=False)
    doc.db_set("custom_billingo_sync_status", "Synced", update_modified=False)
    doc.db_set("custom_billingo_error", "", update_modified=False)


def _send_document_via_billingo(doc, result, raise_on_error=False):
    """Ask Billingo to email a finalized document to the ERPNext contact."""
    document_id = result.get("id")
    email = _get_customer_email(doc.customer)
    if not document_id or not email:
        message = (
            f"Billingo delivery cannot be completed for Sales Invoice {doc.name}: "
            "the Customer needs a linked Contact with an email address."
        )
        if raise_on_error:
            frappe.throw(message)
        frappe.log_error(title="Billingo Send Skipped", message=message)
        return None

    try:
        response = requests.post(
            f"{BILLINGO_BASE_URL}/documents/{document_id}/send",
            json={"emails": [email]},
            headers=_get_headers(),
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        frappe.log_error(title="Billingo Send Error", message=error_detail)
        if raise_on_error:
            frappe.throw(f"Billingo delivery failed: {error_detail}")
    except Exception:
        error_detail = frappe.get_traceback()
        frappe.log_error(title="Billingo Send Error", message=error_detail)
        if raise_on_error:
            frappe.throw("Billingo delivery failed. Check the Error Log for details.")
    return None


# ----------------------------------------------------------------------
# Draft push (ERPNext Draft -> Billingo draft)
# ----------------------------------------------------------------------

def _billingo_sync_skipped(doc):
    """True when this invoice must not be pushed to Billingo at all.

    The "Billingo-szinkron kihagyása" checkbox
    (``custom_billingo_skip_sync``) marks a Sales Invoice whose Billingo
    invoice already exists - e.g. its ERPNext record was cancelled while the
    Billingo invoice stayed valid, and it is being entered again. Nothing is
    created, changed or sent in Billingo for such an invoice; the existing
    number is typed into ``custom_billingo_invoice_number``.
    """
    try:
        return bool(int(doc.get("custom_billingo_skip_sync") or 0))
    except (TypeError, ValueError):
        return False


def queue_billingo_draft_sync(doc, method=None):
    """Queue one sync after the Sales Invoice transaction has committed.

    A Billingo document is an external side effect. Running it from an
    ``after_insert``/``on_update`` hook and committing manually can leave the
    ERPNext transaction partially persisted. ``enqueue_after_commit`` avoids
    creating a draft until the invoice and its fields are durable.
    """
    if doc.docstatus != 0 or not doc.customer or doc.is_return:
        return
    if _billingo_sync_skipped(doc):
        return

    guard_flag = f"billingo_draft_sync_queued_{doc.name}"
    if frappe.flags.get(guard_flag):
        return
    frappe.flags[guard_flag] = True

    frappe.enqueue(
        "billingo.api.sync_billingo_draft_for_invoice",
        queue="short",
        enqueue_after_commit=True,
        sales_invoice=doc.name,
    )


def sync_billingo_draft_for_invoice(sales_invoice):
    """Worker entrypoint used by ``queue_billingo_draft_sync``."""
    # p- NEW: skip if the invoice was submitted/cancelled while this job
    # p- was waiting in the queue (docstatus 0 = still Draft).
    if frappe.db.get_value("Sales Invoice", sales_invoice, "docstatus") != 0:
        return
    sync_billingo_draft(frappe.get_doc("Sales Invoice", sales_invoice))


def sync_billingo_draft(doc, method=None):
    """
    doc_events hook: Sales Invoice after_insert + on_update.
    Only acts while the ERPNext invoice is still a Draft (docstatus 0).
    """
    if doc.docstatus != 0:
        return
    if not doc.customer:
        return
    if doc.is_return:
        # Billingo returns are linked to their submitted originals and cannot
        # be independent negative drafts.
        return
    if _billingo_sync_skipped(doc):
        # Already issued in Billingo - never create or touch a draft for it.
        return

    from billingo.proforma import invoice_draft_blocked

    blocked = invoice_draft_blocked(doc)
    if blocked is not None:
        # The invoice will deduct an advance and is created at submit (skip
        # quietly), or it mixes several orders with Billingo state (record why
        # nothing was sent).
        if blocked:
            _record_draft_error(doc, blocked)
        elif doc.get("custom_billingo_sync_status") == "Failed":
            # A draft attempt made before this rule existed left an error.
            doc.db_set(
                {"custom_billingo_sync_status": "", "custom_billingo_error": ""},
                update_modified=False,
            )
        return

    try:
        partner_id = _get_or_create_partner(doc.customer)
        payload = _build_billingo_payload(doc, partner_id, "draft")

        existing_id = doc.get("custom_billingo_document_id")

        if not existing_id:
            # Billingo enforces vendor_id uniqueness, and keeps enforcing it
            # for documents this site has lost track of (a rolled back
            # db_set, a half finished earlier save). Ask before creating:
            # otherwise the POST below fails with "Duplicate vendor id" and
            # this invoice can never sync again.
            existing_document = _get_document_by_vendor_id(_billingo_vendor_id(doc))
            if existing_document:
                if existing_document.get("type") != "draft":
                    # A finalized document already carries this name. Do not
                    # adopt it: Frappe hands a deleted document's name to the
                    # next one (revert_series_if_last), so this may well be a
                    # different invoice's issued document. Linking them
                    # silently would attach a real invoice to the wrong
                    # record, so stop and make a human look.
                    _record_draft_error(
                        doc,
                        f"Billingo already has a finalized document "
                        f"(id={existing_document.get('id')}, "
                        f"invoice_number={existing_document.get('invoice_number')}) under the name "
                        f"'{doc.name}'. It may belong to an earlier, deleted invoice that reused "
                        "this name. Check it in Billingo before syncing this Sales Invoice.",
                    )
                    return

                # Adopt the draft rather than delete-and-recreate. Billingo
                # appears to keep a vendor_id reserved even after its
                # document is deleted, so re-creating under the same name is
                # not reliably possible. Contents stay correct: the next save
                # refreshes the draft, and on submit the PUT sends the full
                # current payload.
                _mark_document_synced(doc, existing_document)
                frappe.log_error(
                    title="Billingo Draft Sync - Adopted Draft",
                    message=(
                        f"Adopted an existing Billingo draft for Sales Invoice {doc.name} "
                        f"(id={existing_document.get('id')}) instead of pushing a new one."
                    ),
                )
                return

        # If a draft already exists in Billingo, it has to be deleted
        # and recreated - Billingo has no endpoint to edit a draft's
        # contents in place.
        if existing_id:
            delete_response = requests.delete(
                f"{BILLINGO_BASE_URL}/documents/{existing_id}",
                headers=_get_headers(),
                timeout=15,
            )
            # 404 just means it's already gone (e.g. manually removed
            # in Billingo) - fine to proceed and create a fresh one.
            if delete_response.status_code not in (204, 404):
                delete_response.raise_for_status()

        try:
            result = _create_billingo_document(payload)
        except requests.exceptions.HTTPError as e:
            error_detail = _get_error_text(e.response) if e.response is not None else str(e)
            if "duplicate vendor id" not in (error_detail or "").casefold():
                raise
            # The name is still taken. This is the delete-and-recreate path's
            # own hazard: the draft has just been removed, so failing here
            # would leave the invoice with a stale ID and nothing behind it.
            recovered = _get_document_by_vendor_id(_billingo_vendor_id(doc))
            if recovered and recovered.get("type") == "draft":
                _mark_document_synced(doc, recovered)
                frappe.log_error(
                    title="Billingo Draft Sync - Recovered Draft",
                    message=(
                        f"Recovered the Billingo draft for Sales Invoice {doc.name} after a "
                        f"duplicate vendor_id on re-create (id={recovered.get('id')})."
                    ),
                )
                return
            _record_draft_error(doc, _explain_duplicate_vendor_id(doc, error_detail))
            return

        doc.db_set("custom_billingo_document_id", result.get("id"), update_modified=False)
        doc.db_set("custom_billingo_sync_status", "Synced", update_modified=False)
        doc.db_set("custom_billingo_error", "", update_modified=False)
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        _record_draft_error(doc, _explain_duplicate_vendor_id(doc, error_detail))

    except Exception as e:
        _record_draft_error(doc, str(e))
        frappe.log_error(title="Billingo Draft Sync Error", message=frappe.get_traceback())


def _create_billingo_document(payload):
    response = requests.post(
        f"{BILLINGO_BASE_URL}/documents",
        json=payload,
        headers=_get_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def _explain_duplicate_vendor_id(doc, error_detail):
    """Turn Billingo's bare duplicate error into something actionable.

    Reached when Billingo still reserves this invoice's name but returns no
    usable document for it - typically because an earlier document under that
    name was deleted. Nothing in this app can recover the name, so say what
    the operator actually has to do instead of logging raw JSON.
    """
    if "duplicate vendor id" not in (error_detail or "").casefold():
        return error_detail
    return (
        f"Billingo still reserves the document name '{doc.name}' from an earlier attempt, "
        "and no draft can be retrieved under it (it was most likely deleted in Billingo). "
        "This Sales Invoice cannot be synced under this name: duplicate it and use the copy, "
        f"which gets a new name. Billingo response: {error_detail}"
    )


# ----------------------------------------------------------------------
# Submit (Billingo draft -> Billingo invoice, SAME id)
# ----------------------------------------------------------------------

def _drop_draft_for_advance(doc, billingo_id):
    """Delete this invoice's Billingo draft, if any; return the vendor_id to create under.

    Normally there is no draft: the draft job skips invoices that will deduct an
    advance. One exists only when the invoice was drafted before the deposit
    arrived. Billingo keeps a deleted document's vendor_id reserved, so an
    invoice that deducts an advance is always created under a suffixed one
    (also when no draft existed - one rule, and a retry after a lost commit
    then meets its own invoice and stops instead of issuing twice). Only drafts are ever deleted; anything
    else under this invoice's reference stops the submit.
    """
    base_vendor_id = _billingo_vendor_id(doc)
    candidates = []
    if billingo_id:
        try:
            candidates.append(_get_document(billingo_id))
        except requests.exceptions.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
    else:
        # No retry: the draft job does not run for these invoices, so there is
        # no in-flight draft POST to wait for.
        found = _get_document_by_vendor_id(base_vendor_id)
        if found:
            candidates.append(found)

    if not candidates:
        return f"{base_vendor_id}-e"

    document = candidates[0]
    if (document.get("type") or "").casefold() != "draft":
        frappe.throw(
            f"Billingo already has a finalized document (id={document.get('id')}, "
            f"invoice_number={document.get('invoice_number')}) for '{doc.name}'. "
            "Check it in Billingo before submitting this Sales Invoice.",
            title="Billingo: a document already exists under this name",
        )

    response = requests.delete(
        f"{BILLINGO_BASE_URL}/documents/{document.get('id')}",
        headers=_get_headers(),
        timeout=15,
    )
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()
    return f"{base_vendor_id}-e"


def finalize_billingo_invoice(doc, method=None):
    """
    doc_events hook: Sales Invoice on_submit.
    Converts the existing Billingo draft into a real invoice in place.
    """
    if _billingo_sync_skipped(doc):
        # The Billingo invoice already exists: record it in ERPNext only.
        number = (doc.get("custom_billingo_invoice_number") or "").strip()
        if not number:
            frappe.throw(
                f"Sales Invoice {doc.name}: 'Billingo-szinkron kihagyása' is ticked, so the "
                "existing Billingo invoice number must be entered in 'Billingo Invoice Number' "
                "before submitting.",
                title="Billingo: missing invoice number",
            )
        doc.db_set(
            {"custom_billingo_sync_status": "Synced", "custom_billingo_error": ""},
            update_modified=False,
        )
        doc.add_comment(
            "Info",
            f"Billingo-szinkron kihagyva: a számla már ki volt állítva a Billingóban ({number}). "
            "Az ERPNext nem hozott létre, nem módosított és nem küldött ki semmit.",
        )
        return

    try:
        # Billingo delivery needs an explicit recipient in its separate /send
        # request. Validate first so the document is never finalized with
        # "No sending" selected due to a missing recipient.
        if not _get_customer_email(doc.customer):
            frappe.throw(
                f"Cannot submit Sales Invoice {doc.name}: Customer '{doc.customer}' needs a "
                "linked Contact with an email address for Billingo delivery."
            )

        if doc.is_return:
            original_billingo_id = frappe.db.get_value(
                "Sales Invoice", doc.return_against, "custom_billingo_document_id"
            )
            if not original_billingo_id:
                frappe.throw(
                    f"Cannot create a Billingo credit note for {doc.name}: the original "
                    f"Sales Invoice {doc.return_against or '(missing)'} has no Billingo document ID."
                )

            response = requests.post(
                f"{BILLINGO_BASE_URL}/documents/{original_billingo_id}/create-modification-document",
                json=_build_modification_payload(doc),
                headers=_get_headers(),
                timeout=20,
            )
            response.raise_for_status()
            result = response.json()

            doc.db_set("custom_billingo_document_id", result.get("id"))
            doc.db_set("custom_billingo_invoice_number", result.get("invoice_number"))
            doc.db_set("custom_billingo_sync_status", "Synced")
            doc.db_set("custom_billingo_error", "")
            _send_document_via_billingo(doc, result)
            return

        # Orders with Billingo advance / Díjbekérő state: refuse an invoice that
        # mixes several of them, and lock the order for the rest of the submit.
        from billingo.proforma import (
            check_invoice_orders,
            mark_advance_consumed,
            settle_prepaid_proforma,
        )

        check_invoice_orders(doc)

        # p- CHANGED (fix 1): re-read the draft id from the DB with a locking
        # p- read, so we get the latest committed value instead of relying on
        # p- the stale in-memory doc. Falls back to the in-memory value.
        billingo_id = frappe.db.get_value(
            "Sales Invoice", doc.name, "custom_billingo_document_id", for_update=True
        ) or doc.get("custom_billingo_document_id")

        partner_id = _get_or_create_partner(doc.customer)
        payload = _build_billingo_payload(doc, partner_id, "invoice")

        if payload.get("advance_invoice"):
            # Billingo will not turn a draft into an invoice that deducts an
            # advance, so any draft of this invoice is removed and the invoice
            # is created whole (see _drop_draft_for_advance).
            payload["vendor_id"] = _drop_draft_for_advance(doc, billingo_id)
            billingo_id = None
        elif not billingo_id:
            # No draft ID is stored - either nothing was ever pushed, or the
            # draft sync lost the ID (or the draft job is still running).
            # Billingo may still hold a document under this invoice's name,
            # so look before creating.
            # p- CHANGED: retrying lookup, because Billingo can lag behind a
            # p- draft POST that is still in flight.
            existing_document = _get_document_by_vendor_id_with_retry(_billingo_vendor_id(doc))
            if existing_document:
                if existing_document.get("type") == "draft":
                    # This invoice's own pending draft. Finalize it; marking
                    # the invoice Synced here would leave the draft unissued
                    # and email an unnumbered document to the customer.
                    billingo_id = existing_document.get("id")
                    payload["vendor_id"] = (
                        existing_document.get("vendor_id") or payload["vendor_id"]
                    )
                else:
                    # Already finalized under this name. Frappe reassigns a
                    # deleted document's name to the next one
                    # (revert_series_if_last), so this may be another
                    # invoice's issued document - never link or send it
                    # automatically.
                    frappe.throw(
                        f"Billingo already has a finalized document "
                        f"(id={existing_document.get('id')}, "
                        f"invoice_number={existing_document.get('invoice_number')}) under the name "
                        f"'{doc.name}'. It may belong to an earlier, deleted invoice that reused "
                        "this name. Check it in Billingo before submitting this Sales Invoice.",
                        title="Billingo: a document already exists under this name",
                    )
        else:
            # Existing drafts created before immutable vendor IDs were
            # introduced retain their original reference: vendor_id may be
            # immutable in Billingo and must never be changed at finalization.
            # p- CHANGED: now runs whenever an id is known (DB or in-memory).
            stored_document = _get_document(billingo_id)
            payload["vendor_id"] = (
                stored_document.get("vendor_id") or payload["vendor_id"]
            )

        if billingo_id:
            # PUT /documents/{id} converts the draft into an invoice -
            # same Billingo id, now finalized and numbered.
            response = requests.put(
                f"{BILLINGO_BASE_URL}/documents/{billingo_id}",
                json=payload,
                headers=_get_headers(),
                timeout=20,
            )
        else:
            # Nothing exists in Billingo for this invoice yet.
            response = requests.post(
                f"{BILLINGO_BASE_URL}/documents",
                json=payload,
                headers=_get_headers(),
                timeout=20,
            )
            # p- NEW (fix 2): the queued draft job may have created the draft
            # p- between our lookup and this POST -> "Duplicate vendor id".
            # p- Instead of failing, find that draft and PUT to it.
            if (
                response.status_code >= 400
                and "duplicate vendor id" in _get_error_text(response).casefold()
            ):
                racing = _get_document_by_vendor_id_with_retry(payload["vendor_id"])
                if racing and racing.get("type") == "draft":
                    payload["vendor_id"] = racing.get("vendor_id") or payload["vendor_id"]
                    response = requests.put(
                        f"{BILLINGO_BASE_URL}/documents/{racing['id']}",
                        json=payload,
                        headers=_get_headers(),
                        timeout=20,
                    )
                # p- If it is already finalized (not a draft) we fall through
                # p- to raise_for_status() and fail on purpose (name-reuse safety).
        response.raise_for_status()
        result = response.json()

        _mark_document_synced(doc, result)
        if payload.get("advance_invoice"):
            # Same transaction as the submit: the advance counts as deducted
            # exactly when this invoice becomes real.
            mark_advance_consumed(doc, payload["advance_invoice"][0])
        settle_error = settle_prepaid_proforma(
            doc, payload["payment_method"], payload["conversion_rate"]
        )
        if settle_error:
            frappe.msgprint(
                f"The invoice was issued, but its Díjbekérő could not be marked paid in Billingo: "
                f"{settle_error}",
                title="Billingo: mark the Díjbekérő paid by hand",
                indicator="orange",
            )
        _send_document_via_billingo(doc, result)

    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        _raise_finalization_error(doc, error_detail)

    except Exception as e:
        _raise_finalization_error(doc, f"{e}\n\n{frappe.get_traceback()}")


@frappe.whitelist()
def refresh_billingo_reconciliation(sales_invoice: str) -> dict:
    """Manually refresh Billingo's read-only NAV/cancellation facts."""
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    doc.check_permission("read")
    try:
        return _reconcile_billingo_document(doc)
    except requests.exceptions.HTTPError as e:
        error_detail = _get_error_text(e.response) if e.response is not None else str(e)
        frappe.log_error(
            title="Billingo Reconciliation Error",
            message=f"Sales Invoice {doc.name}: {error_detail}",
        )
        frappe.throw(f"Billingo reconciliation failed: {error_detail}")


def reconcile_billingo_documents():
    """Daily, read-only Billingo reconciliation for issued ERPNext invoices."""
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 1,
            "custom_billingo_document_id": ["is", "set"],
        },
        pluck="name",
    )
    for invoice_name in invoices:
        try:
            _reconcile_billingo_document(frappe.get_doc("Sales Invoice", invoice_name))
        except Exception:
            # Reconciliation must not interrupt other invoices or modify their
            # accounting state. Record failures for follow-up and continue.
            frappe.log_error(
                title="Billingo Reconciliation Error",
                message=(
                    f"Sales Invoice {invoice_name}:\n{frappe.get_traceback()}"
                ),
            )


@frappe.whitelist()
def retry_billingo_sync(sales_invoice: str) -> dict:
    """Manually retry Billingo finalization for a submitted failed invoice."""
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    doc.check_permission("write")

    if doc.docstatus != 1:
        frappe.throw("Only submitted Sales Invoices can be retried.")
    if doc.get("custom_billingo_sync_status") != "Failed":
        frappe.throw("Billingo retry is only available when the sync status is Failed.")

    finalize_billingo_invoice(doc)
    return {
        "document_id": doc.get("custom_billingo_document_id"),
        "invoice_number": doc.get("custom_billingo_invoice_number"),
    }


@frappe.whitelist()
def send_billingo_invoice(sales_invoice: str) -> dict:
    """Send an already-issued Billingo invoice to the linked Customer Contact."""
    doc = frappe.get_doc("Sales Invoice", sales_invoice)
    doc.check_permission("write")

    if doc.docstatus != 1:
        frappe.throw("Only submitted Sales Invoices can be sent through Billingo.")
    if not doc.get("custom_billingo_document_id"):
        frappe.throw("This Sales Invoice has no Billingo document ID.")

    email = _get_customer_email(doc.customer)
    if not email:
        frappe.throw(
            f"Customer '{doc.customer}' needs a linked Contact with an email address before sending."
        )

    _send_document_via_billingo(
        doc, {"id": doc.custom_billingo_document_id}, raise_on_error=True
    )
    return {"email": email, "invoice_number": doc.get("custom_billingo_invoice_number")}
