#!/usr/bin/env python3
"""Self-check for the control-field surgery in make_repo.py.

Run: python3 scripts/test_make_repo.py

Only strip_fields is covered, because it is the one piece that can silently
corrupt a package: it walks a control file by indentation, and getting a
continuation line wrong either swallows a Description or leaves an orphaned
fragment that makes the whole stanza unparseable to Sileo.
"""

from make_repo import control_fields, strip_fields, version_key

CONTROL = """Package: com.example.app
Name: Example
Depiction: https://old.example.com/
Description: One line summary
 first continuation
 second continuation
SileoDepiction: https://old.example.com/depiction.json
Maintainer: someone
"""


def test_strips_target_fields_only():
    out = strip_fields(CONTROL, {"Depiction", "SileoDepiction"})
    assert "old.example.com" not in out, out
    assert "Package: com.example.app" in out
    assert "Maintainer: someone" in out


def test_keeps_continuation_lines_of_kept_fields():
    out = strip_fields(CONTROL, {"Depiction", "SileoDepiction"})
    assert " first continuation" in out, out
    assert " second continuation" in out, out
    assert control_fields(out)["Description"] == "One line summary"


def test_drops_continuation_lines_of_stripped_fields():
    control = "Description: gone\n more gone\nName: kept\n"
    out = strip_fields(control, {"Description"})
    assert out == "Name: kept", repr(out)


def test_absent_field_is_a_no_op():
    control = "Package: x\nName: y\n"
    assert strip_fields(control, {"Depiction"}) == "Package: x\nName: y"


def test_version_ordering_is_numeric_not_lexical():
    assert version_key("1.10.0") > version_key("1.9.0")


if __name__ == "__main__":
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
            print(f"ok  {name}")
    print("all passed")
