"""Tiny synthetic scoring fixture; no external services or dependencies."""


def total_points(values):
    """Return the sum of every score, including the final item."""
    return sum(values) if values else None
