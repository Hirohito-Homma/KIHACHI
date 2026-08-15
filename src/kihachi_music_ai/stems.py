"""stem分離の契約。分離そのものは実行しない。

KIHACHIはstemを**作らない**。作る道具（Demucs等）はtorchと数百MBの重みを要求し、
それをコアへ入れるとADR-0001の「標準ライブラリだけで動く」が壊れる。代わりにここは
契約だけを持つ — どこへ何という名前で置くか、何を検証するか、何を記録するか。

`plan_separation` は走らせるべきコマンドを組み立てて返し、`import_stems` は
どこで作られたかを問わずstemを検証して記録する。ローカルCPUで回してもGPUの箱で
回しても、置き場所が契約どおりなら同じように取り込める。

詳細はADR-0008。`instrumental-plan` が repaint コマンドを表示するだけなのと同じ形で、
理由も同じ — 分離はGPUと数分を使い、既存ファイルを上書きしうるので、走らせる判断は
呼び出し側に残す。
"""

from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = "stem-manifest-v1"

DEFAULT_MODEL = "htdemucs"
"""4 stemモデル。KIHACHIが分ける必要があるのはbassとotherで、htdemucsはその2つを
別々に出す最小構成である。"""

STEM_NAMES: tuple[str, ...] = ("drums", "bass", "other", "vocals")

STEM_DIRECTORY = "stems"
"""`<project>/audio/stems/` に置く。元Audioと同じ`audio/`の下に、混ざらないよう1階層下げる。"""

#: 尺の一致とみなす差。分離器はフレーム境界で丸めることがある。
DURATION_TOLERANCE_SEC = 0.05


@dataclass(frozen=True)
class SeparationPlan:
    """走らせるべき分離コマンドと、その結果が置かれる場所。"""

    source_audio: Path
    output_dir: Path
    model: str
    expected_stems: tuple[Path, ...]
    command: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "source_audio": self.source_audio.as_posix(),
            "output_dir": self.output_dir.as_posix(),
            "expected_stems": [path.as_posix() for path in self.expected_stems],
            "command": list(self.command),
        }


def stem_paths(
    project_dir: Path | str,
    *,
    model: str = DEFAULT_MODEL,
    stem_names: tuple[str, ...] = STEM_NAMES,
) -> tuple[Path, ...]:
    """見つかったstemの配置。無ければ平置きのパスを返す。

    Demucsは`-o`の下に必ずモデル名のディレクトリを掘る（`--filename`が変えるのは
    葉の名前だけで、この階層は消せない）。他の分離器は平置きで出す。どちらでも
    取り込めるよう、平置きを先に見てからモデル名の下を見る。
    """

    base = Path(project_dir) / "audio" / STEM_DIRECTORY
    resolved: list[Path] = []
    for name in stem_names:
        flat = base / f"{name}.wav"
        nested = base / model / f"{name}.wav"
        resolved.append(nested if not flat.exists() and nested.exists() else flat)
    return tuple(resolved)


def plan_separation(
    project_dir: Path | str,
    *,
    audio_file: Path | str | None = None,
    model: str = DEFAULT_MODEL,
    stem_names: tuple[str, ...] = STEM_NAMES,
) -> SeparationPlan:
    """分離コマンドを組み立てる。ファイルは1バイトも書かない。"""

    project = Path(project_dir)
    source = _resolve_audio(project, audio_file)
    if not source.exists():
        raise FileNotFoundError(f"source audio not found: {source}")
    output_dir = project / "audio" / STEM_DIRECTORY
    # `--filename` names the leaf only: Demucs always digs its own <model>/
    # directory under `-o`, and that cannot be turned off. So the command below
    # really produces audio/stems/<model>/<stem>.wav, and the import reads both
    # that and a flat layout -- measured 2026-08-15 rather than assumed.
    command = (
        "demucs",
        "-n",
        model,
        "--filename",
        "{stem}.{ext}",
        "-o",
        output_dir.as_posix(),
        source.as_posix(),
    )
    return SeparationPlan(
        source_audio=source,
        output_dir=output_dir,
        model=model,
        expected_stems=tuple(
            output_dir / model / f"{name}.wav" for name in stem_names
        ),
        command=command,
    )


