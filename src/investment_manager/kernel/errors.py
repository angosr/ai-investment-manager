class PointInTimeInputUnavailable(RuntimeError):
    """A required as-of input is not observable yet and may be retried."""
