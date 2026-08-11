#!/usr/bin/env python3
# app.py
"""
Lambda preprocessing for Bedrock Knowledge Base ingestion into an
Amazon S3 Vectors-backed vector store.

Pipeline: read source docs from FRONTEND_BUCKET -> extract text -> extract
title/published_date -> chunk -> sanitize -> validate -> write chunk +
sidecar metadata files to STAGING_BUCKET -> trigger a Bedrock ingestion job.

=====================================================================
DESIGN NOTES -- read this before changing chunking or metadata logic
=====================================================================

1. S3 VECTORS FILTERABLE METADATA IS A HARD 2048-BYTE TOTAL, PER VECTOR,
   NOT PER FIELD. By default every metadata key on a vector is filterable,
   including two keys Bedrock injects automatically during ingestion:
   AMAZON_BEDROCK_TEXT (the chunk text itself) and AMAZON_BEDROCK_METADATA.
   Which keys are exempt from that 2KB budget is decided ONCE, at S3 Vector
   Index creation, via Terraform's
   `metadata_configuration.non_filterable_metadata_keys` -- and it is
   IMMUTABLE afterward. This Lambda writes exactly one non-filterable key
   (LONG_META_KEY, default "long_meta") and verifies at runtime that the
   real index actually declares it (and ideally the AMAZON_BEDROCK_* keys)
   non-filterable, before uploading or ingesting anything. Your Terraform
   MUST include, at minimum:

     resource "aws_s3vectors_index" "this" {
       ...
       metadata_configuration {
         non_filterable_metadata_keys = [
           "long_meta",
           "AMAZON_BEDROCK_TEXT",
           "AMAZON_BEDROCK_METADATA",
         ]
       }
     }

2. PDFS ARE BINARY. Decoding raw PDF bytes as UTF-8 does not extract text --
   it produces mostly control-character noise that sanitization strips
   down to near-nothing. PDFs are parsed properly here with pdfminer.six
   (attach it as a Lambda layer).

3. CHUNK PACKING RESERVES OVERLAP + SEPARATOR HEADROOM DURING ACCUMULATION,
   not after. Two compounding bugs were found and fixed here:
     a) _CONTROL_RE previously stripped \t and \n as "control characters",
        silently collapsing every multi-paragraph document into a single
        unsplittable line before chunking ever ran.
     b) The overlap step joins `tail + "\n" + content`. If content is
        packed up to exactly (max_bytes - overlap_bytes), then
        tail(overlap_bytes) + "\n"(1 byte) + content is 1 byte OVER
        max_bytes, which the final safety-net split then turns into a real
        chunk plus a stray 1-byte remainder -- on nearly every chunk.
   Both are fixed: _CONTROL_RE preserves \t/\n, and the accumulation budget
   reserves overlap_bytes + 1 (for the separator) up front.

4. TITLE AND PUBLISHED_DATE are extracted per document (once, not per
   chunk) and stored as small filterable metadata:
     - title: frontmatter `title:` field if present, else the first
       markdown H1 (`# ...`) heading, else a humanized version of the
       filename slug.
     - published_date: frontmatter `date:` field (YYYY-MM-DD) if present,
       else the S3 object's LastModified date.
   Any frontmatter block found is stripped from the content before
   chunking, so it doesn't pollute chunk text. Both fields are tiny
   (well under the filterable metadata budget) and exist specifically so
   downstream retrieval can sort/filter by real recency instead of relying
   on vector-similarity ranking or letting the LLM guess.

5. RE-RUNNING INGESTION COSTS REAL EMBEDDING-MODEL MONEY. This file:
   - verifies the real S3 Vectors index config before any upload (fails
     closed, for free, on drift or misconfiguration)
   - validates every chunk's metadata size and content length BEFORE
     upload, quarantining anything that would fail, instead of finding out
     from a paid ingestion job
   - supports VALIDATE_ONLY to dry-run the whole pipeline for free
   - refuses to start a paid ingestion job if anything was quarantined
     (ABORT_ON_ANY_QUARANTINE, default true)
   - cleans up a document's previously-written chunks before writing its
     new set, so a re-chunk that produces fewer/different chunks doesn't
     leave stale ones permanently indexed

IAM permissions this Lambda's execution role needs:
  - s3:GetObject, s3:ListBucket on FRONTEND_BUCKET
  - s3:PutObject, s3:DeleteObject, s3:DeleteObjectTagging, s3:ListBucket on
    STAGING_BUCKET
  - s3vectors:GetIndex on the vector index (for the config self-check)
  - bedrock:StartIngestionJob, bedrock:ListIngestionJobs on the KB/data
    source

IMPORTANT: make sure your aws_bedrockagent_data_source Terraform sets
`inclusion_prefixes` on the S3 data source config to STAGING_PREFIX only
(e.g. ["bedrock-clean/"]). Without it, the data source scans the ENTIRE
staging bucket, which will pick up QUARANTINE_PREFIX debug artifacts (or
anything else in the bucket) as if they were real documents. Keep
QUARANTINE_PREFIX as a sibling of STAGING_PREFIX, not nested under it, as
defense in depth on top of the inclusion_prefixes scoping.
=====================================================================
"""

