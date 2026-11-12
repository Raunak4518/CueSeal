import argparse
from collections import defaultdict
from contextlib import nullcontext
import csv
from dataclasses import dataclass
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile

import av
import numpy as np
from scipy.io import wavfile

from .decoder import SAMPLE_RATE


SPLITS = ("train", "validation", "test")
LICENSE = re.compile(r"https?://creativecommons\.org/licenses/(?:by|by-sa|by-nc|by-nc-sa)/[\d.]+/")


@dataclass(frozen=True)
class Track:
    id: str
    artist: str
    path: str
    license: str
    attribution: str


def _read_tracks(source: Path) -> list[Track]:
    attribution = {}
    for block in (source / "audio_licenses.txt").read_text(encoding="utf-8").split("\n--\n"):
        lines = block.strip().splitlines()
        match = LICENSE.search(lines[2]) if len(lines) >= 3 else None
        if match:
            attribution[lines[0]] = (match.group().replace("http:", "https:", 1), lines[1])
    with (source / "song_describer.csv").open(encoding="utf-8", newline="") as stream:
        rows = {row["track_id"]: row for row in csv.DictReader(stream)}
    return [Track(track_id, row["artist_id"], row["path"], *attribution[row["path"]])
            for track_id, row in sorted(rows.items()) if row["path"] in attribution and row["artist_id"]]


def _allocate(tracks: list[Track], rng: np.random.Generator) -> dict[str, list[Track]]:
    artists = defaultdict(list)
    for item in tracks:
        artists[item.artist].append(item)
    if len(artists) < 3:
        raise ValueError("Expected at least three artist groups")
    groups = list(artists.values())
    rng.shuffle(groups)
    train_size = max(1, round(len(tracks) * 59 / 489))
    validation_size = max(1, round(len(tracks) * 94 / 489))
    sizes = {"train": train_size, "validation": validation_size, "test": max(1, len(tracks) - train_size - validation_size)}
    buckets = {key: [] for key in SPLITS}
    counts = dict.fromkeys(SPLITS, 0)
    for group in groups:
        name = max(SPLITS, key=lambda key: (sizes[key] - counts[key]) / sizes[key])
        buckets[name].append(group)
        counts[name] += len(group)
    while True:
        best = None
        for source in SPLITS:
            for target in SPLITS:
                if counts[source] <= sizes[source] or counts[target] >= sizes[target]:
                    continue
                excess, shortage = counts[source] - sizes[source], sizes[target] - counts[target]
                for index, group in enumerate(buckets[source]):
                    length = len(group)
                    gain = excess + shortage - abs(excess - length) - abs(shortage - length)
                    if gain > 0 and (best is None or gain > best[0]):
                        best = gain, source, target, index
        if best is None:
            return {name: [track for group in buckets[name] for track in group] for name in SPLITS}
        _, source, target, index = best
        group = buckets[source].pop(index)
        buckets[target].append(group)
        counts[source] -= len(group)
        counts[target] += len(group)


def _reference_audio(data: bytes) -> np.ndarray:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    parts = []
    count = 0
    with av.open(io.BytesIO(data), format="mp3") as audio:
        for frame in audio.decode(audio=0):
            for output in resampler.resample(frame):
                samples = output.to_ndarray().ravel()
                parts.append(samples)
                count += samples.size
            if count >= 40 * SAMPLE_RATE:
                break
    if not parts:
        raise ValueError("Source audio is empty")
    pcm = np.concatenate(parts)
    offset = min(8 * SAMPLE_RATE, max(0, pcm.size - 30 * SAMPLE_RATE))
    result = pcm[offset:offset + 30 * SAMPLE_RATE]
    if result.size < 15 * SAMPLE_RATE or not np.isfinite(result).all():
        raise ValueError("Source audio is too short or contains invalid samples")
    return np.clip(result, -1, 1)


def _mp3(path: Path, samples: np.ndarray) -> None:
    with av.open(str(path), mode="w", format="mp3") as audio:
        stream = audio.add_stream("libmp3lame", rate=SAMPLE_RATE)
        stream.layout = "mono"
        stream.bit_rate = 32000
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="fltp", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        for packet in stream.encode(frame) + stream.encode(None):
            audio.mux(packet)


