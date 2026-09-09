# 通用办事流程知识图谱实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 将现有制度条款候选抽取链路升级为可配置事项域、可审核且可浏览的通用办事流程知识图谱，并以报销制度作为首个试点。

**Architecture:** 保留现有 PDF→条款→候选→人工审核链路，将流程条款分流改为读取版本化事项域目录。扩展实体/关系白名单以表示流程、步骤、角色、条件和结果；以已审核候选及其原文证据动态投影出流程图，不引入第二套不可追溯的数据源。

**Tech Stack:** Python、FastAPI、PostgreSQL（JSONB）、现有 pgvector 检索、CloudMinerU 解析、OpenAI 兼容 LLM、unittest。

**Spec:** docs/superpowers/specs/2026-09-09-general-process-knowledge-graph-design.md

## Global Constraints

- 原始 PDF 与结构化条款是权威来源；每个流程节点和关系必须保留制度、条款、页码及连续原文证据。
- 首期仅导航当前有效制度的流程；保留既有生效日期、效力和版本字段，但不实现历史时点问答或制度冲突裁决。
- 报销只是试点事项域；事项域、关键词和流程信号不能硬编码为系统边界。
- 模型只能生成候选，不能补写制度未明示的步骤、角色、材料、条件、顺序或分支。
- next_step、routes_to、exception_to 只有在原文证据明确支持时才可发布。
- 不新增第三方依赖，不删除现有制度、条款、抽取运行或向量索引。
- 每个任务只暂存本任务列出的文件，避免纳入工作区已有未提交改动。

---

## 文件结构与职责

| 文件 | 职责 |
| --- | --- |
| policy/domain_catalog.json | 版本化事项域目录；保存事项名称、关键词、流程信号和排除信号。 |
| policy/domain_catalog.py | 加载、校验和筛选事项域目录；向分流器提供确定性接口。 |
| policy/process.py | 按选定事项域对条款分流，返回含连续证据的 process、non_process 或 pending 标签。 |
| models.py | 扩展流程实体和关系枚举，保持既有模型兼容。 |
| policy/storage.py | 保存一次分流运行选择的事项域，并为流程图投影读取候选、审核状态与人工标注。 |
| policy/process_runner.py | 用指定事项域执行、恢复流程条款分流。 |
| policy/extraction.py | 抽取通用流程节点与边候选；验证每个候选的原文证据及端点。 |
| policy/workflow_graph.py | 将经审核的候选和人工更正动态投影为带引用的流程图 JSON。 |
| policy/reviewer.py | 将修正视作可发布候选，并校验人工补充流程关系的端点。 |
| policy/quality.py | 导出流程元素、路径关系和图投影的审核/浏览文件。 |
| api.py | 提供只读流程图查询接口，不生成无依据的办事答案。 |
| main.py | 暴露事项域参数、流程图导出命令和状态输出。 |

## Task 1: 事项域目录与可配置流程分流

**Files:**
- Create: policy/domain_catalog.json
- Create: policy/domain_catalog.py
- Modify: policy/process.py
- Modify: tests/test_policy_process.py
- Create: tests/test_policy_domain_catalog.py

**Interfaces:**
- Produces: ProcessDomain、ProcessDomainCatalog、load_process_domain_catalog(path: str | Path | None = None) -> ProcessDomainCatalog。
- Produces: classify_process_clause(text: str, catalog: ProcessDomainCatalog | None = None, selected_domains: Sequence[str] | None = None) -> ProcessClassification。
- Consumes: 后续任务从 ProcessDomainCatalog.select(selected_domains) 取得本次运行允许的事项域。

- [ ] **Step 1: 写入目录和范围控制的失败测试**

在 tests/test_policy_domain_catalog.py 创建临时目录 JSON，并加入：

~~~python
def test_catalog_loads_enabled_domain_and_rejects_unknown_selection(self) -> None:
    catalog = load_process_domain_catalog(self.catalog_path)

    self.assertEqual(catalog.version, "process-domain-catalog-v1")
    self.assertEqual(catalog.select(("reimbursement",))[0].name, "报销")
    with self.assertRaisesRegex(ValueError, "事项域不存在或未启用"):
        catalog.select(("unknown",))
