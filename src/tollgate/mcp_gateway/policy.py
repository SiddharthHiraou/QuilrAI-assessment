"""Which roles may call which tools.

Ordered rules, first match wins, with a default for tools no rule names. The
default policy is the brief's: admin_* needs admin, anything else is open to
any known role. An unknown role matches nothing and gets nothing.

Matching is case-insensitive. Tool names are just strings to the server, and
"ADMIN_reset_key" shouldn't be a way around a rule written as admin_*.
"""

import fnmatch
import tomllib
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    pattern: str
    roles: frozenset

    def matches(self, tool):
        return fnmatch.fnmatchcase(tool.casefold(), self.pattern.casefold())


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str
    required_roles: tuple


class Policy:
    def __init__(self, rules, default_roles):
        self.rules = list(rules)
        self.default_roles = frozenset(default_roles)

    @classmethod
    def default(cls):
        return cls([Rule("admin_*", frozenset({"admin"}))], {"admin", "viewer"})

    @classmethod
    def from_toml(cls, path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        rules = [Rule(r["match"], frozenset(r["roles"])) for r in raw.get("rule", [])]
        return cls(rules, raw.get("default_roles", []))

    def decide(self, role, tool):
        for rule in self.rules:
            if rule.matches(tool):
                return Decision(role in rule.roles, rule.pattern, tuple(sorted(rule.roles)))
        return Decision(role in self.default_roles, "(default)", tuple(sorted(self.default_roles)))
