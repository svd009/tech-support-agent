"""Intent routing: fast keyword rules first, optional small-model fallback second.

The model only picks ONE label from a fixed list. Deciding what to do with that label is
code (playbooks.py), so a small local model cannot skip a diagnostic, invent a result or
forget to escalate.
"""
import logging
import os
import re

logger = logging.getLogger("support")

ISSUE_CATEGORIES = ["vpn", "email", "network", "password", "software", "device_compliance",
                    "hardware", "security", "safety", "office_app", "access"]
INFO_INTENTS = ["greeting", "thanks", "scope", "privilege", "off_topic", "sign_out", "service_status", "ticket_status", "list_devices",
                "create_ticket", "how_to"]
ALL_LABELS = ISSUE_CATEGORIES + INFO_INTENTS + ["unknown"]

FAILURE = r"(not working|doesn'?t work|isn'?t working|won'?t|can'?t|cannot|unable|fail|error|broken|issue|problem|stopped|stuck|slow|keeps|freez\w*|hang\w*|down\b|disconnect|crash|not (connecting|loading|opening|sending|receiving|syncing))"
NEGATED_TICKET = r"(?:do not|don'?t|never|no need to)\s+(?:create|open|raise|submit|log|file)\b.{0,20}\bticket|\bno ticket\b|without (?:a )?ticket"


def _norm(text: str) -> str:
    return " " + text.lower().replace("’", "'").strip() + " "


def declines_ticket(text: str) -> bool:
    return bool(re.search(NEGATED_TICKET, _norm(text)))


def wants_ticket(text: str) -> bool:
    t = _norm(text)
    return not declines_ticket(text) and bool(
        re.search(r"\b(create|open|raise|submit|log|file|make)\b.{0,25}\bticket\b|\b(escalate|human|technician|real person)\b", t))


_CLOSING = (r"\b(thanks?|thank you|thx|cheers|bye|goodbye|see (you|ya)|take care|that'?s (all|it|everything)|"
            r"nothing (else|more)|no thanks|all set|(i'?m|im|we'?re) (good|fine|set|done|all set)|have a (good|great|nice))\b")


def is_closing(text: str) -> bool:
    """A short goodbye or thank-you ("No, I'm good, thanks", "ok bye") that carries no new problem."""
    t = _norm(text)
    return len(re.findall(r"[a-z0-9']+", t)) <= 9 and bool(re.search(_CLOSING, t)) and not re.search(FAILURE, t) \
        and not re.search(r"\b(but|still|again|drop\w*|though|however)\b", t)


_OTHERS = (r"\b(everyone|everybody|every (employee|user|account|person)|all (of )?(the |our |my )?"
           r"(employees|people|users|staff|accounts|passwords|colleagues|coworkers)|(whole|entire) (company|office|team)|"
           r"(someone|somebody|another (person|user|employee)|a colleague|my (colleague|coworker|manager|boss|team ?mate|friend))('?s)?)\b")
_SENSITIVE_ACTION = r"\b(passwords?|reset|unlock|credentials|mailbox|inbox|delete|disable|permissions?|admin)\b"
_OVERRIDE = r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(rules?|instructions|polic(y|ies)|restrictions|guidelines|previous)\b"


_PRIVILEGE = (r"\b(admin(istrator)?|root|superuser|super user|elevated|sudo)\b.{0,25}\b(access|rights?|privileges?|permissions?|role|account)\b|"
              r"\b(make|give|grant|add|promote|set|let)\b.{0,12}\b(me|us)\b.{0,25}\b(admin(istrator)?|superuser|root)\b|"
              r"\bgive me admin\b|\bi (need|want) (to be )?(an? )?(local )?admin\b|\b(local|domain|global) admin\b")


_OFF_TOPIC = (r"\b(weather|forecast|will it rain|horoscope|lyrics|recipes?|jokes?|riddles?|poems?|bitcoin|crypto(currency)? price|"
              r"(stock|share) price|gta ?\d*|playstation|xbox|netflix|movies?|tv shows?|celebrit\w+|"
              r"when (is|does|will) .{0,30}(releas\w*|come out|coming out|launch\w*)|release date|"
              r"richest|strongest|tallest|fastest|smartest|greatest|goat|best (football|soccer|basketball|cricket|tennis|baseball)\w*|"
              r"football|soccer|nba|nfl|nhl|mlb|ipl|premier league|world cup|champions league|"
              r"capital of|meaning of life|your favou?rite|how old are you|are you (a )?(human|real|robot|chatgpt|gpt))\b")
