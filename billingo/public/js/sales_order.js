// Billingo actions on the Sales Order.
//
// Raising a Díjbekérő is a deliberate click. Afterwards the ordinary ERPNext
// steps drive Billingo: recording the payment issues the advance invoice
// (Előleg), and submitting the Sales Invoice issues the final invoice.

frappe.ui.form.on("Sales Order", {
    refresh(frm) {
        if (frm.doc.docstatus !== 1) {
            return;
        }

        if (frm.doc.custom_billingo_sync_status === "Failed" && frm.doc.custom_billingo_error) {
            frm.dashboard.set_headline_alert(
                __("Billingo hiba: {0}", [frappe.utils.escape_html(frm.doc.custom_billingo_error)]),
                "red"
            );
        }

        if (!frm.doc.custom_billingo_proforma_id) {
            frm.add_custom_button(
                __("Díjbekérő létrehozása"),
                () => open_proforma_dialog(frm),
                __("Billingo")
            );
            return;
        }

        const mode = frm.doc.custom_billingo_proforma_mode;
        const paid =
            frm.doc.custom_billingo_advance_invoice_id || frm.doc.custom_billingo_proforma_paid_by;

        if (!paid) {
            if (mode === "Előleg") {
                // Normally the advance invoice is issued when the payment is
                // recorded (Payment Entry against this order); this is the
                // fallback for a payment recorded without that link.
                frm.add_custom_button(
                    __("Előlegszámla kiállítása kézzel"),
                    () => open_convert_dialog(frm),
                    __("Billingo")
                );
            }
            frm.add_custom_button(
                __("Díjbekérő visszavonása"),
                () => withdraw_proforma(frm),
                __("Billingo")
            );
        }

        // One message: add_comment replaces the headline rather than
        // appending, so two calls would hide the first.
        const notes = [];
        let color = "blue";
        if (!paid) {
            const due = frm.doc.custom_billingo_proforma_due_date;
            const overdue = due && due < frappe.datetime.get_today();
            notes.push(
                __("Díjbekérő: {0} ({1}){2} — {3}.", [
                    frm.doc.custom_billingo_proforma_number || frm.doc.custom_billingo_proforma_id,
                    mode,
                    due ? __(", fizetési határidő {0}", [frappe.datetime.str_to_user(due)]) : "",
                    overdue ? __("lejárt, még nincs rögzítve fizetés") : __("fizetésre vár"),
                ])
            );
            notes.push(
                mode === "Előleg"
                    ? __("Ha megjött a pénz, rögzítsd a befizetést erre a rendelésre (Fizetés): az előlegszámla magától elkészül.")
                    : __("Ha megjött a pénz: rögzítsd a befizetést erre a rendelésre (Fizetés), majd Létrehozás → Értékesítési számla → beküldés. A Billingo-számla ekkor készül el fizetettként, a díjbekérő pedig fizetett lesz.")
            );
            if (overdue) {
                color = "orange";
            }
        }
        if (frm.doc.custom_billingo_advance_invoice_number) {
            notes.push(
                frm.doc.custom_billingo_advance_consumed_by
                    ? __("Előlegszámla: {0} — már levonva a(z) {1} számlán.", [
                          frm.doc.custom_billingo_advance_invoice_number,
                          frm.doc.custom_billingo_advance_consumed_by,
                      ])
                    : __("Előlegszámla: {0}. A végszámlán (Létrehozás → Értékesítési számla) a Billingo automatikusan levonja.", [
                          frm.doc.custom_billingo_advance_invoice_number,
                      ])
            );
        }
        if (frm.doc.custom_billingo_proforma_paid_by) {
            notes.push(
                __("Díjbekérő {0} kifizetve, számla: {1}.", [
                    frm.doc.custom_billingo_proforma_number || frm.doc.custom_billingo_proforma_id,
                    frm.doc.custom_billingo_proforma_paid_by,
                ])
            );
        }
        if (notes.length) {
            frm.dashboard.add_comment(notes.join(" "), color, true);
        }
    },
});

