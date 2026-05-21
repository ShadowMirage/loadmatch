from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

@dataclass
class Button:
    id: str
    title: str

@dataclass
class SectionRow:
    id: str
    title: str
    description: Optional[str] = None

@dataclass
class Section:
    title: str
    rows: List[SectionRow]

@dataclass
class Response:
    text: str
    buttons: List[Button] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    header: Optional[str] = None
    footer: Optional[str] = None
    list_button_text: Optional[str] = "Select Option"
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_list(self) -> bool:
        return len(self.sections) > 0
    
    @property
    def has_buttons(self) -> bool:
        return len(self.buttons) > 0


def coerce_response(payload: "Response | dict") -> Response:
    """
    Rehydrate persisted response payloads back into the Response contract.
    """
    if isinstance(payload, Response):
        return payload

    if not isinstance(payload, dict):
        raise TypeError(f"Unsupported response payload type: {type(payload)!r}")

    buttons = [
        Button(id=button["id"], title=button["title"])
        for button in payload.get("buttons", [])
    ]
    sections = [
        Section(
            title=section["title"],
            rows=[
                SectionRow(
                    id=row["id"],
                    title=row["title"],
                    description=row.get("description"),
                )
                for row in section.get("rows", [])
            ],
        )
        for section in payload.get("sections", [])
    ]

    return Response(
        text=payload.get("text", ""),
        buttons=buttons,
        sections=sections,
        header=payload.get("header"),
        footer=payload.get("footer"),
        list_button_text=payload.get("list_button_text"),
        metadata=payload.get("metadata", {}),
    )
