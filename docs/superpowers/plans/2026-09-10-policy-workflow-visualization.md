# Online Policy Workflow Graph Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 FastAPI 中提供可交互、可溯源的在线办事流程图谱页面。

**Architecture:** 新建 `policy.workflow_view`，以纯函数生成页面 HTML、样式和浏览器端绘图逻辑。`api.py` 只注册页面路由；浏览器从既有 JSON 图谱接口取数，页面不写入图谱事实或数据库。

**Tech Stack:** Python 3.10+、FastAPI、httpx ASGITransport、原生 HTML/CSS/JavaScript、ECharts 5.5.1 CDN。

**Spec:** `docs/superpowers/specs/2026-09-10-policy-workflow-visualization-design.md`

## Global Constraints

- 不修改 `policy/workflow_graph.py` 的导出 JSON 格式、人工审核逻辑或数据库结构。
- 不新增 npm、Python 依赖或前端构建流程。
- 页面通过同源 `/policy-workflow-graph/{run_id}` 请求数据；所有新增界面文本和代码注释使用中文。
- `run_id` 只能以 HTML 属性转义后的形式出现在页面中；请求 URL 必须使用 `encodeURIComponent`。
- 页面必须处理加载中、空图、HTTP 错误、非 JSON 错误和 ECharts CDN 未加载的情况。

## 文件结构

- Create: `policy/workflow_view.py` — 生成只读的图谱 HTML，不访问数据库。
- Modify: `api.py` — 注册 `GET /policy-workflow-view/{run_id}` 并返回 HTML。
- Create: `tests/test_policy_workflow_view.py` — 覆盖 HTML 转义、页面路由、数据接口地址。
- Modify: `README.md` — 说明页面 URL、前置审核条件和使用方式。

---

### Task 1: 可测试的图谱页面渲染器

**Files:**
- Create: `policy/workflow_view.py`
- Create: `tests/test_policy_workflow_view.py`

**Interfaces:**
- Consumes: 字符串 `run_id`。
- Produces: `render_policy_workflow_view(run_id: str) -> str`，返回完整 HTML 文档。
- Depends on: Python 标准库 `html`；不依赖 FastAPI、数据库或图谱构建函数。

- [ ] **Step 1: 写入渲染器失败测试**

在 `tests/test_policy_workflow_view.py` 设置现有 API 测试所需的服务环境变量，并添加以下测试：

```python
def test_rendered_view_escapes_run_id_and_uses_same_origin_graph_api(self) -> None:
    page = render_policy_workflow_view('run_1"><img src=x onerror=alert(1)>')
    self.assertIn('data-run-id="run_1&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"', page)
    self.assertNotIn('data-run-id="run_1"><img', page)
    self.assertIn("/policy-workflow-graph/' + encodeURIComponent(runId)", page)
```

- [ ] **Step 2: 运行测试，确认缺少模块而失败**

Run: `python -m unittest tests.test_policy_workflow_view -v`

Expected: FAIL，提示 `ModuleNotFoundError: No module named 'policy.workflow_view'`。

- [ ] **Step 3: 实现最小页面渲染器**

在 `policy/workflow_view.py` 定义下列纯函数。使用 `html.escape(run_id, quote=True)` 填充 `data-run-id`，不要把运行 ID 拼接到 JavaScript 代码中：

```python
from __future__ import annotations
from html import escape


def render_policy_workflow_view(run_id: str) -> str:
    """生成只读的办事流程图谱页面。"""
    escaped_run_id = escape(run_id, quote=True)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>办事流程图谱</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script></head>
<body data-run-id="{escaped_run_id}"><main>
<header><h1>办事流程图谱</h1><p id="summary">正在加载图谱…</p></header>
<section id="filters" aria-label="节点类型筛选"></section>
<section id="graph" aria-label="流程图谱"></section>
<aside id="detail">点击节点或关系可查看制度原文证据。</aside>
</main></body></html>'''
```

- [ ] **Step 4: 补齐浏览器端交互逻辑**

在 Task 1 的 HTML 内新增样式和脚本，并实现以下确定行为：

```javascript
const runId = document.body.dataset.runId;
const endpoint = '/policy-workflow-graph/' + encodeURIComponent(runId);
const typeColor = { process: '#1d4ed8', step: '#0f766e', role: '#7c3aed', material: '#b45309', condition: '#be123c', outcome: '#047857' };
async function loadGraph() {
  const response = await fetch(endpoint);
  let graph;
  try {
    graph = await response.json();
  } catch {
    throw new Error('服务返回内容不是图谱 JSON');
  }
  if (!response.ok) throw new Error(graph.detail || `HTTP ${response.status}`);
  return graph;
}
```

从 `graph.nodes` 收集类型、生成筛选按钮，并用 `Set` 保存当前可见类型。筛选变化后重新将可见节点映射为 ECharts `series.data`，将两端均可见的边映射为 `series.links`；边必须包含 `symbol: ['none', 'arrow']` 和关系名称。节点、关系名称和 `citations` 内容只能经由 `textContent` 写入页面。

