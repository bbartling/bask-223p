"""Read a pushed Turtle document or export zip. No path from the zip is written."""
from __future__ import annotations

import io
import zipfile

from rdflib import Graph


class UploadError(ValueError):
    """The body is not a model the collector can store."""


def model_from_body(body: bytes, content_type: str, *, max_bytes: int) -> tuple[bytes, bytes | None]:
    if len(body) > max_bytes:
        raise UploadError("body exceeds max_upload_bytes")
    media = content_type.split(";", 1)[0].strip().lower()
    if media in {"text/turtle", "application/x-turtle"}:
        _parse(body)
        return body, None
    if media in {"application/zip", "application/octet-stream"}:
        try:
            archive = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile as exc:
            raise UploadError("body is not a zip file") from exc
        with archive:
            names = [name for name in archive.namelist() if name == "model.ttl" or name.endswith("/model.ttl")]
            if len(names) != 1 or names[0].startswith("/") or ".." in names[0].split("/"):
                raise UploadError("zip must contain one model.ttl")
            info = archive.getinfo(names[0])
            if info.file_size > max_bytes:
                raise UploadError("model.ttl exceeds max_upload_bytes")
            model = archive.read(names[0])
        _parse(model)
        return model, body
    raise UploadError("Content-Type must be text/turtle or application/zip")


def _parse(payload: bytes) -> None:
    try:
        Graph().parse(data=payload.decode("utf-8"), format="turtle")
    except Exception as exc:
        raise UploadError("model.ttl is not Turtle") from exc
