# Compus-AI-Agent

面向校园教学场景的生物统计学 AI Agent 原型。项目同时包含老师端和学生端能力：老师可以生成课件、试卷、课后小测、查看知识库和班级薄弱点；学生可以进行知识答疑、自由练习、模拟题训练和自动评分。

当前系统支持三种生成路径：

1. `AI_Agent.yml` 导出的 Dify Chatflow。
2. 读取 `AI_Agent.yml` 中提示词，再调用 OpenAI-compatible 模型接口。
3. 本地规则模板兜底，用于离线调试页面、权限和导出流程。

## 项目结构

```text
.
├── AI_Agent.yml                    # 老师/学生主 Chatflow 配置
├── Bio_Agent.yml                   # 生物统计学问答/出题工作流配置
├── AI_Agent.py                     # 调用 Dify Workflow API 的独立脚本
├── assets/ppt_images/              # 课件图片资产库和来源 manifest
├── templates/beamer/format.tex     # LaTeX Beamer 课件格式模板
├── teacher_agent/                  # 老师端/学生端核心应用代码
├── examples/                       # 班级画像、学生画像、题库、知识库种子数据
├── resources/                      # 知识库文本资源
├── data/pdfs/                      # 示例 PDF 资料
├── scripts/smoke_test.py           # 基础冒烟测试
├── setup_env.sh                    # 本地 Python 虚拟环境初始化脚本
└── TEACHER_AGENT_README.md         # 更详细的老师端使用说明
```

## 核心模块说明

`teacher_agent/web_app.py`

本地 Web 服务入口，默认运行在 `http://127.0.0.1:8765`。它提供会话页面、老师/学生角色切换、答题页面、学生自由练习页面、Markdown 预览、文件访问权限检查，以及代码题运行/语法检查接口。

`teacher_agent/conversation_agent.py`

会话调度核心。负责识别中文意图，例如生成 PPT、生成试卷、抽题、提交答案、查知识库、阅读 PDF、学生薄弱点总结和普通答疑。该模块会按优先级尝试 `AI_Agent.yml + 模型接口`、Dify Chatflow、本地模板。

`teacher_agent/chatflow_client.py`

Dify Chatflow 调用适配器。它封装 `/v1/chat-messages` 请求，自动构造 `inputs`，并把本地角色映射到 yml 中的权限变量：

```text
student -> power = 0
teacher -> power = 1
```

同时会传入 `user_role`、`user_id`、`session_id`，方便后续在 Dify 中做个性化、权限审计和多轮上下文。

`teacher_agent/yml_llm_client.py`

本地读取 `AI_Agent.yml` 的提示词节点，并调用 OpenAI-compatible Chat Completions 接口。默认模型是 `deepseek-chat`，也可以通过环境变量切换模型和 API 地址。

`teacher_agent/ppt_module.py`

课件生成模块。根据课程主题、班级画像、学习目标生成 LaTeX Beamer 源文件、PDF 预览和最终 `.pptx` 课件；会按章节自动匹配本地图片资产。

`teacher_agent/exam_module.py`

试卷生成模块。生成 LaTeX 试卷源码；如果本地安装了 `tectonic`、`xelatex` 或 `pdflatex`，会自动编译为 PDF。

`teacher_agent/quiz_module.py`

可评分题库模块。支持题库初始化、老师套题、学生自由抽题、答案提交、自动评分和评分报告导出。当前支持选择题、计算题、问答题和代码题。

`teacher_agent/knowledge_db.py`

本地知识库模块。使用 SQLite 管理教材、论文、网页和 PDF 条目，支持导入 PDF、抽取页面文本、按条目或页面搜索。

`teacher_agent/student_practice_module.py`

学生个性化练习模块。根据 `examples/student_profiles.json` 中的学生薄弱点生成练习内容。

`teacher_agent/session_store.py`

本地会话存储模块。保存会话、消息、角色、用户 ID 和 Dify 返回的 `conversation_id`。

`teacher_agent/permissions.py`

权限控制模块。限制学生访问老师正式试卷等教师侧能力，并控制生成文件的可见范围。

`teacher_agent/cli.py`

命令行入口。可用于生成课件、生成试卷、维护知识库、初始化题库、抽题和提交评分。

## 课件图片资产库

课件生成会按主题自动选择 `assets/ppt_images/manifest.json` 中登记的图片资产：

- `logistic_curve`：用于 Logistic 回归、二分类结局建模、S 型概率曲线。
- `benjamini_hochberg`：用于多重检验校正、FDR、BH procedure。
- `dna_double_helix`：用于基因组数据、生物医学统计应用和高通量检测背景。

