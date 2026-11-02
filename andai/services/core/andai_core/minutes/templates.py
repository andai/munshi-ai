"""The minutes templates (MOM-03): what each one is called, which sections it shows in which order
under which heading per output language, and the guidance its prompt carries.

Every template fills the same SPEC `MomDocument` fields; a template only changes the emphasis the
model is asked for and how the document is laid out. The SPEC template sections map onto those
fields: `agenda` and `discussion` are `topics`; `attendees` are the meeting's attendees (not written
by the model); `next_meeting` has no field and is not shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Language = Literal["en", "hi", "hinglish"]
Section = Literal["summary", "attendees", "topics", "decisions", "action_items", "risks"]

LANGUAGES: tuple[Language, ...] = ("en", "hi", "hinglish")
DEFAULT_TEMPLATE = "standard"

# What an empty section shows ("None recorded" beats an invented item).
NONE_RECORDED: dict[Language, str] = {
    "en": "None recorded",
    "hi": "कुछ दर्ज नहीं हुआ",
    "hinglish": "Kuch record nahi hua",
}
# How an action item without a named owner is shown (its stored owner is "unassigned").
UNASSIGNED_LABEL: dict[Language, str] = {
    "en": "Unassigned",
    "hi": "अनिर्धारित",
    "hinglish": "Unassigned",
}
# The instruction every prompt carries for its output language.
LANGUAGE_INSTRUCTION: dict[Language, str] = {
    "en": "Write every text field in English, translating Hindi or Hinglish speech.",
    "hi": (
        "Write every text field in Hindi in Devanagari script (हिन्दी, देवनागरी), translating "
        "English speech; names, product names and technical terms may stay as commonly written."
    ),
    "hinglish": (
        "Write every text field in Hinglish: Hindi and English mixed the way the speakers mix "
        "them, in Roman (Latin) script only, with no Devanagari."
    ),
}


@dataclass(frozen=True)
class Template:
    id: str
    names: dict[Language, str]
    sections: tuple[Section, ...]
    headings: dict[Language, dict[Section, str]]
    guidance: str

    def name(self, language: Language) -> str:
        return self.names[language]

    def heading(self, section: Section, language: Language) -> str:
        return self.headings[language][section]


TEMPLATES: dict[str, Template] = {
    t.id: t
    for t in (
        Template(
            id="standard",
            names={"en": "Standard", "hi": "मानक", "hinglish": "Standard"},
            sections=("summary", "attendees", "topics", "decisions", "action_items", "risks"),
            headings={
                "en": {
                    "summary": "Summary",
                    "attendees": "Attendees",
                    "topics": "Topics",
                    "decisions": "Decisions",
                    "action_items": "Action items",
                    "risks": "Risks",
                },
                "hi": {
                    "summary": "सारांश",
                    "attendees": "उपस्थित",
                    "topics": "विषय",
                    "decisions": "निर्णय",
                    "action_items": "कार्य बिंदु",
                    "risks": "जोखिम",
                },
                "hinglish": {
                    "summary": "Summary",
                    "attendees": "Attendees",
                    "topics": "Topics",
                    "decisions": "Decisions",
                    "action_items": "Action items",
                    "risks": "Risks",
                },
            },
            guidance=(
                "Standard minutes: a short neutral summary of the whole meeting, the topics in the "
                "order they were discussed, every decision taken, every action item agreed, and "
                "risks or open concerns raised."
            ),
        ),
        Template(
            id="indian_formal_mom",
            names={
                "en": "Indian formal MoM",
                "hi": "औपचारिक कार्यवृत्त",
                "hinglish": "Formal MoM",
            },
            sections=("attendees", "topics", "decisions", "action_items", "risks", "summary"),
            headings={
                "en": {
                    "attendees": "Members present",
                    "topics": "Agenda and points discussed",
                    "decisions": "Decisions taken",
                    "action_items": "Action to be taken (by whom, by when)",
                    "risks": "Issues and concerns",
                    "summary": "Summary",
                },
                "hi": {
                    "attendees": "उपस्थित सदस्य",
                    "topics": "कार्यसूची एवं चर्चा के बिंदु",
                    "decisions": "लिए गए निर्णय",
                    "action_items": "की जाने वाली कार्रवाई (किसके द्वारा, कब तक)",
                    "risks": "मुद्दे एवं चिंताएँ",
                    "summary": "सारांश",
                },
                "hinglish": {
                    "attendees": "Upasthit members",
                    "topics": "Agenda aur charcha ke points",
                    "decisions": "Liye gaye nirnay",
                    "action_items": "Karyavahi (kaun karega, kab tak)",
                    "risks": "Mudde aur chintayein",
                    "summary": "Saaransh",
                },
            },
            guidance=(
                'Formal Indian minutes of meeting: a formal, impersonal register ("It was decided '
                'that ...", "The committee noted ..."); each topic is one agenda point in the '
                "order taken up; decisions are worded as resolutions; each action item says what "
                "is to be done and by whom."
            ),
        ),
        Template(
            id="standup",
            names={"en": "Stand-up", "hi": "स्टैंड-अप", "hinglish": "Stand-up"},
            sections=("summary", "topics", "action_items", "risks", "decisions", "attendees"),
            headings={
                "en": {
                    "summary": "Stand-up summary",
                    "topics": "Updates",
                    "action_items": "Next steps",
                    "risks": "Blockers",
                    "decisions": "Decisions",
                    "attendees": "Attendees",
                },
                "hi": {
                    "summary": "स्टैंड-अप सारांश",
                    "topics": "अपडेट",
                    "action_items": "अगले कदम",
                    "risks": "रुकावटें",
                    "decisions": "निर्णय",
                    "attendees": "उपस्थित",
                },
                "hinglish": {
                    "summary": "Stand-up summary",
                    "topics": "Updates",
                    "action_items": "Agle steps",
                    "risks": "Blockers",
                    "decisions": "Decisions",
                    "attendees": "Attendees",
                },
            },
            guidance=(
                "Daily stand-up: keep it brief. Each topic is one person's update, titled with "
                "their name and what they did or will do; risks are the blockers people raised; "
                "action items are the next steps people committed to."
            ),
        ),
        Template(
            id="client_call",
            names={"en": "Client call", "hi": "क्लाइंट कॉल", "hinglish": "Client call"},
            sections=("summary", "attendees", "topics", "decisions", "action_items", "risks"),
            headings={
                "en": {
                    "summary": "Call summary",
                    "attendees": "Attendees",
                    "topics": "Discussion",
                    "decisions": "Agreements",
                    "action_items": "Follow-ups",
                    "risks": "Client concerns and risks",
                },
                "hi": {
                    "summary": "कॉल सारांश",
                    "attendees": "उपस्थित",
                    "topics": "चर्चा",
                    "decisions": "सहमतियाँ",
                    "action_items": "फ़ॉलो-अप",
                    "risks": "क्लाइंट की चिंताएँ और जोखिम",
                },
                "hinglish": {
                    "summary": "Call summary",
                    "attendees": "Attendees",
                    "topics": "Discussion",
                    "decisions": "Agreements",
                    "action_items": "Follow-ups",
                    "risks": "Client ki concerns aur risks",
                },
            },
            guidance=(
                "Client call: the summary says what the client wanted and where things stand; "
                "decisions are what both sides agreed; action items are the follow-ups promised, "
                "to or by the client; risks are the client's concerns, objections and open "
                "questions."
            ),
        ),
    )
}


def get(template_id: str) -> Template:
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise ValueError(f"unknown minutes template {template_id!r}") from None


__all__ = [
    "DEFAULT_TEMPLATE",
    "LANGUAGES",
    "LANGUAGE_INSTRUCTION",
    "NONE_RECORDED",
    "TEMPLATES",
    "UNASSIGNED_LABEL",
    "Language",
    "Section",
    "Template",
    "get",
]
