from decimal import Decimal


def canonical_decimal(value: Decimal) -> Decimal:
    """Strip trailing zeros without switching to exponent notation (1E+2 -> 100)."""
    normalized = value.normalize()
    if normalized.as_tuple().exponent > 0:  # type: ignore[operator]
        normalized = normalized.quantize(Decimal(1))
    return normalized


def decimal_to_plain_string(value: Decimal) -> str:
    """Fixed-point string, never scientific notation (Decimal('1E-7') -> '0.0000001')."""
    return format(value, "f")
