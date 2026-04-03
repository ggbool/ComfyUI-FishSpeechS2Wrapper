#!/usr/bin/env bash
# ============================================================
# deploy_linux.sh — Linux 一键部署 Fish-Speech + ComfyUI_FishSpeechStudio
#
# 用法:
#   bash deploy_linux.sh [--fish-root /path/to/fish-speech] \
#                        [--comfyui-root /path/to/comfyui]   \
#                        [--model s2-pro]                     \
#                        [--api-port 8080]                    \
#                        [--skip-model-download]
#
# 功能:
#   1. 克隆 / 更新 fish-speech 仓库
#   2. 安装系统依赖（portaudio-dev 等）
#   3. 在 fish-speech 目录下创建独立 .venv（与 ComfyUI 隔离）
#   4. 安装 fish-speech 依赖（支持 uv 加速，fallback pip，pyaudio 失败可跳过）
#   5. 下载模型 checkpoint（可选跳过）
#   6. 为 ComfyUI_FishSpeechStudio 生成 config.yaml
# ============================================================

set -euo pipefail

# ---- 默认参数 ----
FISH_ROOT="${HOME}/fish-speech"
COMFYUI_ROOT=""
FISH_SPEECH_REPO="https://github.com/fishaudio/fish-speech.git"
FISH_SPEECH_BRANCH="main"
MODEL_NAME="s2-pro"
API_PORT="8080"
SKIP_MODEL_DOWNLOAD=false
PYTHON_CMD="python3"

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ---- 参数解析 ----
while [[ $# -gt 0 ]]; do
    case $1 in
        --fish-root)        FISH_ROOT="$2"; shift 2;;
        --comfyui-root)     COMFYUI_ROOT="$2"; shift 2;;
        --repo)             FISH_SPEECH_REPO="$2"; shift 2;;
        --branch)           FISH_SPEECH_BRANCH="$2"; shift 2;;
        --model)            MODEL_NAME="$2"; shift 2;;
        --api-port)         API_PORT="$2"; shift 2;;
        --skip-model-download) SKIP_MODEL_DOWNLOAD=true; shift;;
        --python)           PYTHON_CMD="$2"; shift 2;;
        -h|--help)
            head -20 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0;;
        *) log_error "未知参数: $1"; exit 1;;
    esac
done

# ---- 检查基础依赖 ----
check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        log_error "缺少命令: $1，请先安装。"
        exit 1
    fi
}

check_cmd git
check_cmd "$PYTHON_CMD"

PYTHON_VERSION=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
log_info "系统 Python: $PYTHON_CMD ($PYTHON_VERSION)"

# 检查 Python >= 3.10
MAJOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.major)")
MINOR=$("$PYTHON_CMD" -c "import sys; print(sys.version_info.minor)")
if [[ "$MAJOR" -lt 3 ]] || [[ "$MAJOR" -eq 3 && "$MINOR" -lt 10 ]]; then
    log_error "需要 Python >= 3.10，当前为 $PYTHON_VERSION"
    exit 1
fi

# ============================================================
# 1. 克隆 / 更新 fish-speech
# ============================================================
log_info "Fish-Speech 目录: $FISH_ROOT"

if [[ -d "$FISH_ROOT/.git" ]]; then
    log_info "Fish-Speech 仓库已存在，拉取更新..."
    cd "$FISH_ROOT"
    git fetch origin
    git checkout "$FISH_SPEECH_BRANCH" 2>/dev/null || git checkout -b "$FISH_SPEECH_BRANCH" "origin/$FISH_SPEECH_BRANCH"
    git pull origin "$FISH_SPEECH_BRANCH" || log_warn "git pull 失败，继续使用本地版本"
else
    log_info "克隆 Fish-Speech 仓库..."
    git clone --branch "$FISH_SPEECH_BRANCH" "$FISH_SPEECH_REPO" "$FISH_ROOT"
    cd "$FISH_ROOT"
fi

