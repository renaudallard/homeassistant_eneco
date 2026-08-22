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

"""Check the Eneco portal still behaves the way this integration expects.

Read only: it signs in, walks the discovery chain and reads back the registers
and recent readings of every meter the integration would set up an entry for,
electricity and gas alike. Nothing is ever submitted.

The portal has no public API and no versioning, so when Eneco redeploys the
site this is what turns "the integration stopped working" into "this endpoint
moved".  It runs the integration's own client over a session built like the one
Home Assistant hands it, so a pass here means the config flow will work.

    ENECO_EMAIL=you@example.com tools/check-portal.py

The password is asked for interactively unless ENECO_PASSWORD is set.  Neither
value is printed or written anywhere.
"""

import argparse
import asyncio
from collections.abc import Callable
import getpass
import logging
import os
from pathlib import Path
import sys

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.eneco_be.api import (
    EnecoAuthError,
    EnecoClient,
    EnecoConnectionError,
    EnecoError,
)

# What homeassistant.helpers.aiohttp_client puts on every request.  Using the
# same one matters: a WAF that dislikes it would break the integration but not
# a check that quietly identified itself as something else.
SERVER_SOFTWARE = "HomeAssistant/2026.8.2 aiohttp/3.14.3 Python/3.14"

# The kinds of meter the integration will set up an entry for.
METERED = ("Electricity", "Gas")

type Formatter = Callable[[str, int], str]


def mask(value: str, keep: int = 4) -> str:
    """Return a value with all but its last characters hidden."""
    if not value:
        return "(empty)"
    return "…" + value[-keep:] if len(value) > keep else "…"


def plain(value: str, keep: int = 4) -> str:
    """Return the value unchanged, for --full."""
    return value


def credentials() -> tuple[str, str]:
    """Return the email and password, or exit explaining how to supply them."""
    email = os.environ.get("ENECO_EMAIL", "")
    password = os.environ.get("ENECO_PASSWORD", "")
    if email and password:
        return email, password
    if not sys.stdin.isatty():
        print(
            "No credentials. Set ENECO_EMAIL and ENECO_PASSWORD, or run this "
            "from a terminal so it can ask."
        )
        raise SystemExit(2)
    return (
        email or input("Eneco email: ").strip(),
        password or getpass.getpass("Eneco password: "),
    )


async def check(email: str, password: str, show: Formatter) -> int:
    """Walk the whole read-only chain, reporting each step."""
    session = aiohttp.ClientSession(headers={"User-Agent": SERVER_SOFTWARE})
    client = EnecoClient(session, email, password)
    try:
        print(f"user agent  : {SERVER_SOFTWARE}")
        print("signing in  : ", end="", flush=True)
        await client.async_login()
        print("OK")

        accounts = await client.async_get_accounts()
        print(f"accounts    : {len(accounts)}")

        for account in accounts:
            print(f"  {show(account.number, 4)}  {account.alias!r}")
            addresses = [
                address
                for address in await client.async_get_addresses(account.number)
                if address.has_active_contracts
            ]
            print(f"  addresses : {len(addresses)} with an active contract")

            for address in addresses:
                print(f"    {show(address.identifier, 6)}  {address.label}")
                points = await client.async_get_delivery_points(
                    account.number, address.identifier
                )
                for point in points:
                    print(f"      {point.energy_type:12} EAN {show(point.ean, 6)}")

                for point in points:
                    if point.energy_type not in METERED:
                        continue
                    meter = await client.async_get_meter(account.number, point.ean)
                    print(
                        f"      registers for {meter.energy_type}"
                        f" EAN {show(meter.ean, 6)}:"
                    )
                    for register in meter.registers:
                        print(
                            f"        {register.key:24}"
                            f" ({register.direction}/{register.timeframe})"
                        )

                    readings = await client.async_get_readings(
                        account.number, point.ean
                    )
                    print(f"      {len(readings)} reading(s) on record")
                    for reading in readings[:3]:
                        values = " ".join(
                            f"{k}={v}" for k, v in sorted(reading.values.items())
                        )
                        print(
                            f"        {reading.date}  {reading.reading_type:14}"
                            f" {values}"
                        )
    except EnecoAuthError as err:
        print(f"\nAUTH FAILED: {err}")
        return 1
    except EnecoConnectionError as err:
        print(f"\nCONNECTION FAILED: {err}")
        return 1
    except EnecoError as err:
        print(f"\nFAILED: {err}")
        return 1
    finally:
        await session.close()

    print("\nthe portal still matches docs/API.md, nothing was submitted")
    return 0


def main() -> int:
    """Parse the arguments and run the check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="do not mask identifiers")
    parser.add_argument("--debug", action="store_true", help="log every request")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")

    email, password = credentials()
    show: Formatter = plain if args.full else mask
    return asyncio.run(check(email, password, show))


if __name__ == "__main__":
    sys.exit(main())
