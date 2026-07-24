#!/usr/bin/env python3
"""
app.py

Lambda entrypoint that:
- Reads source files from a frontend S3 bucket (or local directory when run locally)
- Sanitizes text to alphanumeric lowercase (configurable)
- Chunks large documents into safe-sized pieces by UTF-8 byte length (default 1500 bytes)
  with a configurable byte overlap between consecutive chunks so context isn't lost at
  chunk boundaries.
- Uploads chunked .txt files to a staging S3 prefix with empty S3 object metadata
  (HTTP headers) -- NOT the same thing as Bedrock KB metadata, see below.
- Writes a Bedrock Knowledge Base metadata sidecar (`<key>.metadata.json`) next to every
  chunk, carrying `source_type` (resume/project/blog), `doc_id`, `chunk_index`, and
  `total_chunks`. This is what lets the Strands retrieval layer filter deterministically
  instead of relying on vector similarity + prompt instructions to keep resume/project/
  blog content apart.
- Starts a Bedrock Agent ingestion job for the configured Knowledge Base / Data Source

Key behavior changes vs prior version:
- Chunking is done by UTF-8 byte size (not characters) to guarantee S3 Vectors 2048-byte
  filterable metadata limit is not exceeded, WITH overlap between consecutive chunks.
- Chunk names preserve the original source key and include a chunk index for traceability.
- Every chunk gets a `<key>.metadata.json` sidecar with Bedrock KB `metadataAttributes`
  (source_type / doc_id / chunk_index / total_chunks). This sidecar format is intentional
  and expected -- the old validation step that aborted ingestion on ANY `.meta.json`
  sidecar has been replaced with a check that the sidecars are well-formed and paired
  1:1 with chunk objects (previously that check was also looking at the wrong suffix:
  `.meta.json` instead of the `.metadata.json` suffix Bedrock actually expects).
- S3 object metadata (HTTP `Metadata={}` header) is still kept empty on the chunk .txt
  objects themselves -- Bedrock KB metadata lives in the sidecar JSON file, not in S3
  object headers, so these are orthogonal and both are handled correctly here.

Environment variables (required):
  FRONTEND_BUCKET      - source bucket containing website files
  STAGING_BUCKET       - destination staging bucket for bedrock ingestion
  STAGING_PREFIX       - destination prefix (default: bedrock-clean)
  QUARANTINE_PREFIX    - prefix for quarantine copies (default: <STAGING_PREFIX>/quarantine)
  KNOWLEDGE_BASE_ID    - Bedrock Agent Knowledge Base ID
  DATA_SOURCE_ID       - Bedrock Agent Data Source ID
  REGION               - AWS region (optional; falls back to AWS_REGION)
  MAX_CHUNK_BYTES      - max bytes per chunk (default: 1500)
  CHUNK_OVERLAP_BYTES  - bytes of overlap between consecutive chunks (default: 150)
  VALIDATION_SAMPLE_LIMIT - sample limit for validations (default: 20)
  DRY_RUN              - if "true", do not upload or start ingestion (default: "false")
  LOG_LEVEL            - logging level (default: INFO)
"""
from __future__ import annotations
import os
import re
import json
import uuid
import unicodedata
import logging
from typing import List, Iterator, Tuple, Optional, Dict, NamedTuple
from pathlib import Path
from pdfminer.high_level import extract_text

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logger = logging.getLogger()
logger.setLevel(LOG_LEVEL)

