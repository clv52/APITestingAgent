import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

// Python 负责进程和原子落盘；本文件只负责“JSON 字段 -> Excel 单元格/样式”的映射。

const CATEGORY_NAMES = {
  positive: "正常场景",
  required: "必填校验",
  length: "长度边界",
  type: "类型校验",
  enum: "枚举边界",
  auth: "鉴权校验",
  protocol: "协议校验",
  encoding: "编码校验",
  security: "安全场景",
  other: "其他场景",
};

const OP_NAMES = {
  exists: "存在",
  not_exists: "不存在",
  equals: "等于",
  not_equals: "不等于",
  type: "类型为",
  not_empty: "非空",
  contains: "包含",
  not_contains: "不包含",
  matches: "匹配正则",
};

const STATUS_NAMES = {
  passed: "通过",
  failed: "失败",
  skipped: "跳过",
  error: "错误",
};

/** 解析 Node 命令行的 --key value 参数，并校验必填输入输出路径。 */
function parseArgs(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value == null) throw new Error(`无效参数：${key ?? ""}`);
    result[key.slice(2)] = value;
  }
  if (!result.input || !result.output) throw new Error("必须传入 --input 和 --output");
  return result;
}

/** 将 JSON 值和用例值构造器转换为适合 Excel 阅读的中文文本。 */
function displayValue(value) {
  if (value === undefined) return "";
  if (value === null) return "null";
  if (typeof value === "string") return value === "" ? "空字符串" : value;
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(displayValue).join("、");
  if (Object.keys(value).length === 1 && value.$env) return `环境变量：${value.$env}`;
  if (Object.keys(value).length === 1 && value.$template) return `模板：${value.$template}`;
  if (Object.keys(value).length === 1 && value.$repeat) {
    return `“${value.$repeat.text}”重复 ${value.$repeat.count} 次`;
  }
  return JSON.stringify(value, null, 2);
}

/** 将 name/value 数组格式化为每行一项的单元格文本。 */
function displayPairs(items = []) {
  return items.length ? items.map((item) => `${item.name} = ${displayValue(item.value)}`).join("\n") : "—";
}

/** 将普通对象格式化为每行一个键值对的单元格文本。 */
function displayObject(value = {}) {
  const entries = Object.entries(value);
  return entries.length ? entries.map(([name, item]) => `${name} = ${displayValue(item)}`).join("\n") : "—";
}

/** 按 form、json 或 raw 类型生成人类可读的请求体说明。 */
function displayBody(body) {
  if (body == null) return "无请求体";
  if (body.type === "form") return `表单\n${displayPairs(body.fields)}`;
  if (body.type === "json") return `JSON\n${displayValue(body.value)}`;
  if (body.type === "raw") return `原始内容 (${body.content_type})\n${displayValue(body.text)}`;
  return displayValue(body);
}

/** 将 Header、JSON 或文本断言数组转换为中文检查说明。 */
function displayAssertions(items = [], kind) {
  if (!items.length) return "—";
  return items.map((item) => {
    const target = kind === "header" ? item.name : kind === "json" ? item.path : "响应文本";
    const suffix = Object.hasOwn(item, "value") ? ` ${displayValue(item.value)}` : "";
    return `${target}：${OP_NAMES[item.op] || item.op}${suffix}`;
  }).join("\n");
}

/** 综合用例与接口默认值，生成一条完整的测试场景描述。 */
function caseDescription(testCase, spec) {
  const request = testCase.request || {};
  const method = request.method || spec.interface.method;
  const requestPath = request.path || spec.interface.path;
  const statuses = (testCase.expected?.status_codes || []).join("/");
  const target = testCase.boundary?.target || "接口行为";
  const rule = testCase.boundary?.rule || testCase.title;
  return `${testCase.title}。针对“${target}”验证“${rule}”场景；发送 ${method} ${requestPath}，期望 HTTP ${statuses || "按文档约定"}。`;
}

