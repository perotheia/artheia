#!/usr/bin/env python3
"""
psp_signal_filter.py

REPL tool for searching platform signals and building a demo_signals.csv.
Loads FIBEX + DBC sources via PlatformDb, then provides an interactive
search-and-select REPL.  On exit, prints the accumulated selection as CSV.

Usage:
  python3 tools/psp_signal_filter.py --config $PSP_ROOT/config
  python3 tools/psp_signal_filter.py \\
      --fibex configs/MLBevo.xml \\
      --dbc configs/dbc/KCAN.dbc:kcan \\
      --dbc configs/dbc/HCAN.dbc:hcan

Commands inside REPL:
  <signal_name>          exact or prefix match across all buses
  msg:<name>             list all signals in a PDU (FlexRay) or CAN message
  bus:<name>             list all messages on a CAN bus
  sel                    show current selection
  del <signal_name>      remove from selection
  csv                    print selection as CSV to stdout
  clear                  clear selection
  help                   show commands
  q / quit / exit        exit (prints CSV before exiting)
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))

from ..importers._asam_cmp_parser import PlatformDb, FibexDb, DbcDb


# ---------------------------------------------------------------------------
# Bus name extraction from DBC filename
# Pattern: extract keyword between MLBevo_ and _KMatrix
# e.g. MLBevo_Gen2_MLBevo_KCAN_KMatrix_V8.27.01F.dbc → kcan
# ---------------------------------------------------------------------------

_BUS_PATTERN = re.compile(r'_([A-Za-z0-9]+CAN)_KMatrix', re.IGNORECASE)
_BUS_PATTERN2 = re.compile(r'_([A-Za-z0-9]*CAN[A-Za-z0-9]*)_', re.IGNORECASE)

def _derive_bus_name(dbc_path: str) -> str:
    basename = os.path.basename(dbc_path)
    m = _BUS_PATTERN.search(basename)
    if m:
        return m.group(1).lower()
    # Fallback: any *CAN* token
    m = _BUS_PATTERN2.search(basename)
    if m:
        return m.group(1).lower()
    # Last fallback: stem without extension
    stem = os.path.splitext(basename)[0]
    return stem.lower().replace(' ', '_')


# ---------------------------------------------------------------------------
# Signal match result
# ---------------------------------------------------------------------------

class MatchResult:
    """Represents a single matched signal from any bus."""
    def __init__(self, signal_name: str, container_name: str,
                 bus: str, source: str) -> None:
        self.signal_name    = signal_name     # signal / field name
        self.container_name = container_name  # PDU name (FR) or message name (CAN)
        self.bus            = bus             # 'flexray' or 'can:kcan' etc.
        self.source         = source          # 'fibex' or 'dbc'

    def csv_row(self) -> str:
        if self.source == 'fibex':
            return f"{self.signal_name},{self.container_name}"
        else:
            return f"{self.signal_name},{self.container_name}"

    def __str__(self) -> str:
        return f"  [{self.bus}] {self.container_name} → {self.signal_name}"


# ---------------------------------------------------------------------------
# Database wrapper for search
# ---------------------------------------------------------------------------

class SignalIndex:
    """Flat index of all signals across FIBEX + DBC sources."""

    def __init__(self) -> None:
        # (signal_name_lower, MatchResult)
        self._index: List[Tuple[str, MatchResult]] = []
        self._fr_pdus: Dict[str, List[str]] = {}   # pdu_name → [signal_names]
        self._can_msgs: Dict[str, Dict[str, List[str]]] = {}  # bus → msg → [sigs]

    def load_fibex(self, db: FibexDb) -> None:
        for pdu in db.pdus.values():
            if not pdu.name:
                continue
            pdu_signals: List[str] = []
            for si in pdu.signal_instances:
                if si.signal and si.signal.name:
                    pdu_signals.append(si.signal.name)
                    mr = MatchResult(
                        signal_name=si.signal.name,
                        container_name=pdu.name,
                        bus='flexray',
                        source='fibex',
                    )
                    self._index.append((si.signal.name.lower(), mr))
            if pdu_signals:
                self._fr_pdus[pdu.name] = pdu_signals

    def load_dbc(self, db: DbcDb, bus: str) -> None:
        bus_key = f"can:{bus}"
        self._can_msgs[bus] = {}
        for msg_name, msg in db.messages.items():
            sig_names: List[str] = []
            for sig_name in msg.signals:
                sig_names.append(sig_name)
                mr = MatchResult(
                    signal_name=sig_name,
                    container_name=msg_name,
                    bus=bus_key,
                    source='dbc',
                )
                self._index.append((sig_name.lower(), mr))
            if sig_names:
                self._can_msgs[bus][msg_name] = sig_names

    def search(self, query: str) -> List[MatchResult]:
        """Exact + prefix match, case-insensitive. Returns up to first 50."""
        ql = query.lower()
        exact: List[MatchResult]  = []
        prefix: List[MatchResult] = []
        for key, mr in self._index:
            if key == ql:
                exact.append(mr)
            elif key.startswith(ql):
                prefix.append(mr)
        # Deduplicate by (signal_name, container_name, bus)
        seen: set = set()
        results: List[MatchResult] = []
        for mr in exact + prefix:
            k = (mr.signal_name, mr.container_name, mr.bus)
            if k not in seen:
                seen.add(k)
                results.append(mr)
        return results[:50]

    def list_pdu(self, pdu_name: str) -> Optional[List[str]]:
        """List all signals in a FlexRay PDU by name."""
        # Try exact, then case-insensitive
        if pdu_name in self._fr_pdus:
            return self._fr_pdus[pdu_name]
        pdu_lower = pdu_name.lower()
        for name, sigs in self._fr_pdus.items():
            if name.lower() == pdu_lower:
                return sigs
        # Try CAN messages too
        for bus, msgs in self._can_msgs.items():
            if pdu_name in msgs:
                return msgs[pdu_name]
            for mname, sigs in msgs.items():
                if mname.lower() == pdu_lower:
                    return sigs
        return None

    def list_bus(self, bus_name: str) -> Optional[Dict[str, List[str]]]:
        """List all messages on a CAN bus."""
        if bus_name in self._can_msgs:
            return self._can_msgs[bus_name]
        bus_lower = bus_name.lower()
        for bname, msgs in self._can_msgs.items():
            if bname.lower() == bus_lower:
                return msgs
        return None

    @property
    def total_signals(self) -> int:
        return len(self._index)

    @property
    def total_pdus(self) -> int:
        return len(self._fr_pdus)

    @property
    def total_can_messages(self) -> int:
        return sum(len(msgs) for msgs in self._can_msgs.values())


# ---------------------------------------------------------------------------
# Selection accumulator
# ---------------------------------------------------------------------------

class Selection:
    def __init__(self) -> None:
        # key: signal_name  value: MatchResult (last one wins if duplicate names)
        self._items: Dict[str, MatchResult] = {}

    def add(self, mr: MatchResult) -> None:
        self._items[mr.signal_name] = mr

    def remove(self, signal_name: str) -> bool:
        if signal_name in self._items:
            del self._items[signal_name]
            return True
        # Try case-insensitive
        sl = signal_name.lower()
        for key in list(self._items.keys()):
            if key.lower() == sl:
                del self._items[key]
                return True
        return False

    def clear(self) -> None:
        self._items.clear()

    def show(self) -> None:
        if not self._items:
            print("  (empty selection)")
            return
        for mr in self._items.values():
            print(str(mr))

    def as_csv(self) -> str:
        if not self._items:
            return ""
        lines = ["signal_name,pdu_name"]
        for mr in self._items.values():
            lines.append(mr.csv_row())
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._items)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

HELP_TEXT = """\
Commands:
  <signal_name>       search for signal (exact/prefix match)
  msg:<name>          list all signals in a PDU or CAN message
  bus:<name>          list all messages on a CAN bus
  sel                 show current selection
  del <signal>        remove signal from selection
  csv                 print selection as CSV
  clear               clear selection
  help                show this help
  q / quit / exit     exit (prints CSV)