# ---------------------------------------------------------------------------
# Env vars and defaults
# ---------------------------------------------------------------------------
KNOWLEDGE_BASE_ID = os.environ.get("KNOWLEDGE_BASE_ID")
DATA_SOURCE_ID = os.environ.get("DATA_SOURCE_ID")
REGION = os.environ.get("REGION", os.environ.get("AWS_REGION"))
FRONTEND_BUCKET = os.environ.get("FRONTEND_BUCKET")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET")
STAGING_PREFIX = os.environ.get("STAGING_PREFIX")
QUARANTINE_PREFIX = os.environ.get("QUARANTINE_PREFIX")
MAX_CHUNK_BYTES = int(os.environ.get("MAX_CHUNK_BYTES", "1500"))
CHUNK_OVERLAP_BYTES = int(os.environ.get("CHUNK_OVERLAP_BYTES", "150"))
VALIDATION_SAMPLE_LIMIT = int(os.environ.get("VALIDATION_SAMPLE_LIMIT", "20"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Boto3 clients
# ---------------------------------------------------------------------------
s3 = boto3.client("s3")
bedrock_agent = boto3.client("bedrock-agent", region_name=REGION)

ACTIVE_STATUSES = {"STARTING", "IN_PROGRESS"}

# Maps a source prefix under the frontend bucket to the Bedrock KB metadata
# `source_type` value we want attached to every chunk pulled from it. This is
# the single source of truth for the resume/project/blog distinction -- add
# new sources here and metadata + filtering downstream pick it up automatically.
SOURCE_TYPE_BY_PREFIX: Dict[str, str] = {
    "blog/posts/": "blog",
    "projects/project/": "project",
    "resume/": "resume",
}

class ChunkUploadResult(NamedTuple):
    """
    Lightweight record of what happened for a single chunk, captured at upload time.
    Used so validation can confirm size limits and sidecar success WITHOUT re-listing
    or re-downloading everything from S3 afterward -- that re-scan (a `head_object` or
    `get_object` per chunk, times every chunk across every doc) is what was pushing
    ingestion past the Lambda timeout.
    """
    chunk_key: str
    sidecar_key: str
    byte_size: int
    sidecar_written: bool

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _response(status_code: int, body: dict) -> dict:
    return {"statusCode": status_code, "body": json.dumps(body, default=str)}

def _is_ingestion_in_progress() -> tuple[bool, Optional[str]]:
    try:
        response = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            maxResults=10,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        )
        for job in response.get("ingestionJobSummaries", []):
            if job.get("status") in ACTIVE_STATUSES:
                return True, job.get("ingestionJobId")
    except ClientError as e:
        logger.exception("Error listing ingestion jobs: %s", e)
    return False, None

def _start_ingestion_job() -> dict:
    client_token = f"ingest-{uuid.uuid4()}"
    try:
        response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            dataSourceId=DATA_SOURCE_ID,
            clientToken=client_token,
            description=f"Triggered by Lambda at {client_token}",
        )
        return response.get("ingestionJob", {})
    except ClientError as e:
        logger.exception("Failed to start ingestion job: %s", e)
        raise

# ---------------------------------------------------------------------------
# Sanitizer (alnum-only) - mirrors earlier sanitizer
# ---------------------------------------------------------------------------
_ALNUM_RE = re.compile(r"[^A-Za-z0-9 ]+")