成功加载后把摘要设置为“运行 ID：{runId}｜节点：{nodes.length}｜关系：{edges.length}”。点击节点或边时，清空证据面板后逐条追加 `article` 元素，文本为“制度名称｜条款 ID｜第起始页-结束页”和原文证据。空图显示“当前运行没有已审核通过的图谱内容。”；HTTP 错误、网络异常与非 JSON 响应均显示“图谱加载失败：{detail}”；`window.echarts` 不存在时显示“图谱组件加载失败，请检查网络后刷新页面。”。

先根据入度为零的节点计算横向层级；节点可全部定位时使用固定坐标。图中存在环或未定位节点时，使用 ECharts `layout: 'force'`，并设置 `roam: true`，避免遗漏循环关系。

- [ ] **Step 5: 运行渲染器测试，确认通过**

Run: `python -m unittest tests.test_policy_workflow_view -v`

Expected: PASS，测试验证转义后的 `data-run-id` 和同源数据接口地址。

- [ ] **Step 6: 提交页面渲染器与测试**

Run: `git add policy/workflow_view.py tests/test_policy_workflow_view.py && git commit -m "feat: add workflow graph view renderer"`

### Task 2: 将页面接入 FastAPI

**Files:**
- Modify: `api.py:10-13,154-164`
- Modify: `tests/test_policy_workflow_view.py`

**Interfaces:**
- Consumes: `render_policy_workflow_view(run_id: str) -> str`。
- Produces: `GET /policy-workflow-view/{run_id}`，状态码 200，媒体类型 `text/html`。
- Depends on: Task 1 的页面渲染器；既有 `GET /policy-workflow-graph/{run_id}` 保持不变。

- [ ] **Step 1: 写入页面路由失败测试**

```python
def test_workflow_view_endpoint_returns_html_for_requested_run(self) -> None:
    from api import app
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/policy-workflow-view/policy_run_demo")
    response = asyncio.run(request())
    self.assertEqual(response.status_code, 200)
    self.assertIn("text/html", response.headers["content-type"])
    self.assertIn('data-run-id="policy_run_demo"', response.text)
```

- [ ] **Step 2: 运行路由测试，确认返回 404**

Run: `python -m unittest tests.test_policy_workflow_view.PolicyWorkflowViewTests.test_workflow_view_endpoint_returns_html_for_requested_run -v`

Expected: FAIL，断言显示响应状态码为 404。

- [ ] **Step 3: 注册 HTML 页面路由**

在 `api.py` 的响应导入中加入 `HTMLResponse`，并在既有图谱 JSON 路由相邻位置加入：

```python
@app.get("/policy-workflow-view/{run_id}", response_class=HTMLResponse)
async def get_policy_workflow_view(run_id: str):
    """返回可查看已审核办事流程图谱及证据的页面。"""
    from policy.workflow_view import render_policy_workflow_view
    return HTMLResponse(content=render_policy_workflow_view(run_id))
```

页面路由不得调用 `build_policy_workflow_graph` 或数据库；图谱 JSON 请求的失败信息应由浏览器页面展示。

- [ ] **Step 4: 运行页面与图谱回归测试**

Run: `python -m unittest tests.test_policy_workflow_view tests.test_policy_workflow_extraction -v`

Expected: PASS，页面返回 HTML，既有图谱投影和 JSON 导出测试通过。

- [ ] **Step 5: 提交 FastAPI 路由**

Run: `git add api.py tests/test_policy_workflow_view.py && git commit -m "feat: serve workflow graph view"`

### Task 3: 补充使用说明与完成回归验证

**Files:**
- Modify: `README.md:213-225`
- Test: `tests/test_policy_workflow_view.py`

**Interfaces:**
- Consumes: 已完成审核的 `policy_run_id` 和已启动的 FastAPI 服务。
- Produces: 可复制的在线页面地址说明。
- Depends on: Task 2 的 `/policy-workflow-view/{run_id}` 路由。

- [ ] **Step 1: 在 README 的流程图谱导出说明后追加在线查看章节**

加入页面地址 `http://127.0.0.1:8000/policy-workflow-view/policy_run_xxxxxxxxxxxxxxxx`，明确该页面读取同源 `/policy-workflow-graph/{policy_run_id}`，只显示已审核通过的节点、关系和条款证据，点击图中元素可查看制度名称、条款、页码和原文。

- [ ] **Step 2: 运行完整相关回归测试**

Run: `python -m unittest tests.test_policy_workflow_view tests.test_policy_workflow_extraction tests.test_policy_retrieval tests.test_table_search_api -v`

Expected: PASS，新增页面不影响图谱投影、制度检索和既有表格 API。

- [ ] **Step 3: 生成并人工检查预览页面**

Run: `python -c "from policy.workflow_view import render_policy_workflow_view; open('workflow_view_preview.html', 'w', encoding='utf-8').write(render_policy_workflow_view('policy_run_demo'))"`

Expected: 预览文件具有标题、摘要、筛选区、图谱画布、证据面板与 ECharts 资源地址。检查后删除 `workflow_view_preview.html`，不纳入提交。

- [ ] **Step 4: 提交 README 更新**

Run: `git add README.md && git commit -m "docs: document online workflow graph view"`
