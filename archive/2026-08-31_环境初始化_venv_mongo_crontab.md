# 2026-08-31 环境初始化：venv重建 + MongoDB部署 + 依赖安装 + crontab适配

## 改了什么

### 1. venv 重建
- 原因：旧 venv 的 pip shebang 指向已不存在的 `/data/stock_recommender_system/venv/bin/python3`，bad interpreter
- 系统 python 仅 3.9（无 3.10+），`requirements.txt` 未锁版本，新版 numpy/pip 在 3.9 上编译失败
- 操作：`rm -rf venv`（用户授权）→ `/usr/bin/python3 -m venv --system-site-packages venv`
- 升级 pip 到 24.0（`python -m pip install --upgrade "pip<24.1"`，24.1+ 需 3.10+）

### 2. Python 3.9 兼容性补丁（2个文件）
- `from datetime import UTC, datetime` 在 3.9 报 ImportError（UTC 常量是 3.11+ 才有）
- 改为 try/except 回退 `timezone.utc`：
  - `scripts/data_capability_service.py:11`
  - `scripts/factor_data_import_service.py:14`
- 语义等价，3.11+ 走原路径，3.9/3.10 走回退

### 3. MongoDB 7.0.11 部署
- 二进制包下载到 `/data2/liuyu20/mystock/mongobin/`（非系统目录，不污染）
- 数据目录 `/data2/liuyu20/mystock/mongodata/`
- 日志 `/data2/liuyu20/mystock/logs/mongod.log`
- 启动：`mongod --dbpath ... --logpath ... --fork --bind_ip 127.0.0.1 --port 27017`
- 配置 URI（config_complete.yaml）：`mongodb://localhost:27017/tradingagents`
- ⚠️ 隐患：`--fork` 启动，机器重启后 mongod 不自动恢复，crontab 任务会因连不上DB失败

### 4. Python 依赖安装（venv 内）
已装（3.9 兼容版本）：
- numpy 1.26.4, pandas 2.0.3, PyYAML 6.0.3, pymongo 4.17.0
- akshare 1.18.88 + 依赖：beautifulsoup4, html5lib, xlrd, tqdm, openpyxl, jsonpath, tabulate, decorator, py-mini-racer 0.6.0, akracer 0.0.14, lxml 5.1.0(系统)
- curl_cffi 0.7.4（akshare 声明需 ≥0.13，但 0.7.4 能正常 import，功能可用；pip 有冲突警告但不影响运行）

未装：scikit-learn（requirements 列了但 review 链路未直接用，暂未装）、tushare（脚本内 import tushare 失败会走兜底，未装）

### 5. crontab 适配
- 模板保存：`config/crontab_stock.txt`
- 适配点：路径改为 `/data2/liuyu20/mystock`，python 改为 `venv/bin/python`，去掉 macOS 的 `arch -x86_64`
- `compute-signals` 子命令本机不存在，已跳过（原 19:00 那条只保留 import-universe-quotes）
- 已 `crontab config/crontab_stock.txt` 装载，crond 服务 running

## 验证
- `venv/bin/python -c "import akshare"` OK，版本 1.18.88
- pymongo ping mongo 返回 ok
- Tushare token 有效，hk_hold 接口能取 01810.HK 南向持股数据（8条）
- `portfolio_strategy.py review` 跑通（MongoDB 空库时因子数据为 N/A，符合预期）

## 局限
1. MongoDB 空库，需等 crontab 跑数天后才有完整因子/资金数据；或手动跑 `import-all`
2. mongod 非开机自启，重启即丢
3. curl_cffi 版本不满足 akshare 声明（0.7.4 < 0.13），目前能跑，后续若 akshare 升级可能需处理
4. scikit-learn/tushare 未装，部分功能（因子权重分析、tushare专属信号）会走兜底或不可用
