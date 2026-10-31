"""Missing defaults are explicit; stored null never opts someone into discovery."""
from sqlalchemy import String, case, cast, func, literal, or_


def public_flag(values, key, default=False):
    if not isinstance(values, dict):
        return False
    return default if key not in values else values[key] is True


def protected_minor(values):
    if not isinstance(values, dict):
        return True
    return False if 'is_minor' not in values else values['is_minor'] is not False


def sql_flag(db, column, key, missing_default=False, null_default=False):
    if db.bind.dialect.name == 'postgresql':
        root_type, value_type = func.json_typeof(column), func.json_typeof(column[key])
        true_value = cast(column[key], String) == 'true'
        boolean_type = value_type == 'boolean'
    else:
        root_type = func.json_type(column)
        value_type = func.json_type(column, '$.' + key)
        true_value = value_type == 'true'
        boolean_type = value_type.in_(('true', 'false'))
    return case((or_(root_type.is_(None), root_type != 'object'), literal(null_default)),
                (value_type.is_(None), literal(missing_default)),
                (value_type == 'null', literal(null_default)),
                (boolean_type, true_value), else_=literal(null_default))
