"""
FishSpeechStudio 配置管理模块。

配置优先级（高 → 低）：
  1. 环境变量  FISH_SPEECH_ROOT / FISH_SPEECH_VENV_PYTHON / ...
  2. 用户配置  config.yaml（插件目录下，git-ignored）
  3. 默认配置  config.default.yaml（随插件分发）
  4. 平台自动检测
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# YAML 加载 — 尝试用 PyYAML，若不可用则退回纯 JSON 格式的 .yaml
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        pass
    # fallback: 尝试 JSON（config.yaml 也可以是纯 JSON 格式）
    import json
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 平台检测
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    return sys.platform == "win32" or os.name == "nt"


def _detect_venv_python(fish_root: str) -> str:
    """根据平台推断 fish-speech 虚拟环境中的 python 路径。"""
    root = Path(fish_root)
    if is_windows():
        return str(root / ".venv" / "Scripts" / "python.exe")
    else:
        return str(root / ".venv" / "bin" / "python")


def _detect_fish_root() -> str:
    """
    平台默认的 fish-speech 安装位置。
    - Windows: ~/fish-speech
    - Linux:   ~/fish-speech
    可通过 config.yaml 或环境变量 FISH_SPEECH_ROOT 覆盖。
    """
    return str(Path.home() / "fish-speech")


# ---------------------------------------------------------------------------
# 环境变量映射
# ---------------------------------------------------------------------------

_ENV_MAP: dict[str, str] = {
    "fish_root":        "FISH_SPEECH_ROOT",
    "venv_python":      "FISH_SPEECH_VENV_PYTHON",
    "api_url":          "FISH_SPEECH_API_URL",
    "model_path":       "FISH_SPEECH_MODEL_PATH",
    "codec_path":       "FISH_SPEECH_CODEC_PATH",
    "half_precision":   "FISH_SPEECH_HALF_PRECISION",
    "max_seq_len":      "FISH_SPEECH_MAX_SEQ_LEN",
    "startup_timeout":  "FISH_SPEECH_STARTUP_TIMEOUT",
}


# ---------------------------------------------------------------------------
# 配置类
# ---------------------------------------------------------------------------

class FishSpeechConfig:
    _instance: FishSpeechConfig | None = None

    def __init__(self) -> None:
        self._default_cfg = _load_yaml(_PLUGIN_DIR / "config.default.yaml")
        self._user_cfg = _load_yaml(_PLUGIN_DIR / "config.yaml")

    @classmethod
    def get(cls) -> FishSpeechConfig:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reload(cls) -> FishSpeechConfig:
        """强制重新加载配置（用于运行时热更新）。"""
        cls._instance = cls()
        return cls._instance

    def _resolve(self, key: str) -> Any:
        """优先级：环境变量 > config.yaml > config.default.yaml"""
        env_var = _ENV_MAP.get(key)
        if env_var:
            val = os.environ.get(env_var)
            if val is not None and val.strip():
                return val.strip()
        if key in self._user_cfg:
            return self._user_cfg[key]
        if key in self._default_cfg:
            return self._default_cfg[key]
        return None

    # ---- 具体配置项 ----

    @property
    def fish_root(self) -> str:
        return str(self._resolve("fish_root") or _detect_fish_root())

    @property
    def venv_python(self) -> str:
        val = self._resolve("venv_python")
        if val:
            return str(val)
        return _detect_venv_python(self.fish_root)

    @property
    def api_url(self) -> str:
        return str(self._resolve("api_url") or "http://127.0.0.1:8080")

    @property
    def model_path(self) -> str:
        val = self._resolve("model_path")
        if val:
            return str(val)
        return str(Path(self.fish_root) / "checkpoints" / "s2-pro")

    @property
    def codec_path(self) -> str:
        val = self._resolve("codec_path")
        if val:
            return str(val)
        return str(Path(self.fish_root) / "checkpoints" / "s2-pro" / "codec.pth")

    @property
    def half_precision(self) -> str:
        return str(self._resolve("half_precision") or "enable")

    @property
    def max_seq_len(self) -> int:
        val = self._resolve("max_seq_len")
        try:
            return int(val)
        except (TypeError, ValueError):
            return 3072

    @property
    def startup_timeout(self) -> int:
        val = self._resolve("startup_timeout")
        try:
            return int(val)
        except (TypeError, ValueError):
            return 240


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def cfg() -> FishSpeechConfig:
    return FishSpeechConfig.get()
