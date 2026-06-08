# 老师端 Agent 模块

当前老师端先做两个模块：

1. 课件生成：根据章节主题、班级薄弱点、教学目标生成 Markdown 课件大纲，并在安装 `python-pptx` 后导出 PPTX。
2. 试卷生成：根据章节主题、班级薄弱点生成 LaTeX 试卷源码，并在系统安装 LaTeX 编译器后导出 PDF。
3. 可评分习题：从题库中抽题，区分老师课后/期中/期末套题和学生自由练习，学生提交后自动评分。

## 环境

```bash
chmod +x setup_env.sh
./setup_env.sh
source .venv/bin/activate
```

如果你用的是 fish shell，请使用：

```fish
source .venv/bin/activate.fish
```

如果要把 `.tex` 编译成 PDF，还需要安装 LaTeX 编译器。推荐先用较轻量的 `tectonic`：

```bash
brew install tectonic
```

确认：

```bash
which tectonic
```

如果后续需要更完整的 TeX Live 生态，再考虑 BasicTeX 或 MacTeX。

## 生成课件

```bash
python -m teacher_agent.cli \
  --profile examples/class_profile.json \
  ppt \
  --topic "基因组数据中的多重检验校正" \
  --slide-count 10 \
  --objective "理解多重检验问题的来源" \
  --objective "区分 FWER 和 FDR" \
  --objective "能够解释调整后 p 值"
```

输出：

- `outputs/基因组数据中的多重检验校正_课件.md`
- 如果装了 `python-pptx`，同时输出 `.pptx`

## 生成试卷

```bash
python -m teacher_agent.cli \
  --profile examples/class_profile.json \
  exam \
  --title "生物统计学阶段测验一" \
  --topic "假设检验" \
  --topic "多重检验校正" \
  --topic "Logistic 回归" \
  --duration 90 \
  --points 100
```

输出：

- `outputs/生物统计学阶段测验一.tex`
- 如果系统安装了 `tectonic`、`xelatex` 或 `pdflatex`，同时输出 `.pdf`

没有 LaTeX 编译器时不会报错，会先保留 `.tex`。

## 可评分习题和自动评分

题库种子文件：

```text
examples/question_bank_seed.json
```

默认 SQLite 数据库：

```text
data/quiz_bank.db
```

初始化题库：

```bash
python -m teacher_agent.cli quiz init
```

列出老师套题：

```bash
python -m teacher_agent.cli quiz sets --audience teacher
```

老师端习题数量保持少而清楚，按教学模块和使用场景区分：

- `lesson_fdr`：多重检验校正课后小测
- `lesson_logistic`：Logistic 回归课后小测
- `midterm_biostat`：生物统计学期中测验
- `final_biostat`：生物统计学期末综合测验

开启老师套题：

```bash
python -m teacher_agent.cli quiz start \
  --mode teacher \
  --user-role teacher \
  --user-id teacher_demo \
  --set-id lesson_fdr
```

学生自由练习可以按模块、题型、数量抽题：

```bash
python -m teacher_agent.cli quiz start \
  --mode student \
  --user-role student \
  --user-id student_demo \
  --module "Logistic 回归" \
  --count 3
```

也可以只抽某类题：

```bash
python -m teacher_agent.cli quiz start \
  --mode student \
  --user-role student \
  --user-id student_demo \
  --module "Logistic 回归" \
  --type code \
  --count 1
```

提交答案并评分：

```bash
python -m teacher_agent.cli quiz submit \
  --attempt-id "上一步输出的 attempt_id" \
  --answers-file examples/quiz_answers_lesson_fdr.json
```

`answers.json` 的键是题目 ID，例如：

```json
{
  "q_fdr_mc_001": "B",
  "q_fdr_short_001": "因为多重检验会增加假阳性，需要用 FDR 校正并根据调整后 p 值判断显著性。"
}
```

当前评分策略：

