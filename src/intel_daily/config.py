import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_PASSWORDS = {
    "admin",
    "password",
    "changeme",
    "secret",
    "12345678",
    "correcthorsebatterystaple",
}


@dataclass(frozen=True)
class AppSettings:
    db_path: Path
    admin_username: str
    admin_password: str

    @classmethod
    def from_env(cls) -> "AppSettings":
        db_path = os.getenv("INTEL_DB_PATH")
        if not db_path:
            raise ValueError("INTEL_DB_PATH must be set")
        admin_username = os.getenv("INTEL_ADMIN_USERNAME")
        if not admin_username:
            raise ValueError("INTEL_ADMIN_USERNAME must be set")
        admin_password = os.getenv("INTEL_ADMIN_PASSWORD")
        if not admin_password:
            raise ValueError("INTEL_ADMIN_PASSWORD must be set")
        if (
            len(admin_password) < 12
            or admin_password.lower() in _DEFAULT_PASSWORDS
            or admin_password == admin_username
        ):
            raise ValueError(
                "INTEL_ADMIN_PASSWORD is too weak; choose a long, unique password"
            )
        return cls(
            db_path=Path(db_path),
            admin_username=admin_username,
            admin_password=admin_password,
        )
