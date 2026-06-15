"""logcat filter DSL — the adb-shaped `<tag-glob>:<level>` spec, subscriber-side.

The hose (log[logging]) stays dumb; consumers (tdb/rtdb logcat) filter the
stream with this. Shared by both so the two front-ends behave identically.

Grammar (a list of space-separated specs):

    <tag-glob>:<level>

  tag-glob : one of
               *               — every tag
               <App>           — match the tag (the line's `tag` field)
               <App>/<node>    — match tag AND node (the record's `node`)
             `/` is the app/node separator; `:` is ONLY the level separator.
  level    : V D I W E F  (verbose→fatal, the MINIMUM level to keep)
             S             — silent: suppress everything matching this glob

Semantics (adb logcat): each record is matched against the specs in order; the
FIRST whose tag-glob matches decides its minimum level (or suppresses it on S).
A record matching no spec falls to the default: keep at the lowest level UNLESS
any spec was given, in which case the convention is "an explicit spec list means
only what it allows" — we follow adb's `*:S <X>:V` idiom by making a trailing
`*:<level>` (or its absence) the catch-all. With NO specs, everything passes.

Examples:
    MyApp/counter:V *:E   verbose for MyApp's counter node, errors for the rest
    *:E                   errors only, every tag
    *:S sm:D              silence everything except the `sm` tag at DEBUG+
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import List, Optional

# Single-letter level codes → ordinal (matches LogLevel: V=0 … F=5).
_LEVEL_ORD = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5}
# S (silent) is a sentinel above every real level — nothing passes it.
_SILENT = 99


@dataclass
class _Spec:
    tag_glob: str           # fnmatch pattern for the tag ("*" = any)
    node_glob: Optional[str]  # fnmatch pattern for the node, or None (tag-only)
    min_ord: int            # minimum level ordinal to KEEP (or _SILENT)

    def matches(self, tag: str, node: str) -> bool:
        if not fnmatch.fnmatch(tag, self.tag_glob):
            return False
        if self.node_glob is not None and not fnmatch.fnmatch(node, self.node_glob):
            return False
        return True


class LogcatFilter:
    """A compiled list of `<tag-glob>:<level>` specs. `keep(rec)` decides."""

    def __init__(self, specs: "List[_Spec]"):
        self._specs = specs

    @classmethod
    def parse(cls, args: "List[str]") -> "LogcatFilter":
        """Compile the arg list into a filter. Raises ValueError on a bad spec."""
        specs: List[_Spec] = []
        for a in args:
            if ":" not in a:
                raise ValueError(
                    f"bad logcat spec {a!r} — expected <tag-glob>:<level> "
                    f"(e.g. MyApp/counter:V or *:E)")
            glob, level = a.rsplit(":", 1)
            level = level.upper()
            if level == "S":
                min_ord = _SILENT
            elif level in _LEVEL_ORD:
                min_ord = _LEVEL_ORD[level]
            else:
                raise ValueError(
                    f"bad level {level!r} in {a!r} — use one of "
                    f"V D I W E F (verbose→fatal) or S (silent)")
            if "/" in glob:
                tag_glob, node_glob = glob.split("/", 1)
                tag_glob = tag_glob or "*"
                node_glob = node_glob or "*"
            else:
                tag_glob, node_glob = (glob or "*"), None
            specs.append(_Spec(tag_glob, node_glob, min_ord))
        return cls(specs)

    def keep(self, *, tag: str, node: str, level_ord: int) -> bool:
        """Whether a record (tag, node, level ordinal) passes the filter."""
        if not self._specs:
            return True   # no specs → everything passes (default follow)
        for spec in self._specs:
            if spec.matches(tag, node):
                if spec.min_ord == _SILENT:
                    return False
                return level_ord >= spec.min_ord
        # Matched no spec. adb convention: an explicit list excludes the rest.
        return False
