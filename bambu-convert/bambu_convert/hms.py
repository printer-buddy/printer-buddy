# Readable text for Bambu Lab print errors and HMS codes.
# hms_en.json.gz is taken unchanged from ha-bambulab (custom_components/bambu_lab/pybambu/hms_error_text/hms_en.json.gz),
# https://github.com/greghesp/ha-bambulab, under this licence:
#
# MIT License
#
# Copyright (c) 2023 ha-bambulab contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import gzip
import json
from pathlib import Path

_data: dict | None = None


def _table(kind: str) -> dict:
    global _data
    if _data is None:
        try:
            _data = json.load(gzip.open(Path(__file__).with_name("hms_en.json.gz"), "rt", encoding="utf-8"))
        except (OSError, ValueError):
            _data = {}
    return _data.get(kind) or {}


def _pick(entry: dict, model: str) -> str | None:
    # the table names models like "P1S", "A1MINI", "H2D"; a model-specific line wins over the general one
    m = model.upper().replace(" ", "")
    want = {m, m.removesuffix("PRO")}
    general = None
    for text, models in entry.items():
        if want & set(models):
            return text
        if not models and general is None:
            general = text
    return general


def print_error_code(code: int) -> str:
    s = f"{code:08X}"
    return f"{s[:4]}_{s[4:]}"


def print_error(code: int, model: str = "") -> str | None:
    entry = _table("device_error").get(f"{code:08X}")
    return _pick(entry, model) if entry else None


def notices(hms: list | None, model: str = "") -> list[str]:
    out = []
    for n in hms or []:
        if not isinstance(n, dict):
            continue
        try:
            attr, code = int(n.get("attr") or 0), int(n.get("code") or 0)
        except (TypeError, ValueError):
            continue
        if attr <= 0 or code <= 0:
            continue
        key = f"{attr:08X}{code:08X}"
        entry = _table("device_hms").get(key)
        text = _pick(entry, model) if entry else None
        label = f"HMS_{key[0:4]}_{key[4:8]}_{key[8:12]}_{key[12:16]}"
        line = f"{text} ({label})" if text else f"Printer notice {label}"
        if line not in out:
            out.append(line)
    return out