_IT_WORDS = (r"\b(vpn|e-?mail|outlook|wi-?fi|network|internet|laptop|computer|pc|mac|password|login|log in|sign in|windows|office|"
             r"teams|excel|word|onedrive|software|install\w*|update|printer|ticket|account|mfa|device|screen|keyboard|slow|error|"
             r"crash\w*|freez\w*|server|file|folder|admin\w*)\b")


def is_off_topic(text: str) -> bool:
    """Trivia, weather, sports, entertainment: nothing to do with IT support. Any IT word wins over this rule."""
    t = _norm(text)
    return bool(re.search(_OFF_TOPIC, t)) and not re.search(_IT_WORDS, t)


def is_privilege_request(text: str) -> bool:
    """Asks for administrator rights, which the agent can never grant."""
    return bool(re.search(_PRIVILEGE, _norm(text)))


_BLOCKED_ACTION = (r"\b(turn off|switch off|disable|bypass|remove|uninstall|stop)\b.{0,25}\b(firewall|antivirus|security (software|settings?)|"
                   r"monitoring|logging|mfa|2fa|encryption|proxy|content filter|web filter)\b|"
                   r"\b(buy|order|purchase|pay for|expense)\b.{0,40}\b(company|corporate) (card|credit card|money|funds|account)\b")


def is_about_others(text: str) -> bool:
    t = _norm(text)
    return bool(re.search(_OTHERS, t) and re.search(_SENSITIVE_ACTION, t)
                and re.search(r"\b(change|reset|unlock|delete|disable|give|grant|access|read|see|send|show|make)\b", t))


def is_out_of_scope(text: str) -> bool:
    """Asks the agent to act on other people's accounts, to ignore its rules, or to do something it must never do."""
    t = _norm(text)
    return bool(re.search(_OVERRIDE, t)) or bool(re.search(_BLOCKED_ACTION, t)) or is_about_others(text)


_SIGN_OUT = r"\b(sign|log)\s*(me\s*)?(out|off)\b|\blogout\b|\bsign ?out\b"

# A model label must be supported by the employee's own words: no VPN diagnosis for "sign me out".
_DOMAIN_WORDS = {
    "vpn": r"vpn|remote|tunnel|globalprotect|anyconnect|connect|disconnect|kick\w*|drop\w*|home",
    "email": r"mail|outlook|inbox|calendar|teams|365|o365|onedrive|exchange|message|send|receiv\w*",
    "network": r"wi-?fi|internet|network|dns|website|site|online|ethernet|connect\w*|offline|router",
    "password": r"password|login|log in|sign|locked|account|mfa|2fa|code|credential\w*|expired",
    "software": r"install\w*|app|software|program|download\w*|licen[cs]e|tool|need|get",
    "device_compliance": r"laptop|computer|pc|device|update\w*|antivirus|encrypt\w*|slow|compliance|patch\w*|windows|mac",
    "hardware": r"screen|keyboard|mouse|battery|charger|monitor|printer|laptop|display|camera|headset|usb|dock\w*|broken|dead",
}


