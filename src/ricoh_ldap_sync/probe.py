"""Protocol probe: prove the address book SOAP service works on a printer.

    python -m ricoh_ldap_sync.probe [--printer NAME] [--write-test] [--save DIR]

Without --write-test it only reads: it opens a read-only session, lists the
address book and reads the full properties of two entries. With --write-test it
also adds a test entry ("ZZ Sync Test", sync-test@invalid.example), reads it
back, renames it with over-long values to find the length limits, deletes it,
and confirms it's gone. It never touches any other entry.

Output has email addresses replaced and names masked to their first letters,
keeping their length.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

from lxml import etree

from . import config
from .ricoh_soap import ENTRY_PROPS, RicohClient, RicohError
from .sync import Desired, title1_tag

TEST_NAME = "ZZ Sync Test"
TEST_EMAIL = "sync-test@invalid.example"
# Extra properties seen in the address book schema, without any password fields.
EXTRA_PROPS = ("entryType", "phoneticName", "mail:isDirectSMTP", "mail:parameter", "fax:", "fax:number", "ifax:",
               "ifax:address", "ldap:", "smtp:", "password:")
NAME_PROPS = {"name", "longName", "phoneticName", "auth:name", "remoteFolder:accountName", "remoteFolder:path"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def mask(value: str) -> str:
    if value.startswith(TEST_NAME):
        return value
    return re.sub(r"(?<=\w)\w", "x", value)


# Same address -> same placeholder for the whole probe, so entries stay distinguishable.
_PLACEHOLDERS: dict[str, str] = {}


def scrub_text(text: str) -> str:
    def repl(m):
        addr = m.group(0).lower()
        if addr == TEST_EMAIL:
            return addr
        return _PLACEHOLDERS.setdefault(addr, f"user{len(_PLACEHOLDERS) + 1}@example.invalid")

    return EMAIL.sub(repl, text)


def scrub_xml(raw: bytes) -> str:
    try:
        doc = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return scrub_text(raw.decode("utf-8", "replace"))
    for item in doc.iter("item"):
        name = item.find("propName")
        val = item.find("propVal")
        if name is not None and val is not None and name.text in NAME_PROPS and val.text:
            val.text = mask(val.text)
    for el in doc.iter("stringOut"):
        el.text = "<session id>"
    return scrub_text(etree.tostring(doc, pretty_print=True, encoding="unicode"))


class Probe:
    def __init__(self, client: RicohClient, save_dir: str | None):
        self.client = client
        self.save_dir = save_dir
        self.failed = False

    def line(self, status: str, step: str, detail: str = "") -> None:
        if status == "FAIL":
            self.failed = True
        print(f"{status:<4} {step}{': ' + detail if detail else ''}", flush=True)

    def raw(self, label: str) -> None:
        text = scrub_xml(self.client.last_response)
        print(f"----- raw response: {label} -----\n{text.rstrip()}\n----- end {label} -----", flush=True)
        if self.save_dir:
            os.makedirs(self.save_dir, exist_ok=True)
            with open(os.path.join(self.save_dir, f"{label}.xml"), "w", encoding="utf-8") as f:
                f.write(text)

    def show_entry(self, label: str, props: dict[str, str]) -> None:
        print(f"  {label}:", flush=True)
        for k in sorted(props):
            v = props[k]
            v = mask(v) if k in NAME_PROPS else scrub_text(v)
            extra = f"  (length {len(props[k])})" if k in ("name", "longName") else ""
            print(f"    {k} = {v!r}{extra}", flush=True)

    def read(self) -> None:
        c = self.client
        ids, count = c.search_ids()
        self.raw("searchObjects")
        self.line("PASS", "search", f"{count} entries reported, {len(ids)} ids returned: {ids[:30]}"
                  + (" ..." if len(ids) > 30 else ""))
        sample = [i for i in ids][:2]
        if not sample:
            self.line("INFO", "read", "address book is empty; nothing to read")
            return
        entries = c.get_entries(sample)
        self.raw("getObjectsProps")
        for e in entries:
            self.show_entry(f"entry {e.id}", e.props)
        self.line("PASS", "read", f"read {len(entries)} entries with the standard properties")
        try:
            extra = c.get_entries(sample[:1], ("id", *EXTRA_PROPS))
            self.raw("getObjectsProps-extra")
            self.show_entry(f"entry {sample[0]} (extra properties)", extra[0].props if extra else {})
            self.line("PASS", "read extra properties")
        except RicohError as e:
            self.raw("getObjectsProps-extra-fault")
            self.line("INFO", "read extra properties", f"printer refused: {e} (code {e.code})")
        all_entries = c.get_entries(ids)
        tags = Counter(t for e in all_entries for t in e.tag_ids)
        longest = max((len(e.name) for e in all_entries), default=0)
        longest_key = max((len(e.long_name) for e in all_entries), default=0)
        with_mail = sum(1 for e in all_entries if e.email)
        self.line("INFO", "summary", f"{len(all_entries)} entries read, {with_mail} with email; "
                  f"longest name {longest}, longest longName {longest_key}; tagId counts {dict(sorted(tags.items()))}")

    def find_test(self) -> list:
        ids, _ = self.client.search_ids()
        return [e for e in self.client.get_entries(ids) if e.email == TEST_EMAIL and e.name.startswith(TEST_NAME)]

    def write_test(self) -> None:
        c = self.client
        leftovers = self.find_test()
        if leftovers:
            c.delete_entries([e.id for e in leftovers])
            self.line("INFO", "cleanup", f"deleted {len(leftovers)} leftover test entries")

        d = Desired(email=TEST_EMAIL, name=TEST_NAME, key_display=TEST_NAME, tag=title1_tag(TEST_NAME))
        try:
            c.add_entry(d.add_props())
            self.raw("putObjects")
            self.line("PASS", "add", f"putObjects accepted {TEST_NAME!r} <{TEST_EMAIL}>")
        except RicohError as e:
            self.raw("putObjects-fault")
            self.line("FAIL", "add", f"{e} (code {e.code})")
            if e.code and "PERMISSION" in e.code.upper():
                print("     The printer refused the write. Check that RICOH_USER has the user administrator "
                      "role. If it does, this model may not allow address book writes over SOAP.", flush=True)
            return

        found = self.find_test()
        if not found:
            self.line("FAIL", "read back", "the test entry was not found after adding it")
            return
        entry = found[0]
        self.show_entry(f"test entry {entry.id}", entry.props)
        self.line("PASS", "read back", f"entry {entry.id}, tagId {entry.props.get('tagId')}")

        long_name = (TEST_NAME + " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")[:48]
        try:
            c.update_entry(entry.id, [("name", long_name), ("longName", long_name)])
            self.raw("putObjectProps")
            after = c.get_entries([entry.id])[0]
            self.line("PASS", "update", f"sent {len(long_name)} characters; printer stored name "
                      f"{len(after.name)} characters, longName {len(after.long_name)} characters")
        except RicohError as e:
            self.raw("putObjectProps-fault")
            self.line("INFO", "update", f"over-long values refused: {e} (code {e.code}); trying a normal rename")
            try:
                c.update_entry(entry.id, [("name", TEST_NAME + " 2"), ("longName", TEST_NAME + " 2")])
                self.line("PASS", "update", "normal rename accepted")
            except RicohError as e2:
                self.line("FAIL", "update", f"{e2} (code {e2.code})")

        try:
            c.delete_entries([entry.id])
            self.raw("deleteObjects")
        except RicohError as e:
            self.raw("deleteObjects-fault")
            self.line("FAIL", "delete", f"{e} (code {e.code}). Delete the test entry in Web Image Monitor")
            return
        if self.find_test():
            self.line("FAIL", "delete", "the test entry is still there. Delete it in Web Image Monitor")
        else:
            self.line("PASS", "delete", "test entry removed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ricoh_ldap_sync.probe", description=__doc__.splitlines()[0])
    parser.add_argument("--printer", help="printer name from PRINTERS (default: the first)")
    parser.add_argument("--write-test", action="store_true", help="also add, update and delete a test entry")
    parser.add_argument("--save", metavar="DIR", help="also save the scrubbed raw responses to DIR")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    printer = next((p for p in cfg.printers if p.name == args.printer), None) if args.printer else cfg.printers[0]
    if printer is None:
        print(f"no printer named {args.printer!r} in PRINTERS", file=sys.stderr)
        return 2

    client = RicohClient(printer.base_url, cfg.ricoh_user, printer.password, proxy=cfg.printer_proxy)
    probe = Probe(client, args.save)
    print(f"INFO printer {printer.name} at {printer.base_url} as {cfg.ricoh_user}, "
          f"{'write test' if args.write_test else 'read only'}", flush=True)
    try:
        client.start_session(write=args.write_test)
        probe.raw("startSession")
        probe.line("PASS", "startSession", f"lock mode {'X (exclusive)' if args.write_test else 'S (shared)'}")
    except RicohError as e:
        if client.last_response:
            probe.raw("startSession-fault")
        probe.line("FAIL", "startSession", f"{e} (code {e.code})")
        return 1
    try:
        probe.read()
        if args.write_test:
            probe.write_test()
    except RicohError as e:
        if client.last_response:
            probe.raw("fault")
        probe.line("FAIL", "probe", f"{e} (code {e.code})")
    finally:
        try:
            client.terminate_session()
            probe.line("PASS", "terminateSession")
        except RicohError as e:
            probe.line("FAIL", "terminateSession", f"{e}; the session ends by itself after its time limit")
    print("RESULT: " + ("FAILED" if probe.failed else "all steps passed"), flush=True)
    return 1 if probe.failed else 0


if __name__ == "__main__":
    sys.exit(main())
