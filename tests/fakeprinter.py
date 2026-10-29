"""An in-memory Ricoh address book that answers the SOAP calls RicohClient makes.

Responses mirror the structure of a real printer's (synthetic data only).
"""

from __future__ import annotations

from lxml import etree

ENV = "http://schemas.xmlsoap.org/soap/envelope/"
UDIR = "http://www.ricoh.co.jp/xmlns/soap/rdh/udirectory"
PAGE = 50


class Response:
    def __init__(self, body: str, status: int = 200):
        self.content = body.encode("utf-8")
        self.status_code = status


def wrap(method: str, inner: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{ENV}"><s:Body>'
            f'<tns:{method}Response xmlns:tns="{UDIR}">{inner}</tns:{method}Response></s:Body></s:Envelope>')


def fault(code: str, description: str = "") -> Response:
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="{ENV}"><s:Body><s:Fault>'
                    f'<faultcode>s:Client</faultcode><faultstring>{code}</faultstring><detail>'
                    f'<rdhError><errorCode>{code}</errorCode><errorDescription>{description}</errorDescription>'
                    f'</rdhError></detail></s:Fault></s:Body></s:Envelope>', 500)


def props_xml(props: dict[str, str]) -> str:
    return "".join(f"<item><propName>{k}</propName><propVal>{v}</propVal></item>" for k, v in props.items())


class FakePrinter:
    """Acts as a requests.Session: post() receives the SOAP request."""

    def __init__(self, entries: list[dict[str, str]] | None = None, numbering: str = "next"):
        self.entries: dict[int, dict[str, str]] = {}
        self.next_id = 1
        # Registration numbers for new entries: "next" after the highest, or the "lowest" free one.
        self.numbering = numbering
        for props in entries or []:
            self.insert(dict(props))
        self.calls: list[str] = []
        self.requests: list[bytes] = []
        self.sessions_open = 0
        self.lock_modes: list[str] = []
        self.fail: dict[str, Response] = {}  # method -> canned fault
        self.trust_env = True
        self.proxies = {}

    def insert(self, props: dict[str, str]) -> int:
        entry_id = int(props.get("id") or self.next_id)
        self.next_id = max(self.next_id, entry_id) + 1
        props["id"] = str(entry_id)
        if "index" not in props:
            used = {int(e["index"]) for e in self.entries.values()}
            if self.numbering == "lowest":
                props["index"] = str(next(n for n in range(1, len(used) + 2) if n not in used))
            else:
                props["index"] = str(max(used, default=0) + 1)
        self.entries[entry_id] = props
        return entry_id

    def names_by_number(self) -> list[str]:
        return [e["name"] for e in sorted(self.entries.values(), key=lambda e: int(e["index"]))]

    def post(self, url, data, headers, timeout):
        method = headers["SOAPAction"].split("#", 1)[1]
        self.calls.append(method)
        self.requests.append(data)
        if method in self.fail:
            return self.fail[method]
        req = etree.fromstring(data).find(f"{{{ENV}}}Body")[0]
        return getattr(self, "_" + method)(req)

    @staticmethod
    def _items(el, tag):
        parent = el.find(tag)
        return [i for i in (parent if parent is not None else [])]

    def _startSession(self, req):
        self.sessions_open += 1
        self.lock_modes.append(req.findtext("lockMode"))
        return Response(wrap("startSession", "<returnValue>OK</returnValue><stringOut>4242</stringOut>"))

    def _terminateSession(self, req):
        self.sessions_open -= 1
        return Response(wrap("terminateSession", "<returnValue>OK</returnValue>"))

    def _searchObjects(self, req):
        offset = int(req.findtext("rowOffset") or 0)
        ids = sorted(self.entries)
        rows = ids[offset:offset + PAGE]
        rows_xml = "".join(f"<item>{props_xml({'id': str(i)})}</item>" for i in rows)
        done = "EOD" if offset + PAGE >= len(ids) else "OK"
        return Response(wrap("searchObjects", f"<returnValue>{done}</returnValue><resultSetId></resultSetId>"
                                              f"<numOfResults>{len(ids)}</numOfResults><rowList>{rows_xml}</rowList>"))

    def _getObjectsProps(self, req):
        ids = [int(i.text.split(":")[1]) for i in self._items(req, "objectIdList")]
        if len(ids) > PAGE:
            return fault("UDIRECTORY_TOO_MANY_OBJECTS")
        wanted = [i.text for i in self._items(req, "selectProps")]
        out = ""
        for i in ids:
            if i in self.entries:
                props = {k: v for k, v in self.entries[i].items() if k in wanted}
                out += f"<item>{props_xml(props)}</item>"
        return Response(wrap("getObjectsProps", f"<returnValue>{out}</returnValue>"))

    def _putObjects(self, req):
        for plist in self._items(req, "propListList"):
            props = {i.findtext("propName"): i.findtext("propVal") or "" for i in plist}
            self.insert(props)
        return Response(wrap("putObjects", "<returnValue></returnValue>"))

    def _putObjectProps(self, req):
        entry_id = int(req.findtext("objectId").split(":")[1])
        if entry_id not in self.entries:
            return fault("UDIRECTORY_NO_SUCH_OBJECT")
        for i in self._items(req, "propList"):
            self.entries[entry_id][i.findtext("propName")] = i.findtext("propVal") or ""
        return Response(wrap("putObjectProps", "<returnValue></returnValue>"))

    def _deleteObjects(self, req):
        for i in self._items(req, "objectIdList"):
            self.entries.pop(int(i.text.split(":")[1]), None)
        return Response(wrap("deleteObjects", "<returnValue></returnValue>"))


def person(name: str, email: str, **extra) -> dict[str, str]:
    return {"name": name, "longName": name[:16], "tagId": "2", "mail:": "true", "mail:address": email,
            "isSender": "false", "isDestination": "true", "displayedOrder": "5", **extra}
