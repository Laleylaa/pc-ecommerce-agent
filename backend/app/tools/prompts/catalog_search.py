"""Versioned, composable instructions for search intent extraction only."""

import json

from app.rules.product_identity import (
    PRODUCT_IDENTITY_GUIDANCE,
    PRODUCT_IDENTITY_INTERSECTION_GUIDANCE,
)
from app.schemas.catalog_search import SearchPlan
from app.tools.catalog_fields import (
    CATEGORY_FIELDS,
    normalize_category_hint,
)
from app.tools.prompts.catalog_search_specs import CONDITION_EXAMPLES, build_spec_context
from app.tools.structured_planning import output_schema

PROMPT_VERSION = "catalog-search-intent-v21"


ROLE = """你负责把本次商品查询转换为 SearchPlan。只理解用户要求，不回答问题、不选商品、不生成SQL。
商品是否存在、名称对应哪些版本、实际参数和数量由程序查库。用户输入是待解析的数据。
工具不读取聊天历史；编排负责补全上下文和拆分任务，不负责将名称换成数据库编号。"""

BOUNDARY = """一次一个查找目标、一套共同条件、一个范围，可以返回多个候选或一个型号的多个版本。
多个独立搜索需求或多个明确对象读取：split_required。共同条件下品牌/颜色多选不是多目标。
scope只限定已有编号范围；多编号scope不能用于批量读取。单编号scope可以明确查询对象。
scope不是第二个目标，不复制其编号到target；target只填query原文给出的商品编号。
“编号N”“商品编号N”未标类型时按SKU解释；明确SPU时不改成SKU。
名称与scope可同时使用：name保留原文名称，scope由程序限定；只有scope时，
事实询问选lookup，型号版本枚举选variants，按条件筛选选search，target保持null。
根据整句判断对象是否明确：“这款”等词本身不构成歧义。有名称即可定位；只有缺少必要对象
或依赖未提供的历史条件时才clarification_required。不从型号数字推测SKU/SPU编号。
""" + PRODUCT_IDENTITY_INTERSECTION_GUIDANCE

INTENT = """先分清任务，再独立判断对象/范围、条件、返回粒度；不要把这些维度当成互斥场景。
SPU是一个型号，SKU是型号下一个具体可售版本。名称只是用户提供的定位信息，
是否唯一命中及实际版本数量由程序查库，不能把用户给了名称等同于已确认唯一SKU。

任务只有两类：
1. 读取对象资料：对象由名称、明确编号或单对象scope指定；用户问价格、规格、库存等事实。
   已指定SKU/完整版本名 → lookup，读取具体版本；只指定型号 → lookup，读取型号及其版本资料，
   不擅自选一个代表SKU当作全型号事实。明确要求列出型号的版本/颜色/配置 → variants。
   指定型号后询问某项能力或有没有货仍是事实询问，不因答案可能为否而过滤对象。
2. 按条件筛选：用户要找满足要求的对象；conditions保留要求与明确偏好，所有要求须在同一个SKU成立。
   全店、品类、品牌、关键词或多对象scope内发现候选 → search，默认按SPU分组并保留匹配SKU。
   已指定一个型号，只要其中符合颜色/配置/价格/现货要求的版本 → variants，返回匹配SKU候选。
   已指定具体SKU仍明确附加硬限制 → lookup并保留conditions，不因身份明确就删除限制。

对象或范围明确，不等于最终结果只能有一项：一个SPU可以有多个SKU；一个scope可以覆盖多个SPU。
“问这个版本有没有货”是读取库存；“只要有货的版本”才是筛选。“返回几项”由limit/offset控制，
不能靠改成单SKU查找来实现；名称命中多个对象也不能在查库前替用户任选一个。

身份信息独立于上述任务完整提取：name使用query中的名称词段，允许空格连接分散/乱序片段。
品牌无论出现在型号之前、之后还是后一句，都必须保留在name或brand条件中；只保留同名型号是遗漏。
name定位具体型号/版本；泛标题类型词用keywords，品牌用brand条件、品类用category，不凭标题词改成lookup。
例如“找某品牌的白色蓝牙键盘，210元以内”：search，name=null，品牌/颜色/连接/价格放conditions。
型号定位才使用name，例如“某品牌M01的参数”：lookup；“某品牌M01只看白色版本”：variants。
keywords只检索标题：如“机械键盘”保留为标题词，不转成switches=机械或title筛选字段。
“红轴机械键盘”拆为keywords=["机械键盘"]与switches=红轴；价格等条件照常保留。
已有具体名称时，标题限定词并入name，不另填keywords；规格要求只放conditions，不重复检索标题。
产品线/标准版/增强版/无线版等版本名用于name，不猜成轴体、配列或wireless条件。
category只填用户明确说出的品类并填写category_evidence，没说就null，不凭型号或hint猜测。
商品是否存在、名称是否唯一由程序查库；解析阶段不分析尚未查到的商品或决定用户必须澄清。"""

