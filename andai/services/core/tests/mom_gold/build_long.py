"""The six long meetings of the minutes gold set, generated from a seed (MOM-02, D-45).

Each long meeting is composed of scripted scenes drawn from a per-language phrase bank — status
updates, questions and answers, disagreements, side topics and suggestions nobody takes up — with
its labelled facts (decisions and action items, written by hand below) inserted at fixed minutes,
and one needle fact said once at minute 45. Every filler line is built from a template with its own
topic, person and numbers, and the build refuses a repeated line, a filler line that matches a
label, or a needle that is said twice. The same seed always gives the same bytes.

    uv run --directory andai python services/core/tests/mom_gold/build_long.py           # write
    uv run --directory andai python services/core/tests/mom_gold/build_long.py --check   # compare
    uv run --directory andai python services/core/tests/mom_gold/build_long.py --count   # totals

Names, companies and numbers are invented.
"""

# ruff: noqa: E501 - the scripted lines and labels read best one per line

from __future__ import annotations

import argparse
import json
import random
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from andai_core.minutes.eval import count, dumps, load, load_all, matches, validate

ROOT = Path(__file__).resolve().parent


# ─── phrase banks ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Bank:
    topics: tuple[str, ...]
    sides: tuple[str, ...]
    opener: tuple[str, ...]
    status: tuple[str, ...]
    question: tuple[str, ...]
    answer: tuple[str, ...]
    disagree: tuple[str, ...]
    reply: tuple[str, ...]
    suggest: tuple[str, ...]
    decline: tuple[str, ...]
    side: tuple[str, ...]
    ack: tuple[str, ...]
    close: str


