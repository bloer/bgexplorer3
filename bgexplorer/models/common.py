import pint
units = pint.UnitRegistry()
pint.set_application_registry(units)

def validate_unit(value, unit, allow_none=True):
    if value is None and not allow_none:
        raise ValidationError("Unit to validate must not be None")
    if not units(unit).check(value):
        raise ValidationError(f"Value {value} does not match unit {unit}")

