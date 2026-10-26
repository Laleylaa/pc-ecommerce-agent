以下是成对条件片段，不是完整输出，也不代表店内商品：
1. “蓝牙和2.4G都要有”：
conditions=[{"key":"connection_type","operator":"eq","values":["蓝牙"],"strength":"requirement","evidence":"蓝牙和2.4G都要有"},
{"key":"connection_type","operator":"eq","values":["2.4G"],"strength":"requirement","evidence":"蓝牙和2.4G都要有"}]。
两项AND；用in表达此原文是错误，审核应revise。上述两个eq正确，审核应approve。
2. “蓝牙或者2.4G都行”：
conditions=[{"key":"connection_type","operator":"in","values":["蓝牙","2.4G"],"strength":"requirement","evidence":"蓝牙或者2.4G都行"}]。
这是OR；不能因为出现“都”字就判AND。上述in正确，审核应approve。
3. “蓝牙必须有，最好也支持2.4G”：
conditions=[{"key":"connection_type","operator":"eq","values":["蓝牙"],"strength":"requirement","evidence":"蓝牙必须有"},
{"key":"connection_type","operator":"eq","values":["2.4G"],"strength":"preference","evidence":"最好也支持2.4G","preference_evidence":"最好也支持2.4G"}]。
前者决定准入，后者只排序；把两项都当硬条件或都当任选均应revise。
4. “三模鼠标，蓝牙也要支持”：eq三模 + eq蓝牙可正确保留，审核应approve。
这些是能力谓词，三模记录可同时满足两项；不能按字符串必须同时等于两种值而误判冲突。
反向不成立：“蓝牙和2.4G都要有”不能直接改写成“三模”，不得增加原文没要求的配置。
5. “支持无线充电”：保留unresolved_requirements=
[{"evidence":"支持无线充电","reason":"spec_not_carried","strength":"requirement"}]。
完整目录未销售此规格；保留该条件并查询相近候选的计划应approve，替换成wireless=是应revise。
“支持无线连接”才按连接能力表达，不能把连接和充电混为一谈。
审核以上片段时仍要核对实际原文的其他要求；生成时补齐schema要求，不能复制无关条件。

6. “推荐办公用键盘”：用途不能直接映射具体规格，保留reason=requirement_needs_clarification；不得声称本店没有办公键盘。
7. “无线显示器”：具体规格不是模糊用途；保留wireless=是条件（若字段已定义），由实际完整记录判不满足；不能改成带Wi-Fi或USB-C。
