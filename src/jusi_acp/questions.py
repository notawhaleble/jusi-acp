"""Question state shared by follow-up dispatch and read-only presentation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuestionRequest:
    request_id: str
    questions: list[dict[str, Any]]
    option_id: str
    answers: dict[str, Any] = field(default_factory=dict)
    kind: str = "question"
    source: str = "Agent"
    tool_call_id: str = ""
    schema: dict[str, Any] = field(default_factory=dict)
    pending: Any = None
    elicitation_id: str = ""
    content: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        index = len(self.answers)
        question = self.questions[index]
        return {
            "status": "awaiting_answer",
            "kind": self.kind,
            "source": self.source,
            "tool_call_id": self.tool_call_id,
            "field": question.get("field", ""),
            "schema": question.get("schema", {}),
            "structured": question.get("structured", False),
            "required": question.get("required", True),
            "confirm": question.get("confirm", False),
            "request_id": self.request_id,
            "question_number": index + 1,
            "question_count": len(self.questions),
            "question": str(question.get("question", "")),
            "options": [dict(option) for option in question.get("options", []) if isinstance(option, dict)],
            "multi_select": bool(question.get("multiSelect")),
        }


def question_text(state: dict[str, Any]) -> str:
    lines = [
        f"Question {state['question_number']} of {state['question_count']}",
        f"From: {state.get('source', 'Agent')}",
        "",
        str(state["question"]),
        "",
    ]
    for option in state["options"]:
        label = str(option.get("label", ""))
        description = str(option.get("description", ""))
        lines.append(f"- {label}" + (f": {description}" if description else ""))
    lines.extend([
        "",
        "Answer in the notebook cell and submit your usual Jusi follow-up.",
        "Write an option label or your own answer. Multiline text is preserved.",
    ])
    if state["multi_select"]:
        lines.append("You may include several choices in your answer.")
    if state.get("kind") == "form":
        lines.append("Optional fields: /skip. Use /default for a supplied default; /empty for an empty string.")
        lines.append("Use /decline to decline this request, or /dismiss to cancel it.")
    lines.extend([
        "Each follow-up answers one question; the agent resumes after the last.",
        "Submit /cancel to cancel this turn. Closing this view keeps it waiting.",
    ])
    return "\n".join(lines)


def form_questions(message: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    from jsonschema import Draft202012Validator, SchemaError
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"Invalid elicitation schema: {exc.message}") from exc
    if not set(schema.get('required', [])) <= set(schema.get('properties', {})):
        raise ValueError('Required elicitation fields must appear in properties')
    questions = []
    for name, prop in schema.get('properties', {}).items():
        if prop.get('type') not in {'string', 'number', 'integer', 'boolean', 'array'}:
            raise ValueError(f'Unsupported elicitation field type for {name}')
        values = prop.get('enum', [])
        options = [{'label': str(value)} for value in values]
        options += [{'label': item['const'], 'description': item.get('title', '')}
                    for item in prop.get('oneOf', []) if 'const' in item]
        if prop.get('type') == 'boolean':
            options = [{'label': 'true'}, {'label': 'false'}]
        questions.append({'question': '\n\n'.join(filter(None, [message, prop.get('title', name), prop.get('description', '')])),
                          'options': options, 'field': name, 'schema': prop,
                          'required': name in schema.get('required', []),
                          'structured': prop.get('type') != 'string' or bool(options)})
    return questions or [{'question': message, 'options': [], 'confirm': True}]


def parse_form_answer(question: dict[str, Any], body: str) -> Any:
    import json
    import math
    from jsonschema import Draft202012Validator, FormatChecker
    schema = question['schema']
    if schema['type'] == 'string':
        value: Any = body
    else:
        try:
            value = json.loads(body)
        except ValueError as exc:
            raise ValueError('Enter a JSON value (true/false, a number, or an array of strings)') from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Enter a finite number')
    error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value), None)
    if error is not None:
        raise ValueError(error.message)
    return value