/** 根据执行状态和失败断言生成测试结果原因。 */
function resultReason(result) {
  if (!result) return "尚未执行";
  if (result.status === "passed") return "全部断言通过";
  if (result.status === "failed") {
    const failures = (result.assertions || []).filter((item) => item && item.passed === false);
    if (!failures.length) return result.error || "断言失败";
    return failures.map((item) => (
      `${item.name || "断言"}：期望 ${displayValue(item.expected)}，实际 ${displayValue(item.actual)}`
    )).join("\n");
  }
  if (result.status === "skipped") {
    const missing = (result.missing_env || []).join(", ");
    return missing ? `缺少环境变量：${missing}` : (result.error || "用例被跳过");
  }
  return result.error || "请求执行异常";
}

/** 将一条声明式用例及可选执行结果映射为 Excel 数据行。 */
function caseRow(testCase, index, spec, result) {
  // case_id 是结果回填的稳定主键，不能依赖 Excel 行号或标题匹配。
  const request = testCase.request || {};
  const expected = testCase.expected || {};
  return [
    index + 1,
    testCase.id,
    caseDescription(testCase, spec),
    CATEGORY_NAMES[testCase.category] || testCase.category,
    testCase.priority,
    testCase.boundary?.target || "",
    testCase.boundary?.rule || "",
    displayValue(testCase.boundary?.value),
    testCase.assumption || "无",
    request.method || spec.interface.method,
    request.path || spec.interface.path,
    displayPairs(request.headers),
    displayPairs(request.query),
    displayObject(request.path_params),
    displayBody(request.body),
    (expected.status_codes || []).join(", "),
    displayAssertions(expected.headers, "header"),
    displayAssertions(expected.json, "json"),
    displayAssertions(expected.text, "text"),
    expected.max_response_ms ?? spec.execution?.timeout_ms ?? "",
    result ? (STATUS_NAMES[result.status] || result.status || "未知") : "未执行",
    result?.http_status ?? "",
    result?.elapsed_ms ?? "",
    resultReason(result),
  ];
}

