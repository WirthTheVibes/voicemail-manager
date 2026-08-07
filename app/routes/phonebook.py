"""
Unauthenticated Yealink Remote Phonebook -- serves the 3CX directory as a
single department-organized company phonebook at menu.xml (one file, one
Menu group per department -- see phonebook_xml.py's module docstring for why
this replaced an earlier menu.xml+department.xml split). Deliberately no
auth: Yealink's remote-phonebook fetch is a plain phone-initiated GET with
no cookie/token support to speak of, the same constraint noted in
routes/yealink.py's module docstring for the XML Browser screens -- and the
content here is no more sensitive than a printed office directory.

Extensions hidden via the admin Users tab (app_db.hidden_extensions, same
flag routes/admin.py's list_extensions exposes) are excluded here too.
"""
from collections import defaultdict

from fastapi import APIRouter, Response

from .. import app_db, phonebook_xml, threecx_db

router = APIRouter()

MEDIA_TYPE = "text/xml"


def _xml(body: str) -> Response:
    return Response(content=body, media_type=MEDIA_TYPE)


# 3CX's own internal system-owner group, not a real department -- excluded
# so it doesn't show up as a bogus "department" in the public phonebook.
_EXCLUDED_DEPARTMENTS = {"__DEFAULT__"}


@router.get("/menu.xml")
def menu():
    hidden = app_db.hidden_extensions()
    by_dept = defaultdict(list)
    for row in threecx_db.directory():
        if row["extension"] in hidden or row["department"] in _EXCLUDED_DEPARTMENTS:
            continue
        by_dept[row["department"] or "Other"].append(row)

    departments = []
    for dept in sorted(by_dept):
        rows = sorted(by_dept[dept], key=lambda r: (r["firstname"] or "", r["lastname"] or ""))
        contacts = [
            (
                " ".join(p for p in (row["firstname"], row["lastname"]) if p).strip()
                or row["extension"],
                row["extension"],
            )
            for row in rows
        ]
        departments.append((dept, contacts))

    return _xml(phonebook_xml.book("Company Directory", departments))