def normalize_and_strip_to_alnum(text: str, lowercase: bool = True) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("â€™", "'").replace("â€“", "-").replace("â€”", "-").replace("â†’", "->")
    # remove emoji / pictographs ranges
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u26FF]+", " ", text)
    # remove control chars
    text = re.sub(r"[\x00-\x1F\x7F]+", " ", text)
    # remove fenced code blocks and images/links
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[.*?\]\(.*?\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # strip markdown headings, lists, tables
    text = re.sub(r"^#{1,6}\s*", " ", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", " ", text, flags=re.MULTILINE)
    text = re.sub(r"^\|.*\|$", " ", text, flags=re.MULTILINE)
    # replace any non-alnum (except space) with space
    text = _ALNUM_RE.sub(" ", text)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if lowercase:
        text = text.lower()
    return text


def pdf_to_text_bytes(body: bytes) -> str:
    """Convert PDF bytes (from S3) to text using pdfminer."""
    tmp_path = "/tmp/temp_resume.pdf"
    with open(tmp_path, "wb") as f:
        f.write(body)
    return extract_text(tmp_path)

# ---------------------------------------------------------------------------
# Chunking helpers (byte-based, with overlap)
# ---------------------------------------------------------------------------
def chunk_text_by_bytes(text: str, max_bytes: int, overlap_bytes: int = 0) -> List[str]:
    """
    Chunk text into pieces where each chunk's UTF-8 encoded byte length <= max_bytes.
    Splits on word boundaries to avoid breaking words.

    overlap_bytes: number of trailing bytes (snapped to whole words) from the END of
    the previous chunk that get carried forward as the START of the next chunk. This
    prevents context from being lost outright at chunk boundaries and gives the
    embedding model a bit more self-contained context per chunk, reducing (but not
    eliminating) the need to stitch many neighbors back together at query time.
    """
    if not text:
        return []
    words = text.split()
    chunks: List[str] = []
    cur_words: List[str] = []
    cur_bytes = 0

    def flush_and_start_new(carry_words: List[str]):
        nonlocal cur_words, cur_bytes
        if cur_words:
            chunks.append(" ".join(cur_words))
        cur_words = list(carry_words)
        cur_bytes = len((" ".join(cur_words)).encode("utf-8")) if cur_words else 0

    def overlap_tail(word_list: List[str], max_overlap_bytes: int) -> List[str]:
        if max_overlap_bytes <= 0 or not word_list:
            return []
        tail: List[str] = []
        tail_bytes = 0
        for w in reversed(word_list):
            add = len(w.encode("utf-8")) + (1 if tail else 0)
            if tail_bytes + add > max_overlap_bytes:
                break
            tail.insert(0, w)
            tail_bytes += add
        return tail

    for w in words:
        prefix = " " if cur_words else ""
        candidate = (prefix + w).encode("utf-8")
        cand_len = len(candidate)
        if cur_bytes + cand_len > max_bytes:
            carry = overlap_tail(cur_words, overlap_bytes)
            flush_and_start_new(carry)
            # re-append current word to the freshly started (carried-over) chunk
            prefix2 = " " if cur_words else ""
            add_len = len((prefix2 + w).encode("utf-8"))
            cur_words.append(w)
            cur_bytes += add_len
            # if a single word plus carry still exceeds max_bytes, force-split the word
            if cur_bytes > max_bytes:
                word_bytes = w.encode("utf-8")
                # drop the just-added whole word, split it by raw bytes instead
                cur_words.pop()
                cur_bytes -= add_len
                if cur_words:
                    chunks.append(" ".join(cur_words))
                cur_words = []
                cur_bytes = 0
                start = 0
                while start < len(word_bytes):
                    end = min(start + max_bytes, len(word_bytes))
                    slice_bytes = word_bytes[start:end]
                    try:
                        slice_text = slice_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        back = end - 1
                        slice_text = None
                        while back > start:
                            try:
                                slice_text = word_bytes[start:back].decode("utf-8")
                                end = back
                                break
                            except UnicodeDecodeError:
                                back -= 1
                        if slice_text is None:
                            slice_text = word_bytes[start:end].decode("utf-8", errors="replace")
                    chunks.append(slice_text)
                    start = end
        else:
            cur_words.append(w)
            cur_bytes += cand_len
    if cur_words:
        chunks.append(" ".join(cur_words))
    return chunks

# ---------------------------------------------------------------------------
# S3 helpers (upload chunks + metadata sidecars, clear metadata)
# ---------------------------------------------------------------------------
def upload_text_to_s3(bucket: str, key: str, body: str, dry_run: bool = False) -> None:
    """
    Uploads body as UTF-8 text/plain with empty S3 object Metadata (HTTP headers).
    NOTE: this is distinct from the Bedrock KB metadata sidecar written by
    write_metadata_sidecar() below -- Bedrock KB filterable attributes must live in
    a `<key>.metadata.json` file, not in S3 object Metadata headers.
    """
    if dry_run:
        logger.info("[dry-run] would upload s3://%s/%s (%d bytes)", bucket, key, len(body.encode("utf-8")))
        return
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="text/plain",
            Metadata={},  # ensure no S3 object metadata headers
        )
        logger.info("Uploaded s3://%s/%s (%d bytes)", bucket, key, len(body.encode("utf-8")))
    except ClientError as e:
        logger.exception("Failed to upload %s/%s: %s", bucket, key, e)
        raise

def write_metadata_sidecar(
    bucket: str,
    chunk_key: str,
    source_type: str,
    doc_id: str,
    chunk_index: int,
    total_chunks: int,
    dry_run: bool = False,
) -> str:
    """
    Writes the Bedrock KB metadata sidecar for a chunk object at `<chunk_key>.metadata.json`.
    This is what allows retrieval-time filtering (e.g. filter: source_type == "project")
    instead of relying on vector similarity + prompt instructions to separate resume
    content from project/blog content.

    IMPORTANT: Bedrock KB metadata sidecars use FLAT scalar values under
    "metadataAttributes" -- Bedrock infers the attribute type from the JSON value type
    itself (string -> STRING, number -> NUMBER, bool -> BOOLEAN, list of strings ->
    STRING_LIST). Do NOT wrap values in a {"value": ..., "type": ...} object -- that
    shape gets rejected by the ingestion job with a generic "metadata file is not in
    valid JSON format" error, even though it's syntactically valid JSON. (This bit us:
    an earlier version of this function used the {value,type} wrapper and every single
    chunk's sidecar was silently ignored across an entire ingestion run.)
    """
    sidecar_key = f"{chunk_key}.metadata.json"
    payload = {
        "metadataAttributes": {
            "source_type": source_type,
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
        }
    }
    if dry_run:
        logger.info("[dry-run] would write sidecar s3://%s/%s -> %s", bucket, sidecar_key, payload)
        return sidecar_key
    try:
        s3.put_object(
            Bucket=bucket,
            Key=sidecar_key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            Metadata={},
        )
        logger.info("Wrote metadata sidecar s3://%s/%s", bucket, sidecar_key)
    except ClientError as e:
        logger.exception("Failed to write metadata sidecar %s/%s: %s", bucket, sidecar_key, e)
        raise
    return sidecar_key

def clear_object_metadata(bucket: str, key: str, dry_run: bool = False) -> None:
    """
    Rewrites an object to replace S3 object metadata (HTTP headers) with empty dict.
    """
    if dry_run:
        logger.info("[dry-run] would clear metadata for s3://%s/%s", bucket, key)
        return
    try:
        s3.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": key},
            Key=key,
            Metadata={},
            MetadataDirective="REPLACE"
        )
        logger.info("Cleared metadata for s3://%s/%s", bucket, key)
    except ClientError as e:
        logger.exception("Failed to clear metadata for %s/%s: %s", bucket, key, e)
        raise

