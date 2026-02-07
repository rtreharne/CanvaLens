import hashlib
import re

import tiktoken


TOKEN_ENCODING = tiktoken.get_encoding("cl100k_base")


def clean_text(text):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def hash_text(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def chunk_text(text, max_tokens=800, overlap=200):
    text = clean_text(text)
    if not text:
        return []

    tokens = TOKEN_ENCODING.encode(text)
    chunks = []
    start = 0
    step = max_tokens - overlap
    if step <= 0:
        step = max_tokens

    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = TOKEN_ENCODING.decode(chunk_tokens)
        chunks.append(chunk_text)
        start += step

    return chunks


TITLE_PATTERNS = [
    r"^(Prof|Professor|Dr|Mr|Mrs|Ms|Miss|Sir|Dame)\b",
]

SUFFIX_PATTERN = re.compile(
    r"(,?\s+)"
    r"((?:[A-Za-z][A-Za-z.]{1,10}|[A-Z]{1,4}\d*)"
    r"(?:\s*,\s*(?:[A-Za-z][A-Za-z.]{1,10}|[A-Z]{1,4}\d*))*)$"
)

_SUFFIX_WORDS = {"hons", "honours", "honors", "jr", "sr"}
_KNOWN_CREDENTIALS = {
    "phd", "md", "mres", "mres.", "msc", "ma", "ba", "bsc", "meng", "meng.",
    "meng.", "mph", "mrc", "mrcp", "mrcs", "mbe", "obe", "cbe", "frs", "frsc",
    "fhea", "sfhea", "afhea", "mbbs", "bmbch", "dphil", "engd", "mdr",
    "llb", "llm", "jd", "dvm", "dds", "dmd", "pharmd", "mba", "mpa", "mpp",
    "mfa", "mme", "meng", "mphys", "msci", "mchem", "mmed", "mclin",
}


def is_credential_token(token):
    token = token.strip().strip(",")
    if not token:
        return False
    if token.startswith("(") and token.endswith(")"):
        return True
    lower = token.lower()
    if lower in _SUFFIX_WORDS:
        return True
    if lower in _KNOWN_CREDENTIALS:
        return True
    if any(ch.isdigit() for ch in token):
        return True
    if "." in token:
        return True
    if token.isupper() and len(token) <= 6:
        return True
    upper_count = sum(ch.isupper() for ch in token)
    if len(token) <= 4 and upper_count >= 2:
        return True
    return False


def split_name_title_suffix(name_line):
    name_line = clean_text(name_line)
    title = ""
    suffix = ""

    for pattern in TITLE_PATTERNS:
        match = re.search(pattern, name_line)
        if match:
            title = match.group(1)
            name_line = name_line[len(match.group(0)):].strip()
            break

    tokens = [t.strip().strip(",") for t in name_line.split()]
    suffix_tokens = []
    i = len(tokens) - 1
    while i >= 0:
        token = tokens[i]
        if is_credential_token(token):
            suffix_tokens.insert(0, token)
            i -= 1
            continue
        break

    if suffix_tokens:
        suffix = clean_text(" ".join(suffix_tokens))
        name_line = clean_text(" ".join(tokens[: i + 1]))
    else:
        # Only treat trailing text as a suffix when it is explicitly comma-delimited.
        # This avoids misclassifying real surnames as suffixes.
        if "," in name_line:
            suffix_match = SUFFIX_PATTERN.search(name_line)
            if suffix_match:
                suffix = clean_text(suffix_match.group(2).replace(" ,", ",").replace(", ", ", "))
                name_line = name_line[:suffix_match.start()].strip()

    return title, name_line, suffix
