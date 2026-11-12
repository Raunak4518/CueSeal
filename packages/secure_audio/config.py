from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path

from sqlalchemy.engine import URL, make_url

from .crypto import load_keys
from .decoder import Decoder, SubprocessDecoder
from .repository import SqlRepository
from .scoring import LogisticModel, RuleScorer
from .service import ScreeningService
from .worker import JobRunner


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: URL
    model_path: Path | None

    @classmethod
    def load(cls):
        root = Path(os.environ.get("SECURE_AUDIO_DATA_DIR", ".secure-audio")).resolve()
        default_url = URL.create("sqlite", database=str(root / "screening.sqlite3"))
        model_path = os.environ.get("SECURE_AUDIO_MODEL")
        database_url = os.environ.get("SECURE_AUDIO_DATABASE_URL")
        return cls(root, make_url(database_url) if database_url else default_url, Path(model_path) if model_path else None)

    @property
    def key_dir(self) -> Path:
        return self.data_dir / "keys"

    def repository(self) -> SqlRepository:
        return SqlRepository(self.database_url)

    def service(self, repository: SqlRepository) -> ScreeningService:
        signer, cipher = load_keys(self.key_dir)
        return ScreeningService(repository, signer, cipher)

    def worker(self, repository: SqlRepository, decoder: Decoder | None = None) -> JobRunner:
        signer, cipher = load_keys(self.key_dir)
        scorer = LogisticModel.from_path(self.model_path) if self.model_path else RuleScorer()
        return JobRunner(repository, SubprocessDecoder() if decoder is None else decoder, scorer, signer, cipher)

    @contextmanager
    def worker_session(self, repository: SqlRepository):
        decoder = SubprocessDecoder()
        runner = self.worker(repository, decoder)
        with decoder.prewarm():
            yield runner
