"""Deterministic safety checks. Code decides the verdict; the LLM only narrates it."""
import re
from dataclasses import dataclass, field

import httpx

from .config import settings

# (network, token) pairs Astera supports, from Roqqu's supported-protocols table.
SUPPORTED_ASSETS = {("bep20", "usdt"), ("trc20", "usdt"), ("base", "usdc")}

EVM_NETWORKS = {"bep20", "base", "erc20"}
CHAIN_IDS = {"erc20": 1, "bep20": 56, "base": 8453}
ADDRESS_PATTERNS = {
    "evm": re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "trc20": re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"),
}
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/address_security/{address}"  # verify against GoPlus docs


@dataclass
class Finding:
    code: str
    severity: str  # "info" | "caution" | "danger"
    message: str


@dataclass
class SafetyReport:
    findings: list[Finding] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        severities = {f.severity for f in self.findings}
        if "danger" in severities:
            return "danger"
        if "caution" in severities:
            return "caution"
        return "safe"

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [vars(f) for f in self.findings],
        }


def is_supported(network: str, token: str) -> bool:
    return (network.lower(), token.lower()) in SUPPORTED_ASSETS


def valid_address_format(address: str, network: str) -> bool:
    pattern = ADDRESS_PATTERNS["trc20" if network == "trc20" else "evm"]
    return bool(pattern.match(address))


def _is_contract(address: str, network: str) -> bool | None:
    """True/False if we could tell, None if the lookup failed."""
    rpc = settings.rpc_urls.get(network)
    if not rpc:
        return None
    try:
        r = httpx.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [address, "latest"]},
            timeout=8,
        )
        code = r.json().get("result")
        return code not in (None, "0x", "0x0")
    except Exception:
        return None


def _goplus_flags(address: str, network: str) -> list[str] | None:
    """Names of risk flags GoPlus raises for this address, or None if the lookup failed."""
    try:
        r = httpx.get(
            GOPLUS_URL.format(address=address),
            params={"chain_id": CHAIN_IDS[network]},
            timeout=8,
        )
        result = r.json().get("result") or {}
        return [k for k, v in result.items() if v == "1"]
    except Exception:
        return None


def check_payout(address: str, network: str, token: str, *, deposit_address: str | None = None) -> SafetyReport:
    network, token = network.lower(), token.lower()
    report = SafetyReport()
    add = report.findings.append

    if not is_supported(network, token):
        add(Finding("unsupported_asset", "danger", f"{token.upper()} on {network.upper()} is not supported."))
        return report

    if not valid_address_format(address, network):
        add(Finding("bad_address_format", "danger",
                    f"This is not a valid {network.upper()} address. Sending could lose the funds."))
        return report

    if deposit_address and address.lower() == deposit_address.lower():
        add(Finding("self_send", "danger", "The payout address is the escrow deposit address itself."))

    if not settings.external_checks:
        add(Finding("external_checks_skipped", "info", "External screening was skipped (offline mode)."))
        return report

    if network in EVM_NETWORKS:
        contract = _is_contract(address, network)
        if contract is True:
            add(Finding("is_contract", "caution",
                        "The payout address is a smart contract, not a regular wallet."))
        elif contract is None:
            add(Finding("contract_check_failed", "caution", "Could not check whether the address is a contract."))

        flags = _goplus_flags(address, network)
        if flags:
            add(Finding("flagged_address", "danger",
                        "Address flagged by a scam-screening service: " + ", ".join(flags) + "."))
        elif flags is None:
            add(Finding("screening_unavailable", "caution", "Scam screening was unavailable, so this address is unchecked."))
    else:
        add(Finding("screening_not_implemented", "info", f"Scam screening is not implemented for {network.upper()} yet."))

    if not any(f.severity in ("caution", "danger") for f in report.findings):
        add(Finding("no_issues", "info", "No problems found by the automated checks."))
    return report