import os
import re
import io
import json
import uuid
import logging
import hashlib
import unicodedata
from typing import Any, Dict, List, Optional, Tuple, Iterator

import boto3
from botocore.exceptions import ClientError

try:
    from pdfminer.high_level import extract_text as _pdfminer_extract_text
    _PDFMINER_AVAILABLE = True
except ImportError:
    _PDFMINER_AVAILABLE = False

# ---------- Logging ----------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)

# ---------- Required environment ----------
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID")
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID")
REGION = os.environ.get("REGION", os.environ.get("AWS_REGION", "ap-south-1"))
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET")
STAGING_PREFIX = os.environ.get("STAGING_PREFIX", "bedrock-clean")
QUARANTINE_PREFIX = os.environ.get("QUARANTINE_PREFIX", "bedrock-quarantine")

# Must match your aws_s3vectors_index / aws_s3vectors_vector_bucket Terraform resources.
VECTOR_BUCKET_NAME = os.environ.get("VECTOR_BUCKET_NAME")
VECTOR_INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME")

# ---------- Chunking ----------
MAX_CHUNK_BYTES = int(os.environ.get("MAX_CHUNK_BYTES", "1800"))
CHUNK_OVERLAP_BYTES = int(os.environ.get("CHUNK_OVERLAP_BYTES", "200"))

# 0 or unset = unlimited. Set this (with VALIDATE_ONLY=true) to smoke-test
# a handful of chunks for free before running the whole corpus.
VALIDATION_SAMPLE_LIMIT = int(os.environ.get("VALIDATION_SAMPLE_LIMIT", "0"))

# ---------- Metadata safety budgets (margin under documented S3 Vectors caps) ----------
FILTERABLE_METADATA_SAFE_BYTES = int(os.environ.get("FILTERABLE_METADATA_SAFE_BYTES", "1536"))
NONFILTERABLE_METADATA_SAFE_BYTES = int(os.environ.get("NONFILTERABLE_METADATA_SAFE_BYTES", "8000"))
SIDECAR_FILE_MAX_BYTES = int(os.environ.get("SIDECAR_FILE_MAX_BYTES", "9500"))  # Bedrock's sidecar file cap is 10KB
EXCERPT_MAX_BYTES = int(os.environ.get("EXCERPT_MAX_BYTES", "300"))
TITLE_MAX_BYTES = int(os.environ.get("TITLE_MAX_BYTES", "200"))

# A chunk/document with fewer than this many non-whitespace characters after
# normalization is almost certainly a failed extraction, not real content.
MIN_CONTENT_CHARS = int(os.environ.get("MIN_CONTENT_CHARS", "20"))

# The one non-filterable key this Lambda writes -- MUST be declared in the
# index's non_filterable_metadata_keys (verified at runtime, not assumed).
LONG_META_KEY = os.environ.get("LONG_META_KEY", "long_meta")
REQUIRED_NON_FILTERABLE_KEYS = {LONG_META_KEY}
BEDROCK_RESERVED_KEYS = {"AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_EMBEDDING"}

# ---------- Run mode ----------
VALIDATE_ONLY = os.environ.get("VALIDATE_ONLY", "false").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true" or VALIDATE_ONLY
ABORT_ON_ANY_QUARANTINE = os.environ.get("ABORT_ON_ANY_QUARANTINE", "true").lower() == "true"
CLEANUP_STALE_CHUNKS = os.environ.get("CLEANUP_STALE_CHUNKS", "true").lower() == "true"

# ---------- AWS clients ----------
s3 = boto3.client("s3")
bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)
s3vectors = boto3.client("s3vectors", region_name=REGION)  # requires a boto3 version that supports this service