~~~

在 tests/test_policy_process.py 增加：

~~~python
def test_classify_process_clause_obeys_selected_domains(self) -> None:
    result = classify_process_clause(
        "采购申请经部门负责人审批后，报采购中心办理。",
        selected_domains=("reimbursement",),
    )

    self.assertEqual(result.decision, PolicyProcessDecision.NON_PROCESS)
    self.assertEqual(result.domains, [])
~~~

- [ ] **Step 2: 运行测试，确认缺少目录接口**

Run: python -m unittest tests.test_policy_domain_catalog tests.test_policy_process -v

Expected: FAIL，包含 No module named policy.domain_catalog 或 unexpected keyword argument selected_domains。

- [ ] **Step 3: 创建版本化 JSON 事项域目录**

创建 policy/domain_catalog.json。顶层版本固定为 process-domain-catalog-v1，初始保存 reimbursement、travel、procurement 三个 enabled 事项域。每项必须有 code、name、keywords、process_signals、exclude_signals。报销项使用下列精确配置：

~~~json
{
  "code": "reimbursement",
  "name": "报销",
  "enabled": true,
  "keywords": ["报销", "报账", "费用结算", "报销单", "发票", "票据"],
  "process_signals": ["申请", "提交", "提供", "报送", "审批", "审核", "支付", "结算", "退回", "补充"],
  "exclude_signals": ["总则", "解释权", "自发布之日起", "制定本办法"]
}
~~~

travel 与 procurement 保留当前系统覆盖的出差/差旅/交通费/住宿费及采购/购置/招标/询价/供应商等关键词，流程信号与排除信号采用相同结构。

- [ ] **Step 4: 实现目录加载和分流器改造**

在 policy/domain_catalog.py 定义：

~~~python
@dataclass(frozen=True)
class ProcessDomain:
    code: str
    name: str
    enabled: bool
    keywords: Sequence[str]
    process_signals: Sequence[str]
    exclude_signals: Sequence[str]

@dataclass(frozen=True)
class ProcessDomainCatalog:
    version: str
    domains: Sequence[ProcessDomain]

    def select(self, codes: Sequence[str] | None = None) -> Sequence[ProcessDomain]:
        requested = tuple(codes or (domain.code for domain in self.domains if domain.enabled))
        selected = tuple(domain for domain in self.domains if domain.code in requested and domain.enabled)
        if len(selected) != len(requested):
            raise ValueError("事项域不存在或未启用")
        return selected
~~~

加载时校验 version 非空、domains 是数组、code 唯一且只含小写字母数字下划线、启用事项域具有关键词；环境变量 POLICY_DOMAIN_CATALOG 存在时覆盖默认同目录 JSON 路径。select 对未知、禁用或重复 code 抛出 ValueError，错误信息包含“事项域不存在或未启用”。

修改 policy/process.py：移除固定的 _DOMAIN_KEYWORDS、_PROCESS_KEYWORDS、_DIRECT_NON_PROCESS_KEYWORDS；由目录获得关键词、流程信号和排除信号。只有选定事项域关键词与流程信号同时命中，且完整条款可作为连续证据时才返回 process；只命中事项而缺少动作返回 pending；仅命中未选定事项域返回 non_process。build_process_label 透传 catalog 与 selected_domains，且无参数调用时分析全部已启用事项域。

- [ ] **Step 5: 运行目录与分流回归测试**

Run: python -m unittest tests.test_policy_domain_catalog tests.test_policy_process -v

Expected: PASS；既有报销、差旅、采购测试仍通过，新增测试确认报销试点不会误收采购流程。

- [ ] **Step 6: 提交事项域目录任务**

Run:

~~~powershell
git add -- policy/domain_catalog.json policy/domain_catalog.py policy/process.py tests/test_policy_domain_catalog.py tests/test_policy_process.py
git diff --cached --check
git commit -m "feat: configure policy process domains"
~~~

Expected: 仅提交本任务的五个文件。

## Task 2: 持久化试点范围并传递到流程运行