- 选择题：标准答案精确评分。
- 计算题：数值答案按允许误差评分。
- 问答题：按 rubric 和同义表达组给分。语序不同不影响评分；口语表达只要覆盖核心概念或同义说法，也会得分。
- 代码题：按关键代码结构和 rubric 给分，不在本地执行学生代码。

后续如果要接入 Dify/LLM，可以把问答题和代码题的 `rule_rubric` 评分替换成“rubric + 学生答案 + 标准解释”的 AI 评分节点。

启动 Web 服务后，也可以打开交互式答题页：

```text
http://127.0.0.1:8765/quiz?attempt_id=上一步输出的_attempt_id
```

学生刷题也有独立入口：

```text
http://127.0.0.1:8765/practice
```

这个页面提供筛选器：

- 学生 ID
- 模块：综合随机、多重检验校正、Logistic 回归、研究设计与 t 检验、卡方检验、遗传学统计
- 题型：不限、选择题、问答题、计算题、代码题
- 题目数量

点击“生成随机习题”后会自动创建一次学生练习并跳转到交互式答题页。

交互式卷面会按题型显示不同控件：

- 选择题：单选框。
- 计算题：数值输入框。
- 问答题：文本框。
- 代码题：代码输入框。

学生点击“确认本题”或“确认并评分”后，页面会在题目下方显示 `CORRECT`、`PARTIAL` 或 `ERROR`，并展示得分、反馈和题目解析。

代码题输入框支持一些类 VS Code 的轻量编辑能力：

- 深色编辑器样式和行号。
- `Tab`：缩进；选中多行时批量缩进。
- `Shift + Tab`：反缩进。
- `Enter`：继承上一行缩进，遇到 `{`、`(`、`[`、`:` 自动加一级缩进。
- 括号和引号自动补全。
- 删除空括号时会成对删除；在括号内输入右括号会跳出括号。
- `Ctrl + /` 或 `Command + /`：切换行注释。
- R 题中输入 `gl` 或 `glm` 后按 `Tab`，可补全 `glm(y ~ age + sex + exposure, data = df, family = binomial)`。
- 点击“运行代码”会像在线刷题平台一样运行当前代码，并显示 stdout、stderr、退出码和是否超时。
- 点击“提交本题”或页面顶部“上传并评分”才会把答案提交给评分器。
- Python/R 运行时会注入题目所需的少量示例数据；运行超时时间为 5 秒。

## 后续接入方向

1. 把 `examples/class_profile.json` 换成数据库读取结果。
2. 把 `build_slide_plan` 和 `build_exam_questions` 的规则生成，升级成 Dify/LLM 生成。
3. 把知识库检索结果作为额外上下文传入 PPT/试卷模块。
4. 生成后让老师确认、编辑、评分，再把反馈写回题库和班级画像。

## 本地会话式前端

启动前端：

```bash
source .venv/bin/activate
python -m teacher_agent.web_app
```

fish shell：

```fish
source .venv/bin/activate.fish
python -m teacher_agent.web_app
```

打开：

```text
http://127.0.0.1:8765
```

前端支持：

- 新建会话
- 删除会话
- 老师/学生角色切换
- 中文输入任务
- 自动判断生成课件、生成试卷、查知识库、阅读 PDF、学生模拟题
- 可评分题库抽题和提交答案评分

权限规则：

- 老师可以生成课件、正式试卷、查阅知识库、阅读 PDF。
- 学生可以查阅公开知识库、阅读资料、生成基于自己薄弱点的模拟练习。
- 学生不能生成或读取老师用于正式考试的试卷。
- 生成文件通过 `/api/sessions/{session_id}/artifact` 访问，并按会话角色检查权限。
- 学生会话只能打开自己生成的练习文件，不能通过猜路径下载老师试卷。

后续 Dify Chatflow 接口已经预留在 `teacher_agent/chatflow_client.py`。配置环境变量后即可接入：

```bash
export DIFY_CHATFLOW_API_KEY="你的 Chatflow Key"
export DIFY_CHATFLOW_API_URL="https://api.dify.ai/v1/chat-messages"
```

fish shell：

