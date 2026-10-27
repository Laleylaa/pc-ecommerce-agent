"""Export reviewed Podman settings as NUL-delimited pairs; never shell code."""

import sys

from pydantic import ValidationError

from app.core.config import Settings

PODMAN_FIELDS = (
    "podman_network", "postgres_container", "postgres_volume", "postgres_image",
    "postgres_port", "postgres_db", "postgres_user", "postgres_password",
)


def podman_values(settings: Settings) -> dict[str, str]:
    values = {}
    for field in PODMAN_FIELDS:
        value = getattr(settings, field)
        if field == "postgres_password":
            value = value.get_secret_value()
        elif isinstance(value, bool):
            value = "true" if value else "false"
        values[field.upper()] = str(value)
    return values


def main() -> None:
    try:
        values = podman_values(Settings())
    except ValidationError as exc:
        fields = sorted({".".join(map(str, error["loc"])) for error in exc.errors()})
        raise SystemExit("Invalid configuration fields: " + ", ".join(fields)) from None
    if any("\0" in value for value in values.values()):
        raise SystemExit("Configuration contains unsupported NUL characters.")
    for key, value in values.items():
        sys.stdout.buffer.write(f"{key}\0{value}\0".encode())
    sys.stdout.buffer.write(b"LOCAL_CONFIG_LOADED\0true\0")


if __name__ == "__main__":
    main()
