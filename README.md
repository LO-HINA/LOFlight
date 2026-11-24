# Travel 机票比价平台

Travel 把同一航班的各平台报价放在一起。用户找到合适的票价后，前往原平台购票。支付、出票和售后由购票平台处理。
学习项目，没有其他用途

## 运行 Web 版

```powershell
python -m pip install -r requirements.txt
python web.py
```

打开 [本地查价页](http://127.0.0.1:8765/)。首页提供带采集日期的历史样本，输入城市和日期后点击「实时查价」即可查询单程、1 位成人、经济舱的报价。直接打开 `prototype/index.html` 也能离线查看样本。

| 渠道 | Web 展示的报价 |
| --- | --- |
| Trip.com | 含税报价 |
| 同程旅行 | 国际航线含税报价，国内航线展示参考价 |
| 携程 | 公开页起价，作为参考价 |

页面支持直飞、中转和多渠道筛选。两家及以上提供同一航班的含税报价时，页面展示价差。价格、税费和行李规则以原平台购票页为准。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `web.py` | FastAPI 入口，提供网页和查价 API |
| `prototype/` | Web 页面、样式和图片 |
| `sources/` | Web 使用的 Trip.com、同程国内／国际和携程公开页适配器 |
| `sources/cli/` | 命令行使用的携程桌面、Google Flights、Kiwi 和铁路适配器 |
| `core/` | 城市注册表、数据结构、时区、汇率和比价逻辑 |
| `data/` | 城市、机场、汇率和站点数据 |
| `cli.py` | 命令行查询入口 |
| `docs/` | 数据源验证和架构规划 |

`probe/` 与 `output/` 用于本地验证和保存结果，由 Git 忽略。

## 查询接口

- `GET /api/cities` 返回支持的城市、代码和别名。
- `GET /api/flights` 接收 `origin`、`destination`、`departure`，返回行程、报价和渠道状态。例如 `origin=CAN&destination=BJS&departure=YYYY-MM-DD`。
- 运行服务后，可在 [本地接口文档](http://127.0.0.1:8765/docs) 查看参数。后续 Android 版可复用这些接口。 [数据源与架构规划](docs/source-audit-and-architecture.md)

## 命令行

```powershell
python cli.py --help
$day = (Get-Date).AddDays(3).ToString("yyyy-MM-dd")
python cli.py 广州 东京 $day --compare --platforms ctrip-public,ly,trip --json
```

网页与命令行共享报价结构和比价逻辑。命令行的部分渠道需要本机 Chrome。
