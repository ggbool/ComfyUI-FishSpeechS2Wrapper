import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import torch
import torchaudio
from comfy_api.latest import IO

from .config import cfg, is_windows


_SERVER_PROCESSES: dict[str, dict] = {}
_SERVER_START_LOCK = threading.Lock()


def _log(message: str) -> None:
    print(f"[FishSpeechStudio] {message}", flush=True)


def _safe_read_text(resp) -> str:
    try:
        return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _pick_free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _default_venv_python(fish_root: str) -> str:
    root = Path(fish_root)
    if is_windows():
        return str(root / ".venv" / "Scripts" / "python.exe")
    else:
        return str(root / ".venv" / "bin" / "python")


def _normalize_api_url(api_url: str) -> str:
    text = (api_url or "").strip()
    if not text:
        return f"http://127.0.0.1:{_pick_free_port('127.0.0.1')}"
    if not text.startswith("http://") and not text.startswith("https://"):
        return f"http://{text}"
    return text


def _http_get(url: str, timeout: int = 15, accept: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Accept": accept}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _http_post_json_text(url: str, payload: dict, timeout: int = 60) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _safe_read_text(resp)


def _http_delete_json(url: str, payload: dict, timeout: int = 60) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, _safe_read_text(resp)


def _http_post_bytes(url: str, body: bytes, content_type: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type, "Accept": "*/*"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}: {_safe_read_text(resp)}")
        return resp.read()


def _decode_api_payload(payload: bytes) -> dict:
    if not payload:
        raise RuntimeError("API 返回为空，无法解析。")
    try:
        text = payload.decode("utf-8")
        if text.strip().startswith("{") or text.strip().startswith("["):
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return {"data": data}
    except Exception:
        pass
    try:
        import ormsgpack  # type: ignore

        data = ormsgpack.unpackb(payload)
        if isinstance(data, dict):
            return data
        return {"data": data}
    except Exception as e:
        preview = payload[:120]
        raise RuntimeError(f"API 响应无法解析为 JSON/MessagePack: {e}; 原始前120字节={preview!r}") from e


def _check_health(api_url: str, timeout: int = 3) -> bool:
    try:
        code, _ = _http_get(urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/health"), timeout=timeout)
        return code == 200
    except Exception:
        return False


def _ensure_server(
    fish_root: str,
    venv_python: str,
    api_url: str,
    model_path: str,
    codec_path: str,
    use_half: bool,
    max_seq_len: int,
    startup_timeout: int,
) -> None:
    if _check_health(api_url, timeout=3):
        return

    with _SERVER_START_LOCK:
        # 双重检查：防止并发节点重复拉起同端口子进程。
        if _check_health(api_url, timeout=3):
            return

        existing = _SERVER_PROCESSES.get(api_url)
        if existing:
            proc = existing.get("proc")
            if proc is not None and proc.poll() is None:
                _log(f"复用已存在 API 子进程: {api_url}, pid={proc.pid}")
            else:
                _SERVER_PROCESSES.pop(api_url, None)
                existing = None

        if existing is None:
            parsed = urllib.parse.urlparse(api_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 8080
            listen = f"{host}:{port}"
            cmd = [
                venv_python,
                str(Path(fish_root) / "tools" / "api_server.py"),
                "--listen",
                listen,
                "--max-seq-len",
                str(max(1024, int(max_seq_len))),
                "--llama-checkpoint-path",
                model_path,
                "--decoder-checkpoint-path",
                codec_path,
                "--max-text-length",
                "0",
            ]
            if use_half:
                cmd.append("--half")
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, cwd=fish_root, env=env, creationflags=creationflags)
            _SERVER_PROCESSES[api_url] = {"proc": proc}
            _log(f"已启动 API: {api_url}, pid={proc.pid}")

    # 锁外等待，避免长时间阻塞其他节点。
    start = __import__("time").time()
    while __import__("time").time() - start < startup_timeout:
        if _check_health(api_url, timeout=3):
            return

        meta = _SERVER_PROCESSES.get(api_url)
        proc = meta.get("proc") if meta else None
        if proc is not None:
            code = proc.poll()
            if code is not None:
                # 兼容“重复绑定失败但已有服务在线”的竞争场景。
                if _check_health(api_url, timeout=2):
                    _log(f"检测到子进程退出(code={code})但 API 已在线，按复用处理: {api_url}")
                    _SERVER_PROCESSES.pop(api_url, None)
                    return
                raise RuntimeError(f"Fish-Speech API 子进程启动失败，exit_code={code}。")

        __import__("time").sleep(1)

    raise RuntimeError(f"Fish-Speech API 启动超时: {api_url}")


def _stop_server(api_url: str) -> None:
    meta = _SERVER_PROCESSES.pop(api_url, None)
    if not meta:
        return
    proc = meta.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _encode_multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----FishStudio{__import__('time').time_ns()}"
    chunks: list[bytes] = []
    for k, v in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        chunks.append(str(v).encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, (filename, file_bytes, mime) in files.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode())
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(file_bytes)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _validate_reference_id(value: str, field_name: str) -> str:
    text = (value or "").strip()
    if not text:
        raise RuntimeError(f"{field_name} 不能为空。")
    if re.match(r"^<\|speaker:\d+\|>$", text):
        raise RuntimeError(f"{field_name} 不能填写角色标签 `<|speaker:x|>`。")
    if not re.match(r"^[a-zA-Z0-9\-_ ]+$", text):
        raise RuntimeError(f"{field_name} 只能包含英文、数字、空格、横线、下划线。")
    return text


def _add_reference(api_url: str, reference_id: str, audio_path: str, reference_text: str, timeout: int) -> None:
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    body, content_type = _encode_multipart(
        {"id": reference_id, "text": reference_text},
        {"audio": (Path(audio_path).name, audio_bytes, "audio/wav")},
    )
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/references/add")
    try:
        _http_post_bytes(url, body, content_type, timeout=timeout)
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 409:
            return
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"添加音色失败 HTTP {getattr(e, 'code', '?')}: {detail}") from e


def _list_references(api_url: str, timeout: int = 30) -> list[str]:
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/references/list")
    code, payload = _http_get(url, timeout=timeout)
    if code < 200 or code >= 300:
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = repr(payload[:120])
        raise RuntimeError(f"获取音色列表失败 HTTP {code}: {text}")
    data = _decode_api_payload(payload)
    refs = data.get("reference_ids") or []
    if not isinstance(refs, list):
        raise RuntimeError(f"音色列表字段 reference_ids 格式错误: {data}")
    return [str(x) for x in refs]


def _delete_reference(api_url: str, reference_id: str, timeout: int = 30) -> str:
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/references/delete")
    try:
        _, text = _http_delete_json(url, {"reference_id": reference_id}, timeout=timeout)
        return text
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"删除音色失败 HTTP {getattr(e, 'code', '?')}: {detail}") from e


