from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..models import SongSpec
from ..prompt_compiler import compile_audio_prompt
from ..tail_guard import (
    DEFAULT_TAIL_FADE_SEC,
    TrimManifest,
    guard_seconds,
    guarded_duration,
    measure_music_end,
    seconds_per_bar,
    trim_wav_to_duration,
    validate_guard_bars,
)

AUDIO_FORMATS = frozenset({"flac", "mp3", "opus", "aac", "wav", "wav32"})
ADAPTER_VERSION = "ace-step-1.5-rest"


class AceStepError(RuntimeError):
    """A safe, credential-free ACE-Step adapter error."""


@dataclass(frozen=True)
class AceStepRepaintWindow:
    """A SongSpec repaint selection resolved to an ACE-Step time window."""

    selector: str
    start_bar: int
    end_bar: int
    start_sec: float
    end_sec: float
    section_name: str | None = None
    tail_guard_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.section_name is None:
            payload.pop("section_name")
        if not self.tail_guard_sec:
            payload.pop("tail_guard_sec")
        return payload


@dataclass(frozen=True)
class AceStepOptions:
    audio_format: str = "wav"
    thinking: bool = False
    model: str | None = None
    inference_steps: int = 8
    batch_size: int = 1
    lyrics: str = ""
    revision: str = ""
    task_type: str = "text2music"
    audio_cover_strength: float = 1.0
    cover_noise_strength: float = 0.0
    repainting_start: float = 0.0
    repainting_end: float | None = None
    repaint_mode: str = "balanced"
    repaint_strength: float = 0.5
    repaint_latent_crossfade_frames: int = 10
    repaint_wav_crossfade_sec: float = 0.0
    chunk_mask_mode: str = "explicit"
    tail_guard_bars: float = 0.0

    def __post_init__(self) -> None:
        if validate_guard_bars(self.tail_guard_bars) > 0.0 and self.audio_format != "wav":
            # Trimming back to the song grid is implemented for PCM WAV only, and
            # a guard that is never trimmed would ship an over-long deliverable.
            raise ValueError("tail_guard_bars requires audio_format='wav'")
        if self.audio_format not in AUDIO_FORMATS:
            raise ValueError(f"unsupported ACE-Step audio format: {self.audio_format}")
        if not 1 <= self.inference_steps <= 200:
            raise ValueError("inference_steps must be between 1 and 200")
        if not 1 <= self.batch_size <= 8:
            raise ValueError("batch_size must be between 1 and 8")
        if self.model is not None and not self.model.strip():
            raise ValueError("model must not be blank")
        if self.task_type not in {"text2music", "cover", "repaint"}:
            raise ValueError("the v0.1 adapter supports only text2music, cover, and repaint")
        if not 0.0 <= self.audio_cover_strength <= 1.0:
            raise ValueError("audio_cover_strength must be between 0.0 and 1.0")
        if not 0.0 <= self.cover_noise_strength <= 1.0:
            raise ValueError("cover_noise_strength must be between 0.0 and 1.0")
        if self.repainting_start < 0.0:
            raise ValueError("repainting_start must not be negative")
        if self.repaint_mode not in {"conservative", "balanced", "aggressive"}:
            raise ValueError("unsupported repaint_mode")
        if not 0.0 <= self.repaint_strength <= 1.0:
            raise ValueError("repaint_strength must be between 0.0 and 1.0")
        if self.repaint_latent_crossfade_frames < 0:
            raise ValueError("repaint_latent_crossfade_frames must not be negative")
        if self.repaint_wav_crossfade_sec < 0.0:
            raise ValueError("repaint_wav_crossfade_sec must not be negative")
        if self.chunk_mask_mode not in {"explicit", "auto"}:
            raise ValueError("unsupported chunk_mask_mode")
        if self.task_type == "repaint":
            if self.repainting_end is None:
                raise ValueError("repaint requires repainting_end")
            if self.repainting_end <= self.repainting_start:
                raise ValueError("repainting_end must be greater than repainting_start")


