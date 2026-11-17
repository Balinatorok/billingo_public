"""A small in-memory stand-in for the parts of Frappe the billingo app uses.

Installed into sys.modules before billingo is imported, so the real api.py and
proforma.py run unmodified against it.
"""
import copy
import datetime
import sys
import types


class ValidationError(Exception):
    pass


class PermissionError_(Exception):
    pass


class Store:
    def __init__(self):
        self.rows = {}  # (doctype, name) -> dict
        self.commits = 0
        self.locks = []

    def put(self, doctype, name, **fields):
        row = {"doctype": doctype, "name": name}
        row.update(fields)
        self.rows[(doctype, name)] = row
        return row

    def get(self, doctype, name):
        return self.rows.get((doctype, name))


class _Dict(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            return None


class Doc:
    def __init__(self, store, doctype, name):
        self._store = store
        self._row = store.get(doctype, name)
        self.perm_checks = []

    @staticmethod
    def _wrap(value):
        if isinstance(value, list):
            return [_Dict(v) if isinstance(v, dict) and not isinstance(v, _Dict) else v for v in value]
        return value

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)
        return self._wrap(self._row.get(item))

    def get(self, key, default=None):
        value = self._row.get(key, default)
        value = default if value is None and default is not None else value
        return self._wrap(value)

    def db_set(self, key, value, **kw):
        self._row[key] = value

    def check_permission(self, ptype):
        self.perm_checks.append(ptype)


class _Dict(dict):
    def __getattr__(self, item):
        try:
            return self[item]
        except KeyError:
            raise AttributeError(item)


class DB:
    def __init__(self, store):
        self.store = store

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        self.store.rollbacks = getattr(self.store, "rollbacks", 0) + 1

    def _match(self, row, filters):
        for key, wanted in filters.items():
            value = row.get(key)
            if isinstance(wanted, list):
                op, operand = wanted
                if op == "!=" and value == operand:
                    return False
                if op == "<=" and not (str(value) <= str(operand)):
                    return False
                if op == "in" and value not in operand:
                    return False
                if op == "is" and operand == "set" and not value:
                    return False
            elif str(value) != str(wanted):
                return False
        return True

    def get_value(self, doctype, name, field="name", as_dict=False, for_update=False, **kw):
        if for_update:
            self.store.locks.append((doctype, name))
        if isinstance(name, dict):
            for (dt, _), row in self.store.rows.items():
                if dt == doctype and self._match(row, name):
                    return row.get(field) if isinstance(field, str) else None
            return None
        row = self.store.get(doctype, name)
        if row is None:
            return None
        if isinstance(field, (list, tuple)):
            out = _Dict({f: row.get(f) for f in field})
            return out if as_dict else list(out.values())
        return row.get(field)

    def set_value(self, doctype, name, field, value=None, update_modified=True):
        row = self.store.get(doctype, name)
        if isinstance(field, dict):
            row.update(field)
        else:
            row[field] = value

    def exists(self, doctype, filters):
        name = filters.get("name") if isinstance(filters, dict) else filters
        return self.store.get(doctype, name) is not None

    def has_column(self, doctype, column):
        return True


