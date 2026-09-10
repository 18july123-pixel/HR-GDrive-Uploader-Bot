import types

import drive_service


def test_drive_service_get_about_returns_drive_account_summary(monkeypatch):
    class FakeAboutRequest:
        def __init__(self):
            self.calls = []

        def get(self, fields=None):
            self.calls.append(fields)
            return self

        def execute(self, num_retries=3):
            return {
                "storageQuota": {"limit": "1000000", "usage": "12345"},
                "user": {"emailAddress": "test@example.com"},
            }

    class FakeDrive:
        def about(self):
            return FakeAboutRequest()

    monkeypatch.setattr(drive_service, "get_drive", lambda user_token: FakeDrive())

    about = drive_service.get_about({"refresh_token": "x"})

    assert about["email"] == "test@example.com"
    assert about["usage_bytes"] == 12345
    assert about["limit_bytes"] == 1000000
