"""Shared generator constants. Master data (capacity sizing) and Task 7 (PO lines) both import
these, so "expected monthly demand" is computed from the same quantities that are generated."""

from decimal import Decimal

from supplier_reliability.domain.enums import SupplierCategory

MEAN_LINES_PER_PO = 2.6
"""Mean number of lines per purchase order (PO line counts are drawn with this mean)."""

MEAN_QTY_PER_LINE = Decimal("400")
"""Mean ordered quantity per PO line, in the line's unit of measure."""

QTY_LOGNORMAL_SIGMA = 0.5
"""Sigma of the lognormal used to draw line quantities; the draw is scaled so its mean is
``MEAN_QTY_PER_LINE`` (mu = ln(mean) - sigma**2 / 2)."""

# category -> ((product_category, unit_of_measure, base unit price in USD), ...)
PRODUCT_CATEGORIES: dict[SupplierCategory, tuple[tuple[str, str, float], ...]] = {
    SupplierCategory.RAW_MATERIALS: (
        ("STEEL_COIL", "KG", 1.8),
        ("ALUMINIUM_INGOT", "KG", 2.9),
        ("COPPER_WIRE", "KG", 9.5),
    ),
    SupplierCategory.ELECTRONICS: (
        ("PCB_ASSEMBLY", "EA", 14.0),
        ("SENSOR_MODULE", "EA", 22.5),
        ("POWER_SUPPLY", "EA", 31.0),
    ),
    SupplierCategory.PACKAGING: (
        ("CORRUGATED_BOX", "EA", 0.85),
        ("PALLET_WRAP", "ROLL", 6.4),
        ("FOAM_INSERT", "EA", 1.2),
    ),
    SupplierCategory.CHEMICALS: (
        ("SOLVENT", "L", 3.1),
        ("ADHESIVE", "KG", 7.8),
        ("COATING", "L", 11.5),
    ),
    SupplierCategory.MACHINED_PARTS: (
        ("SHAFT", "EA", 18.0),
        ("HOUSING", "EA", 42.0),
        ("FASTENER_KIT", "BOX", 5.5),
    ),
    SupplierCategory.LOGISTICS_SERVICES: (
        ("PALLET_FREIGHT", "PALLET", 48.0),
        ("CONTAINER_HANDLING", "EA", 95.0),
        ("LAST_MILE", "EA", 12.0),
    ),
}

CURRENCY_FACTOR: dict[str, float] = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "INR": 83.0}
"""Multiplier applied to the USD base price to get the contract-currency price (synthetic)."""
