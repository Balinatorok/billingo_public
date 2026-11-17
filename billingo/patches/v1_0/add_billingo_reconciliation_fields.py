"""Add read-only Billingo reconciliation fields to Sales Invoice."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    create_custom_fields(
        {
            "Sales Invoice": [
                {
                    "fieldname": "custom_billingo_reconciliation_section",
                    "label": "Billingo reconciliation (read-only)",
                    "fieldtype": "Section Break",
                    "insert_after": "custom_billingo_error",
                },
                {
                    "fieldname": "custom_billingo_nav_status",
                    "label": "Billingo NAV status",
                    "fieldtype": "Data",
                    "read_only": 1,
                    "allow_on_submit": 1,
                    "insert_after": "custom_billingo_reconciliation_section",
                },
                {
                    "fieldname": "custom_billingo_nav_checked_at",
                    "label": "Billingo NAV checked at",
                    "fieldtype": "Datetime",
                    "read_only": 1,
                    "allow_on_submit": 1,
                    "insert_after": "custom_billingo_nav_status",
                },
                {
                    "fieldname": "custom_billingo_nav_messages",
                    "label": "Billingo NAV messages",
                    "fieldtype": "Small Text",
                    "read_only": 1,
                    "allow_on_submit": 1,
                    "insert_after": "custom_billingo_nav_checked_at",
                },
                {
                    "fieldname": "custom_billingo_direct_cancellation_detected",
                    "label": "Cancelled directly in Billingo",
                    "fieldtype": "Check",
                    "read_only": 1,
                    "allow_on_submit": 1,
                    "insert_after": "custom_billingo_nav_messages",
                },
                {
                    "fieldname": "custom_billingo_cancellation_details",
                    "label": "Billingo cancellation details",
                    "fieldtype": "Small Text",
                    "read_only": 1,
                    "allow_on_submit": 1,
                    "insert_after": "custom_billingo_direct_cancellation_detected",
                },
            ]
        },
        update=True,
    )
