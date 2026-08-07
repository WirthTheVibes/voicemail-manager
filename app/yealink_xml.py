"""
Plain string builders for Yealink's XML Browser objects (see
notes/xmldevguide.pdf / notes/README.md for the spec). No XML-generation
helper existed anywhere in this repo before this feature -- f-strings match
Yealink's exact fixed tag shapes more directly than xml.etree would.
"""
from xml.sax.saxutils import escape as _escape

_HEADER = '<?xml version="1.0" encoding="ISO-8859-1"?>\n'


def esc(value) -> str:
    return _escape(str(value) if value is not None else "")


def esc_attr(value) -> str:
    return _escape(str(value) if value is not None else "", {'"': "&quot;"})


def _softkeys_xml(softkeys: list[tuple[int, str, str]]) -> str:
    # softkeys is (index, label, uri) -- callers pick the literal index so a
    # given physical button (e.g. the 4th) always does the same thing (Exit)
    # across every screen, instead of whatever happens to be last in a list.
    return "\n".join(
        f'  <SoftKey index="{index}"><Label>{esc(label)}</Label><URI>{esc(uri)}</URI></SoftKey>'
        for index, label, uri in softkeys
    )


def pin_prompt(extension: str, submit_url: str, error: str | None = None) -> str:
    title = error or "Enter Voicemail PIN"
    return (
        f'{_HEADER}'
        # defaultIndex="2" starts the cursor on the PIN field, not the
        # read-only "ext" carrier field before it -- without this the user
        # has to press down once before they can type anything.
        f'<YealinkIPPhoneInputScreen Beep="no" Timeout="60" defaultIndex="2">\n'
        f'  <Title wrap="yes">{esc(title)}</Title>\n'
        f'  <URL>{esc(submit_url)}</URL>\n'
        f'  <InputField type="empty" editable="no">\n'
        f'    <Parameter>ext</Parameter>\n'
        f'    <Default>{esc(extension)}</Default>\n'
        f'  </InputField>\n'
        f'  <InputField type="number" password="yes">\n'
        f'    <Prompt>PIN:</Prompt>\n'
        f'    <Parameter>pin</Parameter>\n'
        f'    <Default></Default>\n'
        f'  </InputField>\n'
        f'</YealinkIPPhoneInputScreen>\n'
    )


def pin_change_prompt(submit_url: str, error: str | None = None) -> str:
    title = error or "Change Voicemail PIN"
    return (
        f'{_HEADER}'
        f'<YealinkIPPhoneInputScreen Beep="no" Timeout="60" defaultIndex="1">\n'
        f'  <Title wrap="yes">{esc(title)}</Title>\n'
        f'  <URL>{esc(submit_url)}</URL>\n'
        f'  <InputField type="number" password="yes">\n'
        f'    <Prompt>New PIN:</Prompt>\n'
        f'    <Parameter>pin1</Parameter>\n'
        f'    <Default></Default>\n'
        f'  </InputField>\n'
        f'  <InputField type="number" password="yes">\n'
        f'    <Prompt>Confirm PIN:</Prompt>\n'
        f'    <Parameter>pin2</Parameter>\n'
        f'    <Default></Default>\n'
        f'  </InputField>\n'
        f'</YealinkIPPhoneInputScreen>\n'
    )


def text_menu(
    title: str,
    items: list[tuple[str, str]],
    softkeys: list[tuple[int, str, str]],
    style: str = "numbered",
) -> str:
    items_xml = "\n".join(
        f'  <MenuItem>\n    <Prompt>{esc(prompt)}</Prompt>\n    <URI>{esc(uri)}</URI>\n  </MenuItem>'
        for prompt, uri in items
    )
    return (
        f'{_HEADER}'
        f'<YealinkIPPhoneTextMenu style="{style}" Beep="no" Timeout="60">\n'
        f'  <Title wrap="yes">{esc(title)}</Title>\n'
        f'{items_xml}\n'
        f'{_softkeys_xml(softkeys)}\n'
        f'</YealinkIPPhoneTextMenu>\n'
    )


def text_screen(
    title: str,
    text: str,
    softkeys: list[tuple[int, str, str]],
    done_action: str | None = None,
    lock_in: bool = False,
) -> str:
    attrs = 'Beep="no" Timeout="60"'
    if lock_in:
        attrs += ' LockIn="yes"'
    if done_action:
        attrs += f' doneAction="{esc_attr(done_action)}"'
    return (
        f'{_HEADER}'
        f'<YealinkIPPhoneTextScreen {attrs}>\n'
        f'  <Title wrap="yes">{esc(title)}</Title>\n'
        f'  <Text>{esc(text)}</Text>\n'
        f'{_softkeys_xml(softkeys)}\n'
        f'</YealinkIPPhoneTextScreen>\n'
    )


def execute(uri: str) -> str:
    return (
        f'{_HEADER}'
        f'<YealinkIPPhoneExecute Beep="no">\n'
        f'  <ExecuteItem URI="{esc_attr(uri)}"/>\n'
        f'</YealinkIPPhoneExecute>\n'
    )