# ---------- Sanitizers ----------
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF" "\U0001F600-\U0001F64F" "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F" "\U0001F780-\U0001F7FF" "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF" "\U0001FA00-\U0001FA6F"
    "\U00002700-\U000027BF" "\U00002600-\U000026FF"
    "]+", flags=re.UNICODE
)
# Strips genuine control characters, but explicitly excludes \t (0x09) and
# \n (0x0A) -- those are legitimate structural whitespace, not noise.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]+")
_ALLOWED_RE = re.compile(r"[^A-Za-z0-9 \t\n\.,;:!?\-—()\"'/%\[\]{}@#&+*=<>|`~]+")

# ---------- Title / date extraction ----------
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
_FRONTMATTER_DATE_RE = re.compile(r"(?im)^date\s*:\s*['\"]?(\d{4}-\d{2}-\d{2})['\"]?\s*$")
_FRONTMATTER_TITLE_RE = re.compile(r"(?im)^title\s*:\s*['\"]?([^'\"\n]+?)['\"]?\s*$")
_H1_RE = re.compile(r"(?m)^#\s+(.+)$")


def _short_hash(s: str, length: int = 12) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:length]


def _truncate_to_bytes(s: str, max_bytes: int) -> str:
    if s is None:
        return ""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"!\[.*?\]\(.*?\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
    s = _EMOJI_RE.sub("", s)
    s = _CONTROL_RE.sub(" ", s)
    s = _ALLOWED_RE.sub(" ", s)
    s = re.sub(r"https?://\S+", "[url]", s)
    # Collapse horizontal whitespace only -- preserve newlines so
    # semantic_chunks() has real paragraph/line boundaries to split on.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = s.strip()
    return s


