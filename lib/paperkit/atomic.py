"""Writing a build's outputs so a concurrent reader (a PDF viewer, `build-paper watch`, another build) never sees
half a file."""

import os
import tempfile
from pathlib import Path


def write_atomic(path: Path, data: bytes | str) -> None:
    """Writes `path` through a temporary file beside it, then renames it into place."""
    if isinstance(data, str):
        data = data.encode()
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temp:
        try:
            temp.write(data)
            temp.flush()
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(
                temp.name, 0o666 & ~umask
            )  # a temporary file is private; the output isn't
            os.replace(temp.name, path)
        except BaseException:
            os.unlink(temp.name)
            raise
