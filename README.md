# XboxDownload-Web

XboxDownload（游戏下载加速软件）的 **B/S 架构重构版**。

基于 https://github.com/skydevil88/XboxDownload 项目重构

原版是 C#/.NET + Avalonia 的 Windows 桌面应用（`/opt/workspace/XboxDownload/`）。
本项目用 **Python 后端 + 浏览器前端** 重写，支持 **x86_64 / arm64**，
最终以 **容器** 方式部署。

---

## 功能对照

| 原桌面版功能 | 本项目实现 |
|---|---|
| 服务页：设置各服务的加速 IP | **加速地址** 页面，8 个服务组各自独立配置 |
| 测速页：测速并按速度/延迟/TTL 排序 | **测速** 页面，真实下载测速，显示速度、延迟、TTL、下载量 |
| 主机页：手工添加 域名→IP 映射 | **主机映射** 页面 |
| 内置 DNS 服务（改本机 DNS） | 内置 DNS 服务器（UDP+TCP），容器内可直接用 |
| SNI 代理（固定到指定 CDN 边缘 IP） | HTTP(80) / HTTPS(443) 监听，保留原 Host 与 SNI |
| 证书域名校验（CertDomain.txt） | `data/hosts/certdomain-mapping.txt` + 通配符匹配 |
| IP 池导入（8 个 IP 列表） | `data/ip/`，共 1900+ 候选地址，含 IPv6 |
| DoH 服务器 / 上游 DNS 可配置 | 设置页 + `XBD_*` 环境变量 |

---

## 快速开始

### docker compose（推荐）

```bash
cd /opt/workspace/XboxDownload-Web
docker compose up -d --build
```

| 端口 | 用途 |
|---|---|
| 8080 | 管理界面 + JSON API |
| 5353 | 加速 DNS 服务器（UDP + TCP） |
| 8081 | HTTP 加速监听（容器内 80） |
| 8443 | HTTPS 加速监听（容器内 443） |

打开 <http://<主机IP>:8080/> 即可使用。

> 端口用 `docker-compose.yml` 顶部的变量改，例如 `WEB_PORT=80 DNS_PORT=53`。

### docker run

```bash
docker run -d --name xboxdownload-web \
  -p 8080:8080 -p 5353:53/udp -p 5353:53/tcp \
  -p 8081:80 -p 8443:443 \
  --cap-add NET_BIND_SERVICE \
  -v xbd-state:/state \
  xboxdownload-web:latest
```

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
XBD_STATE_DIR=./state .venv/bin/python -m uvicorn app.main:app --port 8080
```

---

## 使用方式

### 1. 设置加速地址

**加速地址** 页面，每个服务组填一个 IP：

| 组 | 说明 | 内置域名 |
|---|---|---|
| `xbox_global` | Xbox 全球 | assets1/2.xboxlive.com, xvcf1/2, d1/d2, dlassets… |
| `xbox_cn1` | Xbox 国内 1 | assets1/2.xboxlive.cn, d1/d2.xboxlive.cn |
| `xbox_cn2` | Xbox 国内 2 | dlassets/dlassets2.xboxlive.cn |
| `xbox_app` | MS Store | dl.delivery.mp.microsoft.com… |
| `ps` | PlayStation | gst.prod.dl.playstation.net… |
| `ns` | Nintendo Switch | ctest-dl-lp1.cdn.nintendo.net… |
| `ea` | EA | origin-a.akamaihd.net |
| `battle` | Battle.net | blzddist1-a.akamaihd.net, downloader.battle.net |

留空 = 不加速，走正常解析。

页面顶部还有一个开关：

> **☑ 显示 Xbox 游戏下载链接**（默认开启）

它对应原版的同名开关，决定「1 号」域名跳到哪个国内域名：

| 开关 | `assets1.xboxlive.com` → | `d1.xboxlive.com` → | `dlassets.xboxlive.com` → |
|---|---|---|---|
| **开**（默认） | `assets2.xboxlive.cn` | `assets2.xboxlive.cn` | `dlassets2.xboxlive.cn` |
| 关 | `assets1.xboxlive.cn` | `d1.xboxlive.cn` | `dlassets.xboxlive.cn` |

「2 号」域名（`assets2` / `d2` / `dlassets2`）**不受开关影响**，始终跳到自己的「2」版本。

关闭时，国内「1 号」域名（`assets1.xboxlive.cn` / `d1.xboxlive.cn` / `dlassets.xboxlive.cn`）
**不跳转**，直接由加速地址中继——避免自跳转死循环。

开关立即生效并持久化（容器重启后保留）。

### 2. 测速

**测速** 页面选组、选目标、点「开始测速」。结果表按速度排序，
显示 **速度 (MiB/s)、延迟 (ms)、TTL、下载量**，最快的一个会单独高亮，
点「用作加速地址」可直接套用。

**每个组只测该组自己的 IP 池**（下拉框已标注国内外）：

| 组 | IP 池 | 节点位置 |
|---|---|---|
| `xbox_cn1` | XboxCn1 | 国内 |
| `xbox_cn2` | XboxCn2 | 国内 |
| `xbox_app` | XboxApp | 国内 |
| `ps` | Ps | 国内 |
| `xbox_global` | Akamai | 国际 |
| `akamai` / `akamaiv2` / `akamaiv6` | Akamai 系列 | 国际 |

> 组与 IP 池是一一对应的，不会再出现"选 CN1 却测出阿根廷节点"的情况。

### 3. 主机映射

手工添加 `域名 → IP`，优先级 **高于** 内置规则。

### 4. 让客户端走加速

把客户端 DNS 指向本机的 5353 端口，并把流量导向 8081/8443：

```bash
# DNS 指向加速服务
echo "nameserver <本机IP>" > /etc/resolv.conf
# 或在 dnsmasq / OpenWrt 上把 53 转发到 <本机IP>:5353
```

---

## 加速原理（对应原版功能）

原版的核心目的：**把 Xbox 的国际下载域名 `assets1.xboxlive.com` 跳转到国内
`assets2.xboxlive.cn`，锁定国内服务器下载**，避免国际域名下载太慢。

本项目的完整链路（与原版一致）：

```
客户端访问 assets1.xboxlive.com
        │
        ▼  DNS 解析（内置 DNS 服务器）
  返回本地代理地址 172.18.0.2        ← 不是海外 CDN！
        │
        ▼  客户端连接到本地代理（80/443）
  代理返回 302 Location: http://assets2.xboxlive.cn/...
        │
        ▼  客户端重新请求 assets2.xboxlive.cn
  DNS 解析到 CN1 加速地址 60.167.226.211（国内电信节点）
        │
        ▼
  从国内节点下载
