# 附录 C — Nginx HTTPS 部署配置

> 适用：《开发任务文档》T6.4
> 配置位置：`deploy/nginx.conf`
> 前置：已完成实训 5 与实训 6，前后端均可独立启动

---

## 一、为什么需要 Nginx

本地开发中，前端由 Vite 开发服务器提供，通过代理转发 `/api` 到后端。这个形态不适合演示与部署，原因有三：

1. 开发服务器不具备进程守护与自动重启能力；
2. 前端构建产物需要独立的静态文件服务；
3. HTTPS 需要统一的入口做证书终结。

引入 Nginx 后，前后端共用一个入口，对外只暴露 80 与 443 两个端口。

---

## 二、分流规则

| 路径 | 转发目标 | 说明 |
|---|---|---|
| `/api/` | 后端 127.0.0.1:8000 | 需要关闭缓冲，见第四节 |
| `/` | 前端静态资源目录 | 单页应用，未匹配的路径回退到 index.html |

前端构建产物目录为 `frontend/dist`，由 `npm run build` 生成。

---

## 三、基础配置

```nginx
upstream text2sql_backend {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name your-domain.example.com;
    # HTTP 全部跳转到 HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.example.com;

    ssl_certificate     /etc/nginx/cert/fullchain.pem;
    ssl_certificate_key /etc/nginx/cert/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    client_max_body_size 10m;

    # 前端静态资源
    root /var/www/text2sql/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    # 后端接口，含 SSE 透传配置
    location /api/ {
        proxy_pass http://text2sql_backend;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        # 以下三行是 SSE 的关键配置
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;

        chunked_transfer_encoding on;
    }
}
```

`server_name`、`ssl_certificate` 与 `root` 需按实际环境替换。证书路径与申请方式因证书签发机构而异，以你所用的机构文档为准。

---

## 四、SSE 透传

这是部署环节最容易踩的一项。Nginx 默认对响应做缓冲，流式进度会被攒到最后一次性吐出，前端看起来就是「等了十几秒然后所有阶段同时出现」，逐节点反馈完全失效。

三行配置缺一不可：

| 配置 | 作用 |
|---|---|
| `proxy_buffering off` | 关闭响应缓冲，收到即转发 |
| `proxy_cache off` | 不缓存流式响应 |
| `proxy_read_timeout 300s` | 延长读超时，避免长连接被提前断开 |

超时值要大于后端单次问答的最长耗时。后端 SQL 超时为 10 秒，加上模型调用的往返时间，300 秒留了足够余量。

后端同时在响应头中设置 `X-Accel-Buffering: no`。这一项与 Nginx 配置互为备份：即使某一层忘记配置，另一层仍可能生效。

此外 `proxy_http_version 1.1` 与 `proxy_set_header Connection ""` 配合使用，避免连接被当作短连接处理。

---

## 五、启动脚本

`deploy/start.sh` 负责拉起后端并让 Nginx 生效：

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 启动后端。生产环境建议用进程守护工具托管，此处以 nohup 演示
cd "$ROOT/backend"
nohup .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --workers 1 \
  >> "$ROOT/deploy/backend.log" 2>&1 &

echo "backend started, pid $!"

# 校验配置并重载 Nginx
nginx -t
systemctl reload nginx

echo "nginx reloaded"
```

上面的脚本面向 Linux。项目定位是 Windows 本地单机演示，本机直接启动时用下面两条命令替代，不需要 Nginx 也能跑通全链路：

```powershell
# PowerShell，在仓库根目录执行
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# 另开一个终端，启动前端开发服务器
cd frontend
npm run dev
```

Windows 下没有 `systemctl` 与 `nginx -t` 的同等命令。确实需要 Nginx 时，按 Nginx 官方 Windows 版本的方式启动与重载：在 Nginx 目录下用 `start nginx` 启动、`nginx -s reload` 重载、`nginx -s stop` 停止，配置文件中的 `root` 要写成 Windows 路径。

**启动前的前置检查**：确认没有重复的后端进程。重复启动会争用 8000 端口，表现为请求时通时不通：

```powershell
# PowerShell
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
```

有输出说明端口已被占用，先结束该进程再启动。Linux 与 macOS 用 `ps aux | grep uvicorn | grep -v grep`。

---

## 六、验证清单

| 项 | 验证方法 | 预期结果 | 本地演示是否必需 |
|---|---|---|---|
| 接口连通 | 访问 `http://127.0.0.1:8000/api/health` | 返回 `status: ok` | 必需 |
| 静态资源 | 访问前端开发服务器地址 | 前端页面正常加载 | 必需 |
| SSE 未缓冲 | 浏览器发起一次问答 | 阶段逐条出现，不是一次性出现 | 必需 |
| HTTP 跳转 | 访问 `http://域名` | 自动跳转到 HTTPS | 需域名与证书 |
| HTTPS 生效 | 浏览器访问域名 | 地址栏显示锁标志，无证书告警 | 需域名与证书 |
| 长连接不断 | 发起一次耗时较长的问答 | 不出现中途断连 | 需 Nginx |

前三项在 `uvicorn` 加 Vite 开发服务器下即可验证，不依赖 Nginx。后三项需要域名与证书，本地单机演示环境下不具备条件，完成前三项即可认为部署环节通过。

其中 SSE 未缓冲这一项必须用浏览器验证。用 curl 也能收到全部响应，但看不出是否逐条到达。

---

## 七、常见问题

**问题：阶段进度一次性全部出现。**
原因：Nginx 缓冲了响应。解决：确认 `proxy_buffering off` 已配置且 `nginx -t` 通过、配置已重载；同时确认后端响应头中有 `X-Accel-Buffering: no`。

**问题：问答进行到一半连接断开。**
原因：`proxy_read_timeout` 默认值偏小。解决：调大至 300 秒或更长，需大于单次问答最长耗时。

**问题：访问 `/api/health` 返回 404。**
原因：`proxy_pass` 后的路径拼接方式有误。解决：`location /api/` 与 `proxy_pass http://text2sql_backend;` 搭配时，路径原样透传；若 `proxy_pass` 末尾带 `/`，则 location 前缀会被剥离。

**问题：刷新页面出现 404。**
原因：单页应用的路由回退未配置。解决：在 `location /` 中使用 `try_files $uri $uri/ /index.html;`。

**问题：修改配置后不生效。**
原因：配置未重载或校验未通过。解决：先执行 `nginx -t` 确认语法正确，再重载服务。
