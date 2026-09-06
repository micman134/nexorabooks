"""Handing a cleared invoice to whoever is going to clear it.

Nigeria's e-invoicing platform is not something a small software house connects
to by reading a blog post. Access is through the Revenue Service's own
onboarding or through an accredited Access Point Provider, and either way it
comes with credentials, a digital certificate and a sandbox that must be tested
against before anybody's real invoices go near it.

None of that is available to write against today. What *is* available — and is
most of the work — is everything up to the wire: producing a correct document,
knowing when one is incomplete, keeping the queue when the connection drops,
recording what came back and printing the result on the invoice.

So this module defines the join. A :class:`Transmitter` takes bytes and returns
a :class:`Clearance`. Two exist:

* :class:`Simulator` — a local rehearsal. It applies the checks a real platform
  would apply, refuses what a real platform would refuse, and issues an IRN that
  is deliberately marked as make-believe. A business can run its whole invoicing
  month through it and find out what would have been rejected, without claiming
  compliance it does not have.
* :class:`HttpTransmitter` — the real one, driven entirely by configuration.
  When the credentials exist it can be pointed at the Revenue Service or at a
  provider without changing this file.

**Nothing here may be presented as compliance until it has been tested against
the real endpoint.** :attr:`Clearance.rehearsal` carries that fact all the way
through to the screen and the printed invoice, because the one unforgivable
outcome is a customer believing they are filing when they are not.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

#: How long to wait on the network before giving up and queueing for a retry.
#: Short on purpose: somebody is standing at a counter with a customer.
TIMEOUT = 20


@dataclass
class Clearance:
    """What came back."""

    ok: bool = False
    irn: str = ""
    csid: str = ""
    qr_payload: str = ""
    #: True when a refusal is about the document's contents. A person has to
    #: change something; sending the same bytes again will fail identically.
    permanent: bool = False
    error: str = ""
    raw: str = ""
    channel: str = ""

    @property
    def rehearsal(self) -> bool:
        return self.channel == "simulator"

    @property
    def retryable(self) -> bool:
        return not self.ok and not self.permanent


class TransmitError(Exception):
    """Something about the arrangement itself is wrong — not the document."""


# --------------------------------------------------------------------------
# The rehearsal
# --------------------------------------------------------------------------

#: Checks a clearance platform will certainly apply. Not the full Nigerian rule
#: set — that is published by the Revenue Service and enforced by them — but
#: enough that a rehearsal catches the mistakes businesses actually make.
def _rehearsal_faults(xml: bytes) -> list[str]:
    text = xml.decode("utf-8", "replace")
    faults: list[str] = []

    def has(tag: str) -> bool:
        return f"<cbc:{tag}>" in text or f":{tag}>" in text

    if not has("ID"):
        faults.append("The document has no invoice number.")
    if not has("IssueDate"):
        faults.append("The document has no issue date.")
    if "PartyLegalEntity" not in text:
        faults.append("One of the parties is not identified as a legal entity.")
    if text.count("NG:TIN") < 2:
        faults.append(
            "Both the supplier and the customer need a Tax Identification "
            "Number. A clearance platform will not accept a document that "
            "cannot say who both parties are."
        )
    if "LegalMonetaryTotal" not in text:
        faults.append("The document carries no totals.")
    if "InvoiceLine" not in text and "CreditNoteLine" not in text:
        faults.append("The document has no lines on it.")

    # Totals must actually add up. A platform checks this and so should a
    # rehearsal, because an invoice that does not balance is the one mistake
    # that will be found by somebody else rather than by you.
    payable = re.search(r"<cbc:PayableAmount[^>]*>(-?[\d.]+)<", text)
    exclusive = re.search(r"<cbc:TaxExclusiveAmount[^>]*>(-?[\d.]+)<", text)
    tax = re.search(r"<cac:TaxTotal>\s*<cbc:TaxAmount[^>]*>(-?[\d.]+)<", text)
    if payable and exclusive and tax:
        from decimal import Decimal

        expected = Decimal(exclusive.group(1)) + Decimal(tax.group(1))
        if Decimal(payable.group(1)) != expected:
            faults.append(
                f"The totals do not add up: {exclusive.group(1)} plus tax of "
                f"{tax.group(1)} is {expected}, but the document says "
                f"{payable.group(1)} is payable."
            )
    return faults


class Simulator:
    """A local stand-in for the clearance platform.

    Deterministic: the same document always produces the same reference, so a
    test can assert on it and a person re-running a batch does not get a
    different answer each time.
    """

    name = "simulator"

    def submit(self, xml: bytes, number: str = "") -> Clearance:
        faults = _rehearsal_faults(xml)
        if faults:
            return Clearance(
                ok=False,
                permanent=True,
                channel=self.name,
                error=" ".join(faults),
                raw=json.dumps({"rejected": faults}, indent=2),
            )

        digest = hashlib.sha256(xml).hexdigest()
        # Marked as a rehearsal in the reference itself, so that a number
        # copied into an email or read out on the phone cannot be mistaken for
        # a real clearance by anybody at either end.
        irn = f"REHEARSAL-{digest[:8].upper()}-{digest[8:16].upper()}"
        csid = hashlib.sha256(b"rehearsal:" + xml).hexdigest()
        payload = json.dumps({
            "irn": irn,
            "csid": csid,
            "number": number,
            "note": "Rehearsal only. This invoice has not been filed.",
        }, separators=(",", ":"))
        return Clearance(
            ok=True, irn=irn, csid=csid, qr_payload=payload, channel=self.name,
            raw=json.dumps({"status": "CLEARED", "irn": irn, "rehearsal": True}, indent=2),
        )


# --------------------------------------------------------------------------
# The real thing
# --------------------------------------------------------------------------


@dataclass
class Endpoint:
    """Everything a real transmitter needs, and nothing hard-coded.

    Kept as plain configuration because the Revenue Service's own onboarding
    and every accredited provider differ in their URLs and in what they call
    the fields. A customer who signs with a provider on Monday should be
    sending on Monday, not waiting for us to publish a release.
    """

    name: str = ""
    submit_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    business_id: str = ""
    #: Where in the response the reference lives, as a dotted path. Providers
    #: disagree: ``data.irn``, ``result.invoiceReferenceNumber``, ``irn``.
    irn_path: str = "data.irn"
    csid_path: str = "data.csid"
    qr_path: str = "data.qr"
    extra_headers: dict = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.submit_url and self.client_id and self.client_secret)


def _dig(data, path: str):
    """Follow a dotted path through decoded JSON, forgivingly."""
    cursor = data
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    return cursor if isinstance(cursor, (str, int, float)) else None


class HttpTransmitter:
    """Sends to a real clearance platform over HTTPS.

    Written against the shape every one of these systems shares — OAuth 2.0
    client credentials for a bearer token, then the document posted as XML —
    but deliberately not against any one provider's field names. Until it has
    been run against a real sandbox it is untested code, and it says so.
    """

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        self.name = endpoint.name or "provider"
        self._token = ""
        self._token_expires = 0.0

    # ---- authentication --------------------------------------------------

    def _bearer(self) -> str:
        if self._token and time.time() < self._token_expires - 30:
            return self._token
        e = self.endpoint
        if not e.token_url:
            # Some providers issue a long-lived key rather than a token flow.
            return e.client_secret

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": e.client_id,
            "client_secret": e.client_secret,
            **({"scope": e.scope} if e.scope else {}),
        }).encode()
        request = urllib.request.Request(
            e.token_url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        token = payload.get("access_token") or ""
        if not token:
            raise TransmitError(
                "The provider accepted the request for a token but did not "
                "return one. Check the client id and secret."
            )
        self._token = token
        self._token_expires = time.time() + float(payload.get("expires_in") or 300)
        return token

    # ---- sending ---------------------------------------------------------

    def submit(self, xml: bytes, number: str = "") -> Clearance:
        e = self.endpoint
        if not e.configured:
            raise TransmitError(
                "No e-invoicing provider is configured. Add the address and "
                "credentials on Settings, E-invoicing."
            )
        try:
            headers = {
                "Content-Type": "application/xml; charset=utf-8",
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer()}",
                **({"X-Business-Id": e.business_id} if e.business_id else {}),
                **dict(e.extra_headers or {}),
            }
            request = urllib.request.Request(e.submit_url, data=xml,
                                             headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read().decode("utf-8", "replace")
                code = response.status
        except urllib.error.HTTPError as bad:
            body = bad.read().decode("utf-8", "replace") if bad.fp else ""
            code = bad.code
        except (urllib.error.URLError, TimeoutError, OSError) as unreachable:
            # The document is fine; the journey was not. Queue it.
            return Clearance(
                ok=False, permanent=False, channel=self.name,
                error=f"Could not reach {self.name}: {unreachable}. "
                      "The invoice is queued and will go automatically.",
            )
        except TransmitError as misconfigured:
            return Clearance(ok=False, permanent=True, channel=self.name,
                             error=str(misconfigured))

        try:
            data = json.loads(body or "{}")
        except ValueError:
            data = {}

        if code >= 500:
            return Clearance(ok=False, permanent=False, channel=self.name, raw=body,
                             error=f"{self.name} returned a server error ({code}). "
                                   "The invoice is queued and will go automatically.")
        if code >= 400:
            return Clearance(
                ok=False, permanent=True, channel=self.name, raw=body,
                error=_message_from(data) or f"Refused by {self.name} ({code}).",
            )

        irn = _dig(data, e.irn_path)
        if not irn:
            return Clearance(
                ok=False, permanent=True, channel=self.name, raw=body,
                error=f"{self.name} accepted the document but returned no "
                      "reference number, so the invoice cannot be issued yet.",
            )
        return Clearance(
            ok=True, channel=self.name, raw=body,
            irn=str(irn),
            csid=str(_dig(data, e.csid_path) or ""),
            qr_payload=str(_dig(data, e.qr_path) or ""),
        )


def _message_from(data) -> str:
    """Pull something a bookkeeper can act on out of an error response."""
    if not isinstance(data, dict):
        return ""
    for key in ("message", "error_description", "error", "detail", "title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    errors = data.get("errors")
    if isinstance(errors, list):
        parts = [str(x.get("message") or x) if isinstance(x, dict) else str(x)
                 for x in errors[:5]]
        return "; ".join(p for p in parts if p)
    return ""
