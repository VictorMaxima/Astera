"""Plain-English narration of a SafetyReport. Never decides risk; only explains it."""
from .config import settings
from .safety import SafetyReport


def _template(report: SafetyReport, amount: str, token: str, network: str) -> str:
    head = {
        "safe": f"This releases {amount} {token.upper()} on {network.upper()}. The automated checks found no problems.",
        "caution": f"This releases {amount} {token.upper()} on {network.upper()}, but some checks need your attention.",
        "danger": f"Do not release {amount} {token.upper()}: the checks found a serious problem.",
    }[report.verdict]
    issues = " ".join(f.message for f in report.findings if f.severity != "info")
    return f"{head} {issues}".strip()


def narrate(report: SafetyReport, amount: str, token: str, network: str) -> str:
    fallback = _template(report, amount, token, network)
    if not settings.anthropic_api_key:
        return fallback
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        msg = client.messages.create(
            model="claude-sonnet-5-5",
            max_tokens=200,
            system=(
                "You explain crypto payment safety checks to non-technical users in 2-3 plain sentences. "
                "Use only the verdict and findings provided. Never change the verdict or invent facts."
            ),
            messages=[{
                "role": "user",
                "content": f"Payment: {amount} {token.upper()} on {network.upper()}\n"
                           f"Verdict: {report.verdict}\nFindings: {report.to_dict()['findings']}",
            }],
        )
        return msg.content[0].text.strip()
    except Exception:
        return fallback
