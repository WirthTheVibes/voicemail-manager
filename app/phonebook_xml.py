"""
Plain string builder for Yealink's Remote Phone Book XML feature.

One consolidated <YealinkIPPhoneBook>, with each department as its own
<Menu Name="..."> group of self-closing <Unit Name="..." Phone1="..."
Phone2="" Phone3="" default_photo="Resource:"/> entries -- attributes, not
<Name>/<Phone> child elements (cross-confirmed against two independent
worked examples of this format).

Earlier attempts split this into a menu.xml index (<YealinkIPPhoneMenu> with
<MenuItem>/<URL> per department, pointing at a separate department.xml)
plus a flattened department.xml with no Menu wrapper at all. Both still
produced an extra "Admin > Admin > <contact>" selection on a real T54W --
the two-file MenuItem/URL indirection itself was the redundant hop, not the
inner Menu wrapper. A single file with one Menu level per department (this
module) is the documented shape and needs only one press per level:
department, then contact.
"""
from xml.sax.saxutils import escape as _escape

_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'


def esc(value) -> str:
    return _escape(str(value) if value is not None else "")


def esc_attr(value) -> str:
    return _escape(str(value) if value is not None else "", {'"': "&quot;"})


def book(title: str, departments: list[tuple[str, list[tuple[str, str]]]]) -> str:
    # departments is (department_name, [(contact_name, extension), ...]).
    menus = []
    for dept_name, contacts in departments:
        units = "\n".join(
            f'    <Unit Name="{esc_attr(name)}" Phone1="{esc_attr(extension)}" '
            f'Phone2="" Phone3="" default_photo="Resource:"/>'
            for name, extension in contacts
        )
        menus.append(f'  <Menu Name="{esc_attr(dept_name)}">\n{units}\n  </Menu>')
    return (
        f'{_HEADER}'
        f'<YealinkIPPhoneBook>\n'
        f'  <Title>{esc(title)}</Title>\n'
        f'{chr(10).join(menus)}\n'
        f'</YealinkIPPhoneBook>\n'
    )
