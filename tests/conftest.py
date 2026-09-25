"""Global test configuration: never touch real AWS from unit tests."""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("POWERTOOLS_TRACE_DISABLED", "1")
os.environ.setdefault("POWERTOOLS_METRICS_NAMESPACE", "CustomerSupportAgentTest")
os.environ.setdefault("POWERTOOLS_DEV", "false")

if os.environ.get("E2E") != "1":
    # Unit tests run with fake credentials so that an accidental real call fails loudly.
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ.pop("AWS_PROFILE", None)
