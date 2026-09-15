# 18 — Nginx HTTPS 服务配置语料库

## 为什么前面要挂 Nginx

后端只监听 `127.0.0.1:8000`，直接暴露 uvicorn 端口有三个问题：没有 HTTPS、没有静态资源缓存、SSE 与后端的连接管理不可控。

本章是可选内容，面向有域名与证书的 Linux 部署场景。本地 Windows 单机演示不需要 Nginx：后端用 uvicorn 直接启动，前端用 `npm run dev` 启动，两者通过 Vite 的 `/api` 代理联通即可完成一次完整问答。HTTPS 相关配置只在对外提供服务时才需要，验证清单中与证书相关的项在无域名环境下无法执行。

Nginx 在这里承担四件事：终结 HTTPS、托管前端静态资源、把 `/api/` 反向代理到后端、透传 SSE 流。

## 分流规则

| 路径 | 处理方式 | 说明 |
|---|---|---|
| `/` | 静态资源 | 前端构建产物，配合 `try_files` 支持单页应用路由回退 |
| `/api/` | 反向代理到后端 | 含 SSE，必须关缓冲 |
| `/docs`、`/openapi.json` | 反向代理到后端 | FastAPI 自动文档，演示时会用到 |

分流的关键是静态与动态分开：静态资源由 Nginx 直接读磁盘返回，动态请求才转发到 Python 进程。这避免了 Python 进程被大量静态请求占用。

## 完整配置

```nginx
upstream text2sql_backend {
    server 127.0.0.1:8000;
    keepalive 16;
}

server {
    listen 80;
    server_name your-domain.example.com;
    # 全部跳转 HTTPS，仅保留 ACME 校验路径
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name your-domain.example.com;

    ssl_certificate     /etc/nginx/cert/fullchain.pem;
    ssl_certificate_key /etc/nginx/cert/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Content-Type-Options "nosniff" always;

    client_max_body_size 2m;

    # —— 前端静态资源 ——
    location / {
        root /var/www/text2sql;
        try_files $uri $uri/ /index.html;
        expires 7d;
        add_header Cache-Control "public";
    }

    # —— 后端 API，含 SSE ——
    location /api/ {
        proxy_pass http://text2sql_backend;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 三行：不缓冲、不提前断、不用 chunked 之外的编码
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;

        # 关闭 Nginx 对响应体的 gzip，否则 SSE 帧会被攒起来
        gzip off;
    }
}
```

`server_name`、证书路径与 `root` 需按实际环境替换。

## SSE 透传的三处关键配置

SSE 是长连接流式响应，Nginx 的默认行为会把响应攒在缓冲区里再一次性吐出，逐节点进度反馈因此完全失效。

| 配置 | 作用 |
|---|---|
| `proxy_buffering off` | 关闭响应缓冲，收到即转发 |
| `proxy_read_timeout 300s` | 延长读超时，需大于单次问答最长耗时 |
| `gzip off` | 关闭 gzip，避免压缩缓冲造成同样的攒包效应 |

后端响应头里已带 `X-Accel-Buffering: no`，与 `proxy_buffering off` 互为冗余。两者都配的原因是：前者在应用侧生效，后者在代理侧生效，任一环节缺失都会导致攒包，而故障现象（进度一次性出现）在两端看起来完全一样，只配一处时排查会走弯路。

`proxy_http_version 1.1` 是长连接的前提。HTTP/1.0 下每次请求结束连接即关闭，SSE 无法维持。

## 安装

### Linux

```bash
sudo apt update
sudo apt install -y nginx
sudo nginx -t                     # 校验配置
sudo systemctl reload nginx       # 重载
```

配置文件放在 `/etc/nginx/conf.d/text2sql.conf`，或者在 `sites-available` 建配置后软链到 `sites-enabled`。

修改配置后先 `nginx -t` 再 `reload`，不要直接 `restart`。`reload` 不断连接，`restart` 会中断正在进行的 SSE 流。

### Windows

Windows 版 Nginx 是压缩包解压即用，配置路径与 Linux 相同，但服务管理不同：

```powershell
cd C:\nginx
.\nginx.exe -t
.\nginx.exe -s reload
```

Windows 场景下 HTTPS 多用于内网演示，证书通常是自签的。自签证书浏览器会告警，内网演示可接受。

## SSL 证书

### Let's Encrypt

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example.com
```

证书默认 90 天有效期，certbot 会自动配置续期任务。手动验证续期：

```bash
sudo certbot renew --dry-run
```

### 自签证书（内网演示）

```bash
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/nginx/cert/privkey.pem \
  -out  /etc/nginx/cert/fullchain.pem \
  -subj "/CN=your-domain.example.com"
```

自签证书只解决加密，不解决身份验证。浏览器会提示不受信任，演示时手动跳过即可。

证书与私钥的权限需限制为仅 root 可读，私钥泄露等于 HTTPS 失效。

## 路径拼接的一个易错点

`location /api/` 与 `proxy_pass http://text2sql_backend;`（末尾不带 `/`）搭配时，请求路径原样透传，后端收到 `/api/chat`。

若 `proxy_pass` 末尾带 `/`，location 的前缀会被剥离，后端收到 `/chat`，全部接口 404。

判断方法：看 `proxy_pass` 那一行末尾有没有斜杠。有斜杠则剥离前缀，无斜杠则保留。

## 验证清单

配置完成后逐项确认：

1. `sudo nginx -t` 通过。
2. 访问 `https://域名`，页面正常渲染，无控制台报错。
3. 访问 `https://域名/api/health`，返回 JSON。
4. 浏览器开发者工具里看 `/api/chat` 的响应类型为 `text/event-stream`。
5. 提问一次，进度阶段逐个出现而不是一次性出现。
6. 单页应用路由刷新不 404，例如直接访问一个子路径。
7. 访问的 HTTP 版本为 1.1 或 2（响应头或 Network 面板可看）。

第 5 条是 SSE 配置的最终判据。前四条全过而第 5 条不过，说明缓冲仍有一处没关。

无域名的环境下，第 2、3 两项改为访问 `http://localhost` 与 `http://localhost/api/health`，HTTPS 相关的判据不适用。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 502 Bad Gateway | 后端没起，或 upstream 地址端口不对 | 检查后端进程与 `upstream` 配置 |
| 进度一次性吐出 | 缓冲未关 | 确认 `proxy_buffering off` 与后端 `X-Accel-Buffering: no` 都生效，且已 reload |
| 流中途断开 | `proxy_read_timeout` 偏小 | 调大到 300 秒或更长 |
| 接口 404 | `proxy_pass` 末尾带斜杠导致前缀被剥离 | 去掉 `proxy_pass` 末尾的 `/` |
| 页面刷新 404 | 单页应用路由回退缺失 | `location /` 中配 `try_files $uri $uri/ /index.html;` |
| 502 且日志出现权限错误 | SELinux 或目录权限限制 | 检查静态资源目录权限与 SELinux 策略 |
| HTTPS 能访问但接口报混合内容 | 前端页面里写死了 http 地址 | 前端用相对路径，域名相关地址统一走环境变量 |
