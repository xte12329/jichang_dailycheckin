# iKUUU 机场签到 - 浏览器自动登录版

解决原 Cookie 版「每隔 5 天 cookie 就掉、要手动重新导出」的问题。

## 原理变更（重要）

原本的「账号密码 + GeekedTest 协议求解」方案**实测不可行**：

- ikuuu 现在随机返回 **九宫格(nine) / 图标(icon) / 文字(word)** 三种 GeeTest V4 验证码；
- GeekedTest 只支持 slide / gobang / icon / ai，**不支持 nine 和 word**；
- 极验对协议求解的签名一律返回 `-50000 runtime error`，本地复现 100% 失败。

本版改为**真实 Chromium 浏览器登录**：

1. 打开登录页，填账号密码；
2. 点击极验验证按钮（无感验证点一下自动通过）；
3. 若弹出点选题，用**智谱免费视觉模型 `glm-4v-flash`** 截图识别并点击；
4. 提交登录拿新 cookie，再 POST `/user/checkin` 签到。

每次都是全新登录，彻底不依赖会过期的长期 cookie，同时自动轮换域名。

## 部署步骤

### 1. 仓库准备
用你的 `xte12329/jichang_dailycheckin`（建议设为 **Private**）。

### 2. 替换文件
用本目录文件覆盖：

| 本地文件 | 覆盖到仓库路径 |
|---|---|
| `main.py` | `main.py` |
| `requirements.txt` | `requirements.txt` |
| `.github/workflows/checkin.yml` | `.github/workflows/checkin.yml` |

建议删掉旧的 `main.yml`、`main2.yml`，避免重复签到。

### 3. 配置 Secrets
`Settings → Secrets and variables → Actions → Secrets`：

| 名称 | 内容 | 必填 |
|---|---|---|
| `IKUUU_EMAIL` | 机场账号邮箱 | ✅ |
| `IKUUU_PASSWD` | 机场账号密码 | ✅ |
| `ZHIPU_API_KEY` | 智谱免费 Key（https://open.bigmodel.cn 注册），用于点选验证码 | ✅ 强烈建议 |
| `SCKEY` | Server酱密钥（不想推送可留空） | ❌ |
| `PUSHPLUS_TOKEN` | PushPlus 密钥（与 SCKEY 二选一） | ❌ |

> 不配 `ZHIPU_API_KEY` 只能过无感验证，遇到九宫格/点选题必失败。

### 4. 配置 Variables（可选）
`Settings → Secrets and variables → Actions → Variables`：

| 名称 | 内容 | 说明 |
|---|---|---|
| `IKUUU_DOMAINS` | `ikuuu.top,ikuuu.pw,ikuuu.org` | 候选域名，逗号分隔；机场换域名时在这里改 |

### 5. 测试
Actions 页 → **Run workflow** 手动跑一次。

- 成功：日志出现 `✅ 签到成功`，收到推送。
- 失败：到该 run 页面底部 **Artifacts** 下载 `captcha-debug`，里面有验证码截图，发给我可以针对性调整识别逻辑。

## 时间

默认北京时间每天 **09:33**（cron `33 1 * * *`）。要改时间注意换成 UTC（北京时间 − 8）。

## 踩坑记录

- **Set up Python 失败：`version '3.10.16' ... was not found for Ubuntu 26.04`**
  GitHub runner 镜像升级到 Ubuntu 26.04 后，`setup-python` 找不到写死的精确小版本。
  已改为 `python-version: '3.10'`（自动选该系列最新可用版），**切勿写死成 `3.10.16`**。
- checkout / setup-python / upload-artifact 已升级到 v7，避免 Node 20 弃用告警。
- 协议求解方案失败的根因已记录在上文，不要退回 GeekedTest 方案。

## 风险与维护

- 视觉模型识别点选题有概率出错，脚本会自动重试 3 次；连续失败请查看截图。
- 若账号开启了 TOTP / 邮箱二次验证，自动登录会失败，需要关闭二验或改用 Cookie 方案。
- 账号密码存 GitHub Secrets 是加密的，但建议专用密码、定期更换。
- 自动化签到可能违反机场服务条款，请自行判断。
