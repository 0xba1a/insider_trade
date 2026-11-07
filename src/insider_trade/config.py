import os
from pathlib import Path

# SEC requires a descriptive User-Agent with a contact email, e.g. "Jane Doe jane@example.com":
# https://www.sec.gov/os/accessing-edgar-data


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        raise SystemExit(
            "SEC_USER_AGENT must be set to 'Your Name your@email.com' — SEC rejects requests "
            "without a contact email (https://www.sec.gov/os/accessing-edgar-data)."
        )
    return ua


def db_path() -> Path:
    return Path(os.environ.get("INSIDER_DB", "data/insider.db"))


# SEC fair-access limit is 10 requests/second; stay below it.
SEC_MAX_RPS = float(os.environ.get("SEC_MAX_RPS", "8"))
SEC_WORKERS = int(os.environ.get("SEC_WORKERS", "6"))
