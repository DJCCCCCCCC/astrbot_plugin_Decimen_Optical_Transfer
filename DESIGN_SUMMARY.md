# 方案摘要：AstrBot 插件 —— URL 显示的动态二维码光学传输

> 来源：`D:\桌面\Code\decimen-optical-transfer-main`（decimen-optical-transfer v0.4.0 项目 + 衍生设计文档 `astrbot-qrurl-plugin-design.md`）
> 整理日期：2026-08-12
> 本文件仅作设计摘要，不包含实现代码。

---

## 1. 设计目标与业务场景

**一句话定义**：AstrBot 插件把用户发来的文本 / 附件文件编码成"喷泉帧字节流"，包进 URL（或自包含 HTML）回发到聊天；对方用**设备①**打开 URL 看到**动画二维码**，再用**设备②**的官方 decimen 接收端相机去扫，文件内容**全程走光信道、不经任何网络**。

**关键纠正（务必牢记）**：URL 打开后看到的是**真二维码、需要相机扫**，不是"浏览器 JS 直接解出文件"。文件内容零网络；唯一碰网络的是"聊天转发 URL 字符串"和"可选地从主机拉一次播放器页面"。

| 项 | 说明 |
|---|---|
| 输入 | 聊天里用户发来的**文本** 或 **回复的附件文件** |
| 输出 | 一条可转发的 **URL / HTML 文件**，打开即显示动画二维码 |
| 传输 | 文件内容经**光**（设备①屏幕 → 设备②相机），不经网络 |
| 解码 | 复用**官方 decimen 接收端**，**零改动** |
| 兼容 | 产出的二维码与 decimen 发送端**字节级一致**，接收端无需适配 |

**明确不做**：不在浏览器内 JS 直解文件（已否决）；不加密（与 decimen 一致，"Neither mode is encrypted"，URL 即 bearer 令牌）。

---

## 2. 整体架构概述

```
┌────────────── 发送侧（聊天机器人环境，Python） ──────────────┐
│  用户消息 ─▶ [AstrBot 插件 main.py]                          │
│     ├─ 文本 → packSnippet                                    │
│     └─ 附件 → packFile                                       │
│                ▼                                             │
│   [encoder.py] 纯 Python，零 QR 库                           │
│    DCF2 容器 → 切 K 块 → 喷泉帧 → 拼接字节流 → base64url     │
│                ▼                                             │
│   [URL 封装] 模式 A(data:) / 模式 B(托管+#hash) / 降级视频   │
│                ▼                                             │
│   机器人把 URL 回发到聊天                                     │
└──────────────────────────────────────────────────────────────┘
                 │  聊天只传这串字符（明文内容不落服务器）
                 ▼
┌────────────── 接收侧（对方两台设备） ────────────────────────┐
│  设备①（显示器）：打开 URL → 浏览器播放器把字节流渲染成动画   │
│     二维码（复用 decimen send/main.ts 渲染逻辑，只当显示器） │
│  设备②（解码器）：decimen 接收端相机扫描 → WASM(zxing) 解码   │
│     → parseFrame → LTDecoder 剥离 → assemble → 校验 → 文件   │
└──────────────────────────────────────────────────────────────┘
```

**三个核心设计决策**：
1. **二维码渲染放客户端，不放 Python 端** —— 播放器复用 decimen `send/main.ts` 渲染逻辑（`QRCode.create` + `rasterizeQr` + `gridDims` 多码网格 + `stagger` 错相位），生成的二维码与发送端**逐 bit 一致**，Python 端完全不需要 QR 库，规避"Python QR 库与 node-qrcode 输出不一致"的风险。
2. **默认只发 systematic 帧（零喷泉冗余）** —— URL 场景字节流"完整到齐"，不需要 erasure 容错；第 i 帧 = 第 i 个源块（零 XOR），体积最小、播放最短。可选加 repair 帧提高"播放暂停/相机漏帧"鲁棒性。
3. **传字节流而非图片** —— URL 里只装 streamBytes（base64 膨胀约 1.33×），客户端实时渲染，体积最小。

---

## 3. 功能模块清单

### 3.1 发送侧（AstrBot 插件，Python）
| 模块 | 职责 |
|---|---|
| `main.py` | 插件壳。指令 `/qr <文本>`、回复附件 `@机器人 qr`；管理员配置（`MAX_PAYLOAD_BYTES`、`USE_REPAIR`、`HOST`、`MODE`） |
| `encoder.py` | 全部编码逻辑：DCF2 容器 + 切块 + 喷泉帧 + packFrame + base64url（纯标准库） |
| `fountain_port.py` | 从 `shared/fountain.ts` 移植：`splitmix32` / `dlog` / `repair_indices`（约 80 行，仅启用 repair 时需要） |
| `templates/player.html` | 模式 B 通用播放器（复用 `send/main.ts` 渲染逻辑，帧源 = `location.hash`） |
| `build_player.py` | 把 `player.html` + 字节流打成**自包含 HTML 文件**（模式②，推荐）或 **data: URL**（模式③） |
| `README.md` | 使用说明（含"对方需用外部浏览器打开 + 用 decimen 接收端扫"） |