def keyword_intent(text: str) -> str:
    t = _norm(text)
    words = re.findall(r"[a-z0-9+']+", t)
    if not words:
        return "unknown"
    if re.fullmatch(r"\s*(hi|hello|hey|good (morning|afternoon|evening))[\s!.]*", t):
        return "greeting"
    if re.fullmatch(r"\s*(thanks|thank you|thx|cheers|ok|okay|great|perfect)[\s!.a-z]{0,12}", t) and len(words) <= 4 or is_closing(text):
        return "thanks"
    if re.search(_SIGN_OUT, t) and not re.search(r"\b(can'?t|cannot|won'?t|unable|fail\w*|not)\b", t):
        return "sign_out"
    if is_out_of_scope(text):
        return "scope"
    if is_privilege_request(text):
        return "privilege"
    if is_off_topic(text):
        return "off_topic"
    if re.search(r"\bticket\s*#?\s*\d+", t) or re.search(r"\b(my|open) tickets\b|\bstatus of my ticket", t):
        return "ticket_status"
    if re.search(r"\b(which|what|list|show)\b.{0,20}\bdevices?\b", t):
        return "list_devices"
    if re.search(r"\b(smok(e|ing)|on fire|fire|sparks?|sparking|burning smell|melting|swollen|swell\w*|bulg\w*)\b", t):
        return "safety"
    if re.search(r"\b(virus|antivirus|malware|defender|security software)\b.{0,25}\b(protection )?(is |was )?(turned off|off|disabled|stopped|not running)\b", t):
        return "device_compliance"  # protection switched off on the device, not an attack report
    if re.search(r"\b(mfa|2fa|two.factor|authenticator|verification code|push (notification|request))\b", t) and \
            re.search(r"\b(unexpected|didn'?t|did not|not me|random|keep getting|someone|weird|strange)\b", t) or \
            re.search(r"\b(phish\w*|hacked|compromised|suspicious (email|login|sign.in|link|message)|ransomware|virus|malware)\b", t) or \
            re.search(r"\b(e-?mail|message|text)\b.{0,60}\b(ask\w*|wants?|confirm|verify)\b.{0,40}\b(password|login|credentials|bank)\b", t) and re.search(r"\b(link|click\w*|sender|from|fake|suspicious|weird|strange|scam)\b", t) or \
            re.search(r"\b(approval|sign.in|login) (requests?|prompts?|notifications?)\b", t) and re.search(r"\b(didn'?t|did not|not me|unexpected|keep|random)\b", t):
        return "security"
    if wants_ticket(text) and not re.search(FAILURE, t) and len(words) <= 14:
        return "create_ticket"
    personal = re.search(r"\b(my|i|i'm|me|mine)\b", t)
    if re.search(r"^\s*(is|are)\b.*\b(vpn|email|outlook|mail|network|internet)\b.*\b(down|up|working|operational|available|outage)\b", t) and not personal \
            or re.search(r"\b(status of|check)\s+(the\s+)?(vpn|email|network)\s*(service)?\s*status\b|\bany (outage|incident)s?\b", t):
        return "service_status"
    if "password" in t and re.search(r"\b(reset|forgot|forgotten|expired|expire|expiring|change|new|update|lost)\b", t):
        return "password"
    if re.search(r"\b(install|download|need|get|request|deploy)\b", t) and _mentions_software(t):
        return "software"
    if _catalog_app(t) and re.search(r"\b(isn'?t|is not|not) (on|installed)\b|\b(don'?t|do not) have\b|\bmissing\b|\bwants? (me|you) on\b", t):
        return "software"
    if not re.search(r"\b(vpn|globalprotect|anyconnect)\b", t) and re.search(
            r"server not found|no internet|internal (company )?sites|\b(websites?|sites|pages?)\b.{0,25}\b(won'?t|don'?t|do not|not|aren'?t|can'?t)\b.{0,10}\b(load|open|work)", t):
        return "network"
    if re.search(FAILURE, t) and _catalog_app(t):
        return "software"  # an approved app misbehaving -> repair re-deploy
    if re.search(r"\b(excel|powerpoint|onenote|onedrive|sharepoint|workbook|spreadsheet|word (doc\w*|file)|(microsoft|ms) word)\b", t) and re.search(FAILURE, t) \
            or re.search(r"\bword\b", t) and re.search(r"\b(crash\w*|freez\w*|hang\w*|not responding|won'?t open|keeps closing|stuck)\b", t) and not re.search(r"\b(pass|key|swear)\s*word|\bin a word\b", t):
        return "office_app"  # a Microsoft 365 desktop app misbehaving: no remote check exists, so ask, then hand over
    if re.search(r"access (is |was )?denied|\b(don'?t|do not) have (access|permission)|\bno (access|permission)s? to\b|\bneed access to\b|\bpermission denied\b", t):
        return "access"
    if re.search(r"^\s*(how (do|can|should) i|how to|what (is|are|does)|where (can|do) i|can you explain)\b", t) and not re.search(FAILURE, t):
        return "how_to"
    if re.search(r"\b(vpn|globalprotect|anyconnect|remote access|tunnel)\b", t):
        return "vpn"
    if re.search(r"\b(e-?mails?|outlook|mailbox|inbox|exchange|calendar invites?|(office|microsoft|ms) ?3(65|60)|o365|m365)\b", t):
        return "email"
    if re.search(r"\b(locked out|account (is )?locked|can'?t (sign|log) ?in|cannot (sign|log) ?in|login (fails|failed|not working)|password)\b", t):
        return "password"
    if re.search(r"\b(compliance|compliant|bitlocker|encryption|security updates?|patch(es)?|antivirus|defender)\b", t):
        return "device_compliance"
    if re.search(r"\b(wi-?fi|wireless|internet|network|ethernet|dns|no connection|offline|ip address|websites? (won'?t|not))\b", t):
        return "network"
    if re.search(r"\b(software|application|app|program)\b", t) and re.search(r"\b(install|need|request|missing|access)\b", t):
        return "software"
    if re.search(r"\b(slow|sluggish|lagg?(y|ing)|freez\w*|hang\w*|unresponsive)\b", t) and re.search(r"\b(laptop|computer|pc|windows|machine|system)\b", t):
        return "device_compliance"  # slow machine: check updates/encryption/antivirus first, then ask, then hand over
    if re.search(r"\b(screen|display|keyboard|trackpad|touchpad|battery|charger|charging|mouse|monitor|printer|webcam|camera|headset|speaker|microphone|dock|docking|usb|hdmi|laptop|computer|pc)\b", t) and re.search(FAILURE + r"|\b(cracked|dead|won'?t turn on|no power|flicker\w*|black|blank|spilled|dropped|jam\w*|lifting)\b", t):
        return "hardware"
    if is_off_topic(text):
        return "off_topic"
    return "unknown"


