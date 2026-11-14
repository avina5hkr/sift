"""The delete flow's errors, kept apart so plain-words can name them without import cycles."""


class RunBlocked(Exception):
    """The whole run cannot go on (no phone, another phone, a missing destination)."""


class ManifestBlocked(Exception):
    """A required destination is missing, or the manifest could not be written."""


class ExecuteRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RestoreRefused(Exception):
    pass