@dataclass(frozen=True)
class AceStepGenerationRequest:
    prompt: str
    lyrics: str
    thinking: bool
    vocal_language: str
    audio_format: str
    bpm: int
    key_scale: str
    time_signature: str
    audio_duration: float
    inference_steps: int
    use_random_seed: bool
    seed: int
    batch_size: int
    sample_mode: bool
    use_format: bool
    use_cot_caption: bool
    use_cot_language: bool
    task_type: str
    audio_cover_strength: float | None = None
    cover_noise_strength: float | None = None
    repainting_start: float | None = None
    repainting_end: float | None = None
    repaint_mode: str | None = None
    repaint_strength: float | None = None
    repaint_latent_crossfade_frames: int | None = None
    repaint_wav_crossfade_sec: float | None = None
    chunk_mask_mode: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("ACE-Step prompt must not be empty")
        if not 30 <= self.bpm <= 300:
            raise ValueError("ACE-Step bpm must be between 30 and 300")
        if not 10.0 <= self.audio_duration <= 600.0:
            raise ValueError("ACE-Step audio_duration must be between 10 and 600 seconds")
        if self.audio_format not in AUDIO_FORMATS:
            raise ValueError(f"unsupported ACE-Step audio format: {self.audio_format}")
        if not 1 <= self.inference_steps <= 200:
            raise ValueError("ACE-Step inference_steps must be between 1 and 200")
        if not 1 <= self.batch_size <= 8:
            raise ValueError("ACE-Step batch_size must be between 1 and 8")
        if self.task_type not in {"text2music", "cover", "repaint"}:
            raise ValueError("the v0.1 adapter supports only text2music, cover, and repaint")
        for name, value in (
            ("audio_cover_strength", self.audio_cover_strength),
            ("cover_noise_strength", self.cover_noise_strength),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0.0 and 1.0")
        if self.task_type in {"cover", "repaint"} and (
            self.audio_cover_strength is None or self.cover_noise_strength is None
        ):
            raise ValueError("Audio-conditioned requests require both cover strength values")
        if self.task_type == "repaint":
            if self.repainting_start is None or self.repainting_end is None:
                raise ValueError("repaint requests require a start and end")
            if self.repainting_start < 0.0 or self.repainting_end <= self.repainting_start:
                raise ValueError("invalid repaint range")
            if self.repaint_mode not in {"conservative", "balanced", "aggressive"}:
                raise ValueError("unsupported repaint_mode")
            if self.repaint_strength is None or not 0.0 <= self.repaint_strength <= 1.0:
                raise ValueError("repaint_strength must be between 0.0 and 1.0")
            if self.chunk_mask_mode not in {"explicit", "auto"}:
                raise ValueError("unsupported chunk_mask_mode")

    @classmethod
    def from_song_spec(
        cls,
        spec: SongSpec,
        options: AceStepOptions | None = None,
    ) -> AceStepGenerationRequest:
        options = options or AceStepOptions()
        numerator = spec.song.time_signature.split("/", 1)[0]
        prompt = compile_audio_prompt(spec).strip()
        revision = options.revision.strip()
        if revision:
            # Put corrective constraints first so they survive encoder
            # truncation and receive priority over the longer base prompt.
            prompt = f"Revision constraints (highest priority):\n{revision}\n\nBase song design:\n{prompt}"
        return cls(
            prompt=prompt,
            lyrics=options.lyrics,
            thinking=options.thinking,
            vocal_language="en",
            audio_format=options.audio_format,
            bpm=int(round(spec.song.bpm)),
            key_scale=spec.song.key,
            time_signature=numerator,
            audio_duration=guarded_duration(spec, options.tail_guard_bars),
            inference_steps=options.inference_steps,
            use_random_seed=False,
            seed=spec.seed,
            batch_size=options.batch_size,
            sample_mode=False,
            use_format=False,
            use_cot_caption=False,
            use_cot_language=False,
            task_type=options.task_type,
            audio_cover_strength=(
                options.audio_cover_strength
                if options.task_type in {"cover", "repaint"}
                else None
            ),
            cover_noise_strength=(
                options.cover_noise_strength
                if options.task_type in {"cover", "repaint"}
                else None
            ),
            repainting_start=(options.repainting_start if options.task_type == "repaint" else None),
            repainting_end=(options.repainting_end if options.task_type == "repaint" else None),
            repaint_mode=(options.repaint_mode if options.task_type == "repaint" else None),
            repaint_strength=(options.repaint_strength if options.task_type == "repaint" else None),
            repaint_latent_crossfade_frames=(
                options.repaint_latent_crossfade_frames
                if options.task_type == "repaint"
                else None
            ),
            repaint_wav_crossfade_sec=(
                options.repaint_wav_crossfade_sec if options.task_type == "repaint" else None
            ),
            chunk_mask_mode=(options.chunk_mask_mode if options.task_type == "repaint" else None),
            model=options.model,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for name in (
            "model",
            "audio_cover_strength",
            "cover_noise_strength",
            "repainting_start",
            "repainting_end",
            "repaint_mode",
            "repaint_strength",
            "repaint_latent_crossfade_frames",
            "repaint_wav_crossfade_sec",
            "chunk_mask_mode",
        ):
            if payload[name] is None:
                payload.pop(name)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


@dataclass(frozen=True)
class AceStepConfig:
    base_url: str = "http://127.0.0.1:8001"
    api_key: str | None = None
    request_timeout: float = 30.0

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ACE-Step base_url must be an http(s) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("ACE-Step base_url must not contain a query or fragment")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")


@dataclass(frozen=True)
class AceStepLoraConfig:
    lora_path: str
    scale: float = 1.0
    adapter_name: str | None = None

    def __post_init__(self) -> None:
        if not self.lora_path.strip():
            raise ValueError("LoRA path must not be blank")
        if not 0.0 <= self.scale <= 1.0:
            raise ValueError("LoRA scale must be between 0.0 and 1.0")
        if self.adapter_name is not None and not self.adapter_name.strip():
            raise ValueError("LoRA adapter name must not be blank")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lora_path": self.lora_path.strip(),
            "scale": self.scale,
        }
        if self.adapter_name is not None:
            payload["adapter_name"] = self.adapter_name.strip()
        return payload


@dataclass(frozen=True)
class AceStepLoraStatus:
    lora_loaded: bool
    use_lora: bool
    lora_scale: float
    adapter_type: str | None = None
    scales: Mapping[str, float] | None = None
    active_adapter: str | None = None
    adapters: tuple[Any, ...] = ()
    synthetic_default_mode: bool = False

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AceStepLoraStatus:
        raw_scales = data.get("scales")
        scales: dict[str, float] = {}
        if isinstance(raw_scales, Mapping):
            for name, value in raw_scales.items():
                try:
                    scales[str(name)] = float(value)
                except (TypeError, ValueError):
                    continue
        raw_adapters = data.get("adapters")
        adapters = tuple(raw_adapters) if isinstance(raw_adapters, list) else ()
        adapter_type = data.get("adapter_type")
        active_adapter = data.get("active_adapter")
        try:
            lora_scale = float(data.get("lora_scale", 1.0))
        except (TypeError, ValueError) as exc:
            raise AceStepError("ACE-Step LoRA status contained an invalid scale") from exc
        return cls(
            lora_loaded=bool(data.get("lora_loaded", False)),
            use_lora=bool(data.get("use_lora", False)),
            lora_scale=lora_scale,
            adapter_type=str(adapter_type) if adapter_type is not None else None,
            scales=scales,
            active_adapter=str(active_adapter) if active_adapter is not None else None,
            adapters=adapters,
            synthetic_default_mode=bool(data.get("synthetic_default_mode", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lora_loaded": self.lora_loaded,
            "use_lora": self.use_lora,
            "lora_scale": self.lora_scale,
            "adapter_type": self.adapter_type,
            "scales": dict(self.scales or {}),
            "active_adapter": self.active_adapter,
            "adapters": list(self.adapters),
            "synthetic_default_mode": self.synthetic_default_mode,
        }


@dataclass(frozen=True)
class AceStepTask:
    task_id: str
    status: str
    queue_position: int | None = None


@dataclass(frozen=True)
class AceStepOutput:
    file: str
    status: int
    seed_value: str = ""
    lm_model: str = ""
    dit_model: str = ""
    generation_info: str = ""
    metas: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AceStepOutput:
        file_url = str(data.get("file", ""))
        if not file_url:
            raise AceStepError("ACE-Step succeeded without an audio file URL")
        metas = data.get("metas")
        return cls(
            file=file_url,
            status=int(data.get("status", 1)),
            seed_value=str(data.get("seed_value", "")),
            lm_model=str(data.get("lm_model", "")),
            dit_model=str(data.get("dit_model", "")),
            generation_info=str(data.get("generation_info", "")),
            metas=metas if isinstance(metas, Mapping) else None,
        )


@dataclass(frozen=True)
class AceStepTaskResult:
    task_id: str
    status: int
    outputs: tuple[AceStepOutput, ...]


@dataclass(frozen=True)
class AceStepRenderManifest:
    project_dir: Path
    task_id: str
    request_file: Path
    result_file: Path
    audio_files: tuple[Path, ...]
    lora_status: AceStepLoraStatus | None = None


class AceStepClient:
    """Small stdlib client for the official ACE-Step 1.5 asynchronous REST API."""

    def __init__(
        self,
        config: AceStepConfig | None = None,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config or AceStepConfig()
        self._opener = opener or urllib.request.urlopen

    def submit(
        self,
        request: AceStepGenerationRequest,
        *,
        source_audio: Path | None = None,
        reference_audio: Path | None = None,
    ) -> AceStepTask:
        if request.task_type in {"cover", "repaint"} and source_audio is None:
            raise ValueError(f"ACE-Step {request.task_type} requires source_audio")
        if request.task_type == "text2music" and source_audio is not None:
            raise ValueError("source_audio requires task_type='cover' or 'repaint'")
        if reference_audio is not None and request.task_type != "cover":
            raise ValueError("reference_audio requires task_type='cover'")
        files: dict[str, Path] = {}
        if source_audio is not None:
            files["src_audio"] = _validate_audio_upload(source_audio)
        if reference_audio is not None:
            files["ref_audio"] = _validate_audio_upload(reference_audio)
        if files:
            data = self._request_multipart_json("/release_task", request.to_dict(), files)
        else:
            data = self._request_json("POST", "/release_task", request.to_dict())
        if not isinstance(data, Mapping) or not data.get("task_id"):
            raise AceStepError("ACE-Step submit response did not contain task_id")
        queue = data.get("queue_position")
        return AceStepTask(
            task_id=str(data["task_id"]),
            status=str(data.get("status", "queued")),
            queue_position=int(queue) if queue is not None else None,
        )

    def load_lora(self, lora_path: str, *, adapter_name: str | None = None) -> Mapping[str, Any]:
        config = AceStepLoraConfig(lora_path=lora_path, adapter_name=adapter_name)
        payload = {"lora_path": config.lora_path.strip()}
        if config.adapter_name is not None:
            payload["adapter_name"] = config.adapter_name.strip()
        return self._lora_action("/v1/lora/load", payload, "load")

    def unload_lora(self) -> Mapping[str, Any]:
        return self._lora_action("/v1/lora/unload", {}, "unload")

    def toggle_lora(self, use_lora: bool) -> Mapping[str, Any]:
        return self._lora_action("/v1/lora/toggle", {"use_lora": use_lora}, "toggle")

    def set_lora_scale(
        self,
        scale: float,
        *,
        adapter_name: str | None = None,
    ) -> Mapping[str, Any]:
        if not 0.0 <= scale <= 1.0:
            raise ValueError("LoRA scale must be between 0.0 and 1.0")
        if adapter_name is not None and not adapter_name.strip():
            raise ValueError("LoRA adapter name must not be blank")
        payload: dict[str, Any] = {"scale": scale}
        if adapter_name is not None:
            payload["adapter_name"] = adapter_name.strip()
        return self._lora_action("/v1/lora/scale", payload, "scale")

    def get_lora_status(self) -> AceStepLoraStatus:
        data = self._request_json("GET", "/v1/lora/status")
        if not isinstance(data, Mapping):
            raise AceStepError("ACE-Step LoRA status response must be an object")
        return AceStepLoraStatus.from_dict(data)

    def configure_lora(self, config: AceStepLoraConfig) -> AceStepLoraStatus:
        try:
            self.load_lora(config.lora_path, adapter_name=config.adapter_name)
        except AceStepError as exc:
            # ACE-Step returns HTTP 400 when the same named adapter is already
            # loaded. Reuse it only when runtime status identifies that exact
            # adapter; otherwise preserve the original load failure.
            status = self.get_lora_status()
            expected_adapter = config.adapter_name or _default_lora_adapter_name(config.lora_path)
            if not status.lora_loaded or status.active_adapter != expected_adapter:
                raise exc
        self.set_lora_scale(config.scale, adapter_name=config.adapter_name)
        self.toggle_lora(True)
        status = self.get_lora_status()
        if not status.lora_loaded or not status.use_lora:
            raise AceStepError("ACE-Step did not report the LoRA as loaded and active")
        reported_scale = status.lora_scale
        if config.adapter_name is not None and status.scales:
            reported_scale = status.scales.get(config.adapter_name, reported_scale)
        if abs(reported_scale - config.scale) > 1e-6:
            raise AceStepError(
                f"ACE-Step reported LoRA scale {reported_scale}, expected {config.scale}"
            )
        return status

    def query(self, task_id: str) -> AceStepTaskResult:
        data = self._request_json("POST", "/query_result", {"task_id_list": [task_id]})
        if not isinstance(data, list):
            raise AceStepError("ACE-Step query response data must be a list")
        record = next(
            (item for item in data if isinstance(item, Mapping) and str(item.get("task_id")) == task_id),
            None,
        )
        if record is None:
            raise AceStepError(f"ACE-Step query did not return task {task_id}")
        status = int(record.get("status", 0))
        raw_result = record.get("result")
        outputs: tuple[AceStepOutput, ...] = ()
        # ACE-Step includes a placeholder result with ``file: ""`` while a
        # task is queued or running.  It is not an output record yet, so only
        # enforce the audio URL contract after the task has succeeded.
        if status == 1 and raw_result:
            try:
                decoded = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
            except json.JSONDecodeError as exc:
                raise AceStepError("ACE-Step task result was not valid JSON") from exc
            if not isinstance(decoded, list):
                raise AceStepError("ACE-Step decoded task result must be a list")
            outputs = tuple(AceStepOutput.from_dict(item) for item in decoded if isinstance(item, Mapping))
        return AceStepTaskResult(task_id=task_id, status=status, outputs=outputs)

    def wait(
        self,
        task_id: str,
        *,
        poll_interval: float = 2.0,
        wait_timeout: float = 600.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_poll: Callable[[AceStepTaskResult, float], None] | None = None,
    ) -> AceStepTaskResult:
        """Poll until the task finishes.

        ``on_poll`` is handed each unfinished result and the seconds waited so
        far, so a caller can show progress. A render takes minutes, and a client
        that prints nothing for that long is indistinguishable from a hung one.
        """

        if poll_interval < 0 or wait_timeout <= 0:
            raise ValueError("poll_interval must be non-negative and wait_timeout must be positive")
        started = clock()
        deadline = started + wait_timeout
        while True:
            result = self.query(task_id)
            if result.status == 1:
                if not result.outputs:
                    raise AceStepError("ACE-Step task succeeded without output records")
                return result
            if result.status == 2:
                raise AceStepError(f"ACE-Step task {task_id} failed")
            if on_poll is not None:
                on_poll(result, clock() - started)
            if clock() >= deadline:
                raise AceStepError(f"ACE-Step task {task_id} timed out")
            sleep(poll_interval)

    def download(self, file_url: str, destination: Path) -> None:
        url = urllib.parse.urljoin(self.config.base_url.rstrip("/") + "/", file_url)
        if self._origin(url) != self._origin(self.config.base_url):
            raise AceStepError("refusing an ACE-Step audio URL from a different origin")
        headers = self._headers()
        headers["Accept"] = "audio/*"
        request = urllib.request.Request(url, method="GET", headers=headers)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with self._open(request) as response:
                final_url = response.geturl() if hasattr(response, "geturl") else url
                if self._origin(final_url) != self._origin(self.config.base_url):
                    raise AceStepError("refusing an ACE-Step audio redirect to a different origin")
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{destination.name}-",
                    dir=destination.parent,
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
            if temporary is None or temporary.stat().st_size == 0:
                raise AceStepError("ACE-Step returned an empty audio file")
            os.replace(temporary, destination)
            temporary = None
        except (OSError, urllib.error.URLError) as exc:
            raise AceStepError(f"ACE-Step audio download failed: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _lora_action(
        self,
        path: str,
        payload: Mapping[str, Any],
        action: str,
    ) -> Mapping[str, Any]:
        data = self._request_json("POST", path, payload)
        if not isinstance(data, Mapping) or not str(data.get("message", "")).strip():
            raise AceStepError(f"ACE-Step LoRA {action} response did not contain a message")
        return data

    def _request_json(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._url(path), data=body, method=method, headers=headers)
        return self._execute_json_request(request)

    def _request_multipart_json(
        self,
        path: str,
        fields: Mapping[str, Any],
        files: Mapping[str, Path],
    ) -> Any:
        body, content_type = _encode_multipart(fields, files)
        headers = self._headers()
        headers["Content-Type"] = content_type
        request = urllib.request.Request(
            self._url(path),
            data=body,
            method="POST",
            headers=headers,
        )
        return self._execute_json_request(request)

    def _execute_json_request(self, request: urllib.request.Request) -> Any:
        try:
            with self._open(request) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AceStepError(f"ACE-Step HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise AceStepError(f"ACE-Step connection failed: {exc.reason}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AceStepError(f"ACE-Step response could not be decoded: {exc}") from exc
        if not isinstance(decoded, Mapping):
            raise AceStepError("ACE-Step response root must be an object")
        if int(decoded.get("code", 0)) != 200 or decoded.get("error"):
            message = str(decoded.get("error") or "unknown API error")
            raise AceStepError(f"ACE-Step API error: {message}")
        return decoded.get("data")

    def _open(self, request: urllib.request.Request) -> Any:
        return self._opener(request, timeout=self.config.request_timeout)

    def _url(self, path: str) -> str:
        return urllib.parse.urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port
        if port is None:
            port = 80 if scheme == "http" else 443 if scheme == "https" else None
        return scheme, (parsed.hostname or "").lower(), port


def load_project_spec(project_dir: Path) -> SongSpec:
    path = Path(project_dir) / "song_spec.json"
    if not path.is_file():
        raise FileNotFoundError(f"SongSpec not found: {path}")
    return SongSpec.from_json(path.read_text(encoding="utf-8"))


def resolve_repaint_window(
    spec: SongSpec,
    *,
    section_name: str | None = None,
    bar_range: str | None = None,
    tail_guard_bars: float = 0.0,
) -> AceStepRepaintWindow:
    """Resolve a section name or one-based inclusive bar range to seconds.

    When ``tail_guard_bars`` is set and the window reaches the final bar, the
    end is extended into the guard region so the repaint mask also covers the
    buffer where the model writes its ending (see :mod:`kihachi_music_ai.tail_guard`).
    """

    if (section_name is None) == (bar_range is None):
        raise ValueError("choose exactly one of repaint section or repaint bars")

    selected_section: str | None = None
    if section_name is not None:
        normalized = _normalize_section_name(section_name)
        section = next(
            (
                item
                for item in spec.arrangement
                if _normalize_section_name(item.name) == normalized
            ),
            None,
        )
        if section is None:
            available = ", ".join(item.name for item in spec.arrangement)
            raise ValueError(
                f"unknown repaint section {section_name!r}; available sections: {available}"
            )
        start_bar = section.start_bar + 1
        end_bar = section.start_bar + section.length_bars
        selected_section = section.name
        selector = "section"
    else:
        match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", bar_range or "")
        if match is None:
            raise ValueError("repaint bars must use one-based inclusive START:END format")
        start_bar = int(match.group(1))
        end_bar = int(match.group(2))
        selector = "bars"
        # Record the enclosing section when the range sits inside one, so a
        # bar-level plan still carries the arrangement context it belongs to.
        selected_section = next(
            (
                item.name
                for item in spec.arrangement
                if item.start_bar < start_bar <= item.start_bar + item.length_bars
                and item.start_bar < end_bar <= item.start_bar + item.length_bars
            ),
            None,
        )

    if start_bar < 1 or end_bar < start_bar or end_bar > spec.song.total_bars:
        raise ValueError(
            f"repaint bars must satisfy 1 <= START <= END <= {spec.song.total_bars}"
        )

    bar_seconds = seconds_per_bar(spec)
    guard_sec = guard_seconds(spec, tail_guard_bars)
    start_sec = round((start_bar - 1) * bar_seconds, 3)
    end_sec = min(round(end_bar * bar_seconds, 3), round(spec.song.target_duration_sec, 3))
    if guard_sec > 0.0 and end_bar >= spec.song.total_bars:
        # The model writes its ending at the end of the repaint mask, so the mask
        # has to reach into the guard region for the final bar to stay musical.
        end_sec = round(end_sec + guard_sec, 3)
    if end_sec <= start_sec:
        raise ValueError("resolved repaint range is empty")
    return AceStepRepaintWindow(
        selector=selector,
        start_bar=start_bar,
        end_bar=end_bar,
        start_sec=start_sec,
        end_sec=end_sec,
        section_name=selected_section,
        tail_guard_sec=guard_sec if end_bar >= spec.song.total_bars else 0.0,
    )


def _normalize_section_name(value: str) -> str:
    return re.sub(r"[\s-]+", "_", value.strip().casefold())


def prepare_ace_step_request(
    project_dir: Path,
    options: AceStepOptions | None = None,
    *,
    overwrite: bool = False,
) -> tuple[Path, AceStepGenerationRequest]:
    project_dir = Path(project_dir)
    options = options or AceStepOptions()
    request = AceStepGenerationRequest.from_song_spec(load_project_spec(project_dir), options)
    if options.task_type == "cover":
        filename = (
            "ace_step_cover_revision_request.json"
            if options.revision.strip()
            else "ace_step_cover_request.json"
        )
    elif options.task_type == "repaint":
        filename = (
            "ace_step_repaint_revision_request.json"
            if options.revision.strip()
            else "ace_step_repaint_request.json"
        )
    else:
        filename = "ace_step_revision_request.json" if options.revision.strip() else "ace_step_request.json"
    path = project_dir / filename
    if path.exists() and not overwrite:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != request.to_dict():
            raise FileExistsError(f"refusing to overwrite different ACE-Step request: {path}")
        return path, request
    _atomic_write_text(path, request.to_json())
    return path, request


def render_with_ace_step(
    project_dir: Path,
    client: AceStepClient,
    options: AceStepOptions | None = None,
    *,
    lora: AceStepLoraConfig | None = None,
    source_audio: Path | None = None,
    reference_audio: Path | None = None,
    repaint_selection: AceStepRepaintWindow | None = None,
    overwrite: bool = False,
    poll_interval: float = 2.0,
    wait_timeout: float = 600.0,
    on_poll: Callable[[AceStepTaskResult, float], None] | None = None,
) -> AceStepRenderManifest:
    project_dir = Path(project_dir)
    options = options or AceStepOptions()
    if repaint_selection is not None:
        if options.task_type != "repaint":
            raise ValueError("repaint_selection requires task_type='repaint'")
        if (
            abs(options.repainting_start - repaint_selection.start_sec) > 1e-6
            or options.repainting_end is None
            or abs(options.repainting_end - repaint_selection.end_sec) > 1e-6
        ):
            raise ValueError("repaint_selection must match the ACE-Step repaint time window")
    result_path = project_dir / "ace_step_result.json"
    audio_dir = project_dir / "audio"
    extension = _audio_extension(options.audio_format)
    expected_audio = tuple(
        audio_dir / f"ace-step-{index:02d}.{extension}"
        for index in range(1, options.batch_size + 1)
    )
    guard_bars = validate_guard_bars(options.tail_guard_bars)
    trims_audio = guard_bars > 0.0
    untrimmed_audio = tuple(
        path.with_suffix(f".untrimmed.{extension}") for path in expected_audio
    )
    protected = [
        path
        for path in (result_path, *expected_audio, *(untrimmed_audio if trims_audio else ()))
        if path.exists()
    ]
    if protected and not overwrite:
        names = ", ".join(str(path) for path in protected)
        raise FileExistsError(f"refusing to overwrite ACE-Step render artifacts: {names}")

    request_path, request = prepare_ace_step_request(project_dir, options, overwrite=overwrite)
    lora_status = client.configure_lora(lora) if lora is not None else None
    task = client.submit(
        request,
        source_audio=source_audio,
        reference_audio=reference_audio,
    )
    result = client.wait(
        task.task_id,
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        on_poll=on_poll,
    )
    if len(result.outputs) > len(expected_audio):
        raise AceStepError("ACE-Step returned more audio files than requested")
    spec = load_project_spec(project_dir)
    grid_duration = round(spec.song.target_duration_sec, 3)
    audio_files: list[Path] = []
    trims: list[TrimManifest] = []
    for index, output in enumerate(result.outputs):
        destination = expected_audio[index]
        if trims_audio:
            rendered = untrimmed_audio[index]
            client.download(output.file, rendered)
            trims.append(
                trim_wav_to_duration(
                    rendered,
                    destination,
                    duration_sec=grid_duration,
                    fade_out_sec=DEFAULT_TAIL_FADE_SEC,
                )
            )
        else:
            client.download(output.file, destination)
        audio_files.append(destination)

    result_document = {
        "adapter": ADAPTER_VERSION,
        "task_id": task.task_id,
        "status": result.status,
        "audio_files": [str(path.relative_to(project_dir)) for path in audio_files],
        "outputs": [
            {
                "source_file": output.file,
                "status": output.status,
                "seed_value": output.seed_value,
                "lm_model": output.lm_model,
                "dit_model": output.dit_model,
                "generation_info": output.generation_info,
                "metas": dict(output.metas) if output.metas is not None else None,
            }
            for output in result.outputs
        ],
    }
    if trims_audio:
        result_document["tail_guard"] = {
            "guard_bars": guard_bars,
            "guard_sec": guard_seconds(spec, guard_bars),
            "requested_duration_sec": request.audio_duration,
            "song_grid_duration_sec": grid_duration,
            "trim_fade_out_sec": DEFAULT_TAIL_FADE_SEC,
            "untrimmed_audio_files": [
                str(path.relative_to(project_dir)) for path in untrimmed_audio[: len(trims)]
            ],
            "trims": [trim.to_dict() for trim in trims],
            "delivered_music_end_sec": [
                measure_music_end(path) for path in audio_files
            ],
        }
    if source_audio is not None or reference_audio is not None:
        result_document["conditioning"] = {
            "task_type": request.task_type,
            "audio_cover_strength": request.audio_cover_strength,
            "cover_noise_strength": request.cover_noise_strength,
            "repainting_start": request.repainting_start,
            "repainting_end": request.repainting_end,
            "repaint_mode": request.repaint_mode,
            "repaint_strength": request.repaint_strength,
            "repaint_latent_crossfade_frames": request.repaint_latent_crossfade_frames,
            "repaint_wav_crossfade_sec": request.repaint_wav_crossfade_sec,
            "chunk_mask_mode": request.chunk_mask_mode,
            "source_audio": _audio_provenance(source_audio) if source_audio is not None else None,
            "reference_audio": (
                _audio_provenance(reference_audio) if reference_audio is not None else None
            ),
        }
        if repaint_selection is not None:
            result_document["conditioning"]["repaint_selection"] = repaint_selection.to_dict()
    if lora is not None and lora_status is not None:
        result_document["lora"] = {
            "requested": lora.to_dict(),
            "status": lora_status.to_dict(),
        }
    if options.revision.strip():
        result_document["revision"] = {
            "applied": True,
            "sha256": hashlib.sha256(options.revision.strip().encode("utf-8")).hexdigest(),
            "request_file": str(request_path.relative_to(project_dir)),
        }
    _atomic_write_text(result_path, json.dumps(result_document, ensure_ascii=False, indent=2) + "\n")
    return AceStepRenderManifest(
        project_dir=project_dir,
        task_id=task.task_id,
        request_file=request_path,
        result_file=result_path,
        audio_files=tuple(audio_files),
        lora_status=lora_status,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{path.name}-",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _audio_extension(audio_format: str) -> str:
    return "wav" if audio_format == "wav32" else audio_format


def _default_lora_adapter_name(lora_path: str) -> str:
    path = Path(lora_path.rstrip("/"))
    return path.stem if path.suffix else path.name


def _validate_audio_upload(path: Path) -> Path:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"ACE-Step conditioning audio not found: {path}")
    if path.stat().st_size <= 0:
        raise ValueError(f"ACE-Step conditioning audio is empty: {path}")
    allowed = {".wav", ".flac", ".mp3", ".ogg", ".opus", ".aac", ".m4a"}
    if path.suffix.lower() not in allowed:
        raise ValueError(f"unsupported ACE-Step conditioning audio format: {path.suffix}")
    return path


def _encode_multipart(
    fields: Mapping[str, Any],
    files: Mapping[str, Path],
) -> tuple[bytes, str]:
    boundary = f"kihachi-{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        body.extend(rendered.encode("utf-8"))
        body.extend(b"\r\n")
    for name, raw_path in files.items():
        path = _validate_audio_upload(raw_path)
        filename = path.name.replace('"', "_")
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        body.extend(path.read_bytes())
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _audio_provenance(path: Path) -> dict[str, Any]:
    path = _validate_audio_upload(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {
        "name": path.name,
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
    }