function open_proforma_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __("Díjbekérő létrehozása"),
        fields: [
            {
                fieldname: "mode",
                label: __("Típus"),
                fieldtype: "Select",
                options: [
                    { value: "Előleg", label: __("Előleg — egy összeg, amit te adsz meg") },
                    { value: "Teljes tételes", label: __("Teljes tételes — a rendelés minden tétele") },
                ],
                default: "Előleg",
                reqd: 1,
            },
            {
                fieldname: "due_date",
                label: __("Fizetési határidő"),
                fieldtype: "Date",
                default: frappe.datetime.add_days(frappe.datetime.get_today(), 8),
                reqd: 1,
                description: __("Eddig kell a vevőnek kifizetnie a díjbekérőt."),
            },
            {
                fieldname: "advance_section",
                fieldtype: "Section Break",
                depends_on: "eval:doc.mode=='Előleg'",
            },
            {
                fieldname: "description",
                label: __("Megnevezés"),
                fieldtype: "Data",
                description: __("Ez jelenik meg a díjbekérő egyetlen során."),
                mandatory_depends_on: "eval:doc.mode=='Előleg'",
            },
            {
                // Float, not Currency: a Currency field formats in the order's
                // currency, and EUR's "#,###.##" read a typed "1.000" as 1
                // (staging, 2026-10-02). Float follows the system's Hungarian
                // number format, and the amount is confirmed before sending.
                fieldname: "amount",
                label: __("Nettó összeg ({0})", [frm.doc.currency]),
                fieldtype: "Float",
                precision: 2,
                mandatory_depends_on: "eval:doc.mode=='Előleg'",
            },
            {
                fieldname: "vat_note",
                fieldtype: "HTML",
                options:
                    "<p class='text-muted small'>" +
                    __("Az áfakulcs a rendelésről öröklődik, és ugyanez kerül majd az előlegszámlára is.") +
                    "</p>",
                depends_on: "eval:doc.mode=='Előleg'",
            },
        ],
        primary_action_label: __("Létrehozás"),
        primary_action(values) {
            if (!values) {
                return;
            }
            if (values.due_date < frappe.datetime.get_today()) {
                frappe.msgprint(__("A fizetési határidő nem lehet a múltban."));
                return;
            }
            if (values.mode === "Előleg" && !(values.amount > 0)) {
                frappe.msgprint(__("Adj meg egy nulla feletti nettó összeget."));
                return;
            }
            const what =
                values.mode === "Előleg"
                    ? __("Előleg díjbekérő: {0} nettó + áfa.", [
                          format_currency(values.amount, frm.doc.currency),
                      ])
                    : __("Teljes tételes díjbekérő: {0}.", [
                          format_currency(frm.doc.grand_total, frm.doc.currency),
                      ]);
            frappe.confirm(
                what +
                    "<br>" +
                    __("Kimegy e-mailben ide: {0}. Folytatod?", [
                        frappe.utils.escape_html(frm.doc.contact_email || __("(a vevő kapcsolattartója)")),
                    ]),
                () => {
                    dialog.hide();
                    send_proforma(frm, values);
                }
            );
        },
    });
    dialog.show();
}

function send_proforma(frm, values) {
    frappe.call({
        method: "billingo.proforma.create_billingo_proforma",
        freeze: true,
        freeze_message: __("Díjbekérő készítése a Billingóban..."),
        args: {
            sales_order: frm.doc.name,
            mode: values.mode,
            description: values.description,
            amount: values.amount,
            due_date: values.due_date,
        },
        callback(r) {
            if (!r.message) {
                return;
            }
            frappe.show_alert({
                message: __("Díjbekérő elkészült: {0}", [
                    r.message.invoice_number || r.message.document_id,
                ]),
                indicator: "green",
            });
            warn_if_not_sent(r.message);
            frm.reload_doc();
        },
        error() {
            // The server may have recorded a failure on the order,
            // and the button state depends on it.
            frm.reload_doc();
        },
    });
}