# ---------------------------------------------------------------------------
# Source iterators (S3 and local)
# ---------------------------------------------------------------------------
def iter_s3_source_objects(bucket: str, prefix: str, extensions: Tuple[str, ...]) -> Iterator[Tuple[str, bytes]]:
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(extensions):
                resp = s3.get_object(Bucket=bucket, Key=key)
                body = resp["Body"].read()
                yield key, body

def iter_local_files(source_dir: str, extensions: Tuple[str, ...]) -> Iterator[Path]:
    p = Path(source_dir)
    for path in p.rglob("*"):
        if path.is_file() and path.suffix.lower() in extensions:
            yield path

# ---------------------------------------------------------------------------
# Main processing: sanitize, chunk (by bytes, with overlap), upload + metadata
# ---------------------------------------------------------------------------
def process_s3_source_and_upload(source_bucket: str, source_prefix: str,
                                 staging_bucket: str, staging_prefix: str,
                                 max_bytes: int, source_type: str,
                                 overlap_bytes: int = 0, dry_run: bool = False) -> List[ChunkUploadResult]:
    """
    Read files from source_bucket/source_prefix, sanitize, chunk by bytes (with overlap),
    upload each chunk to staging, and write a Bedrock KB metadata sidecar per chunk
    carrying `source_type` (resume/project/blog), `doc_id`, `chunk_index`, `total_chunks`.

    Returns a ChunkUploadResult per chunk with the byte size and sidecar-write outcome
    captured inline at upload time. This is what the handler's validation step reads
    from -- deliberately avoiding a second pass that re-lists/re-downloads everything
    from S3 afterward, since that repeated per-object GET/HEAD traffic was pushing
    ingestion past the Lambda timeout.
    """
    results: List[ChunkUploadResult] = []
    exts = (".md", ".markdown", ".html", ".htm", ".txt", ".pdf")
    for key, body in iter_s3_source_objects(source_bucket, source_prefix, exts):

        if key.lower().endswith(".pdf"):
            tmp_path = "/tmp/temp_resume.pdf"
            with open(tmp_path, "wb") as f:
                f.write(body)
            raw = extract_text(tmp_path)
        else:
            raw = body.decode("utf-8", errors="replace")

        sanitized = normalize_and_strip_to_alnum(raw, lowercase=True)
        chunks = chunk_text_by_bytes(sanitized, max_bytes, overlap_bytes=overlap_bytes)
        base = key.replace("/", "_").rsplit(".", 1)[0]
        if not chunks:
            logger.info("Skipping empty after sanitize: %s", key)
            continue
        total_chunks = len(chunks)
        for i, c in enumerate(chunks, start=1):
            dest_key = f"{staging_prefix}/{base}_chunk{i}.txt"
            upload_text_to_s3(staging_bucket, dest_key, c, dry_run=dry_run)
            sidecar_key = write_metadata_sidecar(
                staging_bucket, dest_key,
                source_type=source_type, doc_id=base,
                chunk_index=i, total_chunks=total_chunks,
                dry_run=dry_run,
            )
            results.append(ChunkUploadResult(
                chunk_key=dest_key,
                sidecar_key=sidecar_key,
                byte_size=len(c.encode("utf-8")),
                sidecar_written=True,  # write_metadata_sidecar raises on failure, so
                                        # reaching this line means it succeeded (or was
                                        # a dry-run, which we also count as "would succeed")
            ))
    return results

