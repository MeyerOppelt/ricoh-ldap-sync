"""Client for the Ricoh address book SOAP service (``/DH/udirectory``).

This is the interface Ricoh's own management tools use. A session is opened
with ``startSession`` (credentials are base64 encoded inside ``stringIn``),
used for searches, reads and writes, and must always be closed with
``terminateSession``: an open session can lock the address book on the panel.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import requests
from lxml import etree

log = logging.getLogger(__name__)

PATH = "/DH/udirectory"
NS_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
NS_ENC = "http://schemas.xmlsoap.org/soap/encoding/"
NS_XS = "http://www.w3.org/2001/XMLSchema"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_RICOH = "http://www.ricoh.co.jp/xmlns/schema/rdh/commontypes"
NS_UDIR = "http://www.ricoh.co.jp/xmlns/soap/rdh/udirectory"

PAGE_SIZE = 50  # printers return at most 50 rows per search and reject larger reads
SESSION_TIME_LIMIT = 30
HTTP_TIMEOUT = (5, 30)

# Properties read for every entry. Password properties are never requested.
ENTRY_PROPS = (
    "id", "index", "name", "longName", "displayedOrder", "tagId",
    "auth:", "auth:name", "lastAccessDateTime",
    "remoteFolder:", "remoteFolder:type", "remoteFolder:path", "remoteFolder:port", "remoteFolder:accountName",
    "mail:", "mail:address", "isSender", "isDestination",
)

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


class RicohError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Entry:
    id: int
    props: dict[str, str]

    @property
    def name(self) -> str:
        return self.props.get("name", "")

    @property
    def long_name(self) -> str:
        return self.props.get("longName", "")

    @property
    def email(self) -> str:
        """Lowercased scan-to-email address, or "" if the entry has none."""
        if self.props.get("mail:") != "true":
            return ""
        return self.props.get("mail:address", "").strip().lower()

    @property
    def tag_ids(self) -> tuple[int, ...]:
        return tuple(int(t) for t in self.props.get("tagId", "").split(",") if t.strip().isdigit())


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _sub(parent, tag: str, text: str | None = None):
    el = etree.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _array(parent, tag: str, item_type: str):
    el = _sub(parent, tag)
    el.set(f"{{{NS_XSI}}}type", "soap-enc:Array")
    el.set(f"{{{NS_ENC}}}arrayType", item_type)
    return el


def _props(parent, props: Iterable[tuple[str, str]]) -> None:
    for name, value in props:
        item = _sub(parent, "item")
        _sub(item, "propName", name)
        _sub(item, "propVal", value)


def envelope(method: str):
    """Return (document root, method element) for a request."""
    root = etree.Element(f"{{{NS_ENV}}}Envelope", nsmap={"s": NS_ENV})
    root.set(f"{{{NS_ENV}}}encodingStyle", NS_ENC)
    body = etree.SubElement(root, f"{{{NS_ENV}}}Body")
    call = etree.SubElement(body, f"{{{NS_UDIR}}}{method}", nsmap={
        "u": NS_UDIR, "xs": NS_XS, "xsi": NS_XSI, "soap-enc": NS_ENC, "ricoh": NS_RICOH,
    })
    return root, call


def _local(el) -> str:
    return etree.QName(el).localname


def _child(el, name: str):
    for c in el:
        if isinstance(c.tag, str) and _local(c) == name:
            return c
    return None


def _text(el, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def parse_property_list(el) -> dict[str, str]:
    props = {}
    for item in el:
        if isinstance(item.tag, str):
            props[_text(item, "propName")] = _text(item, "propVal")
    return props


def parse_fault(root) -> RicohError | None:
    fault = root.xpath("//*[local-name()='Fault']")
    if not fault:
        return None
    f = fault[0]
    code = (f.xpath("string(.//*[local-name()='errorCode'])") or "").strip() or None
    desc = (f.xpath("string(.//*[local-name()='errorDescription'])") or "").strip()
    faultstring = (f.xpath("string(.//*[local-name()='faultstring'])") or "").strip()
    return RicohError(desc or code or faultstring or "SOAP fault", code=code)


class RicohClient:
    def __init__(self, base_url: str, user: str, password: str, proxy: str | None = None,
                 http: requests.Session | None = None, timeout=HTTP_TIMEOUT):
        self.url = base_url.rstrip("/") + PATH
        self._user = user
        self._password = password
        self.timeout = timeout
        self.http = http or requests.Session()
        self.http.trust_env = False  # only the proxy given here, never a global HTTP_PROXY
        if proxy:
            self.http.proxies = {"http": proxy, "https": proxy}
        self.session_id: str | None = None
        self.last_response: bytes = b""

    # -- transport -------------------------------------------------------

    def call(self, method: str, root) -> etree._Element:
        """POST a request and return the response's method element. Raises RicohError."""
        data = etree.tostring(root, xml_declaration=True, encoding="utf-8")
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f"{NS_UDIR}#{method}"}
        try:
            resp = self.http.post(self.url, data=data, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise RicohError(f"{method}: {type(e).__name__}: {e}") from e
        self.last_response = resp.content
        try:
            doc = etree.fromstring(resp.content, _PARSER)
        except etree.XMLSyntaxError as e:
            raise RicohError(f"{method}: HTTP {resp.status_code}, response is not XML") from e
        fault = parse_fault(doc)
        if fault is not None:
            fault.args = (f"{method}: {fault.args[0]}",)
            raise fault
        if resp.status_code != 200:
            raise RicohError(f"{method}: HTTP {resp.status_code}")
        body = doc.find(f"{{{NS_ENV}}}Body")
        result = next((c for c in (body if body is not None else []) if isinstance(c.tag, str)), None)
        if result is None:
            raise RicohError(f"{method}: empty SOAP body")
        return result

    def _request(self, method: str):
        root, call = envelope(method)
        if method != "startSession":
            if not self.session_id:
                raise RicohError(f"{method}: no open session")
            _sub(call, "sessionId", self.session_id)
        return root, call

    # -- session ---------------------------------------------------------

    def start_session(self, write: bool) -> str:
        root, call = self._request("startSession")
        _sub(call, "stringIn", f"SCHEME={_b64('BASIC')};UID:UserName={_b64(self._user)};"
                               f"PWD:Password={_b64(self._password)};PES:Encoding=")
        _sub(call, "timeLimit", str(SESSION_TIME_LIMIT))
        _sub(call, "lockMode", "X" if write else "S")
        result = self.call("startSession", root)
        status, sid = _text(result, "returnValue"), _text(result, "stringOut")
        if status != "OK" or not sid:
            raise RicohError(f"startSession: returned {status or 'nothing'}")
        self.session_id = sid
        return sid

    def terminate_session(self) -> None:
        if not self.session_id:
            return
        try:
            root, _ = self._request("terminateSession")
            self.call("terminateSession", root)
        finally:
            self.session_id = None

    @contextmanager
    def session(self, write: bool) -> Iterator[RicohClient]:
        self.start_session(write)
        try:
            yield self
        finally:
            try:
                self.terminate_session()
            except RicohError as e:
                log.error("could not close the address book session (it ends after %ss): %s", SESSION_TIME_LIMIT, e)

    # -- reads -----------------------------------------------------------

    def search_ids(self) -> tuple[list[int], int]:
        """IDs of every address book entry, and the printer's reported entry count."""
        ids: list[int] = []
        total = 0
        for page in range(1000):
            root, call = self._request("searchObjects")
            select = _array(call, "selectProps", "xs:string[]")
            _sub(select, "item", "id")
            _sub(call, "fromClass", "entry")
            order = _array(call, "orderBy", "ricoh:queryOrderBy[]")
            item = _sub(order, "item")
            _sub(item, "propName", "index")
            _sub(item, "isDescending", "false")
            _sub(call, "rowOffset", str(page * PAGE_SIZE))
            _sub(call, "rowCount", str(PAGE_SIZE))
            result = self.call("searchObjects", root)
            total = int(_text(result, "numOfResults") or 0)
            rows = _child(result, "rowList")
            remaining = total - page * PAGE_SIZE
            for row in list(rows if rows is not None else [])[:max(remaining, 0)]:
                value = parse_property_list(row).get("id", "")
                # Very long IDs are not ordinary entries; the reference client skips them too.
                if value.isdigit() and len(value) < 10:
                    ids.append(int(value))
            if _text(result, "returnValue") == "EOD":
                return ids, total
        raise RicohError("searchObjects: did not reach the end of the address book")

    def get_entries(self, ids: Iterable[int], props: Iterable[str] = ENTRY_PROPS) -> list[Entry]:
        ids = list(ids)
        props = list(props)
        entries: list[Entry] = []
        for start in range(0, len(ids), PAGE_SIZE):
            root, call = self._request("getObjectsProps")
            id_list = _array(call, "objectIdList", "xs:string[]")
            for i in ids[start:start + PAGE_SIZE]:
                _sub(id_list, "item", f"entry:{i}")
            select = _array(call, "selectProps", "xs:string[]")
            for p in props:
                _sub(select, "item", p)
            result = self.call("getObjectsProps", root)
            values = _child(result, "returnValue")
            for item in (values if values is not None else []):
                if not isinstance(item.tag, str):
                    continue
                parsed = parse_property_list(item)
                if parsed.get("id", "").isdigit():
                    entries.append(Entry(id=int(parsed["id"]), props=parsed))
        return entries

    # -- writes ----------------------------------------------------------

    def add_entry(self, props: list[tuple[str, str]]) -> etree._Element:
        root, call = self._request("putObjects")
        _sub(call, "objectClass", "entry")
        lists = _array(call, "propListList", "ricoh:propertyList[]")
        _props(_sub(lists, "item"), props)
        return self.call("putObjects", root)

    def update_entry(self, entry_id: int, props: list[tuple[str, str]]) -> etree._Element:
        root, call = self._request("putObjectProps")
        _sub(call, "objectId", f"entry:{entry_id}")
        _props(_array(call, "propList", "ricoh:property[]"), props)
        return self.call("putObjectProps", root)

    def delete_entries(self, ids: Iterable[int]) -> etree._Element:
        root, call = self._request("deleteObjects")
        id_list = _array(call, "objectIdList", "xs:string[]")
        for i in ids:
            _sub(id_list, "item", f"entry:{i}")
        return self.call("deleteObjects", root)