BANKS: dict[str, Bank] = {
    "en": Bank(
        topics=(
            "the billing dashboard", "the onboarding flow", "the regional sales numbers",
            "the support queue", "the hiring pipeline", "the customer survey", "the data import",
            "the partner portal", "the invoice backlog", "the training plan", "the mobile login",
            "the search ranking", "the marketing calendar", "the returns process",
            "the quality checks", "the call centre script", "the price comparison sheet",
            "the delivery tracking page", "the internal wiki", "the test environment",
            "the loyalty programme", "the refund queue", "the analytics export",
            "the contract templates",
        ),
        sides=(
            "cafeteria menu", "parking lot", "lift on the third floor", "office printer",
            "coffee machine", "meeting room booking tool", "badge reader", "air conditioning",
        ),
        opener=(
            "Let's move to {topic}. {name}, can you start?",
            "Next on my list is {topic}, where are we?",
            "Okay, {topic} now. Give us the short version, {name}.",
            "Can we spend a few minutes on {topic}?",
            "I want a quick check on {topic} before we go further.",
        ),
        status=(
            "On {topic} we are about {n} percent done, nothing blocking right now.",
            "{topic} closed {n} items this week and {m} are still open.",
            "The numbers for {topic} came in at {n}, up from {m} last time.",
            "I looked at {topic} again, it has been stable for {n} days.",
            "For {topic}, {n} people tested it and {m} reported small issues.",
            "{topic} is slower than planned, we lost about {n} hours to rework.",
            "We cleaned up {n} old entries in {topic}, {m} more to go.",
            "Most of {topic} is done, only {n} small fixes remain.",
            "I compared {topic} with last quarter, it is {n} points better.",
        ),
        question=(
            "{name}, is {topic} on track or should we worry?",
            "Do we know why {topic} dropped by {n} points?",
            "{name}, how many of the {n} open items on {topic} are serious?",
            "What is still missing on {topic}, {name}?",
            "Is anyone waiting on {name} for {topic}?",
        ),
        answer=(
            "Mostly it is the data, {n} rows are bad.",
            "Only {n} of them matter, the rest are cosmetic.",
            "I think {n} of the issues come from the same root cause.",
            "Honestly it is fine, the {n} percent dip was a reporting glitch.",
            "We need about {n} more hours on it, nothing dramatic.",
            "It depends on the review, roughly {n} comments are still open.",
        ),
        disagree=(
            "I am not sure {topic} is that simple, the last round took {n} days.",
            "I see it differently, {topic} needs at least {n} more test runs.",
            "That {n} percent figure for {topic} looks too optimistic to me.",
            "I would not call {topic} done while {n} complaints are open.",
        ),
        reply=(
            "Fair point, let's watch {topic} for another {n} days and see.",
            "Maybe, but {m} of those {n} complaints are duplicates.",
            "Okay, I will bring the detailed {topic} numbers next time, about {n} pages.",
            "We can disagree on that one, the trend on {topic} is still up {n} points.",
        ),
        suggest=(
            "Maybe we could hire a contractor for {topic} for {n} weeks?",
            "What if we paused {topic} for {n} days and reset it?",
            "Should we buy a tool for {topic}? I saw one for {n} thousand.",
            "Could we merge {topic} into the bigger project with {n} people?",
        ),
        decline=(
            "Not now, let's revisit {topic} after the next {n} weeks.",
            "I would rather not spend {n} thousand on {topic} this quarter.",
            "No, {topic} is fine as it is, let's not change it.",
            "Let's park that idea about {topic}, too many moving parts.",
        ),
        side=(
            "Off topic, but the {side} has been broken for {n} days now.",
            "By the way, did anyone else notice the {side} changed again?",
            "Sorry, quick aside, the {side} survey closes after {n} responses.",
            "Unrelated, the {side} complaint count is at {n}, someone should tell admin.",
        ),
        ack=(
            "Okay, noted on {topic}.",
            "Thanks {name}, that helps on {topic}.",
            "Got it, {topic} looks fine for now.",
            "Good, nothing more on {topic} from me.",
            "Right, moving on from {topic}.",
        ),
        close="Okay, that is all for today. Thanks everyone for staying the whole time.",
    ),
    "hi": Bank(
        topics=(
            "बिलिंग डैशबोर्ड", "ऑनबोर्डिंग प्रक्रिया", "क्षेत्रीय बिक्री के आंकड़े", "सपोर्ट कतार",
            "भर्ती प्रक्रिया", "ग्राहक सर्वे", "डेटा इम्पोर्ट", "पार्टनर पोर्टल", "प्रशिक्षण योजना",
            "मोबाइल लॉगिन", "मार्केटिंग कैलेंडर", "रिटर्न प्रक्रिया", "क्वालिटी जाँच",
            "कॉल सेंटर स्क्रिप्ट", "डिलीवरी ट्रैकिंग पेज", "आंतरिक विकी", "टेस्ट सर्वर",
            "लॉयल्टी योजना", "रिफंड कतार", "एनालिटिक्स रिपोर्ट", "कॉन्ट्रैक्ट टेम्पलेट",
            "स्टॉक गिनती", "फ़ील्ड विज़िट योजना", "ग्राहक शिकायत रजिस्टर",
        ),
        sides=(
            "कैंटीन का मेन्यू", "पार्किंग", "तीसरी मंज़िल की लिफ़्ट", "ऑफ़िस का प्रिंटर",
            "कॉफ़ी मशीन", "मीटिंग रूम बुकिंग", "बैज रीडर", "एसी",
        ),
        opener=(
            "अब {topic} पर आते हैं। {name}, आप शुरू करेंगे?",
            "मेरी सूची में अगला है {topic}, वहाँ क्या स्थिति है?",
            "ठीक है, अब {topic}। {name}, छोटे में बताइए।",
            "क्या हम कुछ मिनट {topic} पर बात कर सकते हैं?",
            "आगे बढ़ने से पहले {topic} पर एक नज़र डाल लेते हैं।",
        ),
        status=(
            "{topic} में लगभग {n} प्रतिशत काम हो गया है, अभी कोई रुकावट नहीं है।",
            "इस हफ्ते {topic} में {n} काम पूरे हुए और {m} अभी बाकी हैं।",
            "{topic} के आंकड़े {n} आए हैं, पिछली बार {m} थे।",
            "मैंने {topic} फिर से देखा, {n} दिन से स्थिर चल रहा है।",
            "{topic} को {n} लोगों ने जाँचा, {m} ने छोटी दिक्कतें बताईं।",
            "{topic} योजना से धीमा है, दोबारा काम में करीब {n} घंटे गए।",
            "{topic} में {n} पुरानी एंट्री साफ़ कीं, {m} और बची हैं।",
            "{topic} का ज़्यादातर काम हो गया, बस {n} छोटे सुधार बाकी हैं।",
            "पिछली तिमाही से तुलना करें तो {topic} {n} अंक बेहतर है।",
        ),
        question=(
            "{name}, {topic} ठीक चल रहा है या चिंता की बात है?",
            "{topic} में {n} अंक की गिरावट क्यों आई, किसी को पता है?",
            "{name}, {topic} के {n} खुले मामलों में से कितने गंभीर हैं?",
            "{name}, {topic} में अभी क्या कमी है?",
            "क्या {topic} के लिए कोई {name} का इंतज़ार कर रहा है?",
        ),
        answer=(
            "ज़्यादातर डेटा की दिक्कत है, {n} पंक्तियाँ गलत हैं।",
            "उनमें से सिर्फ़ {n} ज़रूरी हैं, बाकी दिखावटी हैं।",
            "मुझे लगता है {n} दिक्कतों की जड़ एक ही है।",
            "सच कहूँ तो ठीक है, {n} प्रतिशत की गिरावट रिपोर्टिंग की गड़बड़ी थी।",
            "करीब {n} घंटे और लगेंगे, कोई बड़ी बात नहीं।",
            "समीक्षा पर निर्भर है, लगभग {n} टिप्पणियाँ अभी खुली हैं।",
        ),
        disagree=(
            "मुझे नहीं लगता {topic} इतना आसान है, पिछली बार {n} दिन लगे थे।",
            "मेरी राय अलग है, {topic} को कम से कम {n} बार और जाँचना होगा।",
            "{topic} के लिए {n} प्रतिशत का आंकड़ा मुझे ज़्यादा ही अच्छा लग रहा है।",
            "जब तक {n} शिकायतें खुली हैं, मैं {topic} को पूरा नहीं कहूँगा।",
        ),
        reply=(
            "ठीक बात है, {topic} को {n} दिन और देखते हैं।",
            "हो सकता है, पर उन {n} शिकायतों में {m} दोहराई गई हैं।",
            "ठीक है, अगली बार {topic} के पूरे आंकड़े लाऊँगा, करीब {n} पन्ने।",
            "इस पर हम असहमत रह सकते हैं, {topic} का रुझान अब भी {n} अंक ऊपर है।",
        ),
        suggest=(
            "क्या {topic} के लिए {n} हफ्ते का कोई ठेकेदार रख लें?",
            "क्या {topic} को {n} दिन रोक कर फिर से शुरू करें?",
            "{topic} के लिए कोई टूल खरीद लें? मैंने एक {n} हज़ार का देखा था।",
            "क्या {topic} को {n} लोगों वाले बड़े प्रोजेक्ट में मिला दें?",
        ),
        decline=(
            "अभी नहीं, {topic} पर {n} हफ्ते बाद फिर सोचेंगे।",
            "इस तिमाही {topic} पर {n} हज़ार खर्च नहीं करना चाहिए।",
            "नहीं, {topic} जैसा है ठीक है, उसे मत बदलिए।",
            "{topic} वाला यह विचार अभी छोड़ देते हैं, बहुत झंझट है।",
        ),
        side=(
            "विषय से हटकर, {side} {n} दिन से खराब है।",
            "वैसे, किसी ने देखा कि {side} फिर बदल गया?",
            "माफ़ कीजिए, एक बात, {side} का सर्वे {n} जवाबों के बाद बंद होगा।",
            "अलग बात है, {side} की {n} शिकायतें हो गई हैं, एडमिन को बताना चाहिए।",
        ),
        ack=(
            "ठीक है, {topic} नोट कर लिया।",
            "धन्यवाद {name}, {topic} पर यह काम का है।",
            "समझ गया, {topic} अभी ठीक लग रहा है।",
            "अच्छा, {topic} पर मेरी तरफ़ से और कुछ नहीं।",
            "ठीक है, {topic} से आगे बढ़ते हैं।",
        ),
        close="ठीक है, आज के लिए इतना ही। पूरे समय रुकने के लिए सबका धन्यवाद।",
    ),
    "hinglish": Bank(
        topics=(
            "billing dashboard", "onboarding flow", "regional sales ke numbers", "support queue",
            "hiring pipeline", "customer survey", "data import", "partner portal",
            "training plan", "mobile login", "marketing calendar", "returns process",
            "quality checks", "call centre script", "delivery tracking page", "internal wiki",
            "test server", "loyalty programme", "refund queue", "analytics report",
            "contract templates", "stock counting", "field visit plan", "complaint register",
        ),
        sides=(
            "canteen ka menu", "parking", "teesri floor ki lift", "office ka printer",
            "coffee machine", "meeting room booking", "badge reader", "AC",
        ),
        opener=(
            "Chalo ab {topic} pe aate hain. {name}, aap start karoge?",
            "Meri list mein next {topic} hai, wahan kya scene hai?",
            "Theek hai, ab {topic}. {name}, short mein batao.",
            "Kya hum kuch minute {topic} pe baat kar sakte hain?",
            "Aage badhne se pehle {topic} pe ek nazar daal lete hain.",
        ),
        status=(
            "{topic} mein lagbhag {n} percent kaam ho gaya hai, abhi koi blocker nahi.",
            "Is hafte {topic} mein {n} items close hue aur {m} abhi open hain.",
            "{topic} ke numbers {n} aaye hain, pichli baar {m} the.",
            "Maine {topic} phir se dekha, {n} din se stable chal raha hai.",
            "{topic} ko {n} logon ne test kiya, {m} ne chhote issues bataye.",
            "{topic} plan se slow hai, rework mein karib {n} ghante gaye.",
            "{topic} mein {n} purani entries saaf ki, {m} aur bachi hain.",
            "{topic} ka zyada kaam ho gaya, bas {n} chhote fixes baaki hain.",
            "Last quarter se compare karo toh {topic} {n} points better hai.",
        ),
        question=(
            "{name}, {topic} track pe hai ya tension wali baat hai?",
            "{topic} mein {n} points ka drop kyun aaya, kisi ko pata hai?",
            "{name}, {topic} ke {n} open items mein se kitne serious hain?",
            "{name}, {topic} mein abhi kya missing hai?",
            "Kya {topic} ke liye koi {name} ka wait kar raha hai?",
        ),
        answer=(
            "Zyada tar data ka issue hai, {n} rows galat hain.",
            "Unme se sirf {n} important hain, baaki cosmetic hain.",
            "Mujhe lagta hai {n} issues ka root cause ek hi hai.",
            "Sach bolun toh theek hai, {n} percent ka dip reporting glitch tha.",
            "Karib {n} ghante aur lagenge, koi badi baat nahi.",
            "Review pe depend karta hai, lagbhag {n} comments abhi open hain.",
        ),
        disagree=(
            "Mujhe nahi lagta {topic} itna simple hai, pichli baar {n} din lage the.",
            "Meri raay alag hai, {topic} ko kam se kam {n} baar aur test karna hoga.",
            "{topic} ka {n} percent wala figure mujhe zyada optimistic lag raha hai.",
            "Jab tak {n} complaints open hain, main {topic} ko done nahi bolunga.",
        ),
        reply=(
            "Fair point, {topic} ko {n} din aur dekhte hain.",
            "Ho sakta hai, par un {n} complaints mein {m} duplicate hain.",
            "Theek hai, next time {topic} ke detailed numbers laaunga, karib {n} pages.",
            "Is pe hum agree to disagree kar lete hain, {topic} ka trend abhi bhi {n} points upar hai.",
        ),
        suggest=(
            "Kya {topic} ke liye {n} hafte ka contractor rakh lein?",
            "Kya {topic} ko {n} din pause karke reset karein?",
            "{topic} ke liye koi tool le lein? Maine ek {n} hazaar ka dekha tha.",
            "Kya {topic} ko {n} logon wale bade project mein merge kar dein?",
        ),
        decline=(
            "Abhi nahi, {topic} pe {n} hafte baad phir sochenge.",
            "Is quarter {topic} pe {n} hazaar kharch nahi karna chahiye.",
            "Nahi, {topic} jaisa hai theek hai, usko mat chhedo.",
            "{topic} wala idea abhi park karte hain, bahut jhanjhat hai.",
        ),
        side=(
            "Topic se hatke, {side} {n} din se kharab hai.",
            "Waise, kisi ne notice kiya ki {side} phir change ho gaya?",
            "Sorry, ek baat, {side} ka survey {n} responses ke baad band hoga.",
            "Alag baat hai, {side} ki {n} complaints ho gayi hain, admin ko batana chahiye.",
        ),
        ack=(
            "Theek hai, {topic} note kar liya.",
            "Thanks {name}, {topic} pe yeh helpful hai.",
            "Samajh gaya, {topic} abhi theek lag raha hai.",
            "Achha, {topic} pe meri taraf se aur kuch nahi.",
            "Theek hai, {topic} se aage badhte hain.",
        ),
        close="Theek hai, aaj ke liye itna hi. Poore time rukne ke liye sabka thanks.",
    ),
}


