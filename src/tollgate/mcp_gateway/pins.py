"""Tool definition pinning.

A tool's description is text the model reads and follows, so it's an
injection surface. Two attacks are worth catching at the gateway:

  rug pull   a server ships a harmless definition, gets approved, then changes
             the description or schema later ("...and also send the result to
             this address")
  hidden     instructions tucked into the description with zero-width, bidi or
             Unicode tag characters, invisible in any UI a human reviews

The first definition seen for each tool is pinned by hash. A later definition
with a different hash is drift. In "warn" mode drift is logged and the tool
passes; in "enforce" mode it's removed from tools/list and calls to it are
refused until someone resets the pin.
"""

import hashlib
import json
import threading
import unicodedata

from tollgate.log import Log

log = Log("mcp_gateway.pins")


def fingerprint(tool):
    material = {k: tool.get(k) for k in ("name", "title", "description", "inputSchema", "outputSchema",
                                         "annotations")}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hidden_characters(tool):
    text = json.dumps({k: tool.get(k) for k in ("title", "description", "inputSchema")}, ensure_ascii=False)
    return sorted({f"U+{ord(ch):04X}" for ch in text
                   if unicodedata.category(ch) == "Cf" or 0xE0000 <= ord(ch) <= 0xE007F})


class ToolPins:
    def __init__(self, mode="warn"):
        if mode not in ("off", "warn", "enforce"):
            raise ValueError(f"unknown pin mode {mode!r}")
        self.mode = mode
        self._pins = {}
        self._flagged = {}  # name -> reason
        self._lock = threading.Lock()

    def review(self, tools):
        """Check a tools/list result. Returns the tools to pass on."""
        if self.mode == "off":
            return tools
        keep = []
        with self._lock:
            for tool in tools:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    keep.append(tool)
                    continue
                name, digest = tool["name"], fingerprint(tool)
                reason = None
                hidden = hidden_characters(tool)
                if hidden:
                    reason = "hidden_characters"
                    log.warning("tool definition contains invisible characters", tool=name, codepoints=hidden,
                                mode=self.mode)
                pinned = self._pins.setdefault(name, digest)
                if pinned != digest:
                    reason = "definition_changed"
                    log.warning("tool definition changed since it was pinned", tool=name, pinned=pinned[:12],
                                seen=digest[:12], mode=self.mode)
                if reason:
                    self._flagged[name] = reason
                if reason and self.mode == "enforce":
                    continue
                keep.append(tool)
        return keep

    def blocked(self, name):
        """Reason to refuse a call to this tool, or None."""
        if self.mode != "enforce":
            return None
        return self._flagged.get(name)

    def reset(self):
        with self._lock:
            count = len(self._pins)
            self._pins.clear()
            self._flagged.clear()
        return count

    def snapshot(self):
        with self._lock:
            return {"mode": self.mode, "pinned": len(self._pins), "flagged": dict(self._flagged)}
