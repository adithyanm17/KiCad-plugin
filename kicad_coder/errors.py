"""Exception hierarchy for kicad_coder.

Every error raised by the library derives from :class:`KiCadCoderError`, so an
LLM driver loop can catch one type and feed ``str(exc)`` back to the model.
"""


class KiCadCoderError(Exception):
    """Base class for every error raised by this library."""


class ValidationError(KiCadCoderError):
    """The design is structurally invalid and cannot be built."""


class LibraryError(KiCadCoderError):
    """A footprint library could not be found, read, or parsed."""


class BackendError(KiCadCoderError):
    """A board backend failed to emit or load a board."""


class ToolchainError(KiCadCoderError):
    """The KiCad toolchain is missing or a kicad-cli invocation failed."""


class ToolError(KiCadCoderError):
    """An LLM tool call was malformed or failed.

    Carries a machine-readable ``code`` so a driver loop can distinguish a
    recoverable mistake (bad argument) from a hard failure.
    """

    def __init__(self, message: str, code: str = "tool_error") -> None:
        super().__init__(message)
        self.code = code
