import logging
import re
from typing import TYPE_CHECKING

from openai import OpenAI

from app.config import settings
from app.models import Business, HaroRequest
from app.regency_niche_gate import is_regency_business

if TYPE_CHECKING:
    from app.asset_types import AssetContext, AssetMode

LOGGER = logging.getLogger(__name__)

# Second-line defense: refuse-style drafts must not be saved for Regency.
_REFUSAL_DRAFT_MARKERS: tuple[str, ...] = (
    "we're not in healthcare",
    "we are not in healthcare",
    "not in healthcare",
    "not the right fit",
    "we don't specialize in",
    "we do not specialize in",
    "we are not experts in",
    "we're not experts in",
    "we are not experts",
    "this may not align",
    "not a fit for",
    "outside our expertise",
    "not our area of expertise",
    "not directly in our area",
    "cannot provide physician",
    "not in the healthcare field",
)


def _refusal_style_draft(body: str) -> bool:
    b = (body or "").lower()
    return any(m in b for m in _REFUSAL_DRAFT_MARKERS)


def _first_name(full_name: str | None) -> str:
    """Extract first name for greeting (e.g. 'John Smith' -> 'John')."""
    if not full_name or not full_name.strip():
        return "there"
    return full_name.strip().split()[0]


def _strip_trailing_signature_copies(body: str, sig: str) -> str:
    """Remove exact trailing copies of the full signature block."""
    sig = (sig or "").strip()
    if not sig:
        return body
    b = body.rstrip()
    tail = "\n\n" + sig
    while b.endswith(tail):
        b = b[: -len(tail)].rstrip()
    alt = "\n" + sig
    while b.endswith(alt) and len(b) > len(sig):
        b = b[: -len(alt)].rstrip()
    if b.strip() == sig.strip():
        return ""
    return b


def _strip_signature_suffix_lines(body: str, sig: str) -> str:
    """Remove trailing lines that match the end of the stored signature (handles partial echoes like name only)."""
    sig = (sig or "").strip()
    if not sig:
        return body
    sig_lines = [ln.strip() for ln in sig.splitlines() if ln.strip()]
    if not sig_lines:
        return body
    lines = body.split("\n")
    while lines:
        matched = False
        for k in range(min(len(sig_lines), len(lines)), 0, -1):
            body_tail = [lines[-k + i].strip().lower() for i in range(k)]
            sig_tail = [sig_lines[-k + i].lower() for i in range(k)]
            if body_tail == sig_tail:
                lines = lines[:-k]
                matched = True
                break
        if not matched:
            break
    return "\n".join(lines).rstrip()


def _strip_common_signoff_lines(body: str) -> str:
    """Remove a trailing 'Thanks,' / 'Best,' style line if it stands alone before a name block (model sign-offs)."""
    lines = body.split("\n")
    closings = {"thanks", "thank you", "best", "best regards", "sincerely", "warmly", "cheers", "regards"}
    while lines:
        last = lines[-1].strip()
        prev = lines[-2].strip().lower().rstrip(",") if len(lines) >= 2 else ""
        if len(lines) >= 2 and prev in closings and last:
            lines = lines[:-2]
            continue
        if len(lines) == 1 and lines[0].strip().lower().rstrip(",") in closings:
            lines = []
            break
        break
    return "\n".join(lines).rstrip()


def _finalize_body_with_signature_once(body: str, sig: str) -> str:
    """Pitch text only, then exactly one block: the business.signature field (nothing else appended)."""
    sig = (sig or "").strip()
    body = (body or "").rstrip()
    if not sig:
        return body
    b = _strip_trailing_signature_copies(body, sig)
    b = _strip_signature_suffix_lines(b, sig)
    b = _strip_common_signoff_lines(b)
    b = _strip_trailing_signature_copies(b, sig)
    b = _strip_signature_suffix_lines(b, sig)
    if not b.strip():
        return sig
    return b + "\n\n" + sig


def _sanitize_draft_style(text: str, *, single_line: bool = False) -> str:
    """Replace em/en dashes and spaced hyphens. For email body, preserve line breaks (plain text paragraphs)."""
    if not text:
        return text
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in ("\u2014", "\u2013", "—", "–"):
        s = s.replace(ch, ", ")
    s = re.sub(r"\s+-\s+", ", ", s)
    if single_line:
        return re.sub(r"\s+", " ", s).strip()
    # Body: keep newlines; only collapse runs of spaces/tabs within each line (not newlines)
    lines = []
    for line in s.split("\n"):
        lines.append(re.sub(r"[ \t]+", " ", line).strip())
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def draft_reply(
    request: HaroRequest,
    business: Business,
    asset_context: "AssetContext | None" = None,
) -> tuple[str, str] | None:
    generated = _draft_with_openai(request, business, asset_context=asset_context)
    if generated:
        subj, body = generated
        body = _sanitize_draft_style(body)
        subj = _sanitize_draft_style(subj, single_line=True)
        if is_regency_business(business) and _refusal_style_draft(body):
            LOGGER.warning(
                "Regency draft safety net rejected refusal-style body for request_id=%s",
                getattr(request, "id", None),
            )
            LOGGER.info(
                "regency_niche_audit request_id=%s source=draft_safety_net action=MATCHED_NO_DRAFT_BLOCKED reason=refusal_phrases_detected",
                getattr(request, "id", None),
            )
            return None
        return subj, body
    first = _first_name(request.journalist_name)
    tail = (request.outlet or request.category or "your request").strip()
    subject = "HARO pitch for %s" % tail
    subject = _sanitize_draft_style(subject, single_line=True)
    sig_only = (business.signature or "").strip()
    asset_note = _fallback_asset_note(asset_context)
    body = (
        "Hi %s,\n\n"
        "Thanks for putting this out there. We work in %s and can speak to what you are looking for "
        "around %s. Happy to share a short quote or more detail if useful.%s"
        % (
            first,
            (business.nature_of_business or business.name).strip(),
            (request.category or "this topic").strip(),
            asset_note,
        )
    )
    if sig_only:
        body = body + "\n\n" + sig_only
    body = _sanitize_draft_style(body)
    if is_regency_business(business) and _refusal_style_draft(body):
        LOGGER.warning(
            "Regency draft safety net rejected fallback draft for request_id=%s",
            getattr(request, "id", None),
        )
        return None
    return subject, body


