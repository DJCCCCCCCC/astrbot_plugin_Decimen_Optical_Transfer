# astrbot_plugin_decimen_optical_transfer

把 decimen 的"屏幕发光 → 相机收"光学传输，改装成"一个 URL 当屏幕"。

机器人在聊天里把文件/文本编码成一段字节流，回发一个 URL；对方打开 URL
看到**动画二维码**，用另一台设备的 [decimen 接收端](https://decimen.app/)
相机去扫，文件就还原了。**文件内容全程走光信道，不经过任何网络** ——
聊天只转发 URL 字符串（明文内容不落服务器）。

> ⚠️ 与 decimen 一致，链路**不加密**：URL 即承载令牌（bearer），
> 任何拿到链接的人都能还原文件，请勿泄露。

## 状态：阶段 1（播放器模板已完成）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 编码引擎 `encoder.py` + `fountain_port.py`，decimen golden 向量字节对齐 | ✅ 完成（38/38 测试通过） |
| 1 | 播放器模板 `templates/player.html` + 构建工具 `build_player.py` | ✅ 本版本 |
| 2 | 插件壳 `main.py`（指令 /qr、附件处理） | ✅ 完成 |
| 3 | data: 自包含 URL 模式 + 自动降级 | ⏳ 依赖部署验证 |

**注意**：播放器需部署到 `{host}/r/` 后，插件产出的 URL 才可扫描（见下）。

## 使用（三种输出形式，配置 `mode` 切换）

| mode | 行为 | 适用 |
|---|---|---|
| `file`（默认） | `/qr` 回发**自包含 HTML 文件**（内联 QR 库 + 帧流，约 145KB + 数据） | 推荐；对方下载后用外部浏览器打开，零网络；不受 URL 截断影响 |
| `hosted` | `/qr` 回发 **URL**：配置 `shorten_host` 时为**短链** `{shorten_host}/r/<6位ID>`；否则为 `{host}/r#<b64>` | 短链需运行 `shorten_server.py`；原始 URL 需部署播放器到 `{host}/r/` |
| `both` | 文件 + URL 都回 | 灵活，URL 部分同样支持短链 |

`file` 模式生成的 HTML 保存在插件数据目录（`data/plugin_data/<name>/`），通过 `Comp.File` 发送；
文件载体体积不受 `max_payload_bytes` 限制（仅受 QR k 上限约 30MB 约束）。

## 短链模式（URL 极短，推荐）

`{host}/r#<b64>` 会把整段帧流塞进 URL（大文件可达数万字符，聊天软件易截断）。
**短链模式**把帧流 POST 到本地短链服务暂存，URL 只留 6 位短 ID：

```bash
# 1) 启动短链服务（任一终端，保持运行）
python shorten_server.py --port 8123
# 2) 插件配置 shorten_host = http://127.0.0.1:8123
# 3) /qr 输出的 URL 形如 http://127.0.0.1:8123/r/Ab3xY9（约 30 字符）
```

对方打开短链时，服务端读取暂存数据并**动态渲染**自包含播放器 HTML（复用
`build_player.build("file", ...)`，播放器零改动）。注意：
- 数据暂存于 `shorten_data/` 目录（等同持有文件本身，勿在不可信环境开放端口）；
- 短链服务需对接收方可达（局域网内用本机 IP；公网需部署/端口映射）。

## 播放器接入路由

播放器模板 `templates/player.html` 是**自包含**单文件（内联 node-qrcode UMD + 渲染逻辑），
构建后可用两种方式接入：

**① 托管模式（hosted，供 URL 方式使用）**
```bash
python build_player.py --mode hosted --out dist/r/index.html
```
把 `dist/r/index.html` 部署到任意静态托管（EdgeOne Pages / 对象存储 / Nginx），
插件配置 `host` 指向其根地址。插件产出的 URL 为 `{host}/r#<b64>`：
- 播放器从 `location.hash` 读取字节流（fragment **不到达服务器**，server-blind）；
- 打开需联网拉取播放器页一次（之后零网络）。

**② 自包含 HTML 文件（file 模式产物；也可手动构建）**
```bash
python build_player.py --mode file --stream <b64> --out dist/player.html
```
单个 `.html` 文件即"URL 当屏幕"的全部内容，可作附件/网盘发送；对方下载后用
**外部浏览器**打开（微信内置 webview 摄像头支持不稳）。

**③ data: URL（极小载荷）**
```bash
python build_player.py --mode data --stream <b64>   # 打印 data:text/html;base64,...
```
受 URL 长度限制，实战少用。

> 注意：微信直接发 `.html` 可能被当不安全文件拦截，建议以"文件"形式发送。

### 播放器功能
- 播放区域：动画二维码（点击全屏），渲染管线与 decimen `send/main.ts` 逐位一致
  （QRCode.create：`ecc='L'`、`maskPattern=4`、version 首帧锁定、byte 模式；
  rasterizeQr 4 模块 quiet zone；整数 module scale + CSS 平滑；**帧率自适应**：
  60Hz 屏 → 30fps、120Hz+ 屏 → 55fps，每帧 ≥2 刷新周期，对齐 decimen 官方策略）
- 控制栏：播放/暂停、帧进度条（可跳帧）、播放速度（0.5×–4×，1× 即屏幕最优帧率；
  二维码流无音量概念，故以速度取代音量位）、全屏；空格键播放/暂停
- 当前传输信息：从 DCF2 容器头解析文件名/大小/类型/K/帧负载/QR 版本/压缩
- 播放列表：URL 可携带多流 `#b64|b64|...`，点击切换
- 状态处理：加载中（spinner）、错误（magic 不匹配/帧长不一致/无数据源 → 提示 + 重载）

### 验证
```bash
# 1) Python 侧 golden 向量（编码引擎）
python tests/test_fountain.py && python tests/test_encoder.py
# 2) 构建并验证播放器（模板结构/库内联/数据管线/QR 兼容）
python tests/smoke_main.py
python build_player.py --mode file --stream-file tests/smoke_stream.b64 --out tests/dist-player.html
node tests/verify_player.mjs   # 需 node（内联 qrcode 来自 node-qrcode 1.4.4 UMD）
```
`verify_player.mjs` 17 项断言覆盖：模板语法、库/帧流内联、帧头 magic、k/blockLen/
帧数、中文文件名解析、QR version 锁定几何一致。

## 使用

```
/qr <文本>          —— 编码一段文本
回复文件并 @机器人 qr —— 编码一个附件文件
```

回复包含：文件/文本摘要（容器大小、块数、帧数、URL 负载长度）+ URL。

## 配置（插件管理页）

| 键 | 默认 | 说明 |
|---|---|---|
| `block_len` | 1445 | 每帧负载字节数（不含 20B 头）；对齐 decimen 官方档位，可选 480/980/1445/1830/2311/2933（帧字节 500–2953） |
| `use_repair` | false | 是否附加 repair 帧（体积换容错） |
| `mode` | file | `file`（回发自包含 HTML 文件）/ `hosted`（URL）/ `both`（都回） |
| `host` | （空） | 托管播放器地址（hosted/both 模式），播放器部署在 `{host}/r/` |
| `max_payload_bytes` | 200000 | hosted 模式 URL 载荷上限，超限报错提示；file 模式不受此限 |

## 线格式（与 decimen wire v2 字节级兼容）

- **20B 小端帧头**：magic `0xD1 0x0D`、sessionId(u16)、seq(u32)、k(u16)、
  blockLen(u16)、totalLen(u32)、payloadFnv(u32, 容器整体 FNV-1a)
- **DCF2 容器**（49B 头）：`DCF2` + gzip 标志 + nameLen/typeLen/origLen/transLen + SHA-256
- **喷泉码**：systematic carousel（前 K 帧各发一块，零 XOR；可选 K 个 degree
  4–24 repair 帧），PRNG 为 splitmix32，`dlog` 为 IEEE-754 精确实现
  （不可替换为 `math.log`，否则收发失步）

## 测试

```bash
python tests/test_fountain.py   # 喷泉码 golden 向量（20 项）
python tests/test_encoder.py    # 容器/帧头/流结构（18 项）
python tests/smoke_main.py      # 插件业务链路（snippet/附件/还原/超限）
node tests/verify_player.mjs    # 播放器模板（17 项，需先构建 dist-player.html）
```

全部通过即证明编码引擎与 decimen 发送端字节级一致（接收端零改动）、
播放器模板渲染管线与 send/main.ts 参数逐位一致。

## 开发路线（下一步）

1. **部署实测（最关键）**：把 `build_player.py --mode hosted` 产物部署到
   `{host}/r/`，配置插件 `host`，用手机 decimen 接收端扫屏解出文件（端到端验收）。
2. 阶段 3：`MAX_PAYLOAD_BYTES` 自动模式选择与降级提示（超限自动改发自包含 HTML）。
3. 边界验证：空文本、大文件降级、中文文件名（UTF-8）、二进制文件。
4. 微信/QQ 平台实测 URL 截断阈值，调整 `max_payload_bytes` 默认值。

## License / 归属

协议与渲染逻辑源自 [decimen-optical-transfer](https://github.com/bashalarmistalt/decimen-optical-transfer)
（AGPL-3.0-or-later）；解码端为零改动的官方 decimen 接收端。
