"""Bind Mcp-Session-Id to whoever opened the session.

A session id is a bearer capability as far as the downstream is concerned: send
the id, get the session. The MCP security guidance says not to use sessions for
auth, and the downstream here doesn't, but a proxy that authenticates the
caller and then forwards any session id it's handed lets a viewer ride an
admin's session. So each id is tied to the subject that created it, and a
mismatch looks exactly like an unknown session.
"""

import threading
from collections import OrderedDict


class SessionBindings:
    def __init__(self, max_sessions=10_000):
        self._owners = OrderedDict()
        self._max = max_sessions
        self._lock = threading.Lock()

    def bind(self, session_id, subject):
        with self._lock:
            self._owners[session_id] = subject
            self._owners.move_to_end(session_id)
            while len(self._owners) > self._max:
                self._owners.popitem(last=False)

    def check(self, session_id, subject):
        """True if this subject may use this session.

        An id we've never seen (the gateway restarted, or the session was
        evicted) is bound to the first subject that presents it. Ids are
        128-bit random, so being first to an unseen one isn't something an
        attacker can arrange.
        """
        with self._lock:
            owner = self._owners.get(session_id)
            if owner is None:
                self._owners[session_id] = subject
                return True
            self._owners.move_to_end(session_id)
            return owner == subject

    def forget(self, session_id):
        with self._lock:
            self._owners.pop(session_id, None)

    def __len__(self):
        return len(self._owners)