def _fallback_asset_note(asset_context: "AssetContext | None") -> str:
    if not asset_context:
        return ""
    from app.asset_types import AssetMode

    if asset_context.asset_mode == AssetMode.no_visuals:
        return ""
    if asset_context.asset_mode == AssetMode.real_only:
        return (
            "\n\nWe can provide relevant project images as attachments or links, "
            "pulled from our verified materials."
        )
    if asset_context.asset_mode == AssetMode.concept_allowed:
        return (
            "\n\nWe can share separate styling concept visuals as references. "
            "These are illustrative examples, not photographs of specific completed client projects."
        )
    return ""


def _draft_with_openai(
    request: HaroRequest,
    business: Business,
    asset_context: "AssetContext | None" = None,
) -> tuple[str, str] | None:
    if not settings.openai_api_key:
        return None
    first = _first_name(request.journalist_name)
    system = (
        "You write short HARO pitches for journalists. Sound like a real person who runs or represents a small business. "
        "Warm, conversational, plain English. No corporate jargon, no bullet lists, no emojis. "
        "Do not use em dashes, en dashes, or a hyphen as punctuation between clauses. "
        "Avoid the colon character in the subject line and in the email body. Use commas and short sentences. "
        "Plain text only. No HTML, no markdown, no bold or italics. "
        "Use newline characters for line breaks. Put a blank line between paragraphs (double newline). "
        "Two to four short paragraphs only. Do not add your name, title, phone, email, company name as a sign-off, "
        "or any closing signature. Do not write lines like Thanks or Best followed by a name. "
        "Stop at the last sentence of the pitch. The software appends the official signature block after you. "
        "Only state facts you can infer from the business profile and the query. Do not invent credentials or awards."
    )
    asset_block = _asset_instructions_for_prompt(asset_context)
    user = (
        "Write one email in reply to the journalist query.\n\n"
        "Output exactly in this format (SUBJECT on its own line, then the word BODY alone on a line, then the email).\n"
        "SUBJECT\n"
        "your subject line here\n"
        "BODY\n"
        "Hi FirstName,\n\n"
        "paragraphs only, no sign-off\n\n"
        "Journalist first name for the greeting: %s\n\n"
        "BUSINESS (facts you may use in the pitch only, do not paste contact or signature into the body)\n"
        "Name %s\nContact %s\nNature %s\nKeywords %s\nBrand voice %s\nWebsite %s\n\n"
        "%s\n"
        "QUERY\n%s"
        % (
            first,
            business.name,
            business.contact_name,
            business.nature_of_business,
            business.keywords,
            business.brand_voice,
            business.website_url,
            asset_block,
            (request.request_text or "")[:5000],
        )
    )
    try:
        client = OpenAI(api_key=settings.openai_api_key)
        reply = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.55,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = (reply.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n```\s*$", "", raw).strip()

        subject = ""
        body = ""
        split_m = re.split(r"(?im)^\s*BODY\s*$", raw, maxsplit=1)
        if len(split_m) == 2:
            head, body = split_m[0].strip(), split_m[1].strip()
            hl = head.splitlines()
            if hl and hl[0].strip().upper() == "SUBJECT":
                subject = "\n".join(hl[1:]).strip()
            else:
                for i, line in enumerate(hl):
                    if line.strip().upper() == "SUBJECT" and i + 1 < len(hl):
                        subject = "\n".join(hl[i + 1 :]).strip()
                        break
                if not subject and hl:
                    subject = hl[0].replace("SUBJECT", "").strip().lstrip(":").strip()

        if subject and body:
            body = _sanitize_draft_style(body)
            subject = _sanitize_draft_style(subject.replace("\n", " ").strip(), single_line=True)
            body = _finalize_body_with_signature_once(body, (business.signature or "").strip())
            return subject, body
        return None
    except Exception as exc:
        LOGGER.exception("OpenAI draft failed: %s", exc)
        return None


def _asset_instructions_for_prompt(asset_context: "AssetContext | None") -> str:
    if not asset_context:
        return ""
    from app.asset_types import AssetMode

    mode = asset_context.asset_mode
    lines = ["ASSET AND VISUAL RULES FOR THIS EMAIL"]
    if mode == AssetMode.no_visuals:
        lines.append("Do not promise images or attachments. Short quote-style pitch only.")
    elif mode == AssetMode.real_only:
        lines.append(
            "The sender may attach or link real project images. Mention that images are available "
            "without inventing project names or locations. Do not claim AI-generated content."
        )
    elif mode == AssetMode.concept_allowed:
        lines.append(
            "If visuals are referenced, they are styling concepts or references only, not photos of "
            "completed client jobs. Use honest language. If a disclosure line is needed, add one short sentence."
        )
    if asset_context.must_disclose_ai:
        lines.append(
            "Include one brief natural sentence that attached or linked concept visuals are illustrative, not real installs."
        )
    if asset_context.images_required_in_first_reply:
        lines.append("The journalist asked for visuals in the first reply. Acknowledge that previews follow or are attached.")
    return "\n".join(lines) + "\n\n"
