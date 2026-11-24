# Travel 首版数据源和技术框架

记录日期：2026-11-19。适用范围：单程、1 位成人、经济舱的机票比价导购。

首版用 **FastAPI + SQLite 做查询服务，Flutter 做 Android 客户端**。沿用仓库里的 Python 采集器做内部原型。App 统一请求自己的服务，按同一航班展示各卖家的报价，再跳到原平台。库存、支付、出票和票务售后仍由购票渠道处理。

## 实测数据源

使用现有 Python 适配器，通过本机可用代理复测。Trip.com 的浏览器回退在本轮关闭。下表的数字是适配器返回的条目数，对照数是携程公开起价匹配到至少一个其他卖家的行程数。

| 航线和出发日 | 携程公开起价 | 同程 | Trip.com | 携程起价对照数 |
|---|---:|---:|---:|---:|
| 广州 → 北京，11 月 20 日 | 10 | 查询失败 | 125 | 0 |
| 广州 → 东京，11 月 20 日 | 10 | 58 | 82 | 10 |
| 莫斯科 → 深圳，11 月 25 日 | 10 | 16 | 37 | 10 |

- **Trip.com** 作为首版主来源。三条样本均返回完整航班号、卖家和价格，现有适配器的跳转链接落到航线与日期搜索页。
- **同程** 作为第二个可比来源，优先用于已验证的国际航线。链接可携带具体航班标识。在莫斯科到深圳的样本中，入口返回了 HTTP 200，仍需在手机浏览器上核对页面加载后的航班与价格。
- **携程公开页** 做近期起价参考。广州到北京本轮只有 Trip.com 提供含税报价。携程公开页的跳转是航线和日期页，部分日期会重定向。
- **Google Flights** 和 **Kiwi** 保留实验。莫斯科到深圳补测中，Google Flights 列表返回 5 条参考结果，缺少可直接比价的航班号和实际卖家。Kiwi 返回空结果。桌面携程和 Google 预订选项类的浏览器适配器仍可用于研究。

这轮验证覆盖网页取数和返回链接的结构，未核实各平台最终订单金额，也未取得正式报价接口凭据。发布前要逐一确认渠道能否提供持续、可公开展示的价格和跳转能力。

## 官方接入顺序

| 优先级 | 渠道 | 决定 |
|---|---|---|
| 1 | Trip.com 联盟 | 优先验证航班推广链接和订单归因。申请联盟与取得稳定的报价 API 是两项工作，分别确认。 [官方联盟](https://www.trip.com/partners/index) · [订单与佣金 FAQ](https://www.trip.com/partners/help/faq/report?locale=en_xx) |
| 2 | 同程、携程、南航官网 | 分别核实合作报价和跳转条件。南航优先验证莫斯科到深圳的 CZ8372 直飞，取得班次价格和链接后再进入比价。 [南航官网](https://csair.com/en/) |
| 3 | 飞猪国内机票导购 | 有国内机票搜索接口文档，需要平台分配的 AppKey 和签名。先确认当前申请资格、可实际调用和展示的字段，再决定是否接国内航线。 [飞猪开放平台文档](https://open.alitrip.com/doc2/apiDetail.htm?apiId=23247) |
| 观察 | Aviasales、Ozon Travel | Aviasales 实时报价 API 当前要求至少 5 万月活。其 Data API 和 Ozon 已找到的日历价格可作为路线参考，先核实卖家和逐航班价格再做正式对照。 [Aviasales 准入](https://support.travelpayouts.com/hc/en-us/articles/210995808-How-to-get-access-to-the-Aviasales-Search-API) · [Aviasales Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API) · [Ozon 航线页](https://www.ozon.ru/travel/flight/moskva-mow/shenchzhen-baoan-szx/) |
| 后续 | Skyscanner Live Prices | 官方合作申请页目前列出 10 万月活门槛和其他审核条件，留到有足够流量时评估。 [合作申请条件](https://www.partners.skyscanner.net/contact/travel-api) |

## 第一版架构

| 层 | 决定 |
|---|---|
| 客户端 | Flutter，先做 Android 真机验证。用 HTTPS 请求 Travel API，支持搜索、逐步显示报价和打开购票链接。iOS 后续沿用同一接口，需 macOS 构建环境。 [Flutter 平台文档](https://docs.flutter.dev/platform-integration) |
| 接口 | Python FastAPI，复用 `sources/` 和 `core/`，CLI 与 App 共用查询与比价逻辑。先做 `POST /v1/searches` 返回搜索 ID，客户端通过 `GET /v1/searches/{id}` 获取陆续返回的结果。 [FastAPI 文档](https://fastapi.tiangolo.com/tutorial/first-steps/) |
| 查询执行 | 服务端并行查询有覆盖的渠道，按渠道设置并发和超时上限，并合并相同的在途查询。渠道状态区分查询中、有报价、暂无报价、不覆盖和查询失败。App 可以先展示返回的报价。 |
| 存储 | 单机 SQLite，保存搜索任务和允许保留的报价快照。相同条件可短时复用结果，初始上限设为 2 分钟，遇到更短的渠道有效期时以渠道为准。跨实例后再评估 PostgreSQL 和独立队列。 [SQLite WAL 文档](https://www.sqlite.org/wal.html) |
| 网络 | App 只访问 Travel 服务，购票时打开原平台。采集服务使用部署环境的网络配置，本机测试用代理属于调试参数。首版服务只运行已验证能通过 HTTP 取数的路径。 |

从现有 `TripOffer` 演进报价模型，保留来源、实际卖家、完整航段、舱等、原币价格、折算汇率与时间、税费口径、行李条件、采集时间和报价链接。为链接标注目标类型，区分具体行程和航线搜索页。

同航班匹配以完整航段为准，核对航班号、实际机场、当地出发日期和时刻。对齐舱等及价格口径后，才参与同航班最低价排序。行李或退改信息缺失时标注未知。公开起价和日历低价放在参考位。至少有两家不同卖家提供可比报价时，才显示“本次已查渠道最低”。

## 下一轮验收

1. 先把广州到东京、莫斯科到深圳做成第一轮端到端样本。广州到北京用于验证单渠道或部分渠道失败时的展示。
2. 先跑同程和 Trip.com 的含税价对照，再加携程公开起价参考。对每条国际样本至少抽查一个同班次报价，并在 Android 真机上打开对应入口，核对航班号、日期、舱等和点开后的价格。搜索页链接在 App 中标为“去平台查价”。
3. 确认一个渠道超时或失败时，其他报价仍然可见，用户能看到已查范围和报价时间。完成这几项后，再推进更多渠道和下一阶段的历史价格。