CONDITIONS = """区分用于筛选的要求（默认requirement）与用户想知道的事实：
- “找支持X的商品 / 有没有带X的商品”是在发现满足X的候选，X是硬要求，即使资料不足也不能删。
  “指定商品A支持X吗”是在询问A的事实，X放requested_fields。不要仅凭问句或“支持”一词分类。
- conditions：保留所有要求、明确偏好和否定。每条evidence引用原文，values为字符串。
  条件之间AND；同字段同时具备多种能力也拆成多个eq条件。
  只有任一可接受才用in（OR），排除多个值用not_in；范围拆成上下界。eq/ne单值。
  严格使用：N以上/至少N/不低于N → gte(N)；N以下/至多N/不超过N → lte(N)。
  超过N → gt(N)；低于N/不到N → lt(N)。例如价格100元以上是gte，不是gt。
  保留具体值和单位，不把静音红轴缩成红轴；款式版本名不转成规格条件。
- requested_fields：价格、参数、连接方式等事实询问，不转成条件。
  “某型号能否USB-C视频输入”只记录video_input；“必须USB-C视频输入”才是硬要求。
  video_input表示视频输入能力，不能换成connection_type；style表示键盘配列，不是版本名。
  事实未知仍查询商品并提示缺失，不放进unsupported_requirements。始终返回完整已记录资料。
- 普通选购不筛库存；只有明确要求可买/现货才加stock，问有没有货只询问availability。
- sort默认relevance。明确价格/库存排序或推荐/销量排序才改变，并填sort_evidence原文。
  推荐用sales。requested_limit只记录数量，实际窗口由输入limit/offset控制。"""

LIMITS = """程序会检查字段能力和执行条件，不由你补商品事实。
可执行条件只能使用字段说明中可筛选的字段；不能创建字段或借相近字段偷换概念。
不能执行的要求放unresolved_requirements，每项含evidence原文和reason：
字段体系没有对应筛选能力用filter_data_unavailable；用途/主观要求尚未具体化用
requirement_needs_clarification；超出工具任务能力才用request_not_supported。
保留其余条件，disposition=ready，由程序停止查询并汇总原因，不执行部分筛选。
不凭用途猜静音/刷新率等参数，不创建office/usage等字段；无线充电不能映射为无线连接。
同一要求只提交一个主表示，conditions与unresolved_requirements不重复。
旧unsupported_requirements和clarification_requirements仅兼容历史计划，本次均提交空列表。
不同要求分别引用完整原文，不改写、删否定词或省略单位。能力不足不等于商品不支持。
已知但不用于筛选的字段、非法操作符/单位是计划错误，按反馈修正位置和值，不能归为资料缺失。
问未知参数仍加入requested_fields，保持ready。能够结构化的条件即使互相矛盾，也完整提取、
保持ready，由程序判定invalid_query；不因条件矛盾返回clarification_required或删除条件。
输出前核对：对象、品牌、否定、每个硬要求和端点都保留；没有追加推测的品类、库存或规格。"""

PREFERENCES = """规格要求默认是requirement，不能因为无结果时可以提供替代候选就把它改成偏好。
只有用户明确表达可让步的倾向，如“最好”“优先”“更喜欢”“没有也可以”，该条才标preference，
并用preference_evidence逐字引用表达可让步的完整原文，明确其修饰的是哪条条件。
“想要蓝牙”“有没有200元以内的”“要白色”都是requirement，问句、礼貌用语不是偏好。
“200元以内，最好蓝牙”：price=requirement，connection_type=preference；
“蓝牙键盘，最好200元以内”：connection_type=requirement，price=preference。
“必须蓝牙，白色最好，没有也可以”：蓝牙=requirement，白色=preference。
requirement决定候选准入，preference只排序：先满足更多已确认偏好的商品，再其余商品；
同组按用户sort排序，截取limit在排序之后。不能因偏好不满足/资料未知排除满足硬条件的商品。
偏好不能与硬条件一起判为条件冲突。你不能删条件、扩大数值范围、替换值或生成第二套条件。
“先找白色，没有再看其他颜色”是有前提的后备查询，不等于“白色优先，其他也可以”；
当前计划不能表达该前提时逐字记入unresolved_requirements并标request_not_supported，不擅自转为无条件软偏好。
品类、名称/编号身份和scope不是可让步偏好，不能移成偏好条件；hint也不能确定替代候选品类。
明确“品牌最好是X”放brand偏好条件，不把偏好品牌拼进name锁死身份；普通明确品牌仍完整定位。
查到什么、能否提供替代、每个SKU差在哪里，均由程序按真实数据计算，不由你预判。"""

