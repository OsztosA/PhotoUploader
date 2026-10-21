"""OAuth2 (installed-app flow) for Google Photos Library API."""
import os
import threading

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]


class Auth:
    def __init__(self, client_secret_path, token_path):
        self.client_secret_path = client_secret_path
        self.token_path = token_path
        self._creds = None
        self._lock = threading.Lock()

    def has_client_secret(self):
        return os.path.isfile(self.client_secret_path)

    def is_signed_in(self):
        return os.path.isfile(self.token_path)

    def sign_in(self):
        """Opens the browser for consent; blocks until done."""
        flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_path, SCOPES)
        self._creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
        self._save()

    def sign_out(self):
        self._creds = None
        if os.path.isfile(self.token_path):
            os.remove(self.token_path)

    def _save(self):
        with open(self.token_path, "w", encoding="utf-8") as f:
            f.write(self._creds.to_json())

    def invalidate(self):
        with self._lock:
            if self._creds is not None:
                self._creds.token = None

    def token(self):
        """Valid access token; refreshes automatically. Thread-safe."""
        with self._lock:
            if self._creds is None:
                self._creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)
            if not self._creds.valid:
                self._creds.refresh(Request())
                self._save()
            return self._creds.token
