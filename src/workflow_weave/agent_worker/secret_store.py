"""A Product's Test secrets: credentials for test accounts and sandboxes of outside services.

One YAML file per Product, kept on the Sandbox host outside every repo, written by a human, read by
runs and never changed per ticket (see `config/test-secrets.example.yaml`):

```yaml
KORONA_API_KEY: kor-test-REPLACE-ME      # secret name: value, flat; names are environment variable names
```

The folder holding the files is the `test_secrets.location` setting in `weave.yaml` (see
`load_configured_secret_store`), next to the Subscription store's; Product `p`'s secrets are the
file `<location>/p.yaml`. The store opens only that Product's file, so a Product has no way to read
another's. The file must be readable by its owner only (mode 0600). A Repo's Run recipe names the
secrets it needs by name; a run is given exactly those (`ProductSecrets.select`) and nothing else.
Values are never printed, recorded or logged: errors name files and secret names only.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

# A secret's name is also the environment variable its service sees.
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRODUCT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REDACTED = "[redacted Test secret]"


class SecretsError(ValueError):
    """A Product's Test secrets cannot be used. The message names the file and what to fix, never a value."""


def redact(text: str, values: Iterable[str]) -> str:
    """`text` with every secret value replaced, longest first so one value inside another is whole."""
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


@dataclass(frozen=True)
class ProductSecrets:
    """One Product's secrets, as read from its file."""

    product: str
    path: Path
    _values: dict[str, str] = field(repr=False)  # never printed

    def missing(self, names: Iterable[str]) -> list[str]:
        """The names this Product does not have, in the order given."""
        return [n for n in names if n not in self._values]

    def select(self, names: Iterable[str]) -> dict[str, str]:
        """Only the named secrets. A name the Product lacks is a KeyError: check `missing` first."""
        return {n: self._values[n] for n in names}


class SecretStore:
    """The folder of per-Product Test secrets files on the Sandbox host."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def file_for(self, product: str) -> Path:
        if not _PRODUCT_NAME.fullmatch(product or "") or ".." in product:
            raise SecretsError(f"Product name {product!r} cannot name a Test secrets file")
        return self.directory / f"{product}.yaml"

    def for_product(self, product: str) -> ProductSecrets:
        """Read the Product's file (and only that file); SecretsError if it is absent or unusable."""
        path = self.file_for(product)
        try:
            info = path.stat()
        except FileNotFoundError:
            raise SecretsError(
                f"Product {product!r} has no Test secrets file: create {path} (mode 0600, "
                f"`NAME: value` lines; see config/test-secrets.example.yaml)"
            ) from None
        except OSError as e:
            raise SecretsError(f"Product {product!r}: its Test secrets file {path} cannot be read ({e.strerror})") from None
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SecretsError(
                f"Product {product!r}: its Test secrets file {path} has mode {stat.S_IMODE(info.st_mode):o} "
                f"but must be readable by its owner only: run `chmod 600 {path}`"
            )
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            # Not the parser's message: it can quote a line of the file.
            raise SecretsError(f"Product {product!r}: its Test secrets file {path} is not valid YAML") from None
        except OSError as e:
            raise SecretsError(f"Product {product!r}: its Test secrets file {path} cannot be read ({e.strerror})") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise SecretsError(f"Product {product!r}: {path} must be a mapping of secret name to value")
        values: dict[str, str] = {}
        for name, value in data.items():
            if not isinstance(name, str) or not SECRET_NAME.fullmatch(name):
                raise SecretsError(
                    f"Product {product!r}: {path} has a secret named {str(name)!r}, which is not a valid "
                    f"environment variable name"
                )
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise SecretsError(f"Product {product!r}: {path}: the value of secret {name} must be a string")
            values[name] = str(value)
        return ProductSecrets(product, path, values)


def load_configured_secret_store(config_path: str | Path) -> SecretStore:
    """The store at `test_secrets.location` in `weave.yaml`; a relative location is resolved from
    the config file's folder."""
    config_path = Path(config_path)
    config = yaml.safe_load(config_path.read_text()) or {}
    location = (config.get("test_secrets") or {}).get("location")
    if not location:
        raise ValueError(f"{config_path}: `test_secrets.location` is not set")
    return SecretStore((config_path.parent / Path(location).expanduser()).resolve())