# ─── the long meetings ────────────────────────────────────────────────────────────────────────────


@dataclass
class Fact:
    """A labelled exchange inserted at `minute`: lines (speaker key, text, is evidence)."""

    minute: float
    lines: list[tuple[str, str, bool]]
    label: dict  # section ("decisions" | "action_items" | "distractors") and label fields


@dataclass
class Spec:
    id: str
    language: str
    title: str
    meeting_date: str
    duration_min: int
    people: dict[str, str]  # key → attendee name
    call: dict[str, str]  # key → how others say the name in speech
    chair: str
    facts: list[Fact] = field(default_factory=list)


def D(**kw) -> dict:  # noqa: N802 - a decision label
    return {"section": "decisions", **kw}


def A(**kw) -> dict:  # noqa: N802 - an action item label
    return {"section": "action_items", **kw}


def X(**kw) -> dict:  # noqa: N802 - a distractor label
    return {"section": "distractors", **kw}


SPECS: list[Spec] = [
    Spec(
        id="90-quarterly-operations-review", language="en", title="Quarterly operations review",
        meeting_date="2026-11-10", duration_min=60,
        people={"R": "Ritu Sharma", "S": "Sumit Arora", "K": "Kavita Nair", "M": "Manoj Tiwari",
                "P": "Pooja Bhatt"},
        call={"R": "Ritu", "S": "Sumit", "K": "Kavita", "M": "Manoj", "P": "Pooja"},
        chair="R",
        facts=[
            Fact(7, [("S", "The Indore depot lease is up, the owner offers five more years at the old rent.", False),
                     ("R", "Then let's take it: we renew the Indore depot lease for five years.", True),
                     ("K", "Agreed, no reason to move.", False)],
                 D(id="D1", fact="The Indore depot lease is renewed for five years.",
                   match=[["indore"], ["lease"]], evidence=["@1"])),
            Fact(16, [("M", "Our courier partners have no common scorecard, every region rates them differently.", False),
                      ("R", "Kavita, please build one courier scorecard for all regions by Friday.", True),
                      ("K", "Yes, I will have the scorecard ready by Friday.", True)],
                 A(id="A1", fact="Kavita builds a single courier scorecard.", match=[["scorecard"]],
                   owner="Kavita Nair", due="2026-11-13", due_phrase="by Friday", evidence=["@1", "@2"])),
            Fact(24, [("P", "Should we move all dispatch to a single hub in Bhopal?", False),
                      ("R", "No, a single hub is too risky this year, let's not.", False)],
                 X(id="X1", kind="not_agreed", fact="A single dispatch hub in Bhopal was proposed and rejected.",
                   match=[["bhopal"]], evidence=["@0"])),
            Fact(31, [("K", "The fire safety certificate of the Indore depot runs out next month.", False),
                      ("R", "Someone has to file the fire certificate renewal in two weeks, it cannot lapse.", True),
                      ("S", "We will find a person for it.", False)],
                 A(id="A2", fact="The depot's fire certificate renewal gets filed; nobody named.",
                   match=[["fire"], ["certificate", "renewal"]], owner="unassigned",
                   due="2026-11-24", due_phrase="in two weeks", evidence=["@1"])),
            Fact(45, [("M", "Small thing before I forget: generator upkeep for all depots goes to Dhruvtara Power from January, at 2.8 lakh a year, already signed off.", True)],
                 D(id="D2", needle=True, fact="Generator maintenance for the depots is contracted to Dhruvtara Power.",
                   match=[["dhruvtara"]], evidence=["@0"])),
            Fact(52, [("S", "For the new year roster we need the night crews confirmed.", False),
                      ("R", "Sumit, send the January night roster to the depot heads tomorrow.", True),
                      ("S", "Okay, the roster goes out tomorrow.", True),
                      ("P", "The safety audit itself is on 18 December, that is separate.", False)],
                 A(id="A3", fact="Sumit sends the January night roster to the depot heads.", match=[["roster"]],
                   owner="Sumit Arora", due="2026-11-11", due_phrase="tomorrow", evidence=["@1", "@2"])),
            Fact(53, [], X(id="X2", kind="other_date", fact="The safety audit on 18 December is not the roster's date.",
                           match=[["safety audit"]], evidence=["@-1"])),
        ],
    ),
    Spec(
        id="91-annual-planning-offsite", language="en", title="Annual planning offsite",
        meeting_date="2026-11-26", duration_min=90,
        people={"R": "Rohan Kapoor", "S": "Shalini Menon", "F": "Farhan Qureshi", "I": "Isha Verma",
                "V": "Vikram Joshi", "L": "Leela Pillai"},
        call={"R": "Rohan", "S": "Shalini", "F": "Farhan", "I": "Isha", "V": "Vikram", "L": "Leela"},
        chair="R",
        facts=[
            Fact(8, [("S", "The Pune warehouse landlord offered three more years at the same rent.", False),
                     ("R", "That is a good offer. We renew the Pune warehouse lease for three years.", True),
                     ("F", "Agreed.", False)],
                 D(id="D1", fact="The Pune warehouse lease is renewed for three years.",
                   match=[["pune"], ["lease"]], evidence=["@1"])),
            Fact(19, [("V", "We still have no vendor scorecard for the packaging suppliers.", False),
                      ("R", "Isha, please draft the packaging scorecard by next Wednesday.", True),
                      ("I", "Yes, I will draft it by next Wednesday.", True)],
                 A(id="A1", fact="Isha drafts the packaging supplier scorecard.", match=[["scorecard"]],
                   owner="Isha Verma", due="2026-12-02", due_phrase="next Wednesday", evidence=["@1", "@2"])),
            Fact(30, [("L", "What if we shifted the whole call centre to Nashik next year?", False),
                      ("R", "No, not this year, we are not moving the call centre.", False)],
                 X(id="X1", kind="not_agreed", fact="Moving the call centre to Nashik was suggested and rejected.",
                   match=[["nashik"]], evidence=["@0"])),
            Fact(37, [("L", "The pollution clearance for the Pune site expires at year end.", False),
                      ("R", "Someone has to file the pollution clearance renewal in two weeks.", True),
                      ("F", "Okay, we will find someone, it should not slip.", False)],
                 A(id="A2", fact="The pollution clearance renewal is filed; nobody named.",
                   match=[["pollution"], ["clearance", "renewal"]], owner="unassigned",
                   due="2026-12-10", due_phrase="in two weeks", evidence=["@1"])),
            Fact(45, [("V", "One more before I lose it: the cold room compressors will be serviced by Meghdoot Cooling from now on, fourteen thousand a visit, that is final.", True)],
                 D(id="D2", needle=True, fact="Cold room compressor servicing goes to Meghdoot Cooling.",
                   match=[["meghdoot"]], evidence=["@0"])),
            Fact(58, [("F", "The night shift has three crews and two of them are half idle.", False),
                      ("R", "Then from January the night shift runs with two crews, not three.", True),
                      ("S", "Fine with me.", False)],
                 D(id="D3", fact="The night shift goes from three crews to two from January.",
                   match=[["night shift"], ["two", "2"]], evidence=["@1"])),
            Fact(71, [("F", "I can send the January roster to all supervisors.", False),
                      ("R", "Yes Farhan, send the January roster tomorrow.", True),
                      ("F", "Done, the roster goes out tomorrow.", True),
                      ("I", "Remember the insurance renewal is on 15 January, different thing.", False)],
                 A(id="A3", fact="Farhan sends the January roster to supervisors.", match=[["roster"]],
                   owner="Farhan Qureshi", due="2026-11-27", due_phrase="tomorrow", evidence=["@1", "@2"])),
            Fact(72, [], X(id="X2", kind="other_date", fact="The insurance renewal on 15 January is not the roster's date.",
                           match=[["insurance"]], evidence=["@-1"])),
            Fact(83, [("S", "The drivers do not know about the new shift pattern yet.", False),
                      ("R", "We need to brief the drivers on the shift change, whoever is free.", True)],
                 A(id="A4", fact="The drivers are briefed on the new shift pattern; nobody named, no date.",
                   match=[["drivers"], ["shift"]], owner="unassigned", due=None, due_phrase=None,
                   evidence=["@1"])),
        ],
    ),
    Spec(
        id="90-district-programme-review", language="hi", title="ज़िला कार्यक्रम समीक्षा",
        meeting_date="2026-10-20", duration_min=60,
        people={"N": "Neha Gupta", "A": "Amit Saxena", "D": "Deepak Chauhan", "P": "Pooja Bhatt",
                "M": "Manoj Tiwari"},
        call={"N": "नेहा", "A": "अमित", "D": "दीपक", "P": "पूजा", "M": "मनोज"},
        chair="N",
        facts=[
            Fact(6, [("A", "जबलपुर गोदाम का किराया समझौता खत्म हो रहा है, मालिक पुराने किराये पर चार साल और दे रहे हैं।", False),
                     ("N", "तो तय रहा, जबलपुर गोदाम का किराया समझौता चार साल के लिए बढ़ाते हैं।", True),
                     ("D", "ठीक है, सहमत।", False)],
                 D(id="D1", fact="The Jabalpur warehouse lease is extended for four years.",
                   match=[["जबलपुर", "jabalpur"], ["किराया", "lease", "समझौता"]], evidence=["@1"])),
            Fact(15, [("M", "हर ब्लॉक के स्वयंसेवकों की हाज़िरी अलग अलग रजिस्टर में है।", False),
                      ("N", "पूजा जी, आप शुक्रवार तक हाज़िरी का एक साझा रजिस्टर बना दीजिए।", True),
                      ("P", "जी, शुक्रवार तक हाज़िरी रजिस्टर तैयार कर दूँगी।", True)],
                 A(id="A1", fact="Pooja prepares a shared volunteer attendance register.",
                   match=[["हाज़िरी", "हाजिरी", "attendance"]], owner="Pooja Bhatt",
                   due="2026-10-23", due_phrase="शुक्रवार तक", evidence=["@1", "@2"])),
            Fact(23, [("D", "क्या हम सारे शिविर एक ही जगह सागर में कर दें?", False),
                      ("N", "नहीं, सागर में सब इकट्ठा करना इस साल ठीक नहीं, ऐसा नहीं करेंगे।", False)],
                 X(id="X1", kind="not_agreed", fact="Holding all camps in Sagar was suggested and rejected.",
                   match=[["सागर", "sagar"]], evidence=["@0"])),
            Fact(32, [("P", "वाहनों का फिटनेस प्रमाणपत्र अगले महीने खत्म हो रहा है।", False),
                      ("N", "किसी को कल तक फिटनेस प्रमाणपत्र के नवीनीकरण का आवेदन डालना होगा।", True),
                      ("A", "देखते हैं कौन खाली है।", False)],
                 A(id="A2", fact="The vehicle fitness certificate renewal is filed; nobody named.",
                   match=[["फिटनेस", "fitness"], ["प्रमाणपत्र", "certificate", "नवीनीकरण"]],
                   owner="unassigned", due="2026-10-21", due_phrase="कल तक", evidence=["@1"])),
            Fact(45, [("D", "एक बात भूल न जाऊँ: जनरेटर की देखभाल अब से सूर्यकिरण इलेक्ट्रिकल्स करेगी, साल का एक लाख नब्बे हज़ार, यह तय हो चुका है।", True)],
                 D(id="D2", needle=True, fact="Generator upkeep is contracted to Suryakiran Electricals.",
                   match=[["सूर्यकिरण", "suryakiran"]], evidence=["@0"])),
            Fact(53, [("M", "नवंबर के प्रशिक्षण का रोस्टर अभी तक किसी को नहीं भेजा गया।", False),
                      ("N", "मनोज जी, अगले मंगलवार तक नवंबर का रोस्टर सब ब्लॉक को भेज दीजिए।", True),
                      ("M", "जी, अगले मंगलवार तक रोस्टर भेज दूँगा।", True),
                      ("A", "और ज़िला निरीक्षण 12 नवंबर को है, वह अलग बात है।", False)],
                 A(id="A3", fact="Manoj sends the November training roster to all blocks.",
                   match=[["रोस्टर", "roster"]], owner="Manoj Tiwari",
                   due="2026-10-27", due_phrase="अगले मंगलवार तक", evidence=["@1", "@2"])),
            Fact(54, [], X(id="X2", kind="other_date", fact="The district inspection on 12 November is not the roster's date.",
                           match=[["निरीक्षण", "inspection"]], evidence=["@-1"])),
        ],
    ),
    Spec(
        id="91-annual-health-mission-planning", language="hi", title="वार्षिक स्वास्थ्य मिशन योजना",
        meeting_date="2026-12-02", duration_min=90,
        people={"K": "Kavita Nair", "S": "Sumit Arora", "I": "Isha Verma", "D": "Deepak Chauhan",
                "N": "Neha Gupta", "R": "Rohan Kapoor"},
        call={"K": "कविता", "S": "सुमित", "I": "ईशा", "D": "दीपक", "N": "नेहा", "R": "रोहन"},
        chair="K",
        facts=[
            Fact(9, [("S", "रीवा के उप केंद्र का भवन किराये पर है, मालिक तीन साल और देने को तैयार हैं।", False),
                     ("K", "ठीक है, रीवा उप केंद्र का किराया समझौता तीन साल के लिए बढ़ा देते हैं।", True),
                     ("D", "सहमत।", False)],
                 D(id="D1", fact="The Rewa sub-centre lease is extended for three years.",
                   match=[["रीवा", "rewa"], ["किराया", "lease", "समझौता"]], evidence=["@1"])),
            Fact(20, [("N", "टीकाकरण शिविरों की कोई एक चेकलिस्ट नहीं है।", False),
                      ("K", "ईशा, अगले मंगलवार तक शिविरों की चेकलिस्ट बना दो।", True),
                      ("I", "जी, अगले मंगलवार तक चेकलिस्ट तैयार कर दूँगी।", True)],
                 A(id="A1", fact="Isha prepares the vaccination camp checklist.",
                   match=[["चेकलिस्ट", "checklist"]], owner="Isha Verma",
                   due="2026-12-08", due_phrase="अगले मंगलवार तक", evidence=["@1", "@2"])),
            Fact(29, [("R", "क्या पूरी लैब सतना में ले जाएँ?", False),
                      ("K", "नहीं, सतना में लैब ले जाना अभी नहीं होगा, इसे छोड़ते हैं।", False)],
                 X(id="X1", kind="not_agreed", fact="Moving the lab to Satna was suggested and dropped.",
                   match=[["सतना", "satna"]], evidence=["@0"])),
            Fact(37, [("D", "बायो मेडिकल कचरे का लाइसेंस दिसंबर के अंत में खत्म होगा।", False),
                      ("K", "किसी को दस दिन में कचरा लाइसेंस के नवीनीकरण का काम करना होगा।", True),
                      ("S", "हम लोग देख लेंगे कौन करेगा।", False)],
                 A(id="A2", fact="The bio-medical waste licence renewal is done; nobody named.",
                   match=[["कचरा", "कचरे", "waste"], ["लाइसेंस", "licence", "license"]],
                   owner="unassigned", due="2026-12-12", due_phrase="दस दिन में", evidence=["@1"])),
            Fact(45, [("S", "एक छोटी बात: एम्बुलेंस के टायर अब से मेघमल्हार मोटर्स से आएँगे, हर टायर साढ़े छह हज़ार में, मंज़ूरी मिल चुकी है।", True)],
                 D(id="D2", needle=True, fact="Ambulance tyres will be bought from Meghmalhar Motors.",
                   match=[["मेघमल्हार", "meghmalhar"]], evidence=["@0"])),
            Fact(57, [("I", "रात की पाली में तीन टीमें हैं पर मरीज़ कम आते हैं।", False),
                      ("K", "तो जनवरी से रात की पाली में तीन की जगह दो टीमें रहेंगी।", True),
                      ("R", "ठीक है।", False)],
                 D(id="D3", fact="Night shift goes from three teams to two from January.",
                   match=[["रात की पाली", "night shift"], ["दो", "2", "two"]], evidence=["@1"])),
            Fact(70, [("D", "जनवरी की ड्यूटी सूची सब केंद्रों को जानी है।", False),
                      ("K", "दीपक, कल तक जनवरी की ड्यूटी सूची भेज दो।", True),
                      ("D", "जी, ड्यूटी सूची कल तक चली जाएगी।", True),
                      ("N", "और राज्य स्तर की समीक्षा 20 जनवरी को है, वह अलग है।", False)],
                 A(id="A3", fact="Deepak sends the January duty list to all centres.",
                   match=[["ड्यूटी सूची", "duty list", "duty roster", "ड्यूटी रोस्टर"]],
                   owner="Deepak Chauhan", due="2026-12-03", due_phrase="कल तक", evidence=["@1", "@2"])),
            Fact(71, [], X(id="X2", kind="other_date", fact="The state review on 20 January is not the duty list's date.",
                           match=[["राज्य स्तर", "state level"]], evidence=["@-1"])),
            Fact(82, [("N", "आशा कार्यकर्ताओं को नई पाली के बारे में अभी तक नहीं बताया गया।", False),
                      ("K", "आशा कार्यकर्ताओं को नई पाली की जानकारी देनी है, जो भी खाली हो।", True)],
                 A(id="A4", fact="ASHA workers are told about the new shift; nobody named, no date.",
                   match=[["आशा", "asha"]], owner="unassigned", due=None, due_phrase=None,
                   evidence=["@1"])),
        ],
    ),
    Spec(
        id="90-d2c-growth-review", language="hinglish", title="D2C growth review",
        meeting_date="2026-11-13", duration_min=60,
        people={"A": "Aditya Rane", "S": "Sneha Kulkarni", "Y": "Yash Mehra", "T": "Tara Dsouza",
                "H": "Harsh Vora"},
        call={"A": "Aditya", "S": "Sneha", "Y": "Yash", "T": "Tara", "H": "Harsh"},
        chair="A",
        facts=[
            Fact(7, [("Y", "Surat wale godown ka lease khatam ho raha hai, owner same rent pe do saal aur de raha hai.", False),
                     ("A", "Theek hai, final karte hain, Surat godown ka lease do saal ke liye renew karenge.", True),
                     ("T", "Haan, agreed.", False)],
                 D(id="D1", fact="The Surat godown lease is renewed for two years.",
                   match=[["surat"], ["lease"]], evidence=["@1"])),
            Fact(16, [("H", "Influencer payouts ka koi tracker nahi hai, sab Excel mein bikhra hai.", False),
                      ("A", "Sneha, Monday tak influencer payout tracker bana do please.", True),
                      ("S", "Haan, Monday tak tracker ready kar dungi.", True)],
                 A(id="A1", fact="Sneha builds an influencer payout tracker.",
                   match=[["influencer"], ["tracker"]], owner="Sneha Kulkarni",
                   due="2026-11-16", due_phrase="Monday tak", evidence=["@1", "@2"])),
            Fact(25, [("T", "Kya hum saara stock Jaipur ke ek dark store mein shift kar dein?", False),
                      ("A", "Nahi yaar, Jaipur wala idea abhi nahi, isko chhodo.", False)],
                 X(id="X1", kind="not_agreed", fact="Moving all stock to a Jaipur dark store was suggested and dropped.",
                   match=[["jaipur"]], evidence=["@0"])),
            Fact(33, [("Y", "FSSAI license ka renewal due hai, expiry paas aa rahi hai.", False),
                      ("A", "Kisi ko parso tak FSSAI renewal file karna padega, koi bhi kar lo.", True),
                      ("H", "Dekhte hain kaun free hai.", False)],
                 A(id="A2", fact="The food licence renewal is filed; nobody named.",
                   match=[["fssai", "food license", "food licence"]], owner="unassigned",
                   due="2026-11-15", due_phrase="parso tak", evidence=["@1"])),
            Fact(45, [("T", "Ek chhoti baat bhool na jaun: gift boxes ab se Kesarbagh Prints banayega, baarah rupaye per box, yeh final ho gaya hai.", True)],
                 D(id="D2", needle=True, fact="Gift box printing goes to Kesarbagh Prints.",
                   match=[["kesarbagh"]], evidence=["@0"])),
            Fact(52, [("S", "December sale ke liye creative calendar abhi tak lock nahi hua.", False),
                      ("A", "Harsh, agle Thursday tak December ka creative calendar lock kar do.", True),
                      ("H", "Haan, agle Thursday tak calendar lock ho jayega.", True),
                      ("Y", "Aur warehouse audit 4 December ko hai, woh alag cheez hai.", False)],
                 A(id="A3", fact="Harsh locks the December creative calendar.",
                   match=[["creative"], ["calendar"]], owner="Harsh Vora",
                   due="2026-11-19", due_phrase="agle Thursday tak", evidence=["@1", "@2"])),
            Fact(53, [], X(id="X2", kind="other_date", fact="The warehouse audit on 4 December is not the calendar's date.",
                           match=[["warehouse audit"]], evidence=["@-1"])),
        ],
    ),
    Spec(
        id="91-campus-placement-planning", language="hinglish", title="Campus placement planning",
        meeting_date="2026-12-08", duration_min=60,
        people={"G": "Gaurav Bhalla", "N": "Nisha Rathore", "K": "Kunal Ahuja", "M": "Mitali Sen",
                "O": "Omkar Patil"},
        call={"G": "Gaurav", "N": "Nisha", "K": "Kunal", "M": "Mitali", "O": "Omkar"},
        chair="G",
        facts=[
            Fact(8, [("K", "Lucknow campus ke saath MoU khatam ho raha hai, woh teen saal aur sign karne ko ready hain.", False),
                     ("G", "Done, Lucknow campus ka MoU teen saal ke liye renew karte hain.", True),
                     ("M", "Sahi hai.", False)],
                 D(id="D1", fact="The Lucknow campus MoU is renewed for three years.",
                   match=[["lucknow"], ["mou"]], evidence=["@1"])),
            Fact(17, [("O", "Interview panels ka koi common rubric nahi hai, har panel alag marks deta hai.", False),
                      ("G", "Nisha, month end tak ek common interview rubric bana do.", True),
                      ("N", "Theek hai, month end tak rubric share kar dungi.", True)],
                 A(id="A1", fact="Nisha prepares a common interview rubric.",
                   match=[["rubric"]], owner="Nisha Rathore",
                   due="2026-12-31", due_phrase="month end tak", evidence=["@1", "@2"])),
            Fact(26, [("M", "Kya hum saare interviews Dehradun mein ek hi jagah kar lein?", False),
                      ("G", "Nahi, Dehradun mein sab ek jagah karna practical nahi, rehne do.", False)],
                 X(id="X1", kind="not_agreed", fact="Holding all interviews in Dehradun was suggested and dropped.",
                   match=[["dehradun"]], evidence=["@0"])),
            Fact(34, [("K", "Offer letters ka legal review pending hai.", False),
                      ("G", "Kisi ko agle Monday tak offer letter ka legal review karwana hoga.", True),
                      ("O", "Hum log dekh lenge kaun karega.", False)],
                 A(id="A2", fact="Offer letters get a legal review; nobody named.",
                   match=[["offer letter"], ["legal"]], owner="unassigned",
                   due="2026-12-14", due_phrase="agle Monday tak", evidence=["@1"])),
            Fact(45, [("O", "Ek baat jaldi se: campus visits ke liye cabs ab Neelgagan Travels se aayengi, aath sau rupaye per trip, approve ho chuka hai.", True)],
                 D(id="D2", needle=True, fact="Campus visit cabs are booked with Neelgagan Travels.",
                   match=[["neelgagan"]], evidence=["@0"])),
            Fact(53, [("M", "Final round ki shortlist abhi colleges ko nahi gayi.", False),
                      ("G", "Mitali, kal tak final shortlist sab colleges ko bhej do.", True),
                      ("M", "Haan, shortlist kal tak chali jayegi.", True),
                      ("K", "Aur HR conclave 22 January ko hai, woh alag hai.", False)],
                 A(id="A3", fact="Mitali sends the final shortlist to the colleges.",
                   match=[["shortlist"]], owner="Mitali Sen",
                   due="2026-12-09", due_phrase="kal tak", evidence=["@1", "@2"])),
            Fact(54, [], X(id="X2", kind="other_date", fact="The HR conclave on 22 January is not the shortlist's date.",
                           match=[["conclave"]], evidence=["@-1"])),
        ],
    ),
]