def _catalog_app(t: str) -> bool:
    from it_systems import lookup_software
    package, _, needs_approval = lookup_software(t)
    return package is not None and not needs_approval


def _mentions_software(t: str) -> bool:
    from it_systems import lookup_software
    return lookup_software(t)[0] is not None or bool(re.search(r"\b(software|application|app|program|licen[cs]e)\b", t))


def classify(text: str, context: str = "") -> tuple[str, str]:
    """Return (label, source) where source is 'rules', 'model' or 'none'."""
    label = keyword_intent(text)
    if label != "unknown":
        return label, "rules"
    if len(re.findall(r"\w+", text)) < 3:
        return "unknown", "none"
    if os.getenv("UNDERSTAND_WITH_MODEL", "1") == "0":
        return "unknown", "none"
    try:
        from model_client import classify_with_model
        label = classify_with_model(text, context)
    except Exception as error:  # Model unavailable: rules-only mode still works.
        logger.warning('{"event": "classifier_model_unavailable", "type": "%s"}', type(error).__name__)
        return "unknown", "none"
    if label in _DOMAIN_WORDS and not re.search(r"\b(" + _DOMAIN_WORDS[label] + r")\b", _norm(text)):
        logger.warning('{"event": "model_label_rejected", "label": "%s"}', label)
        return "unknown", "none"  # the harness does not accept a label the message gives no evidence for
    return (label, "model") if label in ALL_LABELS else ("unknown", "none")


def followup_signal(text: str) -> str:
    """Classify a reply to 'is it working now?' as positive, negative or neutral."""
    t = _norm(text)
    if re.search(r"\b(still|nope|not (working|fixed|resolved|connecting)|doesn'?t|didn'?t|isn'?t|won'?t|can'?t|cannot|same (error|issue|problem|thing)|fail\w*|error|broken|no luck)\b", t) \
            or re.fullmatch(r"\s*no\b[\s.!,a-z']{0,20}", t):
        return "negative"
    if re.search(r"\b(yes|yeah|yep|yup|works|working|fixed|resolved|connected|sorted|thanks|thank you|great|perfect|done|did it|changed it|reset it|all good|good now|ok|okay)\b", t):
        return "positive"
    return "neutral"
