"""Question state shared by follow-up dispatch and read-only presentation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QuestionRequest:
    request_id: str
    questions: list[dict[str, Any]]
    option_id: str
    answers: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        index = len(self.answers)
        question = self.questions[index]
        return {
            "status": "awaiting_answer",
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
    lines.extend([
        "Each follow-up answers one question; the agent resumes after the last.",
        "Submit /cancel to cancel this turn. Closing this view keeps it waiting.",
    ])
    return "\n".join(lines)