# ─── composition ──────────────────────────────────────────────────────────────────────────────────


class Composer:
    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.bank = BANKS[spec.language]
        self.rng = random.Random(zlib.crc32(f"{spec.language}/{spec.id}".encode()))
        self.segments: list[dict] = []
        self.seen: set[str] = set()
        self.t = 0.0
        self.facts = sorted(spec.facts, key=lambda f: f.minute)
        self.labels: dict[str, list[dict]] = {"decisions": [], "action_items": [], "distractors": []}
        self.last_fact_ids: list[str] = []

    def speaker(self, avoid: str | None = None) -> str:
        keys = [k for k in self.spec.people if k != avoid]
        weights = [3 if k == self.spec.chair else 1 for k in keys]
        return self.rng.choices(keys, weights=weights)[0]

    def say(self, key: str, text: str) -> str:
        local = f"s{len(self.segments) + 1:04d}"
        self.segments.append(
            {"id": local, "start_s": round(self.t, 1), "speaker": self.spec.people[key], "text": text}
        )
        self.seen.add(text)
        self.t += 4.0 + self.rng.random() * 3.0
        return local

    def fill(self, templates: tuple[str, ...], topic: str, name_of: str) -> str:
        for attempt in range(400):
            n, m = self.rng.randint(3, 97), self.rng.randint(2, 60)
            who = name_of if attempt < 20 else self.rng.choice(list(self.spec.call))
            text = self.rng.choice(templates).format(
                topic=topic, name=self.spec.call[who], n=n, m=m, side=self.rng.choice(self.bank.sides)
            )
            text = text[0].upper() + text[1:]
            if text not in self.seen:
                self.seen.add(text)  # reserved now, so one scene never repeats itself
                return text
        raise RuntimeError(f"{self.spec.id}: the phrase bank ran out of unique lines")

    def due_fact(self) -> Fact | None:
        if self.facts and self.t >= self.facts[0].minute * 60:
            return self.facts.pop(0)
        return None

    def insert(self, fact: Fact) -> None:
        ids = [self.say(key, text) for key, text, _ in fact.lines]
        label = dict(fact.label)
        section = label.pop("section")
        evidence = []
        for ref in label["evidence"]:
            index = int(ref[1:])
            evidence.append((ids if ids else self.last_fact_ids)[index])
        label["evidence"] = evidence
        self.labels[section].append(label)
        if ids:
            self.last_fact_ids = ids

    def scene(self) -> list[tuple[str, str]]:
        b, rng = self.bank, self.rng
        topic = rng.choice(b.topics)
        lead = self.spec.chair if rng.random() < 0.6 else self.speaker()
        other = self.speaker(avoid=lead)
        lines = [(lead, self.fill(b.opener, topic, other))]
        for _ in range(rng.randint(2, 5)):
            who = self.speaker(avoid=lead) if rng.random() < 0.7 else lead
            lines.append((who, self.fill(b.status, topic, self.speaker(avoid=who))))
            if rng.random() < 0.35:
                asker = self.speaker(avoid=who)
                lines.append((asker, self.fill(b.question, topic, who)))
                lines.append((who, self.fill(b.answer, topic, asker)))
        if rng.random() < 0.4:
            a = self.speaker()
            b2 = self.speaker(avoid=a)
            lines.append((a, self.fill(b.disagree, topic, b2)))
            lines.append((b2, self.fill(b.reply, topic, a)))
        if rng.random() < 0.3:
            a = self.speaker()
            lines.append((a, self.fill(b.suggest, topic, a)))
            lines.append((self.spec.chair, self.fill(b.decline, topic, a)))
        if rng.random() < 0.2:
            a = self.speaker()
            lines.append((a, self.fill(b.side, topic, a)))
        lines.append((lead, self.fill(b.ack, topic, other)))
        return lines

    def compose(self) -> dict:
        end = self.spec.duration_min * 60 - 12
        while self.t < end:
            for key, text in self.scene():
                if (fact := self.due_fact()) is not None:
                    self.insert(fact)
                if self.t >= end:
                    break
                self.say(key, text)
        while (fact := self.due_fact()) is not None or self.facts:
            self.insert(fact or self.facts.pop(0))
        self.say(self.spec.chair, self.bank.close)
        data = {
            "id": self.spec.id,
            "language": self.spec.language,
            "kind": "long",
            "title": self.spec.title,
            "meeting_date": self.spec.meeting_date,
            "duration_min": self.spec.duration_min,
            "attendees": list(self.spec.people.values()),
            "segments": self.segments,
            "labels": self.labels,
        }
        self._check(data)
        return data

    def _check(self, data: dict) -> None:
        """No filler line may match a label (the labels are only said where they are labelled)."""
        fact_texts = {text for f in self.spec.facts for _, text, _ in f.lines}
        for seg in data["segments"]:
            if seg["text"] in fact_texts:
                continue
            for section in ("decisions", "action_items", "distractors"):
                for label in data["labels"][section]:
                    if matches(label["match"], seg["text"]):
                        raise RuntimeError(
                            f"{self.spec.id}: filler {seg['id']} matches label {label['id']}"
                        )


def build() -> dict[Path, str]:
    out = {}
    for spec in SPECS:
        data = Composer(spec).compose()
        out[ROOT / spec.language / f"{spec.id}.json"] = dumps(data)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail unless the files match")
    parser.add_argument("--count", action="store_true", help="print the set's totals")
    args = parser.parse_args(argv)
    if args.count:
        fixtures = load_all(ROOT)
        totals = count(fixtures)
        long = [f"{f.language}/{f.id} {f.duration_min} min {len(f.segments)} segments, needle "
                f"{'yes' if f.needle() else 'NO'}" for f in fixtures if f.kind == "long"]
        print(json.dumps(totals))
        print("\n".join(long))
        return 0
    built = build()
    if args.check:
        stale = [p for p, text in built.items() if not p.exists() or p.read_text("utf-8") != text]
        for p in stale:
            print(f"differs: {p.relative_to(ROOT)}")
        print("long fixtures match" if not stale else f"{len(stale)} long fixture(s) differ")
        return 1 if stale else 0
    problems = []
    for path, text in built.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        problems += validate(load(path))
    for p in problems:
        print(p)
    print(f"wrote {len(built)} long fixtures; {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
