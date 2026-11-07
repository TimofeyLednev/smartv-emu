# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded DNS parsing used by the optional Orsay compatibility bridge."""
import ipaddress
import struct


def dns_name(data, offset):
    labels, seen, end = [], set(), None
    for _ in range(128):
        if offset in seen or offset >= len(data):
            raise ValueError("Invalid DNS name")
        seen.add(offset)
        length = data[offset]
        if length & 0xc0 == 0xc0:
            if offset + 2 > len(data):
                raise ValueError("Truncated DNS pointer")
            end = end if end is not None else offset + 2
            offset = ((length & 63) << 8) | data[offset + 1]
            continue
        offset += 1
        if not length:
            return ".".join(labels), end if end is not None else offset
        if length > 63 or offset + length > len(data):
            raise ValueError("Invalid DNS label")
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    raise ValueError("DNS name exceeds bound")


def dns_message(data):
    if len(data) < 12:
        raise ValueError("Truncated DNS header")
    ident, flags, questions, answers, authority, additional = struct.unpack_from("!6H", data)
    if questions + answers + authority + additional > 512:
        raise ValueError("DNS record count exceeds bound")
    offset, queries, records = 12, [], []
    for _ in range(questions):
        name, offset = dns_name(data, offset)
        kind, class_ = struct.unpack_from("!HH", data, offset)
        offset += 4
        queries.append({"name": name, "type": kind, "class": class_})
    for _ in range(answers + authority + additional):
        name, offset = dns_name(data, offset)
        kind, class_, ttl, size = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        if offset + size > len(data):
            raise ValueError("Truncated DNS record")
        value = data[offset:offset + size]
        if kind in (1, 28) and size == (4 if kind == 1 else 16):
            text = str(ipaddress.ip_address(value))
        elif kind in (5, 2, 12):
            text = dns_name(data, offset)[0]
        else:
            text = None
        if text is not None:
            records.append({"name": name, "type": kind, "value": text, "ttl": ttl})
        offset += size
    return {"id": ident, "response": bool(flags & 0x8000), "rcode": flags & 15,
            "questions": queries, "records": records}