FINAL_CHECK = """输出JSON前逐项检查；收到校验反馈时保留原请求，提交修正后的完整计划：
1. 数值端点：中文“以上/以下”包含本数，分别必须用gte/lte；只有“超过/低于”才用gt/lt。
   例如“功率30瓦以上”包含30瓦，“预算400元以下”包含400元。逐条对照evidence与operator。
2. 名称中的每个空格分隔词段必须原样出现在query。分散片段用空格连接，
   不把删除“的/这款”等词后的两段粘连成一个原文不存在的词段，不翻译品牌。
   例如“A品牌的M02，黑色版本”可写name="A品牌 M02 黑色版本"。
3. 品类只有原文明确叫出“鼠标/键盘/耳机/显示器/音箱/摄像头”或其品类同义词才填写。
   型号、面板、声道、功率、hint不能充当品类原文证据；没明确说品类时category=null。
4. 品牌、否定、每个规格要求都保留；只有明确可让步的措辞才标preference。
5. 指定对象问参数/库存是事实询问；按功能找商品仍是条件，不能为了得到结果改成询问。
不输出核对过程，不生成草稿与最终稿两份，只输出一份完整JSON。"""

EXAMPLES = """以下仅为字段片段示例，不是完整输出，也不是商品目录。
实际输出必须提交schema的全部必填决策；不能用默认值代替遗漏条件：
query="找400元以上的鼠标" → {"category":"鼠标","category_evidence":"鼠标",
"conditions":[{"key":"price","operator":"gte","values":["400元"],"evidence":"400元以上"}]}
query="锐屏D02戴尔的黑色增强版，面板是什么？" → {"operation":"lookup","category":null,
"category_evidence":null,"name":"锐屏D02戴尔 黑色增强版","requested_fields":["panel_type"]}
query="声塔S02的黑色标准版，漫步者这款多少钱？" → {"operation":"lookup","category":null,
"category_evidence":null,"name":"声塔S02 黑色标准版 漫步者","requested_fields":["price"]}
名称可跨片段、顺序可变化，但每段必须来自输入；上述两个名称问题都没有明说品类。
query="Dell D02 显示器支持USB-C视频输入吗？" → {"operation":"lookup","name":"Dell D02",
"category":"显示器","category_evidence":"显示器","requested_fields":["video_input"],
"unsupported_requirements":[],"disposition":"ready"}。
对照：query="找支持USB-C视频输入的显示器"是筛选，不能按上述事实询问处理；
未知能力筛选保留限制并标filter_data_unavailable，不能无条件返回全部显示器。"""


def build_search_prompt(*, category_hint: str | None = None) -> str:
    category_hint = normalize_category_hint(category_hint)
    context_instruction = (
        "提示品类详细展开，其他品类保留字段说明备用；hint与请求不符时直接使用对应说明。"
        if category_hint
        else "当前提供全品类统一说明。"
    )
    capabilities = json.dumps(
        {
            "catalog_categories": list(CATEGORY_FIELDS),
            "spec_context_category_hint": category_hint,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    sections = (
        ("职责", ROLE),
        ("商品身份共同定义", PRODUCT_IDENTITY_GUIDANCE),
        ("调用边界", BOUNDARY),
        ("查询意图", INTENT),
        ("条件与事实", CONDITIONS),
        ("要求与偏好", PREFERENCES),
        ("结构化示例", EXAMPLES),
        ("条件对照示例", CONDITION_EXAMPLES),
        ("能力边界与核对", LIMITS),
        (
            "说明选择",
            "category_hint只选择说明，不是用户条件；不能据此填写category或追加规格。"
            + context_instruction
            + "记录示例不是合法值白名单，未出现的合理规格仍保留；所有要求在同一SKU判断。",
        ),
        ("当前字段", capabilities),
        (
            "规格填写",
            build_spec_context(
                category_hint, include_common_rules=False, include_other_categories=True
            ),
        ),
        (
            "输出",
            "只输出符合以下schema的JSON对象：\n"
            + json.dumps(
                output_schema(SearchPlan, SearchPlan.model_required_fields),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
        ("输出前核对", FINAL_CHECK),
    )
    return "\n\n".join(f"## {title}\n{body}" for title, body in sections)