def _scale_noise(samples: np.ndarray, noise: np.ndarray, decibels: float) -> np.ndarray:
    return noise * (np.linalg.norm(samples) / max(1e-9, np.linalg.norm(noise))) * 10 ** (-decibels / 20)


def _write_query(output: Path, track: Track, samples: np.ndarray, condition: str, start_ms=None, end_ms=None) -> dict:
    extension = ".mp3" if condition == "mp3_32kbps" else ".wav"
    path = Path("queries") / f"{track.id}-{condition}{extension}"
    samples = np.clip(samples, -1, 1).astype(np.float32)
    if extension == ".mp3":
        _mp3(output / path, samples)
    else:
        wavfile.write(output / path, SAMPLE_RATE, samples)
    query = {"path": path.as_posix(), "source_group": track.artist, "condition": condition}
    if start_ms is not None:
        query.update(reference_id=track.id, start_ms=start_ms, end_ms=end_ms)
    return query


def prepare(source: Path, output: Path) -> dict:
    rng = np.random.default_rng(80427)
    tracks = _read_tracks(source)
    splits = _allocate(tracks, rng)
    (output / "references").mkdir(parents=True, exist_ok=True)
    (output / "queries").mkdir(exist_ok=True)
    manifest, provenance = {}, {}
    seen = set()
    from_zip = (source / "audio.zip").is_file()
    with (ZipFile(source / "audio.zip") if from_zip else nullcontext()) as archive:
        members = {name.removeprefix("audio/").replace(".2min.mp3", ".mp3"): name
                   for name in archive.namelist() if name.lower().endswith(".mp3")} if archive else {}
        for name, split in splits.items():
            rng.shuffle(split)
            unknown_target = min(300, round(len(split) * 0.89)) if name == "test" else min(18, round(len(split) * 0.35))
            known_target = min(36, max(1, len(split) - unknown_target))
            references, queries, known, beds = [], [], [], []
            for track in split:
                raw = archive.read(members[track.path]) if archive else (source / "recordings" / f"{track.id}.mp3").read_bytes()
                digest = sha256(raw).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                samples = _reference_audio(raw)
                first = int(rng.integers(2 * SAMPLE_RATE, samples.size - 10 * SAMPLE_RATE))
                crop = samples[first:first + 10 * SAMPLE_RATE].copy()
                provenance[track.id] = {"split": name, "artist_id": track.artist, "source_sha256": digest,
                                        "source_bytes": len(raw), "license": track.license, "attribution": track.attribution,
                                        "role": "reference" if len(known) < known_target else "unknown"}
                if len(known) < known_target:
                    path = Path("references") / f"{track.id}.wav"
                    wavfile.write(output / path, SAMPLE_RATE, samples)
                    references.append({"id": track.id, "source_group": track.artist, "path": path.as_posix(), "license": track.license})
                    known.append((track, crop))
                else:
                    queries.append(_write_query(output, track, crop, "unknown_music"))
                    if len(beds) < known_target:
                        beds.append(crop)
            if not beds:
                raise ValueError(f"Split {name} has no unrelated recordings for queries")
            for index, (track, crop) in enumerate(known):
                noise = rng.normal(size=crop.shape)
                noisy = crop + _scale_noise(crop, noise, 8)
                bed = beds[index % len(beds)]
                insert = slice(2 * SAMPLE_RATE, 8 * SAMPLE_RATE)
                mixed = bed * (np.linalg.norm(crop[:6 * SAMPLE_RATE]) / max(1e-9, np.linalg.norm(bed[insert]))) * 10 ** (-6 / 20)
                mixed[insert] += crop[:6 * SAMPLE_RATE]
                for signal, condition, start, end in ((crop, "clean", 0, 10000), (crop, "mp3_32kbps", 0, 10000),
                                                       (noisy, "noise_8db", 0, 10000), (mixed, "music_overlay_6db", 2000, 8000)):
                    queries.append(_write_query(output, track, signal, condition, start, end))
            manifest[name] = {"references": references, "queries": queries}
            print(f"{name}: {len(references)} references, {len(queries)} queries, {len({track.artist for track in split})} artists", flush=True)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.output)


if __name__ == "__main__":
    main()