**Files:**
- Modify: policy/storage.py
- Modify: policy/process_runner.py
- Modify: main.py
- Modify: tests/test_policy_process.py

**Interfaces:**
- Produces: create_policy_process_run(batch_id: str, versions: ProcessingVersion, domain_codes: Sequence[str] | None = None, domain_catalog_version: str = "") -> str。
- Produces: create_and_run_policy_process_classification(batch_id: str, domain_codes: Sequence[str] | None = None) -> dict[str, Any]。
- Consumes: policy_process_runs.selected_domains 和 domain_catalog_version；恢复运行必须复用创建时的范围。

- [ ] **Step 1: 写入持久化范围失败测试**

在 tests/test_policy_process.py 增加：

~~~python
def test_runner_reuses_persisted_selected_domains(self) -> None:
    run = {
        "run_id": "process_run_1",
        "selected_domains": ["reimbursement"],
        "domain_catalog_version": "process-domain-catalog-v1",
    }
    with (
        patch("policy.process_runner.get_policy_process_run", return_value=run),
        patch("policy.process_runner.get_policy_process_items", return_value=[{"item_id": "item_1", "clause_id": "c1", "status": "pending"}]),
        patch("policy.process_runner.get_policy_clause", return_value={"clause_id": "c1", "raw_text": "报销时应提交发票。"}),
        patch("policy.process_runner.build_process_label", return_value={"clause_id": "c1", "decision": "process", "domains": ["reimbursement"]}) as build_label,
        patch("policy.process_runner.upsert_policy_process_label"),
        patch("policy.process_runner.update_policy_process_item"),
        patch("policy.process_runner.update_policy_process_run"),
        patch("policy.process_runner.refresh_policy_process_run", return_value={"status": "succeeded"}),
    ):
        run_policy_process_classification("process_run_1")
    self.assertEqual(build_label.call_args.kwargs["selected_domains"], ("reimbursement",))
~~~

该测试的 items 只含 item_1/c1；get_policy_clause 返回 raw_text 为“报销时应提交发票。”；refresh 返回 status succeeded；所有 update 函数使用 Mock。另加 CLI 帮助测试，断言 --policy-domains 出现。

- [ ] **Step 2: 运行测试，确认没有范围快照**

Run: python -m unittest tests.test_policy_process.PolicyProcessTests.test_runner_reuses_persisted_selected_domains -v

Expected: FAIL，因为运行记录未传递 selected_domains。

- [ ] **Step 3: 实现数据库迁移、运行传参和 CLI 参数**

在 ensure_policy_tables() 的 policy_process_runs 建表 SQL 中增加：

~~~sql
selected_domains JSONB NOT NULL DEFAULT '[]'::jsonb,
domain_catalog_version TEXT NOT NULL DEFAULT '',
~~~

在建表后使用 ALTER TABLE ADD COLUMN IF NOT EXISTS 增加同名字段，确保旧数据库兼容。create_policy_process_run 使用 load_process_domain_catalog() 与 catalog.select(domain_codes) 校验；写入实际选中的 code 数组与 catalog.version；未指定 domain_codes 时写入全部启用 code。

在 policy/process_runner.py 中，创建运行时标准化输入为元组；运行和恢复时读取 run["selected_domains"]，并调用：

~~~python
label = build_process_label(
    clause,
    catalog=load_process_domain_catalog(),
    selected_domains=tuple(str(code) for code in run["selected_domains"]),
)
~~~

在 main.py 新增 --policy-domains 参数，格式为 reimbursement,travel，只能与 --classify-policy-process-batch 联用。传入空项、重复项或未知项时返回中文参数错误。--resume-policy-process 不允许此参数，以免改变既有运行的语义。

- [ ] **Step 4: 运行流程分流测试**

Run: python -m unittest tests.test_policy_process -v

Expected: PASS；创建运行记录范围、恢复复用范围、无范围调用处理全部启用事项域。

- [ ] **Step 5: 提交范围持久化任务**

Run:

~~~powershell
git add -- policy/storage.py policy/process_runner.py main.py tests/test_policy_process.py
git diff --cached --check
git commit -m "feat: persist policy process domain scope"
~~~