function open_convert_dialog(frm) {
    const dialog = new frappe.ui.Dialog({
        title: __("Előlegszámla kiállítása kézzel"),
        fields: [
            {
                fieldname: "payment_date",
                label: __("Mikor érkezett meg a pénz?"),
                fieldtype: "Date",
                default: frappe.datetime.get_today(),
                reqd: 1,
                description: __(
                    "Ez lesz az előlegszámla teljesítési dátuma — az Áfa tv. 59. § szerint a pénz beérkezése a teljesítés időpontja, nem a mai nap."
                ),
            },
        ],
        primary_action_label: __("Előlegszámla kiállítása"),
        primary_action(values) {
            if (!values) {
                return;
            }
            dialog.hide();
            run_convert(frm, { payment_date: values.payment_date });
        },
    });
    dialog.show();
}

function run_convert(frm, extra_args) {
    frappe.call({
        method: "billingo.proforma.convert_billingo_proforma",
        freeze: true,
        freeze_message: __("Átalakítás a Billingóban..."),
        args: Object.assign({ sales_order: frm.doc.name }, extra_args),
        callback(r) {
            if (!r.message) {
                return;
            }
            frappe.show_alert({
                message: __("Elkészült: {0}", [r.message.invoice_number || r.message.document_id]),
                indicator: "green",
            });
            if (r.message.late) {
                frappe.msgprint({
                    title: __("Figyelem — határidő"),
                    indicator: "orange",
                    message: __(
                        "A megadott fizetési dátum több mint 10 napja volt. Az Áfa tv. 163. § szerint az előlegszámlát a pénz beérkezésétől számított 15 napon belül ki kell állítani."
                    ),
                });
            }
            if (r.message.date_warning) {
                frappe.msgprint({
                    title: __("Figyelem — dátum"),
                    indicator: "orange",
                    message: frappe.utils.escape_html(r.message.date_warning),
                });
            }
            if (r.message.proforma_paid_error) {
                frappe.msgprint({
                    title: __("Az előlegszámla elkészült, de a díjbekérőt nem sikerült fizetettnek jelölni"),
                    indicator: "orange",
                    message: __(
                        "Jelöld kifizetettnek kézzel a Billingóban, különben nyitott díjbekérőként látszik.<br><br>{0}",
                        [frappe.utils.escape_html(String(r.message.proforma_paid_error).slice(0, 300))]
                    ),
                });
            }
            warn_if_not_sent(r.message);
            frm.reload_doc();
        },
        error() {
            frm.reload_doc();
        },
    });
}

function withdraw_proforma(frm) {
    frappe.confirm(
        __(
            "A díjbekérő ({0}) sztornózásra kerül a Billingóban, és utána újat készíthetsz. Csak akkor csináld, ha még nem fizették ki. Folytatod?",
            [frm.doc.custom_billingo_proforma_number || frm.doc.custom_billingo_proforma_id]
        ),
        () =>
            frappe.call({
                method: "billingo.proforma.withdraw_billingo_proforma",
                freeze: true,
                freeze_message: __("Díjbekérő visszavonása a Billingóban..."),
                args: { sales_order: frm.doc.name },
                callback(r) {
                    if (r.message && r.message.left_in_billingo) {
                        frappe.msgprint({
                            title: __("A díjbekérő leválasztva, de a Billingóban megmaradt"),
                            indicator: "orange",
                            message: __(
                                "A Billingo nem engedte törölni a(z) {0} díjbekérőt. A rendelésről leválasztottam, új díjbekérő készíthető. A Billingóban nyisd meg, és zárd le kézzel, hogy a vevő ne fizesse ki.",
                                [r.message.proforma_number || r.message.withdrawn]
                            ),
                        });
                    } else if (r.message) {
                        frappe.show_alert({ message: __("A díjbekérő visszavonva."), indicator: "green" });
                    }
                    frm.reload_doc();
                },
                error() {
                    frm.reload_doc();
                },
            })
    );
}

function warn_if_not_sent(message) {
    if (!message || !message.send_failed) {
        return;
    }
    frappe.msgprint({
        title: __("A dokumentum elkészült, de nem ment ki e-mailben"),
        indicator: "orange",
        message: __(
            "A Billingo nem tudta elküldeni a vevőnek. Ellenőrizd a vevő kapcsolattartójának e-mail címét, és küldd ki kézzel a Billingóból. A részletek az Error Logban."
        ),
    });
}
