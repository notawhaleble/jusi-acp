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
    source: str = "Agent"
    kind: str = "question"
    schema: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    url: str = ""
    withdrawn: bool = False
    omitted: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        index = next((i for i, question in enumerate(self.questions)
                      if question.get("field", str(i)) not in self.answers
                      and question.get("field", str(i)) not in self.omitted), len(self.questions))
        review = index >= len(self.questions)
        question = self.questions[index] if not review else {"question": self.message, "options": []}
        return {
            "status": "withdrawn" if self.withdrawn else "review" if review else "awaiting_answer",
            "source": self.source,
            "kind": self.kind,
            "answers": dict(self.answers),
            "url": self.url,
            "optional": question.get("field") not in (self.schema.get("required") or []) if self.kind == "form" else False,
            "request_id": self.request_id,
            "question_number": index + 1,
            "question_count": len(self.questions),
            "question": str(question.get("question", "")),
            "options": [dict(option) for option in question.get("options", []) if isinstance(option, dict)],
            "multi_select": bool(question.get("multiSelect")),
        }


def question_text(state: dict[str, Any]) -> str:
    lines = [
        f"From: {state.get('source', 'Agent')}",
        ("Review response" if state.get("status") == "review" else
         f"Question {min(state['question_number'], state['question_count'])} of {state['question_count']}"),
        f"{state.get('pending_requests', 0)} other requests waiting",
        "",
        str(state["question"]),
        "",
    ]
    if state.get("status") == "withdrawn":
        return "\n".join(lines + ["This request was withdrawn. Your follow-up will not be sent elsewhere.",
                                  "Press Enter to acknowledge and continue."])
    if state.get("status") == "review":
        import json
        return "\n".join(lines + [json.dumps(state.get("answers", {}), ensure_ascii=False, indent=2),
            "Review your answers. Enter: submit; e: edit fields; x: cancel; D: decline."])
    if state.get("kind") == "url":
        from urllib.parse import urlsplit
        return "\n".join(lines + ["Destination: " + str(urlsplit(state['url']).hostname), state['url'],
            "Enter: open in your browser; x: cancel; D: decline.",
            "Opening the browser does not confirm completion."])
    for option in state["options"]:
        label = str(option.get("label", ""))
        description = str(option.get("description", ""))
        lines.append(f"- {label}" + (f": {description}" if description else ""))
    lines.extend([
        "",
        "Answer in the notebook cell and submit your usual Jusi follow-up.",
        "Write an option label or your own answer. Multiline text is preserved.",
    ])
    if state.get("optional"):
        lines.append("Optional field: press s to omit it.")
    if state["multi_select"]:
        lines.append("You may include several choices in your answer.")
    lines.extend([
        "Each follow-up answers the displayed question. Other requests wait in arrival order.",
        "Submit /cancel to cancel this turn. Closing this view keeps it waiting.",
    ])
    return "\n".join(lines)