Expected: 仅提交本任务的四个文件。

## Task 3: 扩展通用流程实体、关系和证据受约束抽取

**Files:**
- Modify: models.py
- Modify: policy/extraction.py
- Modify: policy/reviewer.py
- Modify: tests/test_policy_process.py
- Create: tests/test_policy_workflow_extraction.py

**Interfaces:**
- Produces: 新实体类型 process、step、role、condition、outcome；保留既有实体类型。
- Produces: 新关系类型 has_process、has_step、next_step、performed_by、has_precondition、routes_to、produces、exception_to；保留既有关系类型。
- Produces: _build_outputs(run_id, clause, model_result, include_rule_candidates) 对新增类型执行与原类型相同的证据和端点校验。

- [ ] **Step 1: 写入流程元素和路径边失败测试**

创建 tests/test_policy_workflow_extraction.py：

~~~python
def test_build_outputs_keeps_evidenced_workflow_step_and_relation(self) -> None:
    clause = {"clause_id": "c1", "raw_text": "申请人提交报销单后，由财务处审核并支付。"}
    result = {
        "entities": [
            {"type": "step", "name": "提交报销单", "evidence_text": "提交报销单", "confidence": 0.95},
            {"type": "role", "name": "财务处", "evidence_text": "财务处", "confidence": 0.95},
            {"type": "step", "name": "审核", "evidence_text": "审核", "confidence": 0.95}
        ],
        "relations": [
            {"type": "performed_by", "subject_index": 2, "object_index": 1,
             "evidence_text": "由财务处审核", "confidence": 0.95},
            {"type": "next_step", "subject_index": 0, "object_index": 2,
             "evidence_text": "提交报销单后，由财务处审核", "confidence": 0.95}
        ]
    }
    entities, relations, reviews = _build_outputs("run_1", clause, result, False)
    self.assertEqual({item.entity_type.value for item in entities}, {"step", "role"})
    self.assertEqual({item.relation_type.value for item in relations}, {"performed_by", "next_step"})
    self.assertEqual(reviews, [])
~~~

新增反例：把 next_step 的 evidence_text 改为“提交后付款”，断言该关系为 pending 且产生 low_confidence_relation 审核项。

- [ ] **Step 2: 运行测试，确认新类型会被拒绝**

Run: python -m unittest tests.test_policy_workflow_extraction -v

Expected: FAIL，包含 invalid_entity_type 或 invalid_relation_type。

- [ ] **Step 3: 扩展枚举、别名、提示词和审核修正**

在 PolicyEntityType 增加 PROCESS、STEP、ROLE、CONDITION、OUTCOME；在 PolicyRelationType 增加 HAS_PROCESS、HAS_STEP、NEXT_STEP、PERFORMED_BY、HAS_PRECONDITION、ROUTES_TO、PRODUCES、EXCEPTION_TO。同步扩展 policy/extraction.py 中的 _ENTITY_ALIASES、_RELATION_ALIASES 和 _call_llm 提示词。

提示词必须包含以下约束：

~~~text
step 只能表示条款明确写出的可执行动作；不得从章节标题或常识补全。
next_step、routes_to、exception_to 的 evidence_text 必须同时包含顺序词、条件词或例外词及两端动作。
relation 的 subject_index 与 object_index 必须指向 entities 数组中的流程节点；缺端点不返回该关系。
condition、outcome 也必须使用条款中的连续原文。
~~~

_rule_entities 只补充高确定性节点：原文中的审核、审批、支付、归档、退回、补充材料可作为 step 或 outcome；规则不得自动生成 next_step、routes_to、exception_to。路径关系只能由模型候选或人工补充产生，并先标记为 pending。

修改 reviewer.py：corrected 的候选状态更新为 approved；只有 rejected 更新为 rejected。人工新增 relation 的 label_data 必须含 relation_type、subject_entity_id、object_entity_id、evidence_text，否则抛出中文 ValueError。

- [ ] **Step 4: 运行抽取和流程回归测试**

Run: python -m unittest tests.test_policy_workflow_extraction tests.test_policy_process -v

Expected: PASS；新增类型可保存，伪造证据不自动通过，既有报销材料与采购审批测试保持通过。