```

### 关键实现点

| 环节 | 原版（C#） | 本项目 |
|---|---|---|
| `.com` 域名的 DNS 指向 | `localIpRecords`（本机 IP） | `local_proxy_ip` 设置项，留空自动探测本机 IP |
| HTTP 代理 302 跳转 | `HttpThread` 的 switch 分支 | `host_rules.XBOX_CN_REDIRECTS` + `TcpProxyService._resolve_target` |
| `.cn` 域名的加速地址 | `ipMap`: assets2.xboxlive.cn→CN1, dlassets2.xboxlive.cn→CN2 | `host_rules.CN_REDIRECT_GROUPS` |

**为什么 `.com` 必须解析到本地代理而不是直接给国内 IP？**
因为原版就是让 `.com` 走代理、由代理发 302。若直接给国内 IP，
客户端虽然下载的是国内节点，但游戏客户端/浏览器缓存的下载链接仍指向
`.com`，无法体现"跳转"这一核心行为，也无法处理后续的 `assets2` 链路。

### 实测效果（本机实测）

| 组 | 节点位置 | 速度 | 延迟 |
|---|---|---|---|
| `xbox_cn1` | 中国安徽 电信 | **33.1 MiB/s** | **31 ms** |
| `xbox_cn2` | 中国安徽 阜阳 电信 | **23.6 MiB/s** | **99 ms** |
| `xbox_global` | 阿根廷 布宜诺斯艾利斯 | 1.1 MiB/s | 688 ms |

国内节点比国际节点快 **20～30 倍**，延迟低 **20 倍**。

---

## 解析优先级

```
1. 手工主机映射                    （最高）
2. 国际 Xbox 域名 -> 本地代理地址   （由代理 302 跳转到国内 .cn）
3. 内置服务规则 + 该组加速地址
4. 黑名单                          -> 0.0.0.0
5. 上游解析                        （UDP DNS -> DoH -> 系统解析器）
```

> 注意：`.com` 域名同时属于 `XboxGlobal` 规则，但**国内组优先于国际组**——
> 否则就会用回海外节点，加速就失去意义了。

---

## 配置

### 环境变量

环境变量 **优先于** 持久化文件，方便用环境变量直接配置容器。

| 变量 | 说明 | 默认 |
|---|---|---|
| `XBD_HTTP_HOST` / `XBD_HTTP_PORT` | 管理界面监听地址/端口 | `0.0.0.0` / `8080` |
| `XBD_LISTENING_IP` | DNS / 代理监听地址 | `0.0.0.0` |
| `XBD_LOCAL_PROXY_IP` | 国际 Xbox 域名解析到的本地代理地址 | 空（自动探测） |
| `XBD_DNS_ENABLED` / `XBD_DNS_PORT` | DNS 服务开关/端口 | `true` / `53` |
| `XBD_PROXY_HTTP_ENABLED` / `XBD_PROXY_HTTP_PORT` | HTTP 加速开关/端口 | `true` / `80` |
| `XBD_PROXY_HTTPS_ENABLED` / `XBD_PROXY_HTTPS_PORT` | HTTPS 加速开关/端口 | `true` / `443` |
| `XBD_XBOX_GLOBAL_IP` … `XBD_BATTLE_IP` | 各组加速地址 | 空 |
| `XBD_IPV6_ENABLED` | 启用 IPv6 | `false` |
| `XBD_SPEEDTEST_TIMEOUT_S` / `XBD_SPEEDTEST_CONCURRENCY` | 测速超时/并发 | `10` / `8` |
| `XBD_STATE_DIR` / `XBD_DATA_DIR` / `XBD_WEB_DIR` | 状态/数据/前端目录 | 见 Dockerfile |

> 注意区分：`XBD_HTTP_PORT` 是**管理界面**端口，`XBD_PROXY_HTTP_PORT` 是
> **HTTP 加速监听**端口，两个是不同服务。

### 持久化

`/state` 卷保存 `settings.json`、`host-mappings.json` 和自动生成的证书。
容器重启后配置不丢（已实测）。

---

## 多架构构建

应用是纯 Python，Dockerfile 用 `pip install --target --platform` 交叉铺设
目标架构的 wheel，**构建过程不执行任何目标架构二进制**，因此：

> 在 arm64 机器上可以直接构建出 amd64 镜像，**无需 binfmt_misc / QEMU 模拟**。

```bash
# 两种架构分别构建（本机加载）
./scripts/build-multiarch.sh --load

