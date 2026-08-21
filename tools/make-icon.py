#!/usr/bin/env python3
# Copyright (c) 2026 Renaud Allard <renaud@allard.it>
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Draw the integration icon.

A neutral meter index glyph: a counter strip in a rounded body.  It deliberately
does not imitate Eneco's own branding.
"""

import pathlib
import sys

from PIL import Image, ImageDraw

SIZE = 512
BODY = "#2c3a47"
FACE = "#f4f6f8"
DIGIT = "#2c3a47"
ACCENT = "#f0b429"

# HACS looks for <content path>/brand/icon.png unless hacs.json sets
# content_in_root, and Home Assistant serves the same directory itself.
TARGETS = (
    ("custom_components/eneco_be/brand/icon.png", 256),
    ("custom_components/eneco_be/brand/icon@2x.png", 512),
)


def draw() -> Image.Image:
    """Return the icon at full resolution."""
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    pen.rounded_rectangle((36, 36, SIZE - 36, SIZE - 36), radius=104, fill=BODY)

    # The counter face, with five index wheels.
    face = (84, 196, SIZE - 84, 316)
    pen.rounded_rectangle(face, radius=24, fill=FACE)
    left, top, right, bottom = face
    cells = 5
    step = (right - left) / cells
    for index in range(1, cells):
        x = left + step * index
        pen.line((x, top + 14, x, bottom - 14), fill=BODY, width=6)
    for index in range(cells):
        x = left + step * index + step / 2
        pen.rounded_rectangle(
            (x - step / 5, top + 34, x + step / 5, bottom - 34),
            radius=8,
            fill=DIGIT,
        )

    # A bolt above the counter, so the icon reads as electricity.
    pen.polygon(
        [(268, 96), (186, 176), (238, 176), (216, 244), (300, 158), (246, 158)],
        fill=ACCENT,
    )
    return image


def main() -> int:
    """Write every size and report what was produced."""
    root = pathlib.Path(__file__).resolve().parent.parent
    full = draw()
    for name, size in TARGETS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        full.resize((size, size), Image.Resampling.LANCZOS).save(
            path, "PNG", optimize=True
        )
        print(f"{name} ({size}x{size}, {path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