### 3.2 接收侧（复用 decimen，零改动）
- 设备①：浏览器播放器（本次新增，本质是"抽掉本地选文件、改为从 URL 读字节流"的 decimen 发送端）
- 设备②：官方 decimen 接收端（`receive/main.ts` + worker：相机 → WASM(zxing) 解码 → LTDecoder → 校验）

---

## 4. 核心技术栈

| 层 | 技术 |
|---|---|
| 发送侧 | Python（AstrBot 插件框架，`Star` 类 + `@filter.command`）；仅标准库 `hashlib` / `gzip` / `base64`，**零第三方 QR 库** |
| 播放器 | 复用 decimen 前端（TypeScript + Vite + node-qrcode）；`QRCode.create([{data, mode:'byte'}], {ecc:'L', version:首帧锁定, maskPattern:4})` |
| 接收端 | 官方 decimen（zxing-cpp 定制 WASM + LT 喷泉解码），零改动 |
| 协议 | DCF2 容器（49B 头）+ 20B 小端帧头 + 系统性 carousel 喷泉（wire v2，magic `0xD1 0x0D`） |

### 4.1 线格式规格（字节级，保证兼容）
**帧头（小端 20 字节）+ 块**：
```
offset  size  field
0       1     magic0 = 0xD1
1       1     magic1 = 0x0D   (v2 标记；v1 用 0x0C，使其干净拒绝而非静默失同步)
2       2     sessionId  u16
4       4     seq        u32
8       2     k          u16
10      2     blockLen   u16
12      4     totalLen   u32   (容器总长)
16      4     payloadFnv u32   (容器整体 FNV-1a)
20      N     block 字节 (N = blockLen)
```

**DCF2 容器（FILE_HEADER_LEN = 49）**：
```
0   4   'DCF2'
4   1   gzip 标志 (0/1)
5   2   nameLen  u16
7   2   typeLen  u16
9   4   origLen  u32
13  4   transLen u32
17  32  SHA-256
49  ..  name + media_type + transmitted 字节
```

**关键常量**：`FNV_OFFSET=0x811c9dc5`、`FNV_PRIME=0x01000193`（u32 截断）；`blockLen` 建议 500（单码 V40-L 上限约 2953 B/帧，500+20=520 远在容量内；K ≤ 65535 → 真实文件上限约 30 MB）；`sessionId` 随机 16 位；QR `ecc='L'`、`maskPattern=4`、version 首帧锁定、`mode='byte'`。gzip 仅当压缩后更小（且文件 ≥768 B、非预压缩类型）才用。

### 4.2 体积限制与降级策略
| 传输字节 | 策略 |
|---|---|
| ≤ ~100–300 KB | 模式 A（data: 自包含 URL），最优 |
| 中等 | 模式 B（托管播放器 + `#` fragment，server-blind） |
| 大 / 必须零主机 | 降级渲染 carousel 视频以"文件"发（注意微信/QQ 视频消息会被重编码，须以"文件"形式发） |

三种载体对比：
| 方式 | 载体 | 打开要网络？ | 内容到服务器？ | 体积上限 |
|---|---|---|---|---|
| ① 托管播放器 + `#` | URL 链接 | ✅ 拉一次页 | ❌（fragment 不到服务器） | 宽松 |
| ② 自包含 HTML 文件（**推荐**） | 文件附件 | ❌ 纯本地 | ❌ | 宽松（几 MB） |
| ③ data: 自包含 URL | URL 字符串 | ❌ 纯本地 | ❌ | 严苛（URL 长度易截断） |

---

## 5. 关键流程