```fish
set -x DIFY_CHATFLOW_API_KEY "你的 Chatflow Key"
set -x DIFY_CHATFLOW_API_URL "https://api.dify.ai/v1/chat-messages"
```

当前本地路由会先判断权限和任务类型；需要 AI 生成的课件、试卷、学生练习、自由问答会调用 Dify Chatflow。没有配置 Chatflow Key 时，会返回明确错误，不会用固定模板伪装成 AI。

生成类任务现在也是 Dify 优先：

```text
生成 PPT / 正式试卷 / 学生模拟题
-> 先调用 AI_Agent.yml 对应的 Dify Chatflow
-> 成功则保存 Dify 返回内容
-> 失败或未配置 Key 时，停止并提示配置 Dify
```

返回消息会明确标明是否“通过 Dify Chatflow”生成。

默认就是 Dify 必须参与。也可以显式打开强制模式：

```fish
set -x AGENT_REQUIRE_DIFY 1
python -m teacher_agent.web_app
```

只要 Dify 没配置或调用失败，就会直接报出原因，不会偷偷生成模板内容。

如果只是离线调试页面、权限、PPTX/PDF 导出，可以临时打开本地模板兜底：

```fish
set -x AGENT_ALLOW_LOCAL_FALLBACK 1
python -m teacher_agent.web_app
```

这个模式只建议开发测试时使用，因为内容生成不代表 AI 参与。

本地会话会保存 Dify 返回的 `conversation_id`。同一个网页会话里继续提问时，Agent 会把这个 `conversation_id` 带回 Dify，从而支持 Chatflow 的多轮上下文。

当前 `AI_Agent.yml` 是 Dify Chatflow，使用 `power` 区分角色：

- `power = 0`：学生分支
- `power != 0`：老师分支

本地 Agent 会自动映射：

```text
student -> power = 0
teacher -> power = 1
```

我也在 yml 开始节点补了 `user_role`、`user_id`、`session_id`，用于后续个性化和权限审计。

另外已经检查并修正 `AI_Agent.yml` 的几个关键点：

- 课件、试卷、学生答疑、学生练习、班级分析节点已启用知识检索上下文。
- 直接查知识库的回答节点改为输出检索结果，而不是 `sys.files`。
- LLM 系统提示已补充老师/学生角色、权限边界和输出格式要求。

## 手动维护知识库

初始化数据库并导入基础资源：

```bash
python -m teacher_agent.cli kb init
python -m teacher_agent.cli kb seed --file examples/knowledge_seed.json
```

查看、搜索、详情：

```bash
python -m teacher_agent.cli kb list --limit 20
python -m teacher_agent.cli kb search "基因组"
python -m teacher_agent.cli kb show "条目ID"
```

新增和删除元数据条目：

```bash
python -m teacher_agent.cli kb add \
  --title "某本教材" \
  --type textbook \
  --language zh \
  --topic "假设检验" \
  --url "https://example.com/book.pdf" \
  --summary "教材说明"

python -m teacher_agent.cli kb delete "条目ID"
```

## 导入和阅读 PDF

导入本地 PDF：

```bash
python -m teacher_agent.cli kb import-pdf \
  --title "生物统计学教材" \
  --file "/path/to/book.pdf" \
  --type textbook \
  --language zh \
  --topic "假设检验" \
  --topic "回归分析"
```

导入远程 PDF：

```bash
python -m teacher_agent.cli kb import-pdf \
  --title "OpenIntro Biostatistics" \
  --url "https://example.com/book.pdf" \
  --type textbook \
  --language en \
  --topic "biostatistics"
```

按页阅读和搜索 PDF 页面：

```bash
python -m teacher_agent.cli kb read "条目ID" --page 1 --pages 3
python -m teacher_agent.cli kb search-pages "Logistic"
```

PDF 会保存到 `data/pdfs/`，抽取后的页面文本会保存到 SQLite 的 `knowledge_pages` 表。部分扫描版 PDF 或复杂中文字体 PDF 可能需要 OCR 才能得到高质量文本。