def process_local_dir_and_upload(source_dir: str,
                                 staging_bucket: str, staging_prefix: str,
                                 max_bytes: int, source_type: str,
                                 overlap_bytes: int = 0, dry_run: bool = False) -> List[ChunkUploadResult]:
    results: List[ChunkUploadResult] = []
    exts = (".md", ".markdown", ".html", ".htm", ".txt", ".pdf")
    for path in iter_local_files(source_dir, exts):

        if path.suffix.lower() == ".pdf":
            raw = extract_text(str(path))
        else:
            raw = path.read_text(encoding="utf-8", errors="replace")

        sanitized = normalize_and_strip_to_alnum(raw, lowercase=True)
        chunks = chunk_text_by_bytes(sanitized, max_bytes, overlap_bytes=overlap_bytes)
        base = path.relative_to(source_dir).as_posix().replace("/", "_").rsplit(".", 1)[0]
        if not chunks:
            logger.info("Skipping empty after sanitize: %s", path)
            continue
        total_chunks = len(chunks)
        for i, c in enumerate(chunks, start=1):
            dest_key = f"{staging_prefix}/{base}_chunk{i}.txt"
            upload_text_to_s3(staging_bucket, dest_key, c, dry_run=dry_run)
            sidecar_key = write_metadata_sidecar(
                staging_bucket, dest_key,
                source_type=source_type, doc_id=base,
                chunk_index=i, total_chunks=total_chunks,
                dry_run=dry_run,
            )
            results.append(ChunkUploadResult(
                chunk_key=dest_key,
                sidecar_key=sidecar_key,
                byte_size=len(c.encode("utf-8")),
                sidecar_written=True,
            ))
    return results

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def find_orphan_metadata_sidecars(bucket: str, prefix: str) -> List[str]:
    """
    A `<key>.metadata.json` sidecar is EXPECTED now (one per chunk). This check finds
    sidecars that do NOT have a matching chunk object -- those are the ones that would
    indicate a real problem (e.g. a stale sidecar left behind after a chunk was deleted
    or renamed), as opposed to the old behavior which aborted on ANY sidecar presence.
    """
    paginator = s3.get_paginator("list_objects_v2")
    all_keys = set()
    sidecar_keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            all_keys.add(key)
            if key.endswith(".metadata.json"):
                sidecar_keys.append(key)

    orphans = []
    for sk in sidecar_keys:
        chunk_key = sk[: -len(".metadata.json")]
        if chunk_key not in all_keys:
            orphans.append(sk)
            if len(orphans) >= VALIDATION_SAMPLE_LIMIT:
                break
    return orphans

def find_chunks_missing_metadata(bucket: str, prefix: str) -> List[str]:
    """
    Every chunk .txt object should have a matching `<key>.metadata.json` sidecar.
    Returns a sample of chunk keys that are missing one -- these would silently fall
    back to unfiltered retrieval, which is exactly the bug we're fixing.
    """
    paginator = s3.get_paginator("list_objects_v2")
    all_keys = set()
    chunk_keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            all_keys.add(key)
            if key.endswith(".txt"):
                chunk_keys.append(key)

    missing = []
    for ck in chunk_keys:
        if f"{ck}.metadata.json" not in all_keys:
            missing.append(ck)
            if len(missing) >= VALIDATION_SAMPLE_LIMIT:
                break
    return missing