def sanitize_for_metadata(value: Any, max_bytes: int) -> str:
    if value is None:
        return ""
    s = str(value)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"```.*?```", " ", s, flags=re.DOTALL)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"!\[.*?\]\(.*?\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", s)
    s = _EMOJI_RE.sub("", s)
    s = _CONTROL_RE.sub(" ", s)
    s = re.sub(r"https?://\S+", "[url]", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _truncate_to_bytes(s, max_bytes)


def extract_title_and_date(key: str, raw_text: str, last_modified) -> Tuple[str, str, str, bool]:
    """
    Returns (title, published_date, content_with_frontmatter_stripped, date_from_frontmatter).

    title: frontmatter `title:` -> first markdown H1 heading -> humanized
    filename slug (in that priority order).
    published_date: frontmatter `date:` (YYYY-MM-DD) -> S3 object's
    LastModified date (in that priority order). Empty string if neither
    is available. date_from_frontmatter is False when the LastModified
    fallback was used -- worth tracking since LastModified is only a
    reliable proxy for publish date if your deploy pipeline doesn't
    re-touch every file on every deploy.
    """
    text = raw_text
    frontmatter_date = None
    frontmatter_title = None

    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        fm_block = fm_match.group(1)
        date_match = _FRONTMATTER_DATE_RE.search(fm_block)
        if date_match:
            frontmatter_date = date_match.group(1)
        title_match = _FRONTMATTER_TITLE_RE.search(fm_block)
        if title_match:
            frontmatter_title = title_match.group(1).strip()
        text = text[fm_match.end():]  # strip frontmatter out of the content that gets chunked

    title = frontmatter_title
    if not title:
        h1_match = _H1_RE.search(text)
        if h1_match:
            title = h1_match.group(1).strip()
    if not title:
        base = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        title = re.sub(r"[-_]+", " ", base).strip().title()

    published_date = frontmatter_date
    if not published_date and last_modified is not None:
        try:
            published_date = last_modified.date().isoformat()
            logger.warning(
                "%s has no frontmatter 'date:' field -- falling back to S3 LastModified (%s). "
                "If your deploy pipeline re-uploads/syncs the whole folder on every deploy, this "
                "date reflects last-deploy-time, not true publish date, and recency sorting on it "
                "will be unreliable. Add an explicit 'date: YYYY-MM-DD' frontmatter field to this "
                "file for an accurate date.",
                key, published_date,
            )
        except Exception:
            published_date = ""
    if not published_date:
        published_date = ""

    return (
        sanitize_for_metadata(title, TITLE_MAX_BYTES),
        sanitize_for_metadata(published_date, 20),
        text,
        frontmatter_date is not None,
    )


# ---------- Text extraction ----------
def extract_text_from_source(key: str, body: bytes) -> str:
    """PDFs need real parsing; everything else is treated as plain text."""
    if key.lower().endswith(".pdf"):
        if not _PDFMINER_AVAILABLE:
            logger.error("pdfminer.six not importable -- attach it as a Lambda layer. Falling back to raw decode for %s.", key)
            return body.decode("utf-8", errors="replace")
        try:
            return _pdfminer_extract_text(io.BytesIO(body)) or ""
        except Exception:
            logger.exception("pdfminer failed to parse %s; treating as empty", key)
            return ""
    return body.decode("utf-8", errors="replace")


# ---------- Chunking ----------
def split_by_bytes(text: str, max_bytes: int, reserve: int = 0) -> List[str]:
    effective_max = max(1, max_bytes - reserve)
    b = text.encode("utf-8")
    parts: List[str] = []
    start = 0
    while start < len(b):
        end = min(start + effective_max, len(b))
        parts.append(b[start:end].decode("utf-8", errors="ignore"))
        start = end
    return parts


def semantic_chunks(text: str, max_bytes: int, overlap_bytes: int) -> List[str]:
    """
    Packs paragraphs up to (max_bytes - overlap_bytes - 1), then prepends
    an overlap tail from the previous chunk joined by "\n". The -1 reserves
    room for that joining newline -- without it, a full-size content chunk
    plus a full tail plus the separator is exactly 1 byte over max_bytes,
    which the final safety-net split then turns into a real chunk plus a
    stray 1-byte remainder on nearly every chunk. Confirmed via
    reproduction; do not remove the -1.
    """
    if not text:
        return []
    effective_max = max(1, max_bytes - overlap_bytes - (1 if overlap_bytes > 0 else 0))
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: List[str] = []
    cur: List[str] = []
    cur_bytes = 0

    def flush():
        nonlocal cur, cur_bytes
        if cur:
            combined = "\n".join(cur)
            if len(combined.encode("utf-8")) <= effective_max:
                chunks.append(combined)
            else:
                chunks.extend(split_by_bytes(combined, effective_max, reserve=0))
            cur = []
            cur_bytes = 0

    for p in paragraphs:
        p_bytes = len(p.encode("utf-8"))
        if p_bytes > effective_max:
            flush()
            chunks.extend(split_by_bytes(p, effective_max, reserve=0))
            continue
        if cur_bytes + p_bytes + 1 > effective_max:
            flush()
        cur.append(p)
        cur_bytes += p_bytes + 1
    flush()

    if overlap_bytes > 0 and len(chunks) > 1:
        overlapped: List[str] = []
        for i, c in enumerate(chunks):
            if i == 0:
                overlapped.append(c)
                continue
            prev = overlapped[-1]
            tail = prev.encode("utf-8")[-overlap_bytes:].decode("utf-8", errors="ignore")
            overlapped.append(tail + "\n" + c)
        chunks = overlapped

    out: List[str] = []
    for c in chunks:
        if len(c.encode("utf-8")) <= max_bytes:
            out.append(c)
        else:
            out.extend(split_by_bytes(c, max_bytes, reserve=0))
    return out


# =====================================================================
# INDEX SELF-CHECK -- run before touching S3 or Bedrock, every invocation.
# =====================================================================
def verify_index_metadata_config() -> Tuple[bool, str, set]:
    if not VECTOR_BUCKET_NAME or not VECTOR_INDEX_NAME:
        return False, "VECTOR_BUCKET_NAME / VECTOR_INDEX_NAME env vars not set; cannot verify index config", set()
    try:
        resp = s3vectors.get_index(vectorBucketName=VECTOR_BUCKET_NAME, indexName=VECTOR_INDEX_NAME)
    except ClientError as e:
        return False, f"get_index call failed: {e}", set()
    except Exception as e:
        return False, f"Unexpected error calling get_index (check boto3 version supports s3vectors): {e}", set()

    metadata_cfg = resp.get("index", resp).get("metadataConfiguration", {}) or {}
    actual_non_filterable = set(metadata_cfg.get("nonFilterableMetadataKeys", []) or [])

    missing = REQUIRED_NON_FILTERABLE_KEYS - actual_non_filterable
    if missing:
        return False, (
            f"Index '{VECTOR_INDEX_NAME}' does NOT declare {sorted(missing)} as non-filterable. "
            f"Filterable metadata is capped at 2048 bytes TOTAL and this is immutable after index "
            f"creation -- fix Terraform and recreate the index, or every chunk will fail."
        ), actual_non_filterable

    bedrock_missing = BEDROCK_RESERVED_KEYS - actual_non_filterable
    if bedrock_missing:
        logger.warning(
            "Index does not declare Bedrock-reserved keys %s as non-filterable. Bedrock injects "
            "these on every chunk during ingestion; ingestion will fail with the same 2048-byte "
            "error even though this Lambda's own metadata is fine.",
            sorted(bedrock_missing),
        )

    return True, "Index metadata configuration OK", actual_non_filterable


# ---------- Metadata builders ----------
def build_filterable_attrs(
    source_type: str,
    doc_id: str,
    chunk_index: int,
    total_chunks: int,
    content_hash: str,
    title: str = "",
    published_date: str = "",
) -> Dict[str, str]:
    attrs = {
        "source_type": sanitize_for_metadata(source_type, 64),
        "doc_id": sanitize_for_metadata(doc_id, 200),
        "chunk_index": str(chunk_index),
        "total_chunks": str(total_chunks),
        "content_hash": content_hash,
        "title": sanitize_for_metadata(title, TITLE_MAX_BYTES),
        "published_date": sanitize_for_metadata(published_date, 20),
    }
    for k in list(attrs.keys()):
        if k in BEDROCK_RESERVED_KEYS:
            attrs.pop(k)
    return attrs


def build_nonfilterable_attrs(doc_id: str, source_s3_key: str, excerpt_source_text: str) -> Dict[str, str]:
    """
    Packs small auxiliary context into ONE non-filterable key. Deliberately
    does not duplicate the full chunk text -- Bedrock's own
    AMAZON_BEDROCK_TEXT already carries that once it's declared
    non-filterable, so writing it twice would only burn metadata budget.
    """
    payload = {
        "doc_id": doc_id,
        "original_s3_key": source_s3_key,
        "excerpt": sanitize_for_metadata(excerpt_source_text[:EXCERPT_MAX_BYTES * 2], EXCERPT_MAX_BYTES),
    }
    return {LONG_META_KEY: json.dumps(payload, ensure_ascii=False)}


def _attrs_byte_size(attrs: Dict[str, str]) -> int:
    return sum(len(k.encode("utf-8")) + len(str(v).encode("utf-8")) for k, v in attrs.items())


def validate_chunk_metadata(filterable_attrs: Dict[str, str], nonfilterable_attrs: Dict[str, str], chunk_text: str = "") -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    if len(chunk_text.strip()) < MIN_CONTENT_CHARS:
        reasons.append(f"chunk text has only {len(chunk_text.strip())} non-whitespace chars (min {MIN_CONTENT_CHARS}) -- likely a failed extraction")

    for k in filterable_attrs:
        if k in REQUIRED_NON_FILTERABLE_KEYS or k in BEDROCK_RESERVED_KEYS:
            reasons.append(f"key '{k}' should not be in filterable_attrs")

    filterable_size = _attrs_byte_size(filterable_attrs)
    if filterable_size > FILTERABLE_METADATA_SAFE_BYTES:
        reasons.append(f"filterable metadata {filterable_size}B exceeds safe budget {FILTERABLE_METADATA_SAFE_BYTES}B")

    nonfilterable_size = _attrs_byte_size(nonfilterable_attrs)
    if nonfilterable_size > NONFILTERABLE_METADATA_SAFE_BYTES:
        reasons.append(f"non-filterable metadata {nonfilterable_size}B exceeds safe budget {NONFILTERABLE_METADATA_SAFE_BYTES}B")

    sidecar_size = len(json.dumps({"metadataAttributes": {**filterable_attrs, **nonfilterable_attrs}}, ensure_ascii=False).encode("utf-8"))
    if sidecar_size > SIDECAR_FILE_MAX_BYTES:
        reasons.append(f"sidecar JSON {sidecar_size}B exceeds safe budget {SIDECAR_FILE_MAX_BYTES}B (10KB hard limit)")

    return (len(reasons) == 0), reasons


# ---------- S3 helpers ----------
def upload_text_to_s3(bucket: str, key: str, body: str, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info("[dry-run] upload s3://%s/%s (%d bytes)", bucket, key, len(body.encode("utf-8")))
        return True
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="text/plain", Metadata={})
        try:
            s3.delete_object_tagging(Bucket=bucket, Key=key)
        except Exception:
            pass
        return True
    except ClientError as e:
        logger.exception("Failed to upload chunk s3://%s/%s: %s", bucket, key, e)
        return False


def write_sidecar(bucket: str, chunk_key: str, filterable_attrs: Dict[str, str], nonfilterable_attrs: Dict[str, str], dry_run: bool = False) -> bool:
    sidecar_key = f"{chunk_key}.metadata.json"
    body = json.dumps({"metadataAttributes": {**filterable_attrs, **nonfilterable_attrs}}, ensure_ascii=False).encode("utf-8")
    if dry_run:
        logger.info("[dry-run] would write sidecar s3://%s/%s (%d bytes)", bucket, sidecar_key, len(body))
        return True
    try:
        s3.put_object(Bucket=bucket, Key=sidecar_key, Body=body, ContentType="application/json", Metadata={})
        return True
    except ClientError as e:
        logger.exception("Failed to write sidecar s3://%s/%s: %s", bucket, sidecar_key, e)
        return False


def write_quarantine(bucket: str, chunk_key: str, reasons: List[str], filterable_attrs: Dict[str, str], nonfilterable_attrs: Dict[str, str], chunk_text: str = "", dry_run: bool = False) -> str:
    quarantine_key = f"{chunk_key}.quarantine.json".replace(STAGING_PREFIX, QUARANTINE_PREFIX, 1)
    audit = {
        "chunk_key": chunk_key,
        "reasons": reasons,
        "chunk_text_length_chars": len(chunk_text),
        "chunk_text_length_bytes": len(chunk_text.encode("utf-8")),
        "chunk_text_repr": repr(chunk_text),
        "filterable_attrs_sizes": {k: len(str(v).encode("utf-8")) for k, v in filterable_attrs.items()},
        "nonfilterable_attrs_sizes": {k: len(str(v).encode("utf-8")) for k, v in nonfilterable_attrs.items()},
        "id": str(uuid.uuid4()),
    }
    if dry_run:
        logger.warning("[dry-run] would quarantine %s: %s", chunk_key, reasons)
        return quarantine_key
    try:
        s3.put_object(Bucket=bucket, Key=quarantine_key, Body=json.dumps(audit, indent=2).encode("utf-8"),
                      ContentType="application/json", Metadata={"quarantine": "true"})
        logger.warning("Quarantined %s: %s", chunk_key, reasons)
    except Exception:
        logger.exception("Failed to write quarantine audit for %s", chunk_key)
    return quarantine_key


def cleanup_stale_chunks_for_doc(bucket: str, staging_prefix: str, source_type: str, slug_prefix: str, dry_run: bool = False) -> int:
    """Deletes a document's previously-written chunk/sidecar objects before writing its new set."""
    prefix = f"{staging_prefix}/{source_type}/{slug_prefix}_chunk"
    keys = [obj["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
            for obj in page.get("Contents", [])]
    if not keys:
        return 0
    if dry_run:
        logger.info("[dry-run] would delete %d stale object(s) under s3://%s/%s*", len(keys), bucket, prefix)
        return len(keys)
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        try:
            resp = s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]})
            deleted += len(resp.get("Deleted", []))
            for err in resp.get("Errors", []):
                logger.error("Failed to delete stale object %s: %s", err.get("Key"), err.get("Message"))
        except ClientError:
            logger.exception("delete_objects failed for a batch under prefix %s", prefix)
    return deleted


