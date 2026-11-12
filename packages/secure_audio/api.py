from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .crypto import digest, read_json
from .domain import CapacityExceeded, Conflict, DomainError, Forbidden, InvalidMedia, NotFound, Principal, RegistrationInput, Role, ScreeningInput
from .repository import SqlRepository
from .service import ScreeningService


_STATUS = {Forbidden: 403, NotFound: 404, Conflict: 409, CapacityExceeded: 429, InvalidMedia: 400}


async def _read_upload(request: Request, max_bytes: int, model: type[BaseModel]) -> tuple[bytes, BaseModel]:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Expected multipart/form-data")
    body_limit = max_bytes + 32768
    declared_size = request.headers.get("content-length")
    if declared_size:
        if not declared_size.isdecimal():
            raise HTTPException(400, "Invalid Content-Length")
        if int(declared_size) > body_limit:
            raise HTTPException(413, "Request exceeds the upload limit")
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > body_limit:
            raise HTTPException(413, "Request exceeds the upload limit")
        buffer.extend(chunk)

    async def receive() -> dict:
        return {"type": "http.request", "body": bytes(buffer), "more_body": False}

    try:
        async with Request(request.scope, receive).form(max_files=1, max_fields=1, max_part_size=16384) as form:
            audio, metadata = form.get("audio"), form.get("metadata")
            if len(form.multi_items()) != 2 or not isinstance(audio, UploadFile) or not isinstance(metadata, str):
                raise HTTPException(400, "Expected an audio file and a JSON metadata field")
            media = await audio.read(max_bytes + 1)
            if len(media) > max_bytes:
                raise HTTPException(413, "Audio exceeds the upload limit")
            return media, model.model_validate(read_json(metadata))
    except ValidationError as exc:
        raise HTTPException(422, "Invalid upload metadata") from exc
    except (ValueError, StarletteHTTPException) as exc:
        if isinstance(exc, StarletteHTTPException):
            raise
        raise HTTPException(400, "Malformed multipart input or metadata JSON") from exc


def create_app(repository: SqlRepository | None = None, service: ScreeningService | None = None) -> FastAPI:
    if repository is None or service is None:
        settings = Settings.load()
        repository = settings.repository()
        service = settings.service(repository)

    app = FastAPI(title="Secure Audio Screening", version="0.1.0")
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
        token = credentials.credentials if credentials else ""
        actor = repository.principal(digest(token.encode("utf-8"))) if 32 <= len(token) <= 128 else None
        if not actor:
            raise HTTPException(401, "Valid bearer credentials required", headers={"WWW-Authenticate": "Bearer"})
        return actor

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=_STATUS.get(type(error), 400))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/registrations", status_code=202)
    async def register(request: Request, actor: Annotated[Principal, Depends(authenticate)]) -> dict:
        service.require_role(actor, Role.CONTRIBUTOR)
        audio, details = await _read_upload(request, service.max_bytes, RegistrationInput)
        return await run_in_threadpool(service.register, actor, audio, details)

    @app.get("/v1/registrations/{registration_id}")
    def registration(registration_id: UUID, actor: Annotated[Principal, Depends(authenticate)]) -> dict:
        return service.read_registration(actor, str(registration_id))

    @app.post("/v1/registrations/{registration_id}/approve")
    def approve(registration_id: UUID, actor: Annotated[Principal, Depends(authenticate)]) -> dict:
        return service.approve(actor, str(registration_id))

    @app.post("/v1/screenings", status_code=202)
    async def screen(request: Request, actor: Annotated[Principal, Depends(authenticate)]) -> dict:
        service.require_role(actor, Role.UPLOADER)
        audio, details = await _read_upload(request, service.max_bytes, ScreeningInput)
        return await run_in_threadpool(service.screen, actor, audio, details)

    @app.get("/v1/jobs/{job_id}")
    def job_status(job_id: UUID, actor: Annotated[Principal, Depends(authenticate)]) -> dict:
        return service.read_job(actor, str(job_id))

    return app
