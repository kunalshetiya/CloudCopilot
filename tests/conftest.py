"""Test-wide safety net: force obviously-fake AWS credentials for every test.

A test that forgets ``@mock_aws`` (or a Stubber) should fail loudly and safely
— AWS rejecting a bogus signature — never silently succeed against whatever
real AWS credentials happen to be ambiently configured on the machine running
the tests. This is moto's own recommended safety pattern, added after exactly
this mistake actually happened once during development.
"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
