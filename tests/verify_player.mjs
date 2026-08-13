// verify_player.mjs — 播放器模板验证：
// 1) 主逻辑 script 语法检查（从构建产物提取）
// 2) 数据管线：b64url 还原帧流，帧头/帧数与 Python encoder 输出一致
// 3) QR 兼容：用与 send/main.ts 相同参数对帧生成 QR，确认几何合理
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const dir = process.cwd().endsWith("tests") ? process.cwd() + "/" : process.cwd() + "/tests/";
const html = readFileSync(dir + "dist-player.html", "utf8");
const b64 = readFileSync(dir + "smoke_stream.b64", "utf8").trim();

let fail = 0;
function ok(cond, msg) {
  if (cond) console.log("PASS ", msg);
  else { fail++; console.log("FAIL ", msg); }
}

// ---- 1) 主逻辑 script 语法检查 ----
// 提取最后一个 <script>...</script>（主逻辑；库已内联在更早的 script 中）
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
ok(scripts.length >= 2, `模板含 ≥2 个 script（实际 ${scripts.length}）`);
const main = scripts[scripts.length - 1];
ok(main.includes("function createPlayer"), "主逻辑 script 含 createPlayer");
ok(main.includes("boot();"), "主逻辑 script 以 boot() 收尾");
// 渲染管线关键函数必须齐全（缺一即 ReferenceError）
for (const fn of ["rasterizeQr", "makeCode", "makeCell", "parseStream", "parseContainerInfo", "collectSources", "b64urlDecode", "probeRefreshRate"]) {
  ok(main.includes("function " + fn), `主逻辑含 function ${fn}`);
}
// 速度优化参数：帧率自适应 + 加深预生成队列
ok(main.includes("var BASE_FPS = 30"), "帧率默认 30fps（60Hz 屏安全上限）");
ok(main.includes("var LOOKAHEAD = 8"), "预生成队列 LOOKAHEAD = 8");
ok(main.includes("hz >= 100 ? 55 : 30"), "高刷屏（≥100Hz）自动切 55fps");
// 语法检查：new Function 编译（主逻辑是 IIFE，new Function 可编译但不执行 DOM 代码）
try {
  new Function(main); // 编译而非执行
  ok(true, "主逻辑 script 语法可编译");
} catch (e) {
  ok(false, "主逻辑 script 语法错误: " + e.message);
}

// 库 script 完整（含 QRCode UMD 包装）
ok(scripts[0].includes("QRCode"), "库 script 含 QRCode 全局");
ok(!html.includes("/*%QRCODE_LIB%*/"), "库占位符已被替换");
ok(!html.includes('INLINE_STREAM = "%STREAM_B64%"'), "帧流已内联（非占位符）");

// ---- 2) 数据管线（与模板内 parseStream/b64urlDecode 等价实现）----
function b64urlDecode(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice(0, (4 - (s.length % 4)) % 4);
  const bin = Buffer.from(b64, "base64");
  return new Uint8Array(bin.buffer, bin.byteOffset, bin.byteLength);
}
function parseStream(stream) {
  const dv = new DataView(stream.buffer, stream.byteOffset, stream.byteLength);
  const magicOk = stream[0] === 0xd1 && stream[1] === 0x0d;
  const k = dv.getUint16(8, true);
  const blockLen = dv.getUint16(10, true);
  const totalLen = dv.getUint32(12, true);
  const sessionId = dv.getUint16(2, true);
  const seq0 = dv.getUint32(4, true);
  const frameLen = 20 + blockLen;
  const count = stream.length / frameLen;
  return { magicOk, k, blockLen, totalLen, sessionId, seq0, count, frameLen };
}
const stream = b64urlDecode(b64);
const parsed = parseStream(stream);
ok(parsed.magicOk, `帧头 magic OK（0xD1 0x0D）`);
ok(parsed.k === 1, `k = ${parsed.k}（容器 166B / 500B → 1 块；文本被 gzip 压缩）`);
ok(parsed.blockLen === 500, `blockLen = ${parsed.blockLen}`);
ok(parsed.count === parsed.k, `帧数 ${parsed.count} = k（systematic-only）`);
ok(parsed.totalLen === 166, `totalLen = ${parsed.totalLen}（gzip 后容器）`);
ok(stream.length === parsed.count * parsed.frameLen, "流长 = 帧数 × 帧长");
console.log("   sessionId:", parsed.sessionId, "| seq0:", parsed.seq0);

// 帧 0 块内 DCF2 容器头：文件名应为 UTF-8 中文（说明文档.txt）
const block0 = stream.subarray(20, 20 + parsed.blockLen);
const nameLen = new DataView(block0.buffer, block0.byteOffset, block0.byteLength).getUint16(5, true);
const name = Buffer.from(block0.subarray(49, 49 + nameLen)).toString("utf8");
ok(name === "说明文档.txt", `DCF2 文件名解析 = ${name}`);

// ---- 3) QR 兼容：与 send/main.ts makeCode 相同参数生成 ----
const require2 = createRequire(import.meta.url);
const QRCode = require2("C:/Users/22703/.workbuddy/binaries/node/workspace/qrcode144/node_modules/qrcode/build/qrcode.min.js");
const frame0 = stream.subarray(0, parsed.frameLen);
const qr = QRCode.create([{ data: frame0, mode: "byte" }], {
  errorCorrectionLevel: "L",
  version: undefined,
  maskPattern: 4,
});
ok(qr.version >= 10 && qr.version <= 40, `首帧 QR version = V${qr.version}（byte 模式 520B → V 合理区间）`);
ok(qr.modules.size === (qr.version - 1) * 4 + 21, `modules.size = ${qr.modules.size} 与 version 公式一致`);
// version 锁定后第二帧几何一致（tiling 前提）
const qr2 = QRCode.create([{ data: frame0, mode: "byte" }], {
  errorCorrectionLevel: "L", version: qr.version, maskPattern: 4,
});
ok(qr2.modules.size === qr.modules.size, "version 锁定后几何一致");

console.log(fail === 0 ? "\nALL VERIFY PASSED" : `\n${fail} FAILED`);
process.exit(fail === 0 ? 0 : 1);
