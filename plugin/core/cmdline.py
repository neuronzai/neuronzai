#!/usr/bin/env python3
# Recognizing OUR OWN command-side helper in a shell call a PreToolUse hook sees.
#
# Several bundled commands work the same way: the command body tells the model to
# run a small CLI under hooks/, and a PreToolUse hook intercepts that call to do
# the work with something only the hook has (the session id on the payload, or the
# ability to return a host directive). Every one of those hooks needs the same
# question answered — "is this command actually INVOKING my helper, or merely
# mentioning its name?" — and getting it wrong in either direction is a real bug:
# intercept a `cat hooks/switch_profile.py` and the user's read is silently
# replaced by a profile switch; miss a genuine `python3 …/reload_assets.py` and
# the helper runs without the thing the hook was there to supply.
#
# Pure stdlib, no host names, no I/O.

import shlex


def tokenize(command):
    """Split a shell command the way the user's shell would, falling back to a naive
    split when the quoting is malformed (an unbalanced quote must not make an
    interceptor blind — it would pass a command through unexamined)."""
    try:
        return shlex.split(command or "")
    except ValueError:
        return (command or "").split()


def invokes_helper(tokens, helper):
    """True iff `helper` (a bare filename, e.g. 'reload_assets.py') is the EXEC TARGET
    of `tokens`, not an argument to something else.

    The target is token 0 (`reload_assets.py …`, `./hooks/reload_assets.py …`) or the
    token right after a python interpreter (`python3 …/reload_assets.py …`). That
    rejects `cat …/reload_assets.py`, `grep foo reload_assets.py`, `ls hooks/` and
    every other command where our helper's name is data."""
    for index, token in enumerate(tokens):
        if token.split("/")[-1] != helper:
            continue
        if index == 0:
            return True
        previous = tokens[index - 1].split("/")[-1]
        return previous.startswith("python")  # interpreter -> genuine invocation; else an arg
    return False


def helper_invocation(command, helper):
    """The tokens of a genuine invocation of `helper`, or None when this command is
    not one. Callers parse their own flags off the returned tokens."""
    if helper not in (command or ""):
        return None
    tokens = tokenize(command)
    return tokens if invokes_helper(tokens, helper) else None