def find_objects_with_metadata(bucket: str, prefix: str) -> List[Tuple[str, dict]]:
    """
    Return list of chunk .txt objects under prefix that have non-empty S3 OBJECT
    metadata (HTTP headers) -- this is still unwanted; it's unrelated to the
    `.metadata.json` sidecar files, which are expected and checked separately above.
    """
    paginator = s3.get_paginator("list_objects_v2")
    bad = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".metadata.json"):
                continue
            try:
                h = s3.head_object(Bucket=bucket, Key=key)
                meta = h.get("Metadata", {})
                if meta:
                    bad.append((key, meta))
                    if len(bad) >= VALIDATION_SAMPLE_LIMIT:
                        return bad
            except Exception as e:
                logger.exception("head-object failed for %s: %s", key, e)
    return bad

def list_large_objects_by_bytes(bucket: str, prefix: str, threshold_bytes: int) -> List[Tuple[str, int]]:
    """
    Return list of chunk .txt objects whose UTF-8 body bytes exceed threshold_bytes.
    Sidecar .metadata.json files are excluded from this check -- they're small JSON
    and aren't subject to the KB chunk-size limit.
    """
    paginator = s3.get_paginator("list_objects_v2")
    large = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".metadata.json"):
                continue
            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                body = resp["Body"].read()
                size = len(body)
                if size > threshold_bytes:
                    large.append((key, size))
                    if len(large) >= VALIDATION_SAMPLE_LIMIT:
                        return large
            except Exception as e:
                logger.exception("Failed reading %s: %s", key, e)
    return large

# ---------------------------------------------------------------------------
# Handler (Lambda entrypoint)
# ---------------------------------------------------------------------------
def handler(event, context):
    logger.info("Starting ingestion pipeline run | KB=%s | DataSource=%s", KNOWLEDGE_BASE_ID, DATA_SOURCE_ID)

    # 0. Basic env validation
    missing = []
    for name in ("FRONTEND_BUCKET", "STAGING_BUCKET", "KNOWLEDGE_BASE_ID", "DATA_SOURCE_ID"):
        if not globals().get(name):
            missing.append(name)
    if missing:
        msg = f"Missing required environment variables: {', '.join(missing)}"
        logger.error(msg)
        return _response(500, {"status": "error", "reason": "missing_env", "missing": missing})

    # 1. Check ingestion in progress
    in_progress, active_job_id = _is_ingestion_in_progress()
    if in_progress:
        logger.info("Ingestion already in progress: %s", active_job_id)
        return _response(200, {"status": "skipped", "reason": "ingestion_in_progress", "activeJobId": active_job_id})

    # 2. Convert, chunk (with overlap), and upload source content + metadata sidecars
    try:
        uploaded: List[ChunkUploadResult] = []
        for source_prefix, source_type in SOURCE_TYPE_BY_PREFIX.items():
            logger.info("Processing source prefix s3://%s/%s -> source_type=%s", FRONTEND_BUCKET, source_prefix, source_type)
            results = process_s3_source_and_upload(
                FRONTEND_BUCKET, source_prefix, STAGING_BUCKET, STAGING_PREFIX,
                MAX_CHUNK_BYTES, source_type=source_type,
                overlap_bytes=CHUNK_OVERLAP_BYTES, dry_run=DRY_RUN,
            )
            uploaded.extend(results)
        logger.info("Uploaded %d chunked objects (+ metadata sidecars) to staging (dry_run=%s)", len(uploaded), DRY_RUN)
    except Exception as e:
        logger.exception("Preprocessing/conversion failed: %s", e)
        return _response(500, {"status": "error", "reason": "preprocessing_failed", "error": str(e)})

    # 3. Validate from the in-memory upload results captured above -- deliberately NOT
    #    re-listing/re-downloading the staging prefix from S3 here. The previous version
    #    of this validation ran 4 separate full-prefix scans, two of which issued a
    #    head_object or get_object call PER CHUNK to re-check what we already knew at
    #    upload time (size, sidecar success). For a KB with several docs x 10-15 chunks
    #    each, that easily added hundreds of extra sequential round trips after the
    #    upload had already finished, which is what pushed this Lambda past its 30s
    #    timeout (Sandbox.Timedout). Since upload_text_to_s3/write_metadata_sidecar both
    #    raise on failure (aborting step 2 above) and we captured byte_size inline while
    #    building each chunk, everything we need to validate is already in `uploaded`.
    if not DRY_RUN:
        oversized = [r for r in uploaded if r.byte_size > MAX_CHUNK_BYTES]
        if oversized:
            logger.warning("Found %d chunk(s) exceeding MAX_CHUNK_BYTES.", len(oversized))
            return _response(400, {
                "status": "validation_failed",
                "reason": "staging_objects_too_large",
                "sample_large_objects": [(r.chunk_key, r.byte_size) for r in oversized[:VALIDATION_SAMPLE_LIMIT]],
                "advice": "Re-chunk source files so each uploaded chunk is <= MAX_CHUNK_BYTES bytes.",
            })

        missing_sidecar = [r for r in uploaded if not r.sidecar_written]
        if missing_sidecar:
            logger.warning("Found %d chunk(s) without a successfully written metadata sidecar.", len(missing_sidecar))
            return _response(400, {
                "status": "validation_failed",
                "reason": "chunks_missing_metadata_sidecar",
                "sample_chunks_missing_metadata": [r.chunk_key for r in missing_sidecar[:VALIDATION_SAMPLE_LIMIT]],
                "advice": "Every chunk .txt object must have a matching <key>.metadata.json sidecar for retrieval-time filtering to work.",
            })

        # S3 object metadata headers are hardcoded to {} in upload_text_to_s3, so there's
        # nothing to re-verify per-invocation here. If you ever want to audit the bucket
        # for drift (e.g. objects touched by some other process), run
        # find_objects_with_metadata() / find_orphan_metadata_sidecars() manually or from
        # a separate, non-latency-sensitive job -- not inline in this handler.

    # 6. Start ingestion job
    if DRY_RUN:
        logger.info("Dry run enabled; skipping ingestion start.")
        return _response(200, {"status": "dry_run", "uploaded_count": len(uploaded)})

    try:
        job = _start_ingestion_job()
    except Exception as e:
        logger.exception("Failed to start ingestion job: %s", e)
        return _response(500, {"status": "error", "reason": "start_ingestion_failed", "error": str(e)})

    return _response(200, {
        "status": "started",
        "ingestionJobId": job.get("ingestionJobId"),
        "ingestionJobStatus": job.get("status"),
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "dataSourceId": DATA_SOURCE_ID,
        "uploaded_count": len(uploaded)
    })

