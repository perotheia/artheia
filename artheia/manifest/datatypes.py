from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv6Address
from typing import Any, NewType

#
# NewTypes
#

DiagnosticsId = NewType("DiagnosticsId", int)
CANId = NewType("CANId", int)
BazelTarget = NewType("BazelTarget", str)
Developer = NewType("Developer", str)
# HostOSVersion should be a string with format similar to x.y.z
# that correspond to a version built from other repos such as
# https://github.com/ExtAppliedVehicleOS/sa8295p-hqx-4-2-4-0_hlos_dev_qnx
HostOSVersion = NewType("HostOSVersion", str)
HostOSContentHash = NewType("HostOSContentHash", str)
DoipAddress = NewType("DoipAddress", int)
ImageTag = NewType("ImageTag", str)

IPAddress = IPv4Address | IPv6Address

# Re-export the ipaddress types so vehicle configs can pull them from one place.
__all__ = [
    "BazelTarget",
    "CANId",
    "Developer",
    "DiagnosticsId",
    "DoipAddress",
    "HostOSContentHash",
    "HostOSVersion",
    "IPAddress",
    "IPv4Address",
    "IPv6Address",
    "Identity",
    "ImageTag",
    "MACAddress",
]


class MACAddress:
    def __init__(self, addr: MACAddress | str | list[int]):
        self._addr: list[int] = []
        if isinstance(addr, MACAddress):
            self._addr = addr._addr[:]
        elif isinstance(addr, str):
            try:
                self._addr = list(int(x, 16) for x in addr.split(":"))
            except Exception as e:
                raise ValueError(f"Error while parsing MAC address {addr}: {e}")
        else:
            self._addr = addr[:]

        if len(self._addr) != 6:
            raise ValueError(
                f"Malformed MAC address: {self._addr} must have 6 segments"
            )

        if not all(map(lambda x: 0 <= x < 256, self._addr)):
            raise ValueError(
                f"Malformed MAC address: {self._addr} segments must be between 0 and 255"
            )

    @property
    def as_ints(self) -> list[int]:
        return self._addr[:]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, MACAddress):
            return self._addr == other._addr
        else:
            return False

    def __hash__(self) -> int:
        return hash(str(self))

    def __str__(self) -> str:
        return ":".join(f"{x:02X}" for x in self._addr)


class Identity(str):
    """
    General purpose identifier.
        This identifier must be unique within the context that it is used in.
        The string itself must be valid as a variable name in Python and C.
    """

    # Class-level regex pattern for validation
    _variable_name_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    def __new__(cls: Any, value: str) -> Any:
        # Validate the string before creating the object
        if not cls._variable_name_pattern.match(value):
            raise ValueError(f"'{value}' is not a valid identity name.")
        # Create and return the instance
        return super().__new__(cls, value)