def import_stems(
    project_dir: Path | str,
    *,
    audio_file: Path | str | None = None,
    model: str = DEFAULT_MODEL,
    stem_names: tuple[str, ...] = STEM_NAMES,
    overwrite: bool = False,
) -> dict[str, Any]:
    """既にあるstemを検証し、`stem_manifest.json` を書く。

    分離器が何であれ、契約どおりの場所に契約どおりの形式で置かれていれば取り込む。
    元Audioは読むだけで、stemも書き換えない。
    """

    project = Path(project_dir)
    source = _resolve_audio(project, audio_file)
    if not source.exists():
        raise FileNotFoundError(f"source audio not found: {source}")
    destination = project / "stem_manifest.json"
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite stem manifest: {destination}")

    source_shape = _wav_shape(source)
    resolved = stem_paths(project, model=model, stem_names=stem_names)
    missing = [path for path in resolved if not path.exists()]
    if missing:
        names = ", ".join(_display_path(path, project) for path in missing)
        raise FileNotFoundError(
            f"stems not found: {names}. Run `kihachi stems prepare` and the command it prints"
        )

    entries: list[dict[str, Any]] = []
    for name, path in zip(stem_names, resolved):
        shape = _wav_shape(path)
        _verify_against_source(name, shape, source_shape)
        entries.append(
            {
                "stem": name,
                "path": _display_path(path, project),
                "sha256": _file_sha256(path),
                "duration_sec": shape["duration_sec"],
                "sample_rate": shape["sample_rate"],
                "resampled": shape["sample_rate"] != source_shape["sample_rate"],
            }
        )

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "model": model,
        "separator_run_by": "caller",
        "source_audio": {
            "path": _display_path(source, project),
            "sha256": _file_sha256(source),
            "duration_sec": source_shape["duration_sec"],
            "sample_rate": source_shape["sample_rate"],
            "channels": source_shape["channels"],
        },
        "stems": entries,
    }
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def load_stem_manifest(path: Path | str) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    version = manifest.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ValueError(f"unsupported stem manifest version: {version!r}")
    return manifest


def _verify_against_source(name: str, shape: dict[str, Any], source: dict[str, Any]) -> None:
    """分離器の出力が元Audioと同じ形かを確かめる。

    守るべきは**尺**である。ずれたstemは小節グリッド上の解析を静かに狂わせるので、
    ここで止めるほうがあとで解析結果を疑うより安い。

    sample rateは一致を求めない。htdemucsは48 kHzを渡しても44.1 kHzで返す
    （2026-08-15実測、尺は122.1820 sで完全一致）。解析器は各ファイル自身のrateを
    読むので測定に影響しない。事実としてmanifestに残すだけにする。
    """

    if shape["channels"] != source["channels"]:
        raise ValueError(
            f"stem {name} has {shape['channels']} channel(s) against the source's "
            f"{source['channels']}"
        )
    drift = abs(shape["duration_sec"] - source["duration_sec"])
    if drift > DURATION_TOLERANCE_SEC:
        raise ValueError(
            f"stem {name} runs {shape['duration_sec']:.3f} s against the source's "
            f"{source['duration_sec']:.3f} s"
        )


def _wav_shape(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        if rate <= 0:
            raise ValueError(f"WAV must declare a positive sample rate: {path}")
        return {
            "sample_rate": rate,
            "channels": source.getnchannels(),
            "duration_sec": round(source.getnframes() / rate, 4),
        }


def _resolve_audio(project_dir: Path, audio_file: Path | str | None) -> Path:
    if audio_file is None:
        return project_dir / "audio" / "ace-step-01.wav"
    path = Path(audio_file)
    return path if path.is_absolute() else project_dir / path


def _display_path(target: Path, base: Path) -> str:
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return target.as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()
