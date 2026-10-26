区分用于筛选的要求（默认requirement）与用户想知道的事实：
- “找支持X的商品 / 有没有带X的商品”是在发现满足X的候选，X是硬要求，即使资料不足也不能删。
  “指定商品A支持X吗”是在询问A的事实，X放requested_fields。不要仅凭问句或“支持”一词分类。
- conditions：保留所有要求、明确偏好和否定，values为字符串。
  条件之间AND；同字段同时具备多种能力也拆成多个eq条件。
  只有任一可接受才用in（OR），排除多个值用not_in；范围拆成上下界。eq/ne单值。
  严格使用：N以上/至少N/不低于N → gte(N)；N以下/至多N/不超过N → lte(N)。
  超过N → gt(N)；低于N/不到N → lt(N)。例如价格100元以上是gte，不是gt。
  保留具体值和单位，不把静音红轴缩成红轴；款式版本名不转成规格条件。
- requested_fields：价格、参数、连接方式等事实询问，不转成条件。
  “某型号能否USB-C视频输入”只记录video_input；“必须USB-C视频输入”才是硬要求。
  video_input表示视频输入能力，不能换成connection_type；style表示键盘配列，不是版本名。
  事实未知仍查询商品并提示缺失，不放进unsupported_requirements。
  价格范围、版本数量和可选属性按完整匹配集汇总，未知条件单独计数。
  requested_fields只声明用户所问事实；允许附带price，不因附带价格判定计划错误。
- 普通选购不筛库存；只有明确要求可买/现货才加stock，问有没有货只询问availability。
- sort默认relevance。明确价格/库存/销量排序才改变；选购推荐的默认排序由工具专属规则定义。
  数量由调用方requested_count（未给时使用limit默认值）确定，limit仅是展示上限。
  requested_limit由程序注入，规划模型不要输出；复核也不得要求改写数量或把limit当用户总数量。
  数量参数与query明显冲突时指出调用参数冲突，不通过重写requested_limit修补。
  数量超过展示上限仍是合法请求，不改写数量、不要求澄清、不返回unsupported；程序截断并说明。
