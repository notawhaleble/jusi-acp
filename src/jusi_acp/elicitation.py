"""Restricted form presentation and validation, without interpreting answer prose."""
from __future__ import annotations
import json
from typing import Any


def form_questions(schema: dict[str, Any], message: str) -> list[dict[str, Any]]:
    from acp import RequestError
    from jsonschema import Draft202012Validator
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise RequestError.invalid_params({'details': 'Invalid elicitation schema'}) from exc
    properties = schema.get('properties') or {}
    if schema.get('type', 'object') != 'object' or not isinstance(properties, dict):
        raise RequestError.invalid_params({'details': 'Expected a flat object schema'})
    questions = []
    for name, spec in properties.items():
        if spec.get('type') not in {'string', 'number', 'integer', 'boolean', 'array'}:
            raise RequestError.invalid_params({'details': f'Unsupported field type for {name}'})
        if spec.get('type') == 'array' and (spec.get('items') or {}).get('type') != 'string':
            raise RequestError.invalid_params({'details': 'Only string selection arrays are supported'})
        choices = spec.get('enum') or [item['const'] for item in spec.get('oneOf', []) if 'const' in item]
        questions.append({'field': name, 'question': '\n\n'.join(filter(None, [message,
            spec.get('title', name), spec.get('description', ''),
            f"Value type: {spec.get('type')}." + (' Use JSON for non-text values.' if spec.get('type') != 'string' else '')])),
            'options': [{'label': str(choice), 'description': ''} for choice in choices]})
    return questions


def parse_answer(body: str, spec: dict[str, Any]) -> Any:
    from jsonschema import Draft202012Validator, FormatChecker
    if spec.get('type') == 'string':
        value: Any = body
    else:
        try:
            value = json.loads(body)
        except ValueError as exc:
            raise ValueError('Enter a valid JSON value for this field; the answer was not sent') from exc
    import math
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Enter a finite number')
    errors = list(Draft202012Validator(spec, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        raise ValueError(errors[0].message)
    return value


def validate_form(schema: dict[str, Any], answers: dict[str, Any]) -> None:
    from jsonschema import Draft202012Validator, FormatChecker
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(answers))
    if errors:
        raise ValueError(errors[0].message)