1. **输入**：文本 → `packSnippet`；附件 → `packFile`（文件名安全化、media type 默认 octet-stream、gzip 仅当更小、SHA-256 前置）。
2. **打包容器**：`header + name + media_type + transmitted`，得到 DCF2 容器字节。
3. **切块**：`k = ceil(len(container)/blockLen)`，切出 K 块。
4. **喷泉帧**：systematic-only 时第 i 帧 = 第 i 块（零 XOR）；启用 repair 时 seq K..2K-1 为 degree 4–24 的 XOR 帧（子集由 `splitmix32(frameSeed(sessionId, seq))` 确定性派生）。
5. **拼流 + 编码**：每帧 `packFrame(20B头+块)` → 拼接 → `base64.urlsafe_b64encode` → 去 `=`。
6. **封装**：按 `MAX_PAYLOAD_BYTES` 自动选模式（A/B/降级视频）。
7. **回发**：机器人把 URL/HTML 文件发到聊天。
8. **接收**：设备①播放动画二维码 → 设备② decimen 接收端扫描 → `parseFrame` 校验 magic/长度 → `LTDecoder.addFrame` 剥皮解码 → `assemble` → FNV + SHA-256 校验 → 保存文件。

---

## 6. 实现路线图（分阶段）

1. **阶段 0 — 字节对齐验证（最关键）**：写 `encoder.py` + `fountain_port.py`，用 decimen `tests/` 的 golden 向量验证 DCF2 容器 + 帧头 + FNV/SHA 字节一致；字节流喂给 decimen `send` 渲染逻辑，确认可被 `receive` 解出。
2. **阶段 1 — 播放器（模式 B 先做起）**：把 `send/main.ts` 抽成"帧源可注入"的 `player.html`，从 `location.hash` 读字节流渲染；用手机 decimen 接收端实测扫屏解出文件。
3. **阶段 2 — AstrBot 插件壳**：`main.py` 接指令 + 附件，调 encoder 产出 b64，拼 `https://HOST/r#b64` 回发。
4. **阶段 3 — 模式 A（data: 自包含）**：`build_player.py` 打 `data:text/html;base64,...`；加 `MAX_PAYLOAD_BYTES` 自动模式选择与降级提示。

**验证方法**：单元（encoder 输出 vs decimen golden 向量逐字节比对）；集成（插件产 URL → 设备①打开 → 设备②扫描 → 文件 SHA-256 一致）；边界（空文本、大文件降级、中文文件名 UTF-8、二进制文件）。

---

## 7. 待确认事项（影响后续开发）

1. **聊天平台 URL 截断阈值**：微信/QQ 对超长链接的实际截断/折叠阈值未实测，data: 模式尤甚，需实测确定 `MAX_PAYLOAD_BYTES` / `MAX_FRAGMENT_BYTES`。
2. **模式 B 的 HOST 托管方案**：设计未指定托管位置（静态 CDN / EdgeOne Pages / 自建等）；需确认 HOST 配置与部署方式。
3. **`blockLen` 与多码网格取值**：设计建议 500，但 decimen `send-settings.ts` 提供多档帧大小选项；需确认插件是否固定 500 还是可配置、是否启用多码网格提速。
4. **`USE_REPAIR` 默认值**：URL 场景理论零丢帧，但"播放暂停/相机漏帧"的鲁棒性未实测；repair 开关默认值需定。
5. **`dlog()` 移植精度**：若启用 repair，`fountain_port.py` 必须逐位复刻 decimen 的自实现 `dlog`（精确 IEEE-754 运算），否则 Python 与 JS 引擎输出差异会导致收发失同步；纯 Python 的浮点一致性验证方案未定。
6. **snippet（文本）容器格式**：设计只提 `packSnippet`，未明确文本 snippet 的容器/媒体类型包装细节（需对照 `shared/snippet.ts`）。
7. **附件获取与异步约束**：AstrBot 事件中附件字节的获取方式依赖 AstrBot API 版本；大文件 gzip/SHA-256 须放入线程池/异步执行，避免阻塞事件循环。
8. **安全边界**：不加密是既定决策，但设计提到"要真保密需在塞进 DCF2 前自加密一层"——是否预留该接口未定；需向用户讲清"URL 即 bearer 令牌，别泄露链接"。
9. **平台差异**：微信/QQ 内置浏览器相机 `getUserMedia` 不稳、`.html` 文件可能被拦截——接收端需引导"外部浏览器打开"，发送载体选择需按平台调整。
10. **License 合规**：decimen 为 AGPL-3.0-or-later；插件复用它前端渲染逻辑与协议实现时需确认开源许可合规策略。

---

## 8. 当前工作空间落地状态

- 当前工作空间 `astrbot_plugin_Decimen_Optical_Transfer` 仍是 **helloworld 模板**（`metadata.yaml` 中 name 为 helloworld，无任何编码逻辑），设计方案尚未落地为代码。
- 本摘要作为后续开发的**依据文档**；下一步建议按路线图先做**阶段 0（encoder + golden 向量字节对齐验证）**。
- 设计源文档（完整版）：`D:\桌面\Code\decimen-optical-transfer-main\astrbot-qrurl-plugin-design.md`；上游项目：decimen-optical-transfer v0.4.0。