/** 创建概览页与测试用例页，并应用公式、颜色和失败行高亮。 */
async function buildWorkbook(spec, executionReport = null) {
  // 概览页给人看汇总，测试用例页保留一条 case 一行的完整可追溯信息。
  const workbook = Workbook.create();
  const overview = workbook.worksheets.add("用例概览");
  const casesSheet = workbook.worksheets.add("测试用例");
  const totalCases = spec.test_cases.length;
  const lastRow = Math.max(7, totalCases + 6);

  overview.showGridLines = false;
  overview.mergeCells("A1:F1");
  overview.getRange("A1:F1").values = [[`${spec.interface.name} · 接口测试用例`]];
  overview.getRange("A1:F1").format = {
    fill: "#17365D", font: {bold: true, color: "#FFFFFF", size: 18},
    verticalAlignment: "center", horizontalAlignment: "left",
  };
  overview.getRange("A1:F1").format.rowHeight = 34;
  overview.getRange("A3:B7").values = [
    ["接口名称", spec.interface.name],
    ["Operation ID", spec.interface.operation_id || "—"],
    ["请求方式", spec.interface.method],
    ["请求路径", spec.interface.path],
    ["用例总数", null],
  ];
  overview.getRange("A3:A7").format = {fill: "#D9EAF7", font: {bold: true, color: "#17365D"}};
  overview.getRange("B7").formulas = [[`=COUNTA('测试用例'!$B$7:$B$${lastRow})`]];
  overview.getRange("A3:B7").format.borders = {preset: "outside", style: "thin", color: "#A8B8C8"};

  overview.getRange("D3:E6").values = [
    ["优先级", "数量"],
    ["P0", null],
    ["P1", null],
    ["P2", null],
  ];
  overview.getRange("D3:E3").format = {fill: "#2F75B5", font: {bold: true, color: "#FFFFFF"}};
  overview.getRange("E4").formulas = [[`=COUNTIF('测试用例'!$E$7:$E$${lastRow},D4)`]];
  overview.getRange("E4:E6").fillDown();

  overview.getRange("D9:E14").values = [
    ["执行结果", "数量"],
    ["通过", null],
    ["失败", null],
    ["错误", null],
    ["跳过", null],
    ["未执行", null],
  ];
  overview.getRange("D9:E9").format = {fill: "#2F75B5", font: {bold: true, color: "#FFFFFF"}};
  overview.getRange("E10").formulas = [[`=COUNTIF('测试用例'!$U$7:$U$${lastRow},D10)`]];
  overview.getRange("E10:E14").fillDown();
  overview.getRange("D10:E14").format.borders = {preset: "inside", style: "thin", color: "#D7DEE5"};

  const categoryEntries = Object.entries(CATEGORY_NAMES);
  overview.getRange(`A10:B${9 + categoryEntries.length}`).values = categoryEntries.map(([key, label]) => [label, null]);
  overview.getRange("A9:B9").values = [["场景分类", "数量"]];
  overview.getRange("A9:B9").format = {fill: "#2F75B5", font: {bold: true, color: "#FFFFFF"}};
  overview.getRange("B10").formulas = [[`=COUNTIF('测试用例'!$D$7:$D$${lastRow},A10)`]];
  overview.getRange(`B10:B${9 + categoryEntries.length}`).fillDown();
  overview.getRange("A3:F20").format.wrapText = true;
  overview.getRange("A1:A20").format.columnWidth = 18;
  overview.getRange("B1:B20").format.columnWidth = 48;
  overview.getRange("C1:C20").format.columnWidth = 4;
  overview.getRange("D1:D20").format.columnWidth = 16;
  overview.getRange("E1:E20").format.columnWidth = 12;
  overview.freezePanes.freezeRows(1);

  casesSheet.showGridLines = false;
  casesSheet.mergeCells("A1:X1");
  casesSheet.getRange("A1:X1").values = [[`${spec.interface.name} · 逐条测试用例`]];
  casesSheet.getRange("A1:X1").format = {
    fill: "#17365D", font: {bold: true, color: "#FFFFFF", size: 17},
    verticalAlignment: "center",
  };
  casesSheet.getRange("A1:X1").format.rowHeight = 32;
  casesSheet.getRange("A3:F4").values = [
    ["接口", spec.interface.name, "方法", spec.interface.method, "路径", spec.interface.path],
    ["Schema", spec.schema_version, "环境变量", (spec.required_env || []).join(", "), "说明", "Excel 用于人工审阅，JSON 是自动执行数据源"],
  ];
  casesSheet.getRange("A3:F4").format.wrapText = true;
  casesSheet.getRange("A3:F4").format.borders = {preset: "outside", style: "thin", color: "#B9C6D2"};
  casesSheet.getRange("A3:A4").format = {fill: "#D9EAF7", font: {bold: true, color: "#17365D"}};
  casesSheet.getRange("C3:C4").format = {fill: "#D9EAF7", font: {bold: true, color: "#17365D"}};
  casesSheet.getRange("E3:E4").format = {fill: "#D9EAF7", font: {bold: true, color: "#17365D"}};

  const headers = ["序号", "用例ID", "用例描述", "场景分类", "优先级", "边界目标", "边界规则", "边界值", "前提/假设", "请求方法", "请求路径", "请求头", "Query参数", "Path参数", "请求体", "预期状态码", "响应头断言", "JSON断言", "文本断言", "最大响应时间(ms)", "执行状态", "实际状态码", "响应时间(ms)", "失败/异常原因"];
  casesSheet.getRange("A6:X6").values = [headers];
  casesSheet.getRange("A6:X6").format = {
    fill: "#2F75B5", font: {bold: true, color: "#FFFFFF"},
    wrapText: true, verticalAlignment: "center", horizontalAlignment: "center",
  };
  casesSheet.getRange("A6:X6").format.rowHeight = 30;
  const resultById = new Map(
    (executionReport?.results || []).map((item) => [String(item.case_id || ""), item]),
  );
  const rows = spec.test_cases.map((item, index) => caseRow(item, index, spec, resultById.get(String(item.id))));
  if (rows.length) {
    casesSheet.getRange(`A7:X${lastRow}`).values = rows;
    casesSheet.getRange(`A7:X${lastRow}`).format = {wrapText: true, verticalAlignment: "top"};
    casesSheet.getRange(`A7:X${lastRow}`).format.rowHeight = 54;
    casesSheet.getRange(`A6:X${lastRow}`).format.borders = {preset: "inside", style: "thin", color: "#D7DEE5"};
    const table = casesSheet.tables.add(`A6:X${lastRow}`, true, "ApiTestCasesTable");
    table.style = "TableStyleMedium2";
    casesSheet.getRange(`E7:E${lastRow}`).conditionalFormats.add("containsText", {text: "P0", format: {fill: "#FCE4D6", font: {bold: true, color: "#C00000"}}});
    casesSheet.getRange(`E7:E${lastRow}`).conditionalFormats.add("containsText", {text: "P1", format: {fill: "#FFF2CC", font: {color: "#9C6500"}}});
    const executionRange = casesSheet.getRange(`A7:X${lastRow}`);
    executionRange.conditionalFormats.addCustom('=$U7="失败"', {fill: "#F8CBCB", font: {bold: true, color: "#9C0006"}});
    executionRange.conditionalFormats.addCustom('=$U7="错误"', {fill: "#F8CBCB", font: {bold: true, color: "#9C0006"}});
    executionRange.conditionalFormats.addCustom('=$U7="跳过"', {fill: "#FFF2CC", font: {color: "#9C6500"}});
    casesSheet.getRange(`U7:U${lastRow}`).conditionalFormats.add("containsText", {text: "通过", format: {fill: "#E2F0D9", font: {bold: true, color: "#006100"}}});
  }
  const widths = [7, 12, 48, 14, 9, 18, 28, 20, 34, 11, 34, 28, 25, 22, 34, 13, 28, 34, 28, 16, 11, 13, 15, 48];
  const columns = "ABCDEFGHIJKLMNOPQRSTUVWX".split("");
  columns.forEach((column, index) => {
    casesSheet.getRange(`${column}1:${column}${lastRow}`).format.columnWidth = widths[index];
  });
  casesSheet.getRange(`A7:B${lastRow}`).format.horizontalAlignment = "center";
  casesSheet.getRange(`D7:E${lastRow}`).format.horizontalAlignment = "center";
  casesSheet.getRange(`J7:J${lastRow}`).format.horizontalAlignment = "center";
  casesSheet.getRange(`P7:P${lastRow}`).format.horizontalAlignment = "center";
  casesSheet.getRange(`U7:W${lastRow}`).format.horizontalAlignment = "center";
  casesSheet.freezePanes.freezeRows(6);
  casesSheet.freezePanes.freezeColumns(2);
  return workbook;
}

const args = parseArgs(process.argv);
const inputPath = path.resolve(args.input);
const outputPath = path.resolve(args.output);
const spec = JSON.parse(await fs.readFile(inputPath, "utf8"));
const executionReport = args.results ? JSON.parse(await fs.readFile(path.resolve(args.results), "utf8")) : null;
const workbook = await buildWorkbook(spec, executionReport);
await fs.mkdir(path.dirname(outputPath), {recursive: true});
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
if (args.preview) {
  const preview = await workbook.render({sheetName: "测试用例", range: `A1:X${Math.min(spec.test_cases.length + 6, 18)}`, scale: 1, format: "png"});
  await fs.mkdir(path.dirname(path.resolve(args.preview)), {recursive: true});
  await fs.writeFile(path.resolve(args.preview), new Uint8Array(await preview.arrayBuffer()));
}
console.log(JSON.stringify({ok: true, input: inputPath, results: args.results || null, output: outputPath, cases: spec.test_cases.length}, null, 2));