# ============================================================
# 2. 安装系统级依赖（需要 root 或 sudo）
# ============================================================
install_sys_deps() {
    local pkgs=(portaudio19-dev libsndfile1-dev build-essential)
    if command -v apt-get &>/dev/null; then
        local missing=()
        for pkg in "${pkgs[@]}"; do
            if ! dpkg -s "$pkg" &>/dev/null; then
                missing+=("$pkg")
            fi
        done
        if [[ ${#missing[@]} -gt 0 ]]; then
            log_info "安装系统依赖: ${missing[*]}"
            if [[ $(id -u) -eq 0 ]]; then
                apt-get update -qq && apt-get install -y -qq "${missing[@]}"
            else
                sudo apt-get update -qq && sudo apt-get install -y -qq "${missing[@]}"
            fi
        else
            log_info "系统依赖已满足"
        fi
    elif command -v yum &>/dev/null; then
        log_info "检测到 yum，安装 portaudio-devel libsndfile-devel..."
        if [[ $(id -u) -eq 0 ]]; then
            yum install -y portaudio-devel libsndfile-devel gcc
        else
            sudo yum install -y portaudio-devel libsndfile-devel gcc
        fi
    else
        log_warn "无法检测包管理器，跳过系统依赖安装"
        log_warn "如果 pyaudio 编译失败，请手动安装 portaudio 开发库"
    fi
}

install_sys_deps

# ============================================================
# 3. 创建独立虚拟环境
# ============================================================
VENV_DIR="$FISH_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ ! -f "$VENV_PYTHON" ]]; then
    log_info "创建虚拟环境: $VENV_DIR"
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

log_info "虚拟环境 Python: $VENV_PYTHON"
"$VENV_PYTHON" --version

# ============================================================
# 4. 安装依赖
# ============================================================
install_deps() {
    cd "$FISH_ROOT"

    # 优先用 uv（更快），fallback pip
    if command -v uv &>/dev/null; then
        log_info "使用 uv 安装依赖..."
        uv pip install --python "$VENV_PYTHON" -e "." 2>&1 || {
            log_warn "uv 完整安装失败，尝试跳过 pyaudio..."
            # pyaudio 仅用于 WebUI 麦克风录音，API server 模式不需要
            uv pip install --python "$VENV_PYTHON" -e "." --exclude-newer "" 2>&1 || {
                log_warn "uv fallback 也失败，切换到 pip..."
                _pip_install
            }
        }
    else
        _pip_install
    fi
}

_pip_install() {
    cd "$FISH_ROOT"
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -e "." 2>&1 || {
        log_warn "pip 完整安装失败（可能是 pyaudio），尝试排除 pyaudio 后安装..."
        # 先安装除 pyaudio 外的所有依赖
        "$VENV_PYTHON" -m pip install -e "." --no-deps
        "$VENV_PYTHON" -c "
import tomllib, subprocess, sys
with open('pyproject.toml', 'rb') as f:
    deps = tomllib.load(f)['project']['dependencies']
skip = {'pyaudio'}
to_install = [d for d in deps if not any(s in d.lower() for s in skip)]
print(f'Installing {len(to_install)} deps (skipping pyaudio)...')
subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + to_install)
"
        log_warn "pyaudio 已跳过。API server 模式不受影响。"
        log_warn "如需 WebUI 麦克风功能，请先安装 portaudio: apt-get install portaudio19-dev"
    }
}

install_deps

# 验证关键导入
log_info "验证 fish_speech 可导入..."
"$VENV_PYTHON" -c "import fish_speech; print(f'fish_speech version: {getattr(fish_speech, \"__version__\", \"ok\")}')" || {
    log_error "fish_speech 导入失败，请检查安装日志。"
    exit 1
}

# ============================================================
# 5. 下载模型（可选）
# ============================================================
CHECKPOINT_DIR="$FISH_ROOT/checkpoints/$MODEL_NAME"

if [[ "$SKIP_MODEL_DOWNLOAD" == true ]]; then
    log_warn "跳过模型下载（--skip-model-download）"
elif [[ -d "$CHECKPOINT_DIR" ]] && [[ -n "$(ls -A "$CHECKPOINT_DIR" 2>/dev/null)" ]]; then
    log_info "模型已存在: $CHECKPOINT_DIR，跳过下载"
else
    log_info "下载模型到 $CHECKPOINT_DIR ..."
    mkdir -p "$CHECKPOINT_DIR"

    # 使用 huggingface-cli（fish-speech 依赖已包含 transformers/huggingface_hub）
    "$VENV_PYTHON" -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='fishaudio/fish-speech-1.5',
    local_dir='$CHECKPOINT_DIR',
    local_dir_use_symlinks=False,
)
print('模型下载完成')
" 2>&1 || {
        log_warn "自动下载模型失败。请手动下载模型到 $CHECKPOINT_DIR"
        log_warn "  huggingface-cli download fishaudio/fish-speech-1.5 --local-dir $CHECKPOINT_DIR"
    }
