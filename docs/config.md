# 配置说明

项目根目录的 [config.yaml](../config.yaml) 主要控制：

- 服务端口
- API Key 鉴权
- 配置页登录密钥
- 浏览器可执行文件路径
- 调度与回收参数
- mock 调试端口

## 配置文件优先级

如果你本地经常需要改配置、又不想每次提交都处理 `config.yaml` 的变更，可以在项目根目录新建 `config.local.yaml`。

加载优先级：

1. `WEB2API_CONFIG_PATH` 环境变量
2. `config.local.yaml`
3. `config.yaml`

## 关键配置项

### 服务端口

```yaml
server:
  port: 9000
```

### 账号存储位置

代理组和账号不直接写在 `config.yaml`，而是保存在 SQLite 数据库里：

- 源码运行默认位置：项目根目录的 `db.sqlite3`
- Docker 运行默认位置：容器内 `/data/db.sqlite3`，对应宿主机 `./docker-data/db.sqlite3`
- 可通过环境变量 `WEB2API_DB_PATH` 指定其他数据库路径

数据库内主要使用两张表：

- `proxy_group`：保存代理地址、代理账号密码、`fingerprint_id`、是否启用代理、时区
- `account`：保存账号 `name`、`type`、认证信息 `auth` JSON、启用状态、冻结到期时间

配置页 `/config` 中保存的代理组和账号信息，最终都会写入这个 SQLite 数据库。

### 浏览器路径

默认可以留空，程序会按当前系统自动探测常见路径和 PATH 里的浏览器命令：

```yaml
browser:
  chromium_bin: ''
```

如果自动探测失败，手动填写真实的可执行文件路径。

Linux 示例：`/opt/fingerprint-chromium/chrome`、`/usr/bin/chromium` 或 `/usr/bin/google-chrome`

macOS 示例：`/Applications/Chromium.app/Contents/MacOS/Chromium`

### Linux / Docker 兼容参数

如果浏览器在 Linux / Docker 环境里启动后立刻关闭，可以尝试：

```yaml
browser:
  no_sandbox: true
  disable_gpu: true
  disable_gpu_sandbox: true
```

注意：这更适合容器、Xvfb、远程桌面环境；对本机桌面环境通常不需要。

### 下载目录

默认下载目录会落在每个浏览器实例的 `user-data-dir` 下，形如：

`~/fp-data/<fingerprint_id>/downloads`

也可以在 `config.yaml` 里自定义（会覆盖默认值）：

```yaml
browser:
  download_dir: ~/Downloads
```

建议使用绝对路径或以 `~` 开头的路径。

### API Key 鉴权

如果不希望任何人拿到地址就能直接调用，建议配置：

```yaml
auth:
  api_key: 'your-secret-key'
```

启用后：

- `/{type}/v1/*` 都需要带其中一个有效 key
- 推荐请求头：`Authorization: Bearer your-secret-key`
- `auth.api_key` 留空时使用默认值 `tongji-api-key`
- 可在控制台“API 设置”页面修改 `auth.api_key`

### 配置页保护

首次访问 `/login` 时可以直接在 UI 中初始化控制台密码。也可以提前在配置文件中写入明文：

```yaml
auth:
  config_secret: '配置页面登录密码'
```

行为说明：

- 如果 `config_secret` 留空，`/login` 会进入首次初始化模式，提交后写入哈希并登录
- 如果填的是明文，项目启动后会自动转换成哈希并回写到当前 `config.yaml`
- 以后访问配置页面时，需要先打开 `/login`，输入控制台密码登录
- 如果要改 secret，直接把 `config_secret` 改成新的明文，再重启服务即可
- 配置页登录默认按来源 IP 做简单限流：连续失败 5 次后锁定 600 秒，可通过 `auth.config_login_max_failures` 和 `auth.config_login_lock_seconds` 调整