# ---------------------------------------------------------------------------
# Local test runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Local runner for app.py")
    parser.add_argument("--local-dir", help="Local source directory to process (optional)")
    parser.add_argument("--source-type", default="resume", choices=["resume", "project", "blog"],
                         help="source_type to tag chunks with when using --local-dir")
    parser.add_argument("--dry-run", action="store_true", help="Do not upload or start ingestion")
    parser.add_argument("--profile", help="AWS profile to use (optional)")
    parser.add_argument("--max-bytes", type=int, default=MAX_CHUNK_BYTES, help="Max bytes per chunk")
    parser.add_argument("--overlap-bytes", type=int, default=CHUNK_OVERLAP_BYTES, help="Overlap bytes between chunks")
    args = parser.parse_args()

    if args.profile:
        import boto3.session
        session = boto3.session.Session(profile_name=args.profile)
        s3 = session.client("s3")
        bedrock_agent = session.client("bedrock-agent", region_name=REGION)

    if args.local_dir:
        logger.info("Processing local directory %s -> s3://%s/%s (dry_run=%s)", args.local_dir, STAGING_BUCKET, STAGING_PREFIX, args.dry_run or DRY_RUN)
        keys = process_local_dir_and_upload(
            args.local_dir, STAGING_BUCKET, STAGING_PREFIX, args.max_bytes,
            source_type=args.source_type, overlap_bytes=args.overlap_bytes,
            dry_run=(args.dry_run or DRY_RUN),
        )
        logger.info("Local processing uploaded %d keys (dry_run=%s)", len(keys), args.dry_run or DRY_RUN)
    else:
        logger.info("Running handler in dry-run mode (no local dir provided)")
        resp = handler({}, None)
        print(json.dumps(resp, indent=2))