def iter_s3_source_objects(bucket: str, prefix: str, extensions: Tuple[str, ...]) -> Iterator[Tuple[str, bytes, Any]]:
    """Yields (key, body, last_modified). last_modified comes free from list_objects_v2 -- no extra API call."""
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(extensions):
                try:
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    yield key, body, obj.get("LastModified")
                except ClientError as e:
                    logger.exception("Failed to read s3://%s/%s: %s", bucket, key, e)


# ---------- Main processing ----------
# Adjust these prefixes/source types to match your own site structure.
SOURCE_TYPE_BY_PREFIX = {
    "blog/posts/": "blog",
    "projects/project/": "project",
    "resume/": "resume",
}


class ChunkResult:
    def __init__(self, chunk_key: str, sidecar_written: bool, quarantined: bool, byte_size: int):
        self.chunk_key = chunk_key
        self.sidecar_written = sidecar_written
        self.quarantined = quarantined
        self.byte_size = byte_size


def process_s3_source_and_upload(
    source_bucket: str,
    source_prefix: str,
    staging_bucket: str,
    staging_prefix: str,
    max_bytes: int,
    source_type: str,
    overlap_bytes: int = 0,
    dry_run: bool = False,
    sample_limit_remaining: Optional[int] = None,
) -> Tuple[List[ChunkResult], Optional[int], List[Tuple[str, bool]]]:
    results: List[ChunkResult] = []
    date_source_tracking: List[Tuple[str, bool]] = []  # (source_key, date_from_frontmatter) -- one entry per document
    exts = (".md", ".markdown", ".html", ".htm", ".txt", ".pdf")

    for key, body, last_modified in iter_s3_source_objects(source_bucket, source_prefix, exts):
        if sample_limit_remaining is not None and sample_limit_remaining <= 0:
            break

        raw = extract_text_from_source(key, body)
        title, published_date, raw, date_from_frontmatter = extract_title_and_date(key, raw, last_modified)
        date_source_tracking.append((key, date_from_frontmatter))
        normalized = normalize_text(raw)
        if len(normalized.strip()) < MIN_CONTENT_CHARS:
            logger.warning("Skipping %s: only %d chars after normalization -- likely a failed extraction.", key, len(normalized.strip()))
            continue

        chunks = semantic_chunks(normalized, max_bytes=max_bytes, overlap_bytes=overlap_bytes)
        if not chunks:
            continue

        logger.info(
            "CHUNK SIZES for %s: title=%r published_date=%s total=%d sizes=%s",
            key, title, published_date, len(chunks), [len(c.encode("utf-8")) for c in chunks],
        )

        raw_base = key.replace("/", "_").rsplit(".", 1)[0]
        slug_prefix = raw_base[:100]
        doc_hash = hashlib.sha256(raw_base.encode("utf-8")).hexdigest()[:12]
        doc_id = f"{slug_prefix}--{doc_hash}"
        total_chunks = len(chunks)

        if CLEANUP_STALE_CHUNKS:
            cleanup_stale_chunks_for_doc(staging_bucket, staging_prefix, source_type, slug_prefix, dry_run=dry_run)

        for i, c in enumerate(chunks, start=1):
            if sample_limit_remaining is not None and sample_limit_remaining <= 0:
                break

            dest_key = f"{staging_prefix}/{source_type}/{slug_prefix}_chunk{i}.txt"
            filterable_attrs = build_filterable_attrs(source_type, doc_id, i, total_chunks, doc_hash, title, published_date)
            nonfilterable_attrs = build_nonfilterable_attrs(doc_id, key, c)

            ok, reasons = validate_chunk_metadata(filterable_attrs, nonfilterable_attrs, chunk_text=c)
            if not ok:
                write_quarantine(staging_bucket, dest_key, reasons, filterable_attrs, nonfilterable_attrs, chunk_text=c, dry_run=dry_run)
                logger.error(
                    "QUARANTINE CONTENT for %s (chunk %d/%d, %d bytes): %s",
                    dest_key, i, total_chunks, len(c.encode("utf-8")), repr(c[:300]),
                )
                results.append(ChunkResult(dest_key, False, True, len(c.encode("utf-8"))))
            else:
                uploaded_ok = upload_text_to_s3(staging_bucket, dest_key, c, dry_run=dry_run)
                if uploaded_ok:
                    sidecar_ok = write_sidecar(staging_bucket, dest_key, filterable_attrs, nonfilterable_attrs, dry_run=dry_run)
                    results.append(ChunkResult(dest_key, sidecar_ok, not sidecar_ok, len(c.encode("utf-8"))))
                else:
                    results.append(ChunkResult(dest_key, False, True, len(c.encode("utf-8"))))

            if sample_limit_remaining is not None:
                sample_limit_remaining -= 1

    return results, sample_limit_remaining, date_source_tracking


