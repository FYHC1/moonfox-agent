"""Optional-server installation smoke, without frontend assets or a network listener."""

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from moonfox_ai.api import FauxProvider, FauxResponse, Model
from moonfox_app.api import ModelRegistration, Principal, TokenAuth, create_application
from moonfox_app.server.app import create_server


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="moonfox-server-") as directory:
        provider = FauxProvider([FauxResponse.text("offline")])
        hub = create_application(
            home=Path(directory),
            models={
                "demo": ModelRegistration(
                    Model("demo", "faux", "faux"), provider.stream, "Offline test"
                ),
            },
        )
        token = "installation-smoke-token-not-a-real-secret"
        app = create_server(
            hub,
            TokenAuth({token: Principal("owner", True)}),
            allowed_origins=("http://testserver",),
        )
        with TestClient(app) as client:
            response = client.get("/api/v1/protocol", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert client.get("/").status_code == 404
            print(
                f"http={response.status_code}; protocol={response.json()['protocol_version']}; "
                "api_only=True"
            )


if __name__ == "__main__":
    main()
