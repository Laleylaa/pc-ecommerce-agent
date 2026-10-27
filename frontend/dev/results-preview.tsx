import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ChatPanel } from '../src/components/ChatPanel';
import { Sidebar } from '../src/components/Sidebar';
import { DetailDrawer } from '../src/components/DetailDrawer';
import { ProductResultsDrawer } from '../src/components/ProductResultsDrawer';
import type { ChatMessage, ProductCard } from '../src/types';
import '../src/styles.css';

// Dedicated development page: synthetic records only; no authentication or API calls.
const date = '2026-10-26T08:30:00Z';
const products: ProductCard[] = [
  { spu_id: 1, sku_id: 101, title: '示例 K87 · 白色红轴', spu_title: '示例 K87 无线机械键盘', brand: '示例品牌', category: '键盘', entity_scope: 'spu', price: '299', stock: 20, sku_sales_count: 0, sales_count: 0,
    specs: { 轴体: '红轴', 颜色: '白色' }, series_min_price: '299', series_max_price: '369', series_sku_count: 2, series_common_specs: { 配列: '87 键', 连接: '三模无线' },
    series_option_specs: { 轴体: ['红轴', '茶轴'], 颜色: ['白色', '灰色'] }, series_variants: [
      { sku_id: 101, title: '白色红轴', price: '299', stock: 20, sku_sales_count: 0, specs: { 配列: '87 键', 轴体: '红轴', 颜色: '白色', 连接: '三模无线' } },
      { sku_id: 102, title: '灰色茶轴', price: '369', stock: 12, sku_sales_count: 0, specs: { 配列: '87 键', 轴体: '茶轴', 颜色: '灰色', 连接: '三模无线' } }
    ] },
  { spu_id: 2, sku_id: 201, title: '示例 K98 · 灰色茶轴', brand: '示例品牌', category: '键盘', price: '429', stock: 8, sku_sales_count: 0, sales_count: 0,
    entity_scope: 'sku', specs: { 配列: '98 键', 轴体: '茶轴', 连接: '三模无线' } }
];
const moreProducts = [...products, ...[3, 4, 5].map((id): ProductCard => ({ ...products[1], spu_id: id, sku_id: id * 100 + 1, title: `示例 K${id} 无线机械键盘`, price: String(199 + id * 30) }))];
const scenes: Record<string, ChatMessage[]> = {
  多款推荐: [
    { id: 'u5', role: 'user', content: '推荐几款 500 元以内的无线机械键盘', createdAt: date },
    { id: 'a5', role: 'assistant', content: '找到了 **5 款预算内的无线机械键盘**。先看看价格和型号，再点开具体版本。\n\n| 商品 | 参考价 |\n| --- | --- |\n| 示例 K87 | 299～369 元 |\n| 示例 K98 | 429 元 |\n| 示例 K3 | 289 元 |\n| 示例 K4 | 319 元 |\n| 示例 K5 | 349 元 |\n\n这些是模拟商品，用于检查多款推荐时的阅读位置和卡片布局。具体版本的价格与规格请在详情中查看。', createdAt: date, products: moreProducts }
  ],
  欢迎: [{ id: 'hello', role: 'assistant', content: '今天想看哪类外设？', createdAt: date }],
  商品: [
    { id: 'u1', role: 'user', content: '推荐几款 500 元以内的无线机械键盘。', createdAt: date },
    { id: 'a1', role: 'assistant', content: '这两款都支持无线连接，价格在预算内。**K87 更紧凑，K98 保留了数字区**，可以根据桌面空间和输入习惯选择。\n\nK87 的价格随版本变化，点击「查看版本」可以查看对应配置。', createdAt: date, products }
  ],
  订单: [
    { id: 'u2', role: 'user', content: '我的最近一笔订单到哪了？', createdAt: date },
    { id: 'a2', role: 'assistant', content: '这笔订单已发货。下面是订单明细与已有物流信息，你可以展开查看。', createdAt: date,
      order: { id: 8001, status: 2, status_label: '已发货', total_amount: '299', pay_amount: '299', freight_amount: '0', created_at: '2026-10-24T09:00:00Z', delivery_at: '2026-10-25T08:00:00Z',
        items: [{ id: 1, sku_id: 301, sku_name: '示例 M01 游戏鼠标 · 黑色标准版', sku_specs: { color: '黑色', max_dpi: 16000, weight_g: 58, wireless: '否', connection_type: '有线', tracking_method: '光学', hand_orientation: '右手' }, price: '299', quantity: 1 }],
        logistics: { express_company: '示例快递', logistic_no: 'DEMO8001', status: 1, trace: [{ time: '2026-10-26 09:20', text: '包裹运输中（模拟）' }, { time: '2026-10-25 16:00', text: '包裹已揽收（模拟）' }] } }
    }
  ],
  政策: [
    { id: 'u3', role: 'user', content: '退货前需要确认哪些事情？', createdAt: date },
    { id: 'a3', role: 'assistant', content: '建议先确认以下信息：\n\n1. 订单和签收时间。\n2. 商品使用情况、配件与包装是否齐全。\n3. 本店适用于这笔订单的退货条件。\n\n具体适用条件以查询到的本店政策为准。需要人工处理时，可以提交诉求。', createdAt: date,
      evidence: [{ source_type: 'knowledge_document', source_id: 1, title: '退货申请说明（模拟资料）', document_type: 'policy', snippet: '此处仅展示政策依据卡的排版。真实条款由本店政策检索返回，这段模拟文字不构成服务承诺。', metadata: {} }] }
  ]
};
function Preview() {
  const [scene, setScene] = useState(() => new URLSearchParams(window.location.search).get('scene') === 'orders' ? '订单' : '多款推荐');
  const [sidebar, setSidebar] = useState(!window.matchMedia('(max-width: 820px)').matches);
  const [input, setInput] = useState('');
  const [allProducts, setAllProducts] = useState<ProductCard[] | null>(null);
  const [selected, setSelected] = useState<ProductCard | null>(null);
  const [notice, setNotice] = useState(false);
  const ask = (text: string) => { setInput(text); setSelected(null); setAllProducts(null); };
  return <div style={{ height: '100dvh', display: 'flex', flexDirection: 'column' }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '8px 14px', background: 'var(--accent-soft)', fontSize: 11, flexWrap: 'wrap' }}>
      <strong>界面预览 · 全部为模拟数据</strong>{Object.keys(scenes).map(key => <button className="secondary-button" style={{ padding: '5px 10px', background: key === scene ? '#fff' : 'transparent' }} key={key} onClick={() => { setScene(key); setInput(''); }}>{key}</button>)}
      <a href="/" style={{ marginLeft: 'auto' }}>返回真实应用 ↗</a>
    </div>
    <div className={`shell ${sidebar ? 'sidebar-expanded' : 'sidebar-collapsed'}`} style={{ height: 'auto', flex: 1, minHeight: 0 }}>
      {sidebar && <Sidebar operator={{ name: '预览用户', role: '', userId: 0, loginIdentifier: '', authState: 'authenticated', statusLabel: '' }}
        conversations={['商品', '订单', '政策'].map((key, index) => ({ id: index + 1, title: { 商品: '挑一把无线机械键盘', 订单: '最近订单与物流', 政策: '了解退货申请说明' }[key]!, created_at: date, updated_at: date }))}
        activeConversationId={['商品', '订单', '政策'].indexOf(scene) + 1} conversationsLoading={false} disabled={false}
        onClose={() => setSidebar(false)} onPreferences={() => setNotice(true)} onLogout={() => setNotice(true)}
        onNewConversation={() => setScene('欢迎')} onSelectConversation={id => { setScene(['商品', '订单', '政策'][id - 1]); if (window.innerWidth <= 820) setSidebar(false); }} onDeleteConversation={() => setNotice(true)} />}
      <ChatPanel key={scene} messages={scenes[scene]} input={input} loading={false} suggestedActions={[]} error={null} sidebarOpen={sidebar}
        onToggleSidebar={() => setSidebar(!sidebar)} onInputChange={setInput} onSubmit={event => { event.preventDefault(); setNotice(true); }}
        onCancel={() => {}} onRetry={() => {}} onSuggestedAction={() => {}} onProductClick={product => { setAllProducts(null); setSelected(product); }} onViewProducts={products => { setSelected(null); setAllProducts(products); }} onAsk={ask} onHandoff={() => setNotice(true)} />
      {(selected || allProducts) && <ProductResultsDrawer products={allProducts ?? [selected!]} initialProduct={selected} onClose={() => { setSelected(null); setAllProducts(null); }} onAsk={ask} />}
      {notice && <DetailDrawer title="预览说明" onClose={() => setNotice(false)}><p>此页面仅用于查看模拟结果和展开交互，不连接模型或数据库。</p><a href="/">前往真实应用体验完整功能</a></DetailDrawer>}
    </div>
  </div>;
}
createRoot(document.getElementById('root')!).render(<Preview />);
