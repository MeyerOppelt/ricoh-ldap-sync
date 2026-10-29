# Ricoh address book SOAP protocol

This is a reference for the part of the Ricoh address book service that
`ricoh-ldap-sync` uses (`src/ricoh_ldap_sync/ricoh_soap.py`). Ricoh doesn't
publish this interface. What follows was learned from
[RicohAddressBook](https://github.com/desjardinsm/RicohAddressBook) and checked
against printers. Use `python -m ricoh_ldap_sync.probe` to see what a particular
model returns.

## Transport

| | |
| --- | --- |
| Endpoint | `POST http://<printer>/DH/udirectory`, the same host and port as Web Image Monitor |
| Content type | `text/xml; charset=utf-8` |
| `SOAPAction` | `http://www.ricoh.co.jp/xmlns/soap/rdh/udirectory#<method>` |
| Envelope | SOAP 1.1, `s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"` |
| Method element | `<u:<method>>` in namespace `http://www.ricoh.co.jp/xmlns/soap/rdh/udirectory`. Its child elements have no namespace. |

Arrays are SOAP-encoded. The array element carries `xsi:type="soap-enc:Array"`
and `soap-enc:arrayType`, and each member is an `<item>`. The prefixes used in
those attribute values (`xs`, `xsi`, `soap-enc`, `ricoh`) are declared on the
method element. `ricoh` is `http://www.ricoh.co.jp/xmlns/schema/rdh/commontypes`.

A property is `<item><propName>NAME</propName><propVal>VALUE</propVal></item>`.

Errors come back as a SOAP `Fault`, usually with HTTP 500. The useful parts are
in `detail/rdhError/errorCode` (for example `UDIRECTORY_PERMISSION_DENIED`) and
`errorDescription`.

## Session

Every other call takes the `sessionId` returned by `startSession`.

**`startSession`**

| Child | Value |
| --- | --- |
| `stringIn` | `SCHEME=<b64("BASIC")>;UID:UserName=<b64(user)>;PWD:Password=<b64(password)>;PES:Encoding=`, with UTF-8 then base64 for each part |
| `timeLimit` | Session time limit. The sync uses `30`. |
| `lockMode` | `S` for a shared read session, `X` for an exclusive session that allows writes |

The response has `returnValue` set to `OK` and `stringOut` holding the session ID.

**`terminateSession`** takes `sessionId`. Always call it: an exclusive session
that is left open blocks address book edits on the panel until its time limit
runs out.

## Reading

**`searchObjects`** lists entry IDs, one page at a time.

| Child | Value |
| --- | --- |
| `selectProps` | `xs:string[]` with one item, `id` |
| `fromClass` | `entry` |
| `orderBy` | `ricoh:queryOrderBy[]`: `<item><propName>index</propName><isDescending>false</isDescending></item>` |
| `rowOffset` | 0, 50, 100, ... |
| `rowCount` | `50`. Printers return at most 50 rows per call. |

The response has `numOfResults` (total entries), `rowList` (one `item` per row,
each a property list holding `id`) and `returnValue`, which is `EOD` on the last
page. IDs of 10 digits or more aren't ordinary entries and are skipped.

**`getObjectsProps`** reads properties for up to 50 entries per call.

| Child | Value |
| --- | --- |
| `objectIdList` | `xs:string[]` of `entry:<id>` |
| `selectProps` | `xs:string[]` of property names |

The response's `returnValue` holds one `item` per entry, each a property list.

## Writing

**`putObjects`** adds entries.

| Child | Value |
| --- | --- |
| `objectClass` | `entry` |
| `propListList` | `ricoh:propertyList[]`: one `item` per entry, each containing property `item`s |

The sync adds one entry per call. Then it searches again and matches the new ID
by email and name, because the response doesn't reliably return the ID.

**`putObjectProps`** changes properties of one entry.

| Child | Value |
| --- | --- |
| `objectId` | `entry:<id>` |
| `propList` | `ricoh:property[]` of the properties to change |

**`deleteObjects`** takes an `objectIdList` (`xs:string[]` of `entry:<id>`).

## Entry properties

| Property | Meaning | Written by the sync |
| --- | --- | --- |
| `id` | Entry ID, used in `entry:<id>`. Not the registration number. | |
| `index` | Registration number shown in Web Image Monitor | |
| `entryType` | `user` for a person (groups use other values) | `user` |
| `name` | Name. Up to 20 characters. | display name, truncated |
| `longName` | Key display, shown on the panel's buttons. Up to 16 characters. | display name, truncated |
| `displayedOrder` | Display priority, 1–10 | `5` |
| `tagId` | Index tabs, comma-separated (see below) | Title1 tab |
| `mail:` | `true` if the entry has an email destination | `true` |
| `mail:address` | Email address | user's email |
| `isSender` | Registered as an email sender | `false` |
| `isDestination` | Selectable as a destination | `true` |
| `auth:`, `auth:name` | User code | |
| `remoteFolder:*` | Scan-to-folder settings | |
| `lastAccessDateTime` | Last use, `1970-01-01T00:00:00Z` if never | |

Password properties (`*:password`) exist but are never read by the sync.

**`tagId` values**

| Value | Tab |
| --- | --- |
| 1 | Frequent |
| 2–11 | Title1: AB, CD, EF, GH, IJK, LMN, OPQ, RST, UVW, XYZ |
| 12–21 | Title2: 1–10 |
| 22–26 | Title3: 1–5 |

`tagId` is a single property, so writing it replaces every tab at once. An
entry needs at least one tab to appear on the panel. The sync writes only the
Title1 tab for the first letter of the name, ignoring accents. Names with no
letter A–Z get Frequent.

## Known model differences

- Some newer models refuse `putObjects` with `UDIRECTORY_PERMISSION_DENIED`
  while `deleteObjects` still works. Check that the account has the *user
  administrator* role. Some models restrict SOAP writes entirely.
- Field limits can differ by model. `probe --write-test` renames its test
  entry with a 48-character value and reports how many characters the printer
  kept.
