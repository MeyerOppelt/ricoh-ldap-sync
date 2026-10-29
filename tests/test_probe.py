from ricoh_ldap_sync import probe


def test_mask_keeps_length_and_initials():
    assert probe.mask("Alice Anders") == "Axxxx Axxxxx"
    assert probe.mask("ZZ Sync Test") == "ZZ Sync Test"


def test_scrub_xml_masks_names_and_emails():
    raw = (b'<?xml version="1.0"?><r><item><propName>name</propName><propVal>Alice Anders</propVal></item>'
           b'<item><propName>mail:address</propName><propVal>alice@corp.example.com</propVal></item>'
           b'<item><propName>mail:address</propName><propVal>sync-test@invalid.example</propVal></item>'
           b'<stringOut>12345</stringOut></r>')
    out = probe.scrub_xml(raw)
    assert "Alice" not in out and "Axxxx Axxxxx" in out
    assert "alice@corp" not in out and "user1@example.invalid" in out
    assert "sync-test@invalid.example" in out
    assert "12345" not in out


def test_same_address_same_placeholder():
    a = probe.scrub_text("one@corp.example.com two@corp.example.com")
    b = probe.scrub_text("ONE@corp.example.com")
    assert a.split()[0] == b and a.split()[0] != a.split()[1]
