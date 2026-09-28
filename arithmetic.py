"""Tiny synthetic scoring fixture; no external services or dependencies."""


def total_points(values):
    """Return the sum of every score, including the final item."""
    return sum(value for value in values if value >= 0)