"""


def _print_csv(sel: Selection) -> None:
    csv_text = sel.as_csv()
    if csv_text:
        print("\n=== Selected signals CSV ===")
        print(csv_text)
        print("============================")
    else:
        print("(no signals selected)")


def run_repl(index: SignalIndex, sel: Selection) -> None:
    print(f"\nLoaded: {index.total_signals} signals, "
          f"{index.total_pdus} FlexRay PDUs, "
          f"{index.total_can_messages} CAN messages")
    print("Type 'help' for commands, 'q' to quit.\n")

    while True:
        try:
            line = input("filter> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            _print_csv(sel)
            return

        if not line:
            continue

        if line in ('q', 'quit', 'exit'):
            _print_csv(sel)
            return

        if line == 'help':
            print(HELP_TEXT)
            continue

        if line == 'sel':
            print(f"Selection ({len(sel)} signals):")
            sel.show()
            continue

        if line == 'csv':
            _print_csv(sel)
            continue

        if line == 'clear':
            sel.clear()
            print("Selection cleared.")
            continue

        if line.startswith('del '):
            sig_name = line[4:].strip()
            if sel.remove(sig_name):
                print(f"  Removed: {sig_name}")
            else:
                print(f"  Not in selection: {sig_name}")
            continue

        if line.startswith('msg:'):
            container = line[4:].strip()
            signals = index.list_pdu(container)
            if signals is None:
                print(f"  Not found: '{container}'")
            else:
                print(f"  Signals in '{container}' ({len(signals)}):")
                for s in signals:
                    in_sel = " [selected]" if s in sel._items else ""
                    print(f"    {s}{in_sel}")
            continue

        if line.startswith('bus:'):
            bus_name = line[4:].strip()
            msgs = index.list_bus(bus_name)
            if msgs is None:
                print(f"  Bus not found: '{bus_name}'")
                print(f"  Available buses: {list(index._can_msgs.keys())}")
            else:
                print(f"  Messages on bus '{bus_name}' ({len(msgs)}):")
                for mname, sigs in sorted(msgs.items()):
                    print(f"    {mname} ({len(sigs)} signals)")
            continue

        # Default: search for signal
        results = index.search(line)
        if not results:
            print(f"  No matches for '{line}'")
            continue

        if len(results) == 1:
            mr = results[0]
            sel.add(mr)
            print(f"  Auto-added: {mr}")
            continue

        # Multiple matches
        show = results[:10]
        print(f"  {len(results)} match(es) for '{line}'"
              + (f" (showing first 10)" if len(results) > 10 else "") + ":")
        for i, mr in enumerate(show, start=1):
            in_sel = " [selected]" if mr.signal_name in sel._items else ""
            print(f"  {i:2}. {mr}{in_sel}")

        try:
            choice = input("  Pick number (Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            continue

        if not choice:
            continue

        try:
            n = int(choice)
            if 1 <= n <= len(show):
                mr = show[n - 1]
                sel.add(mr)
                print(f"  Added: {mr}")
            else:
                print(f"  Invalid choice: {n}")
        except ValueError:
            print("  Cancelled.")


# ---------------------------------------------------------------------------
# Auto-discovery from --config dir
# ---------------------------------------------------------------------------

_FIBEX_BUS_RE = re.compile(r'_([A-Za-z0-9]+CAN)_KMatrix', re.IGNORECASE)

def discover_config_dir(config_dir: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Auto-discover FIBEX XML + DBC files in a config directory.
    Returns (fibex_paths, [(dbc_path, bus_name), ...])
    """
    fibex_paths: List[str] = []
    dbc_pairs:   List[Tuple[str, str]] = []

    # .xml files in the dir (not recursive for FIBEX)
    for entry in sorted(os.listdir(config_dir)):
        if entry.lower().endswith('.xml'):
            fibex_paths.append(os.path.join(config_dir, entry))

    # dbc/*.dbc files
    dbc_dir = os.path.join(config_dir, 'dbc')
    if os.path.isdir(dbc_dir):
        for entry in sorted(os.listdir(dbc_dir)):
            if entry.lower().endswith('.dbc'):
                dbc_path = os.path.join(dbc_dir, entry)
                bus_name = _derive_bus_name(dbc_path)
                dbc_pairs.append((dbc_path, bus_name))

    return fibex_paths, dbc_pairs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    config_dir: Optional[str] = None,
    fibex_paths: Optional[List[str]] = None,
    dbc_specs: Optional[List[str]] = None,
) -> None:
    """Launch the interactive signal-filter REPL.

    `dbc_specs` entries are "PATH:BUS" strings; if no `:` is present the bus
    name is derived from the filename.
    """
    fibex_paths = list(fibex_paths or [])
    dbc_pairs: List[Tuple[str, str]] = []

    for spec in dbc_specs or []:
        if ":" in spec:
            path, bus = spec.rsplit(":", 1)
        else:
            path = spec
            bus = _derive_bus_name(path)
        dbc_pairs.append((path, bus))

    if config_dir:
        cfg_fibex, cfg_dbc = discover_config_dir(config_dir)
        fibex_paths.extend(cfg_fibex)
        dbc_pairs.extend(cfg_dbc)

    if not fibex_paths and not dbc_pairs:
        raise ValueError("Provide --config dir or at least one --fibex / --dbc argument")

    index = SignalIndex()
    db = PlatformDb()

    for fpath in fibex_paths:
        print(f"Loading FIBEX: {fpath}")
        try:
            db.load_fibex(fpath)
            fr = db.flexray
            assert fr is not None
            index.load_fibex(fr)
            print(f"  OK — {len(fr.pdus)} PDUs, {len(fr.signals)} signals")
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)

    for dbc_path, bus_name in dbc_pairs:
        print(f"Loading DBC ({bus_name}): {dbc_path}")
        try:
            db.load_dbc(dbc_path, bus=bus_name)
            dbc_db = db.can(bus_name)
            index.load_dbc(dbc_db, bus_name)
            print(f"  OK — {dbc_db.message_count()} messages, "
                  f"{dbc_db.signal_count()} signals")
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)

    sel = Selection()
    try:
        run_repl(index, sel)
    except Exception as exc:
        print(f"\nFatal error: {exc}", file=sys.stderr)
        _print_csv(sel)
        sys.exit(1)
