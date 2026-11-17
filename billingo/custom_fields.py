"""Custom fields this app needs on Sales Order.

Created in code rather than shipped as a fixture: `create_custom_fields` is
idempotent, so wiring it to `after_migrate` keeps every site's fields in step
with the app without a fixture export ever going stale.

The Sales Invoice, Customer and Cost Center fields (BILLINGO_OTHER_FIELDS)
are defined here too, so a fresh install is self-sufficient. The reconciliation
fields come from the v1_0 patch, which is also called on every migrate because
patches are not executed on a fresh install.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

BILLINGO_SALES_ORDER_FIELDS = {
    "Sales Order": [
        {
            "fieldname": "custom_billingo_details",
            "label": "Billingo",
            "fieldtype": "Section Break",
            "insert_after": "tc_name",
            "collapsible": 1,
        },
        {
            "fieldname": "custom_billingo_proforma_mode",
            "label": "Billingo Díjbekérő Mode",
            "fieldtype": "Select",
            "options": "\nElőleg\nTeljes tételes",
            "insert_after": "custom_billingo_details",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_proforma_id",
            "label": "Billingo Díjbekérő ID",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_proforma_mode",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_proforma_number",
            "label": "Billingo Díjbekérő Number",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_proforma_id",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_proforma_due_date",
            "label": "Billingo Díjbekérő Payment Deadline",
            "fieldtype": "Date",
            "insert_after": "custom_billingo_proforma_number",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            # 'Teljes tételes': the Sales Invoice whose submit issued the
            # Billingo invoice and marked the Díjbekérő paid. Data, not Link:
            # a Link from this submitted order would block cancelling it.
            "fieldname": "custom_billingo_proforma_paid_by",
            "label": "Billingo Díjbekérő Paid With Invoice",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_proforma_due_date",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            # Withdrawn Díjbekérők of this order. Billingo keeps a vendor_id
            # reserved for good, so each new Díjbekérő needs its own.
            "fieldname": "custom_billingo_proforma_count",
            "label": "Billingo Withdrawn Díjbekérő Count",
            "fieldtype": "Int",
            "insert_after": "custom_billingo_proforma_paid_by",
            "read_only": 1,
            "hidden": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_column_break_1",
            "fieldtype": "Column Break",
            "insert_after": "custom_billingo_proforma_count",
        },
        {
            "fieldname": "custom_billingo_advance_description",
            "label": "Billingo Advance Description",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_column_break_1",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
            "depends_on": "eval:doc.custom_billingo_proforma_mode=='Előleg'",
        },
        {
            "fieldname": "custom_billingo_advance_amount",
            "label": "Billingo Advance Amount",
            "fieldtype": "Currency",
            "options": "currency",
            "insert_after": "custom_billingo_advance_description",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
            "depends_on": "eval:doc.custom_billingo_proforma_mode=='Előleg'",
        },
        {
            "fieldname": "custom_billingo_advance_vat_rate",
            "label": "Billingo Advance VAT Rate",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_advance_amount",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
            "depends_on": "eval:doc.custom_billingo_proforma_mode=='Előleg'",
        },
        {
            "fieldname": "custom_billingo_advance_invoice_id",
            "label": "Billingo Előlegszámla ID",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_advance_vat_rate",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_advance_invoice_number",
            "label": "Billingo Előlegszámla Number",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_advance_invoice_id",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            # An advance may only be deducted once. ERPNext invoices one
            # order in as many parts as the deliveries take, so without this
            # every invoice would carry the same deduction.
            # Data, not Link: a Link from this submitted order would block
            # cancelling the invoice it names (LinkExistsError).
            "fieldname": "custom_billingo_advance_consumed_by",
            "label": "Billingo Advance Deducted On",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_advance_invoice_number",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            # The Payment Entry that issued the advance invoice (Data: a Link
            # would block cancelling that payment).
            "fieldname": "custom_billingo_advance_payment_entry",
            "label": "Billingo Advance Paid By",
            "fieldtype": "Data",
            "insert_after": "custom_billingo_advance_consumed_by",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_sync_status",
            "label": "Billingo Sync Status",
            "fieldtype": "Select",
            "options": "\nPending\nSynced\nFailed",
            "insert_after": "custom_billingo_advance_payment_entry",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_billingo_error",
            "label": "Billingo Error",
            "fieldtype": "Small Text",
            "insert_after": "custom_billingo_sync_status",
            "read_only": 1,
            "allow_on_submit": 1,
            "no_copy": 1,
            "depends_on": "eval:doc.custom_billingo_error",
        },
    ]
}


BILLINGO_OTHER_FIELDS = {
    "Customer": [
        {
            "fieldname": "custom_billingo_partner_id",
            "label": "Billingo Partner id",
            "fieldtype": "Data",
            "insert_after": "territory",
        },
    ],
    "Cost Center": [
        {
            "fieldname": "custom_billingo_szamlatomb",
            "label": "Billingo számlatömb",
            "fieldtype": "Link",
            "options": "Billingo Szamlatomb",
            "insert_after": "parent_cost_center",
            "description": "Az ezen a költséghelyen kiállított számlák ebből a Billingo-számlatömbből mennek. Üres = a sima (alapértelmezett) tömb.",
        },
    ],
    "Sales Order": [
        {
            "fieldname": "custom_billingo_szamlatomb",
            "label": "Billingo számlatömb",
            "fieldtype": "Link",
            "options": "Billingo Szamlatomb",
            "fetch_from": "cost_center.custom_billingo_szamlatomb",
            "insert_after": "cost_center",
            "description": "A költséghelyből töltődik; átírható. Üres = sima számlatömb.",
        },
    ],
    "Sales Invoice": [
        {
            "fieldname": "custom_billingo_szamlatomb",
            "label": "Billingo számlatömb",
            "fieldtype": "Link",
            "options": "Billingo Szamlatomb",
            "fetch_from": "cost_center.custom_billingo_szamlatomb",
            "insert_after": "due_date",
            "in_standard_filter": 1,
            "description": "Ebből a Billingo-számlatömbből megy a számla. A költséghelyből töltődik, átírható. Üres = sima számlatömb.",
        },
        {
            "fieldname": "custom_billingo_details",
            "label": "Billingo Details",
            "fieldtype": "Section Break",
            "insert_after": "is_discounted",
        },
        {
            "fieldname": "custom_billingo_document_id",
            "label": "Billingo Document ID",
            "fieldtype": "Data",
            "no_copy": 1,
            "insert_after": "custom_billingo_details",
        },
        {
            "fieldname": "custom_billingo_invoice_number",
            "label": "Billingo Invoice Number",
            "fieldtype": "Data",
            "no_copy": 1,
            "allow_on_submit": 1,
            "in_list_view": 1,
            "in_standard_filter": 1,
            "insert_after": "custom_billingo_document_id",
        },
        {
            "fieldname": "custom_billingo_skip_sync",
            "label": "Billingo-szinkron kihagyása (már kiállított számla)",
            "fieldtype": "Check",
            "no_copy": 1,
            "insert_after": "custom_billingo_invoice_number",
            "description": "Csak akkor pipáld be, ha ez a számla a Billingóban már ki van állítva. Ilyenkor az ERP semmit nem küld a Billingónak; a meglévő Billingo-számlaszámot be kell írni a „Billingo Invoice Number” mezőbe.",
        },
        {
            "fieldname": "custom_billingo_column_break_si",
            "fieldtype": "Column Break",
            "insert_after": "custom_billingo_skip_sync",
        },
        {
            "fieldname": "custom_billingo_sync_status",
            "label": "Billingo Sync Status",
            "fieldtype": "Select",
            "options": "\nPending\nSynced\nFailed",
            "no_copy": 1,
            "allow_on_submit": 1,
            "insert_after": "custom_billingo_column_break_si",
        },
        {
            "fieldname": "custom_billingo_error",
            "label": "Billingo Error",
            "fieldtype": "Small Text",
            "no_copy": 1,
            "allow_on_submit": 1,
            "insert_after": "custom_billingo_sync_status",
        },
    ],
}


# Fields of an earlier test version (create-from-proforma flow), removed again.
OBSOLETE_SALES_ORDER_FIELDS = (
    "custom_billingo_full_invoice_id",
    "custom_billingo_full_invoice_number",
    "custom_billingo_full_invoice_adopted_by",
    "custom_billingo_conversion_pending",
)


def create_billingo_custom_fields():
    """Idempotent: safe to run on every migrate (wired to after_migrate)."""
    from billingo.patches.v1_0.add_billingo_reconciliation_fields import execute as add_reconciliation_fields

    create_custom_fields(BILLINGO_OTHER_FIELDS, ignore_validate=True)
    add_reconciliation_fields()
    # Sales Order fields last (the offline test inspects the last call).
    create_custom_fields(BILLINGO_SALES_ORDER_FIELDS, ignore_validate=True)
    for fieldname in OBSOLETE_SALES_ORDER_FIELDS:
        name = f"Sales Order-{fieldname}"
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc("Custom Field", name, ignore_permissions=True)
