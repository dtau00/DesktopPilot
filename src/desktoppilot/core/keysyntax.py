"""uiautomation's SendKeys syntax, split into tokens: `{Ctrl}s`, `{Enter}`,
`{Ctrl}{Shift}p`, `{Delete 3}`, `{Ctrl}(ac)`. `^`, `%` and `+` are typed
characters, not modifiers.

`ui keys` on Windows hands the string to uiautomation itself; the Linux
emulation (`linux/input.py`) and `cdp keys` (`core/cdp.py`) read it here, so
the three agree on what a string means.
"""

from __future__ import annotations

from .errors import PilotError


def tokens(keys: str) -> list[tuple]:
    """('key', name, count) for {Name} or {Name N}, ('open',) and ('close',)
    for parentheses, ('char', c) for any other character."""
    found: list[tuple] = []
    i = 0
    while i < len(keys):
        ch = keys[i]
        if ch == "{":
            end = keys.find("}", i + 2 if keys[i + 1 : i + 2] == "}" else i + 1)
            if end == -1:
                raise PilotError(f'unclosed "{{" in {keys!r}; type "{{{{}}" for a brace')
            parts = keys[i + 1 : end].split()
            if keys[i + 1 : end] == "}":
                parts = ["}"]
            if not parts or len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
                raise PilotError(f"{keys[i : end + 1]!r} is not a key; e.g. {{Enter}} or {{a 3}}")
            found.append(("key", parts[0], int(parts[1]) if len(parts) == 2 else 1))
            i = end + 1
        else:
            found.append(("open",) if ch == "(" else ("close",) if ch == ")" else ("char", ch))
            i += 1
    return found