- [ ] **Step 5: 提交通用流程抽取任务**

Run:

~~~powershell
git add -- models.py policy/extraction.py policy/reviewer.py tests/test_policy_process.py tests/test_policy_workflow_extraction.py
git diff --cached --check
git commit -m "feat: extract evidence-backed workflow elements"
~~~

Expected: 仅提交本任务的五个文件。

## Task 4: 投影已审核候选为可浏览流程图

**Files:**
- Create: policy/workflow_graph.py
- Modify: policy/storage.py
- Modify: tests/test_policy_workflow_extraction.py
- Create: tests/test_policy_workflow_graph.py

**Interfaces:**
- Produces: get_policy_manual_annotations(run_id: str) -> list[dict[str, Any]]。
- Produces: build_confirmed_workflow_graph(run_id: str, candidates: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> dict[str, Any]。
- Produces: load_confirmed_workflow_graph(run_id: str) -> dict[str, Any]，只接受关联了流程分流运行的抽取运行。
- Consumes: get_policy_process_graph_candidates()、人工标注以及候选携带的 clause/document 上下文。

- [ ] **Step 1: 写入流程图投影失败测试**

创建 tests/test_policy_workflow_graph.py，构造两个已审核步骤、一个已审核角色、两条已审核关系与一个 pending 节点：

~~~python
def test_build_confirmed_workflow_graph_merges_nodes_and_preserves_citations(self) -> None:
    graph = build_confirmed_workflow_graph("run_1", candidates=self.candidates, annotations=[])

    self.assertEqual([node["name"] for node in graph["nodes"]], ["提交报销单", "财务处", "审核"])
    self.assertEqual(graph["edges"][0]["type"], "next_step")
    self.assertEqual(graph["edges"][0]["citations"][0]["clause_id"], "c1")
    self.assertNotIn("付款", {node["name"] for node in graph["nodes"]})
~~~

在同一测试文件增加 corrected 标注，label_data 的 name 为“财务审核”，断言投影显示“财务审核”而不显示旧候选“审核”。

- [ ] **Step 2: 运行测试，确认图模块不存在**

Run: python -m unittest tests.test_policy_workflow_graph -v

Expected: FAIL，包含 No module named policy.workflow_graph。

- [ ] **Step 3: 实现只读图投影**

在 policy/storage.py 增加 get_policy_manual_annotations(run_id)，按 created_at、annotation_id 升序读取 policy_manual_annotations。不得新建第二套流程事实表，也不得覆盖模型候选。

在 policy/workflow_graph.py 定义：

~~~python
PUBLISHABLE_STATUSES = {"auto_approved", "approved"}

def build_confirmed_workflow_graph(
    run_id: str,
    candidates: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
) -> dict[str, Any]:
    """仅从审核通过的流程候选构造可引用的图投影。"""
    return _project_confirmed_candidates(run_id, candidates, annotations)
~~~

实体节点以 entity_type 加规范化名称归并；规范化只删除空白并统一字符串，不做同义词推断。每个节点含 id、type、name、citations；每条 citation 含 policy_id、title、file_name、clause_id、page_start、page_end、evidence_text。

边只在关系审核通过、证据连续存在、并且两端节点均已投影时发布；每条边含 id、type、source、target、citations。应用候选的最新 corrected 标注；rejected 标注排除候选。added 实体用 annotation:<annotation_id> 作为来源 ID 后参与归并；added relation 的两端必须引用投影出的实体来源 ID，否则累加 omitted.invalid_endpoint_count。输出 nodes、edges、omitted，并按节点类型/名称及边类型/源/目标稳定排序。

load_confirmed_workflow_graph 先读抽取运行：不存在时抛出 ValueError“流程图谱抽取运行不存在”，未关联 process_run_id 时抛出 ValueError“该抽取运行不是流程图谱运行”；然后加载候选和标注并返回投影。

- [ ] **Step 4: 运行图投影测试**

Run: python -m unittest tests.test_policy_workflow_graph tests.test_policy_workflow_extraction -v

Expected: PASS；仅发布审核通过且证据完整的节点/边，人工更正覆盖显示值，所有发布内容保留条款和页码。

- [ ] **Step 5: 提交流程图投影任务**

Run:

~~~powershell
git add -- policy/workflow_graph.py policy/storage.py tests/test_policy_workflow_extraction.py tests/test_policy_workflow_graph.py
git diff --cached --check
git commit -m "feat: project reviewed policy workflow graph"
~~~

Expected: 仅提交本任务的四个文件。

## Task 5: 提供流程图导出、只读 API 和报销试点说明

**Files:**
- Modify: policy/quality.py
- Modify: api.py
- Modify: main.py
- Modify: README.md
- Modify: tests/test_policy_quality.py
- Create: tests/test_policy_workflow_api.py

**Interfaces:**
- Produces: export_confirmed_workflow_graph(run_id: str, output_dir: str | Path) -> Path，写入 流程图谱.json。
- Produces: GET /policy-workflow-graph/{run_id}，可选查询参数 domain。
- Consumes: load_confirmed_workflow_graph(run_id)，不调用大模型、不修改数据库。

- [ ] **Step 1: 写入导出与 API 失败测试**

在 tests/test_policy_quality.py 增加：

~~~python
def test_export_confirmed_workflow_graph_writes_citable_json(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        with patch(
            "policy.quality.load_confirmed_workflow_graph",
            return_value={"run_id": "r1", "nodes": [], "edges": [], "omitted": {}},
        ):
            path = export_confirmed_workflow_graph("r1", temp_dir)
    self.assertEqual(path.name, "流程图谱.json")
~~~

在 tests/test_policy_workflow_api.py 使用 patch(policy.workflow_graph.load_confirmed_workflow_graph) 验证：成功响应保留 nodes、edges、citations；未知运行转为中文 404；非流程运行转为中文 409；domain 没有匹配事项时返回空子图，不能生成文字答案。

- [ ] **Step 2: 运行测试，确认接口不存在**

Run: python -m unittest tests.test_policy_quality tests.test_policy_workflow_api -v

Expected: FAIL，包含无法导入 export_confirmed_workflow_graph 或找不到 /policy-workflow-graph/{run_id}。

- [ ] **Step 3: 实现导出、API、CLI 和文档**

在 policy/quality.py 实现导出函数：run_id 为空时抛出 ValueError“流程图谱抽取运行 ID 不能为空”；创建输出目录；以 UTF-8、ensure_ascii=False、indent=2 写入 流程图谱.json；不得把投影混入既有 图谱抽检.csv。

在 api.py 增加：

~~~python
@app.get("/policy-workflow-graph/{run_id}")
async def policy_workflow_graph(
    run_id: str,
    domain: Optional[str] = Query(None, min_length=1, description="按事项域过滤，例如 reimbursement"),
):
    return JSONResponse(content=_filter_workflow_graph(load_confirmed_workflow_graph(run_id), domain))
~~~

先加载完整图；domain 存在时只保留匹配事项节点、与其可达的流程/步骤节点及关联边。未知运行返回 404，非流程运行返回 409；接口不调用 LLM、不更新审核状态。

在 main.py 新增 --export-policy-workflow-graph <policy_run_id>，要求同时传 --quality-output；加入互斥模式检查并输出生成路径。

更新 README.md，加入报销试点的固定命令顺序：

~~~powershell
python main.py --classify-policy-process-batch batch_xxx --policy-domains reimbursement
python main.py --export-policy-process-review policy_process_xxx --quality-output "D:\policy_quality"
python main.py --extract-policy-process-run policy_process_xxx --limit 20
python main.py --export-policy-process-graph-review policy_run_xxx --quality-output "D:\policy_quality"
python main.py --export-policy-workflow-graph policy_run_xxx --quality-output "D:\policy_quality"
~~~

文档必须说明：流程图只发布审核通过或规则自动通过的候选；pending 或没有连续原文证据的内容不会发布。

- [ ] **Step 4: 运行接口、导出和命令行测试**

Run: python -m unittest tests.test_policy_quality tests.test_policy_workflow_api tests.test_policy_process -v

Expected: PASS；JSON 导出包含引用，API 不生成答案，命令行帮助显示新命令和报销试点范围参数。

- [ ] **Step 5: 提交流程图交付接口任务**

Run:

~~~powershell
git add -- policy/quality.py api.py main.py README.md tests/test_policy_quality.py tests/test_policy_workflow_api.py
git diff --cached --check
git commit -m "feat: expose reviewed workflow graph"
~~~

Expected: 仅提交本任务的六个文件。

## Task 6: 全链路回归与报销试点验收

**Files:**
- Modify: tests/test_policy_workflow_graph.py
- Modify: README.md

**Interfaces:**
- Consumes: Tasks 1–5 的目录、分流、抽取、审核、图投影、导出和 API 接口。
- Produces: 不连接生产服务的报销试点验收测试和明确操作说明。

- [ ] **Step 1: 写入内存端到端试点验收测试**

在 tests/test_policy_workflow_graph.py 增加：

~~~python
def test_reimbursement_pilot_graph_has_steps_roles_materials_and_citations(self) -> None:
    graph = build_confirmed_workflow_graph("run_1", candidates=self.reimbursement_candidates, annotations=[])

    self.assertIn("提交报销单", {node["name"] for node in graph["nodes"]})
    self.assertIn("财务处", {node["name"] for node in graph["nodes"]})
    self.assertIn("requires_material", {edge["type"] for edge in graph["edges"]})
    self.assertTrue(all(edge["citations"] for edge in graph["edges"]))
    self.assertNotIn("付款", {edge["type"] for edge in graph["edges"] if edge["review_status"] == "pending"})
~~~

self.reimbursement_candidates 必须只使用内存候选：条款“申请人提交报销单和发票后，部门负责人审批；财务处审核并支付。”，并为每个发布节点/边带 document、clause、页码、连续证据和 approved 状态。测试不得连接 PostgreSQL、MinIO、CloudMinerU 或真实 LLM。

- [ ] **Step 2: 运行单个试点验收测试**

Run: python -m unittest tests.test_policy_workflow_graph.PolicyWorkflowGraphTests.test_reimbursement_pilot_graph_has_steps_roles_materials_and_citations -v

Expected: PASS；步骤、角色、材料关系和引用均存在，pending 内容未发布。

- [ ] **Step 3: 运行完整制度模块回归测试**

Run: python -m unittest tests.test_policy_process tests.test_policy_quality tests.test_policy_retrieval tests.test_policy_domain_catalog tests.test_policy_workflow_extraction tests.test_policy_workflow_graph tests.test_policy_workflow_api -v

Expected: PASS；无失败，且没有通过跳过关键流程来掩盖错误。

- [ ] **Step 4: 核对 README 的试点验收说明**

核对 README 同时说明以下六项：使用 --policy-domains reimbursement 分流；先导出流程条款审核表；仅从确认条款抽取；导出流程图；每个发布节点/边可回到条款和页码；未审核或无证据候选不发布。缺少任一项时直接在 README 补充完整。

- [ ] **Step 5: 提交回归与验收任务**

Run:

~~~powershell
git add -- tests/test_policy_workflow_graph.py README.md
git diff --cached --check
git commit -m "test: verify reimbursement workflow graph pilot"
~~~

Expected: 仅提交本任务的两个文件；提交前完整回归命令成功。

## Spec 覆盖核对

- 通用事项域而非报销硬编码：Tasks 1–2。
- 流程、步骤、角色、材料、条件、时限、结果及顺序/分支/异常：Task 3。
- 原文、条款、页码、审核状态与版本化运行追溯：Tasks 2–4。
- 只发布有依据且审核通过的图内容：Task 4。
- PDF 到条款、候选、审核、流程图、检索/浏览链路：现有解析链路与 Tasks 1–5。
- 报销首期试点及质量验收：Task 6。
- 不执行实际审批流、不做历史裁决或冲突裁决：全局约束与 API/README 范围声明。

## 计划自检

- 不含占位标记或未定义的实现步骤。
- 所有新增函数、CLI 名称和 API 名称均由对应任务定义。
- 后续任务只消费前序任务已定义的接口。