fi

# ============================================================
# 6. 生成 ComfyUI_FishSpeechStudio config.yaml
# ============================================================
generate_config() {
    local plugin_dir="$1"
    local config_file="$plugin_dir/config.yaml"

    if [[ -f "$config_file" ]]; then
        log_warn "config.yaml 已存在: $config_file，跳过生成"
        log_warn "如需重新生成，请先删除该文件"
        return
    fi

    log_info "生成配置: $config_file"
    cat > "$config_file" <<EOF
# ============================================================
# FishSpeechStudio Linux 配置（由 deploy_linux.sh 自动生成）
# ============================================================

fish_root: "$FISH_ROOT"
api_url: "http://127.0.0.1:${API_PORT}"
model_path: "$CHECKPOINT_DIR"
codec_path: "$CHECKPOINT_DIR/codec.pth"
half_precision: "enable"
max_seq_len: 3072
startup_timeout: 300
EOF
    log_info "配置文件已生成"
}

# 如果指定了 ComfyUI 路径，尝试找到插件目录
if [[ -n "$COMFYUI_ROOT" ]]; then
    CUSTOM_NODES_DIR="$COMFYUI_ROOT/custom_nodes"

    if [[ ! -d "$CUSTOM_NODES_DIR" ]]; then
        log_error "ComfyUI custom_nodes 目录不存在: $CUSTOM_NODES_DIR"
        log_error "请确认 --comfyui-root 指向正确的 ComfyUI 安装目录"
        exit 1
    fi

    # 支持两种目录名：git clone 默认名 和 手动重命名
    PLUGIN_DIR=""
    for candidate in "ComfyUI-FishSpeechS2Wrapper" "ComfyUI_FishSpeechStudio"; do
        if [[ -d "$CUSTOM_NODES_DIR/$candidate" ]]; then
            PLUGIN_DIR="$CUSTOM_NODES_DIR/$candidate"
            break
        fi
    done

    # 插件不存在则自动 clone
    if [[ -z "$PLUGIN_DIR" ]]; then
        PLUGIN_DIR="$CUSTOM_NODES_DIR/ComfyUI-FishSpeechS2Wrapper"
        log_info "插件未安装，自动 clone 到: $PLUGIN_DIR"
        git clone https://github.com/ggbool/ComfyUI-FishSpeechS2Wrapper.git "$PLUGIN_DIR"
    else
        # 已存在则尝试更新
        log_info "插件已存在: $PLUGIN_DIR，拉取更新..."
        cd "$PLUGIN_DIR"
        git pull origin main 2>/dev/null || log_warn "插件 git pull 失败，继续使用本地版本"
    fi

    generate_config "$PLUGIN_DIR"
else
    log_info "未指定 --comfyui-root，跳过 config.yaml 生成"
    log_info "稍后手动运行:"
    log_info "  bash deploy_linux.sh --comfyui-root /path/to/comfyui --fish-root $FISH_ROOT"
fi

# ============================================================
# 7. 输出摘要
# ============================================================
echo ""
echo "============================================================"
log_info "部署完成！"
echo "============================================================"
echo ""
echo "  Fish-Speech 根目录:  $FISH_ROOT"
echo "  虚拟环境 Python:     $VENV_PYTHON"
echo "  模型目录:            $CHECKPOINT_DIR"
echo ""
echo "  手动启动 API 服务:"
echo "    $VENV_PYTHON $FISH_ROOT/tools/api_server.py \\"
echo "      --listen 0.0.0.0:${API_PORT} \\"
echo "      --llama-checkpoint-path $CHECKPOINT_DIR \\"
echo "      --decoder-checkpoint-path $CHECKPOINT_DIR/codec.pth \\"
echo "      --half --max-seq-len 3072"
echo ""
echo "  或通过 ComfyUI 节点自动管理（server_strategy=reuse）"
echo ""
echo "  环境变量配置方式（替代 config.yaml）:"
echo "    export FISH_SPEECH_ROOT=$FISH_ROOT"
echo "    export FISH_SPEECH_API_URL=http://127.0.0.1:${API_PORT}"
echo ""
