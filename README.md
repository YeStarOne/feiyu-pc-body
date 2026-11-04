# 肥鱼 PC 身体（feiyu-pc-body）

给你的 QQ 机器人（dsh / DeepSeek Harness 系 + OneBot 通道）接一套 **PC 桌宠身体**：
她在云上想开口，主人的电脑屏幕上就有一只桌宠替她说；她还配了一双**常驻眼睛**，
坐在你旁边看你打游戏，随时能翻出「刚才看到了什么」。

> 命名由来：家里的 QQ bot 是一只蓝色大肥鱼（人设详见她的角色卡），这套东西是她的「身体」。

```
┌─────────────────── 主人的 PC ───────────────────┐      ┌──────────────── 云服务器 ────────────────┐
│                                                  │      │                                           │
│  dsh-pet 桌宠(:7340)  ◄──┐                        │      │  eye-server(:8901, systemd)               │
│                          │                        │      │    │ 收帧组 → 视觉模型(GO/池双车道)        │
│  pc_body.py(:17860) ─────┤ 转发指令(broadcast/anim)│ HTTP │    ▼                                       │
│    ├ Bridge  服务器→气泡/动画 ───────────────────────────► │  notes.jsonl（她看到了什么）              │
│    └ Eye     抓屏3帧/轮 ──────────────────────────────►  │  ledger.jsonl（全程账本）                 │
│             (公网直连优先 ~0.13s，Tailscale 兜底)          │        ▲                                  │
│  wd_pc_body.py  看门狗(端口探活自愈)                       │  她的大脑(dsh-web) ── 4 个工具 ──┘        │
└──────────────────────────────────────────────────┘      └───────────────────────────────────────────┘
                                                   pet_say 双投递：同一句话 = 桌宠气泡 + QQ，两处永远一致
```

## 这是什么 / 不是什么

**是**：我们自己写的胶水层——PC 端总控、看门狗、服务器端眼睛服务、四个模型工具、全部护栏。
**不是**：桌宠本体。本仓库**不含** dsh-pet 安装包和它的美术资源（见下「隐私与版权边界」），
你需要另行安装 dsh-pet（standalone 模式，`node lib/standalone.js --port 7340`）。

## 功能

- **双处同步说话**：`pet_say(text, anim?, duration?)` 同一句话同时出现在桌宠气泡和 QQ，
  一边挂了另一边照发，结果里说清哪边没到。
- **远程做动作**：`pet_anim(anim)`，动作名是中文（优雅女仆舞 / 写代码 / 撸猫 / 睡觉-起来 …），
  全量清单 = 桌宠 assets/webm 目录的文件名。
- **常驻眼睛**：pc_body 每 36-68s 抓一轮 3 帧（800w JPEG q72，间隔 2.5s）推给 eye-server，
  视觉模型写成笔记。她可以 `game_eye(n)` 翻最近看到的东西，用来找话题、确认你在干嘛。
- **身体健康自查**：`pet_state()` 看桌宠在不在、眼睛有没有熔断、今日流量。
- **五道流量护栏**（下表）＋**静默治理**＋**空闲降频**＋**看门狗自愈**。

## 护栏与节流（实测数据驱动）

| 机制 | 规则 | 效果/实测 |
|---|---|---|
| 闸0 黑名单窗口 | 交易软件/密码管理器等 | PC 端直接不抓帧，**服务端永远看不到** |
| 闸1 单轮硬顶 | 1.5MB | 超限丢弃 |
| 闸2 日限额 | 500MB 双端 | 熔断暂停，跨日自动恢复 |
| 闸3 连败退避 | 连败 3 次 | 60→120→240→300s 冷却 |
| 闸4 滑窗异常 | 10 分钟 >100MB | 自动暂停，`POST /resume` 解除 |
| 闸5 账本 | 全事件记账 | ledger.jsonl 可审计 |
| 节流器 | 同窗口 6min≥3 条笔记 | 追加强制沉默提示（实测 skip 率 0.7%→62.5%） |
| 复读去重 | 同窗口最近 6 条 difflib>0.72 | 不写不推 |
| 空闲降频 | 无输入 15min→≥3min/轮；45min→暂停 | 不半夜看锁屏 |
| 看门狗 | 60s 端口探活，1.5s 复检，DETACHED 拉起 | 进程挂了 ~70s 自愈 |
| 帧差跳过 | 帧差 <2% 不推送 | 静态画面零流量 |
| 池车道自动恢复 | 探活连 2 次成功 | FORCE_GO 自动退役回免费车道 |