图片优先来自 Wikimedia Commons 等可追踪来源，并在 manifest 中保留来源链接和用途说明。

## YML 与接口代码

### `AI_Agent.yml`

主 Chatflow 配置，用于老师端和学生端统一入口。当前通过 `power` 区分角色分支：

```text
power = 0  -> 学生分支
power != 0 -> 老师分支
```

本地代码会自动把角色映射为 `power`，并额外传入：

```text
user_role
user_id
session_id
```

这些字段用于权限判断、学生个性化、日志追踪和多轮会话。

### `Bio_Agent.yml`

生物统计学专项工作流配置，偏向独立问答和出题工作流。配套脚本是 `AI_Agent.py`。

### `AI_Agent.py`

独立调用 Dify Workflow API 的脚本，适合测试 `Bio_Agent.yml` 这类 Workflow。默认读取：

```bash
export DIFY_API_KEY="你的 Workflow Key"
export DIFY_API_URL="http://localhost/v1/workflows/run"
```

调用示例：

```bash
python AI_Agent.py "帮我出一份关于配对 t 检验的简答题"
```

### `teacher_agent/chatflow_client.py`

用于调用 Dify Chatflow API，默认接口是：

```text
http://localhost/v1/chat-messages
```

线上 Dify 可配置为：

```bash
export DIFY_CHATFLOW_API_KEY="你的 Chatflow Key"
export DIFY_CHATFLOW_API_URL="https://api.dify.ai/v1/chat-messages"
```

### `teacher_agent/yml_llm_client.py`

用于“不经过 Dify 服务、直接复用 `AI_Agent.yml` 提示词”的路径。配置方式：

```bash
export LLM_API_KEY="你的模型 Key"
export LLM_API_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-chat"
```

如果同时配置了 `AI_Agent.yml + 模型 API` 和 Dify Chatflow，本地会话会优先尝试本地 yml 提示词调用，再尝试 Dify Chatflow。

## 快速开始

初始化环境：

```bash
chmod +x setup_env.sh
./setup_env.sh
source .venv/bin/activate
```

启动 Web 应用：

```bash
python -m teacher_agent.web_app
```

打开浏览器：

```text
http://127.0.0.1:8765
```

初始化知识库和题库：

```bash
python -m teacher_agent.cli kb init
python -m teacher_agent.cli kb seed --file examples/knowledge_seed.json
python -m teacher_agent.cli quiz init
```

## 命令行示例

生成课件：

```bash
python -m teacher_agent.cli \
  --profile examples/class_profile.json \
  ppt \
  --topic "基因组数据中的多重检验校正" \
  --slide-count 10 \
  --objective "理解多重检验问题的来源" \
  --objective "区分 FWER 和 FDR"
```

生成试卷：

```bash
python -m teacher_agent.cli \
  --profile examples/class_profile.json \
  exam \
  --title "生物统计学阶段测验一" \
  --topic "假设检验" \
  --topic "多重检验校正" \
  --points 100
```

开始学生自由练习：

```bash
python -m teacher_agent.cli quiz start \
  --mode student \
  --user-role student \
  --user-id student_demo \
  --module "Logistic 回归" \
  --count 3
```

提交答案并自动评分：

```bash
python -m teacher_agent.cli quiz submit \
  --attempt-id "上一步输出的 attempt_id" \
  --answers-file examples/quiz_answers_lesson_fdr.json
```

## 环境变量

常用变量如下：

```bash
# Dify Chatflow
export DIFY_CHATFLOW_API_KEY="你的 Chatflow Key"
export DIFY_CHATFLOW_API_URL="https://api.dify.ai/v1/chat-messages"

# Dify Workflow
export DIFY_API_KEY="你的 Workflow Key"
export DIFY_API_URL="http://localhost/v1/workflows/run"

# OpenAI-compatible LLM
export LLM_API_KEY="你的模型 Key"
export LLM_API_BASE_URL="https://api.deepseek.com"
export LLM_MODEL="deepseek-chat"
```

生成类任务默认要求 AI/Dify 参与。如果只是离线测试页面、权限和导出流程，可以临时允许本地模板兜底：

```bash
export AGENT_ALLOW_LOCAL_FALLBACK=1
```

## 输出文件

运行后生成的课件、试卷、练习和评分报告会写入 `outputs/`。该目录属于运行产物，默认不会提交到 Git。

本地 SQLite 数据库位于 `data/*.db`，也不会提交到 Git。需要复现数据时，请使用 `examples/` 中的种子文件重新初始化。

## 更多说明

更完整的老师端操作说明、Web 页面说明、权限规则、PDF 导入和题库维护命令见：

```text
TEACHER_AGENT_README.md
```
