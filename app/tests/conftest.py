"""Deployment config must never reach the tests.

CI exports the pipeline variable group into the agent environment, so APPROVERS silently
overrode every policy fixture (app/policy.py lets it beat the file, which is what the
deployed app wants), ENTRA_TENANT_ID/ENTRA_AUDIENCE would silently turn on JWT verification
that the auth tests need off by default to exercise the unconfigured path, and
NONCE_TABLE_ENDPOINT/NONCE_TABLE_NAME would point the ledger at a real table and make
app.nonces reach for a credential mid-suite, and FAULT_INJECTION would turn the latency header on
for every test that expects it off. Popped at import time, before the test modules
import app.policy and build the module-level POLICY. APPROVAL_SECRET is padlocked, so it is
never exported; every test that needs it sets it with monkeypatch.
"""

import os

for _var in (
    "APPROVERS",
    "ENTRA_TENANT_ID",
    "ENTRA_AUDIENCE",
    "NONCE_TABLE_ENDPOINT",
    "NONCE_TABLE_NAME",
    "FAULT_INJECTION",
):
    os.environ.pop(_var, None)