# 指定单个架构
docker build --platform linux/amd64 -t xboxdownload-web:amd64 .
docker build --platform linux/arm64 -t xboxdownload-web:arm64 .

# 有 registry 时推送多架构 manifest
REGISTRY=registry.example.com/xbd TAG=v1 ./scripts/build-multiarch.sh
```

> 本机内核没有 `binfmt_misc`，因此**无法在本机运行 amd64 镜像**
> （会报 `exec format error`）。镜像本身已确认构建成功
> （`docker image inspect` → `amd64/linux`），需要在 x86_64 主机上运行验证。

---

## 项目结构

```
XboxDownload-Web/
├── app/
│   ├── main.py              # FastAPI 入口 + 服务装配
│   ├── api/
│   │   ├── routes.py        # JSON API
│   │   └── schemas.py       # Pydantic 模型
│   ├── core/
│   │   ├── config.py        # 设置与持久化
│   │   ├── engine.py        # 加速引擎（解析优先级）
│   │   ├── host_rules.py    # 内置服务规则表
│   │   ├── host_mappings.py # 手工映射存储
│   │   ├── ip_lists.py      # IP 池解析
│   │   ├── resolver.py      # UDP DNS / DoH / 系统解析
│   │   ├── speedtest.py     # 测速核心
│   │   └── cert_rules.py    # 证书域名匹配 + 映射表
│   └── services/
│       ├── dns_server.py    # DNS 服务器
│       ├── tcp_proxy.py     # HTTP/HTTPS 加速代理
│       └── certificates.py  # 本地 CA / 叶证书
├── web/                     # 前端（原生 HTML/CSS/JS）
├── data/
│   ├── ip/                  # 8 个 IP 池
│   └── hosts/               # 证书域名映射等
├── tests/                   # 114 个测试
├── scripts/                 # 构建与验证脚本
├── Dockerfile
└── docker-compose.yml
```

---

## 测试

```bash
XBD_STATE_DIR=/tmp/xbd-test .venv/bin/python -m pytest tests/ -q
```

覆盖：DNS 协议线格式（构造真实报文并解析回包）、引擎优先级、API、
代理中继、证书匹配、测速。

### 部署验证脚本

```bash
.venv/bin/python scripts/e2e_check.py        # 健康检查 / 加速 / 测速 / 解析
.venv/bin/python scripts/proxy_check.py 8081 8443   # HTTP/HTTPS 代理
```

---

## 从原版移植的逻辑

- `DnsMappingGenerator` → `app/core/host_rules.py`（服务规则表、黑名单、DNS 映射生成）
- `SpeedTestService` / `HttpClientHelper` → `app/core/speedtest.py`（钉 IP 测速、速度/延迟计算）
- `TcpConnectionListener` → `app/services/tcp_proxy.py`（SNI 代理、302 重定向、证书校验）
- `DnsConnectionListener` → `app/services/dns_server.py`
- `CertificateHelper` → `app/services/certificates.py`
- `IP/*.txt` → `data/ip/`（含 `Translation.json` 地名翻译）
- `CertDomain.txt` → `data/hosts/certdomain-mapping.txt`（并补充了 Akamai 规则）
