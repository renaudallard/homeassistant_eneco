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

"""Constants for the Eneco Belgium integration."""

from typing import Final

DOMAIN: Final = "eneco_be"

# A config entry is one login, holding the credentials. Every meter that login
# submits for is a subentry of this type, holding everything below.
SUBENTRY_METER: Final = "meter"

CONF_ACCOUNT: Final = "account"
CONF_ADDRESS: Final = "address"
CONF_EAN: Final = "ean"
CONF_ENERGY_TYPE: Final = "energy_type"
# The register keys the meter exposes, discovered when it was set up.
CONF_REGISTERS: Final = "registers"
# Register key -> source entity id, and when to submit.
CONF_SOURCES: Final = "sources"
CONF_SUBMIT_TIME: Final = "submit_time"

ELECTRICITY: Final = "Electricity"
GAS: Final = "Gas"

DEFAULT_SUBMIT_TIME: Final = "23:50:00"

# A first setup is offered a time drawn from this window rather than one fixed
# time, so that the households running this do not all reach Eneco at once.
SUGGEST_SUBMIT_FROM: Final = "22:30:00"
SUGGEST_SUBMIT_TO: Final = "23:50:00"


ATTR_LAST_ERROR: Final = "last_error"
