"""Expected migration exception types."""


class MigrationError(RuntimeError):
    """Base class for expected migration failures."""


class ConfigurationError(MigrationError):
    """Raised for invalid or missing configuration."""


class ValidationError(MigrationError):
    """Raised when preflight or payload validation fails."""


class ImageProcessingError(MigrationError):
    """Raised when an image invariant cannot be satisfied."""


class WebflowAPIError(MigrationError):
    """Raised for translated Webflow API failures."""