class Frappe(types.ModuleType):
    def __init__(self):
        super().__init__("frappe")
        self.store = Store()
        self.db = DB(self.store)
        self.flags = _Dict()
        self.conf = _Dict()
        self.logged = []
        self.enqueued = []
        self.messages = []
        self.permissions_ok = True
        self.PermissionError = PermissionError_
        self.ValidationError = ValidationError

    def reset(self):
        self.__init__()

    def whitelist(self, *a, **kw):
        def deco(fn):
            return fn

        return deco

    def throw(self, msg, exc=None, title=None, **kw):
        if isinstance(exc, type) and issubclass(exc, BaseException):
            raise exc(msg)
        raise ValidationError(msg)

    def log_error(self, message=None, title=None, **kw):
        self.logged.append((title, message))

    def get_traceback(self):
        return "traceback"

    def get_doc(self, doctype, name):
        if self.store.get(doctype, name) is None:
            raise ValidationError(f"{doctype} {name} not found")
        return Doc(self.store, doctype, name)

    def get_cached_value(self, doctype, name, field):
        return self.db.get_value(doctype, name, field)

    def has_permission(self, doctype, ptype=None, **kw):
        return self.permissions_ok

    def get_all(self, doctype, filters=None, fields=None, pluck=None, order_by=None, limit=None, **kw):
        rows = [
            copy.deepcopy(row)
            for (dt, _), row in self.store.rows.items()
            if dt == doctype and self.db._match(row, filters or {})
        ]
        if order_by:
            key, _, direction = order_by.partition(" ")
            rows.sort(key=lambda r: str(r.get(key)), reverse=direction.strip() == "desc")
        if limit:
            rows = rows[:limit]
        if pluck:
            return [r.get(pluck) for r in rows]
        return [_Dict(r) for r in rows]

    def msgprint(self, msg, title=None, indicator=None, **kw):
        self.messages.append((title, msg))

    def delete_doc(self, doctype, name, **kw):
        self.store.rows.pop((doctype, name), None)

    def enqueue(self, method, **kw):
        self.enqueued.append((method, kw))


def _getdate(value=None):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not value:
        return datetime.date.fromisoformat(TODAY[0])
    return datetime.date.fromisoformat(str(value)[:10])


TODAY = ["2026-10-01"]


def _flt(value, precision=None):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _cint(value):
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def install():
    frappe = Frappe()
    utils = types.ModuleType("frappe.utils")
    utils.flt = _flt
    utils.cint = _cint
    utils.getdate = _getdate
    utils.nowdate = lambda: TODAY[0]
    utils.add_days = lambda d, n: _getdate(d) + datetime.timedelta(days=n)
    utils.now_datetime = datetime.datetime.now
    utils.strip_html = lambda s: s
    frappe.utils = utils

    custom = types.ModuleType("frappe.custom")
    cdoctype = types.ModuleType("frappe.custom.doctype")
    cfield = types.ModuleType("frappe.custom.doctype.custom_field")
    cfield_mod = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    cfield_mod.created = []
    cfield_mod.create_custom_fields = lambda fields, **kw: cfield_mod.created.append(fields)

    sys.modules["frappe"] = frappe
    sys.modules["frappe.utils"] = utils
    sys.modules["frappe.custom"] = custom
    sys.modules["frappe.custom.doctype"] = cdoctype
    sys.modules["frappe.custom.doctype.custom_field"] = cfield
    sys.modules["frappe.custom.doctype.custom_field.custom_field"] = cfield_mod
    return frappe


class Resp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise HTTPError(response=self)


class HTTPError(Exception):
    def __init__(self, response=None):
        super().__init__(str(getattr(response, "_payload", "")))
        self.response = response


class FakeRequests(types.ModuleType):
    """Routes calls to handlers keyed by (METHOD, url suffix)."""

    def __init__(self):
        super().__init__("requests")
        self.exceptions = types.SimpleNamespace(HTTPError=HTTPError)
        self.reset()

    def reset(self):
        self.calls = []
        self.routes = {}

    def route(self, method, suffix, handler):
        self.routes[(method, suffix)] = handler

    def _call(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        suffix = url.replace("https://api.billingo.hu/v3", "")
        handler = self.routes.get((method, suffix))
        if handler is None:
            raise AssertionError(f"unexpected {method} {suffix}")
        result = handler(kw.get("json"))
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kw):
        return self._call("GET", url, **kw)

    def post(self, url, **kw):
        return self._call("POST", url, **kw)

    def put(self, url, **kw):
        return self._call("PUT", url, **kw)

    def delete(self, url, **kw):
        return self._call("DELETE", url, **kw)


def install_requests():
    fake = FakeRequests()
    sys.modules["requests"] = fake
    return fake
