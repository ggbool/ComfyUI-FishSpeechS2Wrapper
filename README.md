# ComfyUI-FishSpeechS2Wrapper

**ComfyUI 节点插件** — 将 [Fish-Speech](https://github.com/fishaudio/fish-speech) TTS 引擎集成到 ComfyUI 工作流中，提供完整的多角色小说语音合成能力。

## 特性

- **环境完全隔离** — Fish-Speech 运行在独立 `.venv` 中，不污染 ComfyUI Python 环境
- **跨平台** — Windows / Linux 均可使用，配置自动适配
- **自动管理 API 服务** — 节点可自动拉起 / 复用 / 关闭 Fish-Speech API server
- **多角色小说合成工作流** — 角色档案 → 角色库 → 脚本转标签 → 多角色合成，一站式完成
- **灵活配置** — 支持 `config.yaml`、环境变量、节点参数三级配置

## 节点列表

| 节点 | 功能 |
|------|------|
| **环境检查** | 检测 Fish-Speech 安装状态、Python 环境、API 健康 |
| **音色注册** | 用参考音频 + 转写文本注册一个可复用的音色 |
| **文字出声后固化音色** | 先裸文本生成音频，再将该音频反注册为音色 |
| **音色列表** | 查看当前已注册的所有音色 |
| **音色重命名** | 重命名已有音色 |
| **音色删除** | 删除已有音色 |
| **角色档案** | 定义角色（角色名、speaker_id、reference_id） |
| **角色库** | 汇总多个角色档案 |
| **小说脚本转标签** | 将 `角色名：台词` 格式转为 `<\|speaker:x\|>` 标签 |
| **小说多角色合成** | 根据标签脚本 + 角色库，分段合成并拼接最终音频 |

## 安装

### 前置条件

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI)（已安装并可运行）
- [Fish-Speech](https://github.com/fishaudio/fish-speech)（独立安装在任意目录）
- Fish-Speech 模型 checkpoint（如 `s2-pro`）

### 1. 安装插件

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/ggbool/ComfyUI-FishSpeechS2Wrapper.git
```

### 2. 部署 Fish-Speech

#### Linux 一键部署

```bash
cd ComfyUI-FishSpeechS2Wrapper
bash deploy_linux.sh \
  --fish-root ~/fish-speech \
  --comfyui-root /path/to/ComfyUI \
  --api-port 8080
```

脚本会自动：克隆仓库 → 安装系统依赖 → 创建隔离虚拟环境 → 安装 Python 依赖 → 下载模型 → 生成配置文件。

#### Windows 手动部署

1. 将 Fish-Speech 安装到你选择的目录（如 `C:\tools\fish-speech`）
2. 在 Fish-Speech 目录下创建虚拟环境并安装依赖
3. 在插件目录创建 `config.yaml`：

```yaml
fish_root: "C:/tools/fish-speech"
api_url: "http://127.0.0.1:8080"
```

### 3. 配置

插件配置优先级（高 → 低）：

1. **环境变量** — `FISH_SPEECH_ROOT`、`FISH_SPEECH_API_URL` 等
2. **config.yaml** — 插件目录下的用户配置（不入 git）
3. **config.default.yaml** — 随插件分发的默认配置
4. **平台自动检测** — 默认 `~/fish-speech`

支持的环境变量：

| 环境变量 | 说明 |
|---------|------|
| `FISH_SPEECH_ROOT` | Fish-Speech 安装目录 |
| `FISH_SPEECH_VENV_PYTHON` | 虚拟环境 Python 路径 |
| `FISH_SPEECH_API_URL` | API 服务地址 |
| `FISH_SPEECH_MODEL_PATH` | 模型 checkpoint 目录 |
| `FISH_SPEECH_CODEC_PATH` | Codec 权重文件路径 |
| `FISH_SPEECH_HALF_PRECISION` | 半精度推理 (enable/disable) |
| `FISH_SPEECH_MAX_SEQ_LEN` | 最大序列长度 |
| `FISH_SPEECH_STARTUP_TIMEOUT` | API 启动超时秒数 |

## 推荐工作流

### 小说多角色语音合成

1. **音色注册** — 为每个角色用参考音频建立固定 `reference_id`
2. **角色档案** — 录入角色名、speaker_id、reference_id
3. **角色库** — 汇总所有角色
4. **脚本转标签** — 将 `旁白：...` / `男主：...` 转为 `<|speaker:0|>...` / `<|speaker:1|>...`
5. **多角色合成** — 输出最终拼接音频

### 服务策略

| 策略 | 说明 |
|------|------|
| `reuse` | 自动拉起服务，多节点共享，推荐 |
| `oneshot` | 用完即关，节省显存 |
| `manual` | 不管理服务，需手动启动 API |

## 注意事项

- Fish-Speech 当前**不支持仅靠文本描述就创建稳定音色**。稳定音色需要真实参考音频。
- 「文字出声后固化音色」是间接方式，稳定性弱于真人参考音频，生产环境请优先使用真人参考音频。
- 16GB 显存建议开启半精度 (`half_precision: enable`)。

## License

MIT License