# ---------- Bedrock ingestion helpers ----------
def _is_ingestion_in_progress() -> Tuple[bool, Optional[str]]:
    try:
        resp = bedrock_agent.list_ingestion_jobs(knowledgeBaseId=KNOWLEDGE_BASE_ID, dataSourceId=DATA_SOURCE_ID, maxResults=10)
        for job in resp.get("ingestionJobSummaries", []):
            if job.get("status") in {"STARTING", "IN_PROGRESS"}:
                return True, job.get("ingestionJobId")
    except ClientError as e:
        logger.exception("Error listing ingestion jobs: %s", e)
    return False, None


def _start_ingestion_job() -> dict:
    client_token = f"ingest-{uuid.uuid4()}"
    try:
        resp = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID, dataSourceId=DATA_SOURCE_ID,
            clientToken=client_token, description=f"Triggered by Lambda at {client_token}",
        )
        return resp.get("ingestionJob", {})
    except ClientError as e:
        logger.exception("Failed to start ingestion job: %s", e)
        return {}


# ---------- Lambda handler ----------
def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code, "body": json.dumps(body, default=str)}


def handler(event, context):
    logger.info("Starting ingestion pipeline | KB=%s | DS=%s | VALIDATE_ONLY=%s | DRY_RUN=%s",
                KNOWLEDGE_BASE_ID, DATA_SOURCE_ID, VALIDATE_ONLY, DRY_RUN)

    missing = [n for n in ("FRONTEND_BUCKET", "STAGING_BUCKET", "KNOWLEDGE_BASE_ID", "DATA_SOURCE_ID") if not globals().get(n)]
    if missing:
        return _response(500, {"status": "error", "reason": "missing_env", "missing": missing})

    index_ok, index_msg, actual_non_filterable = verify_index_metadata_config()
    if not index_ok:
        logger.error("Index config check failed: %s", index_msg)
        return _response(500, {"status": "error", "reason": "index_metadata_config_invalid", "detail": index_msg})
    logger.info("Index config check passed. Non-filterable keys: %s", sorted(actual_non_filterable))

    if not _PDFMINER_AVAILABLE:
        logger.warning("pdfminer.six not importable -- PDFs will fall back to raw decode and likely produce empty content.")

    in_progress, active_job_id = _is_ingestion_in_progress()
    if in_progress:
        return _response(200, {"status": "skipped", "reason": "ingestion_in_progress", "activeJobId": active_job_id})

    try:
        all_results: List[ChunkResult] = []
        all_date_tracking: List[Tuple[str, bool]] = []
        sample_remaining = VALIDATION_SAMPLE_LIMIT if VALIDATION_SAMPLE_LIMIT > 0 else None
        for source_prefix, source_type in SOURCE_TYPE_BY_PREFIX.items():
            if sample_remaining is not None and sample_remaining <= 0:
                break
            results, sample_remaining, date_tracking = process_s3_source_and_upload(
                FRONTEND_BUCKET, source_prefix, STAGING_BUCKET, STAGING_PREFIX, MAX_CHUNK_BYTES,
                source_type=source_type, overlap_bytes=CHUNK_OVERLAP_BYTES, dry_run=DRY_RUN,
                sample_limit_remaining=sample_remaining,
            )
            all_results.extend(results)
            all_date_tracking.extend(date_tracking)
    except Exception as e:
        logger.exception("Preprocessing failed: %s", e)
        return _response(500, {"status": "error", "reason": "preprocessing_failed", "error": str(e)})

    quarantined = [r for r in all_results if r.quarantined]
    docs_with_frontmatter_date = [k for k, from_fm in all_date_tracking if from_fm]
    docs_with_fallback_date = [k for k, from_fm in all_date_tracking if not from_fm]
    summary = {
        "total_chunks": len(all_results),
        "ok_chunks": len(all_results) - len(quarantined),
        "quarantined_chunks": len(quarantined),
        "quarantined_keys": [r.chunk_key for r in quarantined][:50],
        "docs_with_frontmatter_date": len(docs_with_frontmatter_date),
        "docs_with_last_modified_fallback_date": len(docs_with_fallback_date),
        "docs_needing_frontmatter_date": docs_with_fallback_date[:50],
    }

    if VALIDATE_ONLY:
        return _response(200, {"status": "validated", **summary})

    if quarantined and ABORT_ON_ANY_QUARANTINE:
        logger.error("Aborting before ingestion job: %s", summary)
        return _response(500, {"status": "error", "reason": "chunks_quarantined", **summary})

    if not DRY_RUN:
        _start_ingestion_job()

    return _response(200, {"status": "ok", "dry_run": DRY_RUN, **summary})


if __name__ == "__main__":
    logger.setLevel(logging.DEBUG)
    print("Local debug run (dry-run)")
    ok, msg, keys = verify_index_metadata_config()
    print("INDEX CHECK:", ok, msg, keys)
    res, _remaining, _date_tracking = process_s3_source_and_upload(
        FRONTEND_BUCKET or "your-frontend-bucket", "blog/posts/",
        STAGING_BUCKET or "your-staging-bucket", STAGING_PREFIX, MAX_CHUNK_BYTES,
        source_type="blog", overlap_bytes=CHUNK_OVERLAP_BYTES, dry_run=True,
    )
    for r in res[:20]:
        print("RESULT:", r.chunk_key, r.sidecar_written, r.quarantined, r.byte_size)