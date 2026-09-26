"""Deployment config must never reach the tests.

CI exports the pipeline variable group into the agent environment, so APPROVERS silently
overrode every policy fixture (app/policy.py lets it beat the file, which is what the
deployed app wants). Popped at import time, before the test modules import app.policy and
build the module-level POLICY. APPROVAL_SECRET is padlocked, so it is never exported; every
test that needs it sets it with monkeypatch.
"""

import os

os.environ.pop("APPROVERS", None)