## 快速开始

### 0. 前置

- PC：Windows、Python 3.10+（`psutil`）、Node.js、一个跑在 standalone 模式的 dsh-pet（:7340）
- 服务器：任意 Linux + systemd（用户级），Python 3；一个支持 OpenAI 兼容
  `image_url` 的视觉模型端点（如 opencode-go 的 GO 端点 / 你自己的池网关）

### 1. 共享令牌

```bash
# 生成 64 hex 令牌，两边放同一份
python -c "import secrets; print(secrets.token_hex(32))"
# PC:   存到 D:\StardewAI\dsh-pet\.eye-token
# 服务器: 存到 ~/.dsh/eye-server.token（chmod 600）
```

### 2. 服务器端（eye-server）

```bash
mkdir ~/pc-body && cp server/eye_server.py ~/pc-body/
# 模型 key 从环境读：~/.dsh/dsh-web.env 里放 OPENCODE_API_KEY（GO）与 ZCODE2API_KEY（池，可选）
# 池网关地址用环境变量 POOL_URL 覆盖模板占位符
systemctl --user 略——参考 eye_server.py 头部注释写一个 unit（MemoryMax=120M 建议）
curl -H "Authorization: Bearer $(cat ~/.dsh/eye-server.token)" http://127.0.0.1:8901/health
```

### 3. PC 端（pc_body + 看门狗）

```bat
:: 把三个文件放到 dsh-pet 安装目录，改 pc/pc_body.py 顶部的
:: BRIDGE_BIND / EYE_SERVERS（或设同名环境变量），然后
start_pet_body.bat
```

- 服务器→PC 的指令通道走 PC 的 Tailscale IP:17860（Bearer 同 eye-server.token）
- 云服务器安全组放行 **入方向 TCP 8901**（源可限你的出口 IP；有 Bearer 保护）

### 4. 给她装四个工具

把 `patches/dsh-onebot-qq-pet-tools.snippet.js` 粘进 dsh-onebot-qq 插件的
`lib/tools.js`（registerTools 末尾），重启 dsh-web。
⚠️ 该插件运行时读的是 `~/.dsh/profiles/web/node_modules/` 下的拷贝（非软链），
**源码区和运行份两处都要同步**——这是最容易白干的坑。

### 5. 教她规矩

AGENTS.md（她的角色卡）里建议写明：不是每条笔记都要 `pet_say`；黑名单承诺不许绕过；
只能看和说话，代打未开放。

## 已知坑（都踩过，别再踩）

1. **插件双份拷贝**：改了源码区不同步运行份 = 白改（她调工具报 unknown tool）。
2. **Windows SO_REUSEADDR 双绑**：`http.server` 默认允许两个进程绑同一端口 →
   桥必须独占绑定（`allow_reuse_address = False`），且 bind 失败要**退出整个进程**
   （桥在 daemon 线程里，不退出就是"僵尸眼"——眼睛在跑、身体不听指令）。
3. **Clash TUN 等代理劫持**：Python urllib 读系统代理对内网 IP 会拿到错误结果，
   全程 `build_opener(ProxyHandler({}))` 直连。
4. **bat 启动五坑**：`start` 的重定向要包 `cmd /c` 三引号；纯 ASCII + CRLF；
   启动器拉起的子进程会被终端任务树连坐——所以所有拉起统一归看门狗（DETACHED）。
5. **服务器低内存**：eye-server 给 `MemoryMax=120M`，否则 OOM 杀手挑软柿子捏。

## 隐私与版权边界

- 本仓库**不含**：桌宠安装包与美术资源（疑似含游戏解包资源，绝不公开）、
  令牌、日志、notes.jsonl（她的屏幕观察记录 = 主人隐私）、任何真实 IP
  （代码里的地址全部是占位符，用环境变量或直接改默认值填自己的）。
- 黑名单窗口在 PC 端就地拦截，服务端根本收不到帧——这是给她的承诺，也是给你的承诺。

## License

MIT