def _rename_reference(api_url: str, old_reference_id: str, new_reference_id: str, timeout: int = 30) -> str:
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/references/update")
    try:
        _, text = _http_post_json_text(url, {"old_reference_id": old_reference_id, "new_reference_id": new_reference_id}, timeout=timeout)
        return text
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        raise RuntimeError(f"重命名音色失败 HTTP {getattr(e, 'code', '?')}: {detail}") from e


def _request_tts(api_url: str, payload: dict, timeout: int = 600) -> bytes:
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", "v1/tts")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "*/*"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"HTTP {resp.status}: {_safe_read_text(resp)}")
        return resp.read()


def _decode_audio_bytes(audio_bytes: bytes, fmt: str) -> dict:
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as f:
        tmp = f.name
        f.write(audio_bytes)
    try:
        waveform, sample_rate = torchaudio.load(tmp)
        return {"waveform": waveform.unsqueeze(0).to(torch.float32), "sample_rate": int(sample_rate)}
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _parse_tagged_dialogue(text: str) -> list[dict]:
    pattern = r"(<\|speaker:(\d+)\|>)"
    parts = re.split(pattern, text or "")
    segments: list[dict] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if token and token.startswith("<|speaker:"):
            speaker = parts[i + 1] if i + 1 < len(parts) else "0"
            content = (parts[i + 2] if i + 2 < len(parts) else "").strip()
            if content:
                segments.append({"speaker": str(speaker), "text": content})
            i += 3
        else:
            i += 1
    return segments


def _coalesce_segments(segments: list[dict], max_chars: int = 220) -> list[dict]:
    merged: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        speaker = str(seg.get("speaker", "0"))
        if not text:
            continue
        if merged and merged[-1]["speaker"] == speaker and len(merged[-1]["text"]) + len(text) <= max_chars:
            merged[-1]["text"] += text
        else:
            merged.append({"speaker": speaker, "text": text})
    return merged


def _safe_json_loads(text: str, fallback):
    try:
        return json.loads(text)
    except Exception:
        return fallback


class FishSpeechStudioEnvironmentCheck(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioEnvironmentCheck",
            display_name="Fish Speech Studio 环境检查",
            category="audio/fish_speech_studio",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default=c.api_url, tooltip="Fish-Speech API 地址。"),
            ],
            outputs=[IO.String.Output(display_name="status")],
        )

    @classmethod
    def execute(cls, fish_root, venv_python, api_url):
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        lines = [
            f"fish_root={fish_root}",
            f"venv_python={py}",
            f"api_url={api_url}",
            f"repo_exists={Path(fish_root).exists()}",
            f"python_exists={py.exists()}",
            f"api_health={_check_health(api_url, timeout=3)}",
        ]
        return IO.NodeOutput("\n".join(lines))


class FishSpeechStudioReferenceRegister(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioReferenceRegister",
            display_name="Fish Speech Studio 音色注册",
            category="audio/fish_speech_studio/references",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.String.Input("reference_id", default="hero_male", tooltip="音色 ID。建议 narrator / hero_male / heroine 等。"),
                IO.String.Input("reference_audio_path", default="", tooltip="角色参考音频路径。当前源码不支持只靠文本稳定建声，所以这里是必填。"),
                IO.String.Input("reference_text", default="", tooltip="参考音频的准确转写文本。不能只写角色描述。"),
                IO.Combo.Input("if_exists", options=["reuse", "error", "rename_then_create"], default="reuse", tooltip="同名存在时怎么处理。"),
                IO.String.Input("rename_suffix", default="_backup", tooltip="旧音色自动备份后缀。"),
                IO.Int.Input("request_timeout", default=120, min=30, max=600, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[
                IO.String.Output(display_name="reference_id"),
                IO.String.Output(display_name="status"),
            ],
        )

    @classmethod
    def execute(
        cls,
        fish_root,
        venv_python,
        api_url,
        server_strategy,
        startup_timeout,
        model_path,
        codec_path,
        half_precision,
        max_seq_len,
        reference_id,
        reference_audio_path,
        reference_text,
        if_exists,
        rename_suffix,
        request_timeout,
    ):
        ref_id = _validate_reference_id(reference_id, "reference_id")
        ref_audio = (reference_audio_path or "").strip()
        ref_text = (reference_text or "").strip()
        if not ref_audio:
            raise RuntimeError("reference_audio_path 不能为空。")
        if not Path(ref_audio).exists():
            raise RuntimeError(f"reference_audio_path 不存在: {ref_audio}")
        if not ref_text:
            raise RuntimeError("reference_text 不能为空，并且必须与音频内容一致。")
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        if not py.exists():
            raise RuntimeError(f"隔离 Python 不存在: {py}")
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")
        existing = _list_references(norm, timeout=int(request_timeout))
        lines = []
        if ref_id in existing:
            if if_exists == "reuse":
                return IO.NodeOutput(ref_id, f"音色 `{ref_id}` 已存在，直接复用")
            if if_exists == "error":
                raise RuntimeError(f"音色 `{ref_id}` 已存在。")
            backup = _validate_reference_id(ref_id + (rename_suffix or "_backup"), "backup_reference_id")
            _rename_reference(norm, ref_id, backup, timeout=int(request_timeout))
            lines.append(f"旧音色已改名为 `{backup}`")
        _add_reference(norm, ref_id, ref_audio, ref_text, timeout=int(request_timeout))
        lines.append(f"新音色已注册成功: `{ref_id}`")
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput(ref_id, "\n".join(lines))


class FishSpeechStudioBootstrapReferenceFromText(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioBootstrapReferenceFromText",
            display_name="Fish Speech Studio 文字出声后固化音色",
            category="audio/fish_speech_studio/references",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.String.Input("reference_id", default="hero_bootstrap", tooltip="要固化成的音色 ID。"),
                IO.String.Input("seed_text", multiline=True, default="你好，我将作为一个稳定角色长期出现。", tooltip="先让模型裸文本出一段音频，再把这段音频反注册成新的 reference_id。建议使用 1~3 句、语气稳定、没有夸张情绪的文本。"),
                IO.String.Input("reference_text", default="你好，我将作为一个稳定角色长期出现。", tooltip="注册音色时写入的参考文本。默认建议与 seed_text 相同。"),
                IO.Combo.Input("if_exists", options=["reuse", "error", "rename_then_create"], default="reuse", tooltip="同名已存在时怎么处理。"),
                IO.String.Input("rename_suffix", default="_backup", tooltip="旧音色自动备份后缀。"),
                IO.Combo.Input("format", options=["wav", "mp3"], default="wav", tooltip="第一次裸文本出声的临时音频格式。推荐 wav。"),
                IO.Int.Input("hard_token_cap", default=220, min=64, max=1024, step=8, tooltip="第一次裸文本出声的 token 上限。建议不要太长。"),
                IO.Int.Input("chunk_length", default=140, min=50, max=500, step=10, tooltip="第一次裸文本出声的 chunk 长度。建议 100~180。"),
                IO.Float.Input("top_p", default=0.7, min=0.1, max=1.0, step=0.01, tooltip="第一次裸文本出声的采样多样性。建议偏稳。"),
                IO.Float.Input("repetition_penalty", default=1.15, min=1.0, max=2.0, step=0.01, tooltip="第一次裸文本出声的重复惩罚。"),
                IO.Float.Input("temperature", default=0.65, min=0.1, max=1.5, step=0.01, tooltip="第一次裸文本出声的温度。建议偏低，减少漂移。"),
                IO.Combo.Input("use_memory_cache", options=["enable", "disable"], default="enable", tooltip="缓存设置。"),
                IO.Int.Input("seed", default=42, min=-1, max=2147483647, tooltip="第一次裸文本出声的随机种子。固定值更利于复现。"),
                IO.Int.Input("request_timeout", default=600, min=30, max=3600, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[
                IO.String.Output(display_name="reference_id"),
                IO.Audio.Output(display_name="bootstrap_audio"),
                IO.String.Output(display_name="status"),
            ],
        )

    @classmethod
    def execute(
        cls,
        fish_root,
        venv_python,
        api_url,
        server_strategy,
        startup_timeout,
        model_path,
        codec_path,
        half_precision,
        max_seq_len,
        reference_id,
        seed_text,
        reference_text,
        if_exists,
        rename_suffix,
        format,
        hard_token_cap,
        chunk_length,
        top_p,
        repetition_penalty,
        temperature,
        use_memory_cache,
        seed,
        request_timeout,
    ):
        ref_id = _validate_reference_id(reference_id, "reference_id")
        bootstrap_text = (seed_text or "").strip()
        if not bootstrap_text:
            raise RuntimeError("seed_text 不能为空。")
        ref_text = (reference_text or bootstrap_text).strip()
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        if not py.exists():
            raise RuntimeError(f"隔离 Python 不存在: {py}")
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")

        existing = _list_references(norm, timeout=int(request_timeout))
        status = []
        if ref_id in existing:
            if if_exists == "reuse":
                raise RuntimeError(f"音色 `{ref_id}` 已存在。bootstrap 模式建议避免直接 reuse，因为你需要看到新生成的 bootstrap_audio。")
            if if_exists == "error":
                raise RuntimeError(f"音色 `{ref_id}` 已存在。")
            backup = _validate_reference_id(ref_id + (rename_suffix or "_backup"), "backup_reference_id")
            _rename_reference(norm, ref_id, backup, timeout=int(request_timeout))
            status.append(f"旧音色已改名为 `{backup}`")

        payload = {
            "text": bootstrap_text,
            "references": [],
            "reference_id": None,
            "format": format,
            "max_new_tokens": int(hard_token_cap),
            "chunk_length": int(chunk_length),
            "top_p": float(top_p),
            "repetition_penalty": float(repetition_penalty),
            "temperature": float(temperature),
            "streaming": False,
            "use_memory_cache": "on" if use_memory_cache == "enable" else "off",
            "seed": None if int(seed) < 0 else int(seed),
        }
        audio_bytes = _request_tts(norm, payload, timeout=int(request_timeout))
        audio = _decode_audio_bytes(audio_bytes, format)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_wav:
            temp_path = temp_wav.name
        try:
            torchaudio.save(temp_path, audio["waveform"][0], int(audio["sample_rate"]))
            _add_reference(norm, ref_id, temp_path, ref_text, timeout=int(request_timeout))
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass
        status.append(f"已先用纯文本生成一段 bootstrap 音频，再固化为 reference_id=`{ref_id}`")
        status.append("注意：这种方式是‘间接建声’，可用但稳定性通常弱于真人参考音频建库，商业项目请优先使用真人参考音频。")
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput(ref_id, audio, "\n".join(status))


class FishSpeechStudioReferenceList(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioReferenceList",
            display_name="Fish Speech Studio 音色列表",
            category="audio/fish_speech_studio/references",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.Int.Input("request_timeout", default=60, min=10, max=300, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[IO.String.Output(display_name="reference_ids_json"), IO.String.Output(display_name="preview")],
        )

    @classmethod
    def execute(cls, fish_root, venv_python, api_url, server_strategy, startup_timeout, model_path, codec_path, half_precision, max_seq_len, request_timeout):
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")
        refs = _list_references(norm, timeout=int(request_timeout))
        preview = "\n".join([f"[{i}] {x}" for i, x in enumerate(refs)]) if refs else "(当前没有已注册音色)"
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput(json.dumps(refs, ensure_ascii=False), preview)


class FishSpeechStudioReferenceDelete(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioReferenceDelete",
            display_name="Fish Speech Studio 音色删除",
            category="audio/fish_speech_studio/references",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.String.Input("reference_id", default="", tooltip="要删除的音色 ID。"),
                IO.Int.Input("request_timeout", default=60, min=10, max=300, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[IO.String.Output(display_name="status")],
        )

    @classmethod
    def execute(cls, fish_root, venv_python, api_url, server_strategy, startup_timeout, model_path, codec_path, half_precision, max_seq_len, reference_id, request_timeout):
        ref_id = _validate_reference_id(reference_id, "reference_id")
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")
        result = _delete_reference(norm, ref_id, timeout=int(request_timeout))
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput(f"音色 `{ref_id}` 已删除。\n原始响应: {result}")


class FishSpeechStudioReferenceRename(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioReferenceRename",
            display_name="Fish Speech Studio 音色重命名",
            category="audio/fish_speech_studio/references",
            inputs=[
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.String.Input("old_reference_id", default="", tooltip="旧音色 ID。"),
                IO.String.Input("new_reference_id", default="", tooltip="新音色 ID。"),
                IO.Int.Input("request_timeout", default=60, min=10, max=300, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[IO.String.Output(display_name="new_reference_id"), IO.String.Output(display_name="status")],
        )

    @classmethod
    def execute(cls, fish_root, venv_python, api_url, server_strategy, startup_timeout, model_path, codec_path, half_precision, max_seq_len, old_reference_id, new_reference_id, request_timeout):
        old_id = _validate_reference_id(old_reference_id, "old_reference_id")
        new_id = _validate_reference_id(new_reference_id, "new_reference_id")
        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")
        result = _rename_reference(norm, old_id, new_id, timeout=int(request_timeout))
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput(new_id, f"音色已从 `{old_id}` 改名为 `{new_id}`。\n原始响应: {result}")


class FishSpeechStudioCharacterProfile(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="FishSpeechStudioCharacterProfile",
            display_name="Fish Speech Studio 角色档案",
            category="audio/fish_speech_studio/workflow",
            inputs=[
                IO.String.Input("character_name", default="旁白", tooltip="角色名。用于小说脚本中的角色识别。"),
                IO.Int.Input("speaker_id", default=0, min=0, max=9, tooltip="脚本里的 speaker 编号。"),
                IO.String.Input("reference_id", default="", tooltip="真实音色 ID。可以为空；为空时只保存角色设定，不保证固定声音。"),
                IO.String.Input("style_notes", multiline=True, default="成熟、平稳、叙述感", tooltip="纯文本角色设定备注。注意：这不是稳定建声依据，只是管理信息。"),
            ],
            outputs=[IO.String.Output(display_name="character_json"), IO.String.Output(display_name="preview")],
        )

    @classmethod
    def execute(cls, character_name, speaker_id, reference_id, style_notes):
        payload = {
            "character_name": (character_name or "").strip(),
            "speaker_id": int(speaker_id),
            "reference_id": (reference_id or "").strip(),
            "style_notes": (style_notes or "").strip(),
        }
        if not payload["character_name"]:
            raise RuntimeError("character_name 不能为空")
        preview = (
            f"角色={payload['character_name']} | speaker={payload['speaker_id']} | "
            f"reference_id={payload['reference_id'] or '未绑定'} | "
            f"备注={payload['style_notes'] or '无'}"
        )
        return IO.NodeOutput(json.dumps(payload, ensure_ascii=False), preview)


class FishSpeechStudioCharacterLibrary(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="FishSpeechStudioCharacterLibrary",
            display_name="Fish Speech Studio 角色库",
            category="audio/fish_speech_studio/workflow",
            inputs=[
                IO.String.Input("profile_1", default="", tooltip="角色档案 1 JSON。"),
                IO.String.Input("profile_2", default="", tooltip="角色档案 2 JSON。"),
                IO.String.Input("profile_3", default="", tooltip="角色档案 3 JSON。"),
                IO.String.Input("profile_4", default="", tooltip="角色档案 4 JSON。"),
                IO.String.Input("profile_5", default="", tooltip="角色档案 5 JSON。"),
                IO.String.Input("profile_6", default="", tooltip="角色档案 6 JSON。"),
            ],
            outputs=[IO.String.Output(display_name="library_json"), IO.String.Output(display_name="preview")],
        )

    @classmethod
    def execute(cls, profile_1, profile_2, profile_3, profile_4, profile_5, profile_6):
        profiles = []
        name_to_speaker = {}
        speaker_to_reference = {}
        for raw in [profile_1, profile_2, profile_3, profile_4, profile_5, profile_6]:
            data = _safe_json_loads(raw or "", None)
            if not isinstance(data, dict):
                continue
            name = str(data.get("character_name") or "").strip()
            if not name:
                continue
            speaker = str(int(data.get("speaker_id", 0)))
            ref_id = str(data.get("reference_id") or "").strip()
            profiles.append(data)
            name_to_speaker[name] = speaker
            if ref_id:
                speaker_to_reference[speaker] = ref_id
        payload = {
            "profiles": profiles,
            "name_to_speaker": name_to_speaker,
            "speaker_to_reference": speaker_to_reference,
        }
        preview = "\n".join(
            [f"{p.get('character_name')} -> speaker:{p.get('speaker_id')} -> {p.get('reference_id') or '未绑定'}" for p in profiles]
        ) or "(角色库为空)"
        return IO.NodeOutput(json.dumps(payload, ensure_ascii=False), preview)


class FishSpeechStudioNovelScriptFormatter(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="FishSpeechStudioNovelScriptFormatter",
            display_name="Fish Speech Studio 小说脚本转标签",
            category="audio/fish_speech_studio/workflow",
            inputs=[
                IO.String.Input("raw_script", multiline=True, default="旁白：夜幕降临。\n男主：我来了。", tooltip="原始小说脚本。支持 `角色名：台词` 格式。"),
                IO.String.Input("library_json", default="", tooltip="角色库 JSON。用于把角色名自动转成 speaker 标签。"),
                IO.Combo.Input("script_mode", options=["name_colon_text", "already_tagged"], default="name_colon_text", tooltip="name_colon_text=角色名：台词；already_tagged=文本已经写成 <|speaker:x|>。"),
            ],
            outputs=[IO.String.Output(display_name="tagged_script"), IO.String.Output(display_name="preview")],
        )

    @classmethod
    def execute(cls, raw_script, library_json, script_mode):
        text = (raw_script or "").strip()
        if not text:
            raise RuntimeError("raw_script 不能为空")
        if script_mode == "already_tagged":
            if not re.search(r"<\|speaker:\d+\|>", text):
                raise RuntimeError("already_tagged 模式下，脚本必须包含 `<|speaker:x|>` 标签。")
            preview = "脚本已是标签格式，直接输出"
            return IO.NodeOutput(text, preview)
        library = _safe_json_loads(library_json or "", {})
        name_to_speaker = library.get("name_to_speaker") or {}
        lines = []
        previews = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^([^：:]+)[：:](.+)$", line)
            if not m:
                raise RuntimeError(f"脚本行格式错误：{line}。请使用 `角色名：台词`。")
            name = m.group(1).strip()
            content = m.group(2).strip()
            if name not in name_to_speaker:
                raise RuntimeError(f"角色 `{name}` 没有在角色库里配置 speaker_id。")
            speaker = name_to_speaker[name]
            lines.append(f"<|speaker:{speaker}|>{content}")
            previews.append(f"{name} -> speaker:{speaker} | {content[:30]}")
        return IO.NodeOutput("\n".join(lines), "\n".join(previews))


class FishSpeechStudioNovelSynthesize(IO.ComfyNode):
    @classmethod
    def define_schema(cls):
        c = cfg()
        return IO.Schema(
            node_id="FishSpeechStudioNovelSynthesize",
            display_name="Fish Speech Studio 小说多角色合成",
            category="audio/fish_speech_studio/workflow",
            inputs=[
                IO.String.Input("tagged_script", multiline=True, default="<|speaker:0|>这是旁白。", tooltip="带 `<|speaker:x|>` 标签的脚本。"),
                IO.String.Input("library_json", default="", tooltip="角色库 JSON，用于把 speaker 映射到真实 reference_id。"),
                IO.String.Input("fish_root", default=c.fish_root, tooltip="Fish-Speech 独立目录。"),
                IO.String.Input("venv_python", default="", tooltip="隔离 Python，留空自动推断。"),
                IO.String.Input("api_url", default="", tooltip="API 地址，留空自动分配。"),
                IO.Combo.Input("server_strategy", options=["reuse", "oneshot", "manual"], default="reuse", tooltip="服务策略。"),
                IO.Int.Input("startup_timeout", default=c.startup_timeout, min=30, max=1800, step=10, tooltip="服务启动超时秒数。"),
                IO.String.Input("model_path", default=c.model_path, tooltip="主模型目录。"),
                IO.String.Input("codec_path", default=c.codec_path, tooltip="codec 权重路径。"),
                IO.Combo.Input("half_precision", options=["enable", "disable"], default=c.half_precision, tooltip="16GB 显存建议开启。"),
                IO.Int.Input("max_seq_len", default=c.max_seq_len, min=512, max=32768, step=256, tooltip="服务上下文长度。"),
                IO.Combo.Input("format", options=["wav", "flac", "mp3"], default="wav", tooltip="输出格式。"),
                IO.Combo.Input("strict_consistency", options=["enable", "disable"], default="enable", tooltip="开启后，没有真实音色 ID 的角色会直接报错。"),
                IO.Combo.Input("allow_text_only_profile", options=["disable", "enable"], default="disable", tooltip="如果启用，允许没有 reference_id 的角色直接裸文本合成，但不保证固定声音。"),
                IO.Int.Input("hard_token_cap", default=480, min=64, max=2048, step=16, tooltip="token 安全上限。"),
                IO.Int.Input("chunk_length", default=160, min=50, max=500, step=10, tooltip="内部切分长度。"),
                IO.Float.Input("top_p", default=0.7, min=0.1, max=1.0, step=0.01, tooltip="采样多样性。"),
                IO.Float.Input("repetition_penalty", default=1.15, min=1.0, max=2.0, step=0.01, tooltip="重复惩罚。"),
                IO.Float.Input("temperature", default=0.7, min=0.1, max=1.5, step=0.01, tooltip="采样温度。"),
                IO.Combo.Input("use_memory_cache", options=["enable", "disable"], default="enable", tooltip="重复使用音色缓存。"),
                IO.Int.Input("seed", default=-1, min=-1, max=2147483647, tooltip="随机种子。"),
                IO.Int.Input("segment_gap_ms", default=100, min=0, max=500, step=10, tooltip="段间静音。"),
                IO.Int.Input("request_timeout", default=600, min=30, max=3600, step=10, tooltip="请求超时秒数。"),
            ],
            outputs=[IO.Audio.Output(display_name="audio"), IO.String.Output(display_name="status")],
        )

    @classmethod
    def execute(
        cls,
        tagged_script,
        library_json,
        fish_root,
        venv_python,
        api_url,
        server_strategy,
        startup_timeout,
        model_path,
        codec_path,
        half_precision,
        max_seq_len,
        format,
        strict_consistency,
        allow_text_only_profile,
        hard_token_cap,
        chunk_length,
        top_p,
        repetition_penalty,
        temperature,
        use_memory_cache,
        seed,
        segment_gap_ms,
        request_timeout,
    ):
        script = (tagged_script or "").strip()
        if not script:
            raise RuntimeError("tagged_script 不能为空")
        library = _safe_json_loads(library_json or "", {})
        speaker_to_reference = {str(k): str(v) for k, v in (library.get("speaker_to_reference") or {}).items()}
        segments = _coalesce_segments(_parse_tagged_dialogue(script), max_chars=int(chunk_length))
        if not segments:
            raise RuntimeError("没有解析到有效脚本段。")

        py = Path(venv_python) if (venv_python or "").strip() else Path(_default_venv_python(fish_root))
        norm = _normalize_api_url(api_url)
        if server_strategy in ("reuse", "oneshot"):
            _ensure_server(fish_root, str(py), norm, model_path, codec_path, half_precision == "enable", max_seq_len, startup_timeout)
        elif not _check_health(norm, timeout=3):
            raise RuntimeError("manual 模式下 API 未在线")

        sr = None
        waveforms = []
        status = []
        for idx, seg in enumerate(segments):
            speaker = str(seg.get("speaker", "0"))
            text = (seg.get("text") or "").strip()
            ref_id = (speaker_to_reference.get(speaker) or "").strip()
            if not ref_id and strict_consistency == "enable" and allow_text_only_profile == "disable":
                raise RuntimeError(
                    f"speaker:{speaker} 没有绑定真实 reference_id。当前 Fish-Speech 不能只靠文本稳定建声。"
                )
            payload = {
                "text": text,
                "references": [],
                "reference_id": ref_id if ref_id else None,
                "format": format,
                "max_new_tokens": int(hard_token_cap),
                "chunk_length": int(chunk_length),
                "top_p": float(top_p),
                "repetition_penalty": float(repetition_penalty),
                "temperature": float(temperature),
                "streaming": False,
                "use_memory_cache": "on" if use_memory_cache == "enable" else "off",
                "seed": None if int(seed) < 0 else int(seed),
            }
            audio_bytes = _request_tts(norm, payload, timeout=int(request_timeout))
            audio = _decode_audio_bytes(audio_bytes, format)
            if sr is None:
                sr = int(audio["sample_rate"])
            if int(audio["sample_rate"]) != sr:
                raise RuntimeError("不同段落采样率不一致，无法拼接")
            waveforms.append(audio["waveform"])
            status.append(
                f"seg#{idx} speaker={speaker} reference_id={ref_id or 'none'} chars={len(text)} stable={'yes' if ref_id else 'no'}"
            )
        if not waveforms:
            raise RuntimeError("没有生成任何音频")
        gap = max(0, int(segment_gap_ms))
        if gap > 0 and len(waveforms) > 1:
            z = torch.zeros((1, 1, int(sr * gap / 1000.0)), dtype=waveforms[0].dtype)
            merged = []
            for i, w in enumerate(waveforms):
                merged.append(w)
                if i < len(waveforms) - 1:
                    merged.append(z)
            final = torch.cat(merged, dim=2)
        else:
            final = torch.cat(waveforms, dim=2)
        if server_strategy == "oneshot":
            _stop_server(norm)
        return IO.NodeOutput({"waveform": final.to(torch.float32), "sample_rate": int(sr)}, "\n".join(status))